from __future__ import annotations

from pathlib import Path
from typing import Any

from app import db
from app.collector import _fetch_ticker_payloads


class _FundingIntervalClient:
    def __init__(self, *, ticker_interval_hour: str | None = None, instrument_interval_min: str | int | None = "240") -> None:
        self.ticker_interval_hour = ticker_interval_hour
        self.instrument_interval_min = instrument_interval_min
        self.instrument_calls: list[tuple[str, str]] = []

    def get_tickers(self, category: str, symbol: str | None = None) -> list[dict[str, Any]]:
        ticker: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "lastPrice": "100",
            "bid1Price": "99.9",
            "ask1Price": "100.1",
            "volume24h": "1000",
            "turnover24h": "100000",
            "fundingRate": "0.0001",
            "nextFundingTime": "1710000000000",
            "deliveryTime": "0",
            "time": "1710000000000",
        }
        if self.ticker_interval_hour is not None:
            ticker["fundingIntervalHour"] = self.ticker_interval_hour
        return [ticker]

    def get_instrument_info(self, category: str, symbol: str) -> dict[str, Any] | None:
        self.instrument_calls.append((category, symbol))
        return {
            "category": "linear",
            "symbol": "BTCUSDT",
            "status": "Trading",
            "contractType": "LinearPerpetual",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "deliveryTime": "0",
            "isPreListing": False,
            "fundingInterval": self.instrument_interval_min,
        }


def test_collector_fills_missing_funding_interval_from_instrument_info(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "collector_funding_interval.db"))
    db.init_db(conn)
    client = _FundingIntervalClient(ticker_interval_hour=None, instrument_interval_min="240")
    try:
        ticker_rows, funding_rows, missing_symbols = _fetch_ticker_payloads(
            conn,
            client,  # type: ignore[arg-type]
            "linear",
            "linear",
            ["BTCUSDT"],
            {},
            1710000000,
        )
    finally:
        conn.close()

    assert missing_symbols == []
    assert len(ticker_rows) == 1
    assert len(funding_rows) == 1
    assert funding_rows[0]["funding_rate"] == 0.0001
    assert funding_rows[0]["next_funding_ts"] == 1710000000
    assert funding_rows[0]["funding_interval_min"] == 240
    assert client.instrument_calls == [("linear", "BTCUSDT")]


def test_collector_keeps_ticker_funding_interval_without_extra_instrument_call(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "collector_ticker_interval.db"))
    db.init_db(conn)
    client = _FundingIntervalClient(ticker_interval_hour="8", instrument_interval_min="240")
    try:
        _ticker_rows, funding_rows, _missing_symbols = _fetch_ticker_payloads(
            conn,
            client,  # type: ignore[arg-type]
            "linear",
            "linear",
            ["BTCUSDT"],
            {},
            1710000000,
        )
    finally:
        conn.close()

    assert len(funding_rows) == 1
    assert funding_rows[0]["funding_interval_min"] == 480
    assert client.instrument_calls == []


def test_collector_rejects_instrument_interval_when_product_scope_not_usdt_perpetual(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "collector_bad_instrument_interval.db"))
    db.init_db(conn)
    client = _FundingIntervalClient(ticker_interval_hour=None, instrument_interval_min="240")

    def bad_info(category: str, symbol: str) -> dict[str, Any]:
        client.instrument_calls.append((category, symbol))
        return {
            "category": "linear",
            "symbol": "BTCUSDT",
            "status": "Trading",
            "contractType": "LinearPerpetual",
            "quoteCoin": "USDC",
            "settleCoin": "USDC",
            "deliveryTime": "0",
            "fundingInterval": "240",
        }

    client.get_instrument_info = bad_info  # type: ignore[method-assign]
    try:
        _ticker_rows, funding_rows, _missing_symbols = _fetch_ticker_payloads(
            conn,
            client,  # type: ignore[arg-type]
            "linear",
            "linear",
            ["BTCUSDT"],
            {},
            1710000000,
        )
    finally:
        conn.close()

    assert len(funding_rows) == 1
    assert funding_rows[0]["funding_interval_min"] is None
    assert client.instrument_calls == [("linear", "BTCUSDT")]
