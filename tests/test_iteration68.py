from __future__ import annotations

import importlib
import math
import sys
import time
from pathlib import Path

import pytest

from app import collector, db
from app.recommender import run_recommender_once
from app.settings import Settings


@pytest.fixture()
def client_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    db_path = tmp_path / "api_iter68.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: {})

    conn = db.connect(str(db_path))
    ts_now = int(time.time())
    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts_now - 60,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }],
    )
    db.insert_tickers(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "ts": ts_now - 30,
            "last": 100.5,
            "bid": 100.4,
            "ask": 100.6,
            "vol24h": 1000.0,
            "turnover24h": 100000.0,
        }],
    )
    db.insert_features(conn, "linear", "BTCUSDT", ts_now - 30, {"volume_z": 0.1})
    client = TestClient(app_main.app)
    try:
        yield client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)



def _seed_ohlcv_wave(conn, *, venue: str, symbol: str, now_ts: int, tf_sec: int, n: int, base_price: float) -> None:
    rows = []
    for idx in range(n):
        ts = now_ts - (n - 1 - idx) * tf_sec
        drift = math.sin(idx / 6.0) * base_price * 0.002
        close = base_price + drift + idx * base_price * 0.00005
        open_px = close * 0.999
        high = close * 1.002
        low = close * 0.998
        rows.append(
            {
                "venue": venue,
                "symbol": symbol,
                "tf_sec": tf_sec,
                "ts": ts,
                "open": open_px,
                "high": high,
                "low": low,
                "close": close,
                "volume": 100.0 + idx,
            }
        )
    db.upsert_ohlcv(conn, rows)


def _settings_for_tests(**overrides):
    base = dict(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=["linear"],
        symbols_linear=["BTCUSDT"],
        risk_limits={"max_concurrent_bots": 4, "max_daily_dd_usdt": 200.0, "cooldown_after_loss_min": 30, "max_symbol_bots": 1},
        min_score_to_recommend=0.08,
        min_conf_to_recommend=0.52,
        taker_fee_bps_linear=6.0,
        master_key=None,
        admin_api_key=None,
        sentiment_interval_sec=60,
        futures_collect_interval_sec=900,
        telegram_token=None,
        telegram_chat_id=None,
        require_conf_gate=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_get_symbol_health_marks_symbol_stale_when_ticker_is_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "health_stale_ticker.db"))
    db.init_db(conn)
    base_ts = 1_700_000_000
    monkeypatch.setattr(db, "now_ts", lambda: base_ts)

    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts - 60,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }],
    )
    db.insert_tickers(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "ts": base_ts - 1800,
            "last": 100.5,
            "bid": 100.0,
            "ask": 101.0,
            "vol24h": 1000.0,
            "turnover24h": 100000.0,
        }],
    )

    item = db.get_symbol_health(conn, [], ["BTCUSDT"], stale_sec=300, active_venues=["linear"])[0]
    assert item["status"] == "stale"
    assert item["age_sec"] == 60
    assert item["ticker_age_sec"] == 1800
    assert item["data_age_sec"] == 1800
    conn.close()


def test_run_recommender_once_skips_symbol_when_ticker_is_missing_or_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "reco_stale_ticker.db"))
    db.init_db(conn)
    now = 1_700_000_000
    monkeypatch.setattr(db, "now_ts", lambda: now)
    symbol = "BTCUSDT"
    venue = "linear"
    base_price = 50_000.0

    for tf_sec, n in ((60, 220), (900, 120), (1800, 120), (3600, 120), (14_400, 100), (86_400, 100)):
        _seed_ohlcv_wave(conn, venue=venue, symbol=symbol, now_ts=now, tf_sec=tf_sec, n=n, base_price=base_price)

    db.insert_tickers(
        conn,
        [{
            "venue": venue,
            "symbol": symbol,
            "ts": now - 7200,
            "last": base_price,
            "bid": base_price - 5.0,
            "ask": base_price + 5.0,
            "vol24h": 12_345.0,
            "turnover24h": 5_000_000.0,
        }],
    )
    db.upsert_funding_rate(conn, [{"symbol": symbol, "ts": now, "funding_rate": 0.0001, "next_funding_ts": now + 4 * 3600}])

    settings = _settings_for_tests(stale_data_max_sec=3600)
    result = run_recommender_once(conn, settings)

    assert result["count"] == 0
    stale_rows = conn.execute(
        "SELECT details_json FROM decision_log WHERE action='STALE_DATA_SKIP' ORDER BY id DESC LIMIT 1"
    ).fetchall()
    assert stale_rows
    assert '"source": "ticker"' in stale_rows[0]["details_json"]
    conn.close()


def test_make_runtime_lock_heartbeat_fails_closed_on_db_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "hb_fail_closed.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        conn = db.connect(str(db_path))
        db.init_db(conn)
        conn.close()

        def boom(*args, **kwargs):
            raise RuntimeError("heartbeat db error")

        monkeypatch.setattr(app_main.db, "heartbeat_runtime_lock", boom)
        heartbeat = app_main._make_runtime_lock_heartbeat("runtime:test")

        with pytest.raises(collector.RuntimeLockLostError):
            collector._heartbeat(heartbeat)
    finally:
        sys.modules.pop("app.main", None)


