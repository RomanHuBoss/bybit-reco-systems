from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db


@pytest.fixture()
def client_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / 'api.db'
    monkeypatch.setenv('DB_PATH', str(db_path))
    monkeypatch.setenv('ADMIN_API_KEY', 'test-admin-key')
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT')

    sys.modules.pop('app.main', None)
    app_main = importlib.import_module('app.main')
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(app_main, '_fetch_bybit_instrument_meta', lambda venue, symbol: {})

    conn = db.connect(str(db_path))
    ts_now = int(time.time())
    db.upsert_ohlcv(
        conn,
        [
            {
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'tf_sec': 60,
                'ts': ts_now - 60,
                'open': 100.0,
                'high': 101.0,
                'low': 99.0,
                'close': 100.5,
                'volume': 10.0,
            }
        ],
    )
    db.insert_tickers(
        conn,
        [
            {
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'ts': ts_now - 30,
                'last': 100.5,
                'bid': 100.4,
                'ask': 100.6,
                'vol24h': 1000.0,
                'turnover24h': 100000.0,
            }
        ],
    )
    db.insert_features(conn, 'linear', 'BTCUSDT', ts_now - 30, {'volume_z': 0.1})
    client = TestClient(app_main.app)
    try:
        yield client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop('app.main', None)



def test_api_sentiment_defaults_to_global_crypto_series(client_and_conn):
    client, conn = client_and_conn

    put_payload = {
        'scope': 'global',
        'key': 'crypto',
        'ts': 1_700_000_000,
        'sentiment': 0.35,
        'velocity': 0.12,
        'volume': 7,
        'sources': {'rss': 5},
        'tags': ['macro'],
    }
    put_resp = client.post(
        '/api/v1/sentiment',
        json=put_payload,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()['ok'] is True

    default_resp = client.get('/api/v1/sentiment')
    assert default_resp.status_code == 200
    default_body = default_resp.json()
    assert default_body['scope'] == 'global'
    assert default_body['key'] == 'crypto'
    assert default_body['items'] == [
        {
            'scope': 'global',
            'key': 'crypto',
            'ts': 1_700_000_000,
            'sentiment': 0.35,
            'velocity': 0.12,
            'volume': 7,
            'sources': {'rss': 5},
            'tags': ['macro'],
        }
    ]

    explicit_resp = client.get('/api/v1/sentiment?scope=global&key=crypto&limit=5')
    assert explicit_resp.status_code == 200
    assert explicit_resp.json() == default_body



def test_api_execute_and_trade_lifecycle_is_idempotent(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-1',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8, 'grid_spacing_pct': 0.9, 'price_range_lower': 95.0, 'price_range_upper': 105.0},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    first_exec = client.post(
        '/api/v1/recommendations/R-api-1/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert first_exec.status_code == 200
    first_body = first_exec.json()
    assert first_body['ok'] is True
    assert first_body['idempotent'] is False
    bot_id = first_body['bot_id']

    second_exec = client.post(
        '/api/v1/recommendations/R-api-1/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert second_exec.status_code == 200
    second_body = second_exec.json()
    assert second_body['ok'] is True
    assert second_body['idempotent'] is True
    assert second_body['bot_id'] == bot_id

    trade_payload = {
        'trade_id': 'T-api-1',
        'ts': ts_now + 60,
        'pnl': 12.5,
        'fee': 1.5,
        'operator': 'tester',
        'meta': {'fill_count': 2},
    }
    trade_resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json=trade_payload,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert trade_resp.status_code == 200
    trade_body = trade_resp.json()
    assert trade_body['ok'] is True
    assert trade_body['insert_result'] == 'inserted'
    assert trade_body['realized_pnl'] == pytest.approx(11.0)
    assert trade_body['realized_fee'] == pytest.approx(1.5)

    trade_dup_resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json=trade_payload,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert trade_dup_resp.status_code == 200
    trade_dup_body = trade_dup_resp.json()
    assert trade_dup_body['insert_result'] == 'duplicate'
    assert trade_dup_body['trade_count'] == 1
    assert trade_dup_body['realized_pnl_net'] == pytest.approx(11.0)

    bot_resp = client.get(f'/api/v1/bots/{bot_id}')
    assert bot_resp.status_code == 200
    bot = bot_resp.json()
    assert bot['status'] == 'running'
    assert bot['state']['trade_count'] == 1
    assert bot['state']['realized_pnl_net'] == pytest.approx(11.0)
    assert bot['state']['realized_fee'] == pytest.approx(1.5)



def test_api_recommendations_exposes_snapshot_metadata_and_status_counts(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_regime(conn, ts_now, {'vol_state': 'low', 'trend_state': 'mixed', 'risk_state': 'risk_on', 'confidence': 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-rec',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.41,
                'confidence': 0.71,
                'expected_rr': 1.2,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            },
            {
                'rec_id': 'R-api-supp',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'ETHUSDT',
                'bot_type': 'futures_grid',
                'direction': 'neutral',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.11,
                'confidence': 0.62,
                'expected_rr': 0.3,
                'risk_score': 0.1,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'suppressed',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            },
        ],
    )

    resp = client.get('/api/v1/recommendations?show_suppressed=true&min_conf=0')
    assert resp.status_code == 200
    body = resp.json()
    assert body['snapshot_ts'] == ts_now
    assert body['snapshot_age_sec'] >= 0
    assert body['snapshot_is_stale'] is False
    assert body['status_counts']['recommended'] == 1
    assert body['status_counts']['suppressed'] == 1
    assert body['no_trade'] is False
    assert len(body['items']) == 2


def test_api_recommendations_supports_latest_visible_snapshot_mode(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_regime(conn, ts_now, {"vol_state": "low", "trend_state": "mixed", "risk_state": "risk_on", "confidence": 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-old-visible",
                "ts": ts_now - 120,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.41,
                "confidence": 0.71,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "ok", "mode": "advisory"}},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now - 120,
            },
            {
                "rec_id": "R-new-blocked",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "ETHUSDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.05,
                "confidence": 0.4,
                "expected_rr": 0.1,
                "risk_score": 0.1,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "pending", "mode": "advisory"}},
                "blocks": [{"code": "TEST"}],
                "status": "blocked",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            },
        ],
    )

    resp = client.get('/api/v1/recommendations?snapshot=latest_visible&min_conf=0')
    assert resp.status_code == 200
    body = resp.json()
    assert body['snapshot_mode'] == 'latest_visible'
    assert body['snapshot_ts'] == ts_now - 120
    assert len(body['items']) == 1
    assert body['items'][0]['rec_id'] == 'R-old-visible'
    assert body['llm_status_counts']['ok'] == 1



