from __future__ import annotations

from types import SimpleNamespace

from app import recommender as recommender_module
from app.recommender import _estimate_cost_model, _params, _select_operator_grid_leverage


def _strong_range_features(direction: str = "long") -> dict:
    return {
        "price": 100.0,
        "atr_pct": 0.008,
        "_atr_pct_1h": 0.008,
        "range_score": 0.86,
        "trend_strength": 0.10,
        "spread_bps": 0.0,
        "_direction_agg": {
            "direction": direction,
            "bias": direction,
            "trendiness": 0.10,
            "coherence": 0.76,
            "regime": "range",
            "regime_confidence": 0.82,
            "strength": {"all": 0.70},
        },
    }


def test_operator_min_leverage_is_not_unreachable_below_default_fee_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        recommender_module,
        "settings",
        SimpleNamespace(risk_limits={"min_leverage": 5, "max_leverage": 5}),
    )
    features = _strong_range_features("long")
    cost_model = _estimate_cost_model(
        "futures_grid",
        "linear",
        features,
        taker_fee_bps=6.0,
        direction="long",
        funding_rate=0.0,
        next_funding_ts=9_999_999_999,
        ts_now=1,
    )

    assert cost_model["execution_cost_bps"] > 10.0  # default fee floor already makes the old gate impossible

    params = _params(
        "futures_grid",
        "linear",
        features,
        global_sent=0.0,
        direction="long",
        taker_fee_bps=6.0,
        direction_bias="long",
        direction_bias_strength=0.70,
        atr_pct_for_grid=features["_atr_pct_1h"],
        cost_model=cost_model,
    )

    assert params["leverage"] == 5
    assert params["leverage_policy"]["note"] == "operator_minimum_selected"
    assert params["leverage_policy"]["diagnostics"]["projected_net_profit_bps_est"] >= 2.0
    assert params["economics"]["net_profit_bps"] >= 2.0


def test_leverage_selector_marks_unsafe_or_thin_edge_ideas_not_actionable_without_one_x_payload() -> None:
    leverage, note, diag = _select_operator_grid_leverage(
        direction="long",
        dir_strength=0.80,
        range_score=0.85,
        trendiness=0.10,
        atr_pct=0.008,
        execution_cost_bps=13.0,
        funding_cost_bps=0.0,
        gross_profit_bps_est=14.0,
        min_operator_leverage=5,
        max_operator_leverage=5,
    )

    assert leverage == 5
    assert note == "insufficient_net_edge_for_operator_minimum"
    assert diag["projected_net_profit_bps_est"] == 1.0
    assert diag["operator_minimum_approved"] is False
    assert diag["not_actionable_reason"] == note


def test_neutral_high_quality_range_can_use_operator_minimum_with_liq_checks_downstream(monkeypatch) -> None:
    monkeypatch.setattr(
        recommender_module,
        "settings",
        SimpleNamespace(risk_limits={"min_leverage": 5, "max_leverage": 5}),
    )
    features = _strong_range_features("neutral")
    cost_model = _estimate_cost_model(
        "futures_grid",
        "linear",
        features,
        taker_fee_bps=6.0,
        direction="neutral",
        funding_rate=0.0,
        next_funding_ts=9_999_999_999,
        ts_now=1,
    )

    params = _params(
        "futures_grid",
        "linear",
        features,
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=6.0,
        direction_bias="neutral",
        direction_bias_strength=0.05,
        atr_pct_for_grid=features["_atr_pct_1h"],
        cost_model=cost_model,
    )

    assert params["leverage"] == 5
    assert params["leverage_policy"]["diagnostics"]["neutral_range_quality"] is True
    assert params["economics"]["liquidation_buffer_pct"] >= 12.0
