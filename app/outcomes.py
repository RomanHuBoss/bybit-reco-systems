from __future__ import annotations

from . import db
from .bot_types import GRID_BOT_TYPES, SUPPORTED_BOT_TYPES
import logging

logger = logging.getLogger(__name__)

BOT_HORIZONS: dict[str, int] = {
    "spot_grid": 6 * 3600,
    "futures_grid": 6 * 3600,
}
HORIZON_SEC_DEFAULT = 30 * 60

GRID_BOTS = set(GRID_BOT_TYPES)


def _resolve_effective_horizon(bot_type: str, params: dict | None, fallback_horizon_sec: int) -> tuple[int, bool]:
    params = params or {}
    trade_plan = params.get("trade_plan") or {}
    expected_horizon = trade_plan.get("expected_horizon") or {}
    max_hours_raw = expected_horizon.get("max_hours")
    builtin_horizon = BOT_HORIZONS.get(bot_type)
    if max_hours_raw is None:
        if builtin_horizon is not None:
            return int(builtin_horizon), False
        return int(fallback_horizon_sec), True

    try:
        max_hours = float(max_hours_raw)
    except Exception:
        if builtin_horizon is not None:
            return int(builtin_horizon), False
        return int(fallback_horizon_sec), True

    bounds = {
        "spot_grid": (6.0, 48.0),
        "futures_grid": (6.0, 48.0),
    }
    lo, hi = bounds.get(bot_type, (0.5, 72.0))
    return int(max(lo, min(hi, max_hours)) * 3600), False


def _is_supported_direction(bot_type: str, venue: str, direction: str) -> bool:
    if bot_type == "spot_grid":
        return direction in ("neutral", "long")
    if bot_type == "futures_grid":
        return direction in ("neutral", "long", "short")
    return False


def _get_close_at_or_after(conn, venue: str, symbol: str, ts: int) -> float | None:
    cur = conn.execute(
        """SELECT close FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts>=?
           ORDER BY ts ASC LIMIT 1""",
        (venue, symbol, ts),
    )
    r = cur.fetchone()
    return float(r["close"]) if r else None


def _get_first_tradeable_candle_after(conn, venue: str, symbol: str, ts: int) -> tuple[int, float] | None:
    """Return the first 1m candle strictly after the signal reference candle.

    Recommendation features are computed on the last fully closed candle whose timestamp is
    stored as features_ref_ts (bar start time). Entering at that same candle close is mildly
    look-ahead/optimistic because the signal itself already used that bar's full OHLC. The
    earliest tradeable point from 1m OHLC data is therefore the NEXT candle open.
    """
    cur = conn.execute(
        """SELECT ts, open FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts>?
           ORDER BY ts ASC LIMIT 1""",
        (venue, symbol, ts),
    )
    r = cur.fetchone()
    if not r:
        return None
    return int(r["ts"]), float(r["open"])


def _get_open_at_or_after(conn, venue: str, symbol: str, ts: int) -> float | None:
    cur = conn.execute(
        """SELECT open FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts>=?
           ORDER BY ts ASC LIMIT 1""",
        (venue, symbol, ts),
    )
    r = cur.fetchone()
    return float(r["open"]) if r else None


def _get_price_range_in_window(conn, venue: str, symbol: str, ts_start: int, ts_end_exclusive: int) -> tuple[float, float] | None:
    cur = conn.execute(
        """SELECT MIN(low) as lo, MAX(high) as hi FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60
           AND ts>=? AND ts<?""",
        (venue, symbol, ts_start, ts_end_exclusive),
    )
    r = cur.fetchone()
    if not r or r["lo"] is None:
        return None
    return float(r["lo"]), float(r["hi"])


def _iter_1m_candles(conn, venue: str, symbol: str, ts_start: int, ts_end_exclusive: int):
    cur = conn.execute(
        """SELECT ts, open, high, low, close FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60
           AND ts>=? AND ts<?
           ORDER BY ts ASC""",
        (venue, symbol, ts_start, ts_end_exclusive),
    )
    return cur.fetchall()


