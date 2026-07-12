"""Conservative linear-USDT futures grid economics.

The helpers in this module intentionally avoid exchange-specific hidden state
(wallet balance, current maintenance tier, live mark price). They are suitable
for recommendation/preflight estimates and must be treated as conservative
operator guidance, not as Bybit's exact liquidation engine.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, InvalidOperation, getcontext
from typing import Any

from .trading_semantics import normalize_execution_direction

getcontext().prec = 36

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


def dec(value: Any, default: str | Decimal = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)
    if not d.is_finite():
        return Decimal(default)
    return d


def as_float(value: Decimal) -> float:
    return float(+value)


def strict_integer(value: Any) -> int | None:
    """Parse an exact integer without silently truncating numeric payloads.

    JSON numbers such as ``5.0`` are accepted because they represent an exact
    integer. Fractional values, booleans, blanks and non-finite numbers are
    rejected. This is intentionally stricter than ``int(value)``: exchange
    counts must not change meaning through truncation.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def resolve_integer_aliases(candidates: list[tuple[str, Any]]) -> dict[str, Any]:
    """Resolve duplicated integer fields without masking invalid primaries.

    ``None`` and blank strings are treated as absent legacy fields. Any other
    non-integer value is an explicit invalid source. Multiple valid aliases must
    agree exactly; callers can use ``conservative_max`` for exposure estimates
    and ``conservative_min`` for outcome caps while the strict preflight blocks
    the conflict.
    """
    sources: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for field, raw_value in candidates:
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            continue
        parsed = strict_integer(raw_value)
        if parsed is None:
            invalid.append({"field": str(field), "value": raw_value})
            continue
        sources.append({"field": str(field), "value": parsed})

    distinct_values = sorted({int(item["value"]) for item in sources})
    return {
        "ok": not invalid and len(distinct_values) <= 1,
        "value": distinct_values[0] if len(distinct_values) == 1 else None,
        "conservative_min": min(distinct_values) if distinct_values else None,
        "conservative_max": max(distinct_values) if distinct_values else None,
        "values": distinct_values,
        "sources": sources,
        "invalid": invalid,
        "conflict": len(distinct_values) > 1,
    }


