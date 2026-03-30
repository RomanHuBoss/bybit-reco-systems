from __future__ import annotations

import math
from typing import Any, Callable

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

# Per-timeframe policy for REST collection.
# High-frequency bars are updated aggressively; slow bars are throttled and/or derived locally.
_API_TF_POLICY: dict[int, dict[str, Any]] = {
    60: {"interval": "1", "cold_limit": 360, "delta_limit": 8, "overlap_bars": 4, "refresh_sec": 0},
    900: {"interval": "15", "cold_limit": 120, "delta_limit": 4, "overlap_bars": 3, "refresh_sec": None},
    1800: {"interval": "30", "cold_limit": 120, "delta_limit": 4, "overlap_bars": 3, "refresh_sec": None},
    3600: {"interval": "60", "cold_limit": 420, "delta_limit": 6, "overlap_bars": 4, "refresh_sec": 5 * 60},
    14400: {"interval": "240", "cold_limit": 120, "delta_limit": 4, "overlap_bars": 3, "refresh_sec": None},
    86400: {"interval": "D", "cold_limit": 120, "delta_limit": 3, "overlap_bars": 2, "refresh_sec": 2 * 60 * 60},
}

# Only these TFs are fetched from REST after iteration #62.
# 15m / 30m are maintained from 1m. 4h is maintained from 1h.
_API_FETCH_TFS = (60, 3600, 86400)
_DERIVED_TF_SOURCES: dict[int, int] = {
    900: 60,
    1800: 60,
    14400: 3600,
}

# Derived TFs are maintained locally after the first bootstrap, but the initial 1m bootstrap is
# intentionally shallow (to keep collector cost bounded). Without a one-off cold backfill, 15m/30m
# would start with too little history for the recommender's >=80-candle requirement.
_DERIVED_TF_BOOTSTRAP_MIN_ROWS: dict[int, int] = {
    900: 96,
    1800: 96,
    14400: 96,
}
_DERIVED_TF_BOOTSTRAP_RETRY_SEC = 6 * 60 * 60

# In-memory throttle for slow REST paths. This is intentionally process-local: it protects the
# active leader from hammering Bybit, while warm DB state still survives process restarts.
_LAST_TF_FETCH_ATTEMPT_TS: dict[tuple[str, str, int], int] = {}


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
    disabled[str(symbol or "").upper()] = retry_at
    return retry_at


def _is_not_supported_symbol(err: Exception) -> bool:
    msg = str(err)
    if "10001" not in msg:
        return False
    msg_l = msg.lower()
    return any(
        k in msg_l
        for k in (
            "not supported symbols",
            "symbol invalid",
            "symbol not found",
            "symbol is invalid",
            "instrument does not exist",
            "pre-market",
            "delist",
            "settlement",
        )
    )


