from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collector, db


class _RecordingFetchClient:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def get_tickers(self, category: str, symbol: str | None = None):
        items = [{
            "symbol": str(symbol or "BTCUSDT"),
            "lastPrice": "100",
            "bid1Price": "99",
            "ask1Price": "101",
            "volume24h": "1",
            "turnover24h": "1",
        }]
        return items if symbol else [{**items[0], "symbol": "BTCUSDT"}]

    def get_kline(self, category: str, symbol: str, interval: str = "1", limit: int = 200, start=None, end=None):
        self.calls.append({
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "limit": int(limit),
            "start": start,
            "end": end,
        })
        return []


@pytest.mark.parametrize("tf_sec,interval,limit_rows", [(3600, "60", 10), (86400, "D", 5)])
def test_collect_backfill_once_forces_cold_fetch_when_series_is_fresh_but_short(tmp_path: Path, tf_sec: int, interval: str, limit_rows: int):
    conn = db.connect(str(tmp_path / f"short_{tf_sec}.db"))
    db.init_db(conn)
    now_ts = db.now_ts()
    rows = []
    for idx in range(limit_rows):
        ts = now_ts - (limit_rows - idx) * tf_sec + tf_sec // 2
        rows.append({
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": tf_sec,
            "ts": ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        })
    db.upsert_ohlcv(conn, rows)
    client = _RecordingFetchClient()
    collector._BACKFILL_ROUND_ROBIN_CURSOR.clear()
    try:
        stats = collector.collect_backfill_once(
            conn,
            client,
            "linear",
            ["BTCUSDT"],
            max_workers=1,
            per_tf_budget=1,
            min_rows_per_tf=80,
        )
        calls = [call for call in client.calls if call["interval"] == interval]
        assert stats["symbols_total"] == 1
        assert calls, f"expected cold fetch for tf {tf_sec}"
        assert calls[0]["start"] is None
        assert calls[0]["end"] is None
    finally:
        conn.close()


def test_lifespan_does_not_start_disabled_llm_reviewer_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "lifespan_llm_disabled.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "0")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    started: list[str] = []

    class _DummyThread:
        def __init__(self, *args, name: str | None = None, **kwargs):
            self.name = str(name or "")

        def start(self):
            started.append(self.name)

    try:
        monkeypatch.setattr(app_main.threading, "Thread", _DummyThread)

        async def _run() -> None:
            async with app_main.lifespan(app_main.app):
                return None

        asyncio.run(_run())
        assert "collector" in started
        assert "llm_reviewer" not in started

        with db.connect(str(db_path)) as conn:
            thread_state = db.get_app_config_json(
                conn,
                app_main._background_thread_state_key("llm_reviewer"),
                default={},
            ) or {}
            async_state = db.get_app_config_json(conn, app_main.LLM_REVIEW_ASYNC_STATUS_APP_KEY, default={}) or {}

        assert thread_state.get("state") == "disabled"
        assert async_state.get("enabled") is False
        assert async_state.get("state") == "disabled"
    finally:
        sys.modules.pop("app.main", None)


def test_lifespan_starts_backfill_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "lifespan_backfill.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    started: list[str] = []

    class _DummyThread:
        def __init__(self, *args, name: str | None = None, **kwargs):
            self.name = str(name or "")

        def start(self):
            started.append(self.name)

    try:
        monkeypatch.setattr(app_main.threading, "Thread", _DummyThread)

        async def _run() -> None:
            async with app_main.lifespan(app_main.app):
                return None

        asyncio.run(_run())
        assert "collector" in started
        assert "backfill" in started
        assert "futures_meta" in started
        assert "reco" in started
    finally:
        sys.modules.pop("app.main", None)


def test_api_health_symbols_exposes_warmup_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "health_warmup.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    conn = db.connect(str(db_path))
    db.init_db(conn)
    client = TestClient(app_main.app)
    try:
        resp = client.get("/api/v1/health/symbols")
        assert resp.status_code == 200
        body = resp.json()
        assert "warmup" in body
        assert body["warmup"]["ready"] is False
        assert body["warmup"]["symbols_total"] == 1
        assert body["warmup"]["ready_symbols"] == 0
        assert body["warmup"]["derived_on_read"] is True
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)
