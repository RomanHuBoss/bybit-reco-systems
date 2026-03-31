from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

from app import db


class _DummyThread:
    started: list[str] = []

    def __init__(self, *, target=None, name=None, daemon=None, **kwargs):
        self.target = target
        self.name = str(name or "")
        self.daemon = daemon

    def start(self):
        self.started.append(self.name)


def test_lifespan_starts_backfill_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "lifespan80.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        _DummyThread.started = []
        monkeypatch.setattr(app_main.threading, "Thread", _DummyThread)

        async def _run() -> None:
            async with app_main.lifespan(app_main.app):
                return None

        asyncio.run(_run())

        assert _DummyThread.started == ["collector", "backfill", "sentiment", "reco", "llm_reviewer"]
    finally:
        sys.modules.pop("app.main", None)



def test_symbol_health_exposes_warmup_when_freshness_is_ok_but_readiness_is_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "health80.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    now_ts = int(time.time())
    try:
        conn = db.connect(str(db_path))
        db.init_db(conn)
        db.insert_tickers(
            conn,
            [
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "ts": now_ts,
                    "last": 100.0,
                    "bid": 99.5,
                    "ask": 100.5,
                    "vol24h": 1.0,
                    "turnover24h": 1.0,
                }
            ],
        )
        db.upsert_ohlcv(
            conn,
            [
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "tf_sec": 60,
                    "ts": now_ts,
                    "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.2,
                        "volume": 10.0,
                }
            ],
        )
        db.set_app_config_json(
            conn,
            "collector_warmup",
            {
                "ready": False,
                "symbols_total": 1,
                "ready_symbols": 0,
                "ready_ratio": 0.0,
                "required_tfs": [60, 900, 1800, 3600, 14400, 86400],
                "min_rows_per_tf": 80,
                "min_ready_ratio": 0.85,
                "min_ready_symbols": 1,
                "venues": [
                    {
                        "venue": "linear",
                        "symbols_total": 1,
                        "ready_symbols": 0,
                        "ready_ratio": 0.0,
                        "not_ready_symbols": 1,
                        "reason_counts": {"tf_900_short": 1, "tf_1800_short": 1, "tf_3600_short": 1, "tf_14400_short": 1, "tf_86400_short": 1},
                        "sample_not_ready": [
                            {
                                "symbol": "BTCUSDT",
                                "reasons": ["tf_900_short", "tf_1800_short", "tf_3600_short", "tf_14400_short", "tf_86400_short"],
                                "ticker_age_sec": 0,
                                "candle_age_sec": 0,
                            }
                        ],
                    }
                ],
            },
        )

        payload = app_main.api_symbol_health()
        assert payload["summary"]["ok"] == 1
        assert payload["symbols"][0]["status"] == "ok"
        assert payload["warmup"]["ready"] is False
        assert payload["warmup"]["ready_symbols"] == 0
        assert payload["warmup"]["required_tfs"] == [60, 900, 1800, 3600, 14400, 86400]
    finally:
        conn.close()
        sys.modules.pop("app.main", None)
