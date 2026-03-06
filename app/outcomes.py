from __future__ import annotations

from . import db

# Per-bot-type outcome horizons
BOT_HORIZONS: dict[str, int] = {
    "spot_grid":          4 * 3600,
    "futures_grid":       4 * 3600,
    "dca_bot":           24 * 3600,
    "futures_martingale": 1 * 3600,
    "futures_combo":      2 * 3600,
}
HORIZON_SEC_DEFAULT = 30 * 60

GRID_BOTS        = {"spot_grid", "futures_grid"}
DIRECTIONAL_BOTS = {"dca_bot", "futures_martingale", "futures_combo"}


def _get_close_at_or_after(conn, venue: str, symbol: str, ts: int) -> float | None:
    cur = conn.execute(
        """SELECT close FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts>=?
           ORDER BY ts ASC LIMIT 1""",
        (venue, symbol, ts),
    )
    r = cur.fetchone()
    return float(r["close"]) if r else None


def _get_price_range_in_window(
    conn, venue: str, symbol: str, ts_start: int, ts_end: int
) -> tuple[float, float] | None:
    cur = conn.execute(
        """SELECT MIN(low) as lo, MAX(high) as hi FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60
           AND ts>=? AND ts<=?""",
        (venue, symbol, ts_start, ts_end),
    )
    r = cur.fetchone()
    if not r or r["lo"] is None:
        return None
    return float(r["lo"]), float(r["hi"])


def _iter_1m_candles(conn, venue: str, symbol: str, ts_start: int, ts_end: int):
    cur = conn.execute(
        """SELECT ts, open, high, low, close FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60
           AND ts>=? AND ts<=?
           ORDER BY ts ASC""",
        (venue, symbol, ts_start, ts_end),
    )
    return cur.fetchall()


def _get_rec_params(conn, rec_id: str) -> dict | None:
    cur = conn.execute("SELECT params_json FROM recommendations WHERE rec_id=?", (rec_id,))
    r = cur.fetchone()
    if not r:
        return None
    import json
    try:
        return json.loads(r["params_json"])
    except Exception:
        return None


def _extract_total_cost_bps(params: dict | None, fallback: float = 15.0) -> float:
    if not params:
        return float(fallback)
    for block in (
        params.get("cost_model") or {},
        (params.get("trade_plan") or {}).get("cost_model") or {},
    ):
        if block.get("total_cost_bps") is not None:
            try:
                return float(block.get("total_cost_bps"))
            except Exception:
                pass
    return float(fallback)


def _tp_sl_success(
    conn,
    venue: str,
    symbol: str,
    entry: float,
    direction: str,
    ts_start: int,
    ts_end: int,
    atr_1h: float,
    total_cost_bps: float = 15.0,
) -> int | None:
    """Determine martingale success by TP/SL hit using 1m candles.

    Conservative rule for ambiguous candles: if both TP and SL are touched inside
    the same 1m candle, count it as failure. Anything else would inject an
    arbitrary optimistic path assumption into the training labels.
    """
    cost_floor = total_cost_bps / 10_000
    tp_pct = max(cost_floor * 1.5, 0.30 * atr_1h)
    sl_pct = 2.0 * tp_pct

    if direction == "long":
        tp_price = entry * (1.0 + tp_pct)
        sl_price = entry * (1.0 - sl_pct)
    else:
        tp_price = entry * (1.0 - tp_pct)
        sl_price = entry * (1.0 + sl_pct)

    rows = _iter_1m_candles(conn, venue, symbol, ts_start, ts_end)
    if not rows:
        return None

    for row in rows:
        hi = float(row["high"])
        lo = float(row["low"])
        if direction == "long":
            if hi >= tp_price and lo <= sl_price:
                return 0
            if hi >= tp_price:
                return 1
            if lo <= sl_price:
                return 0
        else:
            if lo <= tp_price and hi >= sl_price:
                return 0
            if lo <= tp_price:
                return 1
            if hi >= sl_price:
                return 0

    return 0