def arithmetic_grid_commitment(
    *,
    lower: Any,
    upper: Any,
    grid_count: Any,
    reference_price: Any,
    direction: str,
) -> dict[str, Any] | None:
    """Resolve initial arithmetic-grid orders and committed notional per unit qty.

    Bybit ``Number of Grids`` is the number of price intervals, so there are
    ``grid_count + 1`` price levels but exactly ``grid_count`` initial orders.
    When the reference lies exactly on one level, that occupied level is idle.
    Between levels, Bybit's dynamic topology keeps one adjacent bridge level idle
    until the neighbouring order fills; it must not be treated as an extra
    immediately executable order.

    Directional close-only orders are backed by the initial position; opening
    orders on the adverse side require additional commitment. Neutral mode starts
    without a position, so every initial Buy and Sell order is opening/margin-
    bearing. ``committed_notional_per_qty`` therefore sums both neutral opening
    stacks, while ``max_abs_position_slots`` remains the larger one-way stack.
    Keeping reservation and maximum net position separate prevents both margin
    understatement and double-counting directional exposure.
    """
    lower_d = dec(lower)
    upper_d = dec(upper)
    reference_d = dec(reference_price)
    count = strict_integer(grid_count)
    direction_norm = normalize_execution_direction(direction)
    if (
        count is None
        or count <= 0
        or lower_d <= ZERO
        or upper_d <= lower_d
        or reference_d < lower_d
        or reference_d > upper_d
        or direction_norm not in {"neutral", "long", "short"}
    ):
        return None

    step = (upper_d - lower_d) / Decimal(count)
    if step <= ZERO:
        return None
    levels = [+(lower_d + step * Decimal(index)) for index in range(count + 1)]
    nearest_index = int(((reference_d - lower_d) / step).to_integral_value(rounding=ROUND_HALF_UP))
    tolerance = max(Decimal("1e-12"), abs(step) * Decimal("1e-10"))
    exact_grid_line = (
        0 <= nearest_index <= count
        and abs(reference_d - levels[nearest_index]) <= tolerance
    )

    if exact_grid_line:
        pivot_index = nearest_index
        idle_grid_index = pivot_index
        buy_indices = list(range(0, pivot_index))
        sell_indices = list(range(pivot_index + 1, count + 1))
        initial_long_slots = len(sell_indices) if direction_norm == "long" else 0
        initial_short_slots = len(buy_indices) if direction_norm == "short" else 0
        cell_index: int | None = None
    else:
        raw_cell = int(((reference_d - lower_d) / step).to_integral_value(rounding=ROUND_FLOOR))
        cell_index = max(0, min(count - 1, raw_cell))
        pivot_index = None

        # Bybit's dynamic Futures Grid topology keeps exactly ``grid_count``
        # initial orders.  When reference lies between two grid prices, one
        # adjacent bridge level is intentionally idle and is created only after
        # the neighbouring order fills.  Placing orders on all ``N + 1`` levels
        # fabricates an extra opening/closing lot and overstates capital, margin
        # and historical fills.
        if direction_norm in {"neutral", "long"}:
            idle_grid_index = cell_index + 1
            buy_indices = list(range(0, cell_index + 1))
            sell_indices = list(range(cell_index + 2, count + 1))
        else:  # short
            idle_grid_index = cell_index
            buy_indices = list(range(0, cell_index))
            sell_indices = list(range(cell_index + 1, count + 1))

        initial_long_slots = len(sell_indices) if direction_norm == "long" else 0
        initial_short_slots = len(buy_indices) if direction_norm == "short" else 0

    active_order_count = len(buy_indices) + len(sell_indices)
    buy_opening_price_sum = sum((levels[index] for index in buy_indices), ZERO)
    sell_opening_price_sum = sum((levels[index] for index in sell_indices), ZERO)
    if direction_norm == "long":
        committed_price_sum = (
            reference_d * Decimal(initial_long_slots)
            + buy_opening_price_sum
        )
        committed_slot_count = initial_long_slots + len(buy_indices)
        max_abs_position_slots = initial_long_slots + len(buy_indices)
    elif direction_norm == "short":
        committed_price_sum = (
            reference_d * Decimal(initial_short_slots)
            + sell_opening_price_sum
        )
        committed_slot_count = initial_short_slots + len(sell_indices)
        max_abs_position_slots = initial_short_slots + len(sell_indices)
    else:
        # Neutral starts flat and every initial resting order is opening/margin-
        # bearing. One-way netting limits the maximum simultaneous position to the
        # larger directional stack, but it does not make the opposite opening
        # orders free. Reserve both stacks for sizing/preflight and keep the
        # maximum net position as a separate risk quantity.
        committed_price_sum = buy_opening_price_sum + sell_opening_price_sum
        committed_slot_count = len(buy_indices) + len(sell_indices)
        max_abs_position_slots = max(len(buy_indices), len(sell_indices))

    if active_order_count <= 0 or committed_price_sum <= ZERO:
        return None
    return {
        "grid_count": int(count),
        "step_abs": as_float(step),
        "grid_prices": [as_float(value) for value in levels],
        "exact_grid_line": bool(exact_grid_line),
        "pivot_index": pivot_index,
        "cell_index": cell_index,
        "idle_grid_index": int(idle_grid_index),
        "buy_indices": buy_indices,
        "sell_indices": sell_indices,
        "initial_long_slots": int(initial_long_slots),
        "initial_short_slots": int(initial_short_slots),
        "initial_position_slots": int(initial_long_slots - initial_short_slots),
        "active_order_count": int(active_order_count),
        "committed_slot_count": int(committed_slot_count),
        "max_abs_position_slots": int(max_abs_position_slots),
        "buy_opening_notional_per_qty": as_float(buy_opening_price_sum),
        "sell_opening_notional_per_qty": as_float(sell_opening_price_sum),
        "committed_notional_per_qty": as_float(committed_price_sum),
    }


