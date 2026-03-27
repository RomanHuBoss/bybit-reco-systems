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



def test_api_health_exposes_llm_reviewer_config(client_and_conn, monkeypatch):
    client, _ = client_and_conn

    resp = client.get('/api/v1/health/symbols')
    assert resp.status_code == 200
    body = resp.json()
    assert 'llm_reviewer' in body
    assert body['llm_reviewer']['enabled'] is False
    assert body['llm_reviewer']['provider'] == 'ollama'
