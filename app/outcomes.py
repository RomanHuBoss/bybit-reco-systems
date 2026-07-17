from __future__ import annotations

from . import db
from .bot_types import GRID_BOT_TYPES, SUPPORTED_BOT_TYPES
from .grid_math import arithmetic_grid_commitment, resolve_integer_aliases, strict_integer
from .settings import load_settings
from .trading_semantics import normalize_execution_direction
import logging
import math
import time
from typing import Callable
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)
settings = load_settings()

BOT_HORIZONS: dict[str, int] = {
    "futures_grid": 12 * 3600,
}
HORIZON_SEC_DEFAULT = 30 * 60
OUTCOME_MAX_ROWS_EXAMINED_PER_CYCLE = 2000

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


def _get_first_tradeable_candle_after(
    conn,
    venue: str,
    symbol: str,
    ts: int,
    publication_ts: int | None = None,
) -> tuple[int, float] | None:
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

    # The next candle after the feature bar is tradeable only when it opens after
    # the recommendation has actually been published. A delayed recommender cycle
    # can finish seconds or minutes after that candle opened; using its historical
    # open would create an impossible pre-publication fill and temporal leakage.
    published = strict_integer(publication_ts) if publication_ts is not None else None
    if publication_ts is not None and (published is None or published <= 0):
        return None
    if published is not None and published >= next_ts:
        next_ts += ((published - next_ts) // 60 + 1) * 60

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


def _get_candle_at_exact(conn, venue: str, symbol: str, ts: int):
    """Return one valid exact 1m candle row or ``None``."""
    target_ts = strict_integer(ts)
    if target_ts is None or target_ts <= 0:
        return None
    cur = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts=?
           LIMIT 1""",
        (venue, symbol, target_ts),
    )
    row = cur.fetchone()
    return row if row is not None and _is_valid_outcome_candle(row) else None


def _get_open_at_exact(conn, venue: str, symbol: str, ts: int) -> float | None:
    """Return the open at the exact requested 1m horizon boundary."""
    row = _get_candle_at_exact(conn, venue, symbol, ts)
    return float(row["open"]) if row is not None else None


def _is_valid_outcome_candle(row: object) -> bool:
    try:
        raw_ts = row["ts"]  # type: ignore[index]
        raw_values = [row[key] for key in ("open", "high", "low", "close")]  # type: ignore[index]
        raw_volume = row["volume"]  # type: ignore[index]
    except Exception:
        return False
    if (
        isinstance(raw_ts, bool)
        or isinstance(raw_volume, bool)
        or any(isinstance(value, bool) for value in raw_values)
    ):
        return False
    candle_ts = strict_integer(raw_ts)
    if candle_ts is None or candle_ts <= 0:
        return False
    try:
        open_px, high_px, low_px, close_px = (float(value) for value in raw_values)
    except Exception:
        return False
    values = (open_px, high_px, low_px, close_px)
    try:
        volume = float(raw_volume)
    except Exception:
        return False
    if not all(math.isfinite(value) and value > 0.0 for value in values):
        return False
    if not math.isfinite(volume) or volume < 0.0:
        return False
    if high_px < max(open_px, close_px, low_px):
        return False
    if low_px > min(open_px, close_px, high_px):
        return False
    return True


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
        if row_ts != start + index * 60 or not _is_valid_outcome_candle(row):
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
        """SELECT ts, open, high, low, close, volume FROM ohlcv
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
    # Execution friction cannot be negative. Duplicate cost_model blocks are
    # aliases of the same generated contract, so a lower/zero/malformed copy must
    # not hide a stricter valid value from another block. Resolve all candidates
    # conservatively instead of accepting the first mapping by precedence.
    fallback_bps = _finite_or_default(fallback_execution_bps, 15.0)
    if fallback_bps < 0.0:
        fallback_bps = 15.0

    def _candidate(value: object, *, signed: bool = False) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except Exception:
            return None
        if not math.isfinite(number) or (not signed and number < 0.0):
            return None
        return float(number)

    execution_candidates: list[float] = []
    net_cost_candidates: list[float] = []
    funding_candidates: list[float] = []
    if isinstance(params, dict):
        trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
        for block in (params.get("cost_model"), trade_plan.get("cost_model")):
            if not isinstance(block, dict):
                continue
            for key in ("execution_cost_bps", "total_cost_bps"):
                candidate = _candidate(block.get(key))
                if candidate is not None:
                    execution_candidates.append(candidate)
            candidate = _candidate(block.get("net_cost_bps"))
            if candidate is not None:
                net_cost_candidates.append(candidate)
            candidate = _candidate(block.get("expected_funding_bps"), signed=True)
            if candidate is not None:
                funding_candidates.append(candidate)

    # Max is fail-closed for duplicated execution/net-cost aliases. For signed
    # funding it chooses the most adverse valid value; if every value is negative,
    # it chooses the smallest possible receipt rather than manufacturing alpha.
    execution_bps = max(execution_candidates) if execution_candidates else None
    net_cost_bps = max(net_cost_candidates) if net_cost_candidates else None
    funding_bps_out = max(funding_candidates) if funding_candidates else 0.0

    if execution_bps is None and net_cost_bps is not None:
        # Legacy/manual payload may contain only net_cost_bps. Outcome labeling
        # accounts for execution and funding separately, so decompose signed net
        # cost rather than subtracting funding twice.
        execution_bps = max(0.0, float(net_cost_bps) - float(funding_bps_out))

    execution_bps_out = float(execution_bps) if execution_bps is not None else float(fallback_bps)
    return float(execution_bps_out), float(funding_bps_out)




def _extract_grid_round_trip_fee_bps(params: dict | None, fallback_market_bps: float) -> float:
    """Resolve recurring fee for one completed resting grid pair.

    Generated payloads expose ``grid_round_trip_fee_bps`` and the legacy
    ``fee_bps_round_trip`` alias. Spread/slippage are market-entry/exit friction
    and must not be charged to each resting limit pair. Old payloads without a
    fee field retain the conservative market-cost fallback.
    """
    params = params if isinstance(params, dict) else {}
    trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    candidates: list[float] = []
    for block in (params.get("cost_model"), trade_plan.get("cost_model")):
        if not isinstance(block, dict):
            continue
        for key in ("grid_round_trip_fee_bps", "fee_bps_round_trip"):
            raw = block.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            try:
                value = float(raw)
            except Exception:
                continue
            if math.isfinite(value) and value >= 0.0:
                candidates.append(value)
    return max(candidates) if candidates else max(0.0, float(fallback_market_bps))

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


def _extract_inventory_funding_model(params: dict | None) -> dict[str, object]:
    """Return one internally consistent funding model from duplicated payload blocks.

    Generated recommendations persist the same ``cost_model`` both at the top level
    and inside ``trade_plan``. These are aliases of one contract, not independent
    estimates. A first-wins merge can combine a rate from one block with a schedule
    from another or let a malformed/receipt alias hide an adverse valid value. Such
    a synthetic model must not produce a calibration label.
    """
    params = params if isinstance(params, dict) else {}
    trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    blocks = (
        ("params.cost_model", params.get("cost_model") if isinstance(params.get("cost_model"), dict) else {}),
        (
            "params.trade_plan.cost_model",
            trade_plan.get("cost_model") if isinstance(trade_plan.get("cost_model"), dict) else {},
        ),
    )

    fields = (
        "funding_rate",
        "next_funding_ts",
        "funding_interval_min",
        "expected_funding_events",
        "directional_funding_bps_per_event",
        "expected_funding_bps",
    )

    def _parse(field: str, value: object) -> object | None:
        if value is None or isinstance(value, bool):
            return None
        if field == "next_funding_ts":
            parsed = strict_integer(value)
            if parsed is None or parsed <= 0:
                return None
            if parsed > 100_000_000_000:
                if parsed % 1000 != 0:
                    return None
                parsed //= 1000
            return int(parsed) if parsed > 0 else None
        if field == "funding_interval_min":
            parsed = strict_integer(value)
            return int(parsed * 60) if parsed is not None and parsed > 0 else None
        if field == "expected_funding_events":
            parsed = strict_integer(value)
            return int(parsed) if parsed is not None and 0 <= parsed <= 1000 else None
        try:
            parsed_float = float(value)
        except Exception:
            return None
        return float(parsed_float) if math.isfinite(parsed_float) else None

    resolved: dict[str, object | None] = {}
    issues: list[dict[str, object]] = []
    for field in fields:
        parsed_values: list[tuple[str, object]] = []
        for source, block in blocks:
            if field not in block or block.get(field) is None:
                continue
            raw = block.get(field)
            parsed = _parse(field, raw)
            if parsed is None:
                issues.append({"field": field, "source": source, "reason": "invalid", "value": raw})
                continue
            parsed_values.append((source, parsed))

        if not parsed_values:
            resolved[field] = None
            continue

        first_value = parsed_values[0][1]
        conflict = False
        for _, candidate in parsed_values[1:]:
            if isinstance(first_value, float) or isinstance(candidate, float):
                if not math.isclose(float(first_value), float(candidate), rel_tol=1e-12, abs_tol=1e-12):
                    conflict = True
                    break
            elif first_value != candidate:
                conflict = True
                break
        if conflict:
            issues.append({
                "field": field,
                "reason": "conflict",
                "values": [{"source": source, "value": value} for source, value in parsed_values],
            })
            resolved[field] = None
        else:
            resolved[field] = first_value

    return {
        "valid": not issues,
        "issues": issues,
        "funding_rate": resolved.get("funding_rate"),
        "next_funding_ts": resolved.get("next_funding_ts"),
        "funding_interval_sec": resolved.get("funding_interval_min"),
        "expected_funding_events": resolved.get("expected_funding_events") or 0,
        "directional_funding_bps_per_event": resolved.get("directional_funding_bps_per_event"),
        "expected_funding_bps": resolved.get("expected_funding_bps"),
    }

def _exact_funding_event_times(model: dict[str, float | int | None], ts_start: int, ts_end: int) -> list[int]:
    next_ts = strict_integer(model.get("next_funding_ts"))
    interval_sec = strict_integer(model.get("funding_interval_sec"))
    if next_ts is None or interval_sec is None or next_ts <= 0 or interval_sec <= 0 or ts_end < ts_start:
        return []
    if next_ts < ts_start:
        jumps = (ts_start - next_ts + interval_sec - 1) // interval_sec
        next_ts += jumps * interval_sec
    events: list[int] = []
    while next_ts <= ts_end and len(events) < 1000:
        events.append(int(next_ts))
        next_ts += interval_sec
    return events


def _fallback_adverse_rate_per_event(model: dict[str, float | int | None]) -> float:
    per_event_raw = model.get("directional_funding_bps_per_event")
    expected_raw = model.get("expected_funding_bps")
    expected_events = _int_from_params(model.get("expected_funding_events"), 0, minimum=0, maximum=1000)
    per_event_bps = max(0.0, float(per_event_raw)) if per_event_raw is not None else 0.0
    if per_event_bps <= 0.0 and expected_raw is not None and expected_events > 0:
        per_event_bps = max(0.0, float(expected_raw)) / float(expected_events)
    return per_event_bps / 10_000.0


def _adverse_funding_cashflow(position_slots: int, price: float, funding_rate: float | None) -> float:
    """Return only adverse funding cashflow; retained for approval-side helpers/tests."""
    if position_slots == 0 or funding_rate is None or not math.isfinite(float(funding_rate)):
        return 0.0
    # Positive rate: longs pay. Negative rate: shorts pay.
    if float(position_slots) * float(funding_rate) <= 0.0:
        return 0.0
    return abs(float(position_slots)) * float(price) * abs(float(funding_rate))


def _signed_settled_funding_pnl(position_slots: int, price: float, funding_rate: float) -> float:
    """Signed historical funding P&L for Linear USDT perpetual inventory.

    Positive funding means longs pay shorts; negative funding means shorts pay
    longs. Historical labels must include both payments and receipts because they
    describe realised Total P&L, not conservative approval edge.
    """
    if position_slots == 0:
        return 0.0
    values = (float(price), float(funding_rate))
    if not all(math.isfinite(value) for value in values) or float(price) <= 0.0:
        return 0.0
    return -float(position_slots) * float(price) * float(funding_rate)


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



def _resolve_grid_range(params: dict, trade_plan: dict) -> tuple[float, float] | None:
    """Resolve duplicated range aliases without silently changing grid geometry.

    A malformed top-level legacy alias may fall back to a valid canonical nested
    range. Two *valid but different* ranges are a contract conflict and therefore
    cannot be labeled: choosing either one would simulate a different bot.
    """
    levels = trade_plan.get("levels") if isinstance(trade_plan.get("levels"), dict) else {}
    nested = levels.get("range") if isinstance(levels.get("range"), dict) else {}
    candidates: list[tuple[str, float, float]] = []

    for source, lower_raw, upper_raw in (
        ("params", params.get("price_range_lower"), params.get("price_range_upper")),
        ("params.trade_plan.levels.range", nested.get("lower"), nested.get("upper")),
    ):
        explicit = lower_raw is not None or upper_raw is not None
        if not explicit:
            continue
        lower = _finite_positive_or_none(lower_raw)
        upper = _finite_positive_or_none(upper_raw)
        if lower is None or upper is None or upper <= lower:
            return None
        candidates.append((source, float(lower), float(upper)))

    if not candidates:
        return None
    _, lower, upper = candidates[0]
    for _, candidate_lower, candidate_upper in candidates[1:]:
        if not (
            math.isclose(lower, candidate_lower, rel_tol=1e-12, abs_tol=1e-12)
            and math.isclose(upper, candidate_upper, rel_tol=1e-12, abs_tol=1e-12)
        ):
            return None
    return float(lower), float(upper)



def _record_outcome_failure(
    diagnostics: dict[str, object] | None,
    reason: str,
    *,
    transient: bool = False,
    **details: object,
) -> None:
    """Store a machine-readable reason for an unavailable outcome.

    ``None`` remains the public return contract for compatibility, but callers can
    now distinguish a permanently invalid recommendation from a transient missing
    dependency such as funding settlement history.
    """
    if diagnostics is None:
        return
    diagnostics.clear()
    diagnostics.update({"reason": str(reason), "transient": bool(transient)})
    diagnostics.update(details)


def _ensure_outcome_failure(
    diagnostics: dict[str, object] | None,
    reason: str,
    *,
    transient: bool = False,
    **details: object,
) -> None:
    """Set a fallback reason only when a more specific helper did not already do so."""
    if diagnostics is None or diagnostics.get("reason"):
        return
    _record_outcome_failure(
        diagnostics,
        reason,
        transient=transient,
        **details,
    )


def _log_outcome_decision_once(
    conn,
    action: str,
    rec_id: str,
    details: dict[str, object],
    *,
    cooldown_sec: int = 3600,
) -> None:
    """Avoid repeating the same unavailable-outcome diagnostic every cycle."""
    cutoff = db.now_ts() - max(1, int(cooldown_sec))
    row = conn.execute(
        """SELECT 1 FROM decision_log
           WHERE action=? AND rec_id=? AND ts>=?
           ORDER BY ts DESC LIMIT 1""",
        (action, rec_id, cutoff),
    ).fetchone()
    if row is None:
        db.log_decision(conn, action, rec_id, None, details)

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
    *,
    diagnostics: dict[str, object] | None = None,
    require_terminal_boundary_candle: bool = False,
) -> tuple[int, float] | None:
    """Estimate arithmetic Futures Grid total P&L from a conservative order ledger.

    Equal-quantity slots reproduce the persisted arithmetic grid. Resting
    quantities are aggregated per level, so an initial directional TP and an
    adjacent replacement TP at the same price remain two executable lots rather
    than being collapsed into one. Observable close->open and open->close
    movements are processed separately. For two-sided OHLC excursions, both
    admissible extreme paths are simulated and a label is kept only when the
    resulting ledgers are equivalent.

    A kill-switch is an actual terminal event: the ledger processes fills up to
    the protective boundary and then liquidates residual inventory at a
    conservative observed-price bound. For inventory harmed by the continued
    breach, the candle extreme is used rather than fabricating a perfect market
    fill at the trigger boundary. Later candles/funding are ignored. A close->open
    gap beyond that boundary is unavailable
    because OHLC cannot identify the stop/grid-order execution chronology or a
    fill at the skipped protective price.
    """
    params = params if isinstance(params, dict) else {}
    direction_norm = normalize_execution_direction(direction)
    if direction_norm not in {"neutral", "long", "short"}:
        _record_outcome_failure(diagnostics, "invalid_direction", direction=direction_norm)
        return None

    market_execution_cost_bps, _ = _extract_cost_components(params)
    grid_round_trip_fee_bps = _extract_grid_round_trip_fee_bps(
        params, fallback_market_bps=market_execution_cost_bps
    )
    market_half_leg_cost_rate = max(0.0, float(market_execution_cost_bps)) / 20_000.0
    grid_half_leg_fee_rate = max(0.0, float(grid_round_trip_fee_bps)) / 20_000.0
    funding_model = _extract_inventory_funding_model(params)
    if funding_model.get("valid") is not True:
        _record_outcome_failure(
            diagnostics,
            "invalid_funding_contract",
            issues=list(funding_model.get("issues") or []),
        )
        return None

    # The recommendation-time ticker fundingRate is a forecast for the next
    # settlement and can change until the funding timestamp. Historical labels
    # therefore use only immutable settled rates collected from Bybit's funding
    # history endpoint. If the persisted schedule says an event belongs to this
    # horizon but the settlement row is missing, the label is unavailable rather
    # than fabricated from the old forecast.
    exact_schedule_known = bool(
        strict_integer(funding_model.get("next_funding_ts")) is not None
        and strict_integer(funding_model.get("funding_interval_sec")) is not None
    )
    expected_event_times = (
        _exact_funding_event_times(funding_model, ts_start, ts_end)
        if exact_schedule_known
        else []
    )
    settlement_rows = db.get_funding_settlements(conn, symbol, ts_start, ts_end)
    settled_by_ts = {
        int(row["ts"]): float(row["funding_rate"]) for row in settlement_rows
    }
    if exact_schedule_known:
        event_timestamps = sorted(set(expected_event_times) | set(settled_by_ts))
        funding_events: list[tuple[int, float | None]] = [
            (event_ts, settled_by_ts.get(event_ts)) for event_ts in event_timestamps
        ]
    else:
        expected_event_count = _int_from_params(
            funding_model.get("expected_funding_events"), 0, minimum=0, maximum=1000
        )
        if expected_event_count > 0 and not settled_by_ts:
            # Without a confirmed schedule or historical settlement rows, neither
            # event timing nor signed rate is known. A forecast-only charge is not
            # a historical outcome. This is transient while the collector backfills
            # public settlement history and must not be reported as bad geometry.
            _record_outcome_failure(
                diagnostics,
                "funding_settlement_history_unavailable",
                transient=True,
                expected_funding_events=int(expected_event_count),
            )
            return None
        funding_events = sorted(settled_by_ts.items())

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
    if grid_count_resolution.get("ok") is not True:
        _record_outcome_failure(
            diagnostics,
            "invalid_grid_count_contract",
            issues=list(grid_count_resolution.get("issues") or []),
        )
        return None
    grid_count = _int_from_params(
        grid_count_resolution.get("value"), 0, minimum=0, maximum=1000
    )

    levels = trade_plan.get("levels") if isinstance(trade_plan.get("levels"), dict) else {}
    kill_switch = levels.get("kill_switch") if isinstance(levels.get("kill_switch"), dict) else None
    resolved_range = _resolve_grid_range(params, trade_plan)
    lower, upper = resolved_range if resolved_range is not None else (None, None)
    ks_lower = _finite_positive_or_none(kill_switch.get("lower")) if kill_switch else None
    ks_upper = _finite_positive_or_none(kill_switch.get("upper")) if kill_switch else None

    rows = _iter_1m_candles(conn, venue, symbol, ts_start, ts_end)
    if not rows or any(not _is_valid_outcome_candle(row) for row in rows):
        _record_outcome_failure(diagnostics, "invalid_or_missing_ohlcv_window")
        return None
    if (
        not math.isfinite(float(entry))
        or not math.isfinite(float(exitp))
        or float(entry) <= 0.0
        or float(exitp) <= 0.0
    ):
        _record_outcome_failure(
            diagnostics,
            "invalid_entry_or_exit_price",
            entry=entry,
            exit=exitp,
        )
        return None
    if lower is None or upper is None or float(upper) <= float(lower):
        _record_outcome_failure(diagnostics, "invalid_grid_range_contract")
        return None
    if grid_count <= 0:
        _record_outcome_failure(diagnostics, "invalid_grid_count", grid_count=grid_count)
        return None
    if not (float(lower) <= float(entry) <= float(upper)):
        _record_outcome_failure(
            diagnostics,
            "entry_outside_grid_range",
            lower=float(lower),
            upper=float(upper),
            entry=float(entry),
        )
        return None
    if ks_lower is None or ks_upper is None:
        _record_outcome_failure(diagnostics, "missing_kill_switch")
        return None
    if not (float(ks_lower) < float(lower) < float(upper) < float(ks_upper)):
        _record_outcome_failure(
            diagnostics,
            "invalid_kill_switch_geometry",
            kill_switch_lower=ks_lower,
            grid_lower=lower,
            grid_upper=upper,
            kill_switch_upper=ks_upper,
        )
        return None

    lower_f = float(lower)
    upper_f = float(upper)
    entry_f = float(entry)
    exit_f = float(exitp)
    ks_lower_f = float(ks_lower)
    ks_upper_f = float(ks_upper)
    topology = arithmetic_grid_commitment(
        lower=lower_f,
        upper=upper_f,
        grid_count=grid_count,
        reference_price=entry_f,
        direction=direction_norm,
    )
    if topology is None:
        _record_outcome_failure(diagnostics, "invalid_grid_topology")
        return None
    step_abs = float(topology["step_abs"])
    grid_prices = [float(value) for value in topology["grid_prices"]]
    buy_indices = set(int(value) for value in topology["buy_indices"])
    sell_indices = set(int(value) for value in topology["sell_indices"])
    initial_long_slots = int(topology["initial_long_slots"])
    initial_short_slots = int(topology["initial_short_slots"])

    # OHLC trade-through proves that the market traded beyond a resting limit,
    # but it still cannot prove a full fill when the requested base quantity is
    # larger than the entire observed minute volume.  For Linear USDT klines,
    # volume is expressed in the same base/contract quantity as order qty.  The
    # aggregate candle volume is only a necessary (not sufficient) capacity
    # bound, yet ignoring even that bound fabricates fills that are
    # mathematically impossible. Legacy/manual payloads without a usable qty keep
    # the older proxy behaviour; current exchange-normalized recommendations
    # always persist qty and are therefore protected by this cap.
    order_qty_decimal = _first_positive_qty(params)
    order_qty_per_slot = (
        float(order_qty_decimal) if order_qty_decimal is not None else None
    )

    # Signed quantity per price level: positive = buy quantity, negative = sell
    # quantity. Directional grids can legitimately accumulate multiple orders at
    # the same price (for example, the existing TP for initial inventory plus a
    # replacement TP created by a newly filled adjacent buy). A side-only map
    # silently collapsed those quantities and corrupted realised PnL, fees and
    # funding inventory.
    orders: dict[int, int] = {index: 1 for index in buy_indices}
    orders.update({index: -1 for index in sell_indices})
    # Replacement orders are created only after the parent fill is observed.
    # One-minute OHLCV does not expose the parent fill timestamp or the moment
    # when the bot submitted the replacement.  Keep those orders pending until
    # the next candle.  If the current candle would already cross a pending
    # replacement, both "filled" and "not yet placed" executions are admissible,
    # so the proxy label must be unavailable rather than assuming zero latency.
    pending_orders: dict[int, int] = {}
    position_slots = int(topology["initial_position_slots"])

    cash = (-float(initial_long_slots) + float(initial_short_slots)) * entry_f
    execution_cost = (
        float(initial_long_slots + initial_short_slots)
        * entry_f
        * market_half_leg_cost_rate
    )
    previous_price = entry_f
    tolerance = max(1e-10, step_abs * 1e-10)
    signed_funding_pnl = 0.0
    conservative_funding_pnl = 0.0
    event_index = 0
    stopped = False
    stop_price: float | None = None
    stop_boundary_price: float | None = None
    stop_observed_extreme: float | None = None
    stop_ts: int | None = None
    ledger_invalid = False
    candle_volume_capacity_qty: float | None = None
    candle_volume_used_qty = 0.0

    def current_position_slots() -> int:
        return int(position_slots)

    def apply_funding_event(settled_rate: float | None, price: float, event_ts: int | None = None) -> None:
        nonlocal signed_funding_pnl, conservative_funding_pnl, ledger_invalid
        slots = current_position_slots()
        if settled_rate is None:
            # Missing settled rate is harmless only when no position was held at
            # the funding timestamp. Otherwise the historical P&L is unknowable.
            if slots != 0:
                _record_outcome_failure(
                    diagnostics,
                    "missing_funding_settlement",
                    transient=True,
                    missing_funding_ts=int(event_ts) if event_ts is not None else None,
                    position_slots=int(slots),
                )
                ledger_invalid = True
            return
        signed_cashflow = _signed_settled_funding_pnl(slots, price, float(settled_rate))
        signed_funding_pnl += signed_cashflow
        # Historical settled funding is retained as a diagnostic truth, but a
        # temporary receipt must not become canonical strategy alpha.  Outcome
        # ``ret`` feeds monetary calibration and publication gates, so it charges
        # every adverse settlement while excluding positive receipts.  Otherwise
        # a flat/losing grid can be labelled successful solely because one funding
        # snapshot happened to pay its current inventory side.
        conservative_funding_pnl += min(0.0, signed_cashflow)

    def adverse_exposure_value(price: float) -> float:
        # Retained only as a path-equivalence state component. Historical funding
        # P&L itself is computed from exact settlement rows below.
        return abs(float(current_position_slots())) * float(price)

    max_adverse_position_value = adverse_exposure_value(entry_f)

    def consume_observed_candle_volume(
        slot_quantity: int,
        *,
        reason: str,
        event_ts: int,
        fill_price: float,
    ) -> bool:
        nonlocal candle_volume_used_qty, ledger_invalid
        if order_qty_per_slot is None:
            return True
        required_slots = abs(int(slot_quantity))
        required_fill_qty = float(required_slots) * float(order_qty_per_slot)
        if required_fill_qty <= 0.0:
            return True
        capacity = candle_volume_capacity_qty
        if capacity is None or not math.isfinite(float(capacity)) or float(capacity) < 0.0:
            _record_outcome_failure(
                diagnostics,
                "invalid_candle_volume_capacity",
                event_ts=int(event_ts),
                fill_price=float(fill_price),
                candle_volume=capacity,
            )
            ledger_invalid = True
            return False
        tolerance_qty = max(
            1e-12,
            abs(float(capacity)) * 1e-12,
            abs(float(order_qty_per_slot)) * 1e-9,
        )
        used_before = float(candle_volume_used_qty)
        if used_before + required_fill_qty > float(capacity) + tolerance_qty:
            _record_outcome_failure(
                diagnostics,
                reason,
                event_ts=int(event_ts),
                fill_price=float(fill_price),
                candle_volume=float(capacity),
                volume_used_before_fill=used_before,
                required_fill_qty=float(required_fill_qty),
                qty_per_order=float(order_qty_per_slot),
                required_slot_count=int(required_slots),
            )
            ledger_invalid = True
            return False
        candle_volume_used_qty = used_before + required_fill_qty
        return True

    def add_order(index: int, signed_quantity: int) -> None:
        nonlocal ledger_invalid
        if signed_quantity == 0 or index < 0 or index > grid_count:
            return
        active_quantity = int(orders.get(index, 0))
        pending_quantity = int(pending_orders.get(index, 0))
        for existing in (active_quantity, pending_quantity):
            if existing != 0 and (existing > 0) != (signed_quantity > 0):
                # Opposing resting orders at one level would imply self-trading
                # or an unresolved cancel/replace chronology. Do not fabricate a
                # label.
                _record_outcome_failure(
                    diagnostics,
                    "conflicting_active_pending_orders",
                    grid_index=int(index),
                    active_signed_slot_quantity=int(active_quantity),
                    pending_signed_slot_quantity=int(pending_quantity),
                    new_signed_slot_quantity=int(signed_quantity),
                )
                ledger_invalid = True
                return
        combined = pending_quantity + int(signed_quantity)
        if combined == 0:
            pending_orders.pop(index, None)
        else:
            pending_orders[index] = combined

    def activate_pending_orders() -> None:
        nonlocal ledger_invalid
        if not pending_orders:
            return
        for index, signed_quantity in list(pending_orders.items()):
            existing = int(orders.get(index, 0))
            if existing != 0 and (existing > 0) != (signed_quantity > 0):
                _record_outcome_failure(
                    diagnostics,
                    "conflicting_pending_activation_orders",
                    grid_index=int(index),
                    active_signed_slot_quantity=int(existing),
                    pending_signed_slot_quantity=int(signed_quantity),
                )
                ledger_invalid = True
                return
            combined = existing + int(signed_quantity)
            if combined == 0:
                orders.pop(index, None)
            else:
                orders[index] = combined
        pending_orders.clear()

    def gap_crosses_kill_switch(target_price: float) -> bool:
        target = float(target_price)
        start = float(previous_price)
        return bool(
            (target > ks_upper_f + tolerance and start < ks_upper_f - tolerance)
            or (target < ks_lower_f - tolerance and start > ks_lower_f + tolerance)
        )

    def process_segment(target_price: float, *, event_ts: int) -> None:
        """Process every resting grid order crossed by one observable price segment."""
        nonlocal cash, execution_cost, position_slots, previous_price
        nonlocal max_adverse_position_value, stopped, stop_price, stop_boundary_price
        nonlocal stop_observed_extreme, stop_ts, ledger_invalid
        if stopped:
            return
        target = float(target_price)
        start = float(previous_price)
        if not math.isfinite(target) or target <= 0.0:
            return

        terminal = target
        breached = False
        if target > start + tolerance and start < ks_upper_f <= target + tolerance:
            terminal = ks_upper_f
            breached = True
        elif target < start - tolerance and target - tolerance <= ks_lower_f < start:
            terminal = ks_lower_f
            breached = True

        def crossed_indices(order_map: dict[int, int]) -> list[int]:
            if terminal > start + tolerance:
                # A resting Sell is not proven filled merely because OHLC
                # ``high`` equals its limit price. The market must trade beyond
                # the level. Include an order at ``start`` so a prior exact touch
                # can be confirmed by later continuation above the same price.
                return sorted(
                    index
                    for index, signed_quantity in order_map.items()
                    if int(signed_quantity) < 0
                    and grid_prices[index] >= start - tolerance
                    and grid_prices[index] < terminal - tolerance
                )
            if terminal < start - tolerance:
                # Symmetric rule for a resting Buy.
                return sorted(
                    (
                        index
                        for index, signed_quantity in order_map.items()
                        if int(signed_quantity) > 0
                        and grid_prices[index] <= start + tolerance
                        and grid_prices[index] > terminal + tolerance
                    ),
                    reverse=True,
                )
            return []

        crossed_pending = crossed_indices(pending_orders)
        if crossed_pending:
            index = int(crossed_pending[0])
            _record_outcome_failure(
                diagnostics,
                "intrabar_replacement_fill_timing_unobservable",
                event_ts=int(event_ts),
                fill_price=float(grid_prices[index]),
                signed_slot_quantity=int(pending_orders.get(index, 0)),
            )
            ledger_invalid = True
            return

        crossed = crossed_indices(orders)

        for index in crossed:
            signed_quantity = int(orders.get(index, 0))
            if signed_quantity == 0:
                continue
            fill_price = grid_prices[index]
            quantity = abs(signed_quantity)
            if not consume_observed_candle_volume(
                quantity,
                reason="insufficient_candle_volume_for_full_fill",
                event_ts=event_ts,
                fill_price=fill_price,
            ):
                return
            orders.pop(index, None)
            execution_cost += float(quantity) * fill_price * grid_half_leg_fee_rate
            if signed_quantity > 0:
                cash -= float(quantity) * fill_price
                position_slots += quantity
                if index + 1 <= grid_count:
                    add_order(index + 1, -quantity)
            else:
                cash += float(quantity) * fill_price
                position_slots -= quantity
                if index - 1 >= 0:
                    add_order(index - 1, quantity)
            if ledger_invalid:
                return

        previous_price = float(terminal)
        max_adverse_position_value = max(
            max_adverse_position_value,
            adverse_exposure_value(float(terminal)),
        )
        if breached:
            stopped = True
            stop_boundary_price = float(terminal)
            stop_observed_extreme = float(target)
            residual_slots = current_position_slots()
            liquidation_bound = float(terminal)
            if target > start + tolerance and residual_slots < 0:
                # A short is harmed by continued upside after the upper stop
                # trigger. OHLC proves trading up to ``target``; pricing a market
                # stop exactly at the boundary systematically understates the
                # observable tail loss.
                liquidation_bound = max(float(terminal), float(target))
            elif target < start - tolerance and residual_slots > 0:
                # Symmetric adverse bound for long inventory below the lower
                # protective trigger.
                liquidation_bound = min(float(terminal), float(target))
            stop_price = float(liquidation_bound)
            if residual_slots != 0 and not consume_observed_candle_volume(
                abs(int(residual_slots)),
                reason="insufficient_candle_volume_for_kill_switch_liquidation",
                event_ts=int(event_ts),
                fill_price=float(liquidation_bound),
            ):
                return
            max_adverse_position_value = max(
                max_adverse_position_value,
                adverse_exposure_value(float(liquidation_bound)),
            )
            stop_ts = int(event_ts)

    def snapshot_ledger() -> dict[str, object]:
        return {
            "cash": float(cash),
            "execution_cost": float(execution_cost),
            "position_slots": int(position_slots),
            "previous_price": float(previous_price),
            "max_adverse_position_value": float(max_adverse_position_value),
            "stopped": bool(stopped),
            "stop_price": None if stop_price is None else float(stop_price),
            "stop_boundary_price": (
                None if stop_boundary_price is None else float(stop_boundary_price)
            ),
            "stop_observed_extreme": (
                None if stop_observed_extreme is None else float(stop_observed_extreme)
            ),
            "stop_ts": stop_ts,
            "ledger_invalid": bool(ledger_invalid),
            "candle_volume_used_qty": float(candle_volume_used_qty),
            "orders": dict(orders),
            "pending_orders": dict(pending_orders),
        }

    def restore_ledger(state: dict[str, object]) -> None:
        nonlocal cash, execution_cost, position_slots, previous_price
        nonlocal max_adverse_position_value, stopped, stop_price, stop_boundary_price
        nonlocal stop_observed_extreme, stop_ts, ledger_invalid, orders
        nonlocal pending_orders, candle_volume_used_qty
        cash = float(state["cash"])
        execution_cost = float(state["execution_cost"])
        position_slots = int(state["position_slots"])
        previous_price = float(state["previous_price"])
        max_adverse_position_value = float(state["max_adverse_position_value"])
        stopped = bool(state["stopped"])
        raw_stop_price = state["stop_price"]
        stop_price = None if raw_stop_price is None else float(raw_stop_price)
        raw_stop_boundary = state["stop_boundary_price"]
        stop_boundary_price = (
            None if raw_stop_boundary is None else float(raw_stop_boundary)
        )
        raw_stop_extreme = state["stop_observed_extreme"]
        stop_observed_extreme = (
            None if raw_stop_extreme is None else float(raw_stop_extreme)
        )
        raw_stop_ts = state["stop_ts"]
        stop_ts = None if raw_stop_ts is None else int(raw_stop_ts)
        ledger_invalid = bool(state["ledger_invalid"])
        candle_volume_used_qty = float(state["candle_volume_used_qty"])
        orders = {
            int(index): int(quantity)
            for index, quantity in dict(state["orders"]).items()  # type: ignore[arg-type]
        }
        pending_orders = {
            int(index): int(quantity)
            for index, quantity in dict(state["pending_orders"]).items()  # type: ignore[arg-type]
        }

    def equivalent_ledger(left: dict[str, object], right: dict[str, object]) -> bool:
        for key in (
            "cash",
            "execution_cost",
            "previous_price",
            "max_adverse_position_value",
            "candle_volume_used_qty",
        ):
            if not math.isclose(
                float(left[key]), float(right[key]), rel_tol=1e-12, abs_tol=max(1e-10, tolerance)
            ):
                return False
        for key in ("position_slots", "stopped", "stop_ts", "ledger_invalid", "orders", "pending_orders"):
            if left[key] != right[key]:
                return False
        for key in ("stop_price", "stop_boundary_price", "stop_observed_extreme"):
            left_value = left[key]
            right_value = right[key]
            if left_value is None or right_value is None:
                if left_value is not right_value:
                    return False
                continue
            if not math.isclose(
                float(left_value),
                float(right_value),
                rel_tol=1e-12,
                abs_tol=max(1e-10, tolerance),
            ):
                return False
        return True

    def simulate_intrabar_path(base: dict[str, object], targets: list[float], *, event_ts: int) -> dict[str, object]:
        restore_ledger(base)
        for target in targets:
            process_segment(float(target), event_ts=event_ts)
            if stopped or ledger_invalid:
                break
        return snapshot_ledger()

    for row_index, row in enumerate(rows):
        row_ts = strict_integer(row["ts"])
        row_open = _finite_positive_or_none(row["open"])
        row_high = _finite_positive_or_none(row["high"])
        row_low = _finite_positive_or_none(row["low"])
        row_close = _finite_positive_or_none(row["close"])
        row_volume = _finite_or_default(row["volume"], -1.0)
        if (
            row_ts is None
            or None in (row_open, row_high, row_low, row_close)
            or row_volume < 0.0
        ):
            _record_outcome_failure(
                diagnostics,
                "invalid_ohlcv_row",
                row_index=int(row_index),
                event_ts=row.get("ts"),
                open=row.get("open"),
                high=row.get("high"),
                low=row.get("low"),
                close=row.get("close"),
                volume=row.get("volume"),
            )
            return None
        candle_volume_capacity_qty = float(row_volume)
        candle_volume_used_qty = 0.0
        activate_pending_orders()
        if ledger_invalid:
            _ensure_outcome_failure(diagnostics, "invalid_grid_ledger_after_activation", event_ts=int(row_ts))
            return None

        # Directional grids enter their initial inventory at the first tradeable
        # candle open. That market quantity is part of the same counterfactual
        # execution and cannot exceed the entire observed minute volume either.
        if row_index == 0 and abs(int(position_slots)) > 0:
            if not consume_observed_candle_volume(
                abs(int(position_slots)),
                reason="insufficient_candle_volume_for_initial_inventory",
                event_ts=row_ts,
                fill_price=float(row_open),
            ):
                return None

        # Funding at the minute boundary is charged against inventory already
        # held before the first trade of that minute.
        while event_index < len(funding_events) and funding_events[event_index][0] <= row_ts:
            _event_ts, settled_rate = funding_events[event_index]
            apply_funding_event(settled_rate, float(row_open), _event_ts)
            event_index += 1
            if ledger_invalid:
                _ensure_outcome_failure(diagnostics, "invalid_grid_ledger_after_funding", event_ts=int(row_ts))
                return None

        # A close->open jump beyond a protective boundary has no observable
        # execution path between the prior close and the new market. Limit fills,
        # stop triggering and cancellation can occur in different orders and the
        # protective order cannot be assumed to execute at the skipped boundary.
        if gap_crosses_kill_switch(float(row_open)):
            _record_outcome_failure(
                diagnostics,
                "gap_crosses_kill_switch_unobservable",
                event_ts=int(row_ts),
                previous_price=float(previous_price),
                target_price=float(row_open),
            )
            return None

        # Previous close -> current open is observable and may cross narrow grid
        # levels even when the candle later closes back at the previous price.
        process_segment(float(row_open), event_ts=row_ts)
        if ledger_invalid:
            _ensure_outcome_failure(diagnostics, "invalid_grid_ledger_after_gap", event_ts=int(row_ts))
            return None
        if stopped:
            break

        upper_breach = float(row_high) >= ks_upper_f - tolerance
        lower_breach = float(row_low) <= ks_lower_f + tolerance
        if upper_breach and lower_breach:
            # OHLC does not reveal which protective boundary was reached first.
            # Any chosen chronology would fabricate a different stopped bot.
            _record_outcome_failure(
                diagnostics,
                "dual_kill_switch_breach_order_unobservable",
                event_ts=int(row_ts),
                candle_high=float(row_high),
                candle_low=float(row_low),
                kill_switch_lower=float(ks_lower_f),
                kill_switch_upper=float(ks_upper_f),
            )
            return None
        if upper_breach:
            base_state = snapshot_ledger()
            stop_first = simulate_intrabar_path(base_state, [float(row_high)], event_ts=row_ts)
            opposite_first = simulate_intrabar_path(
                base_state, [float(row_low), float(row_high)], event_ts=row_ts
            )
            if not equivalent_ledger(stop_first, opposite_first):
                restore_ledger(base_state)
                _record_outcome_failure(
                    diagnostics,
                    "kill_switch_intrabar_order_unobservable",
                    event_ts=int(row_ts),
                    breach_side="upper",
                )
                return None
            restore_ledger(stop_first)
            break
        if lower_breach:
            base_state = snapshot_ledger()
            stop_first = simulate_intrabar_path(base_state, [float(row_low)], event_ts=row_ts)
            opposite_first = simulate_intrabar_path(
                base_state, [float(row_high), float(row_low)], event_ts=row_ts
            )
            if not equivalent_ledger(stop_first, opposite_first):
                restore_ledger(base_state)
                _record_outcome_failure(
                    diagnostics,
                    "kill_switch_intrabar_order_unobservable",
                    event_ts=int(row_ts),
                    breach_side="lower",
                )
                return None
            restore_ledger(stop_first)
            break

        open_f = float(row_open)
        close_f = float(row_close)
        high_excursion = float(row_high) > max(open_f, close_f) + tolerance
        low_excursion = float(row_low) < min(open_f, close_f) - tolerance
        if high_excursion and not low_excursion:
            # Only the upper excursion extends beyond both endpoints, so the
            # chronology open -> high -> close is unambiguous.
            process_segment(float(row_high), event_ts=row_ts)
            process_segment(close_f, event_ts=row_ts)
        elif low_excursion and not high_excursion:
            process_segment(float(row_low), event_ts=row_ts)
            process_segment(close_f, event_ts=row_ts)
        elif high_excursion and low_excursion:
            # OHLC exposes both extremes but not their order. Simulate the two
            # admissible extreme paths and keep the label only when cash, open
            # inventory, fees, adverse funding exposure and resting orders are
            # identical. Endpoint-only accounting can otherwise manufacture a
            # third P&L that no valid intrabar path produces.
            base_state = snapshot_ledger()
            high_first = simulate_intrabar_path(
                base_state, [float(row_high), float(row_low), close_f], event_ts=row_ts
            )
            low_first = simulate_intrabar_path(
                base_state, [float(row_low), float(row_high), close_f], event_ts=row_ts
            )
            if not equivalent_ledger(high_first, low_first):
                restore_ledger(base_state)
                _record_outcome_failure(
                    diagnostics,
                    "intrabar_extreme_order_unobservable",
                    event_ts=int(row_ts),
                    candle_high=float(row_high),
                    candle_low=float(row_low),
                )
                return None
            restore_ledger(high_first)
        else:
            process_segment(close_f, event_ts=row_ts)
        if ledger_invalid:
            _ensure_outcome_failure(diagnostics, "invalid_grid_ledger_after_intrabar_path", event_ts=int(row_ts))
            return None
        if stopped:
            break

    if ledger_invalid:
        _ensure_outcome_failure(diagnostics, "invalid_grid_ledger_after_window")
        return None

    if not stopped:
        while event_index < len(funding_events):
            _event_ts, settled_rate = funding_events[event_index]
            apply_funding_event(settled_rate, exit_f, _event_ts)
            event_index += 1
            if ledger_invalid:
                _ensure_outcome_failure(diagnostics, "invalid_grid_ledger_after_terminal_funding", event_ts=int(_event_ts))
                return None

        # The exact horizon open belongs to a new one-minute candle. Any gap
        # fills and the terminal residual close must share that candle's own
        # observed volume budget; carrying forward the previous minute's budget
        # fabricates liquidity across a timestamp boundary. Production outcome
        # labeling waits until this boundary candle is complete. Direct legacy
        # unit calls may omit it unless strict boundary evidence is requested.
        terminal_candle = _get_candle_at_exact(conn, venue, symbol, ts_end)
        if terminal_candle is None:
            if require_terminal_boundary_candle:
                _record_outcome_failure(
                    diagnostics,
                    "missing_or_invalid_horizon_boundary_candle",
                    transient=True,
                    event_ts=int(ts_end),
                )
                return None
        else:
            terminal_open = float(terminal_candle["open"])
            if not math.isclose(terminal_open, exit_f, rel_tol=1e-12, abs_tol=1e-12):
                _record_outcome_failure(
                    diagnostics,
                    "horizon_boundary_open_mismatch",
                    event_ts=int(ts_end),
                    expected_open=float(terminal_open),
                    supplied_exit=float(exit_f),
                )
                return None
            candle_volume_capacity_qty = float(terminal_candle["volume"])
            candle_volume_used_qty = 0.0

        # A gap through the kill-switch is path-ambiguous and cannot be priced at
        # the skipped protective boundary.
        if gap_crosses_kill_switch(exit_f):
            _record_outcome_failure(
                diagnostics,
                "terminal_gap_crosses_kill_switch_unobservable",
                event_ts=int(ts_end),
                previous_price=float(previous_price),
                target_price=float(exit_f),
            )
            return None
        activate_pending_orders()
        if ledger_invalid:
            _ensure_outcome_failure(diagnostics, "invalid_grid_ledger_at_terminal_activation", event_ts=int(ts_end))
            return None
        process_segment(exit_f, event_ts=ts_end)
        if ledger_invalid:
            _ensure_outcome_failure(diagnostics, "invalid_grid_ledger_at_terminal_segment", event_ts=int(ts_end))
            return None

    liquidation_price = float(stop_price) if stopped and stop_price is not None else exit_f
    open_position_slots = current_position_slots()
    if (
        not stopped
        and open_position_slots != 0
        and _get_candle_at_exact(conn, venue, symbol, ts_end) is not None
        and not consume_observed_candle_volume(
            abs(int(open_position_slots)),
            reason="insufficient_candle_volume_for_terminal_liquidation",
            event_ts=int(ts_end),
            fill_price=float(liquidation_price),
        )
    ):
        return None
    gross_pnl = cash + float(open_position_slots) * liquidation_price
    execution_cost += (
        abs(float(open_position_slots))
        * liquidation_price
        * market_half_leg_cost_rate
    )
    # Normalize by the actual initial grid commitment. Number of Grids is the
    # interval count. There are grid_count + 1 price levels, but dynamic mode
    # keeps one pivot/bridge level idle, so exactly grid_count initial orders exist.
    # Directional close-only orders are
    # backed by the initial position; neutral opening orders on both sides reserve
    # margin. Dividing by entry * grid_count overstated returns whenever the
    # reference lay between levels.
    capital_reference = float(topology["committed_notional_per_qty"])
    if not math.isfinite(capital_reference) or capital_reference <= 0.0:
        _record_outcome_failure(
            diagnostics,
            "invalid_committed_notional_reference",
            committed_notional_per_qty=capital_reference,
        )
        return None
    if isinstance(diagnostics, dict):
        diagnostics.update({
            "signed_settled_funding_pnl": float(signed_funding_pnl),
            "conservative_funding_pnl": float(conservative_funding_pnl),
            "funding_benefit_excluded": float(max(0.0, signed_funding_pnl - conservative_funding_pnl)),
            "fill_volume_confirmation": (
                "aggregate_candle_and_liquidation_volume_cap_v2"
                if order_qty_per_slot is not None
                else "unavailable_qty_not_persisted"
            ),
            "qty_per_order_for_volume_cap": order_qty_per_slot,
            "replacement_fill_confirmation": "next_candle_activation_or_unavailable_v1",
            "kill_switch_fill_confirmation": (
                "adverse_observed_extreme_v1" if stopped else "not_triggered"
            ),
            "kill_switch_boundary_price": (
                None if stop_boundary_price is None else float(stop_boundary_price)
            ),
            "kill_switch_observed_extreme": (
                None if stop_observed_extreme is None else float(stop_observed_extreme)
            ),
            "kill_switch_liquidation_price": (
                None if stop_price is None else float(stop_price)
            ),
        })
    net_proxy = (gross_pnl - execution_cost + conservative_funding_pnl) / capital_reference

    positive_pnl_epsilon = 1e-12
    success = int(
        not stopped
        and math.isfinite(net_proxy)
        and net_proxy > positive_pnl_epsilon
    )
    if isinstance(diagnostics, dict):
        breach_side: str | None = None
        if stopped and stop_boundary_price is not None:
            if math.isclose(float(stop_boundary_price), float(ks_lower), rel_tol=1e-12, abs_tol=1e-12):
                breach_side = "lower"
            elif math.isclose(float(stop_boundary_price), float(ks_upper), rel_tol=1e-12, abs_tol=1e-12):
                breach_side = "upper"
        diagnostics.update({
            "stopped": bool(stopped),
            "kill_switch_breach_side": breach_side,
            "terminal_reason": (
                "kill_switch_breached"
                if stopped
                else (
                    "positive_net_proxy_pnl"
                    if success == 1
                    else "non_positive_net_proxy_pnl"
                )
            ),
            "net_proxy_return": float(net_proxy),
            "success": int(success),
        })
    return success, float(net_proxy)