def _grid_success(
    conn,
    venue: str,
    symbol: str,
    entry: float,
    exitp: float,
    ts_start: int,
    ts_end: int,
    params: dict | None,
) -> int:
    cost_floor = _extract_total_cost_bps(params) / 10_000
    lo = params.get("price_range_lower") if params else None
    hi = params.get("price_range_upper") if params else None
    grid_spacing_pct = float(params.get("grid_spacing_pct") or 0.0) if params else 0.0

    price_window = _get_price_range_in_window(conn, venue, symbol, ts_start, ts_end)
    if price_window is None:
        return 0

    min_p, max_p = price_window
    realized_span_pct = (max_p - min_p) / entry if entry else 0.0
    min_required_span = max(grid_spacing_pct / 100.0, cost_floor * 1.25, 0.002)

    if lo and hi and lo > 0 and hi > lo:
        range_breach = (min_p < lo * 0.995) or (max_p > hi * 1.005)
        in_range_at_exit = lo <= exitp <= hi
        return 1 if (in_range_at_exit and not range_breach and realized_span_pct >= min_required_span) else 0

    # Fallback when exact range is missing: require flat-ish final outcome AND enough oscillation to matter.
    price_ret = abs((exitp - entry) / entry) if entry else 0.0
    return 1 if price_ret < 0.015 and realized_span_pct >= min_required_span else 0


def _simulate_dca_long_success(
    conn,
    venue: str,
    symbol: str,
    entry: float,
    ts_start: int,
    ts_end: int,
    params: dict | None,
) -> tuple[int, float | None]:
    """Path-based approximation of DCA mechanics.

    Equal-sized ladder from the initial entry:
    - buy more every `dca_step_pct` below entry, up to `max_orders`
    - TP from average entry using trade_plan.take_profit_from_avg (or cost floor fallback)
    - stop out at trade_plan.stop_out
    Returns (success, final_exit_close_if_available).
    """
    rows = _iter_1m_candles(conn, venue, symbol, ts_start, ts_end)
    if not rows:
        return 0, None

    params = params or {}
    trade_plan = params.get("trade_plan") or {}
    cost_floor = _extract_total_cost_bps(params) / 10_000
    step_pct = float(params.get("dca_step_pct") or 0.0) / 100.0
    max_orders = int(params.get("max_orders") or 0)

    tp_abs = (((trade_plan.get("levels") or {}).get("take_profit_from_avg") or {}).get("abs"))
    stop_out_price = (((trade_plan.get("levels") or {}).get("stop_out") or {}).get("price"))

    if step_pct <= 0:
        step_pct = max(0.003, cost_floor * 1.5)

    fills = [entry]
    next_order_idx = 1

    for row in rows:
        low = float(row["low"])
        high = float(row["high"])
        close = float(row["close"])

        while next_order_idx <= max_orders:
            ladder_px = entry * (1.0 - step_pct * next_order_idx)
            if low <= ladder_px:
                fills.append(ladder_px)
                next_order_idx += 1
            else:
                break

        avg_entry = sum(fills) / len(fills)
        tp_pct = max(cost_floor * 1.25, (float(tp_abs) / avg_entry) if tp_abs else 0.003)
        tp_price = avg_entry * (1.0 + tp_pct)
        if high >= tp_price:
            return 1, close

        if stop_out_price is not None and low <= float(stop_out_price):
            return 0, close

    final_close = float(rows[-1]["close"])
    avg_entry = sum(fills) / len(fills)
    success = 1 if final_close > avg_entry * (1.0 + cost_floor) else 0
    return success, final_close


