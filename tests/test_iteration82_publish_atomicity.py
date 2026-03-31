from __future__ import annotations

import time

import pytest

from app import db
from app.settings import Settings
from app.shock_guard import APP_CONFIG_KEY as MARKET_SHOCK_APP_KEY
from app.recommender import (
    DIRECTION_STATE_APP_KEY,
    PERSISTENCE_STATE_APP_KEY,
    run_recommender_once,
)
from tests.test_logic import _seed_ohlcv_wave


def _seed_publishable_linear_symbol(conn, *, symbol: str = "BTCUSDT") -> int:
    now = int(time.time())
    base_price = 50_000.0
    venue = "linear"
    for tf_sec, n in ((60, 220), (900, 120), (1800, 120), (3600, 120), (14_400, 100), (86_400, 100)):
        _seed_ohlcv_wave(conn, venue=venue, symbol=symbol, now_ts=now, tf_sec=tf_sec, n=n, base_price=base_price)

    db.insert_tickers(conn, [{
        "venue": venue,
        "symbol": symbol,
        "ts": now,
        "last": base_price,
        "bid": base_price - 5.0,
        "ask": base_price + 5.0,
        "vol24h": 12_345.0,
        "turnover24h": 5_000_000.0,
    }])
    db.upsert_funding_rate(conn, [{
        "symbol": symbol,
        "ts": now,
        "funding_rate": 0.0001,
        "next_funding_ts": now + 4 * 3600,
    }])
    return now


def _settings() -> Settings:
    return Settings(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=["linear"],
        symbols_spot=[],
        symbols_linear=["BTCUSDT"],
        risk_limits={
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
        },
        min_score_to_recommend=0.08,
        min_conf_to_recommend=0.52,
        taker_fee_bps_spot=10.0,
        taker_fee_bps_linear=6.0,
        master_key=None,
        admin_api_key=None,
        sentiment_interval_sec=60,
        futures_collect_interval_sec=900,
        telegram_token=None,
        telegram_chat_id=None,
        require_conf_gate=True,
    )


@pytest.mark.parametrize("failing_key", [PERSISTENCE_STATE_APP_KEY, DIRECTION_STATE_APP_KEY, MARKET_SHOCK_APP_KEY])
def test_run_recommender_once_rolls_back_publish_side_effects_when_insert_fails(tmp_path, failing_key, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "publish_insert_failure.db"
    conn = db.connect(str(db_path))
    db.init_db(conn)
    _seed_publishable_linear_symbol(conn)

    original_insert = db.insert_recommendations

    def failing_insert(conn_, rows, *, commit=True):
        raise RuntimeError("insert failed after staging publish state")

    monkeypatch.setattr(db, "insert_recommendations", failing_insert)
    try:
        with pytest.raises(RuntimeError, match="insert failed"):
            run_recommender_once(conn, _settings())
        conn.rollback()
    finally:
        monkeypatch.setattr(db, "insert_recommendations", original_insert)
        conn.close()

    verify = db.connect(str(db_path))
    try:
        assert verify.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"] == 0
        assert verify.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='PUBLISH'").fetchone()["c"] == 0
        assert verify.execute("SELECT value_json FROM app_config WHERE key=?", (failing_key,)).fetchone() is None
    finally:
        verify.close()


def test_run_recommender_once_rolls_back_recommendations_when_publish_audit_write_fails(tmp_path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "publish_log_failure.db"
    conn = db.connect(str(db_path))
    db.init_db(conn)
    _seed_publishable_linear_symbol(conn)

    original_log_decision = db.log_decision

    def failing_log_decision(conn_, action, rec_id, operator, details, *, commit=True):
        if action == "PUBLISH":
            raise RuntimeError("publish decision log failed")
        return original_log_decision(conn_, action, rec_id, operator, details, commit=commit)

    monkeypatch.setattr(db, "log_decision", failing_log_decision)
    try:
        with pytest.raises(RuntimeError, match="publish decision log failed"):
            run_recommender_once(conn, _settings())
        conn.rollback()
    finally:
        monkeypatch.setattr(db, "log_decision", original_log_decision)
        conn.close()

    verify = db.connect(str(db_path))
    try:
        assert verify.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"] == 0
        assert verify.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='PUBLISH'").fetchone()["c"] == 0
        assert verify.execute("SELECT value_json FROM app_config WHERE key=?", (PERSISTENCE_STATE_APP_KEY,)).fetchone() is None
        assert verify.execute("SELECT value_json FROM app_config WHERE key=?", (DIRECTION_STATE_APP_KEY,)).fetchone() is None
        assert verify.execute("SELECT value_json FROM app_config WHERE key=?", (MARKET_SHOCK_APP_KEY,)).fetchone() is None
    finally:
        verify.close()
