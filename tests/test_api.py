from __future__ import annotations

import importlib
import json
import os
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
    monkeypatch.setenv('SYMBOLS_SPOT', 'BTCUSDT')
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT')

    sys.modules.pop('app.main', None)
    app_main = importlib.import_module('app.main')
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
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
    monkeypatch.setenv('SYMBOLS_SPOT', 'BTCUSDT')
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
    assert env_map['LLM_REVIEWER_MAX_CANDIDATES'] == '60'
    assert env_map['LLM_REVIEWER_MAX_WORKERS'] == '8'
    assert env_map['LLM_REVIEWER_CADENCE_SEC'] == '300'


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


def test_api_status_and_health_report_large_batch_llm_default_capacity(client_and_conn):
    client, _ = client_and_conn

    status_resp = client.get('/api/v1/status')
    assert status_resp.status_code == 200
    assert status_resp.json()['llm_reviewer']['max_candidates'] == 60

    health_resp = client.get('/api/v1/health/symbols')
    assert health_resp.status_code == 200
    assert health_resp.json()['llm_reviewer']['max_candidates'] == 60


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
    monkeypatch.setenv('SYMBOLS_SPOT', 'BTCUSDT')
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
