"""
Iteration 167 — Full Trading System Audit
==========================================
Comprehensive regression suite covering:
- Long/short TP/SL directional math
- PnL sign correctness for both directions  
- Bybit V5 order semantics (side, reduceOnly, closeOnTrigger, triggerDirection)
- Protective order geometry validation
- Kill-switch upper/lower directional mapping
- Neutral grid semantics (no directional TP)
- Risk/reward calculation and fail-closed geometry
- Instrument filter rounding (tickSize, qtyStep, minNotional)
- BTC beta alignment protection
- Shock guard directional gate presence
- UI JavaScript TP/SL directional correctness

All tests must pass without external dependencies (httpx, fastapi, pytest-asyncio).
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"function {name} not found in app.js"
    i = match.start()
    j = match.end()
    depth = 1
    while j < len(source) and depth:
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
        j += 1
    return source[i:j]


def _run_js(code: str) -> dict:
    result = subprocess.run(["node", "-e", code], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def _build_js_harness(*fn_names: str) -> str:
    source = APP_JS.read_text(encoding="utf-8")
    return "\n".join(_extract_js_function(source, n) for n in fn_names) + "\n"


# ─────────────────────────────────────────────────────────────────
# 1. Backend: Directional TP/SL mapping
# ─────────────────────────────────────────────────────────────────

def test_long_tp_is_upper_kill_switch_sl_is_lower() -> None:
    """Long: profit upward ⟹ TP = upper boundary, SL = lower boundary."""
    from app.trading_semantics import directional_exit_levels

    levels = directional_exit_levels("long", 95.0, 110.0)
    assert levels.take_profit == 110.0, f"long TP must be upper bound: {levels.take_profit}"
    assert levels.stop_loss == 95.0, f"long SL must be lower bound: {levels.stop_loss}"
    assert levels.has_directional_take_profit is True
    assert levels.direction == "long"


def test_short_tp_is_lower_kill_switch_sl_is_upper() -> None:
    """Short: profit downward ⟹ TP = lower boundary, SL = upper boundary."""
    from app.trading_semantics import directional_exit_levels

    levels = directional_exit_levels("short", 90.0, 105.0)
    assert levels.take_profit == 90.0, f"short TP must be lower bound: {levels.take_profit}"
    assert levels.stop_loss == 105.0, f"short SL must be upper bound: {levels.stop_loss}"
    assert levels.has_directional_take_profit is True
    assert levels.direction == "short"


def test_neutral_has_no_directional_tp() -> None:
    """Neutral grids must not emit a directional TP."""
    from app.trading_semantics import directional_exit_levels

    levels = directional_exit_levels("neutral", 90.0, 110.0)
    assert levels.take_profit is None, "neutral must not have directional TP"
    assert levels.has_directional_take_profit is False
    assert levels.kill_switch_lower == 90.0
    assert levels.kill_switch_upper == 110.0


# ─────────────────────────────────────────────────────────────────
# 2. Backend: Geometry validation
# ─────────────────────────────────────────────────────────────────

def test_long_tp_must_be_above_entry() -> None:
    from app.trading_semantics import validate_directional_exit_geometry

    errors = validate_directional_exit_geometry("long", 100.0, 110.0, 95.0)
    assert len(errors) == 0, f"valid long geometry: {errors}"

    errors_bad = validate_directional_exit_geometry("long", 100.0, 90.0, 95.0)
    assert any("LONG_TP" in e.get("code", "") for e in errors_bad), (
        f"long TP below entry must be rejected: {errors_bad}"
    )


def test_short_tp_must_be_below_entry() -> None:
    from app.trading_semantics import validate_directional_exit_geometry

    errors = validate_directional_exit_geometry("short", 100.0, 90.0, 110.0)
    assert len(errors) == 0, f"valid short geometry: {errors}"

    errors_bad = validate_directional_exit_geometry("short", 100.0, 110.0, 90.0)
    assert any("SHORT_TP" in e.get("code", "") for e in errors_bad), (
        f"short TP above entry must be rejected: {errors_bad}"
    )


def test_long_sl_must_be_below_entry() -> None:
    from app.trading_semantics import validate_directional_exit_geometry

    errors_bad = validate_directional_exit_geometry("long", 100.0, 110.0, 105.0)
    assert any("LONG_SL" in e.get("code", "") for e in errors_bad), (
        f"long SL above entry must be rejected: {errors_bad}"
    )


def test_short_sl_must_be_above_entry() -> None:
    from app.trading_semantics import validate_directional_exit_geometry

    errors_bad = validate_directional_exit_geometry("short", 100.0, 90.0, 95.0)
    assert any("SHORT_SL" in e.get("code", "") for e in errors_bad), (
        f"short SL below entry must be rejected: {errors_bad}"
    )


def test_tp_at_entry_is_rejected() -> None:
    from app.trading_semantics import validate_directional_exit_geometry

    errors = validate_directional_exit_geometry("long", 100.0, 100.0, 90.0)
    assert len(errors) > 0, "TP == entry for long should be rejected (not strictly above)"

    errors_s = validate_directional_exit_geometry("short", 100.0, 100.0, 110.0)
    assert len(errors_s) > 0, "TP == entry for short should be rejected (not strictly below)"


def test_geometry_neutral_always_passes() -> None:
    from app.trading_semantics import validate_directional_exit_geometry

    assert validate_directional_exit_geometry("neutral", 100.0, 90.0, 110.0) == []
    assert validate_directional_exit_geometry("neutral", 100.0, 110.0, 90.0) == []


# ─────────────────────────────────────────────────────────────────
# 3. Backend: PnL sign model
# ─────────────────────────────────────────────────────────────────

def test_long_profits_when_price_rises() -> None:
    from app.grid_math import linear_pnl_usdt
    from decimal import Decimal

    pnl = linear_pnl_usdt("long", qty=1.0, entry_price=100.0, exit_price=120.0)
    assert pnl == Decimal("20.0"), f"long profit on price rise: {pnl}"


def test_long_loses_when_price_falls() -> None:
    from app.grid_math import linear_pnl_usdt
    from decimal import Decimal

    pnl = linear_pnl_usdt("long", qty=1.0, entry_price=100.0, exit_price=80.0)
    assert pnl == Decimal("-20.0"), f"long loss on price fall: {pnl}"


def test_short_profits_when_price_falls() -> None:
    from app.grid_math import linear_pnl_usdt
    from decimal import Decimal

    pnl = linear_pnl_usdt("short", qty=1.0, entry_price=100.0, exit_price=80.0)
    assert pnl == Decimal("20.0"), f"short profit on price fall: {pnl}"


def test_short_loses_when_price_rises() -> None:
    from app.grid_math import linear_pnl_usdt
    from decimal import Decimal

    pnl = linear_pnl_usdt("short", qty=1.0, entry_price=100.0, exit_price=120.0)
    assert pnl == Decimal("-20.0"), f"short loss on price rise: {pnl}"


def test_pnl_symmetric_long_short() -> None:
    from app.grid_math import linear_pnl_usdt

    for move in (120.0, 80.0, 50.0, 150.0, 100.01):
        pnl_long = linear_pnl_usdt("long", qty=1.0, entry_price=100.0, exit_price=move)
        pnl_short = linear_pnl_usdt("short", qty=1.0, entry_price=100.0, exit_price=move)
        assert pnl_long == -pnl_short, (
            f"long and short PnL must be symmetric for exit={move}: "
            f"long={pnl_long}, short={pnl_short}"
        )


def test_pnl_fails_closed_on_bad_side() -> None:
    from app.grid_math import linear_pnl_usdt
    from decimal import Decimal

    assert linear_pnl_usdt("invalid", 1.0, 100.0, 120.0) == Decimal("0")
    assert linear_pnl_usdt("", 1.0, 100.0, 120.0) == Decimal("0")
    assert linear_pnl_usdt(None, 1.0, 100.0, 120.0) == Decimal("0")


def test_pnl_fails_closed_on_zero_qty_or_price() -> None:
    from app.grid_math import linear_pnl_usdt
    from decimal import Decimal

    assert linear_pnl_usdt("long", qty=0.0, entry_price=100.0, exit_price=120.0) == Decimal("0")
    assert linear_pnl_usdt("long", qty=1.0, entry_price=0.0, exit_price=120.0) == Decimal("0")
    assert linear_pnl_usdt("long", qty=-1.0, entry_price=100.0, exit_price=120.0) == Decimal("0")


# ─────────────────────────────────────────────────────────────────
# 4. Backend: directional_trade_math R/R calculation
# ─────────────────────────────────────────────────────────────────

def test_directional_trade_math_long_risk_reward() -> None:
    from app.trading_semantics import directional_trade_math

    m = directional_trade_math("long", 100.0, 115.0, 90.0, qty=2.0)
    assert m is not None
    assert m.gross_profit_usdt == 30.0, f"long profit: {m.gross_profit_usdt}"
    assert m.gross_loss_usdt == 20.0, f"long loss: {m.gross_loss_usdt}"
    assert abs(m.risk_reward - 1.5) < 1e-9, f"long RR: {m.risk_reward}"
    assert m.reward_pct > 0
    assert m.risk_pct > 0


def test_directional_trade_math_short_risk_reward() -> None:
    from app.trading_semantics import directional_trade_math

    m = directional_trade_math("short", 100.0, 85.0, 110.0, qty=2.0)
    assert m is not None
    assert m.gross_profit_usdt == 30.0, f"short profit: {m.gross_profit_usdt}"
    assert m.gross_loss_usdt == 20.0, f"short loss: {m.gross_loss_usdt}"
    assert abs(m.risk_reward - 1.5) < 1e-9, f"short RR: {m.risk_reward}"


def test_directional_trade_math_fails_closed_on_wrong_geometry() -> None:
    from app.trading_semantics import directional_trade_math

    # Long with TP below entry
    assert directional_trade_math("long", 100.0, 90.0, 95.0) is None
    # Short with TP above entry
    assert directional_trade_math("short", 100.0, 110.0, 90.0) is None
    # Long with SL above entry
    assert directional_trade_math("long", 100.0, 110.0, 105.0) is None
    # Short with SL below entry
    assert directional_trade_math("short", 100.0, 90.0, 85.0) is None


# ─────────────────────────────────────────────────────────────────
# 5. Backend: Bybit order semantics
# ─────────────────────────────────────────────────────────────────

def test_bybit_long_open_is_buy_not_reduce_only() -> None:
    from app.trading_semantics import bybit_linear_order_semantics

    s = bybit_linear_order_semantics("long", "open")
    assert s["side"] == "Buy"
    assert s["reduceOnly"] is False
    assert s["positionIdx"] == 0
    assert s["category"] == "linear"


def test_bybit_long_close_is_sell_reduce_only() -> None:
    from app.trading_semantics import bybit_linear_order_semantics

    s = bybit_linear_order_semantics("long", "close")
    assert s["side"] == "Sell"
    assert s["reduceOnly"] is True
    assert s["closeOnTrigger"] is True


def test_bybit_short_open_is_sell_not_reduce_only() -> None:
    from app.trading_semantics import bybit_linear_order_semantics

    s = bybit_linear_order_semantics("short", "open")
    assert s["side"] == "Sell"
    assert s["reduceOnly"] is False


def test_bybit_short_close_is_buy_reduce_only() -> None:
    from app.trading_semantics import bybit_linear_order_semantics

    s = bybit_linear_order_semantics("short", "close")
    assert s["side"] == "Buy"
    assert s["reduceOnly"] is True
    assert s["closeOnTrigger"] is True


def test_bybit_neutral_direction_raises() -> None:
    from app.trading_semantics import bybit_linear_order_semantics

    try:
        bybit_linear_order_semantics("neutral", "open")
        assert False, "neutral must raise ValueError"
    except ValueError:
        pass


# ─────────────────────────────────────────────────────────────────
# 6. Backend: Protective order trigger direction
# ─────────────────────────────────────────────────────────────────

def test_long_tp_trigger_direction_is_1_upward() -> None:
    from app.trading_semantics import bybit_linear_protective_order_semantics

    s = bybit_linear_protective_order_semantics("long", "take_profit")
    # Long TP triggered by upward price move (price rises to TP)
    assert s["triggerDirection"] == 1, f"long TP triggerDirection: {s['triggerDirection']}"
    assert s["side"] == "Sell"


def test_long_sl_trigger_direction_is_2_downward() -> None:
    from app.trading_semantics import bybit_linear_protective_order_semantics

    s = bybit_linear_protective_order_semantics("long", "stop_loss")
    # Long SL triggered by downward price move (price falls to SL)
    assert s["triggerDirection"] == 2, f"long SL triggerDirection: {s['triggerDirection']}"
    assert s["side"] == "Sell"


def test_short_tp_trigger_direction_is_2_downward() -> None:
    from app.trading_semantics import bybit_linear_protective_order_semantics

    s = bybit_linear_protective_order_semantics("short", "take_profit")
    # Short TP triggered by downward price move (price falls to TP)
    assert s["triggerDirection"] == 2, f"short TP triggerDirection: {s['triggerDirection']}"
    assert s["side"] == "Buy"


def test_short_sl_trigger_direction_is_1_upward() -> None:
    from app.trading_semantics import bybit_linear_protective_order_semantics

    s = bybit_linear_protective_order_semantics("short", "stop_loss")
    # Short SL triggered by upward price move (price rises to SL)
    assert s["triggerDirection"] == 1, f"short SL triggerDirection: {s['triggerDirection']}"
    assert s["side"] == "Buy"


def test_all_protective_orders_reduce_only() -> None:
    from app.trading_semantics import bybit_linear_protective_order_semantics

    for direction in ("long", "short"):
        for exit_kind in ("take_profit", "stop_loss"):
            s = bybit_linear_protective_order_semantics(direction, exit_kind)
            assert s["reduceOnly"] is True, (
                f"{direction} {exit_kind} must be reduceOnly=True"
            )
            assert s["closeOnTrigger"] is True, (
                f"{direction} {exit_kind} must be closeOnTrigger=True"
            )


# ─────────────────────────────────────────────────────────────────
# 7. Backend: Protective trigger geometry validation
# ─────────────────────────────────────────────────────────────────

def test_valid_protective_trigger_geometries() -> None:
    from app.trading_semantics import validate_protective_trigger_geometry

    valid_cases = [
        ("long", "take_profit", 100.0, 110.0),   # TP above entry
        ("long", "stop_loss", 100.0, 90.0),       # SL below entry
        ("short", "take_profit", 100.0, 90.0),    # TP below entry
        ("short", "stop_loss", 100.0, 110.0),     # SL above entry
    ]
    for direction, kind, ref, trigger in valid_cases:
        errs = validate_protective_trigger_geometry(direction, kind, ref, trigger)
        assert len(errs) == 0, (
            f"{direction} {kind} ref={ref} trigger={trigger}: {errs}"
        )


def test_invalid_protective_trigger_geometries_rejected() -> None:
    from app.trading_semantics import validate_protective_trigger_geometry

    invalid_cases = [
        ("long", "take_profit", 100.0, 90.0),    # TP below entry for long
        ("long", "stop_loss", 100.0, 110.0),      # SL above entry for long
        ("short", "take_profit", 100.0, 110.0),   # TP above entry for short
        ("short", "stop_loss", 100.0, 90.0),      # SL below entry for short
    ]
    for direction, kind, ref, trigger in invalid_cases:
        errs = validate_protective_trigger_geometry(direction, kind, ref, trigger)
        assert len(errs) > 0, (
            f"{direction} {kind} ref={ref} trigger={trigger} should be rejected"
        )


def test_protective_plan_geometry_valid_field() -> None:
    from app.trading_semantics import bybit_linear_protective_order_plan

    plan_ok = bybit_linear_protective_order_plan("short", "take_profit", 90.0, 100.0)
    assert plan_ok["geometry_valid"] is True, f"valid short TP plan: {plan_ok}"

    plan_bad = bybit_linear_protective_order_plan("short", "take_profit", 110.0, 100.0)
    assert plan_bad["geometry_valid"] is False, f"inverted short TP should fail: {plan_bad}"


def test_protective_plan_missing_reference_fails_closed() -> None:
    from app.trading_semantics import bybit_linear_protective_order_plan

    plan = bybit_linear_protective_order_plan("long", "take_profit", 110.0, None)
    assert plan["geometry_valid"] is False
    codes = [e.get("code", "") for e in plan["geometry_errors"]]
    assert any("REFERENCE" in c.upper() for c in codes), (
        f"missing reference should raise REFERENCE error: {codes}"
    )


# ─────────────────────────────────────────────────────────────────
# 8. Backend: Instrument filter rounding
# ─────────────────────────────────────────────────────────────────

def test_tick_rounding_down_never_exceeds_lower_bound() -> None:
    from app.grid_math import quantize_step

    cases = [
        (43215.7, 0.10), (2345.678, 0.01), (0.17832, 0.00001),
        (99.999, 0.01), (100.001, 0.10),
    ]
    for price, tick in cases:
        q = quantize_step(price, tick, mode="down")
        assert q is not None
        assert float(q) <= price + 1e-10, (
            f"round_down({price}, {tick}) = {q} > {price}"
        )


def test_tick_rounding_up_never_below_upper_bound() -> None:
    from app.grid_math import quantize_step

    cases = [
        (43215.7, 0.10), (2345.678, 0.01), (0.17832, 0.00001),
        (99.999, 0.01), (100.001, 0.01),
    ]
    for price, tick in cases:
        q = quantize_step(price, tick, mode="up")
        assert q is not None
        assert float(q) >= price - 1e-10, (
            f"round_up({price}, {tick}) = {q} < {price}"
        )


def test_qty_step_rounding_up_never_below_min() -> None:
    from app.grid_math import quantize_step

    raw_qty = 0.00215
    qty_step = 0.001
    q = quantize_step(raw_qty, qty_step, mode="up")
    assert q is not None
    assert abs(float(q) - 0.003) < 1e-9, f"qty step up: {q}"


def test_zero_or_negative_step_returns_none() -> None:
    from app.grid_math import quantize_step

    assert quantize_step(100.0, 0.0) is None
    assert quantize_step(100.0, -0.01) is None


# ─────────────────────────────────────────────────────────────────
# 9. Backend: Liquidation price direction
# ─────────────────────────────────────────────────────────────────

def test_long_liquidation_below_entry() -> None:
    from app.grid_math import estimate_linear_liq_price

    liq = estimate_linear_liq_price("long", entry_price=100.0, leverage=5)
    assert liq is not None, "long liquidation price must be defined"
    assert float(liq) < 100.0, f"long liq must be below entry: {liq}"


def test_short_liquidation_above_entry() -> None:
    from app.grid_math import estimate_linear_liq_price

    liq = estimate_linear_liq_price("short", entry_price=100.0, leverage=5)
    assert liq is not None, "short liquidation price must be defined"
    assert float(liq) > 100.0, f"short liq must be above entry: {liq}"


def test_unknown_side_returns_none_for_liquidation() -> None:
    from app.grid_math import estimate_linear_liq_price

    assert estimate_linear_liq_price("neutral", 100.0, 5) is None
    assert estimate_linear_liq_price("", 100.0, 5) is None
    assert estimate_linear_liq_price(None, 100.0, 5) is None


def test_liquidation_buffer_positive_for_valid_positions() -> None:
    from app.grid_math import estimate_linear_liq_price, liquidation_buffer_pct

    liq_long = estimate_linear_liq_price("long", 100.0, 5)
    buf_long = liquidation_buffer_pct("long", 100.0, liq_long)
    assert buf_long is not None and float(buf_long) > 0

    liq_short = estimate_linear_liq_price("short", 100.0, 5)
    buf_short = liquidation_buffer_pct("short", 100.0, liq_short)
    assert buf_short is not None and float(buf_short) > 0


# ─────────────────────────────────────────────────────────────────
# 10. Backend: Funding cashflow direction
# ─────────────────────────────────────────────────────────────────

def test_long_pays_positive_funding_when_rate_positive() -> None:
    from app.grid_math import funding_cashflow_usdt

    # Positive funding rate: longs pay, shorts receive
    flow = funding_cashflow_usdt("long", position_notional=1000.0, funding_rate=0.0001)
    assert float(flow) > 0, f"long pays positive funding: {flow}"


def test_short_receives_funding_when_rate_positive() -> None:
    from app.grid_math import funding_cashflow_usdt

    flow = funding_cashflow_usdt("short", position_notional=1000.0, funding_rate=0.0001)
    assert float(flow) < 0, f"short receives when positive rate: {flow}"


def test_unknown_side_returns_zero_funding() -> None:
    from app.grid_math import funding_cashflow_usdt
    from decimal import Decimal

    assert funding_cashflow_usdt("neutral", 1000.0, 0.0001) == Decimal("0")


# ─────────────────────────────────────────────────────────────────
# 11. Backend: Risk limits normalization
# ─────────────────────────────────────────────────────────────────

def test_risk_limits_min_leverage_never_exceeds_max() -> None:
    from app.risk import normalize_risk_limits

    limits = normalize_risk_limits({"min_leverage": 10, "max_leverage": 3})
    assert limits["min_leverage"] <= limits["max_leverage"], (
        f"min_leverage {limits['min_leverage']} > max_leverage {limits['max_leverage']}"
    )


def test_risk_limits_concurrent_bots_capped_at_50() -> None:
    from app.risk import normalize_risk_limits

    limits = normalize_risk_limits({"max_concurrent_bots": 999})
    assert limits["max_concurrent_bots"] <= 50


def test_risk_limits_negative_daily_dd_clamped() -> None:
    from app.risk import normalize_risk_limits

    limits = normalize_risk_limits({"max_daily_dd_usdt": -500})
    assert limits["max_daily_dd_usdt"] >= 0


def test_risk_limits_nan_values_replaced_by_defaults() -> None:
    from app.risk import normalize_risk_limits

    limits = normalize_risk_limits({
        "max_daily_dd_usdt": float("nan"),
        "max_leverage": float("inf"),
    })
    assert math.isfinite(limits["max_daily_dd_usdt"])
    assert math.isfinite(limits["max_leverage"])


# ─────────────────────────────────────────────────────────────────
# 12. Backend: BTC beta alignment protection
# ─────────────────────────────────────────────────────────────────

def test_btc_beta_returns_none_for_nan_in_active_window() -> None:
    from app.features import btc_beta

    sym = [100.0 + i for i in range(30)]
    btc = [200.0 + i * 2.0 for i in range(30)]
    btc[-3] = float("nan")

    r = btc_beta(sym, btc, window=24)
    assert r["correlation"] is None, "NaN in BTC window should fail closed"


def test_btc_beta_returns_none_for_zero_price_in_symbol() -> None:
    from app.features import btc_beta

    sym = [100.0 + i for i in range(30)]
    sym[-7] = 0.0
    btc = [200.0 + i * 2.0 for i in range(30)]

    r = btc_beta(sym, btc, window=24)
    assert r["correlation"] is None, "Zero price in symbol window should fail closed"


def test_btc_beta_valid_case() -> None:
    from app.features import btc_beta

    sym = [100.0 + i for i in range(30)]
    btc = [200.0 + i * 2.0 for i in range(30)]

    r = btc_beta(sym, btc, window=24)
    assert r["correlation"] is not None
    assert abs(r["correlation"] - 1.0) < 0.01, f"perfect correlation expected: {r['correlation']}"


# ─────────────────────────────────────────────────────────────────
# 13. JS: Frontend TP/SL directional mapping
# ─────────────────────────────────────────────────────────────────

def _js_exit_levels_harness() -> str:
    fns = (
        "toFiniteNumber", "countDecimalsFromStep", "inferPriceDecimals",
        "quantizeByStep", "formatBybitPrice", "operatorExitLevels",
        "directionalExitGeometryOk", "operatorExitLevelsFromBackend",
    )
    return _build_js_harness(*fns)


def test_js_short_tp_is_lower_kill_switch() -> None:
    harness = _js_exit_levels_harness()
    code = harness + """
