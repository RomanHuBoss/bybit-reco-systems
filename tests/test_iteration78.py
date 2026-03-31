from __future__ import annotations

import importlib
from contextlib import closing
import sys
from pathlib import Path

from app import db
from app import collector


def test_background_supervisor_records_collector_crash_and_restart_state(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "supervisor.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    monkeypatch.setenv("VENUES", "spot")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        calls = {"count": 0}

        def flaky_target():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("collector crashed")
            return None

        app_main._run_supervised_background_target(
            "collector",
            flaky_target,
            restart_delay_sec=0,
            max_restarts=1,
            sleep_fn=lambda _: None,
            treat_return_as_error=False,
        )

        assert calls["count"] == 2
        with closing(db.connect(str(db_path))) as conn:
            state = db.get_app_config_json(conn, app_main._background_thread_state_key("collector"), default={}) or {}
            assert state["state"] == "stopped"
            assert int(state.get("restart_count") or 0) == 1

            row = conn.execute(
                "SELECT details_json FROM decision_log WHERE action='COLLECT_ERROR' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            details = app_main._json_loads_or_default(row["details_json"], {})
            assert details.get("field") == "background_thread"
            assert details.get("component") == "collector"
            assert details.get("stage") == "background_supervisor"
    finally:
        sys.modules.pop("app.main", None)



def test_status_recomputes_warmup_when_cached_snapshot_is_missing(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "status_warmup.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        status = app_main.api_status()
        warmup = status["collector"]["warmup"]
        assert warmup["derived_on_read"] is True
        assert warmup["ready"] is False
        assert warmup["venues"][0]["venue"] == "linear"
        assert warmup["venues"][0]["reason_counts"]["ticker_missing"] == 1
        assert warmup["venues"][0]["reason_counts"]["candle_missing"] == 1
    finally:
        sys.modules.pop("app.main", None)



def test_collect_once_logs_missing_ticker_only_once_per_ttl(tmp_path: Path):
    db_path = tmp_path / "missing_ticker.db"
    conn = db.connect(str(db_path))
    db.init_db(conn)
    collector._MISSING_TICKER_LOG_TS.clear()

    class MissingTickerClient:
        def get_tickers(self, category: str, symbol: str | None = None):
            return []

        def get_kline(self, category: str, symbol: str, interval: str = "1", limit: int = 200, start=None, end=None):
            return []

    try:
        stats1 = collector.collect_once(conn, MissingTickerClient(), "linear", ["MISSUSDT"], max_workers=1)
        count1 = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR' AND details_json LIKE '%ticker_missing%'"
            ).fetchone()["c"]
        )
        stats2 = collector.collect_once(conn, MissingTickerClient(), "linear", ["MISSUSDT"], max_workers=1)
        count2 = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR' AND details_json LIKE '%ticker_missing%'"
            ).fetchone()["c"]
        )

        assert stats1["ticker_missing_symbols"] == 1
        assert stats1["sample_ticker_missing_symbols"] == ["MISSUSDT"]
        assert stats2["ticker_missing_symbols"] == 1
        assert count1 == 1
        assert count2 == 1
    finally:
        conn.close()