def test_api_recommendations_supports_latest_llm_ready_snapshot_mode(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_regime(conn, ts_now, {"vol_state": "low", "trend_state": "mixed", "risk_state": "risk_on", "confidence": 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-old-reviewed",
                "ts": ts_now - 120,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.41,
                "confidence": 0.71,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "ok", "mode": "advisory"}},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now - 120,
            },
            {
                "rec_id": "R-new-pending",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "ETHUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.38,
                "confidence": 0.69,
                "expected_rr": 1.0,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "pending", "mode": "advisory"}},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            },
        ],
    )

    resp = client.get('/api/v1/recommendations?snapshot=latest_llm_ready&min_conf=0')
    assert resp.status_code == 200
    body = resp.json()
    assert body['snapshot_mode'] == 'latest_llm_ready'
    assert body['snapshot_ts'] == ts_now - 120
    assert body['items'][0]['rec_id'] == 'R-old-reviewed'
    assert body['llm_status_counts']['ok'] == 1


def test_api_health_exposes_llm_reviewer_config(client_and_conn, monkeypatch):
    client, _ = client_and_conn

    resp = client.get('/api/v1/health/symbols')
    assert resp.status_code == 200
    body = resp.json()
    assert 'llm_reviewer' in body
    assert body['llm_reviewer']['enabled'] is False
    assert body['llm_reviewer']['provider'] == 'ollama'


def test_api_health_uses_explicit_llm_env_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / 'api.db'
    monkeypatch.setenv('DB_PATH', str(db_path))
    monkeypatch.setenv('ADMIN_API_KEY', 'test-admin-key')
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT')
    monkeypatch.setenv('LLM_REVIEWER_ENABLED', '1')
    monkeypatch.setenv('LLM_REVIEWER_MAX_CANDIDATES', '60')
    monkeypatch.setenv('LLM_REVIEWER_CANDLES_PER_TF', '40')
    monkeypatch.setenv('LLM_REVIEWER_CADENCE_SEC', '420')
    monkeypatch.setenv('LLM_REVIEWER_MAX_WORKERS', '12')

    sys.modules.pop('app.main', None)
    app_main = importlib.import_module('app.main')
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app)
    try:
        resp = client.get('/api/v1/health/symbols')
        assert resp.status_code == 200
        body = resp.json()
        assert body['llm_reviewer']['enabled'] is True
        assert body['llm_reviewer']['max_candidates'] == 60
        assert body['llm_reviewer']['candles_per_tf'] == 40
        assert body['llm_reviewer']['max_workers'] == 12
        assert body['llm_reviewer']['cadence_sec'] == 420
    finally:
        client.close()
        conn.close()
        sys.modules.pop('app.main', None)


def test_api_status_reports_actual_inference_mode(client_and_conn):
    client, _ = client_and_conn

    resp = client.get('/api/v1/status')
    assert resp.status_code == 200
    body = resp.json()
    assert body['inference_ready_bot_count'] == 0
    assert body['confidence_mode_in_use'] == 'raw_only'
    assert body['inference_calibration_mode'] == 'raw_only'


def test_env_example_llm_reviewer_defaults_match_runtime_defaults():
    env_map = {}
    for line in Path('.env.example').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        env_map[key.strip()] = value.strip()

    assert env_map['LLM_REVIEWER_ENABLED'] == '0'
    assert env_map['LLM_REVIEWER_CANDLES_PER_TF'] == '32'
    assert env_map['LLM_REVIEWER_MAX_CANDIDATES'] == '24'
    assert env_map['LLM_REVIEWER_MAX_WORKERS'] == '2'
    assert env_map['LLM_REVIEWER_CADENCE_SEC'] == '300'
    assert env_map['LLM_REVIEWER_KEEP_ALIVE'] == '90s'


def test_load_settings_ignores_inactive_venue_symbol_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('VENUES', 'linear')
    monkeypatch.delenv('SYMBOLS_LINEAR', raising=False)
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT,ETHUSDT')

    from app.settings import load_settings

    settings = load_settings()

    assert settings.venues == ['linear']
    assert settings.symbols_linear == []
    assert settings.symbols_linear == ['BTCUSDT', 'ETHUSDT']


def test_api_health_only_reports_active_venues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / 'api.db'
    monkeypatch.setenv('DB_PATH', str(db_path))
    monkeypatch.setenv('ADMIN_API_KEY', 'test-admin-key')
    monkeypatch.setenv('VENUES', 'linear')
    monkeypatch.delenv('SYMBOLS_LINEAR', raising=False)
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT')

    sys.modules.pop('app.main', None)
    app_main = importlib.import_module('app.main')
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app)
    try:
        resp = client.get('/api/v1/health/symbols')
        assert resp.status_code == 200
        body = resp.json()
        assert body['venues'] == ['linear']
        assert body['summary']['disabled'] == 0
        assert {item['venue'] for item in body['symbols']} == {'linear'}
    finally:
        client.close()
        conn.close()
        sys.modules.pop('app.main', None)


