from __future__ import annotations

from . import db

BOT_HORIZONS: dict[str, int] = {
    "spot_grid": 4 * 3600,
    "futures_grid": 4 * 3600,
    "dca_bot": 24 * 3600,
    "futures_martingale": 1 * 3600,
    "futures_combo": 2 * 3600,
}
HORIZON_SEC_DEFAULT = 30 * 60

GRID_BOTS = {"spot_grid", "futures_grid"}
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


def _get_price_range_in_window(conn, venue: str, symbol: str, ts_start: int, ts_end: int) -> tuple[float, float] | None:
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
    for block in (params.get("cost_model") or {}, (params.get("trade_plan") or {}).get("cost_model") or {}):
        if block.get("total_cost_bps") is not None:
            try:
                return float(block.get("total_cost_bps"))
            except Exception:
                pass
    return float(fallback)


def _signed_return(entry: float, exitp: float, direction: str) -> float:
    if not entry:
        return 0.0
    raw = (float(exitp) - float(entry)) / float(entry)
    return -raw if direction == "short" else raw


# Outcome 'ret' is used later for diagnostics/calibration summaries.
# Keep it net-of-cost and aligned with the mechanics of each bot, otherwise
# the database shows absurd combinations like win_rate=1.0 with strongly
# negative average return for range/grid bots.
def _net_return(entry: float, exitp: float, direction: str, total_cost_bps: float, turns: float = 1.0) -> float:
    gross = _signed_return(entry, exitp, direction)
    cost_pct = max(0.0, float(total_cost_bps)) / 10_000.0 * max(1.0, float(turns))
    return gross - cost_pct


def _tp_sl_outcome(
    conn,
    venue: str,
    symbol: str,
    entry: float,
    direction: str,
    ts_start: int,
    ts_end: int,
    atr_1h: float,
    total_cost_bps: float = 15.0,
) -> tuple[int, float, float] | None:
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
        close = float(row["close"])
        if direction == "long":
            if hi >= tp_price and lo <= sl_price:
                return 0, _net_return(entry, sl_price, direction, total_cost_bps), close
            if hi >= tp_price:
                return 1, _net_return(entry, tp_price, direction, total_cost_bps), close
            if lo <= sl_price:
                return 0, _net_return(entry, sl_price, direction, total_cost_bps), close
        else:
            if lo <= tp_price and hi >= sl_price:
                return 0, _net_return(entry, sl_price, direction, total_cost_bps), close
            if lo <= tp_price:
                return 1, _net_return(entry, tp_price, direction, total_cost_bps), close
            if hi >= sl_price:
                return 0, _net_return(entry, sl_price, direction, total_cost_bps), close

    final_close = float(rows[-1]["close"])
    ret_proxy = _net_return(entry, final_close, direction, total_cost_bps)
    return (1 if ret_proxy > 0 else 0), ret_proxy, final_close


def _simulate_martingale_outcome(
    conn,
    venue: str,
    symbol: str,
    entry: float,
    direction: str,
    ts_start: int,
    ts_end: int,
    params: dict | None,
    total_cost_bps: float,
) -> tuple[int, float, float] | None:
    rows = _iter_1m_candles(conn, venue, symbol, ts_start, ts_end)
    if not rows:
        return None

    params = params or {}
    trade_plan = params.get("trade_plan") or {}
    levels = trade_plan.get("levels") or {}
    cost_floor = max(0.0, float(total_cost_bps) / 10_000.0)

    step_pct = float(params.get("step_pct") or 0.0) / 100.0
    max_steps = int(params.get("max_steps") or 0)
    if step_pct <= 0:
        step_pct = max(0.003, cost_floor * 1.25)

    tp_list = levels.get("take_profit") or []
    tp_price_hint = None
    if isinstance(tp_list, list) and tp_list:
        try:
            tp_price_hint = float((tp_list[0] or {}).get("price"))
        except Exception:
            tp_price_hint = None
    try:
        sl_price_hint = float((levels.get("stop_loss") or {}).get("price"))
    except Exception:
        sl_price_hint = None
    try:
        kill_abs = float((levels.get("risk_kill_switch") or {}).get("max_adverse_move"))
    except Exception:
        kill_abs = None

    fills = [float(entry)]
    next_step_idx = 1

    for row in rows:
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        while next_step_idx <= max_steps:
            if direction == "long":
                ladder_px = entry * (1.0 - step_pct * next_step_idx)
                triggered = low <= ladder_px
            else:
                ladder_px = entry * (1.0 + step_pct * next_step_idx)
                triggered = high >= ladder_px
            if triggered:
                fills.append(ladder_px)
                next_step_idx += 1
            else:
                break

        avg_entry = sum(fills) / len(fills)
        if tp_price_hint is not None and entry > 0:
            tp_pct = abs(tp_price_hint - entry) / entry
        else:
            tp_pct = max(cost_floor * 1.5, 0.0035)
        if sl_price_hint is not None and entry > 0:
            sl_pct = abs(sl_price_hint - entry) / entry
        elif kill_abs is not None and avg_entry > 0:
            sl_pct = max(cost_floor * 2.0, abs(kill_abs) / avg_entry)
        else:
            sl_pct = max(tp_pct * 2.0, cost_floor * 2.0, 0.007)

        turns = max(1.0, len(fills))
        if direction == "long":
            tp_price = avg_entry * (1.0 + tp_pct)
            stop_price = avg_entry * (1.0 - sl_pct)
            hit_tp = high >= tp_price
            hit_stop = low <= stop_price
        else:
            tp_price = avg_entry * (1.0 - tp_pct)
            stop_price = avg_entry * (1.0 + sl_pct)
            hit_tp = low <= tp_price
            hit_stop = high >= stop_price

        if hit_tp and hit_stop:
            return 0, _net_return(avg_entry, stop_price, direction, total_cost_bps, turns=turns), close
        if hit_tp:
            return 1, _net_return(avg_entry, tp_price, direction, total_cost_bps, turns=turns), close
        if hit_stop:
            return 0, _net_return(avg_entry, stop_price, direction, total_cost_bps, turns=turns), close

    final_close = float(rows[-1]["close"])
    avg_entry = sum(fills) / len(fills)
    ret_proxy = _net_return(avg_entry, final_close, direction, total_cost_bps, turns=max(1.0, len(fills)))
    return (1 if ret_proxy > max(cost_floor, 0.001) else 0), ret_proxy, final_close


