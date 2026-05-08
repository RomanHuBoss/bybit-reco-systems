from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app import db


def test_collect_backfill_cycle_uses_full_sweep_budget_while_warmup_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "cycle_budget.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT,ETHUSDT,SOLUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    conn = db.connect(str(db_path))
    db.init_db(conn)
    captured = {}
    try:
        monkeypatch.setattr(app_main, "_warmup_status_payload", lambda _conn: {"ready": False})

        def _fake_collect_backfill_once(conn, client, venue, symbols, **kwargs):
            captured.update(kwargs)
            return {"venue": venue, "symbols_total": len(symbols)}

        monkeypatch.setattr(app_main, "collect_backfill_once", _fake_collect_backfill_once)
        app_main._collect_backfill_cycle(conn, object(), "linear", ["BTCUSDT", "ETHUSDT", "SOLUSDT"], None, 2)
        assert captured["per_tf_budget"] == 3
    finally:
        conn.close()
        sys.modules.pop("app.main", None)


def test_backfill_thread_does_not_call_futures_meta_inline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "backfill_inline.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("COLLECT_INTERVAL_SEC", "5")
    monkeypatch.setenv("FUTURES_COLLECT_INTERVAL_SEC", "60")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")

    called = {"backfill": 0, "futures": 0}

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            pass
        def close(self):
            return None

    try:
        monkeypatch.setattr(app_main, "BybitPublicClient", _DummyClient)
        monkeypatch.setattr(app_main.db, "acquire_runtime_lock", lambda *args, **kwargs: True)
        monkeypatch.setattr(app_main, "_get_lock_conn", lambda: db.connect(str(db_path)))
        monkeypatch.setattr(app_main, "_get_conn", lambda: db.connect(str(db_path)))

        def _fake_backfill(*args, **kwargs):
            called["backfill"] += 1
            raise KeyboardInterrupt()

        def _fake_futures(*args, **kwargs):
            called["futures"] += 1
            return {}

        monkeypatch.setattr(app_main, "_collect_backfill_cycle", _fake_backfill)
        monkeypatch.setattr(app_main, "collect_futures_once", _fake_futures)

        with pytest.raises(KeyboardInterrupt):
            app_main._backfill_thread()

        assert called["backfill"] == 1
        assert called["futures"] == 0
    finally:
        sys.modules.pop("app.main", None)
