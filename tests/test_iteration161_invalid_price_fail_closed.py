from __future__ import annotations

import math
from typing import Any

import pytest

from app.recommender import _build_trade_plan, _params, _sanitize_json_numbers


def _feature(price: Any) -> dict[str, Any]:
    return {
        "price": price,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.01,
        "range_score": 0.72,
        "trend_strength": 0.20,
        "_direction_agg": {
            "direction": "short",
            "regime": "range",
            "regime_confidence": 0.68,
            "coherence": 0.70,
            "trendiness": 0.20,
        },
    }


def _cost_model() -> dict[str, float]:
    return {
        "execution_cost_bps": 10.0,
        "total_cost_bps": 10.0,
        "net_cost_bps": 10.0,
        "expected_funding_bps": 0.0,
    }


def _assert_no_nonfinite_numbers(value: Any) -> None:
    if isinstance(value, float):
        assert math.isfinite(value)
    elif isinstance(value, dict):
        for child in value.values():
            _assert_no_nonfinite_numbers(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_nonfinite_numbers(child)


@pytest.mark.parametrize("bad_price", [0.0, -1.0, "NaN", float("nan"), None])
def test_recommender_params_do_not_synthesize_fake_reference_price_for_invalid_market_data(bad_price: Any) -> None:
    params = _params(
        "futures_grid",
        "linear",
        _feature(bad_price),
        global_sent=0.0,
        direction="short",
        taker_fee_bps=5.5,
        direction_bias="short",
        direction_bias_strength=0.70,
        atr_pct_for_grid=0.01,
        cost_model=_cost_model(),
    )

    assert params["price_input_valid"] is False
    assert params["invalid_price_fail_closed"] is True
    assert params["price_ref"] == 0.0
    assert params["price_range_lower"] == 0.0
    assert params["price_range_upper"] == 0.0
    assert params["sizing"]["order_notional_usdt"] == 0.0
    assert params["sizing"]["exchange_filter_assumption"]["mode"] == "invalid_price"
    _assert_no_nonfinite_numbers(_sanitize_json_numbers(params))


def test_trade_plan_for_invalid_price_has_no_actionable_grid_or_directional_tp_sl_levels() -> None:
    f = _feature(float("nan"))
    params = _params(
        "futures_grid",
        "linear",
        f,
        global_sent=0.0,
        direction="short",
        taker_fee_bps=5.5,
        direction_bias="short",
        direction_bias_strength=0.70,
        atr_pct_for_grid=0.01,
        cost_model=_cost_model(),
    )
    plan = _build_trade_plan("futures_grid", "linear", f, "short", params, cost_model=_cost_model())

    assert plan["reference_price"] is None
    assert plan["levels"]["range"] == {"lower": None, "upper": None}
    assert plan["levels"]["kill_switch"]["lower"] is None
    assert plan["levels"]["kill_switch"]["upper"] is None
    assert plan["levels"]["grid_step"]["step_abs"] is None
    assert plan["levels"]["tp_per_leg"]["abs"] is None
    assert plan["levels"]["tp_per_leg"]["pct"] is None


def test_recommender_params_sanitize_nonfinite_atr_before_grid_geometry() -> None:
    f = _feature(100.0)
    f["atr_pct"] = float("nan")
    params = _params(
        "futures_grid",
        "linear",
        f,
        global_sent=0.0,
        direction="long",
        taker_fee_bps=5.5,
        direction_bias="long",
        direction_bias_strength=0.70,
        atr_pct_for_grid=float("nan"),
        cost_model=_cost_model(),
    )

    assert params["price_input_valid"] is True
    assert params["invalid_price_fail_closed"] is False
    assert params["price_ref"] == 100.0
    assert params["price_range_lower"] is not None
    assert params["price_range_upper"] is not None
    assert params["price_range_upper"] > params["price_range_lower"] > 0
    _assert_no_nonfinite_numbers(_sanitize_json_numbers(params))
