from __future__ import annotations

from . import db
from .bot_types import GRID_BOT_TYPES, SUPPORTED_BOT_TYPES
from .grid_math import resolve_integer_aliases, strict_integer
from .settings import load_settings
from .trading_semantics import normalize_execution_direction
import logging
import math

logger = logging.getLogger(__name__)
settings = load_settings()

BOT_HORIZONS: dict[str, int] = {
    "futures_grid": 12 * 3600,
}
HORIZON_SEC_DEFAULT = 30 * 60

GRID_BOTS = set(GRID_BOT_TYPES)


def _shadow_no_trade_outcome_eligible(status: object, reasons_json: object) -> bool:
    """Allow counterfactual labeling only when the publisher opted in explicitly.

    A ``no_trade`` row can be useful for unbiased research/calibration, but hard
    blocked or malformed candidates must never be silently promoted into the
    outcome sample. The JSON value must be a literal boolean ``true``.
    """
    if str(status or "").strip().lower() != "no_trade":
        return False
    try:
        reasons = db._json_loads_mapping_or_default(reasons_json, {})
    except Exception:
        return False
    policy = reasons.get("outcome_policy") if isinstance(reasons, dict) else None
    if not isinstance(policy, dict) or policy.get("eligible") is not True:
        return False
    if str(policy.get("sample_role") or "") != "shadow_no_trade":
        return False
    risk_checks = reasons.get("risk_checks")
    if not isinstance(risk_checks, dict) or risk_checks.get("passed") is not True:
        return False
    blocks = risk_checks.get("blocks")
    return isinstance(blocks, list) and len(blocks) == 0


