from __future__ import annotations

import pytest

from app.features import funding_signal
from app.grid_math import (
    funding_cashflow_usdt,
    grid_leg_economics,
    linear_pnl_usdt,
    margin_required_usdt,
    estimate_linear_liq_price,
    liquidation_buffer_pct,
)


def test_linear_usdt_pnl_long_and_short_are_settled_in_usdt() -> None:
    assert linear_pnl_usdt("long", "0.5", "10000", "10100") == pytest.approx(50)
    assert linear_pnl_usdt("short", "0.5", "10000", "9900") == pytest.approx(50)
    assert linear_pnl_usdt("short", "0.5", "10000", "10100") == pytest.approx(-50)


def test_grid_leg_economics_is_net_of_execution_costs_and_funding() -> None:
    econ = grid_leg_economics(
        reference_price="100",
        step_pct="0.60",
        order_notional="25",
        taker_fee_bps="5.5",
        execution_cost_bps="16",
        expected_funding_bps="2",
        fill_efficiency="0.70",
    )
    assert econ["gross_profit_bps"] == pytest.approx(42.0)
    assert econ["net_profit_bps"] == pytest.approx(24.0)
    assert econ["net_profit_usdt"] == pytest.approx(0.06)
    assert econ["breakeven"] is True


def test_grid_leg_economics_rejects_fee_dominated_grid() -> None:
    econ = grid_leg_economics(
        reference_price="100",
        step_pct="0.10",
        order_notional="25",
        taker_fee_bps="5.5",
        execution_cost_bps="16",
        expected_funding_bps="0",
        fill_efficiency="0.70",
    )
    assert econ["gross_profit_bps"] == pytest.approx(7.0)
    assert econ["net_profit_bps"] < 0
    assert econ["breakeven"] is False



def test_grid_leg_economics_applies_taker_fee_floor_when_execution_cost_is_missing() -> None:
    econ = grid_leg_economics(
        reference_price="100",
        step_pct="0.20",
        order_notional="100",
        taker_fee_bps="6",
        execution_cost_bps="0",
        expected_funding_bps="0",
        fill_efficiency="1",
    )
    assert econ["gross_profit_bps"] == pytest.approx(20.0)
    assert econ["execution_cost_bps"] == pytest.approx(12.0)
    assert econ["net_profit_bps"] == pytest.approx(8.0)




def test_grid_leg_economics_does_not_approve_from_funding_receipt_windfall() -> None:
    econ = grid_leg_economics(
        reference_price="100",
        step_pct="0.10",
        order_notional="100",
        taker_fee_bps="5.5",
        execution_cost_bps="16",
        expected_funding_bps="-10",
        fill_efficiency="1",
    )

    assert econ["gross_profit_bps"] == pytest.approx(10.0)
    assert econ["funding_benefit_excluded_bps"] == pytest.approx(10.0)
    assert econ["funding_cost_bps"] == pytest.approx(0.0)
    assert econ["net_profit_with_signed_funding_bps"] == pytest.approx(4.0)
    assert econ["net_profit_bps"] == pytest.approx(-6.0)
    assert econ["breakeven"] is False


def test_funding_cashflow_is_directional_for_linear_contracts() -> None:
    assert float(funding_cashflow_usdt("long", "1000", "0.0001", 2)) == pytest.approx(0.2)
    assert float(funding_cashflow_usdt("short", "1000", "0.0001", 2)) == pytest.approx(-0.2)


def test_margin_and_liquidation_buffer_are_conservative_estimates() -> None:
    assert float(margin_required_usdt("1000", "5")) == pytest.approx(200)
    liq = estimate_linear_liq_price("long", "100", "5")
    assert float(liq) == pytest.approx(80.6)
    assert float(liquidation_buffer_pct("long", "100", liq)) == pytest.approx(19.4)


def test_liquidation_helpers_fail_closed_on_unknown_side() -> None:
    assert estimate_linear_liq_price("neutral", "100", "5") is None
    assert liquidation_buffer_pct("neutral", "100", "80") is None


