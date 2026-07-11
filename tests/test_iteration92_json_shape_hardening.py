from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from conftest import safe_linear_grid_params


@pytest.fixture()
def client_conn_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / 'api.db'
    monkeypatch.setenv('DB_PATH', str(db_path))
    monkeypatch.setenv('ADMIN_API_KEY', 'test-admin-key')
    monkeypatch.setenv('SYMBOLS_LINEAR', 'BTCUSDT')
    monkeypatch.setenv('RISK_LIMITS_JSON', '{"max_concurrent_bots":4,"max_daily_dd_usdt":200.0,"cooldown_after_loss_min":30,"max_symbol_bots":1,"min_leverage":1,"max_leverage":5,"max_position_notional_usdt":5000.0,"max_margin_per_bot_usdt":1000.0}')

    sys.modules.pop('app.main', None)
    app_main = importlib.import_module('app.main')
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(app_main, '_fetch_bybit_instrument_meta', lambda venue, symbol: {"category":"linear","symbol":str(symbol or "BTCUSDT").upper(),"status":"Trading","contract_type":"LinearPerpetual","quote_coin":"USDT","settle_coin":"USDT","tick_size":"0.1","qty_step":"0.001","min_order_qty":"0.001","max_order_qty":"1000","min_notional":"5","min_leverage":"1","max_leverage":"100","leverage_step":"0.01"})

    conn = db.connect(str(db_path))
    ts_now = int(time.time())
    db.upsert_ohlcv(
        conn,
        [
            {
                'ts': ts_now - 60,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'tf_sec': 60,
                'open': 60000.0,
                'high': 60100.0,
                'low': 59900.0,
                'close': 60050.0,
                'volume': 1000.0,
            }
        ],
    )
    db.insert_tickers(
        conn,
        [
            {
                'ts': ts_now - 10,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'last': 60050.0,
                'bid': 60049.5,
                'ask': 60050.5,
                'vol24h': 1000000.0,
                'turnover24h': 50000000.0,
            }
        ],
    )
    db.insert_features(conn, 'linear', 'BTCUSDT', ts_now - 30, {'volume_z': 0.1})
    client = TestClient(app_main.app)
    try:
        yield client, conn, app_main
    finally:
        client.close()
        conn.close()
        sys.modules.pop('app.main', None)



def test_api_recommendations_and_details_fail_closed_on_malformed_json_shapes(client_conn_app):
    client, conn, _ = client_conn_app
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-bad-json',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.8,
                'confidence': 0.9,
                'expected_rr': 1.2,
                'risk_score': 0.1,
                'params': safe_linear_grid_params({'grid_levels': 5}, reference=60050.0, lower=59400.0, upper=60600.0),
                'reasons': {'why': 'ok'},
                'blocks': [{'code': 'X'}],
                'status': 'recommended',
                'ttl_sec': 300,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )
    conn.execute(
        "UPDATE recommendations SET params_json=?, reasons_json=?, blocks_json=? WHERE rec_id='R-bad-json'",
        ('"broken"', '[1,2,3]', '{"code":"not-a-list"}'),
    )
    conn.commit()

    resp = client.get('/api/v1/recommendations')
    assert resp.status_code == 200
    assert all(x['rec_id'] != 'R-bad-json' for x in resp.json()['items'])

    blocked_resp = client.get('/api/v1/recommendations?show_blocked=true')
    assert blocked_resp.status_code == 200
    item = next(x for x in blocked_resp.json()['items'] if x['rec_id'] == 'R-bad-json')
    assert item['status'] == 'blocked'
    assert item['effective_status'] == 'blocked'
    assert item['stored_status'] == 'recommended'
    assert item['params'] == {}
    assert item['reasons'] == {}
    assert item['blocks'] == []

    detail = client.get('/api/v1/recommendations/R-bad-json')
    assert detail.status_code == 200
    body = detail.json()
    assert body['status'] == 'blocked'
    assert body['effective_status'] == 'blocked'
    assert body['stored_status'] == 'recommended'
    assert body['params'] == {}
    assert body['reasons'] == {}
    assert body['blocks'] == []



