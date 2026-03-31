from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app import collector, db



def test_collect_once_commits_bootstrap_stage_before_lock_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["spot"].clear()
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    conn = db.connect(str(tmp_path / "bootstrap_stage_commit.db"))
    db.init_db(conn)

    monkeypatch.setattr(db, "now_ts", lambda: 1_700_000_000)
    monkeypatch.setattr(collector, "_API_FETCH_TFS", ())
    monkeypatch.setattr(collector, "_DERIVED_TF_SOURCES", {900: 60})
    monkeypatch.setattr(collector, "_DERIVED_TF_BOOTSTRAP_MIN_ROWS", {900: 96})

    class BootstrapClient:
        def get_tickers(self, *, category: str, symbol: str | None = None):
            if symbol is None:
                return []
            return [{
                "symbol": symbol,
                "lastPrice": "100",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "100000",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int, start: int | None = None, end: int | None = None):
            assert interval == "15"
            base_ts = 1_700_000_000 - (1_700_000_000 % 900)
            return [
                [str((base_ts - (limit - 1 - idx) * 900) * 1000), "100", "101", "99", "100.5", "10", "0"]
                for idx in range(limit)
            ]

    heartbeat_calls = {"count": 0}

    def heartbeat() -> bool:
        heartbeat_calls["count"] += 1
        return heartbeat_calls["count"] < 3

    with pytest.raises(collector.RuntimeLockLostError):
        collector.collect_once(conn, BootstrapClient(), "spot", ["BTCUSDT"], heartbeat=heartbeat)

    tf15 = db.get_latest_ohlcv(conn, "spot", "BTCUSDT", 900, limit=120)
    assert len(tf15) >= 96
    conn.close()



def test_get_symbol_health_ignores_future_poisoned_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "health_future_poison.db"))
    db.init_db(conn)
    base_ts = 1_700_000_000
    monkeypatch.setattr(db, "now_ts", lambda: base_ts)

    db.upsert_ohlcv(
        conn,
        [{
            "venue": "spot",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts - 60,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }],
    )
    db.insert_tickers(
        conn,
        [{
            "venue": "spot",
            "symbol": "BTCUSDT",
            "ts": base_ts - 30,
            "last": 100.0,
            "bid": 99.0,
            "ask": 101.0,
            "vol24h": 1000.0,
            "turnover24h": 100000.0,
        }],
    )

    conn.execute(
        """INSERT OR REPLACE INTO ohlcv(venue, symbol, tf_sec, ts, open, high, low, close, volume)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("spot", "BTCUSDT", 60, base_ts + 86400, 200.0, 201.0, 199.0, 200.5, 10.0),
    )
    conn.execute(
        """INSERT OR REPLACE INTO ticker_snap(venue, symbol, ts, last, bid, ask, vol24h, turnover24h)
           VALUES(?,?,?,?,?,?,?,?)""",
        ("spot", "BTCUSDT", base_ts + 86400, 200.0, 199.0, 201.0, 1000.0, 100000.0),
    )
    conn.commit()

    items = db.get_symbol_health(conn, ["BTCUSDT"], [], stale_sec=300, active_venues=["spot"])
    assert items[0]["status"] == "ok"
    assert items[0]["last_candle_ts"] == base_ts - 60
    assert items[0]["last_ticker_ts"] == base_ts - 30
    conn.close()



def test_get_latest_ticker_prefers_valid_row_over_future_poisoned_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "ticker_future_poison.db"))
    db.init_db(conn)
    base_ts = 1_700_000_000
    monkeypatch.setattr(db, "now_ts", lambda: base_ts)

    db.insert_tickers(
        conn,
        [{
            "venue": "spot",
            "symbol": "BTCUSDT",
            "ts": base_ts - 15,
            "last": 100.0,
            "bid": 99.0,
            "ask": 101.0,
            "vol24h": 1000.0,
            "turnover24h": 100000.0,
        }],
    )
    conn.execute(
        """INSERT OR REPLACE INTO ticker_snap(venue, symbol, ts, last, bid, ask, vol24h, turnover24h)
           VALUES(?,?,?,?,?,?,?,?)""",
        ("spot", "BTCUSDT", base_ts + 7200, 200.0, 199.0, 201.0, 1000.0, 100000.0),
    )
    conn.commit()

    latest = db.get_latest_ticker(conn, "spot", "BTCUSDT")
    assert latest is not None
    assert int(latest["ts"]) == base_ts - 15
    conn.close()



def test_collect_once_falls_back_to_per_symbol_tickers_when_batch_fetch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["spot"].clear()
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    conn = db.connect(str(tmp_path / "ticker_batch_fallback.db"))
    db.init_db(conn)
    monkeypatch.setattr(collector, "_API_FETCH_TFS", ())
    monkeypatch.setattr(collector, "_DERIVED_TF_SOURCES", {})
    monkeypatch.setattr(db, "now_ts", lambda: 1_700_000_000)

    class FallbackClient:
        def __init__(self):
            self.calls: list[str | None] = []

        def get_tickers(self, *, category: str, symbol: str | None = None):
            self.calls.append(symbol)
            if symbol is None:
                raise RuntimeError("batch ticker outage")
            return [{
                "symbol": symbol,
                "lastPrice": "100",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "100000",
            }]

    client = FallbackClient()
    stats = collector.collect_once(conn, client, "spot", ["BTCUSDT", "ETHUSDT"])

    assert stats["tickers_written"] == 2
    assert client.calls[0] is None
    assert set(client.calls[1:]) == {"BTCUSDT", "ETHUSDT"}
    log_rows = conn.execute(
        "SELECT details_json FROM decision_log WHERE action='COLLECT_ERROR' ORDER BY id ASC"
    ).fetchall()
    assert log_rows
    assert "ticker_batch" in log_rows[0]["details_json"]
    conn.close()



def test_collector_thread_heartbeat_uses_fresh_lock_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "collector_hb_conn.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    monkeypatch.setenv("VENUES", "spot")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        seen = {"calls": 0}

        class DummyClient:
            def __init__(self, *args, **kwargs):
                pass
            def close(self):
                return None

        def fake_collect_once(conn, client, venue, symbols, heartbeat=None, *, max_workers=1):
            assert heartbeat is not None
            assert heartbeat() is True
            assert heartbeat() is True
            seen["calls"] += 2
            return {"venue": venue, "tickers_written": 0, "funding_written": 0, "ohlcv_written": 0}

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        monkeypatch.setattr(app_main, "BybitPublicClient", DummyClient)
        monkeypatch.setattr(app_main, "collect_once", fake_collect_once)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._collector_thread()

        assert seen["calls"] == 2
    finally:
        sys.modules.pop("app.main", None)
