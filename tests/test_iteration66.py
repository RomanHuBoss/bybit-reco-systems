from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app import collector, db



def test_collect_once_backfills_long_kline_gap_without_losing_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    conn = db.connect(str(tmp_path / "kline_gap_long.db"))
    db.init_db(conn)

    base_ts = 1_700_000_000 - (1_700_000_000 % 60)
    now_ts = base_ts + 840 * 60  # 14 hours later
    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }],
    )

    monkeypatch.setattr(db, "now_ts", lambda: now_ts)
    monkeypatch.setattr(collector, "_API_FETCH_TFS", (60,))
    monkeypatch.setattr(collector, "_DERIVED_TF_SOURCES", {})

    class GapClient:
        def __init__(self, current_ts: int):
            self.current_ts = current_ts
            self.kline_calls: list[dict[str, int | None]] = []

        def get_tickers(self, *, category: str, symbol: str):
            return [{
                "symbol": symbol,
                "lastPrice": "100",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "100000",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int, start: int | None = None, end: int | None = None):
            self.kline_calls.append({"interval": interval, "limit": limit, "start": start, "end": end})
            assert interval == "1"
            step_sec = 60
            start_ts = (start or 0) // 1000
            end_ts = min(self.current_ts, (end or self.current_ts * 1000) // 1000)
            rows = []
            ts = start_ts
            while ts <= end_ts and len(rows) < limit:
                px = 100.0 + ((ts - base_ts) / step_sec) * 0.01
                rows.append([str(ts * 1000), str(px), str(px + 1.0), str(px - 1.0), str(px + 0.2), "10", "0"])
                ts += step_sec
            return rows

    client = GapClient(now_ts)
    stats = collector.collect_once(conn, client, "linear", ["BTCUSDT"])

    assert stats["api_tf_fetches"] == {"60": 1}
    assert len(client.kline_calls) >= 3
    assert all(call["end"] is not None for call in client.kline_calls)

    rows = list(reversed(db.get_latest_ohlcv(conn, "linear", "BTCUSDT", 60, limit=900)))
    ts_values = [int(r["ts"]) for r in rows if int(r["ts"]) >= base_ts]
    assert ts_values[0] == base_ts
    assert ts_values[-1] == now_ts
    assert len(ts_values) == 841
    assert all((b - a) == 60 for a, b in zip(ts_values, ts_values[1:]))
    conn.close()



def test_collect_futures_once_paginates_open_interest_cursor_for_long_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["linear"].clear()

    conn = db.connect(str(tmp_path / "oi_cursor_gap.db"))
    db.init_db(conn)
    db.upsert_open_interest(conn, "BTCUSDT", [{"ts": 1_700_000_000, "oi": 100.0}])

    now_ts = 1_700_900_000  # much later than 200h
    monkeypatch.setattr(db, "now_ts", lambda: now_ts)

    class CursorClient:
        def __init__(self):
            self.calls: list[dict[str, object]] = []

        def get_open_interest_page(self, symbol: str, interval: str = "1h", limit: int = 48, start_ms: int | None = None, end_ms: int | None = None, cursor: str | None = None):
            self.calls.append({
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "cursor": cursor,
            })
            if cursor is None:
                return ([{"ts": 1_700_720_000, "oi": 120.0}, {"ts": 1_700_723_600, "oi": 121.0}], "cursor-1")
            if cursor == "cursor-1":
                return ([{"ts": 1_700_727_200, "oi": 122.0}, {"ts": 1_700_730_800, "oi": 123.0}], None)
            raise AssertionError(cursor)

    client = CursorClient()
    stats = collector.collect_futures_once(conn, client, ["BTCUSDT"])

    assert stats["open_interest_symbols"] == 1
    assert stats["open_interest_written"] == 4
    assert [call["cursor"] for call in client.calls] == [None, "cursor-1"]
    assert int(client.calls[0]["limit"]) == 200
    assert db.get_latest_open_interest_ts(conn, "BTCUSDT") == 1_700_730_800
    conn.close()



def test_collect_once_does_not_commit_partial_stage_when_lock_is_lost_after_buffered_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    conn = db.connect(str(tmp_path / "collector_buffered_lock_loss.db"))
    db.init_db(conn)

    monkeypatch.setattr(collector, "_API_FETCH_TFS", (60,))
    monkeypatch.setattr(collector, "_DERIVED_TF_SOURCES", {})
    monkeypatch.setattr(db, "now_ts", lambda: 1_700_000_000)

    class MixedClient:
        def get_tickers(self, *, category: str, symbol: str):
            return [{
                "symbol": symbol,
                "lastPrice": "100",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "100000",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int, start: int | None = None, end: int | None = None):
            raise RuntimeError("simulated kline failure")

    with pytest.raises(collector.RuntimeLockLostError):
        collector.collect_once(conn, MixedClient(), "linear", ["BTCUSDT"], heartbeat=lambda: False)

    # Nothing should be committed: stage writes and buffered error logs are still in-memory until the stage ends.
    ticker_count = int(conn.execute("SELECT COUNT(*) AS c FROM ticker_snap").fetchone()["c"])
    ohlcv_count = int(conn.execute("SELECT COUNT(*) AS c FROM ohlcv").fetchone()["c"])
    log_count = int(conn.execute("SELECT COUNT(*) AS c FROM decision_log").fetchone()["c"])
    assert ticker_count == 1  # stage-1 commit completed before the failing stage started
    assert ohlcv_count == 0
    assert log_count == 0
    conn.close()




def test_bybit_client_open_interest_page_forwards_cursor(monkeypatch: pytest.MonkeyPatch):
    from app.bybit_client import BybitPublicClient

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {
                "retCode": 0,
                "result": {
                    "list": [{"timestamp": "1700003600000", "openInterest": "123.45"}],
                    "nextPageCursor": "next-cursor",
                },
            }

    class _Transport:
        def __init__(self):
            self.calls = []
        def get(self, url, params=None):
            self.calls.append((url, dict(params or {})))
            return _FakeResponse()
        def close(self):
            return None

    client = BybitPublicClient("https://api.bybit.com", max_retries=0)
    transport = _Transport()
    client._client = transport  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    rows, cursor = client.get_open_interest_page(
        "BTCUSDT",
        interval="1h",
        limit=7,
        start_ms=1_700_000_000_000,
        end_ms=1_700_003_600_000,
        cursor="prev-cursor",
    )

    assert rows == [{"ts": 1_700_003_600, "oi": 123.45}]
    assert cursor == "next-cursor"
    _url, params = transport.calls[0]
    assert params["cursor"] == "prev-cursor"
    assert params["limit"] == "7"
    client.close()


def test_collector_thread_rolls_back_partial_cycle_on_runtime_lock_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "collector_lock_loss_rollback.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(
            app_main.settings,
            venues=["linear"],
            symbols_linear=["BTCUSDT"],
                collect_interval_sec=5,
            futures_collect_interval_sec=3600,
        )

        class DummyClient:
            def __init__(self, *args, **kwargs):
                pass
            def close(self):
                return None

        def fake_collect_once(conn, client, venue, symbols, heartbeat=None, *, max_workers=1):
            db.insert_tickers(
                conn,
                [{
                    "venue": venue,
                    "symbol": symbols[0],
                    "ts": 1_700_000_000,
                    "last": 100.0,
                    "bid": 99.0,
                    "ask": 101.0,
                    "vol24h": 1000.0,
                    "turnover24h": 100000.0,
                }],
                commit=False,
            )
            raise app_main.RuntimeLockLostError("lost leadership during test")

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        monkeypatch.setattr(app_main, "BybitPublicClient", DummyClient)
        monkeypatch.setattr(app_main, "collect_once", fake_collect_once)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._collector_thread()

        conn = db.connect(str(db_path))
        assert int(conn.execute("SELECT COUNT(*) AS c FROM ticker_snap").fetchone()["c"]) == 0
        err_count = int(conn.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR'").fetchone()["c"])
        assert err_count == 1
        conn.close()
    finally:
        sys.modules.pop("app.main", None)