def arithmetic_grid_cross_margin_stress(
    *,
    lower: Any,
    upper: Any,
    grid_count: Any,
    reference_price: Any,
    direction: str,
    leverage: Any,
    kill_switch_lower: Any,
    kill_switch_upper: Any,
    execution_cost_bps: Any = 0,
    maintenance_margin_rate: Any = "0.005",
) -> dict[str, Any] | None:
    """Conservative bot-equity stress for Bybit Futures Grid cross margin.

    Bybit Futures Grid Bot runs in cross margin and one-way position mode.  A
    single-position isolated liquidation-price formula is therefore not a valid
    safety oracle.  This helper instead asks a narrower, auditable question:
    after funding the bot's initial grid commitment, does the dedicated bot
    equity remain positive after a monotonic adverse move to the configured
    kill-switch, opening/closing execution costs and a maintenance-margin
    reserve?  Funding receipts and grid-profit recoveries are deliberately not
    credited.

    Values are returned per unit order quantity so callers can use the same
    result for leverage selection, UI diagnostics and strict preflight.
    """
    topology = arithmetic_grid_commitment(
        lower=lower,
        upper=upper,
        grid_count=grid_count,
        reference_price=reference_price,
        direction=direction,
    )
    if topology is None:
        return None

    ref = dec(reference_price)
    lower_ks = dec(kill_switch_lower)
    upper_ks = dec(kill_switch_upper)
    lev = dec(leverage)
    mmr = max(ZERO, dec(maintenance_margin_rate))
    half_cost_rate = max(ZERO, dec(execution_cost_bps)) / Decimal("20000")
    direction_norm = normalize_execution_direction(direction)
    if (
        ref <= ZERO
        or lower_ks <= ZERO
        or upper_ks <= lower_ks
        or lev <= ZERO
        or direction_norm not in {"neutral", "long", "short"}
    ):
        return None

    levels = [dec(value) for value in topology.get("grid_prices") or []]
    buy_prices = [levels[int(index)] for index in topology.get("buy_indices") or []]
    sell_prices = [levels[int(index)] for index in topology.get("sell_indices") or []]
    initial_long_slots = int(topology.get("initial_long_slots") or 0)
    initial_short_slots = int(topology.get("initial_short_slots") or 0)

    committed = dec(topology.get("committed_notional_per_qty"))
    if committed <= ZERO:
        return None
    initial_margin = committed / lev

    def _long_stress() -> dict[str, Decimal]:
        entries = [ref] * initial_long_slots + [price for price in buy_prices if price >= lower_ks]
        gross_loss = sum((max(ZERO, entry - lower_ks) for entry in entries), ZERO)
        entry_notional = sum(entries, ZERO)
        exit_notional = lower_ks * Decimal(len(entries))
        execution_cost = (entry_notional + exit_notional) * half_cost_rate
        maintenance = exit_notional * mmr
        total = gross_loss + execution_cost + maintenance
        return {
            "position_slots": Decimal(len(entries)),
            "entry_notional": entry_notional,
            "exit_notional": exit_notional,
            "gross_loss": gross_loss,
            "execution_cost": execution_cost,
            "maintenance_reserve": maintenance,
            "total_stress": total,
            "equity_buffer": initial_margin - total,
        }

    def _short_stress() -> dict[str, Decimal]:
        entries = [ref] * initial_short_slots + [price for price in sell_prices if price <= upper_ks]
        gross_loss = sum((max(ZERO, upper_ks - entry) for entry in entries), ZERO)
        entry_notional = sum(entries, ZERO)
        exit_notional = upper_ks * Decimal(len(entries))
        execution_cost = (entry_notional + exit_notional) * half_cost_rate
        maintenance = exit_notional * mmr
        total = gross_loss + execution_cost + maintenance
        return {
            "position_slots": Decimal(len(entries)),
            "entry_notional": entry_notional,
            "exit_notional": exit_notional,
            "gross_loss": gross_loss,
            "execution_cost": execution_cost,
            "maintenance_reserve": maintenance,
            "total_stress": total,
            "equity_buffer": initial_margin - total,
        }

    long_side = _long_stress()
    short_side = _short_stress()
    applicable: list[tuple[str, dict[str, Decimal]]]
    if direction_norm == "long":
        applicable = [("long", long_side)]
    elif direction_norm == "short":
        applicable = [("short", short_side)]
    else:
        applicable = [("long", long_side), ("short", short_side)]
    worst_side, worst = min(applicable, key=lambda item: item[1]["equity_buffer"])

    def _pct(value: Decimal) -> float | None:
        if initial_margin <= ZERO:
            return None
        return as_float(value / initial_margin * Decimal("100"))

    return {
        "model": "bybit_futures_grid_cross_margin_equity_stress_v1",
        "direction": direction_norm,
        "leverage": as_float(lev),
        "committed_notional_per_qty": as_float(committed),
        "initial_margin_per_qty": as_float(initial_margin),
        "kill_switch_lower": as_float(lower_ks),
        "kill_switch_upper": as_float(upper_ks),
        "execution_cost_bps": as_float(max(ZERO, dec(execution_cost_bps))),
        "maintenance_margin_rate": as_float(mmr),
        "worst_side": worst_side,
        "worst_loss_per_qty": as_float(worst["gross_loss"]),
        "maintenance_and_fee_reserve_per_qty": as_float(
            worst["execution_cost"] + worst["maintenance_reserve"]
        ),
        "equity_buffer_per_qty": as_float(worst["equity_buffer"]),
        "equity_buffer_pct": _pct(worst["equity_buffer"]),
        "long": {
            key: as_float(value) for key, value in long_side.items()
        } | {"equity_buffer_pct": _pct(long_side["equity_buffer"])},
        "short": {
            key: as_float(value) for key, value in short_side.items()
        } | {"equity_buffer_pct": _pct(short_side["equity_buffer"])},
        "funding_benefit_credited": False,
        "grid_profit_credited": False,
    }