def test_api_bot_trade_and_sentiment_payloads_are_shape_normalized(client_conn_app):
    client, conn, _ = client_conn_app
    ts_now = int(time.time())

    db.insert_bot_instance(
        conn,
        {
            'bot_id': 'B-bad-json',
            'started_ts': ts_now,
            'stopped_ts': None,
            'venue': 'linear',
            'symbol': 'BTCUSDT',
            'bot_type': 'futures_grid',
            'mode': {'direction': 'long'},
            'params': {'grid_levels': 7},
            'state': {'trade_count': 0},
            'status': 'running',
            'origin_rec_id': 'R-origin',
        },
    )
    conn.execute(
        "UPDATE bot_instances SET mode_json=?, params_json=?, state_json=? WHERE bot_id='B-bad-json'",
        ('[]', '"broken"', 'null'),
    )

    conn.execute(
        "INSERT INTO trades(trade_id, bot_id, ts, symbol, pnl, fee, meta_json) VALUES(?,?,?,?,?,?,?)",
        ('T-bad-json', 'B-bad-json', ts_now + 1, 'BTCUSDT', 1.0, 0.1, '[]'),
    )

    conn.execute(
        "INSERT INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json) VALUES(?,?,?,?,?,?,?,?)",
        ('global', 'crypto', ts_now, 0.2, 0.0, 1, '[]', '{"bad":1}'),
    )
    conn.commit()

    bot_resp = client.get('/api/v1/bots/B-bad-json')
    assert bot_resp.status_code == 200
    bot = bot_resp.json()
    assert bot['mode'] == {}
    assert bot['params'] == {}
    assert bot['state'] == {}

    trades_resp = client.get('/api/v1/trades?bot_id=B-bad-json')
    assert trades_resp.status_code == 200
    assert trades_resp.json()['items'][0]['meta'] == {}

    sentiment_resp = client.get('/api/v1/sentiment?scope=global&key=crypto&limit=5')
    assert sentiment_resp.status_code == 200
    assert sentiment_resp.json()['items'][0]['sources'] == {}
    assert sentiment_resp.json()['items'][0]['tags'] == []



def test_status_metrics_and_decisions_survive_malformed_app_config_and_details(client_conn_app):
    client, conn, app_main = client_conn_app
    ts_now = int(time.time())

    bad_app_config_rows = [
        ('collector_last_cycle', '[1,2,3]'),
        ('backfill_last_cycle', '"broken"'),
        ('futures_meta_last_cycle', '42'),
        ('collector_warmup', '["bad"]'),
        (app_main.LLM_REVIEW_ASYNC_STATUS_APP_KEY, 'null'),
        (app_main.MARKET_SHOCK_APP_KEY, '[]'),
        ('runtime_thread_state:collector', '"oops"'),
    ]
    conn.executemany(
        'INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)',
        [(key, raw, ts_now) for key, raw in bad_app_config_rows],
    )
    conn.execute(
        'INSERT INTO decision_log(ts, action, rec_id, operator, details_json) VALUES(?,?,?,?,?)',
        (ts_now, 'TEST_BAD_DETAILS', None, None, '[]'),
    )
    conn.commit()

    status_resp = client.get('/api/v1/status')
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body['market_shock']['state'] == 'normal'
    assert status_body['collector']['state'] in {'unknown', 'starting', 'ok', 'stalled', 'error'}
    assert 'duration_ms' not in status_body['collector']
    assert status_body['backfill']['thread']['age_sec'] is None
    assert status_body['futures_meta']['thread']['age_sec'] is None
    assert len(status_body['backfill']) == 1
    assert len(status_body['futures_meta']) == 1
    assert status_body['llm_reviewer']['worker'] == {}

    metrics_resp = client.get('/metrics')
    assert metrics_resp.status_code == 200
    assert 'bybit_reco_collector_cycle_duration_ms 0' in metrics_resp.text

    decisions_resp = client.get('/api/v1/decisions?limit=5')
    assert decisions_resp.status_code == 200
    row = next(x for x in decisions_resp.json() if x['action'] == 'TEST_BAD_DETAILS')
    assert row['details'] == {}



def test_mutating_api_rejects_blank_audit_keys(client_conn_app):
    client, conn, _ = client_conn_app
    ts_now = int(time.time())

    risk_resp = client.post(
        '/api/v1/risk/limits',
        json={'version': '   ', 'limits': {'max_active_bots': 3}},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert risk_resp.status_code == 422
    assert 'version' in risk_resp.json()['detail']

    db.insert_recommendations(
        conn,
        [
            {
                'rec_id': 'R-for-trade-id',
                'ts': ts_now,
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'bot_type': 'futures_grid',
                'direction': 'long',
                'account_mode': 'one_way',
                'margin_mode': 'isolated',
                'score': 0.5,
                'confidence': 0.8,
                'expected_rr': 1.1,
                'risk_score': 0.2,
                'params': safe_linear_grid_params({'grid_levels': 5}, reference=60050.0, lower=59400.0, upper=60600.0),
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 600,
                'model_version': 'test',
                'features_ref_ts': ts_now,
            }
        ],
    )
    exec_resp = client.post(
        '/api/v1/recommendations/R-for-trade-id/action',
        json={'action': 'executed', 'operator': ' tester '},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    trade_resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json={'trade_id': '   ', 'pnl': 1.0, 'fee': 0.1},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert trade_resp.status_code == 422
    assert 'trade_id' in trade_resp.json()['detail']