def _decimal_positive(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _decimal_aligned(value: object, step: object) -> bool:
    number = _decimal_positive(value)
    quantum = _decimal_positive(step)
    if number is None or quantum is None:
        return False
    quotient = number / quantum
    nearest = quotient.to_integral_value()
    return abs(quotient - nearest) <= Decimal("1e-9")


def _first_positive_qty(params: dict[str, object]) -> Decimal | None:
    trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    for mapping in (
        trade_plan.get("sizing") if isinstance(trade_plan.get("sizing"), dict) else {},
        params.get("sizing") if isinstance(params.get("sizing"), dict) else {},
        trade_plan.get("economics") if isinstance(trade_plan.get("economics"), dict) else {},
        params.get("economics") if isinstance(params.get("economics"), dict) else {},
        params,
    ):
        for key in ("qty_per_order", "order_qty", "qty", "qty_per_leg"):
            qty = _decimal_positive(mapping.get(key))
            if qty is not None:
                return qty
    return None


def _record_outcome_observability_attempt(
    conn,
    *,
    rec_id: object,
    recommendation_ts: object,
    label_due_ts: object | None,
    state: str,
    reason: str,
    details: dict[str, object] | None = None,
) -> None:
    """Persist queue progress without inventing an invalid event timestamp.

    Waiting rows need a durable ``last_attempt_ts`` so an old unavailable symbol
    cannot occupy every bounded worker batch forever.  Terminally invalid rows are
    censored and leave the queue.  A database-corrupted recommendation timestamp
    is logged by the caller but is deliberately not replaced with a fabricated
    value in the observability ledger.
    """
    ts_value = strict_integer(recommendation_ts)
    if ts_value is None or ts_value <= 0:
        return
    due_value = strict_integer(label_due_ts) if label_due_ts is not None else None
    if due_value is not None and due_value < ts_value:
        due_value = None
    db.upsert_outcome_observability(
        conn,
        rec_id=str(rec_id),
        recommendation_ts=int(ts_value),
        label_due_ts=int(due_value) if due_value is not None else None,
        state=state,
        reason=reason,
        details=details or {},
        commit=True,
    )


def compute_outcomes_cycle(
    conn,
    horizon_sec: int = HORIZON_SEC_DEFAULT,
    max_to_process: int = 500,
    *,
    heartbeat: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    min_horizon = min(BOT_HORIZONS.values())
    process_limit = max(1, int(max_to_process))
    fetch_limit = max(
        process_limit,
        min(OUTCOME_MAX_ROWS_EXAMINED_PER_CYCLE, process_limit * 12),
    )
    require_llm_verdict = bool(getattr(settings, "llm_reviewer_enabled", False))
    started_monotonic = time.monotonic()
    stats: dict[str, object] = {
        "rows_selected": 0,
        "rows_examined": 0,
        "rows_labeled": 0,
        "rows_waiting": 0,
        "rows_censored": 0,
        "rows_failed": 0,
        "last_processed_rec_id": None,
        "duration_ms": 0,
    }

    def record_observability(_conn, **kwargs: object) -> None:
        _record_outcome_observability_attempt(_conn, **kwargs)
        state = str(kwargs.get("state") or "").strip().lower()
        if state == "waiting":
            stats["rows_waiting"] = int(stats["rows_waiting"]) + 1
        elif state == "censored":
            stats["rows_censored"] = int(stats["rows_censored"]) + 1

    base_sql = """SELECT r.rec_id, r.ts, r.venue, r.symbol, r.bot_type, r.direction,
                  r.params_json, r.features_ref_ts, r.status, r.reasons_json, r.model_version
           FROM recommendations r
           LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
           LEFT JOIN reco_outcome_observability obs ON obs.rec_id = r.rec_id
           WHERE r.ts <= ? AND o.rec_id IS NULL
           AND COALESCE(obs.state, '') <> 'censored'
           AND COALESCE(r.is_outcome_label_root, 1) = 1
           AND r.status NOT IN ('blocked', 'suppressed', 'pending')
           AND (
               r.status <> 'no_trade'
               OR (
                   r.outcome_eligible = 1
                   AND r.outcome_sample_role = 'shadow_no_trade'
                   AND r.risk_checks_passed = 1
                   AND r.risk_blocks_empty = 1
               )
           )"""
    params: list[object] = [db.now_ts() - min_horizon]

    if require_llm_verdict:
        # Actionable roots require a completed LLM verdict. Explicit risk-clean
        # shadow no_trade roots bypass it because the reviewer intentionally never
        # processes non-actionable rows; without this branch the learning bootstrap
        # is permanently stalled. Filter before LIMIT to preserve forward progress.
        base_sql += """
           AND (
               r.llm_review_status = 'ok'
               OR (
                   r.status = 'no_trade'
                   AND r.outcome_eligible = 1
                   AND r.outcome_sample_role = 'shadow_no_trade'
                   AND r.risk_checks_passed = 1
                   AND r.risk_blocks_empty = 1
               )
           )"""

    base_sql += """
           ORDER BY COALESCE(obs.last_attempt_ts, 0) ASC, r.ts ASC LIMIT ?"""
    params.append(fetch_limit)

    cur = conn.execute(base_sql, tuple(params))
    rows = cur.fetchall()
    stats["rows_selected"] = len(rows)
    done = 0

    for r in rows:
        if heartbeat is not None and not heartbeat():
            raise RuntimeError("outcome runtime lock lost")
        rec_id = r["rec_id"]
        stats["rows_examined"] = int(stats["rows_examined"]) + 1
        stats["last_processed_rec_id"] = str(rec_id)
        if progress_callback is not None:
            progress_callback(dict(stats))
        if require_llm_verdict and not db.is_outcome_eligible_under_llm_mode(r["status"], r["reasons_json"]):
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=r["ts"],
                label_due_ts=None,
                state="censored",
                reason="llm_outcome_contract_not_satisfied",
            )
            continue
        if str(r["status"] or "").strip().lower() == "no_trade" and not _shadow_no_trade_outcome_eligible(
            r["status"], r["reasons_json"]
        ):
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=r["ts"],
                label_due_ts=None,
                state="censored",
                reason="shadow_outcome_contract_not_satisfied",
            )
            continue
        bot_type = r["bot_type"]
        if bot_type not in SUPPORTED_BOT_TYPES:
            db.log_decision(conn, "OUTCOME_SKIP_UNSUPPORTED_BOT_TYPE", rec_id, None, {"bot_type": bot_type})
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=r["ts"],
                label_due_ts=None,
                state="censored",
                reason="unsupported_bot_type",
                details={"bot_type": bot_type},
            )
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
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=r["ts"],
                label_due_ts=None,
                state="censored",
                reason="invalid_temporal_fields",
                details={"features_ref_ts": r["features_ref_ts"]},
            )
            continue

        if direction is None or not _is_supported_direction(bot_type, venue, direction):
            db.log_decision(conn, "OUTCOME_SKIP_UNSUPPORTED_DIRECTION", rec_id, None, {
                "bot_type": bot_type,
                "venue": venue,
                "symbol": symbol,
                "direction": direction,
            })
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=ts0,
                label_due_ts=ts0 + int(BOT_HORIZONS.get(bot_type, horizon_sec)) + 120,
                state="censored",
                reason="unsupported_direction",
                details={"direction": direction, "venue": venue},
            )
            continue

        import json
        try:
            params = json.loads(r["params_json"], parse_constant=lambda _token: None) if r["params_json"] else None
        except Exception:
            params = None
        tradeable = _get_first_tradeable_candle_after(conn, venue, symbol, signal_ref_ts, ts0)
        if tradeable is None:
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=ts0,
                label_due_ts=ts0 + int(BOT_HORIZONS.get(bot_type, horizon_sec)) + 120,
                state="waiting",
                reason="missing_tradeable_entry_candle",
                details={"venue": venue, "symbol": symbol, "features_ref_ts": signal_ref_ts},
            )
            continue
        entry_ts, entry = tradeable

        effective_horizon, used_fallback_horizon = _resolve_effective_horizon(bot_type, params, horizon_sec)
        if used_fallback_horizon:
            db.log_decision(conn, "OUTCOME_HORIZON_FALLBACK_USED", rec_id, None, {
                "bot_type": bot_type,
                "fallback_horizon_sec": effective_horizon,
            })
        ts_exit = entry_ts + effective_horizon
        label_available_ts = ts_exit + 60
        if db.now_ts() < label_available_ts:
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=ts0,
                label_due_ts=label_available_ts,
                state="waiting",
                reason="label_horizon_not_mature",
                details={"entry_ts": entry_ts, "label_available_ts": label_available_ts},
            )
            continue
        if not _has_complete_1m_window(conn, venue, symbol, entry_ts, ts_exit):
            db.log_decision(conn, "OUTCOME_SKIP_INCOMPLETE_HORIZON", rec_id, None, {
                "venue": venue,
                "symbol": symbol,
                "entry_ts": entry_ts,
                "label_available_ts": ts_exit,
                "horizon_sec": effective_horizon,
            })
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=ts0,
                label_due_ts=label_available_ts,
                state="waiting",
                reason="incomplete_horizon_window",
                details={"venue": venue, "symbol": symbol, "entry_ts": entry_ts, "horizon_end_ts": ts_exit},
            )
            continue
        if _get_candle_at_exact(conn, venue, symbol, ts_exit) is None:
            _log_outcome_decision_once(
                conn,
                "OUTCOME_WAIT_HORIZON_BOUNDARY_CANDLE",
                rec_id,
                {
                    "venue": venue,
                    "symbol": symbol,
                    "entry_ts": entry_ts,
                    "horizon_end_ts": ts_exit,
                    "label_available_ts": label_available_ts,
                },
                cooldown_sec=3600,
            )
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=ts0,
                label_due_ts=label_available_ts,
                state="waiting",
                reason="missing_horizon_boundary_candle",
                details={"venue": venue, "symbol": symbol, "horizon_end_ts": ts_exit},
            )
            continue

        if entry is None or entry == 0:
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=ts0,
                label_due_ts=label_available_ts,
                state="censored",
                reason="invalid_entry_price",
                details={"entry": entry, "entry_ts": entry_ts},
            )
            continue
        ret_proxy: float
        success: int
        exitp: float

        if bot_type in GRID_BOTS:
            ep = _get_open_at_exact(conn, venue, symbol, ts_exit)
            if ep is None:
                record_observability(
                    conn,
                    rec_id=rec_id,
                    recommendation_ts=ts0,
                    label_due_ts=label_available_ts,
                    state="waiting",
                    reason="missing_exit_open",
                    details={"venue": venue, "symbol": symbol, "horizon_end_ts": ts_exit},
                )
                continue
            exitp = ep
            diagnostics: dict[str, object] = {}
            grid_result = _grid_outcome(
                conn,
                venue,
                symbol,
                entry,
                exitp,
                entry_ts,
                ts_exit,
                direction,
                params,
                diagnostics=diagnostics,
                require_terminal_boundary_candle=True,
            )
            if grid_result is None:
                reason = str(diagnostics.get("reason") or "grid_outcome_unavailable_without_diagnostic")
                transient = diagnostics.get("transient") is True
                details: dict[str, object] = {
                    "venue": venue,
                    "symbol": symbol,
                    "entry_ts": entry_ts,
                    "entry_price": entry,
                    "label_available_ts": ts_exit,
                    "reason": reason,
                    "transient": transient,
                }
                details.update({
                    key: value for key, value in diagnostics.items()
                    if key not in {"reason", "transient"}
                })
                action = (
                    "OUTCOME_WAIT_FUNDING_SETTLEMENT"
                    if transient and reason in {
                        "missing_funding_settlement",
                        "funding_settlement_history_unavailable",
                    }
                    else "OUTCOME_SKIP_INVALID_GRID_CONTRACT"
                )
                db.upsert_outcome_observability(
                    conn,
                    rec_id=str(rec_id),
                    recommendation_ts=int(ts0),
                    label_due_ts=int(label_available_ts),
                    state="waiting" if transient else "censored",
                    reason=reason,
                    details=details,
                    commit=True,
                )
                state_key = "rows_waiting" if transient else "rows_censored"
                stats[state_key] = int(stats[state_key]) + 1
                _log_outcome_decision_once(
                    conn,
                    action,
                    rec_id,
                    details,
                    cooldown_sec=3600 if action == "OUTCOME_WAIT_FUNDING_SETTLEMENT" else 21600,
                )
                continue
            success, ret_proxy = grid_result

        else:
            record_observability(
                conn,
                rec_id=rec_id,
                recommendation_ts=ts0,
                label_due_ts=label_available_ts,
                state="censored",
                reason="unsupported_outcome_model",
                details={"bot_type": bot_type},
            )
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
                "label_available_ts": int(label_available_ts),
                "entry_close": float(entry),
                "exit_close": float(exitp),
                "ret": float(ret_proxy),
                "success": int(success),
                "diagnostics": dict(diagnostics),
            },
        )
        done += 1
        stats["rows_labeled"] = int(done)

    stats["rows_labeled"] = int(done)
    stats["duration_ms"] = max(0, int(round((time.monotonic() - started_monotonic) * 1000.0)))
    if progress_callback is not None:
        progress_callback(dict(stats))
    return stats


def compute_outcomes_once(
    conn,
    horizon_sec: int = HORIZON_SEC_DEFAULT,
    max_to_process: int = 500,
) -> int:
    """Backward-compatible count-only wrapper around the observable cycle API."""
    stats = compute_outcomes_cycle(
        conn,
        horizon_sec=horizon_sec,
        max_to_process=max_to_process,
    )
    return int(stats.get("rows_labeled") or 0)