def _extract_cost_components(params: dict | None, fallback_execution_bps: float = 15.0) -> tuple[float, float]:
    execution_bps = None
    funding_bps = None
    if params:
        for block in (params.get("cost_model") or {}, (params.get("trade_plan") or {}).get("cost_model") or {}):
            if not isinstance(block, dict):
                continue
            if execution_bps is None:
                for key in ("execution_cost_bps", "total_cost_bps", "net_cost_bps"):
                    if block.get(key) is not None:
                        try:
                            execution_bps = float(block.get(key))
                            break
                        except Exception:
                            logger.debug("cost block parse error", exc_info=True)

            if funding_bps is None and block.get("expected_funding_bps") is not None:
                try:
                    funding_bps = float(block.get("expected_funding_bps"))
                except Exception:
                    logger.debug("funding block parse error", exc_info=True)
    return float(execution_bps if execution_bps is not None else fallback_execution_bps), float(funding_bps or 0.0)


def _extract_total_cost_bps(params: dict | None, fallback: float = 15.0) -> float:
    execution_bps, _ = _extract_cost_components(params, fallback_execution_bps=fallback)
    return float(execution_bps)


def _signed_return(entry: float, exitp: float, direction: str) -> float:
    if not entry:
        return 0.0
    raw = (float(exitp) - float(entry)) / float(entry)
    return -raw if direction == "short" else raw


# Outcome 'ret' is used later for diagnostics/calibration summaries.
# Keep it net-of-cost and aligned with the mechanics of each bot, otherwise
# the database shows absurd combinations like win_rate=1.0 with strongly
# negative average return for range/grid bots.
def _net_return(
    entry: float,
    exitp: float,
    direction: str,
    execution_cost_bps: float,
    turns: float = 1.0,
    fixed_cost_bps: float = 0.0,
) -> float:
    gross = _signed_return(entry, exitp, direction)
    exec_cost_pct = max(0.0, float(execution_cost_bps)) / 10_000.0 * max(1.0, float(turns))
    fixed_cost_pct = float(fixed_cost_bps) / 10_000.0
    return gross - exec_cost_pct - fixed_cost_pct


def _grid_outcome(conn, venue: str, symbol: str, entry: float, exitp: float, ts_start: int, ts_end: int, params: dict | None) -> tuple[int, float]:
    params = params or {}
    execution_cost_bps, _ = _extract_cost_components(params)
    cost_floor = execution_cost_bps / 10_000
    lo = params.get("price_range_lower")
    hi = params.get("price_range_upper")
    grid_spacing_pct = float(params.get("grid_spacing_pct") or 0.0)
    grid_levels = int(params.get("grid_levels") or 0)

    price_window = _get_price_range_in_window(conn, venue, symbol, ts_start, ts_end)
    if price_window is None or not entry:
        return 0, -cost_floor

    min_p, max_p = price_window
    # Must match recommender-side economics: only ~70% of nominal spacing is usually
    # monetised per completed grid cycle, so the spacing floor has to exceed costs after
    # that haircut. Otherwise outcome labels become systematically too optimistic.
    min_step_pct = max((cost_floor / 0.70) * 1.15, 0.0008)
    step_pct = max(grid_spacing_pct / 100.0, min_step_pct)
    step_abs = entry * step_pct

    # Conservative path approximation.
    # A grid should earn on back-and-forth traversals between levels, not on a single
    # monotonic drift that merely spans many levels. The previous implementation used
    # only (max - min) inside the window and therefore could mark a one-way move inside
    # the range as successful despite zero round-trips.
    completed_steps = 0
    if step_abs > 0 and lo is not None and hi is not None and float(hi) > float(lo):
        rows = _iter_1m_candles(conn, venue, symbol, ts_start, ts_end)
        closes = [float(r["close"]) for r in rows] if rows else []
        if closes:
            lower = float(lo)
            upper = float(hi)
            n_levels = max(1, int(round((upper - lower) / step_abs)))

            def _level_idx(px: float) -> int:
                rel = (px - lower) / max(step_abs, 1e-12)
                return max(0, min(n_levels, int(rel)))

            idx_prev = _level_idx(closes[0])
            up_moves = 0
            down_moves = 0
            for px in closes[1:]:
                idx_now = _level_idx(px)
                delta = idx_now - idx_prev
                if delta > 0:
                    up_moves += delta
                elif delta < 0:
                    down_moves += -delta
                idx_prev = idx_now

            completed_steps = min(up_moves, down_moves)
            if grid_levels > 0:
                completed_steps = min(completed_steps, grid_levels)

    # Approximate per-leg grid capture using the same ~0.6-0.8 step heuristic as trade_plan.
    # Do not impose an optimistic minimum gross leg above the actual configured spacing:
    # that would fabricate profitable labels on grids whose advertised step is still below
    # economic break-even.
    gross_leg_pct = step_pct * 0.70
    gross_proxy = completed_steps * gross_leg_pct
    net_proxy = gross_proxy - (max(1, completed_steps) * cost_floor)

    if lo and hi and lo > 0 and hi > lo:
        range_breach = (min_p < lo * 0.995) or (max_p > hi * 1.005)
        in_range_at_exit = lo <= exitp <= hi
        if range_breach:
            net_proxy -= max(cost_floor, abs(min(lo - min_p, 0.0)) / entry, abs(max(max_p - hi, 0.0)) / entry)
        if not in_range_at_exit:
            net_proxy -= abs((exitp - entry) / entry)
        success = 1 if (completed_steps >= 1 and net_proxy > 0) else 0
        return success, net_proxy

    end_drift = abs((exitp - entry) / entry) if entry else 0.0
    net_proxy -= end_drift
    success = 1 if (end_drift < 0.015 and completed_steps >= 1 and net_proxy > 0) else 0
    return success, net_proxy