def _normalize_symbols(symbols: list[str], disabled: dict[str, int], now_ts: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if not sym or sym in seen:
            continue
        if int(disabled.get(sym, 0) or 0) > now_ts:
            continue
        out.append(sym)
        seen.add(sym)
    return out


def _sanitize_ohlcv_row(venue: str, symbol: str, tf_sec: int, row: list[Any]) -> dict[str, Any] | None:
    try:
        start_ms = int(row[0])
    except Exception:
        return None
    open_px = _to_float(row[1], minimum=0.0)
    high_px = _to_float(row[2], minimum=0.0)
    low_px = _to_float(row[3], minimum=0.0)
    close_px = _to_float(row[4], minimum=0.0)
    volume = _to_float(row[5], minimum=0.0)
    if None in (open_px, high_px, low_px, close_px, volume):
        return None
    if high_px < max(open_px, close_px, low_px):
        return None
    if low_px > min(open_px, close_px, high_px):
        return None
    return {
        "venue": venue,
        "symbol": symbol,
        "tf_sec": tf_sec,
        "ts": start_ms // 1000,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "volume": volume,
    }


def _remote_ticker_ts(ticker: dict[str, Any], fallback_ts: int) -> int:
    candidates = (
        ticker.get("time"),
        ticker.get("updateTime"),
        ticker.get("ts"),
        ticker.get("lastPriceTime"),
    )
    for raw in candidates:
        try:
            ts = int(raw)
        except Exception:
            continue
        if ts > 10**11:
            ts //= 1000
        if ts > 0:
            return ts
    return int(fallback_ts)


def _extract_funding_row(symbol: str, ticker: dict[str, Any], fallback_ts: int) -> dict[str, Any] | None:
    funding_rate = _to_float(ticker.get("fundingRate"))
    if funding_rate is None:
        return None
    next_funding_ts = None
    try:
        nft = int(ticker.get("nextFundingTime") or 0)
        if nft > 10**11:
            nft //= 1000
        next_funding_ts = nft if nft > 0 else None
    except Exception:
        next_funding_ts = None
    return {
        "symbol": symbol,
        "ts": _remote_ticker_ts(ticker, fallback_ts),
        "funding_rate": funding_rate,
        "next_funding_ts": next_funding_ts,
    }


def _client_get_tickers_batch(client: BybitPublicClient, category: str) -> list[dict[str, Any]] | None:
    try:
        return client.get_tickers(category=category)
    except TypeError:
        return None
    except Exception:
        raise


def _fetch_ticker_payloads(
    conn,
    client: BybitPublicClient,
    venue: str,
    category: str,
    symbols: list[str],
    disabled: dict[str, int],
    now_ts: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    symbols_set = set(symbols)
    ticker_rows: list[dict[str, Any]] = []
    funding_rows: list[dict[str, Any]] = []
    fetched_symbols: set[str] = set()

    batch_items = _client_get_tickers_batch(client, category)
    if batch_items is not None:
        batch_ts = db.now_ts()
        for item in batch_items:
            sym = str(item.get("symbol") or "").upper()
            if sym not in symbols_set:
                continue
            fetched_symbols.add(sym)
            snap = _sanitize_ticker_payload(item)
            ticker_rows.append(
                {
                    "venue": venue,
                    "symbol": sym,
                    "ts": _remote_ticker_ts(item, batch_ts),
                    "last": snap["last"],
                    "bid": snap["bid"],
                    "ask": snap["ask"],
                    "vol24h": snap["vol24h"],
                    "turnover24h": snap["turnover24h"],
                }
            )
            if venue == "linear":
                funding_row = _extract_funding_row(sym, item, batch_ts)
                if funding_row is not None:
                    funding_rows.append(funding_row)

    missing_symbols: list[str] = []
    for sym in symbols:
        if sym in fetched_symbols:
            continue
        try:
            lst = client.get_tickers(category=category, symbol=sym)
            if not lst:
                missing_symbols.append(sym)
                continue
            item = lst[0]
            snap = _sanitize_ticker_payload(item)
            row_ts = _remote_ticker_ts(item, db.now_ts())
            ticker_rows.append(
                {
                    "venue": venue,
                    "symbol": sym,
                    "ts": row_ts,
                    "last": snap["last"],
                    "bid": snap["bid"],
                    "ask": snap["ask"],
                    "vol24h": snap["vol24h"],
                    "turnover24h": snap["turnover24h"],
                }
            )
            if venue == "linear":
                funding_row = _extract_funding_row(sym, item, row_ts)
                if funding_row is not None:
                    funding_rows.append(funding_row)
        except Exception as e:
            if _is_not_supported_symbol(e):
                retry_at = _disable_symbol(venue, sym, now_ts)
                disabled[sym] = retry_at
                db.log_decision(
                    conn,
                    "SYMBOL_DISABLED",
                    None,
                    None,
                    {
                        "venue": venue,
                        "symbol": sym,
                        "reason": str(e),
                        "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                        "retry_at": retry_at,
                    },
                )
                continue
            db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": sym, "field": "ticker", "err": str(e)})
            continue
    return ticker_rows, funding_rows, missing_symbols


def _should_fetch_api_tf(conn, venue: str, symbol: str, tf_sec: int, now_ts: int) -> bool:
    policy = _API_TF_POLICY[tf_sec]
    last_local_ts = db.get_latest_ohlcv_ts(conn, venue, symbol, tf_sec)
    if last_local_ts is None:
        return True
    refresh_sec = policy.get("refresh_sec")
    if refresh_sec in (None, 0):
        return True
    key = (venue, symbol, tf_sec)
    last_attempt_ts = int(_LAST_TF_FETCH_ATTEMPT_TS.get(key, 0) or 0)
    if now_ts - last_local_ts >= tf_sec:
        return True
    if last_attempt_ts <= 0:
        return now_ts - last_local_ts >= int(refresh_sec)
    return now_ts - last_attempt_ts >= int(refresh_sec)



def _fetch_api_kline_rows(client: BybitPublicClient, category: str, symbol: str, tf_sec: int, last_local_ts: int | None) -> list[list[Any]]:
    policy = _API_TF_POLICY[tf_sec]
    interval = str(policy["interval"])
    if last_local_ts is None:
        limit = int(policy["cold_limit"])
        try:
            return client.get_kline(category=category, symbol=symbol, interval=interval, limit=limit)
        except TypeError:
            return client.get_kline(category=category, symbol=symbol, interval=interval, limit=limit)

    overlap_bars = max(1, int(policy.get("overlap_bars", 2) or 2))
    delta_limit = max(2, int(policy.get("delta_limit", overlap_bars + 2) or (overlap_bars + 2)))
    start_ms = max(0, int(last_local_ts - overlap_bars * tf_sec) * 1000)
    try:
        return client.get_kline(category=category, symbol=symbol, interval=interval, limit=delta_limit, start=start_ms)
    except TypeError:
        return client.get_kline(category=category, symbol=symbol, interval=interval, limit=delta_limit)


def _should_bootstrap_derived_tf(conn, venue: str, symbol: str, target_tf_sec: int, now_ts: int) -> bool:
    minimum_rows = int(_DERIVED_TF_BOOTSTRAP_MIN_ROWS.get(target_tf_sec, 0) or 0)
    if minimum_rows <= 0:
        return False
    existing_rows = db.get_latest_ohlcv(conn, venue, symbol, target_tf_sec, limit=minimum_rows)
    if len(existing_rows) >= minimum_rows:
        return False

    # Skip expensive REST bootstrap when the local source timeframe already has enough
    # history to synthesize the target frame immediately. This matters most for 4h:
    # a cold 1h fetch already provides >96 derived 4h candles, so hitting /240 again
    # only burns API budget and can create misleading bootstrap errors.
    source_tf_sec = _DERIVED_TF_SOURCES.get(target_tf_sec)
    if source_tf_sec:
        ratio = max(1, int(target_tf_sec // source_tf_sec))
        source_needed = minimum_rows * ratio
        source_rows = db.get_latest_ohlcv(conn, venue, symbol, source_tf_sec, limit=source_needed)
        if len(source_rows) >= source_needed:
            return False

    key = (venue, symbol, target_tf_sec)
    last_attempt_ts = int(_LAST_TF_FETCH_ATTEMPT_TS.get(key, 0) or 0)
    if last_attempt_ts > 0 and now_ts - last_attempt_ts < _DERIVED_TF_BOOTSTRAP_RETRY_SEC:
        return False
    return True


def _bootstrap_derived_tf_from_api(client: BybitPublicClient, venue: str, category: str, symbol: str, target_tf_sec: int) -> list[dict[str, Any]]:
    policy = _API_TF_POLICY[target_tf_sec]
    interval = str(policy["interval"])
    limit = int(policy["cold_limit"])
    try:
        rows_raw = client.get_kline(category=category, symbol=symbol, interval=interval, limit=limit)
    except TypeError:
        rows_raw = client.get_kline(category=category, symbol=symbol, interval=interval, limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows_raw:
        payload = _sanitize_ohlcv_row(venue, symbol, target_tf_sec, row)
        if payload is not None:
            out.append(payload)
    return out


def _open_interest_fetch_plan(last_oi_ts: int | None, now_ts: int, interval_sec: int = 3600) -> tuple[int, int | None, int | None]:
    if last_oi_ts is None:
        return 72, None, None
    # Backfill enough rows to cover downtime gaps instead of always asking for a tiny tail.
    gap_sec = max(0, int(now_ts) - int(last_oi_ts))
    overlap_bars = 2
    bars_needed = max(6, math.ceil(gap_sec / max(1, interval_sec)) + overlap_bars)
    limit = min(200, bars_needed)
    start_ms = max(0, int(last_oi_ts - overlap_bars * interval_sec) * 1000)
    end_ms = int(now_ts + interval_sec) * 1000
    return limit, start_ms, end_ms


def _resample_rows(source_rows: list[dict[str, Any]], target_tf_sec: int) -> list[dict[str, Any]]:
    if not source_rows:
        return []
    rows_ord = sorted(source_rows, key=lambda r: int(r["ts"]))
    out: list[dict[str, Any]] = []
    current_bucket: int | None = None
    current_row: dict[str, Any] | None = None
    for row in rows_ord:
        ts = int(row["ts"])
        bucket_ts = ts - (ts % int(target_tf_sec))
        if current_bucket != bucket_ts:
            if current_row is not None:
                out.append(current_row)
            current_bucket = bucket_ts
            current_row = {
                "venue": row["venue"],
                "symbol": row["symbol"],
                "tf_sec": int(target_tf_sec),
                "ts": int(bucket_ts),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            continue
        assert current_row is not None
        current_row["high"] = max(float(current_row["high"]), float(row["high"]))
        current_row["low"] = min(float(current_row["low"]), float(row["low"]))
        current_row["close"] = float(row["close"])
        current_row["volume"] = float(current_row["volume"]) + float(row["volume"])
    if current_row is not None:
        out.append(current_row)
    return out


def _derive_local_tf_rows(conn, venue: str, symbol: str, source_tf_sec: int, target_tf_sec: int) -> list[dict[str, Any]]:
    if source_tf_sec == 60:
        source_limit = 360
    elif source_tf_sec == 3600:
        source_limit = 420
    else:
        source_limit = 500
    source_rows = [dict(r) for r in reversed(db.get_latest_ohlcv(conn, venue, symbol, source_tf_sec, limit=source_limit))]
    if not source_rows:
        return []
    return _resample_rows(source_rows, target_tf_sec)


def _heartbeat(heartbeat: Callable[[], None] | None) -> None:
    if heartbeat is None:
        return
    try:
        heartbeat()
    except Exception:
        return


def collect_once(conn, client: BybitPublicClient, venue: str, symbols: list[str], heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    category = VENUE_TO_CATEGORY[venue]
    now_ts = db.now_ts()

    disabled = _purge_expired_disabled_symbols(venue, now_ts)
    symbols2 = _normalize_symbols(symbols, disabled, now_ts)
    stats: dict[str, Any] = {
        "venue": venue,
        "symbols_total": len(symbols2),
        "tickers_written": 0,
        "funding_written": 0,
        "ohlcv_written": 0,
        "api_tf_fetches": {},
        "derived_tf_bootstrap_fetches": {},
        "derived_tf_writes": {},
    }

    ticker_rows, funding_rows, _missing_symbols = _fetch_ticker_payloads(conn, client, venue, category, symbols2, disabled, now_ts)
    if ticker_rows:
        db.insert_tickers(conn, ticker_rows, commit=False)
        stats["tickers_written"] = len(ticker_rows)
    if funding_rows:
        db.upsert_funding_rate(conn, funding_rows, commit=False)
        stats["funding_written"] = len(funding_rows)
    _heartbeat(heartbeat)

    ohlcv_rows: list[dict[str, Any]] = []
    api_fetch_counts: dict[int, int] = {}
    derived_bootstrap_fetch_counts: dict[int, int] = {}
    derived_write_counts: dict[int, int] = {}

    for sym in symbols2:
        if int(disabled.get(sym, 0) or 0) > now_ts:
            continue
        # REST-backed TFs: 1m, 1h, 1d
        for tf_sec in _API_FETCH_TFS:
            if not _should_fetch_api_tf(conn, venue, sym, tf_sec, now_ts):
                continue
            key = (venue, sym, tf_sec)
            _LAST_TF_FETCH_ATTEMPT_TS[key] = now_ts
            try:
                rows_raw = _fetch_api_kline_rows(client, category, sym, tf_sec, db.get_latest_ohlcv_ts(conn, venue, sym, tf_sec))
                appended = 0
                for row in rows_raw:
                    payload = _sanitize_ohlcv_row(venue, sym, tf_sec, row)
                    if payload is None:
                        continue
                    ohlcv_rows.append(payload)
                    appended += 1
                if appended > 0:
                    api_fetch_counts[tf_sec] = api_fetch_counts.get(tf_sec, 0) + 1
            except Exception as e:
                if _is_not_supported_symbol(e):
                    retry_at = _disable_symbol(venue, sym, now_ts)
                    disabled[sym] = retry_at
                    db.log_decision(
                        conn,
                        "SYMBOL_DISABLED",
                        None,
                        None,
                        {
                            "venue": venue,
                            "symbol": sym,
                            "reason": str(e),
                            "field": f"kline_{tf_sec}",
                            "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                            "retry_at": retry_at,
                        },
                    )
                    break
                db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": sym, "field": f"kline_{tf_sec}", "err": str(e)})
                continue
        _heartbeat(heartbeat)

    if ohlcv_rows:
        db.upsert_ohlcv(conn, ohlcv_rows, commit=False)
        stats["ohlcv_written"] += len(ohlcv_rows)

    # One-off cold bootstrap for derived TFs so a fresh DB has enough history for
    # the recommender's multi-timeframe gates immediately after startup.
    for target_tf_sec in _DERIVED_TF_SOURCES:
        for sym in symbols2:
            if int(disabled.get(sym, 0) or 0) > now_ts:
                continue
            if not _should_bootstrap_derived_tf(conn, venue, sym, target_tf_sec, now_ts):
                continue
            key = (venue, sym, target_tf_sec)
            _LAST_TF_FETCH_ATTEMPT_TS[key] = now_ts
            try:
                bootstrap_rows = _bootstrap_derived_tf_from_api(client, venue, category, sym, target_tf_sec)
                if not bootstrap_rows:
                    continue
                db.upsert_ohlcv(conn, bootstrap_rows, commit=False)
                stats["ohlcv_written"] += len(bootstrap_rows)
                derived_bootstrap_fetch_counts[target_tf_sec] = derived_bootstrap_fetch_counts.get(target_tf_sec, 0) + 1
            except Exception as e:
                if _is_not_supported_symbol(e):
                    retry_at = _disable_symbol(venue, sym, now_ts)
                    disabled[sym] = retry_at
                    db.log_decision(
                        conn,
                        "SYMBOL_DISABLED",
                        None,
                        None,
                        {
                            "venue": venue,
                            "symbol": sym,
                            "reason": str(e),
                            "field": f"derived_bootstrap_{target_tf_sec}",
                            "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                            "retry_at": retry_at,
                        },
                    )
                    continue
                db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": sym, "field": f"derived_bootstrap_{target_tf_sec}", "err": str(e)})
        _heartbeat(heartbeat)

    # Maintain derived TFs locally after primary source TFs are written.
    for target_tf_sec, source_tf_sec in _DERIVED_TF_SOURCES.items():
        for sym in symbols2:
            if int(disabled.get(sym, 0) or 0) > now_ts:
                continue
            derived_rows = _derive_local_tf_rows(conn, venue, sym, source_tf_sec, target_tf_sec)
            if not derived_rows:
                continue
            db.upsert_ohlcv(conn, derived_rows, commit=False)
            stats["ohlcv_written"] += len(derived_rows)
            derived_write_counts[target_tf_sec] = derived_write_counts.get(target_tf_sec, 0) + len(derived_rows)
        _heartbeat(heartbeat)

    conn.commit()
    stats["api_tf_fetches"] = {str(tf): cnt for tf, cnt in sorted(api_fetch_counts.items())}
    stats["derived_tf_bootstrap_fetches"] = {str(tf): cnt for tf, cnt in sorted(derived_bootstrap_fetch_counts.items())}
    stats["derived_tf_writes"] = {str(tf): cnt for tf, cnt in sorted(derived_write_counts.items())}
    return stats



def collect_futures_once(conn, client, symbols_linear: list[str], heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    """Collect open interest for all linear symbols.
    Funding rate is refreshed from the linear ticker batch inside collect_once().
    Errors are logged per-symbol and never abort the cycle.
    """
    ts_now = db.now_ts()
    disabled = _purge_expired_disabled_symbols("linear", ts_now)

    oi_written = 0
    oi_symbols = 0
    for sym in _normalize_symbols(symbols_linear, disabled, ts_now):
        if int(disabled.get(sym, 0) or 0) > ts_now:
            continue
        try:
            last_oi_ts = db.get_latest_open_interest_ts(conn, sym)
            limit, start_ms, end_ms = _open_interest_fetch_plan(last_oi_ts, ts_now, interval_sec=3600)
            try:
                oi_rows_raw = client.get_open_interest(sym, interval="1h", limit=limit, start_ms=start_ms, end_ms=end_ms)
            except TypeError:
                oi_rows_raw = client.get_open_interest(sym, interval="1h", limit=limit)
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
                db.upsert_open_interest(conn, sym, oi_rows, commit=False)
                oi_written += len(oi_rows)
                oi_symbols += 1
        except Exception as e:
            if _is_not_supported_symbol(e):
                retry_at = _disable_symbol("linear", sym, ts_now)
                disabled[sym] = retry_at
                db.log_decision(
                    conn,
                    "SYMBOL_DISABLED",
                    None,
                    None,
                    {
                        "venue": "linear",
                        "symbol": sym,
                        "reason": str(e),
                        "field": "open_interest",
                        "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                        "retry_at": retry_at,
                    },
                )
                continue
            db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": "linear", "symbol": sym, "field": "open_interest", "err": str(e)})
        finally:
            _heartbeat(heartbeat)

    conn.commit()
    return {"venue": "linear", "open_interest_symbols": oi_symbols, "open_interest_written": oi_written}
