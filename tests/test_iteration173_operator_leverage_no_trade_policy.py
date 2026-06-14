from __future__ import annotations

from pathlib import Path

import pytest

from app import recommender as recommender_module

ROOT = Path(__file__).resolve().parent.parent


def test_fixed_operator_profile_declines_as_no_trade_instead_of_one_x_fallback() -> None:
    leverage, note, diagnostics = recommender_module._select_operator_grid_leverage(
        direction="long",
        dir_strength=0.20,
        range_score=0.30,
        trendiness=0.75,
        atr_pct=0.030,
        execution_cost_bps=16.0,
        funding_cost_bps=0.0,
        gross_profit_bps_est=6.0,
        min_operator_leverage=5,
        max_operator_leverage=5,
    )

    assert leverage == 5
    assert note in {"atr_too_high_for_operator_minimum", "signal_quality_too_low_for_operator_minimum"}
    assert diagnostics["operator_minimum_approved"] is False
    assert diagnostics["not_actionable_reason"] == note


def test_params_fixed_operator_profile_never_publishes_one_x_payload_for_declined_idea() -> None:
    params = recommender_module._params(
        "futures_grid",
        "linear",
        {
            "price": 100.0,
            "atr_pct": 0.030,
            "_atr_pct_1h": 0.030,
            "trend_strength": 0.75,
            "_direction_agg": {"trendiness": 0.75, "coherence": 0.30, "regime": "trend"},
        },
        global_sent=0.0,
        direction="long",
        taker_fee_bps=6.0,
        direction_bias="long",
        direction_bias_strength=0.20,
        atr_pct_for_grid=0.030,
        cost_model={"execution_cost_bps": 16.0, "expected_funding_bps": 0.0},
        risk_limits={
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
            "min_leverage": 5,
            "max_leverage": 5,
            "max_position_notional_usdt": 5000.0,
            "max_margin_per_bot_usdt": 1000.0,
        },
    )

    assert params["leverage"] == 5
    assert params["leverage_policy"]["operator_minimum_approved"] is False
    assert params["leverage_policy"]["not_actionable_reason"] in {
        "atr_too_high_for_operator_minimum",
        "signal_quality_too_low_for_operator_minimum",
    }


def test_ui_no_trade_reasons_are_not_treated_as_hard_risk_rejections() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
    assert "riskReportNoTradeReasons" in app_js
    assert "const explicitHardBlocked = bybitErrors.length > 0 || blocks.length > 0 || riskReportRejected.length > 0 || status === \"blocked\"" in app_js
    assert "riskReportNoTradeReasons.map" in app_js
    assert "uniqueBlockerItems" in app_js