const r = operatorExitLevels('short', 95, 105);
console.log(JSON.stringify({tp: r.takeProfitValue, sl: r.stopLossValue}));
"""
    out = _run_js(code)
    assert out["tp"] == 95, f"short TP must be lower bound: {out}"
    assert out["sl"] == 105, f"short SL must be upper bound: {out}"


def test_js_long_tp_is_upper_kill_switch() -> None:
    harness = _js_exit_levels_harness()
    code = harness + """
const r = operatorExitLevels('long', 95, 105);
console.log(JSON.stringify({tp: r.takeProfitValue, sl: r.stopLossValue}));
"""
    out = _run_js(code)
    assert out["tp"] == 105, f"long TP must be upper bound: {out}"
    assert out["sl"] == 95, f"long SL must be lower bound: {out}"


def test_js_neutral_has_no_directional_tp() -> None:
    harness = _js_exit_levels_harness()
    code = harness + """
const r = operatorExitLevels('neutral', 95, 105);
console.log(JSON.stringify({label: r.takeProfitLabel}));
"""
    out = _run_js(code)
    assert "не применяется" in out["label"].lower(), (
        f"neutral must not show directional TP: {out}"
    )


def test_js_geometry_validation_long_ok() -> None:
    harness = _js_exit_levels_harness()
    code = harness + """
