from __future__ import annotations

from pathlib import Path

import pytest

from app import collector, db
from app.bybit_client import BybitPublicClient


class _FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _Transport:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return _FakeResponse(self.payload)

    def close(self):
        return None


def test_bybit_ticker_filter_excludes_delivery_and_premarket_contracts() -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=0)
    client._client = _Transport(
        {
            "retCode": 0,
            "result": {
                "list": [
                    {"symbol": "BTCUSDT", "lastPrice": "100000", "deliveryTime": "0", "curPreListingPhase": ""},
                    {"symbol": "ETHUSDT", "lastPrice": "2500", "deliveryTime": "1893456000000"},
                    {"symbol": "SOLUSDT", "lastPrice": "100", "deliveryTime": "0", "curPreListingPhase": "CallAuction"},
                    {"symbol": "XRPUSDC", "lastPrice": "1"},
                ]
            },
        }
    )  # type: ignore[attr-defined]

    try:
        rows = client.get_tickers("linear")
    finally:
        client.close()

    assert rows == [{"symbol": "BTCUSDT", "lastPrice": "100000", "deliveryTime": "0", "curPreListingPhase": ""}]


def test_collect_once_does_not_relabel_wrong_symbol_ticker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    conn = db.connect(str(tmp_path / "wrong-symbol-ticker.db"))
    db.init_db(conn)
    monkeypatch.setattr(collector, "_API_FETCH_TFS", ())
    monkeypatch.setattr(collector, "_DERIVED_TF_SOURCES", {})
    monkeypatch.setattr(db, "now_ts", lambda: 1_700_000_000)

    class WrongSymbolClient:
        def get_tickers(self, *, category: str, symbol: str | None = None):
            if symbol is None:
                return []
            return [{
                "symbol": "ETHUSDT",
                "lastPrice": "2500",
                "bid1Price": "2499",
                "ask1Price": "2501",
                "volume24h": "1000",
                "turnover24h": "2500000",
                "deliveryTime": "0",
            }]

    stats = collector.collect_once(conn, WrongSymbolClient(), "linear", ["BTCUSDT"])

    assert stats["tickers_written"] == 0
    assert stats["ticker_missing_symbols"] == 1
    assert db.get_latest_ticker(conn, "linear", "BTCUSDT") is None
    conn.close()
