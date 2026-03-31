from __future__ import annotations

import importlib
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app import db
from app.features import oi_trend


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "iter72.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_get_latest_ticker_ts_ignores_sanitized_but_invalid_latest_row(conn, monkeypatch: pytest.MonkeyPatch):
    base_ts = 1_700_100_000
    monkeypatch.setattr(db, "now_ts", lambda: base_ts)
    # Insert only a malformed row directly, simulating historical corruption or legacy import.
    conn.execute(
        """INSERT OR REPLACE INTO ticker_snap(venue,symbol,ts,last,bid,ask,vol24h,turnover24h)
           VALUES(?,?,?,?,?,?,?,?)""",
        ("linear", "BTCUSDT", base_ts - 10, 101.0, 102.0, 101.0, 2000.0, 8000.0),
    )
    conn.commit()

    ticker = db.get_latest_ticker(conn, "linear", "BTCUSDT")
    assert ticker is not None
    assert int(ticker["ts"]) == base_ts - 10
    assert ticker["bid"] is None
    assert ticker["ask"] is None

    # Freshness must not be inferred from a sanitized fallback row with invalid quote fields.
    assert db.get_latest_ticker_ts(conn, "linear", "BTCUSDT") is None



def test_sentiment_thread_rolls_back_when_runtime_lock_is_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "sentiment_lock_loss.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(app_main.settings, sentiment_interval_sec=5)

        def fake_collect_sentiment_once():
            return [{
                "scope": "global",
                "key": "crypto",
                "ts": int(time.time()),
                "sentiment": 0.25,
                "velocity": 0.0,
                "volume": 1,
                "sources": {"test": 1},
                "tags": ["unit"],
            }]

        def dead_heartbeat(*args, **kwargs):
            state = {"calls": 0}
            def _hb():
                state["calls"] += 1
                return state["calls"] == 1
            return _hb

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        monkeypatch.setattr(app_main, "collect_sentiment_once", fake_collect_sentiment_once)
        monkeypatch.setattr(app_main, "_make_runtime_lock_heartbeat", dead_heartbeat)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._sentiment_thread()

        check_conn = db.connect(str(db_path))
        try:
            assert int(check_conn.execute("SELECT COUNT(*) AS c FROM sentiment").fetchone()["c"]) == 0
            row = check_conn.execute(
                "SELECT details_json FROM decision_log WHERE action='SENTIMENT_ERROR' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            details = app_main._json_loads_or_default(row["details_json"], {})
            assert details.get("field") == "runtime_lock"
        finally:
            check_conn.close()
    finally:
        sys.modules.pop("app.main", None)



def test_oi_trend_requires_real_time_depth_for_24h_signal() -> None:
    ts_now = 1_700_000_000
    dense_but_short = [
        {"ts": ts_now - idx * 3600, "oi": float(100 + idx)}
        for idx in range(8)
    ]
    trend = oi_trend(dense_but_short)
    assert trend["oi_4h_chg_pct"] is not None
    assert trend["oi_24h_chg_pct"] is None
    assert trend["trend"] == "unknown"

    full = [
        {"ts": ts_now - idx * 3600, "oi": float(100 + idx)}
        for idx in range(30)
    ]
    full_trend = oi_trend(full)
    assert full_trend["oi_24h_chg_pct"] is not None
    assert full_trend["trend"] in {"growing", "stable", "falling"}