console.log(JSON.stringify({
    longOk: directionalExitGeometryOk('long', 110, 90, 100),
    longBad: directionalExitGeometryOk('long', 90, 110, 100)
}));
"""
    out = _run_js(code)
    assert out["longOk"] is True
    assert out["longBad"] is False


def test_js_geometry_validation_short_ok() -> None:
    harness = _js_exit_levels_harness()
    code = harness + """
console.log(JSON.stringify({
    shortOk: directionalExitGeometryOk('short', 90, 110, 100),
    shortBad: directionalExitGeometryOk('short', 110, 90, 100)
}));
"""
    out = _run_js(code)
    assert out["shortOk"] is True
    assert out["shortBad"] is False


def test_js_backend_short_exit_levels_render_correctly() -> None:
    harness = _js_exit_levels_harness()
    code = harness + """
const bk = {
    direction: 'short',
    take_profit: 95,
    stop_loss: 105,
    kill_switch_lower: 95,
    kill_switch_upper: 105,
    has_directional_take_profit: true,
    geometry_valid: true,
    reference_price: 100
};
const r = operatorExitLevelsFromBackend(bk, {}, {});
console.log(JSON.stringify({tp: Number(r.takeProfitValue), sl: Number(r.stopLossValue)}));
"""
    out = _run_js(code)
    assert out["tp"] == 95, f"backend short TP must be 95: {out}"
    assert out["sl"] == 105, f"backend short SL must be 105: {out}"


def test_js_invalid_backend_geometry_falls_back() -> None:
    harness = _js_exit_levels_harness()
    code = harness + """