def quantize_step(value: Any, step: Any, *, mode: str = "nearest") -> Decimal | None:
    v = dec(value)
    s = dec(step)
    if s <= ZERO:
        return None
    units = v / s
    if mode == "down":
        rounded_units = units.to_integral_value(rounding=ROUND_FLOOR)
    elif mode == "up":
        rounded_units = units.to_integral_value(rounding=ROUND_CEILING)
    else:
        rounded_units = units.to_integral_value(rounding=ROUND_HALF_UP)
    return +(rounded_units * s)


def linear_pnl_usdt(side: str, qty: Any, entry_price: Any, exit_price: Any) -> Decimal:
    q = dec(qty)
    entry = dec(entry_price)
    exitp = dec(exit_price)
    if q <= ZERO or entry <= ZERO or exitp <= ZERO:
        return ZERO
    side_norm = normalize_execution_direction(side)
    if side_norm == "long":
        return q * (exitp - entry)
    if side_norm == "short":
        return q * (entry - exitp)
    # Fail closed: a mistyped side must not silently become a long PnL.
    return ZERO


def round_trip_fee_usdt(entry_notional: Any, exit_notional: Any, fee_bps: Any) -> Decimal:
    fee_rate = max(ZERO, dec(fee_bps)) / BPS
    return (max(ZERO, dec(entry_notional)) + max(ZERO, dec(exit_notional))) * fee_rate


def funding_cashflow_usdt(side: str, position_notional: Any, funding_rate: Any, events: Any = 1) -> Decimal:
    """Positive means cost paid by this side; negative means funding received."""
    notional = max(ZERO, dec(position_notional))
    rate = dec(funding_rate)
    parsed_events = strict_integer(events)
    n_events = max(0, parsed_events) if parsed_events is not None else 0
    side_norm = normalize_execution_direction(side)
    if side_norm == "long":
        sign = ONE
    elif side_norm == "short":
        sign = Decimal("-1")
    else:
        # Unknown side: do not guess the funding payer/receiver.
        return ZERO
    return notional * rate * Decimal(n_events) * sign


def margin_required_usdt(notional: Any, leverage: Any) -> Decimal:
    lev = dec(leverage, "1")
    if lev <= ZERO:
        lev = ONE
    return max(ZERO, dec(notional)) / lev


def estimate_linear_liq_price(side: str, entry_price: Any, leverage: Any, mmr: Any = "0.005", fee_buffer_rate: Any = "0.001") -> Decimal | None:
    """Approximate isolated linear liquidation price.

    Long:  entry * (1 - 1/lev + mmr + fee_buffer)
    Short: entry * (1 + 1/lev - mmr - fee_buffer)

    Bybit's exact value depends on risk tiers, account balance, mark price and
    maintenance margin. This estimate is used only for buffer gates and UI risk.
    """
    entry = dec(entry_price)
    lev = dec(leverage, "1")
    maintenance = max(ZERO, dec(mmr))
    fee_buffer = max(ZERO, dec(fee_buffer_rate))
    if entry <= ZERO or lev <= ZERO:
        return None
    inv_lev = ONE / lev
    side_norm = normalize_execution_direction(side)
    if side_norm == "short":
        factor = ONE + inv_lev - maintenance - fee_buffer
    elif side_norm == "long":
        factor = ONE - inv_lev + maintenance + fee_buffer
    else:
        # Unknown side must not silently become a long liquidation estimate.
        return None
    if factor <= ZERO:
        return None
    return +(entry * factor)