def compute_outcomes_once(
    conn, horizon_sec: int = HORIZON_SEC_DEFAULT, max_to_process: int = 500
) -> int:
    """Compute and store outcomes for recommendations whose horizon has passed."""
    min_horizon = min(BOT_HORIZONS.values())

    cur = conn.execute(
        """SELECT r.rec_id, r.ts, r.venue, r.symbol, r.bot_type, r.direction
           FROM recommendations r
           LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
           WHERE r.ts <= ? AND o.rec_id IS NULL
           AND r.status NOT IN ('blocked', 'no_trade', 'suppressed')
           ORDER BY r.ts ASC LIMIT ?""",
        (db.now_ts() - min_horizon, max_to_process),
    )
    rows = cur.fetchall()
    done = 0

    for r in rows:
        rec_id    = r["rec_id"]
        bot_type  = r["bot_type"]
        venue     = r["venue"]
        symbol    = r["symbol"]
        direction = r["direction"]
        ts0       = int(r["ts"])

        effective_horizon = BOT_HORIZONS.get(bot_type, horizon_sec)
        if db.now_ts() < ts0 + effective_horizon:
            continue
        ts_exit = ts0 + effective_horizon

        entry = _get_close_at_or_after(conn, venue, symbol, ts0)
        if entry is None or entry == 0:
            continue

        params = _get_rec_params(conn, rec_id)
        price_ret: float
        success: int
        exitp: float

        if bot_type in GRID_BOTS:
            ep = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp = ep
            price_ret = (exitp - entry) / entry
            success = _grid_success(conn, venue, symbol, entry, exitp, ts0, ts_exit, params)

        elif bot_type in DIRECTIONAL_BOTS:
            ep = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp = ep
            price_ret = (exitp - entry) / entry

            if bot_type == "futures_combo" or direction == "hedge":
                # The project does not model two-leg hedge PnL, so use a conservative
                # volatility-realisation proxy instead of pretending to know exact returns.
                vol = ((params or {}).get("trade_plan") or {}).get("volatility") or {}
                atr_1h = float(vol.get("atr_pct_1h") or vol.get("atr_pct_used") or 0.0)
                cost_floor = _extract_total_cost_bps(params) / 10_000
                window = _get_price_range_in_window(conn, venue, symbol, ts0, ts_exit)
                if window is None:
                    continue
                min_p, max_p = window
                realized_move = max(abs(max_p - entry), abs(min_p - entry)) / entry
                atr_thresh = max(cost_floor * 2.0, 0.020, atr_1h * 0.8 if atr_1h > 0 else 0.0)
                success = 1 if realized_move > atr_thresh else 0

            elif bot_type == "futures_martingale" and direction in ("long", "short"):
                vol = ((params or {}).get("trade_plan") or {}).get("volatility") or {}
                atr_1h = float(vol.get("atr_pct_1h") or vol.get("atr_pct_used") or 0.02)
                total_cost_bps = _extract_total_cost_bps(params)

                tp_sl = _tp_sl_success(
                    conn, venue, symbol, entry, direction,
                    ts0, ts_exit, atr_1h, total_cost_bps,
                )
                if tp_sl is not None:
                    success = tp_sl
                else:
                    direction_ret = -price_ret if direction == "short" else price_ret
                    cost_floor = total_cost_bps / 10_000
                    success = 1 if direction_ret > cost_floor * 1.5 else 0

            elif bot_type == "dca_bot" and direction == "long":
                success, exit_fallback = _simulate_dca_long_success(
                    conn, venue, symbol, entry, ts0, ts_exit, params,
                )
                if exit_fallback is not None:
                    exitp = float(exit_fallback)
                    price_ret = (exitp - entry) / entry

            elif direction in ("long", "short"):
                direction_ret = -price_ret if direction == "short" else price_ret
                cost_floor = _extract_total_cost_bps(params) / 10_000
                success = 1 if direction_ret > cost_floor else 0
            else:
                continue

        else:
            continue

        db.insert_outcome(conn, {
            "rec_id":      rec_id,
            "ts":          ts0,
            "venue":       venue,
            "symbol":      symbol,
            "bot_type":    bot_type,
            "direction":   direction,
            "horizon_sec": effective_horizon,
            "entry_close": float(entry),
            "exit_close":  float(exitp),
            "ret":         float(price_ret),
            "success":     int(success),
        })
        done += 1

    return done
