from __future__ import annotations

import importlib
from contextlib import closing
import sys
from pathlib import Path

import pytest

from app import collector, db


def _seed_flat_ohlcv(conn, *, venue: str, symbol: str, now_ts: int, tf_sec: int, n: int, price: float = 100.0) -> None:
    rows = []
    for idx in range(n):
        ts = now_ts - (n - 1 - idx) * tf_sec
        rows.append(
            {
                "venue": venue,
                "symbol": symbol,
                "tf_sec": tf_sec,
                "ts": ts,
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "close": price,
                "volume": 10.0 + idx,
            }
        )
    db.upsert_ohlcv(conn, rows)


def test_get_recommender_warmup_status_requires_multi_tf_depth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "warmup.db"))
    db.init_db(conn)
    now = 1_700_000_000
    monkeypatch.setattr(db, "now_ts", lambda: now)

    db.insert_tickers(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "ts": now - 30,
            "last": 100.0,
            "bid": 99.9,
            "ask": 100.1,
            "vol24h": 1000.0,
            "turnover24h": 100000.0,
        }],
    )
    _seed_flat_ohlcv(conn, venue="linear", symbol="BTCUSDT", now_ts=now - 60, tf_sec=60, n=100)

    status = db.get_recommender_warmup_status(
        conn,
        [],
        ["BTCUSDT"],
        stale_sec=300,
        min_rows_per_tf=80,
        active_venues=["linear"],
    )
    linear = status["venues"][0]
    assert linear["ready_symbols"] == 0
    assert linear["reason_counts"]["tf_900_short"] == 1
    assert linear["reason_counts"]["tf_1800_short"] == 1

    for tf_sec in (900, 1800, 3600, 14_400, 86_400):
        _seed_flat_ohlcv(conn, venue="linear", symbol="BTCUSDT", now_ts=now - tf_sec, tf_sec=tf_sec, n=100)

    status = db.get_recommender_warmup_status(
        conn,
        [],
        ["BTCUSDT"],
        stale_sec=300,
        min_rows_per_tf=80,
        active_venues=["linear"],
    )
    linear = status["venues"][0]
    assert linear["ready_symbols"] == 1
    assert linear["ready_ratio"] == 1.0
    conn.close()


def test_make_runtime_lock_heartbeat_uses_fresh_connection_each_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "hb_fresh.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        conn = db.connect(str(db_path))
        db.init_db(conn)
        conn.close()
        lock_conn = db.connect(str(app_main.settings.runtime_lock_db_path))
        db.init_runtime_lock_db(lock_conn)
        db.acquire_runtime_lock(lock_conn, "runtime:test", app_main.RUNTIME_OWNER, ttl_sec=120)
        lock_conn.close()

        opened: list[int] = []
        original_get_lock_conn = app_main._get_lock_conn

        def traced_get_lock_conn():
            opened.append(1)
            return original_get_lock_conn()

        monkeypatch.setattr(app_main, "_get_lock_conn", traced_get_lock_conn)
        heartbeat = app_main._make_runtime_lock_heartbeat("runtime:test")
        assert heartbeat() is True
        assert len(opened) == 2
    finally:
        sys.modules.pop("app.main", None)


def test_runtime_lock_uses_sidecar_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "main.db"
    lock_db_path = tmp_path / "locks.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(lock_db_path))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        assert Path(app_main.settings.runtime_lock_db_path) == lock_db_path
        with closing(app_main._get_lock_conn()) as conn:
            db.init_runtime_lock_db(conn)
            assert db.acquire_runtime_lock(conn, "runtime:test", app_main.RUNTIME_OWNER, ttl_sec=120) is True
        assert lock_db_path.exists()
    finally:
        sys.modules.pop("app.main", None)


class _PriorityClient:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def get_tickers(self, category: str, symbol: str | None = None):
        if symbol is None:
            return [
                {"symbol": "BTCUSDT", "lastPrice": "100", "bid1Price": "99", "ask1Price": "101", "volume24h": "1", "turnover24h": "1"},
                {"symbol": "ETHUSDT", "lastPrice": "200", "bid1Price": "199", "ask1Price": "201", "volume24h": "1", "turnover24h": "1"},
            ]
        return []

    def get_kline(self, *, category: str, symbol: str, interval: str = "1", limit: int = 200, start=None, end=None):
        self.calls.append((symbol, interval))
        base = 1_700_000_000
        step = {"1": 60, "15": 900, "30": 1800, "60": 3600, "240": 14_400, "D": 86_400}[interval]
        rows = []
        for idx in range(100):
            ts = (base - (99 - idx) * step) * 1000
            rows.append([ts, "100", "101", "99", "100", "10", "0"])
        return rows


def test_collect_once_prioritizes_1m_before_slower_timeframes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "priority.db"))
    db.init_db(conn)
    now = 1_700_000_000
    monkeypatch.setattr(db, "now_ts", lambda: now)

    client = _PriorityClient()
    stats = collector.collect_once(conn, client, "linear", ["BTCUSDT", "ETHUSDT"], max_workers=1)

    assert stats["tickers_written"] == 2
    assert client.calls[:2] == [("BTCUSDT", "1"), ("ETHUSDT", "1")]
    # Slower REST frames must start only after the initial 1m pass finished.
    assert all(interval == "1" for _, interval in client.calls[:2])
    conn.close()