def test_funding_signal_annualizes_by_bybit_interval() -> None:
    one_hour = funding_signal(0.0001, 60)
    eight_hour = funding_signal(0.0001, 480)
    assert one_hour["carry_cost_bps_interval"] == pytest.approx(1.0)
    assert one_hour["funding_interval_min"] == 60
    assert one_hour["annualized_pct"] == pytest.approx(87.6)
    assert eight_hour["annualized_pct"] == pytest.approx(10.95)


def test_recommender_liquidation_buffer_uses_adverse_grid_boundary() -> None:
    from app.recommender import _params

    params = _params(
        "futures_grid",
        "linear",
        {
            "price": 100.0,
            "atr_pct": 0.02,
            "_atr_pct_1h": 0.02,
            "_direction_agg": {"trendiness": 0.12, "coherence": 0.70, "regime": "range"},
        },
        global_sent=0.0,
        direction="long",
        taker_fee_bps=4.0,
        direction_bias="long",
        direction_bias_strength=0.50,
        atr_pct_for_grid=0.02,
        cost_model={"execution_cost_bps": 8.0, "expected_funding_bps": 0.0},
    )

    econ = params["economics"]
    assert params["leverage"] == 3
    assert econ["liquidation_buffer_pct_adverse_boundary"] < econ["liquidation_buffer_pct_reference"]
    assert econ["liquidation_buffer_pct"] == pytest.approx(econ["liquidation_buffer_pct_adverse_boundary"])


def test_recommender_default_sizing_preserves_target_notional_for_expensive_contracts() -> None:
    from app.recommender import _params

    params = _params(
        "futures_grid",
        "linear",
        {
            "price": 100000.0,
            "atr_pct": 0.01,
            "_atr_pct_1h": 0.01,
            "_direction_agg": {"trendiness": 0.10, "coherence": 0.75, "regime": "range"},
        },
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=4.0,
        direction_bias="neutral",
        direction_bias_strength=0.0,
        atr_pct_for_grid=0.01,
        cost_model={"execution_cost_bps": 8.0, "expected_funding_bps": 0.0},
    )

    sizing = params["sizing"]
    assert params["grid_type"] == "arithmetic"
    assert params["grid_count"] == params["grid_levels"]
    assert sizing["qty_per_order"] == pytest.approx(0.00025)
    assert sizing["order_notional_usdt"] == pytest.approx(25.0)
    assert sizing["exchange_filter_assumption"]["mode"] == "provisional_target_notional_until_bybit_preflight"
    assert sizing["estimated_active_orders"] == params["grid_count"]
    assert sizing["exchange_filter_assumption"]["actual_bybit_filters_required"] is True


def test_linear_helpers_fail_closed_on_unknown_side() -> None:
    assert linear_pnl_usdt("typo", "1", "100", "110") == 0
    assert funding_cashflow_usdt("typo", "1000", "0.0001", 2) == 0


def test_grid_count_is_used_as_interval_count_for_range_span() -> None:
    from app.recommender import _params

    params = _params(
        "futures_grid",
        "linear",
        {
            "price": 100.0,
            "atr_pct": 0.001,
            "_atr_pct_1h": 0.001,
            "_direction_agg": {"trendiness": 0.05, "coherence": 0.80, "regime": "range", "regime_confidence": 0.80},
        },
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=0.0,
        direction_bias="neutral",
        direction_bias_strength=0.0,
        atr_pct_for_grid=0.001,
        cost_model={"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
    )

    # Bybit Number of Grids is price intervals, not rendered price points.
    # The published spacing must match the executable arithmetic geometry:
    # (upper - lower) / grid_count, while the economic minimum remains a floor.
    assert params["grid_count"] == params["grid_levels"]
    lower = params["price_range_lower"]
    upper = params["price_range_upper"]
    ref = params["price_ref"]
    actual_step_pct = ((upper - lower) / params["grid_count"]) / ref * 100.0
    assert params["grid_spacing_pct"] == pytest.approx(actual_step_pct)
    assert params["actual_grid_spacing_pct"] == pytest.approx(actual_step_pct)
    assert params["grid_spacing_pct"] >= params["economic_min_grid_spacing_pct"]
