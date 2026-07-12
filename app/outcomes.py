from __future__ import annotations

from . import db
from .bot_types import GRID_BOT_TYPES, SUPPORTED_BOT_TYPES
from .grid_math import arithmetic_grid_commitment, resolve_integer_aliases, strict_integer
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


def _is_valid_outcome_candle(row: object) -> bool:
    try:
        raw_ts = row["ts"]  # type: ignore[index]
        raw_values = [row[key] for key in ("open", "high", "low", "close")]  # type: ignore[index]
    except Exception:
        return False
    if isinstance(raw_ts, bool) or any(isinstance(value, bool) for value in raw_values):
        return False
    candle_ts = strict_integer(raw_ts)
    if candle_ts is None or candle_ts <= 0:
        return False
    try:
        open_px, high_px, low_px, close_px = (float(value) for value in raw_values)
    except Exception:
        return False
    values = (open_px, high_px, low_px, close_px)
    if not all(math.isfinite(value) and value > 0.0 for value in values):
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
    the protective boundary, liquidates residual inventory there and ignores all
    later candles/funding. A close->open gap beyond that boundary is unavailable
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

    # Signed quantity per price level: positive = buy quantity, negative = sell
    # quantity. Directional grids can legitimately accumulate multiple orders at
    # the same price (for example, the existing TP for initial inventory plus a
    # replacement TP created by a newly filled adjacent buy). A side-only map
    # silently collapsed those quantities and corrupted realised PnL, fees and
    # funding inventory.
    orders: dict[int, int] = {index: 1 for index in buy_indices}
    orders.update({index: -1 for index in sell_indices})
    position_slots = int(topology["initial_position_slots"])

    cash = (-float(initial_long_slots) + float(initial_short_slots)) * entry_f
    execution_cost = (
        float(initial_long_slots + initial_short_slots)
        * entry_f
        * market_half_leg_cost_rate
    )
    previous_price = entry_f
    tolerance = max(1e-10, step_abs * 1e-10)
    funding_pnl = 0.0
    event_index = 0
    stopped = False
    stop_price: float | None = None
    stop_ts: int | None = None
    ledger_invalid = False

    def current_position_slots() -> int:
        return int(position_slots)

    def apply_funding_event(settled_rate: float | None, price: float, event_ts: int | None = None) -> None:
        nonlocal funding_pnl, ledger_invalid
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
        funding_pnl += _signed_settled_funding_pnl(slots, price, float(settled_rate))

    def adverse_exposure_value(price: float) -> float:
        # Retained only as a path-equivalence state component. Historical funding
        # P&L itself is computed from exact settlement rows below.
        return abs(float(current_position_slots())) * float(price)

    max_adverse_position_value = adverse_exposure_value(entry_f)

    def add_order(index: int, signed_quantity: int) -> None:
        nonlocal ledger_invalid
        if signed_quantity == 0 or index < 0 or index > grid_count:
            return
        existing = int(orders.get(index, 0))
        if existing != 0 and (existing > 0) != (signed_quantity > 0):
            # Opposing resting orders at one level would imply self-trading or an
            # unresolved cancel/replace chronology. Do not fabricate a label.
            ledger_invalid = True
            return
        combined = existing + int(signed_quantity)
        if combined == 0:
            orders.pop(index, None)
        else:
            orders[index] = combined

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
        nonlocal max_adverse_position_value, stopped, stop_price, stop_ts, ledger_invalid
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

        if terminal > start + tolerance:
            crossed = [
                index
                for index, grid_price in enumerate(grid_prices)
                if grid_price > start + tolerance and grid_price <= terminal + tolerance
            ]
        elif terminal < start - tolerance:
            crossed = [
                index
                for index in range(grid_count, -1, -1)
                if grid_prices[index] < start - tolerance
                and grid_prices[index] >= terminal - tolerance
            ]
        else:
            crossed = []

        for index in crossed:
            signed_quantity = int(orders.pop(index, 0))
            if signed_quantity == 0:
                continue
            fill_price = grid_prices[index]
            quantity = abs(signed_quantity)
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
            stop_price = float(terminal)
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
            "stop_ts": stop_ts,
            "ledger_invalid": bool(ledger_invalid),
            "orders": dict(orders),
        }

    def restore_ledger(state: dict[str, object]) -> None:
        nonlocal cash, execution_cost, position_slots, previous_price
        nonlocal max_adverse_position_value, stopped, stop_price, stop_ts, ledger_invalid, orders
        cash = float(state["cash"])
        execution_cost = float(state["execution_cost"])
        position_slots = int(state["position_slots"])
        previous_price = float(state["previous_price"])
        max_adverse_position_value = float(state["max_adverse_position_value"])
        stopped = bool(state["stopped"])
        raw_stop_price = state["stop_price"]
        stop_price = None if raw_stop_price is None else float(raw_stop_price)
        raw_stop_ts = state["stop_ts"]
        stop_ts = None if raw_stop_ts is None else int(raw_stop_ts)
        ledger_invalid = bool(state["ledger_invalid"])
        orders = {
            int(index): int(quantity)
            for index, quantity in dict(state["orders"]).items()  # type: ignore[arg-type]
        }

    def equivalent_ledger(left: dict[str, object], right: dict[str, object]) -> bool:
        for key in ("cash", "execution_cost", "previous_price", "max_adverse_position_value"):
            if not math.isclose(
                float(left[key]), float(right[key]), rel_tol=1e-12, abs_tol=max(1e-10, tolerance)
            ):
                return False
        for key in ("position_slots", "stopped", "stop_ts", "ledger_invalid", "orders"):
            if left[key] != right[key]:
                return False
        left_stop = left["stop_price"]
        right_stop = right["stop_price"]
        if left_stop is None or right_stop is None:
            return left_stop is right_stop
        return math.isclose(
            float(left_stop), float(right_stop), rel_tol=1e-12, abs_tol=max(1e-10, tolerance)
        )

    def simulate_intrabar_path(base: dict[str, object], targets: list[float], *, event_ts: int) -> dict[str, object]:
        restore_ledger(base)
        for target in targets:
            process_segment(float(target), event_ts=event_ts)
            if stopped or ledger_invalid:
                break
        return snapshot_ledger()

    for row in rows:
        row_ts = strict_integer(row["ts"])
        row_open = _finite_positive_or_none(row["open"])
        row_high = _finite_positive_or_none(row["high"])
        row_low = _finite_positive_or_none(row["low"])
        row_close = _finite_positive_or_none(row["close"])
        if row_ts is None or None in (row_open, row_high, row_low, row_close):
            return None

        # Funding at the minute boundary is charged against inventory already
        # held before the first trade of that minute.
        while event_index < len(funding_events) and funding_events[event_index][0] <= row_ts:
            _event_ts, settled_rate = funding_events[event_index]
            apply_funding_event(settled_rate, float(row_open), _event_ts)
            event_index += 1
            if ledger_invalid:
                return None

        # A close->open jump beyond a protective boundary has no observable
        # execution path between the prior close and the new market. Limit fills,
        # stop triggering and cancellation can occur in different orders and the
        # protective order cannot be assumed to execute at the skipped boundary.
        if gap_crosses_kill_switch(float(row_open)):
            return None

        # Previous close -> current open is observable and may cross narrow grid
        # levels even when the candle later closes back at the previous price.
        process_segment(float(row_open), event_ts=row_ts)
        if ledger_invalid:
            return None
        if stopped:
            break

        upper_breach = float(row_high) >= ks_upper_f - tolerance
        lower_breach = float(row_low) <= ks_lower_f + tolerance
        if upper_breach and lower_breach:
            # OHLC does not reveal which protective boundary was reached first.
            # Any chosen chronology would fabricate a different stopped bot.
            return None
        if upper_breach:
            base_state = snapshot_ledger()
            stop_first = simulate_intrabar_path(base_state, [ks_upper_f], event_ts=row_ts)
            opposite_first = simulate_intrabar_path(
                base_state, [float(row_low), ks_upper_f], event_ts=row_ts
            )
            if not equivalent_ledger(stop_first, opposite_first):
                restore_ledger(base_state)
                return None
            restore_ledger(stop_first)
            break
        if lower_breach:
            base_state = snapshot_ledger()
            stop_first = simulate_intrabar_path(base_state, [ks_lower_f], event_ts=row_ts)
            opposite_first = simulate_intrabar_path(
                base_state, [float(row_high), ks_lower_f], event_ts=row_ts
            )
            if not equivalent_ledger(stop_first, opposite_first):
                restore_ledger(base_state)
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
                return None
            restore_ledger(high_first)
        else:
            process_segment(close_f, event_ts=row_ts)
        if ledger_invalid:
            return None
        if stopped:
            break

    if not stopped:
        while event_index < len(funding_events):
            _event_ts, settled_rate = funding_events[event_index]
            apply_funding_event(settled_rate, exit_f, _event_ts)
            event_index += 1
            if ledger_invalid:
                return None

        # The exact horizon open is the next observable price after the final
        # in-window close. A gap through the kill-switch is path-ambiguous and
        # cannot be priced at the skipped protective boundary.
        if gap_crosses_kill_switch(exit_f):
            return None
        process_segment(exit_f, event_ts=ts_end)
        if ledger_invalid:
            return None

    liquidation_price = float(stop_price) if stopped and stop_price is not None else exit_f
    open_position_slots = current_position_slots()
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
        return None
    net_proxy = (gross_pnl - execution_cost + funding_pnl) / capital_reference

    positive_pnl_epsilon = 1e-12
    success = int(
        not stopped
        and math.isfinite(net_proxy)
        and net_proxy > positive_pnl_epsilon
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
        tradeable = _get_first_tradeable_candle_after(conn, venue, symbol, signal_ref_ts, ts0)
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
            )
            if grid_result is None:
                reason = str(diagnostics.get("reason") or "unknown_grid_outcome_failure")
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
