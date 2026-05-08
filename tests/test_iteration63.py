from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app import collector, db
from app.bybit_client import BybitPublicClient
from app.features import funding_signal, oi_trend


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {"retCode": 0, "result": {"list": []}}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=httpx.Request("GET", "https://example.com"), response=httpx.Response(self.status_code))

    def json(self):
        return self._payload


class _SequencedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        return None



def test_bybit_client_retries_retryable_retcode_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    client = BybitPublicClient("https://api.bybit.com", max_retries=2, backoff_base_sec=0.0)
    transport = _SequencedTransport(
        [
            _FakeResponse(payload={"retCode": 10006, "retMsg": "Too many visits!"}),
            _FakeResponse(payload={"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}}),
        ]
    )
    client._client = transport  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    rows = client.get_tickers(category="linear")

    assert rows == [{"symbol": "BTCUSDT"}]
    assert len(transport.calls) == 2
    client.close()



def test_bybit_client_open_interest_forwards_start_and_end_params(monkeypatch: pytest.MonkeyPatch):
    client = BybitPublicClient("https://api.bybit.com", max_retries=0)
    transport = _SequencedTransport(
        [
            _FakeResponse(payload={
                "retCode": 0,
                "result": {
                    "list": [
                        {"timestamp": "1700003600000", "openInterest": "123.45"},
                    ]
                },
            })
        ]
    )
    client._client = transport  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    rows = client.get_open_interest("BTCUSDT", interval="1h", limit=7, start_ms=1_700_000_000_000, end_ms=1_700_003_600_000)

    assert rows == [{"ts": 1_700_003_600, "oi": 123.45}]
    _url, params = transport.calls[0]
    assert params["startTime"] == "1700000000000"
    assert params["endTime"] == "1700003600000"
    assert params["limit"] == "7"
    client.close()



def test_collect_futures_once_backfills_open_interest_gap_with_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["linear"].clear()

    conn = db.connect(str(tmp_path / "oi_gap.db"))
    db.init_db(conn)
    db.upsert_open_interest(conn, "BTCUSDT", [{"ts": 1_700_000_000, "oi": 100.0}])

    class GapClient:
        def __init__(self):
            self.calls = []

        def get_open_interest(self, symbol: str, interval: str = "1h", limit: int = 48, start_ms: int | None = None, end_ms: int | None = None):
            self.calls.append({
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "start_ms": start_ms,
                "end_ms": end_ms,
            })
            return [
                {"ts": 1_700_028_800, "oi": 125.0},
                {"ts": 1_700_025_200, "oi": 120.0},
            ]

    client = GapClient()
    now_ts = 1_700_032_400  # 9 hours later
    monkeypatch.setattr(db, "now_ts", lambda: now_ts)

    stats = collector.collect_futures_once(conn, client, ["BTCUSDT"])

    assert stats["open_interest_symbols"] == 1
    assert stats["open_interest_written"] == 2
    assert client.calls[0]["limit"] >= 10
    assert client.calls[0]["start_ms"] == (1_700_000_000 - 2 * 3600) * 1000
    assert client.calls[0]["end_ms"] == (now_ts + 3600) * 1000
    assert db.get_latest_open_interest_ts(conn, "BTCUSDT") == 1_700_028_800
    conn.close()



def test_collector_bootstraps_derived_timeframes_on_cold_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    base_ts = 1_700_000_000
    minute_aligned = base_ts - (base_ts % 60)
    hour_aligned = base_ts - (base_ts % 3600)
    day_aligned = base_ts - (base_ts % 86400)

    class BootstrapClient:
        def __init__(self):
            self.kline_calls: list[tuple[str, int, int | None]] = []

        def get_tickers(self, *, category: str, symbol: str):
            return [{
                "lastPrice": "100",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "100000",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int, start: int | None = None):
            self.kline_calls.append((interval, limit, start))
            if interval == "1":
                rows = []
                for idx in range(360):
                    ts = minute_aligned - (359 - idx) * 60
                    px = 100.0 + idx * 0.1
                    rows.append([str(ts * 1000), str(px), str(px + 1.0), str(px - 1.0), str(px + 0.2), "10", "0"])
                return rows
            if interval == "60":
                rows = []
                for idx in range(420):
                    ts = hour_aligned - (419 - idx) * 3600
                    px = 200.0 + idx
                    rows.append([str(ts * 1000), str(px), str(px + 1.0), str(px - 1.0), str(px + 0.2), "100", "0"])
                return rows
            if interval == "D":
                rows = []
                for idx in range(120):
                    ts = day_aligned - (119 - idx) * 86400
                    px = 300.0 + idx
                    rows.append([str(ts * 1000), str(px), str(px + 1.0), str(px - 1.0), str(px + 0.2), "1000", "0"])
                return rows
            if interval == "15":
                rows = []
                for idx in range(120):
                    ts = (minute_aligned - (119 - idx) * 900) - ((minute_aligned - (119 - idx) * 900) % 900)
                    px = 400.0 + idx * 0.1
                    rows.append([str(ts * 1000), str(px), str(px + 1.0), str(px - 1.0), str(px + 0.2), "15", "0"])
                return rows
            if interval == "30":
                rows = []
                for idx in range(120):
                    ts = (minute_aligned - (119 - idx) * 1800) - ((minute_aligned - (119 - idx) * 1800) % 1800)
                    px = 500.0 + idx * 0.1
                    rows.append([str(ts * 1000), str(px), str(px + 1.0), str(px - 1.0), str(px + 0.2), "30", "0"])
                return rows
            raise AssertionError(interval)

    conn = db.connect(str(tmp_path / "derived_bootstrap.db"))
    db.init_db(conn)
    client = BootstrapClient()
    monkeypatch.setattr(db, "now_ts", lambda: base_ts)

    stats = collector.collect_once(conn, client, "linear", ["BTCUSDT"])

    tf15 = db.get_latest_ohlcv(conn, "linear", "BTCUSDT", 900, limit=120)
    tf30 = db.get_latest_ohlcv(conn, "linear", "BTCUSDT", 1800, limit=120)
    assert len(tf15) >= 80
    assert len(tf30) >= 80
    assert stats["derived_tf_bootstrap_fetches"] == {"900": 1, "1800": 1}
    assert ("15", 120, None) in client.kline_calls
    assert ("30", 120, None) in client.kline_calls
    conn.close()



def test_get_symbol_health_clears_disabled_after_retry_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "health_retry.db"))
    db.init_db(conn)
    base_ts = 1_700_000_000
    monkeypatch.setattr(db, "now_ts", lambda: base_ts)
    db.log_decision(
        conn,
        "SYMBOL_DISABLED",
        None,
        {
            "venue": "linear",
            "symbol": "BADUSDT",
            "retry_after_sec": 300,
            "retry_at": base_ts + 300,
        },
    )

    monkeypatch.setattr(db, "now_ts", lambda: base_ts + 301)
    fresh_bar_ts = base_ts + 240
    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BADUSDT",
            "tf_sec": 60,
            "ts": fresh_bar_ts,
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
            "venue": "linear",
            "symbol": "BADUSDT",
            "ts": fresh_bar_ts + 30,
            "last": 100.5,
            "bid": 100.0,
            "ask": 101.0,
            "vol24h": 1000.0,
            "turnover24h": 100000.0,
        }],
    )

    items = db.get_symbol_health(conn, [], ["BADUSDT"], active_venues=["linear"])
    assert items[0]["status"] == "ok"
    assert items[0]["disabled"] is False
    conn.close()



