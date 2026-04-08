from __future__ import annotations

from . import db
from .bot_types import GRID_BOT_TYPES, SUPPORTED_BOT_TYPES
from .settings import load_settings
import logging
import math

logger = logging.getLogger(__name__)
settings = load_settings()

BOT_HORIZONS: dict[str, int] = {
    "spot_grid": 12 * 3600,
    "futures_grid": 12 * 3600,
}
HORIZON_SEC_DEFAULT = 30 * 60

GRID_BOTS = set(GRID_BOT_TYPES)


def _resolve_effective_horizon(bot_type: str, params: dict | None, fallback_horizon_sec: int) -> tuple[int, bool]:
    params = params if isinstance(params, dict) else {}

    def _hours_to_sec(value: object) -> int | None:
        try:
            hours = float(value)
        except Exception:
            return None
        if not math.isfinite(hours) or hours <= 0:
            return None
        return int(hours * 3600)

    def _bounded_hours(hours: float) -> float:
        bounds = {
            "spot_grid": (6.0, 48.0),
            "futures_grid": (6.0, 48.0),
        }
        lo, hi = bounds.get(bot_type, (0.5, 72.0))
        return max(lo, min(hi, float(hours)))

    trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    expected_horizon = trade_plan.get("expected_horizon") if isinstance(trade_plan.get("expected_horizon"), dict) else {}

    # Outcome labeling is bot-mechanics-specific and should mature on the dedicated
    # label horizon, not on the operator-facing max holding window. Otherwise grid
    # recommendations can sit 28h/48h without labels even though the intended
    # evaluation horizon is 12h.
    explicit_hours = (
        params.get("label_horizon_hours")
        or trade_plan.get("label_horizon_hours")
        or expected_horizon.get("label_horizon_hours")
    )
    explicit_sec = _hours_to_sec(explicit_hours)
    if explicit_sec is not None:
        return int(_bounded_hours(explicit_sec / 3600.0) * 3600), False

    builtin_horizon = BOT_HORIZONS.get(bot_type)
    if builtin_horizon is not None:
        return int(builtin_horizon), False

    max_hours_raw = expected_horizon.get("max_hours")
    max_sec = _hours_to_sec(max_hours_raw)
    if max_sec is not None:
        return int(_bounded_hours(max_sec / 3600.0) * 3600), False

    return int(fallback_horizon_sec), True


def _is_supported_direction(bot_type: str, venue: str, direction: str) -> bool:
    venue_norm = str(venue or "").strip().lower()
    direction_norm = str(direction or "").strip().lower()
    if bot_type == "spot_grid":
        return venue_norm == "spot" and direction_norm in ("neutral", "long")
    if bot_type == "futures_grid":
        return venue_norm == "linear" and direction_norm in ("neutral", "long", "short")
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


def _finite_or_default(value: object, default: float) -> float:
    """Безопасно приводит стоимость к finite float.

    Outcome-labeling не должен получать NaN/inf из legacy params или ручных
    правок JSON: иначе и ret, и calibration diagnostics могут тихо стать
    нечисловыми и сломать downstream-агрегации.
    """
    try:
        num = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(num):
        return float(default)
    return float(num)


def _finite_positive_or_none(value: object) -> float | None:
    """Положительное finite число либо ``None``.

    Для ценовых границ grid-модели poisoned значения вроде ``"NaN"``/``"Infinity"``
    особенно опасны: они не роняют outcome-cycle, но тихо отключают range/kill-switch
    penalties и ломают интерпретацию исторической разметки.
    """
    try:
        if value is None:
            return None
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num) or num <= 0:
        return None
    return float(num)


