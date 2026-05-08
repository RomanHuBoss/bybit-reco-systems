from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app import collector, db


class _RecordingKlineClient:
    def __init__(self):
        self.intervals: list[tuple[str, str]] = []

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
        self.intervals.append((str(symbol), str(interval)))
        return []


class _NoopClient:
    def close(self):
        return None


def test_collect_once_hot_path_skips_slow_tf_and_bootstrap_fetches(tmp_path: Path):
    conn = db.connect(str(tmp_path / "hot_only.db"))
    db.init_db(conn)
    client = _RecordingKlineClient()
    try:
        stats = collector.collect_once(
            conn,
            client,
            "linear",
            ["BTCUSDT"],
            max_workers=1,
            api_fetch_tfs=(60,),
            allow_derived_bootstrap=False,
        )
        assert stats["symbols_total"] == 1
        assert all(interval == "1" for _symbol, interval in client.intervals)
        assert stats["derived_tf_bootstrap_fetches"] == {}
    finally:
        conn.close()



def test_collect_backfill_once_respects_per_tf_budget(tmp_path: Path):
    conn = db.connect(str(tmp_path / "backfill_budget.db"))
    db.init_db(conn)
    client = _RecordingKlineClient()
    collector._BACKFILL_ROUND_ROBIN_CURSOR.clear()
    try:
        stats = collector.collect_backfill_once(
            conn,
            client,
            "linear",
            ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            max_workers=1,
            per_tf_budget=1,
        )
        by_interval: dict[str, list[str]] = {}
        for symbol, interval in client.intervals:
            by_interval.setdefault(interval, []).append(symbol)
        assert stats["budget_per_tf"] == 1
        assert len(by_interval.get("60", [])) <= 1  # 1h backfill
        assert len(by_interval.get("D", [])) <= 1   # 1d backfill
        assert len(by_interval.get("15", [])) <= 1  # 15m bootstrap
        assert len(by_interval.get("30", [])) <= 1  # 30m bootstrap
    finally:
        conn.close()



def test_collector_thread_hot_loop_does_not_run_futures_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "collector_hot.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(
            app_main.settings,
            venues=["linear"],
            symbols_linear=["BTCUSDT"],
                collect_interval_sec=5,
            futures_collect_interval_sec=60,
        )
        futures_calls = {"count": 0}

        monkeypatch.setattr(app_main, "BybitPublicClient", lambda *_args, **_kwargs: _NoopClient())
        monkeypatch.setattr(app_main, "_collect_hot_once", lambda *args, **kwargs: {"venue": "linear"})
        monkeypatch.setattr(
            app_main,
            "collect_futures_once",
            lambda *args, **kwargs: futures_calls.__setitem__("count", futures_calls["count"] + 1) or {"venue": "linear"},
        )
        monkeypatch.setattr(app_main, "_interval_loop_wait", lambda *_args, **_kwargs: (_ for _ in ()).throw(StopIteration))

        with pytest.raises(StopIteration):
            app_main._collector_thread()

        assert futures_calls["count"] == 0
    finally:
        sys.modules.pop("app.main", None)



def test_futures_meta_thread_runs_outside_hot_and_backfill_loops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "backfill_thread.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(
            app_main.settings,
            venues=["linear"],
            symbols_linear=["BTCUSDT"],
                collect_interval_sec=5,
            futures_collect_interval_sec=60,
        )
        futures_calls = {"count": 0}

        monkeypatch.setattr(app_main, "BybitPublicClient", lambda *_args, **_kwargs: _NoopClient())
        monkeypatch.setattr(app_main, "_collect_backfill_cycle", lambda *args, **kwargs: {"venue": "linear"})

        def fake_futures(*args, **kwargs):
            futures_calls["count"] += 1
            return {"venue": "linear", "open_interest_symbols": 1, "open_interest_written": 1}

        monkeypatch.setattr(app_main, "collect_futures_once", fake_futures)
        monkeypatch.setattr(app_main, "_interval_loop_wait", lambda *_args, **_kwargs: (_ for _ in ()).throw(StopIteration))
        monkeypatch.setattr(app_main, "_warmup_status_payload", lambda _conn: {"ready": True})

        with pytest.raises(StopIteration):
            app_main._futures_meta_thread()

        assert futures_calls["count"] == 1
    finally:
        sys.modules.pop("app.main", None)
