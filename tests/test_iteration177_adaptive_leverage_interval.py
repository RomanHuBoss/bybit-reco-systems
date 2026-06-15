from __future__ import annotations

from app import recommender as recommender_module


def test_adaptive_operator_interval_promotes_medium_setup_to_four_x() -> None:
    leverage, note, diagnostics = recommender_module._select_operator_grid_leverage(
        direction="long",
        dir_strength=0.80,
        range_score=0.75,
        trendiness=0.20,
        atr_pct=0.015,
        execution_cost_bps=14.0,
        funding_cost_bps=0.0,
        gross_profit_bps_est=21.0,
        min_operator_leverage=3,
        max_operator_leverage=5,
    )

    assert leverage == 4
    assert note == "adaptive_interval_selected"
    assert diagnostics["operator_minimum_approved"] is True
    assert diagnostics["interval_mode"] == "adaptive"
    assert diagnostics["selected_leverage"] == 4
    assert diagnostics["accepted_leverage_promotions"][-1]["leverage"] == 4


def test_adaptive_operator_interval_promotes_strong_setup_to_3x_5x() -> None:
    leverage, note, diagnostics = recommender_module._select_operator_grid_leverage(
        direction="long",
        dir_strength=0.90,
        range_score=0.85,
        trendiness=0.10,
        atr_pct=0.008,
        execution_cost_bps=14.0,
        funding_cost_bps=0.0,
        gross_profit_bps_est=27.0,
        min_operator_leverage=3,
        max_operator_leverage=5,
    )

    assert leverage == 5
    assert note == "adaptive_interval_selected"
    assert diagnostics["operator_minimum_approved"] is True
    assert diagnostics["selected_leverage"] == 5
    assert [x["leverage"] for x in diagnostics["accepted_leverage_promotions"]] == [4, 5]


def test_adaptive_operator_interval_respects_liquidation_safe_leverage_clamp() -> None:
    leverage, note, diagnostics = recommender_module._select_operator_grid_leverage(
        direction="long",
        dir_strength=0.90,
        range_score=0.85,
        trendiness=0.10,
        atr_pct=0.008,
        execution_cost_bps=14.0,
        funding_cost_bps=0.0,
        gross_profit_bps_est=27.0,
        min_operator_leverage=3,
        max_operator_leverage=5,
        liquidation_safe_max_leverage=4,
    )

    assert leverage == 4
    assert note == "adaptive_interval_selected"
    assert diagnostics["effective_max_operator_leverage"] == 4
    assert diagnostics["liquidation_safe_max_leverage"] == 4
    assert diagnostics["selected_leverage"] == 4


def test_params_use_adaptive_3x_5x_operator_interval_for_strong_grid_setup() -> None:
    params = recommender_module._params(
        "futures_grid",
        "linear",
        {
            "price": 100.0,
            "atr_pct": 0.008,
            "_atr_pct_1h": 0.008,
            "range_score": 0.90,
            "trend_strength": 0.08,
            "spread_bps": 0.0,
            "_direction_agg": {
                "direction": "long",
                "bias": "long",
                "trendiness": 0.08,
                "coherence": 0.82,
                "regime": "range",
                "regime_confidence": 0.86,
                "strength": {"all": 0.90},
            },
        },
        global_sent=0.0,
        direction="long",
        taker_fee_bps=6.0,
        direction_bias="long",
        direction_bias_strength=0.90,
        atr_pct_for_grid=0.008,
        cost_model={"execution_cost_bps": 14.0, "expected_funding_bps": 0.0},
        risk_limits={
            "max_concurrent_bots": 1,
            "max_daily_dd_usdt": 30.0,
            "cooldown_after_loss_min": 90,
            "max_symbol_bots": 1,
            "min_leverage": 3,
            "max_leverage": 5,
            "max_position_notional_usdt": 5000.0,
            "max_margin_per_bot_usdt": 1000.0,
        },
    )

    assert params["leverage"] == 5
    assert params["leverage_policy"]["min_operator_leverage"] == 3
    assert params["leverage_policy"]["max_operator_leverage"] == 5
    assert params["leverage_policy"]["note"] == "adaptive_interval_selected"
    assert params["leverage_policy"]["operator_minimum_approved"] is True
    assert params["leverage_policy"]["diagnostics"]["interval_mode"] == "adaptive"
    assert params["economics"]["liquidation_buffer_pct"] >= 12.0