def test_db_latest_ohlcv_ts_ignores_historical_invalid_newest_rows(tmp_path: Path):
    conn = db.connect(str(tmp_path / "ohlcv_poison.db"))
    db.init_db(conn)
    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": 1_700_000_000,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }],
    )
    # Simulate a poisoned historical row from an older build / manual import.
    conn.execute(
        """INSERT OR REPLACE INTO ohlcv(venue, symbol, tf_sec, ts, open, high, low, close, volume)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("linear", "BTCUSDT", 60, 1_800_000_000, 0.0, 0.0, 0.0, 0.0, 1.0),
    )
    conn.commit()

    assert db.get_latest_ohlcv_ts(conn, "linear", "BTCUSDT", 60) == 1_700_000_000
    latest_rows = db.get_latest_ohlcv(conn, "linear", "BTCUSDT", 60, limit=5)
    assert [int(row["ts"]) for row in latest_rows] == [1_700_000_000]
    conn.close()



def test_db_filters_invalid_funding_and_open_interest_rows(tmp_path: Path):
    conn = db.connect(str(tmp_path / "funding_oi_validation.db"))
    db.init_db(conn)

    db.upsert_funding_rate(
        conn,
        [
            {"symbol": "BTCUSDT", "ts": 1_700_000_000, "funding_rate": 0.0001, "next_funding_ts": 1_700_002_400},
            {"symbol": "BTCUSDT", "ts": 1_700_000_100, "funding_rate": float("nan"), "next_funding_ts": 1_700_002_800},
            {"symbol": "BTCUSDT", "ts": -5, "funding_rate": 0.0002, "next_funding_ts": 1_700_003_200},
        ],
    )
    funding = db.get_latest_funding_rate(conn, "BTCUSDT")
    assert funding == {
        "symbol": "BTCUSDT",
        "ts": 1_700_000_000,
        "funding_rate": 0.0001,
        "next_funding_ts": 1_700_002_400,
        "funding_interval_min": None,
    }

    db.upsert_open_interest(
        conn,
        "BTCUSDT",
        [
            {"ts": 1_700_000_000, "oi": 123.0},
            {"ts": 1_700_000_100, "oi": float("nan")},
            {"ts": 1_700_000_200, "oi": -1.0},
        ],
    )
    # Simulate a bad legacy import with a newer invalid row.
    conn.execute("INSERT OR REPLACE INTO open_interest(symbol, ts, oi) VALUES(?,?,?)", ("BTCUSDT", 1_800_000_000, -5.0))
    conn.commit()

    assert db.get_latest_open_interest_ts(conn, "BTCUSDT") == 1_700_000_000
    assert db.get_oi_series(conn, "BTCUSDT", limit=5) == [{"ts": 1_700_000_000, "oi": 123.0}]
    conn.close()



def test_collector_skips_redundant_4h_bootstrap_when_1h_history_is_sufficient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    base_ts = 1_700_000_000
    minute_aligned = base_ts - (base_ts % 60)
    hour_aligned = base_ts - (base_ts % 3600)
    day_aligned = base_ts - (base_ts % 86400)

    class Client:
        def __init__(self):
            self.intervals: list[str] = []

        def get_tickers(self, *, category: str, symbol: str):
            return [{
                "lastPrice": "100",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "100000",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int, start: int | None = None):
            self.intervals.append(interval)
            if interval == "1":
                return [
                    [str((minute_aligned - (359 - idx) * 60) * 1000), str(100 + idx * 0.1), str(101 + idx * 0.1), str(99 + idx * 0.1), str(100.5 + idx * 0.1), "10", "0"]
                    for idx in range(360)
                ]
            if interval == "60":
                return [
                    [str((hour_aligned - (419 - idx) * 3600) * 1000), str(200 + idx), str(201 + idx), str(199 + idx), str(200.5 + idx), "100", "0"]
                    for idx in range(420)
                ]
            if interval == "D":
                return [
                    [str((day_aligned - (119 - idx) * 86400) * 1000), str(300 + idx), str(301 + idx), str(299 + idx), str(300.5 + idx), "1000", "0"]
                    for idx in range(120)
                ]
            if interval in {"15", "30"}:
                return []
            raise AssertionError(f"unexpected interval {interval}")

    conn = db.connect(str(tmp_path / "skip_4h_bootstrap.db"))
    db.init_db(conn)
    monkeypatch.setattr(db, "now_ts", lambda: base_ts)
    client = Client()

    stats = collector.collect_once(conn, client, "linear", ["BTCUSDT"])

    assert "240" not in client.intervals
    assert stats["derived_tf_bootstrap_fetches"] == {}
    tf4h = db.get_latest_ohlcv(conn, "linear", "BTCUSDT", 14400, limit=120)
    assert len(tf4h) >= 96
    conn.close()



def test_feature_guards_handle_nonfinite_funding_and_dirty_oi_series():
    assert funding_signal(float("nan"))["signal"] == "unknown"

    series = [
        {"ts": 5, "oi": float("nan")},
        {"ts": 4, "oi": -1.0},
        {"ts": 3, "oi": 120.0},
        {"ts": 2, "oi": 100.0},
        {"ts": 1, "oi": 80.0},
    ]
    trend = oi_trend(series)
    assert trend["oi_now"] == 120.0
    assert trend["trend"] in {"growing", "stable", "falling"}
    assert trend["signal"] == "pending"
