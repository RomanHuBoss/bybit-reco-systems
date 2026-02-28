from __future__ import annotations

import time
from typing import Any

from .bybit_client import BybitPublicClient
from . import db

VENUE_TO_CATEGORY = {
    "spot": "spot",
    "linear": "linear",
}

def _to_float(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def collect_once(conn, client: BybitPublicClient, venue: str, symbols: list[str]) -> None:
    category = VENUE_TO_CATEGORY[venue]
    ts = db.now_ts()

    # Tickers per symbol (fast, small)
    ticker_rows: list[dict[str, Any]] = []
    for sym in symbols:
        lst = client.get_tickers(category=category, symbol=sym)
        if not lst:
            continue
        t = lst[0]
        ticker_rows.append({
            "venue": venue,
            "symbol": sym,
            "ts": ts,
            "last": _to_float(t.get("lastPrice")),
            "bid": _to_float(t.get("bid1Price")),
            "ask": _to_float(t.get("ask1Price")),
            "vol24h": _to_float(t.get("volume24h")),
            "turnover24h": _to_float(t.get("turnover24h")),
        })
    if ticker_rows:
        db.insert_tickers(conn, ticker_rows)

    # Klines (1m)
    ohlcv_rows: list[dict[str, Any]] = []
    for sym in symbols:
        kl = client.get_kline(category=category, symbol=sym, interval="1", limit=120)
        for row in kl:
            # row: [startTime, open, high, low, close, volume, turnover]
            start_ms = int(row[0])
            ohlcv_rows.append({
                "venue": venue,
                "symbol": sym,
                "tf_sec": 60,
                "ts": start_ms // 1000,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
    if ohlcv_rows:
        db.upsert_ohlcv(conn, ohlcv_rows)
