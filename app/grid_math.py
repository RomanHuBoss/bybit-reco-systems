"""Conservative linear-USDT futures grid economics.

The helpers in this module intentionally avoid exchange-specific hidden state
(wallet balance, current maintenance tier, live mark price). They are suitable
for recommendation/preflight estimates and must be treated as conservative
operator guidance, not as Bybit's exact liquidation engine.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, InvalidOperation, getcontext
from typing import Any

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
    side_norm = str(side or "").strip().lower()
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
    if isinstance(events, bool):
        n_events = 0
    else:
        try:
            n_events = max(0, int(events))
        except Exception:
            n_events = 0
    side_norm = str(side or "").strip().lower()
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
    side_norm = str(side or "").strip().lower()
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
    side_norm = str(side or "").strip().lower()
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
    gross_bps = step_frac * BPS * fill_eff
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
