from __future__ import annotations

import math
from typing import Any

from .bybit_client import BybitPublicClient
from . import db

# Symbols that Bybit rejects (e.g., futures-only symbols passed into spot collector).
# Keep a retry TTL instead of poisoning the symbol forever: listings can appear later,
# config may be corrected at runtime, and transient exchange-side validation glitches should self-heal.
_DISABLED_SYMBOLS: dict[str, dict[str, int]] = {"spot": {}, "linear": {}}
DISABLED_SYMBOL_RETRY_TTL_SEC = 6 * 60 * 60

VENUE_TO_CATEGORY = {
    "spot": "spot",
    "linear": "linear",
}

def _to_float(x: Any, *, minimum: float | None = None) -> float | None:
    try:
        if x is None:
            return None
        num = float(x)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    if minimum is not None and num < float(minimum):
        return None
    return num


def _sanitize_ticker_payload(t: dict[str, Any]) -> dict[str, Any]:
    last = _to_float(t.get("lastPrice"), minimum=0.0)
    bid = _to_float(t.get("bid1Price"), minimum=0.0)
    ask = _to_float(t.get("ask1Price"), minimum=0.0)
    if bid is not None and ask is not None and ask < bid:
        bid = None
        ask = None
    return {
        "last": last,
        "bid": bid,
        "ask": ask,
        "vol24h": _to_float(t.get("volume24h"), minimum=0.0),
        "turnover24h": _to_float(t.get("turnover24h"), minimum=0.0),
    }


def _purge_expired_disabled_symbols(venue: str, now_ts: int) -> dict[str, int]:
    disabled = _DISABLED_SYMBOLS.setdefault(venue, {})
    expired = [sym for sym, until_ts in disabled.items() if int(until_ts or 0) <= int(now_ts)]
    for sym in expired:
        disabled.pop(sym, None)
    return disabled


def _disable_symbol(venue: str, symbol: str, now_ts: int) -> int:
    disabled = _DISABLED_SYMBOLS.setdefault(venue, {})
    retry_at = int(now_ts) + DISABLED_SYMBOL_RETRY_TTL_SEC
    disabled[str(symbol or '').upper()] = retry_at
    return retry_at

def _is_not_supported_symbol(err: Exception) -> bool:
    msg = str(err)
    if "10001" not in msg:
        return False
    msg_l = msg.lower()
    # Bybit returns different messages for invalid/pre-market/delisted symbols:
    # "Not supported symbols", "symbol invalid", "params error: symbol invalid"
    return any(k in msg_l for k in ("not supported symbols", "symbol invalid"))

