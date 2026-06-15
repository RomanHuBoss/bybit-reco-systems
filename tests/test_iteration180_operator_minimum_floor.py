from __future__ import annotations

from app import recommender as recommender_module


def test_operator_minimum_floor_is_actionable_when_safety_and_edge_pass() -> None:
    """The 3x floor is not a promotion; quality only promotes above the floor.

    This protects against the regression where score/conf-favoured grid ideas
    with low directional strength could never intersect the operator leverage
    profile even though volatility, execution cost and net grid edge were safe.
    """
    leverage, note, diagnostics = recommender_module._select_operator_grid_leverage(
        direction="long",
        dir_strength=0.20,
        range_score=0.30,
        trendiness=0.75,
        atr_pct=0.015,
        execution_cost_bps=16.0,
        funding_cost_bps=0.0,
        gross_profit_bps_est=22.0,
        min_operator_leverage=3,
        max_operator_leverage=5,
    )

    assert leverage == 3
    assert note == "operator_minimum_selected"
    assert diagnostics["operator_minimum_approved"] is True
    assert diagnostics["not_actionable_reason"] is None
    assert diagnostics["directional_quality"] is False
    assert diagnostics["neutral_range_quality"] is False
    assert diagnostics["accepted_leverage_promotions"] == []


def test_operator_minimum_floor_still_declines_high_atr_before_quality() -> None:
    leverage, note, diagnostics = recommender_module._select_operator_grid_leverage(
        direction="long",
        dir_strength=0.80,
        range_score=0.90,
        trendiness=0.10,
        atr_pct=0.030,
        execution_cost_bps=16.0,
        funding_cost_bps=0.0,
        gross_profit_bps_est=30.0,
        min_operator_leverage=3,
        max_operator_leverage=5,
    )

    assert leverage == 3
    assert note == "atr_too_high_for_operator_minimum"
    assert diagnostics["operator_minimum_approved"] is False
    assert diagnostics["not_actionable_reason"] == note
