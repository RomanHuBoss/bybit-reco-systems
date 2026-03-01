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
    if "10001" not in msg:
        return False
    # Bybit returns different messages for invalid/pre-market/delisted symbols:
    # "Not supported symbols", "symbol invalid", "params error: symbol invalid"
    return any(k in msg for k in ("Not supported symbols", "symbol invalid", "Symbol invalid"))

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
                db.log_decision(conn, "SYMBOL_DISABLED", None, None, {"venue": venue, "symbol": sym, "reason": str(e)})
                continue
            # Log with symbol name so operator can identify the culprit
            db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": sym, "err": str(e)})
            continue  # don't crash the whole cycle for one symbol

    if ticker_rows:
        db.insert_tickers(conn, ticker_rows)

    # Bybit v5 API valid kline intervals: 1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, W, M
    # "1440" is NOT valid — use "D" for daily candles
    intervals: dict[str, int] = {
        "1": 60,
        "15": 15 * 60,
        "30": 30 * 60,
        "60": 60 * 60,
        "240": 240 * 60,
        "D": 24 * 60 * 60,
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
                    db.log_decision(conn, "SYMBOL_DISABLED", None, None, {"venue": venue, "symbol": sym, "reason": str(e)})
                    break
                # Log with symbol name and continue — don't crash the whole cycle
                db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": sym, "err": str(e)})
                break  # skip remaining intervals for this symbol, try next

    if ohlcv_rows:
        db.upsert_ohlcv(conn, ohlcv_rows)


def collect_futures_once(conn, client, symbols_linear: list[str]) -> None:
    """Collect funding rate + open interest for all linear symbols.
    Called once per collect cycle, only for futures venue.
    Errors are logged per-symbol and never abort the cycle.
    """
    import time
    ts_now = db.now_ts()
    funding_rows: list[dict] = []

    for sym in symbols_linear:
        # Funding rate — reuse linear tickers (already fetched in collect_once,
        # but fundingRate is in the ticker payload, so we grab it separately here
        # to keep concerns separated and allow different call frequencies)
        try:
            fr = client.get_funding_rate(sym)
            if fr:
                fr["ts"] = ts_now
                funding_rows.append(fr)
        except Exception as e:
            db.log_decision(conn, "COLLECT_ERROR", None, None,
                            {"venue": "linear", "symbol": sym, "field": "funding_rate", "err": str(e)})

        # Open interest — 48 × 1h candles
        try:
            oi_rows = client.get_open_interest(sym, interval="1h", limit=48)
            if oi_rows:
                db.upsert_open_interest(conn, sym, oi_rows)
        except Exception as e:
            db.log_decision(conn, "COLLECT_ERROR", None, None,
                            {"venue": "linear", "symbol": sym, "field": "open_interest", "err": str(e)})

    if funding_rows:
        db.upsert_funding_rate(conn, funding_rows)
