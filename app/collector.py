from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .bybit_client import BybitPublicClient
from . import db
from .grid_math import strict_integer

# Symbols that Bybit rejects (e.g., futures-only symbols passed into linear collector).
# Keep a retry TTL instead of poisoning the symbol forever: listings can appear later,
# config may be corrected at runtime, and transient exchange-side validation glitches should self-heal.
_DISABLED_SYMBOLS: dict[str, dict[str, int]] = {"linear": {}}
DISABLED_SYMBOL_RETRY_TTL_SEC = 6 * 60 * 60
MISSING_TICKER_LOG_TTL_SEC = 60 * 60
_MISSING_TICKER_LOG_TS: dict[tuple[str, str], int] = {}
_FUNDING_SETTLEMENT_LOOKBACK_SEC = 35 * 24 * 60 * 60
_FUNDING_SETTLEMENT_REFRESH_SEC = 60 * 60
_LAST_FUNDING_SETTLEMENT_FETCH_TS: dict[tuple[str, str], int] = {}

VENUE_TO_CATEGORY = {
    "linear": "linear",
}


class RuntimeLockLostError(RuntimeError):
    """Raised when the active collector loses the runtime leadership lock mid-cycle."""


def _run_tasks_bounded(tasks: list[Any], worker: Callable[[Any], Any], max_workers: int) -> list[tuple[Any, Any | None, Exception | None]]:
    if not tasks:
        return []
    workers = max(1, int(max_workers or 1))
    if workers <= 1 or len(tasks) <= 1:
        out: list[tuple[Any, Any | None, Exception | None]] = []
        for task in tasks:
            try:
                out.append((task, worker(task), None))
            except Exception as exc:
                out.append((task, None, exc))
        return out

    out: list[tuple[Any, Any | None, Exception | None]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as executor:
        future_map = {executor.submit(worker, task): task for task in tasks}
        for future in as_completed(future_map):
            task = future_map[future]
            try:
                out.append((task, future.result(), None))
            except Exception as exc:
                out.append((task, None, exc))
    return out

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
_BACKFILL_ROUND_ROBIN_CURSOR: dict[tuple[str, str, int], int] = {}


def _to_float(x: Any, *, minimum: float | None = None) -> float | None:
    if isinstance(x, bool):
        return None
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


def _round_robin_take(items: list[Any], budget: int | None, cursor_key: tuple[str, str, int]) -> list[Any]:
    if budget is None:
        return list(items)
    try:
        limit = max(0, int(budget))
    except Exception:
        limit = 0
    if limit <= 0 or len(items) <= limit:
        return list(items)
    start = int(_BACKFILL_ROUND_ROBIN_CURSOR.get(cursor_key, 0) or 0) % len(items)
    out = [items[(start + idx) % len(items)] for idx in range(limit)]
    _BACKFILL_ROUND_ROBIN_CURSOR[cursor_key] = (start + limit) % len(items)
    return out


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


def _ticker_delivery_time_is_perpetual(value: Any) -> bool:
    if value in (None, ""):
        return True
    try:
        return int(str(value).strip() or "0") == 0
    except Exception:
        return False


def _is_exact_linear_usdt_perpetual_ticker(item: dict[str, Any], symbol: str, *, allow_missing_symbol: bool = False) -> bool:
    item_symbol = str((item or {}).get("symbol") or "").strip().upper()
    target_symbol = str(symbol or "").strip().upper()
    if not target_symbol.endswith("USDT"):
        return False
    if item_symbol:
        if item_symbol != target_symbol or not item_symbol.endswith("USDT"):
            return False
    elif not allow_missing_symbol:
        return False
    if not _ticker_delivery_time_is_perpetual((item or {}).get("deliveryTime")):
        return False
    if str((item or {}).get("curPreListingPhase") or "").strip():
        return False
    return True


def _select_exact_ticker(items: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    for item in items or []:
        # Symbol-specific fallbacks still have to echo the exact symbol. A malformed
        # upstream/stub row without symbol is not proof that this is the requested
        # Linear USDT perpetual, and accepting it can write price/funding for the
        # wrong instrument.
        if isinstance(item, dict) and _is_exact_linear_usdt_perpetual_ticker(item, symbol, allow_missing_symbol=False):
            return item
    return None


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


def _should_log_missing_ticker(venue: str, symbol: str, now_ts: int) -> bool:
    key = (str(venue or "").lower(), str(symbol or "").upper())
    last_ts = int(_MISSING_TICKER_LOG_TS.get(key, 0) or 0)
    if last_ts > 0 and int(now_ts) - last_ts < MISSING_TICKER_LOG_TTL_SEC:
        return False
    _MISSING_TICKER_LOG_TS[key] = int(now_ts)
    return True


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


def _is_exact_linear_usdt_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    base = normalized[:-4] if normalized.endswith("USDT") else ""
    return bool(base and normalized.endswith("USDT") and normalized.isalnum())


def _normalize_symbols(symbols: list[str], disabled: dict[str, int], now_ts: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if not sym or sym in seen:
            continue
        # Defense in depth: settings.py already filters SYMBOLS_LINEAR, but tests,
        # scripts or future callers may invoke the collector directly. Do not let
        # malformed spot-style values like BTC/USDT reach Bybit requests or local
        # storage in a Linear USDT futures-only service.
        if not _is_exact_linear_usdt_symbol(sym):
            continue
        if int(disabled.get(sym, 0) or 0) > now_ts:
            continue
        out.append(sym)
        seen.add(sym)
    return out


def _sanitize_ohlcv_row(venue: str, symbol: str, tf_sec: int, row: list[Any]) -> dict[str, Any] | None:
    tf_value = strict_integer(tf_sec)
    try:
        start_ms = strict_integer(row[0])
    except Exception:
        return None
    if tf_value is None or tf_value <= 0 or start_ms is None or start_ms <= 0:
        return None
    # Bybit kline startTime is an exact millisecond timestamp for the start of
    # the requested interval. Floor division must not manufacture a valid
    # second or interval bucket from a shifted/malformed upstream timestamp.
    if start_ms % 1000 != 0:
        return None
    start_ts = start_ms // 1000
    if start_ts % tf_value != 0:
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
        "tf_sec": tf_value,
        "ts": start_ts,
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
        ts = strict_integer(raw)
        if ts is None:
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
        raw_next_funding = ticker.get("nextFundingTime")
        nft = strict_integer(raw_next_funding)
        if nft is None:
            raise ValueError("nextFundingTime must be an exact integer")
        if nft > 10**11:
            nft //= 1000
        next_funding_ts = nft if nft > 0 else None
    except Exception:
        next_funding_ts = None

    # Bybit linear tickers may expose fundingIntervalHour. Store minutes so the
    # recommender counts actual funding events instead of assuming 8h for every
    # USDT perpetual. If the ticker omits this field, collection falls back to
    # instruments-info below, where Bybit publishes fundingInterval in minutes.
    funding_interval_min = None
    interval_hours = strict_integer(ticker.get("fundingIntervalHour"))
    if interval_hours is not None and interval_hours > 0:
        funding_interval_min = int(interval_hours * 60)

    return {
        "symbol": symbol,
        "ts": _remote_ticker_ts(ticker, fallback_ts),
        "funding_rate": funding_rate,
        "next_funding_ts": next_funding_ts,
        "funding_interval_min": funding_interval_min,
    }


def _funding_interval_min_from_instrument_info(client: BybitPublicClient, category: str, symbol: str) -> int | None:
    """Read fundingInterval from instruments-info only after product-scope checks.

    The ticker endpoint can omit fundingIntervalHour. Using a blind default would
    under/over-count funding events and distort grid net edge, so the collector
    tries the per-symbol instrument spec and accepts the value only when the row
    still proves the same Bybit Linear USDT perpetual product boundary.
    """
    try:
        info = client.get_instrument_info(category, symbol)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None

    item_symbol = str(info.get("symbol") or "").strip().upper()
    target_symbol = str(symbol or "").strip().upper()
    contract_type = str(info.get("contractType") or "").strip()
    quote_coin = str(info.get("quoteCoin") or "").strip().upper()
    settle_coin = str(info.get("settleCoin") or "").strip().upper()
    status = str(info.get("status") or "").strip().lower()
    is_pre_listing = str(info.get("isPreListing") or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    if item_symbol != target_symbol or not target_symbol.endswith("USDT"):
        return None
    if contract_type != "LinearPerpetual" or quote_coin != "USDT" or settle_coin != "USDT":
        return None
    if status and status != "trading":
        return None
    if is_pre_listing or not _ticker_delivery_time_is_perpetual(info.get("deliveryTime")):
        return None

    interval_min = strict_integer(info.get("fundingInterval"))
    if interval_min is None or interval_min <= 0:
        return None
    return int(interval_min)


def _ensure_funding_interval_from_instrument_info(
    client: BybitPublicClient,
    category: str,
    symbol: str,
    funding_row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if funding_row is None:
        return None
    current_interval = strict_integer(funding_row.get("funding_interval_min"))
    if current_interval is not None and current_interval > 0:
        return funding_row
    interval_min = _funding_interval_min_from_instrument_info(client, category, symbol)
    if interval_min is not None and interval_min > 0:
        funding_row = dict(funding_row)
        funding_row["funding_interval_min"] = int(interval_min)
    return funding_row


def _fetch_funding_settlements_for_symbol(
    conn,
    client: BybitPublicClient,
    venue: str,
    symbol: str,
    now_ts: int,
) -> list[dict[str, Any]]:
    """Backfill immutable settled funding rates used by historical outcomes.

    Ticker ``fundingRate`` is only the current forecast for the next settlement.
    This path queries the official historical endpoint and paginates backwards so
    up to 35 days of hourly settlements are covered without exceeding Bybit's
    200-row page limit.
    """
    if venue != "linear" or not hasattr(client, "get_funding_rate_history"):
        return []
    key = (venue, symbol)
    last_attempt = int(_LAST_FUNDING_SETTLEMENT_FETCH_TS.get(key, 0) or 0)
    if last_attempt > 0 and int(now_ts) - last_attempt < _FUNDING_SETTLEMENT_REFRESH_SEC:
        return []
    _LAST_FUNDING_SETTLEMENT_FETCH_TS[key] = int(now_ts)

    latest = db.get_latest_funding_settlement_ts(conn, symbol)
    start_sec = max(1, int(now_ts) - _FUNDING_SETTLEMENT_LOOKBACK_SEC)
    if latest is not None:
        start_sec = max(start_sec, int(latest) + 1)
    end_ms = int(now_ts) * 1000
    start_ms = int(start_sec) * 1000
    out_by_ts: dict[int, dict[str, Any]] = {}
    pages = 0
    while end_ms >= start_ms and pages < 16:
        pages += 1
        rows = client.get_funding_rate_history(
            symbol, start_ms=start_ms, end_ms=end_ms, limit=200
        )
        if not rows:
            break
        min_ts: int | None = None
        for row in rows:
            ts = strict_integer(row.get("ts")) if isinstance(row, dict) else None
            if ts is None or ts <= 0:
                continue
            out_by_ts[int(ts)] = dict(row)
            min_ts = int(ts) if min_ts is None else min(min_ts, int(ts))
        if min_ts is None or len(rows) < 200:
            break
        next_end_ms = int(min_ts) * 1000 - 1
        if next_end_ms >= end_ms:
            break
        end_ms = next_end_ms
    return [out_by_ts[ts] for ts in sorted(out_by_ts)]


def _client_get_tickers_batch(client: BybitPublicClient, category: str) -> tuple[list[dict[str, Any]] | None, Exception | None]:
    try:
        return client.get_tickers(category=category), None
    except TypeError:
        return None, None
    except Exception as exc:
        return None, exc


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

    batch_items, batch_error = _client_get_tickers_batch(client, category)
    if batch_error is not None:
        db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": "__BATCH__", "field": "ticker_batch", "err": str(batch_error)}, commit=False)
    if batch_items is not None:
        batch_ts = db.now_ts()
        for item in batch_items:
            sym = str(item.get("symbol") or "").upper()
            if sym not in symbols_set or not _is_exact_linear_usdt_perpetual_ticker(item, sym):
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
                funding_row = _ensure_funding_interval_from_instrument_info(
                    client,
                    category,
                    sym,
                    _extract_funding_row(sym, item, batch_ts),
                )
                if funding_row is not None:
                    funding_rows.append(funding_row)

    missing_symbols: list[str] = []
    for sym in symbols:
        if sym in fetched_symbols:
            continue
        try:
            lst = client.get_tickers(category=category, symbol=sym)
            item = _select_exact_ticker(lst or [], sym)
            if item is None:
                # A successful exact-symbol ticker response with no row can mean the
                # configured instrument no longer exists. Confirm via public metadata
                # before disabling; metadata transport failures remain transient.
                metadata_getter = getattr(client, "get_instrument_info", None)
                if not callable(metadata_getter):
                    missing_symbols.append(sym)
                    continue
                metadata = metadata_getter(category=category, symbol=sym)
                if metadata is None:
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
                            "reason_code": "INSTRUMENT_METADATA_ABSENT",
                            "reason": "exact ticker and instrument metadata are absent",
                            "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                            "retry_at": retry_at,
                        },
                        commit=False,
                    )
                    continue
                missing_symbols.append(sym)
                continue
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
                funding_row = _ensure_funding_interval_from_instrument_info(
                    client,
                    category,
                    sym,
                    _extract_funding_row(sym, item, row_ts),
                )
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
                    {
                        "venue": venue,
                        "symbol": sym,
                        "reason": str(e),
                        "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                        "retry_at": retry_at,
                    },
                    commit=False,
                )
                continue
            db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": sym, "field": "ticker", "err": str(e)}, commit=False)
            continue
    return ticker_rows, funding_rows, missing_symbols


def _api_tf_fetch_state(
    conn,
    venue: str,
    symbol: str,
    tf_sec: int,
    now_ts: int,
    *,
    min_rows_required: int | None = None,
) -> tuple[bool, int | None]:
    """Return whether a REST TF should be fetched and which local anchor to use.

    The returned anchor is normally the latest local candle timestamp. When the
    local series is too short for warm-up requirements, the anchor is forced to
    ``None`` so the caller performs a cold fetch instead of a tiny delta refresh.
    This prevents a fresh-but-short 1h/1d series from getting stuck below the
    recommender's minimum history depth forever.
    """
    policy = _API_TF_POLICY[tf_sec]
    last_local_ts = db.get_latest_ohlcv_ts(conn, venue, symbol, tf_sec)
    rows_required = max(0, int(min_rows_required or 0))
    if rows_required > 0:
        rows = db.get_latest_ohlcv(conn, venue, symbol, tf_sec, limit=rows_required)
        if len(rows) < rows_required:
            return True, None
    if last_local_ts is None:
        return True, None
    refresh_sec = policy.get("refresh_sec")
    if refresh_sec in (None, 0):
        return True, last_local_ts
    key = (venue, symbol, tf_sec)
    last_attempt_ts = int(_LAST_TF_FETCH_ATTEMPT_TS.get(key, 0) or 0)
    if now_ts - last_local_ts >= tf_sec:
        return True, last_local_ts
    if last_attempt_ts <= 0:
        return (now_ts - last_local_ts >= int(refresh_sec), last_local_ts)
    return (now_ts - last_attempt_ts >= int(refresh_sec), last_local_ts)



def _should_fetch_api_tf(conn, venue: str, symbol: str, tf_sec: int, now_ts: int, *, min_rows_required: int | None = None) -> bool:
    should_fetch, _ = _api_tf_fetch_state(
        conn,
        venue,
        symbol,
        tf_sec,
        now_ts,
        min_rows_required=min_rows_required,
    )
    return should_fetch



def _kline_fetch_windows(last_local_ts: int | None, now_ts: int, tf_sec: int) -> list[tuple[int, int | None, int | None]]:
    policy = _API_TF_POLICY[tf_sec]
    if last_local_ts is None:
        return [(int(policy["cold_limit"]), None, None)]

    overlap_bars = max(1, int(policy.get("overlap_bars", 2) or 2))
    delta_limit = max(2, int(policy.get("delta_limit", overlap_bars + 2) or (overlap_bars + 2)))
    chunk_limit = max(delta_limit, int(policy.get("cold_limit", delta_limit) or delta_limit))
    chunk_limit = max(2, min(1000, int(chunk_limit)))

    start_sec = max(0, int(last_local_ts) - overlap_bars * int(tf_sec))
    end_sec = max(start_sec, int(now_ts) + int(tf_sec))
    bars_needed = max(2, math.ceil((end_sec - start_sec) / max(1, int(tf_sec))) + 1)

    windows: list[tuple[int, int | None, int | None]] = []
    offset_bars = 0
    while offset_bars < bars_needed:
        bars_this_call = min(chunk_limit, bars_needed - offset_bars)
        win_start_sec = start_sec + offset_bars * int(tf_sec)
        win_end_sec = win_start_sec + max(0, bars_this_call - 1) * int(tf_sec)
        windows.append((bars_this_call, win_start_sec * 1000, win_end_sec * 1000))
        offset_bars += bars_this_call
    return windows


def _fetch_api_kline_rows(
    client: BybitPublicClient,
    category: str,
    symbol: str,
    tf_sec: int,
    last_local_ts: int | None,
    now_ts: int,
) -> list[list[Any]]:
    policy = _API_TF_POLICY[tf_sec]
    interval = str(policy["interval"])
    rows_raw_all: list[list[Any]] = []
    for limit, start_ms, end_ms in _kline_fetch_windows(last_local_ts, now_ts, tf_sec):
        try:
            rows_raw = client.get_kline(category=category, symbol=symbol, interval=interval, limit=limit, start=start_ms, end=end_ms)
        except TypeError:
            if start_ms is None and end_ms is None:
                rows_raw = client.get_kline(category=category, symbol=symbol, interval=interval, limit=limit)
            elif end_ms is None:
                rows_raw = client.get_kline(category=category, symbol=symbol, interval=interval, limit=limit, start=start_ms)
            else:
                rows_raw = client.get_kline(category=category, symbol=symbol, interval=interval, limit=limit, start=start_ms)
        rows_raw_all.extend(rows_raw or [])
    return rows_raw_all


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


def _fetch_open_interest_rows(
    client: BybitPublicClient,
    symbol: str,
    *,
    interval: str = "1h",
    limit: int = 48,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    rows_all: list[dict[str, Any]] = []
    next_cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        if hasattr(client, "get_open_interest_page"):
            rows, next_cursor = client.get_open_interest_page(
                symbol,
                interval=interval,
                limit=limit,
                start_ms=start_ms,
                end_ms=end_ms,
                cursor=next_cursor,
            )
        else:
            try:
                rows = client.get_open_interest(symbol, interval=interval, limit=limit, start_ms=start_ms, end_ms=end_ms, cursor=next_cursor)
            except TypeError:
                try:
                    rows = client.get_open_interest(symbol, interval=interval, limit=limit, start_ms=start_ms, end_ms=end_ms)
                except TypeError:
                    rows = client.get_open_interest(symbol, interval=interval, limit=limit)
            next_cursor = None
        rows_all.extend(rows or [])
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
    return rows_all


def _resample_rows(source_rows: list[dict[str, Any]], target_tf_sec: int) -> list[dict[str, Any]]:
    """Aggregate only complete, contiguous source-candle buckets.

    A missing or duplicated source bar must not be converted into a seemingly valid
    higher-timeframe candle: that would corrupt rolling volatility/trend features and
    make historical data availability look better than it was at decision time.
    """
    if not source_rows or isinstance(target_tf_sec, bool):
        return []
    try:
        target_tf = int(target_tf_sec)
    except Exception:
        return []
    if target_tf <= 0 or float(target_tf_sec) != float(target_tf):
        return []

    source_tf: int | None = None
    venue: str | None = None
    symbol: str | None = None
    normalized: list[tuple[int, dict[str, Any]]] = []
    seen_timestamps: set[int] = set()

    for row in source_rows:
        try:
            raw_tf = row["tf_sec"]
            raw_ts = row["ts"]
            if isinstance(raw_tf, bool) or isinstance(raw_ts, bool):
                return []
            row_tf = int(raw_tf)
            row_ts = int(raw_ts)
            if float(raw_tf) != float(row_tf) or float(raw_ts) != float(row_ts):
                return []
            row_venue = str(row["venue"])
            row_symbol = str(row["symbol"])
            open_px = float(row["open"])
            high_px = float(row["high"])
            low_px = float(row["low"])
            close_px = float(row["close"])
            volume = float(row["volume"])
        except Exception:
            return []
        if row_tf <= 0 or row_ts <= 0 or row_ts % row_tf != 0:
            return []
        if not all(math.isfinite(value) for value in (open_px, high_px, low_px, close_px, volume)):
            return []
        if open_px <= 0 or high_px <= 0 or low_px <= 0 or close_px <= 0 or volume < 0:
            return []
        if high_px < max(open_px, close_px, low_px) or low_px > min(open_px, close_px, high_px):
            return []
        if source_tf is None:
            source_tf = row_tf
            venue = row_venue
            symbol = row_symbol
        elif row_tf != source_tf or row_venue != venue or row_symbol != symbol:
            return []
        if row_ts in seen_timestamps:
            return []
        seen_timestamps.add(row_ts)
        normalized.append((row_ts, row))

    if source_tf is None or target_tf < source_tf or target_tf % source_tf != 0:
        return []

    expected_count = target_tf // source_tf
    buckets: dict[int, dict[int, dict[str, Any]]] = {}
    for row_ts, row in normalized:
        bucket_ts = row_ts - (row_ts % target_tf)
        buckets.setdefault(bucket_ts, {})[row_ts] = row

    out: list[dict[str, Any]] = []
    for bucket_ts in sorted(buckets):
        rows_by_ts = buckets[bucket_ts]
        expected_timestamps = [bucket_ts + index * source_tf for index in range(expected_count)]
        if set(rows_by_ts) != set(expected_timestamps):
            continue
        rows_ord = [rows_by_ts[ts] for ts in expected_timestamps]
        out.append({
            "venue": venue,
            "symbol": symbol,
            "tf_sec": target_tf,
            "ts": bucket_ts,
            "open": float(rows_ord[0]["open"]),
            "high": max(float(row["high"]) for row in rows_ord),
            "low": min(float(row["low"]) for row in rows_ord),
            "close": float(rows_ord[-1]["close"]),
            "volume": sum(float(row["volume"]) for row in rows_ord),
        })
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


def _heartbeat(heartbeat: Callable[[], Any] | None) -> None:
    if heartbeat is None:
        return
    try:
        result = heartbeat()
    except RuntimeLockLostError:
        raise
    except Exception:
        return
    if result is False:
        raise RuntimeLockLostError("collector runtime lock lost")


def collect_once(conn, client: BybitPublicClient, venue: str, symbols: list[str], heartbeat: Callable[[], Any] | None = None, *, max_workers: int = 1, api_fetch_tfs: tuple[int, ...] | None = None, allow_derived_bootstrap: bool = True) -> dict[str, Any]:
    category = VENUE_TO_CATEGORY[venue]
    now_ts = db.now_ts()

    disabled = _purge_expired_disabled_symbols(venue, now_ts)
    symbols2 = _normalize_symbols(symbols, disabled, now_ts)
    stats: dict[str, Any] = {
        "venue": venue,
        "symbols_total": len(symbols2),
        "tickers_written": 0,
        "funding_written": 0,
        "funding_settlements_written": 0,
        "ohlcv_written": 0,
        "api_tf_fetches": {},
        "derived_tf_bootstrap_fetches": {},
        "derived_tf_writes": {},
    }

    ticker_rows, funding_rows, missing_symbols = _fetch_ticker_payloads(conn, client, venue, category, symbols2, disabled, now_ts)
    missing_symbol_set = {str(sym or "").strip().upper() for sym in missing_symbols}
    # A current exact ticker is the minimum proof that the configured symbol is a
    # live Bybit Linear USDT perpetual in this collect cycle. If ticker is missing
    # or malformed, do not refresh candles/derived TFs for that symbol in the same
    # cycle; otherwise the recommender can see fresh OHLCV beside stale or absent
    # price/funding and overstate readiness.
    active_symbols = [sym for sym in symbols2 if sym not in missing_symbol_set]
    stats["ticker_missing_symbols"] = len(missing_symbols)
    stats["sample_ticker_missing_symbols"] = list(missing_symbols[:8])
    stats["symbols_with_current_ticker"] = len(active_symbols)
    stats["symbols_skipped_without_ticker"] = len(symbols2) - len(active_symbols)
    if ticker_rows:
        db.insert_tickers(conn, ticker_rows, commit=False)
        stats["tickers_written"] = len(ticker_rows)
    if funding_rows:
        db.upsert_funding_rate(conn, funding_rows, commit=False)
        stats["funding_written"] = len(funding_rows)

    settlement_rows: list[dict[str, Any]] = []
    settlement_tasks = list(active_symbols)
    for _task, result, err in _run_tasks_bounded(
        settlement_tasks,
        lambda sym: _fetch_funding_settlements_for_symbol(conn, client, venue, sym, now_ts),
        1,
    ):
        if err is not None:
            db.log_decision(
                conn,
                "COLLECT_ERROR",
                None,
                None,
                {"venue": venue, "symbol": str(_task), "field": "funding_history", "err": str(err)},
                commit=False,
            )
            continue
        settlement_rows.extend(result or [])
    if settlement_rows:
        db.upsert_funding_settlements(conn, settlement_rows, commit=False)
        stats["funding_settlements_written"] = len(settlement_rows)
    for sym in missing_symbols:
        if _should_log_missing_ticker(venue, sym, now_ts):
            db.log_decision(
                conn,
                "COLLECT_ERROR",
                None,
                {"venue": venue, "symbol": sym, "field": "ticker_missing", "err": "ticker payload empty"},
                commit=False,
            )
    if ticker_rows or funding_rows or settlement_rows or missing_symbols:
        conn.commit()
    _heartbeat(heartbeat)

    active_api_fetch_tfs = tuple(api_fetch_tfs or _API_FETCH_TFS)
    api_fetch_counts: dict[int, int] = {}
    derived_bootstrap_fetch_counts: dict[int, int] = {}
    derived_write_counts: dict[int, int] = {}
    touched_by_source_tf: dict[int, set[str]] = {}

    def _api_task_worker(task: tuple[str, int, int | None]) -> tuple[str, int, list[list[Any]]]:
        sym, tf_sec, last_local_ts = task
        rows_raw = _fetch_api_kline_rows(client, category, sym, tf_sec, last_local_ts, now_ts)
        return sym, tf_sec, rows_raw

    # Keep 1m in the hot path. A fresh DB used to interleave 1m/1h/1d fetches across the
    # whole universe, which delayed the first usable 1m layer and made the recommender hit
    # stale-data skips during cold start. Fetch TF groups in priority order instead.
    for tf_sec in active_api_fetch_tfs:
        ohlcv_rows: list[dict[str, Any]] = []
        api_log_events: list[tuple[str, dict[str, Any]]] = []
        api_tasks: list[tuple[str, int, int | None]] = []
        for sym in active_symbols:
            if int(disabled.get(sym, 0) or 0) > now_ts:
                continue
            if not _should_fetch_api_tf(conn, venue, sym, tf_sec, now_ts):
                continue
            key = (venue, sym, tf_sec)
            _LAST_TF_FETCH_ATTEMPT_TS[key] = now_ts
            api_tasks.append((sym, tf_sec, db.get_latest_ohlcv_ts(conn, venue, sym, tf_sec)))

        for idx, (task, result, err) in enumerate(_run_tasks_bounded(api_tasks, _api_task_worker, max_workers), start=1):
            sym, _tf_task, _last_local_ts = task
            if err is not None:
                if _is_not_supported_symbol(err):
                    retry_at = _disable_symbol(venue, sym, now_ts)
                    disabled[sym] = retry_at
                    api_log_events.append((
                        "SYMBOL_DISABLED",
                        {
                            "venue": venue,
                            "symbol": sym,
                            "reason": str(err),
                            "field": f"kline_{tf_sec}",
                            "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                            "retry_at": retry_at,
                        },
                    ))
                else:
                    api_log_events.append(("COLLECT_ERROR", {"venue": venue, "symbol": sym, "field": f"kline_{tf_sec}", "err": str(err)}))
            else:
                _sym_out, _tf_out, rows_raw = result
                appended = 0
                for row in rows_raw:
                    payload = _sanitize_ohlcv_row(venue, sym, tf_sec, row)
                    if payload is None:
                        continue
                    ohlcv_rows.append(payload)
                    appended += 1
                if appended > 0:
                    api_fetch_counts[tf_sec] = api_fetch_counts.get(tf_sec, 0) + 1
                    touched_by_source_tf.setdefault(tf_sec, set()).add(sym)
            if idx % max(1, max_workers) == 0:
                _heartbeat(heartbeat)
        if ohlcv_rows:
            # PostgreSQL deadlock victims abort the whole transaction. Use the
            # retry-capable commit boundary here instead of leaving OHLCV inside
            # a larger caller-managed transaction.
            db.upsert_ohlcv(conn, ohlcv_rows, commit=True)
            stats["ohlcv_written"] += len(ohlcv_rows)
        for action, details in api_log_events:
            db.log_decision(conn, action, None, None, details, commit=False)
        if api_log_events:
            conn.commit()
        _heartbeat(heartbeat)

    # One-off cold bootstrap for derived TFs so a fresh DB has enough history for
    # the recommender's multi-timeframe gates immediately after startup.
    if allow_derived_bootstrap:
        bootstrap_log_events: list[tuple[str, dict[str, Any]]] = []
        bootstrap_tasks: list[tuple[str, int]] = []
        bootstrap_ohlcv_rows: list[dict[str, Any]] = []
        for target_tf_sec in _DERIVED_TF_SOURCES:
            for sym in active_symbols:
                if int(disabled.get(sym, 0) or 0) > now_ts:
                    continue
                if not _should_bootstrap_derived_tf(conn, venue, sym, target_tf_sec, now_ts):
                    continue
                key = (venue, sym, target_tf_sec)
                _LAST_TF_FETCH_ATTEMPT_TS[key] = now_ts
                bootstrap_tasks.append((sym, target_tf_sec))

        def _bootstrap_task_worker(task: tuple[str, int]) -> tuple[str, int, list[dict[str, Any]]]:
            sym, target_tf_sec = task
            rows = _bootstrap_derived_tf_from_api(client, venue, category, sym, target_tf_sec)
            return sym, target_tf_sec, rows

        for idx, (task, result, err) in enumerate(_run_tasks_bounded(bootstrap_tasks, _bootstrap_task_worker, max_workers), start=1):
            sym, target_tf_sec = task
            if err is not None:
                if _is_not_supported_symbol(err):
                    retry_at = _disable_symbol(venue, sym, now_ts)
                    disabled[sym] = retry_at
                    bootstrap_log_events.append((
                        "SYMBOL_DISABLED",
                        {
                            "venue": venue,
                            "symbol": sym,
                            "reason": str(err),
                            "field": f"derived_bootstrap_{target_tf_sec}",
                            "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                            "retry_at": retry_at,
                        },
                    ))
                else:
                    bootstrap_log_events.append(("COLLECT_ERROR", {"venue": venue, "symbol": sym, "field": f"derived_bootstrap_{target_tf_sec}", "err": str(err)}))
            else:
                _sym_out, _target_out, bootstrap_rows = result
                if bootstrap_rows:
                    bootstrap_ohlcv_rows.extend(bootstrap_rows)
                    stats["ohlcv_written"] += len(bootstrap_rows)
                    derived_bootstrap_fetch_counts[target_tf_sec] = derived_bootstrap_fetch_counts.get(target_tf_sec, 0) + 1
                    source_tf = _DERIVED_TF_SOURCES.get(target_tf_sec)
                    if source_tf is not None:
                        touched_by_source_tf.setdefault(source_tf, set()).add(sym)
            if idx % max(1, max_workers) == 0:
                _heartbeat(heartbeat)
        if bootstrap_ohlcv_rows:
            db.upsert_ohlcv(conn, bootstrap_ohlcv_rows, commit=True)
        for action, details in bootstrap_log_events:
            db.log_decision(conn, action, None, None, details, commit=False)
        if bootstrap_log_events:
            conn.commit()
        _heartbeat(heartbeat)

    # Maintain only derivatives whose source TF changed in this cycle. Besides
    # removing write amplification, this prevents the 1m hot worker from rewriting
    # 4h rows concurrently with the 1h backfill worker.
    for target_tf_sec, source_tf_sec in _DERIVED_TF_SOURCES.items():
        derived_batch: list[dict[str, Any]] = []
        target_symbols = sorted(touched_by_source_tf.get(source_tf_sec, set()))
        for sym in target_symbols:
            if int(disabled.get(sym, 0) or 0) > now_ts:
                continue
            derived_rows = _derive_local_tf_rows(conn, venue, sym, source_tf_sec, target_tf_sec)
            if not derived_rows:
                continue
            derived_batch.extend(derived_rows)
            stats["ohlcv_written"] += len(derived_rows)
            derived_write_counts[target_tf_sec] = derived_write_counts.get(target_tf_sec, 0) + len(derived_rows)
        if derived_batch:
            db.upsert_ohlcv(conn, derived_batch, commit=True)
        _heartbeat(heartbeat)

    conn.commit()
    stats["api_tf_fetches"] = {str(tf): cnt for tf, cnt in sorted(api_fetch_counts.items())}
    stats["derived_tf_bootstrap_fetches"] = {str(tf): cnt for tf, cnt in sorted(derived_bootstrap_fetch_counts.items())}
    stats["derived_tf_writes"] = {str(tf): cnt for tf, cnt in sorted(derived_write_counts.items())}
    return stats



def collect_backfill_once(
    conn,
    client: BybitPublicClient,
    venue: str,
    symbols: list[str],
    heartbeat: Callable[[], Any] | None = None,
    *,
    max_workers: int = 1,
    per_tf_budget: int | None = None,
    min_rows_per_tf: int = 80,
) -> dict[str, Any]:
    category = VENUE_TO_CATEGORY[venue]
    now_ts = db.now_ts()

    disabled = _purge_expired_disabled_symbols(venue, now_ts)
    symbols2 = _normalize_symbols(symbols, disabled, now_ts)
    budget = per_tf_budget if per_tf_budget is not None else max(1, int(max_workers or 1) * 2)
    stats: dict[str, Any] = {
        "venue": venue,
        "symbols_total": len(symbols2),
        "budget_per_tf": int(budget),
        "min_rows_per_tf": max(1, int(min_rows_per_tf or 1)),
        "ohlcv_written": 0,
        "api_tf_fetches": {},
        "derived_tf_bootstrap_fetches": {},
        "derived_tf_writes": {},
    }

    api_fetch_counts: dict[int, int] = {}
    derived_bootstrap_fetch_counts: dict[int, int] = {}
    derived_write_counts: dict[int, int] = {}
    touched_by_source_tf: dict[int, set[str]] = {}

    def _api_task_worker(task: tuple[str, int, int | None]) -> tuple[str, int, list[list[Any]]]:
        sym, tf_sec, last_local_ts = task
        rows_raw = _fetch_api_kline_rows(client, category, sym, tf_sec, last_local_ts, now_ts)
        return sym, tf_sec, rows_raw

    for tf_sec in tuple(tf for tf in _API_FETCH_TFS if tf != 60):
        api_tasks: list[tuple[str, int, int | None]] = []
        for sym in symbols2:
            if int(disabled.get(sym, 0) or 0) > now_ts:
                continue
            should_fetch, fetch_anchor_ts = _api_tf_fetch_state(
                conn,
                venue,
                sym,
                tf_sec,
                now_ts,
                min_rows_required=min_rows_per_tf,
            )
            if not should_fetch:
                continue
            api_tasks.append((sym, tf_sec, fetch_anchor_ts))
        api_tasks = _round_robin_take(api_tasks, budget, ("api", venue, tf_sec))
        if not api_tasks:
            continue
        ohlcv_rows: list[dict[str, Any]] = []
        api_log_events: list[tuple[str, dict[str, Any]]] = []
        for sym, _, _ in api_tasks:
            _LAST_TF_FETCH_ATTEMPT_TS[(venue, sym, tf_sec)] = now_ts
        for idx, (task, result, err) in enumerate(_run_tasks_bounded(api_tasks, _api_task_worker, max_workers), start=1):
            sym, _tf_task, _last_local_ts = task
            if err is not None:
                if _is_not_supported_symbol(err):
                    retry_at = _disable_symbol(venue, sym, now_ts)
                    disabled[sym] = retry_at
                    api_log_events.append((
                        "SYMBOL_DISABLED",
                        {
                            "venue": venue,
                            "symbol": sym,
                            "reason": str(err),
                            "field": f"kline_{tf_sec}",
                            "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                            "retry_at": retry_at,
                        },
                    ))
                else:
                    api_log_events.append(("COLLECT_ERROR", {"venue": venue, "symbol": sym, "field": f"kline_{tf_sec}", "err": str(err)}))
            else:
                _sym_out, _tf_out, rows_raw = result
                appended = 0
                for row in rows_raw:
                    payload = _sanitize_ohlcv_row(venue, sym, tf_sec, row)
                    if payload is None:
                        continue
                    ohlcv_rows.append(payload)
                    appended += 1
                if appended > 0:
                    api_fetch_counts[tf_sec] = api_fetch_counts.get(tf_sec, 0) + 1
                    touched_by_source_tf.setdefault(tf_sec, set()).add(sym)
            if idx % max(1, max_workers) == 0:
                _heartbeat(heartbeat)
        if ohlcv_rows:
            db.upsert_ohlcv(conn, ohlcv_rows, commit=True)
            stats["ohlcv_written"] += len(ohlcv_rows)
        for action, details in api_log_events:
            db.log_decision(conn, action, None, None, details, commit=False)
        if api_log_events:
            conn.commit()
        _heartbeat(heartbeat)

    bootstrap_log_events: list[tuple[str, dict[str, Any]]] = []
    bootstrap_tasks: list[tuple[str, int]] = []
    bootstrap_ohlcv_rows: list[dict[str, Any]] = []
    for target_tf_sec in _DERIVED_TF_SOURCES:
        candidates: list[tuple[str, int]] = []
        for sym in symbols2:
            if int(disabled.get(sym, 0) or 0) > now_ts:
                continue
            if not _should_bootstrap_derived_tf(conn, venue, sym, target_tf_sec, now_ts):
                continue
            candidates.append((sym, target_tf_sec))
        selected = _round_robin_take(candidates, budget, ("bootstrap", venue, target_tf_sec))
        for sym, _target_tf in selected:
            _LAST_TF_FETCH_ATTEMPT_TS[(venue, sym, target_tf_sec)] = now_ts
        bootstrap_tasks.extend(selected)

    def _bootstrap_task_worker(task: tuple[str, int]) -> tuple[str, int, list[dict[str, Any]]]:
        sym, target_tf_sec = task
        rows = _bootstrap_derived_tf_from_api(client, venue, category, sym, target_tf_sec)
        return sym, target_tf_sec, rows

    for idx, (task, result, err) in enumerate(_run_tasks_bounded(bootstrap_tasks, _bootstrap_task_worker, max_workers), start=1):
        sym, target_tf_sec = task
        if err is not None:
            if _is_not_supported_symbol(err):
                retry_at = _disable_symbol(venue, sym, now_ts)
                disabled[sym] = retry_at
                bootstrap_log_events.append((
                    "SYMBOL_DISABLED",
                    {
                        "venue": venue,
                        "symbol": sym,
                        "reason": str(err),
                        "field": f"derived_bootstrap_{target_tf_sec}",
                        "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                        "retry_at": retry_at,
                    },
                ))
            else:
                bootstrap_log_events.append(("COLLECT_ERROR", {"venue": venue, "symbol": sym, "field": f"derived_bootstrap_{target_tf_sec}", "err": str(err)}))
        else:
            _sym_out, _target_out, bootstrap_rows = result
            if bootstrap_rows:
                bootstrap_ohlcv_rows.extend(bootstrap_rows)
                stats["ohlcv_written"] += len(bootstrap_rows)
                derived_bootstrap_fetch_counts[target_tf_sec] = derived_bootstrap_fetch_counts.get(target_tf_sec, 0) + 1
                source_tf = _DERIVED_TF_SOURCES.get(target_tf_sec)
                if source_tf is not None:
                    touched_by_source_tf.setdefault(source_tf, set()).add(sym)
        if idx % max(1, max_workers) == 0:
            _heartbeat(heartbeat)
    if bootstrap_ohlcv_rows:
        db.upsert_ohlcv(conn, bootstrap_ohlcv_rows, commit=True)
    for action, details in bootstrap_log_events:
        db.log_decision(conn, action, None, None, details, commit=False)
    if bootstrap_log_events:
        conn.commit()
    _heartbeat(heartbeat)

    for target_tf_sec, source_tf_sec in _DERIVED_TF_SOURCES.items():
        target_symbols = sorted(touched_by_source_tf.get(source_tf_sec, set()))
        if not target_symbols:
            continue
        derived_batch: list[dict[str, Any]] = []
        for sym in target_symbols:
            if int(disabled.get(sym, 0) or 0) > now_ts:
                continue
            derived_rows = _derive_local_tf_rows(conn, venue, sym, source_tf_sec, target_tf_sec)
            if not derived_rows:
                continue
            derived_batch.extend(derived_rows)
            stats["ohlcv_written"] += len(derived_rows)
            derived_write_counts[target_tf_sec] = derived_write_counts.get(target_tf_sec, 0) + len(derived_rows)
        if derived_batch:
            db.upsert_ohlcv(conn, derived_batch, commit=True)
        _heartbeat(heartbeat)

    conn.commit()
    stats["api_tf_fetches"] = {str(tf): cnt for tf, cnt in sorted(api_fetch_counts.items())}
    stats["derived_tf_bootstrap_fetches"] = {str(tf): cnt for tf, cnt in sorted(derived_bootstrap_fetch_counts.items())}
    stats["derived_tf_writes"] = {str(tf): cnt for tf, cnt in sorted(derived_write_counts.items())}
    return stats



def collect_futures_once(conn, client, symbols_linear: list[str], heartbeat: Callable[[], Any] | None = None, *, max_workers: int = 1) -> dict[str, Any]:
    """Collect open interest for all linear symbols.
    Funding rate is refreshed from the linear ticker batch inside collect_once().
    Errors are logged per-symbol and never abort the cycle.
    """
    ts_now = db.now_ts()
    disabled = _purge_expired_disabled_symbols("linear", ts_now)

    oi_written = 0
    oi_symbols = 0
    oi_pending_rows: list[tuple[str, list[dict[str, Any]]]] = []
    oi_log_events: list[tuple[str, dict[str, Any]]] = []
    tasks: list[tuple[str, int, int | None, int | None]] = []
    for sym in _normalize_symbols(symbols_linear, disabled, ts_now):
        if int(disabled.get(sym, 0) or 0) > ts_now:
            continue
        last_oi_ts = db.get_latest_open_interest_ts(conn, sym)
        limit, start_ms, end_ms = _open_interest_fetch_plan(last_oi_ts, ts_now, interval_sec=3600)
        tasks.append((sym, limit, start_ms, end_ms))

    def _oi_task_worker(task: tuple[str, int, int | None, int | None]) -> tuple[str, list[dict[str, Any]]]:
        sym, limit, start_ms, end_ms = task
        oi_rows_raw = _fetch_open_interest_rows(client, sym, interval="1h", limit=limit, start_ms=start_ms, end_ms=end_ms)
        oi_rows = []
        for row in oi_rows_raw or []:
            ts = strict_integer(row.get("ts"))
            oi = _to_float(row.get("oi"), minimum=0.0)
            if ts is None or ts <= 0 or oi is None:
                continue
            oi_rows.append({"ts": ts, "oi": oi})
        return sym, oi_rows

    for idx, (task, result, err) in enumerate(_run_tasks_bounded(tasks, _oi_task_worker, max_workers), start=1):
        sym, _limit, _start_ms, _end_ms = task
        if err is not None:
            if _is_not_supported_symbol(err):
                retry_at = _disable_symbol("linear", sym, ts_now)
                disabled[sym] = retry_at
                oi_log_events.append((
                    "SYMBOL_DISABLED",
                    {
                        "venue": "linear",
                        "symbol": sym,
                        "reason": str(err),
                        "field": "open_interest",
                        "retry_after_sec": DISABLED_SYMBOL_RETRY_TTL_SEC,
                        "retry_at": retry_at,
                    },
                ))
            else:
                oi_log_events.append(("COLLECT_ERROR", {"venue": "linear", "symbol": sym, "field": "open_interest", "err": str(err)}))
        else:
            _sym_out, oi_rows = result
            if oi_rows:
                oi_pending_rows.append((sym, oi_rows))
                oi_written += len(oi_rows)
                oi_symbols += 1
        if idx % max(1, max_workers) == 0:
            _heartbeat(heartbeat)
    for sym, oi_rows in oi_pending_rows:
        db.upsert_open_interest(conn, sym, oi_rows, commit=False)
    for action, details in oi_log_events:
        db.log_decision(conn, action, None, None, details, commit=False)
    if oi_pending_rows or oi_log_events:
        conn.commit()
    _heartbeat(heartbeat)

    conn.commit()
    return {"venue": "linear", "open_interest_symbols": oi_symbols, "open_interest_written": oi_written}
