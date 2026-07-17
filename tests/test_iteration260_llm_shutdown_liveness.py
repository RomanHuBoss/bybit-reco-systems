from __future__ import annotations

import importlib
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app import db


def _import_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "runtime-locks.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "1")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def test_llm_reviewer_stops_after_shutdown_signal_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_main = _import_app(monkeypatch, tmp_path)
    try:
        app_main.RUNTIME_OWNER = "TEST:iteration260"
        app_main.settings = replace(
            app_main.settings,
            llm_reviewer_enabled=True,
            llm_reviewer_cadence_sec=60,
            reco_interval_sec=20,
        )
        app_main._BACKGROUND_STOP_EVENT.clear()
        sweep_calls = 0
        wait_calls = 0

        def fake_sweep(conn, settings, *, heartbeat=None):
            nonlocal sweep_calls
            assert heartbeat is not None
            sweep_calls += 1
            return {"pending_after": 0}

        def signal_shutdown(next_run: float, interval_sec: int) -> float:
            nonlocal wait_calls
            wait_calls += 1
            app_main._BACKGROUND_STOP_EVENT.set()
            if wait_calls > 1:
                raise AssertionError("LLM reviewer continued after the shared shutdown signal")
            return next_run + interval_sec

        monkeypatch.setattr(app_main, "run_llm_review_sweep_once", fake_sweep)
        monkeypatch.setattr(app_main, "_interval_loop_wait", signal_shutdown)

        app_main._run_supervised_background_target(
            "llm_reviewer",
            app_main._llm_reviewer_thread,
            restart_delay_sec=0,
            sleep_fn=lambda _: None,
        )

        assert sweep_calls == 1
        assert wait_calls == 1
        with closing(app_main._get_lock_conn()) as conn:
            row = conn.execute(
                "SELECT owner FROM runtime_locks WHERE lock_key=?",
                ("runtime:llm_reviewer",),
            ).fetchone()
        assert row is None
        with closing(app_main._get_conn()) as conn:
            state = db.get_app_config_json(
                conn,
                "runtime_thread_state:llm_reviewer",
                default={},
            )
        assert state["state"] == "stopped"
        assert int(state.get("consecutive_failures") or 0) == 0
    finally:
        app_main._BACKGROUND_STOP_EVENT.set()
        sys.modules.pop("app.main", None)