def _resolve_effective_horizon(bot_type: str, params: dict | None, fallback_horizon_sec: int) -> tuple[int, bool]:
    params = params if isinstance(params, dict) else {}

    def _hours_to_sec(value: object) -> int | None:
        hours = strict_integer(value)
        if hours is None or hours <= 0:
            return None
        return int(hours * 3600)

    def _bounded_hours(hours: float) -> float:
        bounds = {
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
    """Return the exact next 1m candle after the signal reference candle.

    Recommendation features are computed on the last fully closed candle whose timestamp is
    stored as features_ref_ts (bar start time). Entering at that same candle close is mildly
    look-ahead/optimistic because the signal itself already used that bar's full OHLC. The
    earliest tradeable point from 1m OHLC data is therefore the NEXT candle open. A gap must
    remain unavailable rather than silently moving the hypothetical entry to a later market.
    """
    signal_ts = strict_integer(ts)
    if signal_ts is None or signal_ts <= 0:
        return None
    next_ts = signal_ts + 60
    cur = conn.execute(
        """SELECT ts, open FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts=?
           LIMIT 1""",
        (venue, symbol, next_ts),
    )
    r = cur.fetchone()
    if not r:
        return None
    return int(r["ts"]), float(r["open"])


def _get_open_at_exact(conn, venue: str, symbol: str, ts: int) -> float | None:
    """Return the open at the exact requested 1m horizon boundary."""
    target_ts = strict_integer(ts)
    if target_ts is None or target_ts <= 0:
        return None
    cur = conn.execute(
        """SELECT open FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts=?
           LIMIT 1""",
        (venue, symbol, target_ts),
    )
    r = cur.fetchone()
    return float(r["open"]) if r else None


def _has_complete_1m_window(conn, venue: str, symbol: str, ts_start: int, ts_end_exclusive: int) -> bool:
    start = strict_integer(ts_start)
    end = strict_integer(ts_end_exclusive)
    if start is None or end is None or start <= 0 or end <= start or (end - start) % 60 != 0:
        return False
    rows = _iter_1m_candles(conn, venue, symbol, start, end)
    expected_count = (end - start) // 60
    if len(rows) != expected_count:
        return False
    for index, row in enumerate(rows):
        row_ts = strict_integer(row["ts"])
        if row_ts != start + index * 60:
            return False
    return True


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
    if isinstance(value, bool):
        return float(default)
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
    if isinstance(value, bool):
        return None
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
    parsed = strict_integer(value)
    num = int(default) if parsed is None else int(parsed)
    if minimum is not None:
        num = max(int(minimum), num)
    if maximum is not None:
        num = min(int(maximum), num)
    return int(num)


def _extract_cost_components(params: dict | None, fallback_execution_bps: float = 15.0) -> tuple[float, float]:
    # Execution friction cannot be negative. A poisoned/manual payload with a
    # negative explicit cost would otherwise turn costs into alpha and create
    # optimistic outcome labels. Invalid fallbacks also revert to the canonical
    # conservative default rather than silently becoming zero.
    fallback_bps = _finite_or_default(fallback_execution_bps, 15.0)
    if fallback_bps < 0.0:
        fallback_bps = 15.0

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
                            candidate = _finite_or_default(block.get(key), fallback_bps)
                            execution_bps = candidate if candidate >= 0.0 else fallback_bps
                            break
                        except Exception:
                            logger.debug("cost block parse error", exc_info=True)

            if net_cost_bps is None and block.get("net_cost_bps") is not None:
                try:
                    candidate = _finite_or_default(block.get("net_cost_bps"), fallback_bps)
                    net_cost_bps = candidate if candidate >= 0.0 else fallback_bps
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

    execution_bps_out = _finite_or_default(execution_bps if execution_bps is not None else fallback_bps, fallback_bps)
    if execution_bps_out < 0.0:
        execution_bps_out = fallback_bps
    return float(execution_bps_out), float(funding_bps_out)


def _extract_total_cost_bps(params: dict | None, fallback: float = 15.0) -> float:
    execution_bps, _ = _extract_cost_components(params, fallback_execution_bps=fallback)
    return float(execution_bps)


def _funding_cost_bps_for_outcome_label(signed_funding_bps: float) -> float:
    """Conservative funding charge for historical grid outcome labels.

    Recommendation and execution approval deliberately exclude funding receipts
    from canonical edge because the rate can flip and neutral grids may build the
    adverse inventory side. Outcome labels feed calibration/ranking, so they must
    use the same conservative convention: funding that the setup is expected to
    pay is charged, while a possible receipt is retained only as diagnostics in
    the stored payload and must not inflate win-rate/expectancy.
    """
    return max(0.0, _finite_or_default(signed_funding_bps, 0.0))


def _signed_return(entry: float, exitp: float, direction: str) -> float:
    if not entry:
        return 0.0
    direction_norm = normalize_execution_direction(direction)
    raw = (float(exitp) - float(entry)) / float(entry)
    if direction_norm == "short":
        return -raw
    if direction_norm == "long":
        return raw
    return 0.0


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
        return float(fallback_step_abs)
    return None


def _grid_tp_hit(min_p: float, max_p: float, entry: float, direction: str, tp_leg_abs: float | None) -> bool:
    if not entry or tp_leg_abs is None or tp_leg_abs <= 0.0:
        return False
    direction_norm = normalize_execution_direction(direction)
    long_tp = float(entry) + float(tp_leg_abs)
    short_tp = float(entry) - float(tp_leg_abs)
    if direction_norm == "short":
        return min_p <= short_tp
    if direction_norm == "long":
        return max_p >= long_tp
    # Neutral grids and invalid directions must not shortcut to success on a
    # one-sided barrier touch.
    return False


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
    """Estimate arithmetic Futures Grid total P&L from an explicit order ledger.

    The previous proxy counted paired index movement and then applied one coarse
    end-of-horizon drift to a guessed inventory fraction. That loses the actual
    prices at which directional initial positions are closed or neutral inventory
    is accumulated. It also widened the historical grid step when costs were high,
    so the label no longer represented the persisted recommendation geometry.

    This model keeps one equal-quantity slot per grid interval, creates the same
    initial Long/Short/Neutral order layout documented by Bybit, processes only
    close-to-close grid-line crossings (conservative with 1m OHLCV), charges half
    of the stored round-trip execution cost on every executed leg, and marks the
    remaining one-way position at the horizon exit price.
    """
    params = params if isinstance(params, dict) else {}
    direction_norm = normalize_execution_direction(direction)
    if direction_norm not in {"neutral", "long", "short"}:
        return 0, 0.0

    execution_cost_bps, _ = _extract_cost_components(params)
    half_leg_cost_rate = max(0.0, float(execution_cost_bps)) / 20_000.0

    trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    params_sizing = params.get("sizing") if isinstance(params.get("sizing"), dict) else {}
    params_economics = params.get("economics") if isinstance(params.get("economics"), dict) else {}
    plan_sizing = trade_plan.get("sizing") if isinstance(trade_plan.get("sizing"), dict) else {}
    plan_economics = trade_plan.get("economics") if isinstance(trade_plan.get("economics"), dict) else {}
    grid_count_resolution = resolve_integer_aliases([
        ("params.grid_count", params.get("grid_count")),
        ("params.trade_plan.grid_count", trade_plan.get("grid_count")),
        ("params.grid_levels", params.get("grid_levels")),
        ("params.sizing.grid_count", params_sizing.get("grid_count")),
        ("params.economics.grid_count", params_economics.get("grid_count")),
        ("params.trade_plan.sizing.grid_count", plan_sizing.get("grid_count")),
        ("params.trade_plan.economics.grid_count", plan_economics.get("grid_count")),
    ])
    # Strict generated payloads agree. For legacy conflicting aliases retain the
    # previous conservative lower-cap policy, but never synthesize a larger grid.
    grid_count_raw = (
        grid_count_resolution.get("value")
        if grid_count_resolution.get("ok")
        else grid_count_resolution.get("conservative_min")
    )
    grid_count = _int_from_params(grid_count_raw, 0, minimum=0, maximum=1000)

    levels = trade_plan.get("levels") if isinstance(trade_plan.get("levels"), dict) else {}
    range_block = levels.get("range") if isinstance(levels.get("range"), dict) else {}
    kill_switch = levels.get("kill_switch") if isinstance(levels.get("kill_switch"), dict) else {}

    lower = _finite_positive_or_none(params.get("price_range_lower"))
    upper = _finite_positive_or_none(params.get("price_range_upper"))
    if lower is None:
        lower = _finite_positive_or_none(range_block.get("lower"))
    if upper is None:
        upper = _finite_positive_or_none(range_block.get("upper"))
    ks_lower = _finite_positive_or_none(kill_switch.get("lower")) if kill_switch else None
    ks_upper = _finite_positive_or_none(kill_switch.get("upper")) if kill_switch else None

    rows = _iter_1m_candles(conn, venue, symbol, ts_start, ts_end)
    if (
        not rows
        or not math.isfinite(float(entry))
        or not math.isfinite(float(exitp))
        or float(entry) <= 0.0
        or float(exitp) <= 0.0
        or lower is None
        or upper is None
        or float(upper) <= float(lower)
        or grid_count <= 0
        or not (float(lower) <= float(entry) <= float(upper))
    ):
        return 0, 0.0

    lower_f = float(lower)
    upper_f = float(upper)
    entry_f = float(entry)
    exit_f = float(exitp)
    step_abs = (upper_f - lower_f) / float(grid_count)
    if not math.isfinite(step_abs) or step_abs <= 0.0:
        return 0, 0.0
    grid_prices = [lower_f + step_abs * index for index in range(grid_count + 1)]

    position_on_grid = (entry_f - lower_f) / step_abs
    nearest_index = int(round(position_on_grid))
    exact_grid_line = abs(position_on_grid - nearest_index) <= 1e-9
    if exact_grid_line:
        pivot_index = max(0, min(grid_count, nearest_index))
        buy_indices = set(range(0, pivot_index))
        sell_indices = set(range(pivot_index + 1, grid_count + 1))
        if direction_norm == "long":
            initial_long_slots = grid_count - pivot_index
            initial_short_slots = 0
        elif direction_norm == "short":
            initial_long_slots = 0
            initial_short_slots = pivot_index
        else:
            initial_long_slots = initial_short_slots = 0
    else:
        cell_index = max(0, min(grid_count - 1, int(math.floor(position_on_grid))))
        if direction_norm == "neutral":
            buy_indices = set(range(0, cell_index + 1))
            sell_indices = set(range(cell_index + 1, grid_count + 1))
            initial_long_slots = initial_short_slots = 0
        elif direction_norm == "long":
            buy_indices = set(range(0, cell_index + 1))
            sell_indices = set(range(cell_index + 2, grid_count + 1))
            initial_long_slots = grid_count - cell_index - 1
            initial_short_slots = 0
        else:
            buy_indices = set(range(0, cell_index))
            sell_indices = set(range(cell_index + 1, grid_count + 1))
            initial_long_slots = 0
            initial_short_slots = cell_index

    orders: dict[int, str] = {index: "buy" for index in buy_indices}
    orders.update({index: "sell" for index in sell_indices})
    long_lots = ["initial"] * int(initial_long_slots)
    short_lots = ["initial"] * int(initial_short_slots)

    # Cash plus marked position gives gross P&L for equal base-asset quantity
    # slots. Quantity is normalized to one; denominator restores return on the
    # total reference notional committed to grid_count equal slots.
    cash = (-float(initial_long_slots) + float(initial_short_slots)) * entry_f
    execution_cost = float(initial_long_slots + initial_short_slots) * entry_f * half_leg_cost_rate
    completed_grid_pairs = 0
    executed_grid_legs = 0
    initial_position_closures = 0
    previous_price = entry_f
    tolerance = max(1e-10, step_abs * 1e-10)

    for row in rows:
        close_price = _finite_positive_or_none(row["close"])
        if close_price is None:
            continue
        current_price = float(close_price)
        if current_price > previous_price + tolerance:
            crossed = [
                index
                for index, grid_price in enumerate(grid_prices)
                if grid_price > previous_price + tolerance and grid_price <= current_price + tolerance
            ]
        elif current_price < previous_price - tolerance:
            crossed = [
                index
                for index in range(grid_count, -1, -1)
                if grid_prices[index] < previous_price - tolerance
                and grid_prices[index] >= current_price - tolerance
            ]
        else:
            crossed = []

        for index in crossed:
            side = orders.pop(index, None)
            if side is None:
                continue
            fill_price = grid_prices[index]
            executed_grid_legs += 1
            execution_cost += fill_price * half_leg_cost_rate

            if side == "buy":
                cash -= fill_price
                if short_lots:
                    origin = short_lots.pop()
                    if origin == "grid":
                        completed_grid_pairs += 1
                    else:
                        initial_position_closures += 1
                else:
                    long_lots.append("grid")
                if index + 1 <= grid_count:
                    orders.setdefault(index + 1, "sell")
            else:
                cash += fill_price
                if long_lots:
                    origin = long_lots.pop()
                    if origin == "grid":
                        completed_grid_pairs += 1
                    else:
                        initial_position_closures += 1
                else:
                    short_lots.append("grid")
                if index - 1 >= 0:
                    orders.setdefault(index - 1, "buy")
        previous_price = current_price

    open_position_slots = len(long_lots) - len(short_lots)
    gross_pnl = cash + float(open_position_slots) * exit_f
    capital_reference = entry_f * float(grid_count)
    if capital_reference <= 0.0:
        return 0, 0.0
    net_proxy = (gross_pnl - execution_cost) / capital_reference

    min_low = min(float(row["low"]) for row in rows)
    max_high = max(float(row["high"]) for row in rows)
    kill_switch_breached = bool(
        (ks_lower is not None and min_low <= float(ks_lower))
        or (ks_upper is not None and max_high >= float(ks_upper))
    )

    # The label represents total bot P&L, not only repeated oscillations. One
    # completed neutral pair is already a valid grid profit; Long/Short modes can
    # also succeed by closing their initial directional position as intended.
    material_profit_floor = 0.0005
    if direction_norm == "neutral":
        has_mode_activity = completed_grid_pairs >= 1
    else:
        has_mode_activity = bool(
            initial_position_closures >= 1
            or completed_grid_pairs >= 1
            or executed_grid_legs >= 1
            or abs(exit_f - entry_f) / entry_f >= 0.001
        )
    success = int(
        has_mode_activity
        and not kill_switch_breached
        and math.isfinite(net_proxy)
        and net_proxy > material_profit_floor
    )
    return success, float(net_proxy)


def compute_outcomes_once(conn, horizon_sec: int = HORIZON_SEC_DEFAULT, max_to_process: int = 500) -> int:
    min_horizon = min(BOT_HORIZONS.values())
    fetch_limit = max(int(max_to_process), min(2000, int(max_to_process) * 12))
    require_llm_verdict = bool(getattr(settings, "llm_reviewer_enabled", False))

    base_sql = """SELECT r.rec_id, r.ts, r.venue, r.symbol, r.bot_type, r.direction,
                  r.params_json, r.features_ref_ts, r.status, r.reasons_json
           FROM recommendations r
           LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
           WHERE r.ts <= ? AND o.rec_id IS NULL
           AND COALESCE(r.is_outcome_label_root, 1) = 1
           AND r.status NOT IN ('blocked', 'suppressed', 'pending')
           AND (
               r.status <> 'no_trade'
               OR LOWER(CAST(COALESCE(json_extract(r.reasons_json, '$.outcome_policy.eligible'), 'false') AS TEXT)) IN ('1', 'true')
           )"""
    params: list[object] = [db.now_ts() - min_horizon]

    if require_llm_verdict:
        # Фильтруем outcome-eligible строки сразу в SQL, до ORDER BY/LIMIT.
        # Иначе oldest-first окно навсегда забивается legacy/root рекомендациями
        # без финального llm_review.status='ok', и worker бесконечно перечитывает
        # один и тот же хвост вместо продвижения к более новым созревшим rec.
        base_sql += """
           AND LOWER(COALESCE(json_extract(r.reasons_json, '$.llm_review.status'), '')) = 'ok'"""

    base_sql += """
           ORDER BY r.ts ASC LIMIT ?"""
    params.append(fetch_limit)

    cur = conn.execute(base_sql, tuple(params))
    rows = cur.fetchall()
    done = 0

    for r in rows:
        if done >= int(max_to_process):
            break
        if require_llm_verdict and not db.is_outcome_eligible_under_llm_mode(r["status"], r["reasons_json"]):
            continue
        if str(r["status"] or "").strip().lower() == "no_trade" and not _shadow_no_trade_outcome_eligible(
            r["status"], r["reasons_json"]
        ):
            continue
        rec_id = r["rec_id"]
        bot_type = r["bot_type"]
        if bot_type not in SUPPORTED_BOT_TYPES:
            db.log_decision(conn, "OUTCOME_SKIP_UNSUPPORTED_BOT_TYPE", rec_id, None, {"bot_type": bot_type})
            continue
        venue = r["venue"]
        symbol = r["symbol"]
        direction = normalize_execution_direction(r["direction"])
        ts0 = strict_integer(r["ts"])
        signal_ref_ts = strict_integer(r["features_ref_ts"])
        if ts0 is None or ts0 <= 0 or signal_ref_ts is None or signal_ref_ts <= 0:
            db.log_decision(conn, "OUTCOME_SKIP_INVALID_TEMPORAL_FIELDS", rec_id, None, {
                "bot_type": bot_type,
                "venue": venue,
                "symbol": symbol,
                "recommendation_ts": r["ts"],
                "features_ref_ts": r["features_ref_ts"],
            })
            continue

        if direction is None or not _is_supported_direction(bot_type, venue, direction):
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
        if not _has_complete_1m_window(conn, venue, symbol, entry_ts, ts_exit):
            db.log_decision(conn, "OUTCOME_SKIP_INCOMPLETE_HORIZON", rec_id, None, {
                "venue": venue,
                "symbol": symbol,
                "entry_ts": entry_ts,
                "label_available_ts": ts_exit,
                "horizon_sec": effective_horizon,
            })
            continue

        if entry is None or entry == 0:
            continue
        ret_proxy: float
        success: int
        exitp: float

        if bot_type in GRID_BOTS:
            ep = _get_open_at_exact(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp = ep
            success, ret_proxy = _grid_outcome(conn, venue, symbol, entry, exitp, entry_ts, ts_exit, direction, params)
            _, funding_cost_bps = _extract_cost_components(params)
            if venue == "linear":
                conservative_funding_cost_bps = _funding_cost_bps_for_outcome_label(funding_cost_bps)
                if conservative_funding_cost_bps:
                    ret_proxy -= conservative_funding_cost_bps / 10_000.0
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
                "label_available_ts": int(ts_exit),
                "entry_close": float(entry),
                "exit_close": float(exitp),
                "ret": float(ret_proxy),
                "success": int(success),
            },
        )
        done += 1

    return done