def compute_outcomes_once(conn, horizon_sec: int = HORIZON_SEC_DEFAULT, max_to_process: int = 500) -> int:
    min_horizon = min(BOT_HORIZONS.values())

    cur = conn.execute(
        """SELECT r.rec_id, r.ts, r.venue, r.symbol, r.bot_type, r.direction,
                  r.params_json, r.features_ref_ts
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
        if bot_type not in SUPPORTED_BOT_TYPES:
            db.log_decision(conn, "OUTCOME_SKIP_UNSUPPORTED_BOT_TYPE", rec_id, None, {"bot_type": bot_type})
            continue
        venue = r["venue"]
        symbol = r["symbol"]
        direction = r["direction"]
        ts0 = int(r["ts"])

        if not _is_supported_direction(bot_type, venue, direction):
            db.log_decision(conn, "OUTCOME_SKIP_UNSUPPORTED_DIRECTION", rec_id, None, {
                "bot_type": bot_type,
                "venue": venue,
                "symbol": symbol,
                "direction": direction,
            })
            continue

        import json
        try:
            params = json.loads(r["params_json"]) if r["params_json"] else None
        except Exception:
            params = None
        signal_ref_ts = int(r["features_ref_ts"]) if r["features_ref_ts"] is not None else ts0

        tradeable = _get_first_tradeable_candle_after(conn, venue, symbol, signal_ref_ts)
        if tradeable is None:
            continue
        entry_ts, entry = tradeable

        effective_horizon, used_fallback_horizon = _resolve_effective_horizon(bot_type, params, horizon_sec)
        if used_fallback_horizon:
            db.log_decision(conn, "OUTCOME_HORIZON_FALLBACK_USED", rec_id, None, {
                "bot_type": bot_type,
                "fallback_horizon_sec": effective_horizon,
            })
        if db.now_ts() < entry_ts + effective_horizon:
            continue
        ts_exit = entry_ts + effective_horizon

        if entry is None or entry == 0:
            continue
        price_ret: float
        ret_proxy: float
        success: int
        exitp: float

        if bot_type in GRID_BOTS:
            ep = _get_open_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp = ep
            price_ret = (exitp - entry) / entry
            success, ret_proxy = _grid_outcome(conn, venue, symbol, entry, exitp, entry_ts, ts_exit, params)
            _, funding_cost_bps = _extract_cost_components(params)
            if venue == "linear" and funding_cost_bps:
                ret_proxy -= funding_cost_bps / 10_000.0
                if ret_proxy <= 0:
                    success = 0

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