def test_api_recommendations_counts_missing_llm_review_as_none_and_not_pending(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_regime(conn, ts_now, {"vol_state": "low", "trend_state": "mixed", "risk_state": "risk_on", "confidence": 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-none-1",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.41,
                "confidence": 0.71,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            },
            {
                "rec_id": "R-ok-1",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "ETHUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.39,
                "confidence": 0.69,
                "expected_rr": 1.0,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "ok", "mode": "advisory"}},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            },
            {
                "rec_id": "R-pending-1",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "SOLUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.38,
                "confidence": 0.68,
                "expected_rr": 0.9,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "pending", "mode": "advisory"}},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            },
        ],
    )

    resp = client.get('/api/v1/recommendations?top_n=1&min_conf=0&snapshot=latest')
    assert resp.status_code == 200
    body = resp.json()
    assert len(body['items']) == 1
    assert body['llm_status_counts']['none'] == 1
    assert body['llm_status_counts']['ok'] == 1
    assert body['llm_status_counts']['pending'] == 1


def test_api_duplicate_trade_does_not_apply_new_side_effects(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-dup-sidefx',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    exec_resp = client.post(
        '/api/v1/recommendations/R-api-dup-sidefx/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    first_trade = {
        'trade_id': 'T-api-dup-sidefx',
        'ts': ts_now + 60,
        'pnl': 8.0,
        'fee': 1.0,
        'operator': 'tester',
        'meta': {'fill_count': 1},
        'stop_bot': False,
    }
    first_trade_resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json=first_trade,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert first_trade_resp.status_code == 200
    assert first_trade_resp.json()['insert_result'] == 'inserted'

    duplicate_with_new_side_effect = dict(first_trade)
    duplicate_with_new_side_effect['stop_bot'] = True
    dup_resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json=duplicate_with_new_side_effect,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert dup_resp.status_code == 200
    dup_body = dup_resp.json()
    assert dup_body['insert_result'] == 'duplicate'
    assert dup_body['idempotent'] is True
    assert dup_body['bot_status'] == 'running'
    assert dup_body['trade_count'] == 1

    bot_resp = client.get(f'/api/v1/bots/{bot_id}')
    assert bot_resp.status_code == 200
    bot = bot_resp.json()
    assert bot['status'] == 'running'
    assert bot['state']['trade_count'] == 1
    assert bot['state'].get('stop_reason') is None

    decisions_resp = client.get('/api/v1/decisions?limit=20')
    assert decisions_resp.status_code == 200
    trade_logs = [row for row in decisions_resp.json() if row['action'] == 'TRADE_RECORDED' and row['details'].get('trade_id') == 'T-api-dup-sidefx']
    assert len(trade_logs) == 1


def test_api_duplicate_trade_is_idempotent_when_meta_key_order_changes(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-dup-meta-order',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    exec_resp = client.post(
        '/api/v1/recommendations/R-api-dup-meta-order/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    first_trade = {
        'trade_id': 'T-api-dup-meta-order',
        'ts': ts_now + 60,
        'pnl': 5.0,
        'fee': 0.5,
        'operator': 'tester',
        'meta': {'fills': {'maker': 1, 'taker': 2}, 'tags': ['grid', 'retry']},
        'stop_bot': False,
    }
    first_trade_resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json=first_trade,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert first_trade_resp.status_code == 200
    assert first_trade_resp.json()['insert_result'] == 'inserted'

    duplicate_trade = {
        **first_trade,
        'meta': {'tags': ['grid', 'retry'], 'fills': {'taker': 2, 'maker': 1}},
    }
    dup_resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json=duplicate_trade,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert dup_resp.status_code == 200
    dup_body = dup_resp.json()
    assert dup_body['insert_result'] == 'duplicate'
    assert dup_body['idempotent'] is True
    assert dup_body['trade_count'] == 1



def test_api_execute_expired_recommendation_marks_row_expired_and_creates_no_bot(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-expired-exec',
                'ts': ts_now - 300,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1,
                'model_version': 'test',
                'features_ref_ts': ts_now - 300,
            }
        ],
    )

    resp = client.post(
        '/api/v1/recommendations/R-api-expired-exec/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert resp.status_code == 409
    assert resp.json()['detail'] == 'recommendation already expired'

    rec = db.get_recommendation_by_id(conn, 'R-api-expired-exec')
    assert rec is not None
    assert rec['status'] == 'expired'
    assert db.get_bot_by_origin_rec(conn, 'R-api-expired-exec') is None



def test_api_execute_is_blocked_by_live_risk_limits_without_creating_bot(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.upsert_risk_limits(
        conn,
        'risk-live-block',
        {
            'max_concurrent_bots': 10,
            'max_daily_dd_usdt': 1_000_000.0,
            'cooldown_after_loss_min': 0,
            'max_symbol_bots': 1,
        },
    )
    db.insert_bot_instance(
        conn,
        {
            'bot_id': 'B-risk-existing',
            'started_ts': ts_now - 60,
            'stopped_ts': None,
            'venue': 'linear',
            'symbol': 'BTCUSDT',
            'bot_type': 'futures_grid',
            'mode': {'account_mode': 'one_way', 'margin_mode': 'isolated', 'direction': 'long'},
            'params': {'grid_levels': 8},
            'state': {'seed': 'existing'},
            'status': 'running',
            'origin_rec_id': 'R-risk-existing',
        },
    )
    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-risk-blocked-exec',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    resp = client.post(
        '/api/v1/recommendations/R-api-risk-blocked-exec/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert resp.status_code == 409
    assert 'execution blocked by current risk limits' in resp.json()['detail']
    assert 'MAX_SYMBOL_BOTS' in resp.json()['detail']

    assert db.get_bot_by_origin_rec(conn, 'R-api-risk-blocked-exec') is None
    rec = db.get_recommendation_by_id(conn, 'R-api-risk-blocked-exec')
    assert rec is not None
    assert rec['status'] == 'recommended'
    decisions_resp = client.get('/api/v1/decisions?limit=50')
    assert decisions_resp.status_code == 200
    blocked_logs = [
        row for row in decisions_resp.json()
        if row['action'] == 'EXECUTION_BLOCKED' and row['rec_id'] == 'R-api-risk-blocked-exec'
    ]
    assert len(blocked_logs) == 1


def test_api_recommendations_supports_latest_operator_snapshot_mode(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_regime(conn, ts_now, {"vol_state": "low", "trend_state": "mixed", "risk_state": "risk_on", "confidence": 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-older-reviewed",
                "ts": ts_now - 120,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.41,
                "confidence": 0.71,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "ok", "mode": "advisory"}},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now - 120,
            },
            {
                "rec_id": "R-latest-pending",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "ETHUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.44,
                "confidence": 0.75,
                "expected_rr": 1.1,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "pending", "mode": "advisory"}},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            },
        ],
    )

    resp = client.get('/api/v1/recommendations?snapshot=latest_operator&min_conf=0')
    assert resp.status_code == 200
    body = resp.json()
    assert body['snapshot_mode'] == 'latest_operator'
    assert body['snapshot_ts'] == ts_now - 120
    assert body['items'][0]['rec_id'] == 'R-older-reviewed'