def _grid_outcome(conn, venue: str, symbol: str, entry: float, exitp: float, ts_start: int, ts_end: int, params: dict | None) -> tuple[int, float]:
    params = params or {}
    cost_floor = _extract_total_cost_bps(params) / 10_000
    lo = params.get("price_range_lower")
    hi = params.get("price_range_upper")
    grid_spacing_pct = float(params.get("grid_spacing_pct") or 0.0)
    grid_levels = int(params.get("grid_levels") or 0)

    price_window = _get_price_range_in_window(conn, venue, symbol, ts_start, ts_end)
    if price_window is None or not entry:
        return 0, -cost_floor

    min_p, max_p = price_window
    realized_span_pct = max(0.0, (max_p - min_p) / entry)
    step_pct = max(grid_spacing_pct / 100.0, cost_floor * 1.25, 0.002)
    completed_steps = int(realized_span_pct / step_pct) if step_pct > 0 else 0
    if grid_levels > 0:
        completed_steps = min(completed_steps, grid_levels)

    # Approximate per-leg grid capture using the same ~0.6-0.8 step heuristic as trade_plan.
    gross_leg_pct = max(step_pct * 0.70, cost_floor * 1.10)
    gross_proxy = completed_steps * gross_leg_pct
    net_proxy = gross_proxy - (max(1, completed_steps) * cost_floor)

    if lo and hi and lo > 0 and hi > lo:
        range_breach = (min_p < lo * 0.995) or (max_p > hi * 1.005)
        in_range_at_exit = lo <= exitp <= hi
        if range_breach:
            net_proxy -= max(cost_floor, abs(min(lo - min_p, 0.0)) / entry, abs(max(max_p - hi, 0.0)) / entry)
        if not in_range_at_exit:
            net_proxy -= abs((exitp - entry) / entry)
        success = 1 if (in_range_at_exit and not range_breach and completed_steps >= 1 and net_proxy > 0) else 0
        return success, net_proxy

    end_drift = abs((exitp - entry) / entry) if entry else 0.0
    net_proxy -= end_drift
    success = 1 if (end_drift < 0.015 and completed_steps >= 1 and net_proxy > 0) else 0
    return success, net_proxy


def _simulate_dca_long_outcome(conn, venue: str, symbol: str, entry: float, ts_start: int, ts_end: int, params: dict | None) -> tuple[int, float, float] | None:
    rows = _iter_1m_candles(conn, venue, symbol, ts_start, ts_end)
    if not rows:
        return None

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
        turns = max(1.0, 0.5 + 0.5 * len(fills))
        tp_pct = max(cost_floor * 1.25, (float(tp_abs) / avg_entry) if tp_abs else 0.003)
        tp_price = avg_entry * (1.0 + tp_pct)
        if high >= tp_price:
            return 1, _net_return(avg_entry, tp_price, "long", _extract_total_cost_bps(params), turns=turns), close

        if stop_out_price is not None and low <= float(stop_out_price):
            sop = float(stop_out_price)
            return 0, _net_return(avg_entry, sop, "long", _extract_total_cost_bps(params), turns=turns), close

    final_close = float(rows[-1]["close"])
    avg_entry = sum(fills) / len(fills)
    ret_proxy = _net_return(avg_entry, final_close, "long", _extract_total_cost_bps(params), turns=max(1.0, 0.5 + 0.5 * len(fills)))
    success = 1 if ret_proxy > 0 else 0
    return success, ret_proxy, final_close