def _int_from_params(value: object, default: int = 0, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """Нормализует целочисленные grid-параметры из legacy/manual JSON.

    Исторические рекомендации могут содержать строковый мусор, NaN/inf или
    частично испорченный payload. Outcome-процесс не должен падать на таком
    rec и блокировать дальнейшую разметку; лучше безопасно деградировать к
    консервативному default и продолжить labeling.
    """
    try:
        num = int(float(value))
    except Exception:
        num = int(default)
    if minimum is not None:
        num = max(int(minimum), num)
    if maximum is not None:
        num = min(int(maximum), num)
    return int(num)


def _extract_cost_components(params: dict | None, fallback_execution_bps: float = 15.0) -> tuple[float, float]:
    execution_bps = None
    funding_bps = None
    net_cost_bps = None
    if params:
        for block in (params.get("cost_model") or {}, (params.get("trade_plan") or {}).get("cost_model") or {}):
            if not isinstance(block, dict):
                continue
            if execution_bps is None:
                for key in ("execution_cost_bps", "total_cost_bps"):
                    if block.get(key) is not None:
                        try:
                            execution_bps = _finite_or_default(block.get(key), float(fallback_execution_bps))
                            break
                        except Exception:
                            logger.debug("cost block parse error", exc_info=True)

            if net_cost_bps is None and block.get("net_cost_bps") is not None:
                try:
                    net_cost_bps = _finite_or_default(block.get("net_cost_bps"), float(fallback_execution_bps))
                except Exception:
                    logger.debug("net cost block parse error", exc_info=True)

            if funding_bps is None and block.get("expected_funding_bps") is not None:
                try:
                    funding_bps = _finite_or_default(block.get("expected_funding_bps"), 0.0)
                except Exception:
                    logger.debug("funding block parse error", exc_info=True)

    funding_bps_out = _finite_or_default(funding_bps if funding_bps is not None else 0.0, 0.0)
    if execution_bps is None and net_cost_bps is not None:
        # Legacy/manual payload может содержать только net_cost_bps. Для outcome-labeling
        # execution friction и funding carry учитываются раздельно: grid-модель использует
        # execution-cost floor внутри _grid_outcome(), а funding для linear вычитается позже.
        # Если blindly принять net_cost_bps за execution_cost_bps и потом ещё раз вычесть
        # expected_funding_bps, то funding будет учтён дважды и историческая разметка
        # станет излишне пессимистичной. Поэтому по возможности раскладываем net = exec + funding.
        execution_bps = max(0.0, float(net_cost_bps) - float(funding_bps_out))

    execution_bps_out = _finite_or_default(execution_bps if execution_bps is not None else fallback_execution_bps, float(fallback_execution_bps))
    return float(execution_bps_out), float(funding_bps_out)


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


def _resolve_grid_tp_leg_abs(entry: float, params: dict | None, fallback_step_abs: float | None = None) -> float | None:
    """Return explicit grid TP-per-leg distance in price terms.

    Outcome semantics should respect the operator-facing TP anchor from the
    recommendation payload. If the payload is partially malformed, degrade
    safely to a spacing-based fallback instead of silently disabling the TP
    success path.
    """
    params = params if isinstance(params, dict) else {}
    trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    levels = trade_plan.get("levels") if isinstance(trade_plan.get("levels"), dict) else {}
    tp_block = levels.get("tp_per_leg") if isinstance(levels.get("tp_per_leg"), dict) else {}

    tp_abs = _finite_positive_or_none(tp_block.get("abs")) if tp_block else None
    if tp_abs is not None:
        return float(tp_abs)

    tp_pct = _finite_positive_or_none(tp_block.get("pct")) if tp_block else None
    if tp_pct is not None and entry:
        return float(entry) * float(tp_pct) / 100.0

    if fallback_step_abs is not None and fallback_step_abs > 0.0:
        return float(fallback_step_abs) * 0.70
    return None


def _grid_tp_hit(min_p: float, max_p: float, entry: float, direction: str, tp_leg_abs: float | None) -> bool:
    if not entry or tp_leg_abs is None or tp_leg_abs <= 0.0:
        return False
    long_tp = float(entry) + float(tp_leg_abs)
    short_tp = float(entry) - float(tp_leg_abs)
    if direction == "short":
        return min_p <= short_tp
    if direction == "neutral":
        # Neutral grid should not shortcut to success on a one-sided barrier touch.
        # Keep neutral labeling anchored to realised oscillation / PnL mechanics
        # so a single directional spike does not inflate win-rate.
        return False
    return max_p >= long_tp


def _grid_outcome(
    conn,
    venue: str,
    symbol: str,
    entry: float,
    exitp: float,
    ts_start: int,
    ts_end: int,
    direction: str,
    params: dict | None,
) -> tuple[int, float]:
    params = params or {}
    execution_cost_bps, _ = _extract_cost_components(params)
    cost_floor = execution_cost_bps / 10_000.0
    grid_spacing_pct = _finite_or_default(params.get("grid_spacing_pct"), 0.0)
    grid_levels = _int_from_params(params.get("grid_levels"), 0, minimum=0, maximum=1000)

    trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    levels = trade_plan.get("levels") if isinstance(trade_plan.get("levels"), dict) else {}
    range_block = levels.get("range") if isinstance(levels.get("range"), dict) else {}
    kill_switch = levels.get("kill_switch") if isinstance(levels.get("kill_switch"), dict) else {}

    lo = _finite_positive_or_none(params.get("price_range_lower"))
    hi = _finite_positive_or_none(params.get("price_range_upper"))
    if lo is None:
        lo = _finite_positive_or_none(range_block.get("lower"))
    if hi is None:
        hi = _finite_positive_or_none(range_block.get("upper"))
    ks_lo = _finite_positive_or_none(kill_switch.get("lower")) if kill_switch else None
    ks_hi = _finite_positive_or_none(kill_switch.get("upper")) if kill_switch else None
    if ks_lo is None:
        ks_lo = lo
    if ks_hi is None:
        ks_hi = hi

    rows = _iter_1m_candles(conn, venue, symbol, ts_start, ts_end)
    if not rows or not entry:
        return 0, -cost_floor

    closes = [float(r["close"]) for r in rows]
    min_p = min(float(r["low"]) for r in rows)
    max_p = max(float(r["high"]) for r in rows)

    # Must match recommender-side economics: only a fraction of nominal spacing is
    # usually monetised after inventory skew, partial fills and queue priority.
    # Keep the same break-even floor as the recommender and then add an extra
    # execution haircut so labels do not become unrealistically optimistic.
    min_step_pct = max((cost_floor / 0.70) * 1.15, 0.0008)
    step_pct = max(grid_spacing_pct / 100.0, min_step_pct)
    step_abs = entry * step_pct
    tp_leg_abs = _resolve_grid_tp_leg_abs(entry, params, fallback_step_abs=step_abs)
    tp_hit = _grid_tp_hit(min_p, max_p, entry, direction, tp_leg_abs)

    completed_steps = 0
    in_range_ratio = 0.0
    range_span_pct = 0.0
    if step_abs > 0.0 and lo is not None and hi is not None and hi > lo:
        lower = float(lo)
        upper = float(hi)
        n_levels = max(1, int(round((upper - lower) / max(step_abs, 1e-12))))

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

        in_range_ratio = sum(1 for px in closes if lower <= px <= upper) / max(1, len(closes))
        range_span_pct = max(0.0, (upper - lower) / entry)

    fill_efficiency = 0.58 if direction == "neutral" else 0.62
    gross_leg_pct = step_pct * fill_efficiency
    gross_proxy = completed_steps * gross_leg_pct
    net_proxy = gross_proxy - (max(1, completed_steps) * cost_floor)

    # A grid is meant to harvest oscillation, not trend-following drift. Penalise any
    # unresolved displacement that remains at the end of the label horizon.
    raw_end_drift = abs((exitp - entry) / entry) if entry else 0.0
    signed_drift = _signed_return(entry, exitp, direction)
    if direction == "neutral":
        net_proxy -= raw_end_drift
    else:
        aligned_drift = max(0.0, signed_drift)
        adverse_drift = max(0.0, -signed_drift)
        net_proxy -= adverse_drift * 1.15
        net_proxy -= aligned_drift * 0.25

    main_breach_pct = 0.0
    kill_switch_breach_pct = 0.0
    exit_outside_pct = 0.0
    if lo is not None and hi is not None and hi > lo:
        lower = float(lo)
        upper = float(hi)
        below_main = max(0.0, lower - min_p) / entry
        above_main = max(0.0, max_p - upper) / entry
        main_breach_pct = below_main + above_main
        if main_breach_pct > 0.0:
            net_proxy -= 0.60 * main_breach_pct

        if ks_lo is not None and ks_hi is not None and ks_hi > ks_lo:
            below_ks = max(0.0, float(ks_lo) - min_p) / entry
            above_ks = max(0.0, max_p - float(ks_hi)) / entry
            kill_switch_breach_pct = below_ks + above_ks
            if kill_switch_breach_pct > 0.0:
                net_proxy -= (1.25 * kill_switch_breach_pct) + cost_floor

        if exitp < lower:
            exit_outside_pct = (lower - exitp) / entry
        elif exitp > upper:
            exit_outside_pct = (exitp - upper) / entry
        if exit_outside_pct > 0.0:
            net_proxy -= max(cost_floor * 0.75, exit_outside_pct * 0.75)

    min_range_ratio = 0.58 if direction == "neutral" else 0.45
    if in_range_ratio > 0.0 and in_range_ratio < min_range_ratio:
        occupancy_penalty_base = max(range_span_pct * 0.85, step_pct * 2.0)
        net_proxy -= (min_range_ratio - in_range_ratio) * occupancy_penalty_base

    # Explicit TP achievement should count as success for grid outcome semantics:
    # if the operator-facing per-leg target was touched inside the label window,
    # WR must reflect that realised profit opportunity even when the close of the
    # horizon later drifts away and the oscillation counter stays < 2.
    if tp_hit and tp_leg_abs is not None and entry:
        tp_realized_net = max(0.0001, (float(tp_leg_abs) / float(entry)) - cost_floor)
        net_proxy = max(net_proxy, tp_realized_net)

    # A single profitable leg is too easy to obtain on noisy data and was one of the
    # reasons why historical win-rate inflated toward ~100%. Require at least two
    # matched oscillation legs plus a buffer above explicit trading costs — unless
    # the explicit per-leg TP itself was already reached.
    min_steps_required = 2
    required_profit = max(
        cost_floor * (1.60 if direction == "neutral" else 1.35),
        gross_leg_pct * (1.20 if direction == "neutral" else 0.95),
        0.0005,
    )

    success = int(
        tp_hit
        or (
            completed_steps >= min_steps_required
            and (in_range_ratio == 0.0 or in_range_ratio >= min_range_ratio)
            and kill_switch_breach_pct <= 1e-12
            and net_proxy > required_profit
        )
    )
    return success, net_proxy


def compute_outcomes_once(conn, horizon_sec: int = HORIZON_SEC_DEFAULT, max_to_process: int = 500) -> int:
    min_horizon = min(BOT_HORIZONS.values())
    fetch_limit = max(int(max_to_process), min(2000, int(max_to_process) * 12))
    require_llm_verdict = bool(getattr(settings, "llm_reviewer_enabled", False))

    cur = conn.execute(
        """SELECT r.rec_id, r.ts, r.venue, r.symbol, r.bot_type, r.direction,
                  r.params_json, r.features_ref_ts, r.status, r.reasons_json
           FROM recommendations r
           LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
           WHERE r.ts <= ? AND o.rec_id IS NULL
           AND COALESCE(r.is_outcome_label_root, 1) = 1
           AND r.status NOT IN ('blocked', 'no_trade', 'suppressed', 'pending')
           ORDER BY r.ts ASC LIMIT ?""",
        (db.now_ts() - min_horizon, fetch_limit),
    )
    rows = cur.fetchall()
    done = 0

    for r in rows:
        if done >= int(max_to_process):
            break
        if require_llm_verdict and not db.is_outcome_eligible_under_llm_mode(r["status"], r["reasons_json"]):
            continue
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
            params = json.loads(r["params_json"], parse_constant=lambda _token: None) if r["params_json"] else None
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
        ret_proxy: float
        success: int
        exitp: float

        if bot_type in GRID_BOTS:
            ep = _get_open_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp = ep
            success, ret_proxy = _grid_outcome(conn, venue, symbol, entry, exitp, entry_ts, ts_exit, direction, params)
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