def test_api_recommendations_latest_operator_respects_non_actionable_filters(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_regime(conn, ts_now, {'vol_state': 'low', 'trend_state': 'mixed', 'risk_state': 'risk_on', 'confidence': 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-older-reviewed-visible',
                'ts': ts_now - 120,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.41,
                'confidence': 0.71,
                'expected_rr': 1.2,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {'llm_review': {'status': 'ok', 'mode': 'advisory'}},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now - 120,
            },
            {
                'rec_id': 'R-latest-pending-only',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'ETHUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.33,
                'confidence': 0.55,
                'expected_rr': 0.9,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {'queue': 'awaiting_review'},
                'blocks': [],
                'status': 'pending',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            },
        ],
    )

    resp = client.get('/api/v1/recommendations?snapshot=latest_operator&min_conf=0&show_recommended=false&show_pending=true')
    assert resp.status_code == 200
    body = resp.json()
    assert body['snapshot_mode'] == 'latest_operator'
    assert body['snapshot_ts'] == ts_now
    assert [item['rec_id'] for item in body['items']] == ['R-latest-pending-only']


def test_api_status_and_health_report_large_batch_llm_default_capacity(client_and_conn):
    client, _ = client_and_conn

    status_resp = client.get('/api/v1/status')
    assert status_resp.status_code == 200
    assert status_resp.json()['llm_reviewer']['max_candidates'] == 24

    health_resp = client.get('/api/v1/health/symbols')
    assert health_resp.status_code == 200
    assert health_resp.json()['llm_reviewer']['max_candidates'] == 24


def test_api_details_and_decisions_tolerate_corrupt_json_rows(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-corrupt-1',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {'ok': True},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )
    conn.execute(
        "UPDATE recommendations SET reasons_json=?, blocks_json=? WHERE rec_id=?",
        ('{bad json', '{bad json', 'R-corrupt-1'),
    )
    conn.execute(
        "INSERT INTO decision_log(ts, action, rec_id, operator, details_json) VALUES(?,?,?,?,?)",
        (ts_now, 'BROKEN_JSON', None, None, '{bad json'),
    )
    conn.commit()

    details_resp = client.get('/api/v1/recommendations/R-corrupt-1')
    assert details_resp.status_code == 200
    details = details_resp.json()
    assert details['reasons'] == {}
    assert details['blocks'] == []

    decisions_resp = client.get('/api/v1/decisions?limit=5')
    assert decisions_resp.status_code == 200
    decisions = decisions_resp.json()
    broken = next(item for item in decisions if item['action'] == 'BROKEN_JSON')
    assert broken['details'] == {}



def test_api_decisions_clamps_negative_limit(client_and_conn):
    client, conn = client_and_conn

    base_ts = db.now_ts() + 10
    for idx in range(3):
        conn.execute(
            "INSERT INTO decision_log(ts, action, rec_id, operator, details_json) VALUES(?,?,?,?,?)",
            (base_ts + idx, f"ACTION_{idx}", None, None, json.dumps({"idx": idx})),
        )
    conn.commit()

    resp = client.get('/api/v1/decisions?limit=-5')
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]['action'] == 'ACTION_2'



def test_instrument_meta_failures_are_short_term_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / 'meta-cache.db'
    monkeypatch.setenv('DB_PATH', str(db_path))
    monkeypatch.setenv('ADMIN_API_KEY', 'test-admin-key')
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT')

    sys.modules.pop('app.main', None)
    app_main = importlib.import_module('app.main')

    calls = {'count': 0}

    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_instrument_info(self, category: str, symbol: str):
            calls['count'] += 1
            raise RuntimeError('boom')

        def close(self):
            return None

    monkeypatch.setattr(app_main, 'BybitPublicClient', FailingClient)
    app_main._instrument_meta_cache.clear()

    try:
        first = app_main._fetch_bybit_instrument_meta('linear', 'BTCUSDT')
        second = app_main._fetch_bybit_instrument_meta('linear', 'BTCUSDT')
    finally:
        sys.modules.pop('app.main', None)

    assert first == {}
    assert second == {}
    assert calls['count'] == 1