def liquidation_buffer_pct(side: str, reference_price: Any, liq_price: Any) -> Decimal | None:
    ref = dec(reference_price)
    liq = dec(liq_price)
    if ref <= ZERO or liq <= ZERO:
        return None
    side_norm = normalize_execution_direction(side)
    if side_norm == "short":
        return max(ZERO, (liq - ref) / ref * Decimal("100"))
    if side_norm == "long":
        return max(ZERO, (ref - liq) / ref * Decimal("100"))
    return None


def grid_leg_economics(
    *,
    reference_price: Any,
    step_pct: Any,
    order_notional: Any,
    taker_fee_bps: Any,
    execution_cost_bps: Any,
    expected_funding_bps: Any = 0,
    fill_efficiency: Any = "0.70",
) -> dict[str, float]:
    ref = dec(reference_price)
    step_frac = max(ZERO, dec(step_pct) / Decimal("100"))
    notional = max(ZERO, dec(order_notional))
    fill_eff = min(ONE, max(ZERO, dec(fill_efficiency, "0.70")))
    # A completed arithmetic-grid pair earns the full adjacent price interval.
    # ``fill_efficiency`` describes how much of the *opportunity set* may be
    # captured over a horizon; it must not haircut the P&L of a trade that has
    # already completed. Applying it here understated every completed pair and
    # made the published gross-profit field disagree with Bybit's formula.
    gross_bps = step_frac * BPS
    projected_capture_bps = gross_bps * fill_eff
    # Conservative guard: execution_cost_bps should already include round-trip
    # fees, spread and slippage. If a caller accidentally passes a lower value,
    # never let displayed grid economics ignore the configured taker fee floor.
    fee_floor_bps = max(ZERO, dec(taker_fee_bps)) * Decimal("2")
    exec_bps = max(max(ZERO, dec(execution_cost_bps)), fee_floor_bps)

    # ``expected_funding_bps`` is signed: positive means this side pays funding,
    # negative means it may receive funding. A recommendation must not become
    # executable merely because it assumes a funding receipt that can flip at the
    # next event or disappear with inventory changes. Keep the signed value for
    # diagnostics, but use only adverse funding cost in the canonical net edge.
    signed_funding_bps = dec(expected_funding_bps)
    funding_cost_bps = max(ZERO, signed_funding_bps)
    funding_benefit_excluded_bps = max(ZERO, -signed_funding_bps)

    net_bps = gross_bps - exec_bps - funding_cost_bps
    signed_net_bps = gross_bps - exec_bps - signed_funding_bps
    projected_net_bps = projected_capture_bps - exec_bps - funding_cost_bps
    gross_usdt = notional * gross_bps / BPS
    exec_cost_usdt = notional * exec_bps / BPS
    funding_cost_usdt = notional * funding_cost_bps / BPS
    signed_funding_usdt = notional * signed_funding_bps / BPS
    funding_benefit_excluded_usdt = notional * funding_benefit_excluded_bps / BPS
    net_usdt = gross_usdt - exec_cost_usdt - funding_cost_usdt
    signed_net_usdt = gross_usdt - exec_cost_usdt - signed_funding_usdt
    step_abs = ref * step_frac if ref > ZERO else ZERO
    return {
        "step_abs": as_float(step_abs),
        "gross_profit_bps": as_float(gross_bps),
        "fill_efficiency": as_float(fill_eff),
        "projected_capture_bps": as_float(projected_capture_bps),
        "projected_net_profit_bps": as_float(projected_net_bps),
        "execution_cost_bps": as_float(exec_bps),
        "expected_funding_bps": as_float(signed_funding_bps),
        "funding_cost_bps": as_float(funding_cost_bps),
        "funding_benefit_excluded_bps": as_float(funding_benefit_excluded_bps),
        "net_profit_bps": as_float(net_bps),
        "net_profit_with_signed_funding_bps": as_float(signed_net_bps),
        "gross_profit_usdt": as_float(gross_usdt),
        "execution_cost_usdt": as_float(exec_cost_usdt),
        "expected_funding_usdt": as_float(signed_funding_usdt),
        "funding_cost_usdt": as_float(funding_cost_usdt),
        "funding_benefit_excluded_usdt": as_float(funding_benefit_excluded_usdt),
        "net_profit_usdt": as_float(net_usdt),
        "net_profit_with_signed_funding_usdt": as_float(signed_net_usdt),
        "breakeven": bool(net_bps > ZERO and net_usdt > ZERO),
    }
