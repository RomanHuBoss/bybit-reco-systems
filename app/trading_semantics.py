"""Canonical trading-direction semantics for Bybit linear futures guidance.

The project is an operator/recommendation layer, not a live OMS.  Still, every
place that renders or validates long/short exits must share one small source of
truth so a UI label cannot drift away from the backend risk model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
import math

SUPPORTED_EXECUTION_DIRECTIONS = {"long", "short", "neutral"}
DIRECTIONAL_EXECUTION_DIRECTIONS = {"long", "short"}


def normalize_execution_direction(value: Any) -> str | None:
    direction = str(value or "").strip().lower()
    return direction if direction in SUPPORTED_EXECUTION_DIRECTIONS else None


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return float(number)


@dataclass(frozen=True)
class DirectionalExitLevels:
    direction: str
    take_profit: float | None
    stop_loss: float | None
    kill_switch_lower: float | None
    kill_switch_upper: float | None
    take_profit_label: str
    stop_loss_label: str
    geometry: str
    has_directional_take_profit: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def directional_exit_levels(direction: Any, kill_switch_lower: Any, kill_switch_upper: Any) -> DirectionalExitLevels:
    """Map outer kill-switch bounds to directional operator exit levels.

    Long positions profit upward and lose downward, so TP is the upper boundary
    and SL is the lower boundary.  Shorts are the exact opposite.  Neutral grids
    do not have a single directional TP; both outer bounds are kill-switch exits.
    """
    direction_norm = normalize_execution_direction(direction) or "neutral"
    lower = _finite_float(kill_switch_lower)
    upper = _finite_float(kill_switch_upper)
    if direction_norm == "short":
        return DirectionalExitLevels(
            direction="short",
            take_profit=lower,
            stop_loss=upper,
            kill_switch_lower=lower,
            kill_switch_upper=upper,
            take_profit_label="Take Profit",
            stop_loss_label="Stop Loss",
            geometry="short: TP ниже entry/range, SL выше entry/range",
            has_directional_take_profit=True,
        )
    if direction_norm == "long":
        return DirectionalExitLevels(
            direction="long",
            take_profit=upper,
            stop_loss=lower,
            kill_switch_lower=lower,
            kill_switch_upper=upper,
            take_profit_label="Take Profit",
            stop_loss_label="Stop Loss",
            geometry="long: TP выше entry/range, SL ниже entry/range",
            has_directional_take_profit=True,
        )
    return DirectionalExitLevels(
        direction="neutral",
        take_profit=None,
        stop_loss=None,
        kill_switch_lower=lower,
        kill_switch_upper=upper,
        take_profit_label="Directional TP unavailable",
        stop_loss_label="Kill-switch lower / upper",
        geometry="neutral: направленного TP нет; оба внешних уровня являются kill-switch exits",
        has_directional_take_profit=False,
    )


def validate_directional_exit_geometry(direction: Any, entry_price: Any, take_profit: Any, stop_loss: Any) -> list[dict[str, str]]:
    """Return fail-closed geometry violations for directional TP/SL levels."""
    direction_norm = normalize_execution_direction(direction)
    entry = _finite_float(entry_price)
    tp = _finite_float(take_profit)
    sl = _finite_float(stop_loss)
    errors: list[dict[str, str]] = []
    if direction_norm not in DIRECTIONAL_EXECUTION_DIRECTIONS:
        return errors
    if entry is None or entry <= 0:
        errors.append({"code": "DIRECTIONAL_ENTRY_PRICE_MISSING", "msg": "directional TP/SL geometry requires a positive finite entry/reference price."})
        return errors
    if tp is None or tp <= 0:
        errors.append({"code": "DIRECTIONAL_TP_MISSING", "msg": "directional Take Profit must be a positive finite price."})
    if sl is None or sl <= 0:
        errors.append({"code": "DIRECTIONAL_SL_MISSING", "msg": "directional Stop Loss must be a positive finite price."})
    if tp is None or tp <= 0 or sl is None or sl <= 0:
        return errors

    if direction_norm == "long":
        if tp <= entry:
            errors.append({"code": "LONG_TP_NOT_ABOVE_ENTRY", "msg": f"long Take Profit={tp} must be above entry/reference={entry}."})
        if sl >= entry:
            errors.append({"code": "LONG_SL_NOT_BELOW_ENTRY", "msg": f"long Stop Loss={sl} must be below entry/reference={entry}."})
    elif direction_norm == "short":
        if tp >= entry:
            errors.append({"code": "SHORT_TP_NOT_BELOW_ENTRY", "msg": f"short Take Profit={tp} must be below entry/reference={entry}."})
        if sl <= entry:
            errors.append({"code": "SHORT_SL_NOT_ABOVE_ENTRY", "msg": f"short Stop Loss={sl} must be above entry/reference={entry}."})
    return errors


@dataclass(frozen=True)
class DirectionalTradeMath:
    """Executable long/short price semantics for one-way linear USDT positions.

    Profit and loss are returned as positive magnitudes in USDT.  Invalid or
    swapped TP/SL geometry is rejected by returning ``None`` from the factory
    below instead of silently producing a negative risk/reward value.
    """

    direction: str
    entry_price: float
    take_profit: float
    stop_loss: float
    qty: float
    gross_profit_usdt: float
    gross_loss_usdt: float
    reward_pct: float
    risk_pct: float
    risk_reward: float | None
    take_profit_distance_pct: float
    stop_loss_distance_pct: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def directional_trade_math(
    direction: Any,
    entry_price: Any,
    take_profit: Any,
    stop_loss: Any,
    qty: Any = 1.0,
) -> DirectionalTradeMath | None:
    """Return canonical PnL/risk/reward semantics for a directional trade.

    Long: TP must be above entry and SL below entry.  Short: TP must be below
    entry and SL above entry.  The function is intentionally fail-closed so any
    UI, test, or future execution adapter can share the same sign model.
    """
    direction_norm = normalize_execution_direction(direction)
    if direction_norm not in DIRECTIONAL_EXECUTION_DIRECTIONS:
        return None

    entry = _finite_float(entry_price)
    tp = _finite_float(take_profit)
    sl = _finite_float(stop_loss)
    quantity = _finite_float(qty)
    if entry is None or tp is None or sl is None or quantity is None:
        return None
    if entry <= 0 or tp <= 0 or sl <= 0 or quantity <= 0:
        return None
    if validate_directional_exit_geometry(direction_norm, entry, tp, sl):
        return None

    if direction_norm == "long":
        profit_per_unit = tp - entry
        loss_per_unit = entry - sl
    else:
        profit_per_unit = entry - tp
        loss_per_unit = sl - entry

    if profit_per_unit <= 0 or loss_per_unit <= 0:
        return None

    gross_profit = float(profit_per_unit * quantity)
    gross_loss = float(loss_per_unit * quantity)
    notional = float(entry * quantity)
    reward_pct = float(gross_profit / notional * 100.0)
    risk_pct = float(gross_loss / notional * 100.0)
    risk_reward = float(gross_profit / gross_loss) if gross_loss > 0 else None

    return DirectionalTradeMath(
        direction=direction_norm,
        entry_price=float(entry),
        take_profit=float(tp),
        stop_loss=float(sl),
        qty=float(quantity),
        gross_profit_usdt=gross_profit,
        gross_loss_usdt=gross_loss,
        reward_pct=reward_pct,
        risk_pct=risk_pct,
        risk_reward=risk_reward,
        take_profit_distance_pct=reward_pct,
        stop_loss_distance_pct=risk_pct,
    )


def bybit_linear_order_semantics(direction: Any, action: str) -> dict[str, Any]:
    """Canonical one-way Bybit V5 side/reduceOnly mapping for directional orders.

    This helper is intentionally small and deterministic.  It is suitable for
    tests, preflight documentation and any future execution adapter.  Neutral
    grids must not be converted into a single directional order by this helper.
    """
    direction_norm = normalize_execution_direction(direction)
    action_norm = str(action or "").strip().lower()
    if direction_norm not in DIRECTIONAL_EXECUTION_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(DIRECTIONAL_EXECUTION_DIRECTIONS)}, got {direction!r}")
    if action_norm not in {"open", "close"}:
        raise ValueError("action must be 'open' or 'close'")

    if action_norm == "open":
        side = "Buy" if direction_norm == "long" else "Sell"
        reduce_only = False
    else:
        side = "Sell" if direction_norm == "long" else "Buy"
        reduce_only = True

    return {
        "category": "linear",
        "position_mode": "one_way",
        "positionIdx": 0,
        "direction": direction_norm,
        "action": action_norm,
        "side": side,
        "reduceOnly": reduce_only,
        "closeOnTrigger": bool(reduce_only),
    }


def _normalize_exit_kind(exit_kind: Any) -> str:
    exit_norm = str(exit_kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "tp": "take_profit",
        "takeprofit": "take_profit",
        "take_profit": "take_profit",
        "sl": "stop_loss",
        "stoploss": "stop_loss",
        "stop_loss": "stop_loss",
    }
    purpose = aliases.get(exit_norm)
    if purpose is None:
        raise ValueError("exit_kind must be one of: take_profit, stop_loss")
    return purpose


def _protective_trigger_direction(direction: str, purpose: str) -> int:
    # Bybit V5 requires triggerDirection for linear/inverse conditional orders:
    # 1 means the trigger price is reached on an upward move, 2 on a downward move.
    # Long TP and short SL trigger on rises; short TP and long SL trigger on falls.
    return 1 if (direction == "long") == (purpose == "take_profit") else 2


def bybit_linear_protective_order_semantics(direction: Any, exit_kind: str) -> dict[str, Any]:
    """Canonical one-way Bybit V5 semantics for TP/SL protective exits.

    A protective TP/SL must always reduce or close the existing position.  It
    must therefore use the same side as a close order, set reduceOnly and
    closeOnTrigger, and never be allowed to increase exposure.
    """
    purpose = _normalize_exit_kind(exit_kind)
    semantics = bybit_linear_order_semantics(direction, "close")
    semantics.update({
        "exit_kind": purpose,
        # Do not emit Bybit V5 `orderFilter` here: the official Place Order
        # schema marks Order/StopOrder/tpslOrder filters as spot-only.  Linear
        # perps/futures conditional exits are identified by triggerPrice plus
        # triggerDirection/triggerBy, while reduceOnly+closeOnTrigger prevents a
        # protective exit from increasing or flipping exposure.
        "triggerPurpose": "takeProfit" if purpose == "take_profit" else "stopLoss",
        "triggerDirection": _protective_trigger_direction(semantics["direction"], purpose),
        "triggerBy": "LastPrice",
        "orderType": "Market",
        "reduceOnly": True,
        "closeOnTrigger": True,
    })
    return semantics


def validate_protective_trigger_geometry(
    direction: Any,
    exit_kind: Any,
    reference_price: Any,
    trigger_price: Any,
) -> list[dict[str, str]]:
    """Validate a concrete Bybit protective trigger against one-way direction.

    This is stricter than generic order-side mapping: it catches the dangerous
    class of bugs where short TP/SL prices or triggerDirection are visually or
    programmatically inverted while the reduce-only side still looks plausible.
    """
    direction_norm = normalize_execution_direction(direction)
    purpose = _normalize_exit_kind(exit_kind)
    ref = _finite_float(reference_price)
    trigger = _finite_float(trigger_price)
    errors: list[dict[str, str]] = []

    if direction_norm not in DIRECTIONAL_EXECUTION_DIRECTIONS:
        errors.append({"code": "PROTECTIVE_DIRECTION_INVALID", "msg": "protective TP/SL requires long or short direction."})
        return errors
    if ref is None or ref <= 0:
        errors.append({"code": "PROTECTIVE_REFERENCE_PRICE_INVALID", "msg": "protective TP/SL requires a positive finite reference price."})
        return errors
    if trigger is None or trigger <= 0:
        errors.append({"code": "PROTECTIVE_TRIGGER_PRICE_INVALID", "msg": "protective TP/SL requires a positive finite trigger price."})
        return errors

    if direction_norm == "long" and purpose == "take_profit" and trigger <= ref:
        errors.append({"code": "LONG_TP_TRIGGER_NOT_ABOVE_REFERENCE", "msg": f"long TP trigger={trigger} must be above reference={ref}."})
    elif direction_norm == "long" and purpose == "stop_loss" and trigger >= ref:
        errors.append({"code": "LONG_SL_TRIGGER_NOT_BELOW_REFERENCE", "msg": f"long SL trigger={trigger} must be below reference={ref}."})
    elif direction_norm == "short" and purpose == "take_profit" and trigger >= ref:
        errors.append({"code": "SHORT_TP_TRIGGER_NOT_BELOW_REFERENCE", "msg": f"short TP trigger={trigger} must be below reference={ref}."})
    elif direction_norm == "short" and purpose == "stop_loss" and trigger <= ref:
        errors.append({"code": "SHORT_SL_TRIGGER_NOT_ABOVE_REFERENCE", "msg": f"short SL trigger={trigger} must be above reference={ref}."})

    expected_direction = _protective_trigger_direction(direction_norm, purpose)
    moves_up = trigger > ref
    if (expected_direction == 1 and not moves_up) or (expected_direction == 2 and moves_up):
        errors.append({
            "code": "PROTECTIVE_TRIGGER_DIRECTION_MISMATCH",
            "msg": f"triggerDirection={expected_direction} is inconsistent with trigger={trigger} and reference={ref}.",
        })
    return errors


def bybit_linear_protective_order_plan(
    direction: Any,
    exit_kind: Any,
    trigger_price: Any,
    reference_price: Any | None = None,
) -> dict[str, Any]:
    """Return executable protective-order intent plus fail-closed geometry flags.

    The returned structure is intentionally UI/API-safe: it exposes the Bybit
    one-way close side, triggerDirection and triggerPrice that correspond to the
    same canonical TP/SL model used for backend validation.
    """
    semantics = bybit_linear_protective_order_semantics(direction, str(exit_kind))
    trigger = _finite_float(trigger_price)
    ref = _finite_float(reference_price)
    geometry_errors: list[dict[str, str]] = []
    if ref is None or ref <= 0:
        geometry_errors.append({
            "code": "PROTECTIVE_REFERENCE_PRICE_INVALID",
            "msg": "protective TP/SL requires a positive finite reference price to prove the trigger is on the correct side of entry.",
        })
        if trigger is None or trigger <= 0:
            geometry_errors.append({"code": "PROTECTIVE_TRIGGER_PRICE_INVALID", "msg": "protective TP/SL requires a positive finite trigger price."})
    else:
        geometry_errors = validate_protective_trigger_geometry(
            semantics["direction"],
            semantics["exit_kind"],
            ref,
            trigger,
        )

    plan = dict(semantics)
    plan.update({
        "triggerPrice": trigger,
        "reference_price": ref,
        "geometry_valid": len(geometry_errors) == 0,
        "geometry_errors": geometry_errors,
    })
    return plan

