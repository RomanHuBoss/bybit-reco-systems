from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app import collector, db
from app.bybit_client import BybitPublicClient


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
    collector._DISABLED_SYMBOLS["spot"].clear()
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
    collector._DISABLED_SYMBOLS["spot"].clear()
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

    stats = collector.collect_once(conn, client, "spot", ["BTCUSDT"])

    tf15 = db.get_latest_ohlcv(conn, "spot", "BTCUSDT", 900, limit=120)
    tf30 = db.get_latest_ohlcv(conn, "spot", "BTCUSDT", 1800, limit=120)
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

    items = db.get_symbol_health(conn, [], ["BADUSDT"], active_venues=["linear"])
    assert items[0]["status"] == "ok"
    assert items[0]["disabled"] is False
    conn.close()