def test_execute_recommendation_rolls_back_bot_insert_on_followup_failure(client_and_conn, monkeypatch: pytest.MonkeyPatch):
    client, conn = client_and_conn
    import app.main as app_main

    ts_now = int(time.time())
    db.insert_recommendations(
        conn,
        [{
            'rec_id': 'R-exec-rollback',
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
        }],
    )

    real_update = app_main.db.update_recommendation_status

    def broken_update(*args, **kwargs):
        raise RuntimeError('status write failed')

    monkeypatch.setattr(app_main.db, 'update_recommendation_status', broken_update)

    with pytest.raises(RuntimeError):
        client.post(
            '/api/v1/recommendations/R-exec-rollback/action',
            json={'action': 'executed', 'operator': 'tester'},
            headers={'X-API-Key': 'test-admin-key'},
        )

    # No bot should survive a failed execute path.
    assert db.get_bot_by_origin_rec(conn, 'R-exec-rollback') is None
    rec = db.get_recommendation_by_id(conn, 'R-exec-rollback')
    assert rec is not None
    assert rec['status'] == 'recommended'

    monkeypatch.setattr(app_main.db, 'update_recommendation_status', real_update)


def test_execute_rolls_back_when_status_update_returns_false(client_and_conn, monkeypatch: pytest.MonkeyPatch):
    client, conn = client_and_conn
    import app.main as app_main

    ts_now = int(time.time())
    db.insert_recommendations(
        conn,
        [{
            'rec_id': 'R-exec-status-false',
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
        }],
    )

    real_update = app_main.db.update_recommendation_status

    def false_update(conn_, rec_id, status, operator=None, **kwargs):
        if rec_id == 'R-exec-status-false' and status == 'executed':
            return False
        return real_update(conn_, rec_id, status, operator, **kwargs)

    monkeypatch.setattr(app_main.db, 'update_recommendation_status', false_update)

    resp = client.post(
        '/api/v1/recommendations/R-exec-status-false/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )

    assert resp.status_code == 409
    assert resp.json()['detail'] == 'recommendation status changed during execution'
    assert db.get_bot_by_origin_rec(conn, 'R-exec-status-false') is None
    rec = db.get_recommendation_by_id(conn, 'R-exec-status-false')
    assert rec is not None
    assert rec['status'] == 'recommended'


def test_trade_record_rolls_back_when_stop_bot_status_change_fails(client_and_conn, monkeypatch: pytest.MonkeyPatch):
    client, conn = client_and_conn
    import app.main as app_main

    ts_now = int(time.time())
    db.insert_recommendations(
        conn,
        [{
            'rec_id': 'R-trade-stop-false',
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
        }],
    )
    exec_resp = client.post(
        '/api/v1/recommendations/R-trade-stop-false/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    real_stop = app_main.db.stop_bot

    def false_stop(conn_, bot_id_, **kwargs):
        if bot_id_ == bot_id:
            return False
        return real_stop(conn_, bot_id_, **kwargs)

    monkeypatch.setattr(app_main.db, 'stop_bot', false_stop)

    resp = client.post(
        f'/api/v1/bots/{bot_id}/trades',
        json={
            'trade_id': 'T-trade-stop-false',
            'ts': ts_now + 60,
            'pnl': 4.0,
            'fee': 0.25,
            'operator': 'tester',
            'meta': {'fill_count': 1},
            'stop_bot': True,
        },
        headers={'X-API-Key': 'test-admin-key'},
    )

    assert resp.status_code == 409
    assert resp.json()['detail'] == 'bot status changed during trade finalization'
    assert db.get_trade_by_id(conn, 'T-trade-stop-false') is None
    bot = db.get_bot_instance(conn, bot_id)
    assert bot is not None
    assert bot['status'] == 'running'
    assert bot['state'].get('trade_count', 0) == 0
    assert bot['state'].get('stop_reason') is None


def test_trade_record_rolls_back_on_log_failure(client_and_conn, monkeypatch: pytest.MonkeyPatch):
    client, conn = client_and_conn
    import app.main as app_main

    ts_now = int(time.time())
    db.insert_recommendations(
        conn,
        [{
            'rec_id': 'R-trade-rollback',
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
        }],
    )
    exec_resp = client.post(
        '/api/v1/recommendations/R-trade-rollback/action',
        json={'action': 'executed', 'operator': 'tester'},
        headers={'X-API-Key': 'test-admin-key'},
    )
    assert exec_resp.status_code == 200
    bot_id = exec_resp.json()['bot_id']

    real_log = app_main.db.log_decision

    def broken_log(conn_, action, rec_id, operator, details, **kwargs):
        if action == 'TRADE_RECORDED':
            raise RuntimeError('decision log failed')
        return real_log(conn_, action, rec_id, operator, details, **kwargs)

    monkeypatch.setattr(app_main.db, 'log_decision', broken_log)

    with pytest.raises(RuntimeError):
        client.post(
            f'/api/v1/bots/{bot_id}/trades',
            json={
                'trade_id': 'T-trade-rollback',
                'ts': ts_now + 60,
                'pnl': 5.0,
                'fee': 0.5,
                'operator': 'tester',
                'meta': {'fill_count': 1},
                'stop_bot': True,
            },
            headers={'X-API-Key': 'test-admin-key'},
        )

    assert db.get_trade_by_id(conn, 'T-trade-rollback') is None
    bot = db.get_bot_instance(conn, bot_id)
    assert bot is not None
    assert bot['status'] == 'running'
    assert bot['state'].get('trade_count', 0) == 0
