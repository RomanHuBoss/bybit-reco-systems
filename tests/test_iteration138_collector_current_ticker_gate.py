from __future__ import annotations

from pathlib import Path

from app import collector, db


class MissingTickerButKlineClient:
    def __init__(self) -> None:
        self.kline_calls: list[str] = []

    def get_tickers(self, *, category: str, symbol: str | None = None):
        return []

    def get_kline(self, category: str, symbol: str, interval: str = "1", limit: int = 200, start=None, end=None):
        self.kline_calls.append(symbol)
        return [["1700000000000", "100", "101", "99", "100", "10"]]


def test_collect_once_skips_ohlcv_when_current_ticker_is_missing(tmp_path: Path, monkeypatch) -> None:
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()
    collector._MISSING_TICKER_LOG_TS.clear()

    conn = db.connect(str(tmp_path / "missing-ticker-gate.db"))
    db.init_db(conn)
    monkeypatch.setattr(collector, "_API_FETCH_TFS", (60,))
    monkeypatch.setattr(collector, "_DERIVED_TF_SOURCES", {})
    monkeypatch.setattr(db, "now_ts", lambda: 1_700_000_100)

    client = MissingTickerButKlineClient()
    stats = collector.collect_once(conn, client, "linear", ["MISSUSDT"], max_workers=1)

    assert stats["ticker_missing_symbols"] == 1
    assert stats["symbols_with_current_ticker"] == 0
    assert stats["symbols_skipped_without_ticker"] == 1
    assert client.kline_calls == []
    assert db.get_latest_ohlcv(conn, "linear", "MISSUSDT", 60, limit=1) == []
    conn.close()


def test_collect_once_filters_malformed_symbols_before_market_requests(tmp_path: Path, monkeypatch) -> None:
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()
    collector._MISSING_TICKER_LOG_TS.clear()

    conn = db.connect(str(tmp_path / "malformed-symbol-filter.db"))
    db.init_db(conn)
    monkeypatch.setattr(collector, "_API_FETCH_TFS", (60,))
    monkeypatch.setattr(collector, "_DERIVED_TF_SOURCES", {})
    monkeypatch.setattr(db, "now_ts", lambda: 1_700_000_100)

    client = MissingTickerButKlineClient()
    stats = collector.collect_once(conn, client, "linear", ["BTC/USDT", "ETH-USDT", "USDT"], max_workers=1)

    assert stats["symbols_total"] == 0
    assert stats["ticker_missing_symbols"] == 0
    assert client.kline_calls == []
    conn.close()