const bk = {
    direction: 'short',
    take_profit: 110,
    stop_loss: 90,
    has_directional_take_profit: true,
    geometry_valid: false,
    reference_price: 100
};
const r = operatorExitLevelsFromBackend(bk, {takeProfitValue: '—'}, {});
console.log(JSON.stringify({tp: r.takeProfitValue}));
"""
    out = _run_js(code)
    assert out["tp"] == "—", f"inverted backend geometry must fall back: {out}"


# ─────────────────────────────────────────────────────────────────
# 14. JS: Tick rounding preserves directional boundaries
# ─────────────────────────────────────────────────────────────────

def test_js_tick_round_up_not_shrunk() -> None:
    harness = _build_js_harness(
        "toFiniteNumber", "countDecimalsFromStep", "inferPriceDecimals",
        "quantizeByStep", "formatBybitPrice",
    )
    code = harness + """
const meta = {tick_size: 0.01};
console.log(JSON.stringify({
    up: formatBybitPrice(100.001, meta, 'up'),
    down: formatBybitPrice(99.999, meta, 'down')
}));
"""
    out = _run_js(code)
    assert out["up"] == "100.01", f"round up must not shrink upper bound: {out}"
    assert out["down"] == "99.99", f"round down must not shrink lower bound: {out}"


def test_js_quantize_uses_tick_division_not_pre_rounding() -> None:
    """Verify the fix: quantizeByStep divides by tick first, not pre-rounds the value."""
    source = APP_JS.read_text(encoding="utf-8")
    assert "const unitsRaw = v / tick;" in source, (
        "quantizeByStep must divide by tick first"
    )
    assert "Math.ceil(unitsRaw - eps)" in source, (
        "ceil must operate on tick units, not pre-rounded value"
    )
    assert "Math.floor(unitsRaw + eps)" in source, (
        "floor must operate on tick units, not pre-rounded value"
    )
    assert "const scaledValue = Math.round(v * factor);" not in source, (
        "old pre-rounding pattern must be removed"
    )


# ─────────────────────────────────────────────────────────────────
# 15. Code structure: directional model is canonical and shared
# ─────────────────────────────────────────────────────────────────

def test_trading_semantics_is_imported_in_main() -> None:
    """Ensure main.py imports from the canonical trading_semantics module."""
    src = (ROOT / "app" / "main.py").read_text()
    assert "from .trading_semantics import" in src or "from app.trading_semantics import" in src, (
        "main.py must import trading_semantics for canonical TP/SL"
    )
    assert "directional_exit_levels" in src
    assert "validate_directional_exit_geometry" in src


def test_js_exit_levels_function_present_and_directional() -> None:
    """Ensure operatorExitLevels in app.js handles short direction correctly."""
    source = APP_JS.read_text(encoding="utf-8")
    assert "function operatorExitLevels" in source
    # Check that short branch assigns TP to kill_switch_lower
    fn = _extract_js_function(source, "operatorExitLevels")
    assert "killLower" in fn and "killUpper" in fn, "function must use both kill switch bounds"
    # Short block
    short_idx = fn.find('"short"')
    assert short_idx >= 0, "short case must exist"
    # The short block must have takeProfitValue: killLower
    short_block = fn[short_idx:short_idx + 300]
    assert "killLower" in short_block, "short TP must reference killLower"


def test_static_asset_version_at_least_v31() -> None:
    """Ensure static asset version is >= v31 (tick rounding fix was at v31)."""
    source = (ROOT / "app" / "ui" / "static" / "index.html").read_text()
    matches = re.findall(r"manual-ui-v(\d+)", source)
    assert matches, "index.html must contain a cache-bust version string"
    version = max(int(m) for m in matches)
    assert version >= 31, f"UI version must be >= 31 (tick fix), got v{version}"