def compute_outcomes_once(conn, horizon_sec: int = HORIZON_SEC_DEFAULT, max_to_process: int = 500) -> int:
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
        rec_id = r["rec_id"]
        bot_type = r["bot_type"]
        venue = r["venue"]
        symbol = r["symbol"]
        direction = r["direction"]
        ts0 = int(r["ts"])

        params = _get_rec_params(conn, rec_id)
        cur_rec = conn.execute(
            "SELECT features_ref_ts FROM recommendations WHERE rec_id=?",
            (rec_id,),
        )
        rec_row = cur_rec.fetchone()
        entry_ts = int(rec_row["features_ref_ts"]) if rec_row and rec_row["features_ref_ts"] is not None else ts0

        effective_horizon = BOT_HORIZONS.get(bot_type, horizon_sec)
        if db.now_ts() < entry_ts + effective_horizon:
            continue
        ts_exit = entry_ts + effective_horizon

        entry = None
        trade_plan = ((params or {}).get("trade_plan") or {}) if isinstance(params, dict) else {}
        ref_price = trade_plan.get("reference_price")
        try:
            if ref_price is not None and float(ref_price) > 0:
                entry = float(ref_price)
        except Exception:
            entry = None
        if entry is None:
            entry = _get_close_at_or_after(conn, venue, symbol, entry_ts)
        if entry is None or entry == 0:
            continue
        price_ret: float
        ret_proxy: float
        success: int
        exitp: float

        if bot_type in GRID_BOTS:
            ep = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp = ep
            price_ret = (exitp - entry) / entry
            success, ret_proxy = _grid_outcome(conn, venue, symbol, entry, exitp, ts0, ts_exit, params)

        elif bot_type in DIRECTIONAL_BOTS:
            ep = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp = ep
            price_ret = (exitp - entry) / entry
            total_cost_bps = _extract_total_cost_bps(params)
            ret_proxy = _net_return(entry, exitp, direction if direction in ("long", "short") else "long", total_cost_bps) if direction in ("long", "short") else price_ret

            if bot_type == "futures_combo" or direction == "hedge":
                vol = ((params or {}).get("trade_plan") or {}).get("volatility") or {}
                atr_1h = float(vol.get("atr_pct_1h") or vol.get("atr_pct_used") or 0.0)
                cost_floor = total_cost_bps / 10_000
                window = _get_price_range_in_window(conn, venue, symbol, ts0, ts_exit)
                if window is None:
                    continue
                min_p, max_p = window
                realized_move = max(abs(max_p - entry), abs(min_p - entry)) / entry
                atr_thresh = max(cost_floor * 2.0, 0.020, atr_1h * 0.8 if atr_1h > 0 else 0.0)
                ret_proxy = realized_move - atr_thresh
                success = 1 if realized_move > atr_thresh else 0

            elif bot_type == "futures_martingale" and direction in ("long", "short"):
                sim = _simulate_martingale_outcome(conn, venue, symbol, entry, direction, ts0, ts_exit, params, total_cost_bps)
                if sim is not None:
                    success, ret_proxy, exitp = sim
                    price_ret = (exitp - entry) / entry
                else:
                    vol = ((params or {}).get("trade_plan") or {}).get("volatility") or {}
                    atr_1h = float(vol.get("atr_pct_1h") or vol.get("atr_pct_used") or 0.02)
                    tp_sl = _tp_sl_outcome(conn, venue, symbol, entry, direction, ts0, ts_exit, atr_1h, total_cost_bps)
                    if tp_sl is not None:
                        success, ret_proxy, exitp = tp_sl
                        price_ret = (exitp - entry) / entry
                    else:
                        direction_ret = -price_ret if direction == "short" else price_ret
                        ret_proxy = direction_ret - (total_cost_bps / 10_000)
                        cost_floor = total_cost_bps / 10_000
                        success = 1 if ret_proxy > cost_floor * 0.5 else 0

            elif bot_type == "dca_bot" and direction == "long":
                dca = _simulate_dca_long_outcome(conn, venue, symbol, entry, ts0, ts_exit, params)
                if dca is not None:
                    success, ret_proxy, exitp = dca
                    price_ret = (exitp - entry) / entry

            elif direction in ("long", "short"):
                cost_floor = total_cost_bps / 10_000
                ret_proxy = (-price_ret if direction == "short" else price_ret) - cost_floor
                success = 1 if ret_proxy > 0 else 0
            else:
                continue

        else:
            continue

        db.insert_outcome(
            conn,
            {
                "rec_id": rec_id,
                "ts": ts0,
                "venue": venue,
                "symbol": symbol,
                "bot_type": bot_type,
                "direction": direction,
                "horizon_sec": effective_horizon,
                "entry_close": float(entry),
                "exit_close": float(exitp),
                "ret": float(ret_proxy),
                "success": int(success),
            },
        )
        done += 1

    return done