def test_api_recommendations_uses_single_recommendations_query_for_llm_counts(client_and_conn, monkeypatch: pytest.MonkeyPatch):
    client, conn = client_and_conn
    import app.main as app_main

    ts_now = int(time.time())
    db.insert_regime(conn, ts_now, {"vol_state": "low", "trend_state": "mixed", "risk_state": "risk_on", "confidence": 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-ok",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.41,
                "confidence": 0.71,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {"llm_review": {"status": "ok", "mode": "advisory"}},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            }
        ],
    )

    original_get_recommendations = app_main.db.get_recommendations
    calls: list[int] = []

    def wrapped_get_recommendations(*args, **kwargs):
        calls.append(int(kwargs.get("top_n", -1)))
        return original_get_recommendations(*args, **kwargs)

    monkeypatch.setattr(app_main.db, "get_recommendations", wrapped_get_recommendations)

    resp = client.get('/api/v1/recommendations?top_n=1&min_conf=0&snapshot=latest')
    assert resp.status_code == 200
    assert resp.json()['llm_status_counts']['ok'] == 1
    assert len(calls) == 1
    assert calls[0] >= 1


def test_api_trade_retry_after_stop_bot_is_idempotent(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-stop-retry',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    exec_resp = client.post(
        '/api/v1/recommendations/R-api-stop-retry/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    trade_payload = {
        'trade_id': 'T-api-stop-retry',
        'ts': ts_now + 60,
        'pnl': 5.0,
        'fee': 0.5,
        'operator': 'tester',
        'meta': {'fill_count': 1},
        'stop_bot': True,
    }

    first = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json=trade_payload,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert first.status_code == 200
    assert first.json()['insert_result'] == 'inserted'
    assert first.json()['bot_status'] == 'stopped'

    second = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json=trade_payload,
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert second.status_code == 200
    body = second.json()
    assert body['insert_result'] == 'duplicate'
    assert body['idempotent'] is True
    assert body['bot_status'] == 'stopped'
    assert body['trade_count'] == 1
    assert body['realized_pnl_net'] == pytest.approx(4.5)


