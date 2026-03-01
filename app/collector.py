from __future__ import annotations

from typing import Any

from .bybit_client import BybitPublicClient
from . import db

# Symbols that Bybit rejects (e.g., futures-only symbols passed into spot collector)
_DISABLED_SYMBOLS: dict[str, set[str]] = {"spot": set(), "linear": set()}

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

def _is_not_supported_symbol(err: Exception) -> bool:
    msg = str(err)
    return ("10001" in msg) and ("Not supported symbols" in msg)

def collect_once(conn, client: BybitPublicClient, venue: str, symbols: list[str]) -> None:
    category = VENUE_TO_CATEGORY[venue]
    ts = db.now_ts()

    disabled = _DISABLED_SYMBOLS.setdefault(venue, set())
    symbols2 = [s for s in symbols if s not in disabled]

    ticker_rows: list[dict[str, Any]] = []
    for sym in symbols2:
        try:
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
        except Exception as e:
            if _is_not_supported_symbol(e):
                disabled.add(sym)
                db.log_decision(conn, "SYMBOL_DISABLED", None, None, {"venue": venue, "symbol": sym, "reason": "Not supported symbols"})
                continue
            raise

    if ticker_rows:
        db.insert_tickers(conn, ticker_rows)

    intervals: dict[str, int] = {
        "1": 60,
        "15": 15 * 60,
        "30": 30 * 60,
        "60": 60 * 60,
        "240": 240 * 60,
        "1440": 24 * 60 * 60,
    }

    ohlcv_rows: list[dict[str, Any]] = []
    for sym in symbols2:
        if sym in disabled:
            continue
        for interval, tf_sec in intervals.items():
            try:
                limit = 220 if tf_sec <= 3600 else 320
                kl = client.get_kline(category=category, symbol=sym, interval=interval, limit=limit)
                for row in kl:
                    start_ms = int(row[0])
                    ohlcv_rows.append({
                        "venue": venue,
                        "symbol": sym,
                        "tf_sec": tf_sec,
                        "ts": start_ms // 1000,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    })
            except Exception as e:
                if _is_not_supported_symbol(e):
                    disabled.add(sym)
                    db.log_decision(conn, "SYMBOL_DISABLED", None, None, {"venue": venue, "symbol": sym, "reason": "Not supported symbols"})
                    break
                raise

    if ohlcv_rows:
        db.upsert_ohlcv(conn, ohlcv_rows)