def collect_once(conn, client: BybitPublicClient, venue: str, symbols: list[str]) -> None:
    category = VENUE_TO_CATEGORY[venue]
    ts = db.now_ts()

    disabled = _purge_expired_disabled_symbols(venue, ts)
    symbols2 = [str(s).upper() for s in symbols if int(disabled.get(str(s).upper(), 0) or 0) <= ts]

    ticker_rows: list[dict[str, Any]] = []
    for sym in symbols2:
        try:
            lst = client.get_tickers(category=category, symbol=sym)
            if not lst:
                continue
            t = lst[0]
            snap = _sanitize_ticker_payload(t)
            ticker_rows.append({
                "venue": venue,
                "symbol": sym,
                "ts": ts,
                "last": snap["last"],
                "bid": snap["bid"],
                "ask": snap["ask"],
                "vol24h": snap["vol24h"],
                "turnover24h": snap["turnover24h"],
            })
        except Exception as e:
            if _is_not_supported_symbol(e):
                retry_at = _disable_symbol(venue, sym, ts)
                db.log_decision(conn, "SYMBOL_DISABLED", None, None, {"venue": venue, "symbol": sym, "reason": str(e), "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC, "retry_at": retry_at})
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
        if int(disabled.get(sym, 0) or 0) > ts:
            continue
        for interval, tf_sec in intervals.items():
            try:
                limit = 220 if tf_sec <= 3600 else 320
                kl = client.get_kline(category=category, symbol=sym, interval=interval, limit=limit)
                for row in kl:
                    try:
                        start_ms = int(row[0])
                    except Exception:
                        continue
                    open_px = _to_float(row[1], minimum=0.0)
                    high_px = _to_float(row[2], minimum=0.0)
                    low_px = _to_float(row[3], minimum=0.0)
                    close_px = _to_float(row[4], minimum=0.0)
                    volume = _to_float(row[5], minimum=0.0)
                    if None in (open_px, high_px, low_px, close_px, volume):
                        continue
                    if high_px < max(open_px, close_px, low_px):
                        continue
                    if low_px > min(open_px, close_px, high_px):
                        continue
                    ohlcv_rows.append({
                        "venue": venue,
                        "symbol": sym,
                        "tf_sec": tf_sec,
                        "ts": start_ms // 1000,
                        "open": open_px,
                        "high": high_px,
                        "low": low_px,
                        "close": close_px,
                        "volume": volume,
                    })
            except Exception as e:
                if _is_not_supported_symbol(e):
                    retry_at = _disable_symbol(venue, sym, ts)
                    db.log_decision(conn, "SYMBOL_DISABLED", None, None, {"venue": venue, "symbol": sym, "reason": str(e), "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC, "retry_at": retry_at})
                    break
                # Transient error (rate limit, timeout) — continue with next interval
                # only break for symbol-level errors (_is_not_supported_symbol handles those above)
                db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": sym, "err": str(e)})
                continue  # try next interval; don't abandon all intervals for this symbol

    if ohlcv_rows:
        db.upsert_ohlcv(conn, ohlcv_rows)


def collect_futures_once(conn, client, symbols_linear: list[str]) -> None:
    """Collect funding rate + open interest for all linear symbols.
    Called once per collect cycle, only for futures venue.
    Errors are logged per-symbol and never abort the cycle.
    """
    ts_now = db.now_ts()
    funding_rows: list[dict] = []

    disabled = _purge_expired_disabled_symbols("linear", ts_now)

    for sym in [str(s).upper() for s in symbols_linear]:
        if int(disabled.get(sym, 0) or 0) > ts_now:
            continue
        # Funding rate — reuse linear tickers (already fetched in collect_once,
        # but fundingRate is in the ticker payload, so we grab it separately here
        # to keep concerns separated and allow different call frequencies)
        try:
            fr = client.get_funding_rate(sym)
            if fr:
                funding_rate = _to_float(fr.get("funding_rate"))
                next_funding_ts = None
                try:
                    raw_next_funding_ts = fr.get("next_funding_ts")
                    if raw_next_funding_ts not in (None, ""):
                        next_funding_ts = int(raw_next_funding_ts)
                except Exception:
                    next_funding_ts = None
                if funding_rate is not None:
                    funding_rows.append({
                        "symbol": sym,
                        "ts": ts_now,
                        "funding_rate": funding_rate,
                        "next_funding_ts": next_funding_ts,
                    })
        except Exception as e:
            if _is_not_supported_symbol(e):
                retry_at = _disable_symbol("linear", sym, ts_now)
                db.log_decision(conn, "SYMBOL_DISABLED", None, None, {"venue": "linear", "symbol": sym, "reason": str(e), "field": "funding_rate", "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC, "retry_at": retry_at})
                continue
            db.log_decision(conn, "COLLECT_ERROR", None, None,
                            {"venue": "linear", "symbol": sym, "field": "funding_rate", "err": str(e)})

        # Open interest — 48 × 1h candles
        try:
            oi_rows_raw = client.get_open_interest(sym, interval="1h", limit=48)
            oi_rows = []
            for row in oi_rows_raw or []:
                try:
                    ts = int(row.get("ts") or 0)
                except Exception:
                    continue
                oi = _to_float(row.get("oi"), minimum=0.0)
                if ts <= 0 or oi is None:
                    continue
                oi_rows.append({"ts": ts, "oi": oi})
            if oi_rows:
                db.upsert_open_interest(conn, sym, oi_rows)
        except Exception as e:
            if _is_not_supported_symbol(e):
                retry_at = _disable_symbol("linear", sym, ts_now)
                db.log_decision(conn, "SYMBOL_DISABLED", None, None, {"venue": "linear", "symbol": sym, "reason": str(e), "field": "open_interest", "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC, "retry_at": retry_at})
                continue
            db.log_decision(conn, "COLLECT_ERROR", None, None,
                            {"venue": "linear", "symbol": sym, "field": "open_interest", "err": str(e)})

    if funding_rows:
        db.upsert_funding_rate(conn, funding_rows)