def test_api_trade_rejects_non_finite_numbers(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-inf',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    exec_resp = client.post(
        '/api/v1/recommendations/R-api-inf/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    bot_id = exec_resp.json()['bot_id']

    bad_trade = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json={'trade_id': 'T-bad', 'ts': ts_now + 60, 'pnl': 'inf', 'fee': 0.1},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert bad_trade.status_code == 422


def test_api_recommendations_sanitizes_non_finite_min_conf(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_regime(conn, ts_now, {'vol_state': 'low', 'trend_state': 'mixed', 'risk_state': 'risk_on', 'confidence': 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-minconf',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.41,
                'confidence': 0.71,
                'expected_rr': 1.2,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    resp = client.get('/api/v1/recommendations?min_conf=nan')
    assert resp.status_code == 200
    body = resp.json()
    assert body['min_conf'] == pytest.approx(0.52)
    assert len(body['items']) == 1




def test_api_sentiment_rejects_negative_volume(client_and_conn):
    client, _ = client_and_conn

    resp = client.post(
        '/api/v1/sentiment',
        json={'scope': 'global', 'key': 'crypto', 'sentiment': 0.1, 'velocity': 0.0, 'volume': -1},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert resp.status_code == 422


def test_api_sentiment_get_skips_legacy_non_finite_rows(client_and_conn):
    client, conn = client_and_conn
    now_ts = int(time.time())

    conn.execute(
        """INSERT INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)
               VALUES('global', 'crypto', ?, 1e999, 0.0, 1, '{}', '[]')""",
        (now_ts - 60,),
    )
    db.insert_sentiment_point(
        conn,
        'global',
        'crypto',
        now_ts - 30,
        0.25,
        0.05,
        2,
        {'rss': 2},
        ['sanitized'],
    )

    resp = client.get('/api/v1/sentiment?scope=global&key=crypto&limit=10')
    assert resp.status_code == 200
    assert resp.json()['items'] == [
        {
            'scope': 'global',
            'key': 'crypto',
            'ts': now_ts - 30,
            'sentiment': 0.25,
            'velocity': 0.05,
            'volume': 2,
            'sources': {'rss': 2},
            'tags': ['sanitized'],
        }
    ]

def test_api_sentiment_rejects_non_finite_velocity(client_and_conn):
    client, _ = client_and_conn

    resp = client.post(
        '/api/v1/sentiment',
        json={'scope': 'global', 'key': 'crypto', 'sentiment': 0.1, 'velocity': 'inf', 'volume': 1},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert resp.status_code == 422


def test_metrics_endpoint_reports_core_gauges_and_status_collector_workers(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.upsert_ohlcv(
        conn,
        [{
            'venue': 'linear',
            'symbol': 'BTCUSDT',
            'tf_sec': 60,
            'ts': ts_now,
            'open': 100.0,
            'high': 101.0,
            'low': 99.0,
            'close': 100.5,
            'volume': 10.0,
        }],
    )
    db.insert_tickers(
        conn,
        [{
            'venue': 'linear',
            'symbol': 'BTCUSDT',
            'ts': ts_now,
            'last': 100.5,
            'bid': 100.0,
            'ask': 101.0,
            'vol24h': 1000.0,
            'turnover24h': 100000.0,
        }],
    )
    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-metrics-1',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )
    db.log_decision(conn, 'COLLECT_ERROR', None, None, {'venue': 'linear', 'symbol': 'BTCUSDT', 'field': 'ticker', 'err': 'boom'})
    db.set_app_config_json(conn, 'collector_last_cycle', {'duration_ms': 4321})

    metrics_resp = client.get('/metrics')
    assert metrics_resp.status_code == 200
    body = metrics_resp.text
    assert 'bybit_reco_symbols_total 2' in body
    assert 'bybit_reco_symbols_ok 1' in body
    assert 'bybit_reco_symbols_missing 1' in body
    assert 'bybit_reco_collect_errors_10m 1' in body
    assert 'bybit_reco_recommendations_active 1' in body
    assert 'bybit_reco_collector_cycle_duration_ms 4321' in body
    assert 'bybit_reco_warmup_ready 0' in body
    assert 'bybit_reco_warmup_symbols_total 2' in body
    assert 'bybit_reco_collector_max_workers 8' in body
    assert 'bybit_reco_futures_collect_max_workers 8' in body

    status_resp = client.get('/api/v1/status')
    assert status_resp.status_code == 200
    collector_meta = status_resp.json()['collector']
    assert collector_meta['duration_ms'] == 4321
    assert collector_meta['max_workers'] == 8
    assert collector_meta['futures_max_workers'] == 8


def test_api_health_applies_boot_grace_to_pre_restart_stale_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / 'health_boot_grace.db'
    monkeypatch.setenv('DB_PATH', str(db_path))
    monkeypatch.setenv('ADMIN_API_KEY', 'test-admin-key')
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT')
    monkeypatch.setenv('SYMBOLS_LINEAR', '')
    monkeypatch.setenv('VENUES', 'linear')
    monkeypatch.setenv('STALE_DATA_MAX_SEC', '300')
    monkeypatch.setenv('COLLECT_INTERVAL_SEC', '120')

    sys.modules.pop('app.main', None)
    app_main = importlib.import_module('app.main')
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    db.init_db(conn)
    now = int(time.time())
    db.upsert_ohlcv(conn, [{
        'venue': 'linear', 'symbol': 'BTCUSDT', 'tf_sec': 60, 'ts': now - 320,
        'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1.0,
    }])
    db.insert_tickers(conn, [{
        'venue': 'linear', 'symbol': 'BTCUSDT', 'ts': now - 320,
        'last': 1.0, 'bid': 1.0, 'ask': 1.0, 'vol24h': 1.0, 'turnover24h': 1.0,
    }])
    db.set_app_config_json(conn, 'collector_last_cycle', {'started_ts': now - 600, 'duration_ms': 1500})

    client = TestClient(app_main.app)
    try:
        resp = client.get('/api/v1/health/symbols')
        assert resp.status_code == 200
        body = resp.json()
        assert body['boot_grace']['active'] is True
        assert body['summary']['stale'] == 0
        assert body['summary']['ok'] == 1
        assert body['symbols'][0]['status'] == 'ok'
        assert body['symbols'][0]['raw_status'] == 'stale'
        assert body['symbols'][0]['status_reason'] == 'boot_grace'
    finally:
        client.close()
        conn.close()
        sys.modules.pop('app.main', None)



def test_api_health_boot_grace_does_not_mask_very_old_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / 'health_boot_grace_too_old.db'
    monkeypatch.setenv('DB_PATH', str(db_path))
    monkeypatch.setenv('ADMIN_API_KEY', 'test-admin-key')
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT')
    monkeypatch.setenv('SYMBOLS_LINEAR', '')
    monkeypatch.setenv('VENUES', 'linear')
    monkeypatch.setenv('STALE_DATA_MAX_SEC', '300')
    monkeypatch.setenv('COLLECT_INTERVAL_SEC', '120')

    sys.modules.pop('app.main', None)
    app_main = importlib.import_module('app.main')
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    db.init_db(conn)
    now = int(time.time())
    db.upsert_ohlcv(conn, [{
        'venue': 'linear', 'symbol': 'BTCUSDT', 'tf_sec': 60, 'ts': now - 420,
        'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1.0,
    }])
    db.insert_tickers(conn, [{
        'venue': 'linear', 'symbol': 'BTCUSDT', 'ts': now - 420,
        'last': 1.0, 'bid': 1.0, 'ask': 1.0, 'vol24h': 1.0, 'turnover24h': 1.0,
    }])
    db.set_app_config_json(conn, 'collector_last_cycle', {'started_ts': now - 600, 'duration_ms': 1500})

    client = TestClient(app_main.app)
    try:
        resp = client.get('/api/v1/health/symbols')
        assert resp.status_code == 200
        body = resp.json()
        assert body['boot_grace']['active'] is True
        assert body['summary']['stale'] == 1
        assert body['summary']['ok'] == 0
        assert body['symbols'][0]['status'] == 'stale'
        assert 'raw_status' not in body['symbols'][0]
        assert 'status_reason' not in body['symbols'][0]
    finally:
        client.close()
        conn.close()
        sys.modules.pop('app.main', None)


def test_api_execute_chain_member_reuses_existing_publication_root_bot(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-chain-root',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.44,
                'confidence': 0.74,
                'expected_rr': 1.5,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
                'publication_root_rec_id': 'R-chain-root',
                'is_outcome_label_root': True,
            },
            {
                'rec_id': 'R-chain-active',
                'ts': ts_now + 60,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.45,
                'confidence': 0.75,
                'expected_rr': 1.55,
                'risk_score': 0.19,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'active',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now + 60,
                'publication_root_rec_id': 'R-chain-root',
                'is_outcome_label_root': False,
            },
        ],
    )

    first_exec = client.post(
        '/api/v1/recommendations/R-chain-root/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert first_exec.status_code == 200
    first_body = first_exec.json()
    assert first_body['ok'] is True
    assert first_body['idempotent'] is False
    bot_id = first_body['bot_id']

    second_exec = client.post(
        '/api/v1/recommendations/R-chain-active/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert second_exec.status_code == 200
    second_body = second_exec.json()
    assert second_body['ok'] is True
    assert second_body['idempotent'] is True
    assert second_body['bot_id'] == bot_id

    bots = db.list_bot_instances(conn)
    assert len(bots) == 1
    assert bots[0]['origin_rec_id'] == 'R-chain-root'

    active_rec = db.get_recommendation_by_id(conn, 'R-chain-active')
    assert active_rec is not None
    assert active_rec['status'] == 'executed'



def test_api_execute_active_chain_member_starts_new_bot_after_previous_chain_bot_stopped(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-chain-root-stopped',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.44,
                'confidence': 0.74,
                'expected_rr': 1.5,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
                'publication_root_rec_id': 'R-chain-root-stopped',
                'is_outcome_label_root': True,
            },
            {
                'rec_id': 'R-chain-active-after-stop',
                'ts': ts_now + 60,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.45,
                'confidence': 0.75,
                'expected_rr': 1.55,
                'risk_score': 0.19,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'active',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now + 60,
                'publication_root_rec_id': 'R-chain-root-stopped',
                'is_outcome_label_root': False,
            },
        ],
    )

    first_exec = client.post(
        '/api/v1/recommendations/R-chain-root-stopped/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert first_exec.status_code == 200
    first_body = first_exec.json()
    assert first_body['ok'] is True
    assert first_body['idempotent'] is False
    first_bot_id = first_body['bot_id']

    stop_resp = client.post(
        f'/api/v1/bots/{first_bot_id}/stop',
        json={'reason': 'finished', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert stop_resp.status_code == 200
    assert stop_resp.json()['status'] == 'stopped'

    second_exec = client.post(
        '/api/v1/recommendations/R-chain-active-after-stop/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert second_exec.status_code == 200
    second_body = second_exec.json()
    assert second_body['ok'] is True
    assert second_body['idempotent'] is False
    assert second_body['bot_id'] != first_bot_id
    assert second_body['bot']['status'] == 'running'
    assert second_body['bot']['origin_rec_id'] == 'R-chain-active-after-stop'

    bots = db.list_bot_instances(conn)
    assert len(bots) == 2
    running = [b for b in bots if b['status'] == 'running']
    stopped = [b for b in bots if b['status'] == 'stopped']
    assert len(running) == 1
    assert len(stopped) == 1
    assert running[0]['origin_rec_id'] == 'R-chain-active-after-stop'
    assert stopped[0]['origin_rec_id'] == 'R-chain-root-stopped'

    active_rec = db.get_recommendation_by_id(conn, 'R-chain-active-after-stop')
    assert active_rec is not None
    assert active_rec['status'] == 'executed'


def test_api_stop_bot_is_idempotent(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-stop-idempotent',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.43,
                'confidence': 0.72,
                'expected_rr': 1.3,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    exec_resp = client.post(
        '/api/v1/recommendations/R-stop-idempotent/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    first_stop = client.post(
        f'/api/v1/bots/{bot_id}/stop',
        json={'operator': 'tester', 'reason': 'done'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert first_stop.status_code == 200
    assert first_stop.json() == {'ok': True, 'bot_id': bot_id, 'status': 'stopped', 'idempotent': False}

    second_stop = client.post(
        f'/api/v1/bots/{bot_id}/stop',
        json={'operator': 'tester', 'reason': 'duplicate retry'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert second_stop.status_code == 200
    assert second_stop.json() == {'ok': True, 'bot_id': bot_id, 'status': 'stopped', 'idempotent': True}

    cur = conn.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='BOT_STOPPED' AND details_json LIKE ?", (f'%"bot_id": "{bot_id}"%',))
    row = cur.fetchone()
    assert int(row['c'] or 0) == 1


def test_api_risk_limits_rejects_non_finite_nested_json(client_and_conn):
    client, conn = client_and_conn

    resp = client.post(
        '/api/v1/risk/limits',
        content='{"version":"bad-nan","limits":{"max_daily_dd_usdt":NaN,"nested":{"x":1}}}',
        headers={'X-API-Key': 'test-admin-key', 'content-type': 'application/json'},
    )
    assert resp.status_code == 422
    assert 'limits contains non-finite number' in resp.json()['detail']

    active = db.get_active_risk_limits(conn)
    assert active is not None
    assert active['max_daily_dd_usdt'] == pytest.approx(200.0)


def test_api_trade_rejects_non_finite_nested_meta_payload(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-api-meta-nan',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )
    exec_resp = client.post(
        '/api/v1/recommendations/R-api-meta-nan/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        content='{"trade_id":"T-api-meta-nan","ts":%d,"pnl":1.0,"fee":0.1,"meta":{"fills":{"maker":NaN}}}' % (ts_now + 10),
        headers={'X-API-Key': 'test-admin-key', 'content-type': 'application/json'},
    )
    assert resp.status_code == 422
    assert 'meta contains non-finite number' in resp.json()['detail']
    assert db.list_trades(conn) == []


def test_api_sentiment_rejects_non_finite_nested_sources_payload(client_and_conn):
    client, conn = client_and_conn

    resp = client.post(
        '/api/v1/sentiment',
        content='{"scope":"global","key":"crypto","ts":1700000000,"sentiment":0.1,"velocity":0.01,"volume":1,"sources":{"rss":NaN},"tags":["macro"]}',
        headers={'X-API-Key': 'test-admin-key', 'content-type': 'application/json'},
    )
    assert resp.status_code == 422
    assert 'sources contains non-finite number' in resp.json()['detail']
    assert db.get_sentiment_series(conn, 'global', 'crypto', limit=10) == []


def test_api_risk_status_sanitizes_legacy_non_finite_limits_json(client_and_conn):
    client, conn = client_and_conn

    conn.execute('UPDATE risk_limits SET is_active=0')
    conn.execute(
        'INSERT INTO risk_limits(version, limits_json, is_active, created_ts) VALUES(?,?,?,?)',
        ('legacy-nan', '{"max_daily_dd_usdt": NaN, "max_concurrent_bots": 2}', 1, int(time.time())),
    )
    conn.commit()

    resp = client.get('/api/v1/risk/status')
    assert resp.status_code == 200
    body = resp.json()
    assert body['limits']['max_concurrent_bots'] == 2
    assert body['limits']['max_daily_dd_usdt'] == pytest.approx(200.0)


def test_api_stop_bot_keeps_row_and_state_stopped_ts_in_sync(client_and_conn, monkeypatch):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-stop-ts-sync',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    exec_resp = client.post(
        '/api/v1/recommendations/R-stop-ts-sync/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    monkeypatch.setattr(time, 'time', lambda: ts_now + 123)

    resp = client.post(
        f'/api/v1/bots/{bot_id}/stop',
        json={'operator': 'tester', 'reason': 'manual stop'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert resp.status_code == 200

    bot = db.get_bot_instance(conn, bot_id)
    assert bot is not None
    assert bot['status'] == 'stopped'
    assert bot['stopped_ts'] == ts_now + 123
    assert bot['state']['stopped_ts'] == ts_now + 123


def test_api_trade_stop_bot_keeps_row_and_state_stopped_ts_in_sync(client_and_conn, monkeypatch):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-trade-stop-ts-sync',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.42,
                'confidence': 0.67,
                'expected_rr': 1.4,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )

    exec_resp = client.post(
        '/api/v1/recommendations/R-trade-stop-ts-sync/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    monkeypatch.setattr(time, 'time', lambda: ts_now + 456)

    resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json={
            'trade_id': 'T-trade-stop-ts-sync',
            'ts': ts_now + 60,
            'pnl': 3.5,
            'fee': 0.2,
            'operator': 'tester',
            'meta': {'fill_count': 1},
            'stop_bot': True,
        },
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert resp.status_code == 200
    assert resp.json()['bot_status'] == 'stopped'

    bot = db.get_bot_instance(conn, bot_id)
    assert bot is not None
    assert bot['status'] == 'stopped'
    assert bot['stopped_ts'] == ts_now + 456
    assert bot['state']['stopped_ts'] == ts_now + 456


def test_api_recommendations_collapse_publication_chain_duplicates_by_default(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-root",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "unified",
                "margin_mode": "isolated",
                "score": 0.61,
                "confidence": 0.82,
                "expected_rr": 1.3,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 600,
                "model_version": "test",
                "features_ref_ts": ts_now,
                "publication_root_rec_id": "R-root",
                "is_outcome_label_root": True,
            },
            {
                "rec_id": "R-active-dup",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "unified",
                "margin_mode": "isolated",
                "score": 0.60,
                "confidence": 0.79,
                "expected_rr": 1.28,
                "risk_score": 0.2,
                "params": {"grid_levels": 8},
                "reasons": {
                    "publication_dedupe": {
                        "previous_rec_id": "R-root",
                        "previous_root_rec_id": "R-root",
                        "decision": "reuse_active",
                    }
                },
                "blocks": [],
                "status": "active",
                "ttl_sec": 600,
                "model_version": "test",
                "features_ref_ts": ts_now,
                "publication_root_rec_id": "R-root",
                "is_outcome_label_root": False,
            },
        ],
    )

    resp = client.get('/api/v1/recommendations?venue=linear')
    assert resp.status_code == 200
    body = resp.json()

    assert body['publication_chain_dedupe'] == {'enabled': True, 'hidden_duplicates': 1}
    assert [item['rec_id'] for item in body['items']] == ['R-root']

    raw_resp = client.get('/api/v1/recommendations?venue=linear&collapse_chains=false')
    assert raw_resp.status_code == 200
    raw_body = raw_resp.json()
    assert raw_body['publication_chain_dedupe'] == {'enabled': False, 'hidden_duplicates': 0}
    assert {item['rec_id'] for item in raw_body['items']} == {'R-root', 'R-active-dup'}


def test_api_recommendations_collapse_scans_past_large_duplicate_chain_to_fill_unique_top_n(client_and_conn):
    client, conn = client_and_conn
    ts_now = int(time.time())

    duplicate_rows = []
    for idx in range(18):
        rec_id = f'R-dup-{idx:02d}'
        duplicate_rows.append(
            {
                'rec_id': rec_id,
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'unified',
                'margin_mode': 'isolated',
                'score': 0.95 - (idx * 0.001),
                'confidence': 0.99 - (idx * 0.001),
                'expected_rr': 1.5,
                'risk_score': 0.2,
                'params': {'grid_levels': 8},
                'reasons': {
                    'publication_dedupe': {
                        'previous_root_rec_id': 'R-root-btc',
                        'decision': 'reuse_active',
                    }
                },
                'blocks': [],
                'status': 'active' if idx else 'recommended',
                'ttl_sec': 600,
                'model_version': 'test',
                'features_ref_ts': ts_now,
                'publication_root_rec_id': 'R-root-btc',
                'is_outcome_label_root': idx == 0,
            }
        )

    duplicate_rows.extend(
        [
            {
                'rec_id': 'R-eth',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'ETHUSDT',
                'bot_type': 'futures_grid',
                'direction': 'short',
                'account_mode': 'unified',
                'margin_mode': 'isolated',
                'score': 0.40,
                'confidence': 0.40,
                'expected_rr': 1.2,
                'risk_score': 0.3,
                'params': {'grid_levels': 6},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 600,
                'model_version': 'test',
                'features_ref_ts': ts_now,
                'publication_root_rec_id': 'R-eth',
                'is_outcome_label_root': True,
            },
            {
                'rec_id': 'R-sol',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'SOLUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'unified',
                'margin_mode': 'isolated',
                'score': 0.35,
                'confidence': 0.35,
                'expected_rr': 1.1,
                'risk_score': 0.4,
                'params': {'grid_levels': 5},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 600,
                'model_version': 'test',
                'features_ref_ts': ts_now,
                'publication_root_rec_id': 'R-sol',
                'is_outcome_label_root': True,
            },
        ]
    )
    db.insert_recommendations(conn, duplicate_rows)

    resp = client.get('/api/v1/recommendations?venue=linear&top_n=3&min_conf=0')
    assert resp.status_code == 200
    body = resp.json()

    assert [item['rec_id'] for item in body['items']] == ['R-dup-00', 'R-eth', 'R-sol']
    assert body['publication_chain_dedupe']['enabled'] is True
    assert body['publication_chain_dedupe']['hidden_duplicates'] >= 17
