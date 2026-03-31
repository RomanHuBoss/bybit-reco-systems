from __future__ import annotations

import importlib
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app import db
from app.features import oi_trend


def test_oi_trend_requires_timestamp_depth_not_just_row_count() -> None:
    ts_now = 1_700_000_000
    # 25 rows packed into 2 hours should not masquerade as 24h history.
    compressed = [
        {"ts": ts_now - idx * 300, "oi": float(100 + idx)}
        for idx in range(25)
    ]
    trend = oi_trend(compressed)
    assert trend["oi_now"] == 100.0
    assert trend["oi_4h_chg_pct"] is None
    assert trend["oi_24h_chg_pct"] is None
    assert trend["trend"] == "unknown"



def test_get_latest_ticker_ts_ignores_invalid_fallback_only_rows(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "ticker_strict_ts.db"))
    try:
        db.init_db(conn)
        now_ts = int(time.time())
        conn.execute(
            """INSERT INTO ticker_snap(venue, symbol, ts, last, bid, ask, vol24h, turnover24h)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("linear", "BTCUSDT", now_ts, 100.0, 101.0, 99.0, 10_000.0, 1_000_000.0),
        )
        conn.commit()

        latest = db.get_latest_ticker(conn, "linear", "BTCUSDT")
        assert latest is not None
        assert latest["bid"] is None
        assert latest["ask"] is None
        assert db.get_latest_ticker_ts(conn, "linear", "BTCUSDT") is None

        health = db.get_symbol_health(conn, [], ["BTCUSDT"], stale_sec=300, active_venues=["linear"])
        assert health[0]["status"] == "missing"
    finally:
        conn.close()



def test_sentiment_thread_does_not_persist_after_runtime_lock_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "sentiment_lock_loss.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("VENUES", "spot")
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(app_main.settings, sentiment_interval_sec=5)

        def fake_collect_sentiment_once():
            return [{
                "scope": "global",
                "key": "crypto",
                "ts": int(time.time()),
                "sentiment": 0.7,
                "velocity": 0.1,
                "volume": 3,
                "sources": {"x": 1},
                "tags": ["test"],
            }]

        def dead_heartbeat(*args, **kwargs):
            return lambda: False

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        monkeypatch.setattr(app_main, "collect_sentiment_once", fake_collect_sentiment_once)
        monkeypatch.setattr(app_main, "_make_runtime_lock_heartbeat", dead_heartbeat)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._sentiment_thread()

        conn = db.connect(str(db_path))
        try:
            sentiment_rows = conn.execute("SELECT COUNT(*) AS c FROM sentiment").fetchone()["c"]
            assert int(sentiment_rows) == 0
            row = conn.execute(
                "SELECT details_json FROM decision_log WHERE action='SENTIMENT_ERROR' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            details = app_main._json_loads_or_default(row["details_json"], {})
            assert details.get("field") == "runtime_lock"
        finally:
            conn.close()
    finally:
        sys.modules.pop("app.main", None)
