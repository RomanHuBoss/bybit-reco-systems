from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import db


class _DummyThread:
    started_names: list[str] = []

    def __init__(self, *, target=None, name=None, daemon=None, **_kwargs):
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self):
        self.__class__.started_names.append(str(self.name or ""))


def test_lifespan_starts_backfill_thread(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "lifespan_backfill.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        _DummyThread.started_names = []
        monkeypatch.setattr(app_main.threading, "Thread", _DummyThread)

        async def _run():
            async with app_main.lifespan(app_main.app):
                return None

        asyncio.run(_run())

        assert _DummyThread.started_names == [
            "collector",
            "backfill",
            "sentiment",
            "reco",
            "llm_reviewer",
        ]
    finally:
        sys.modules.pop("app.main", None)


def test_health_endpoint_returns_warmup_summary(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "health_warmup.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    try:
        db.set_app_config_json(
            conn,
            "collector_warmup",
            {
                "ready": False,
                "ready_symbols": 1,
                "symbols_total": 4,
                "ready_ratio": 0.25,
                "required_tfs": [60, 900, 1800, 3600, 14400, 86400],
                "min_rows_per_tf": 80,
                "min_ready_ratio": 0.85,
                "min_ready_symbols": 1,
            },
        )
        client = TestClient(app_main.app)
        try:
            resp = client.get("/api/v1/health/symbols")
            assert resp.status_code == 200
            body = resp.json()
            assert body["warmup"]["ready"] is False
            assert body["warmup"]["ready_symbols"] == 1
            assert body["warmup"]["symbols_total"] == 4
            assert body["warmup"]["min_ready_ratio"] == 0.85
        finally:
            client.close()
    finally:
        conn.close()
        sys.modules.pop("app.main", None)
