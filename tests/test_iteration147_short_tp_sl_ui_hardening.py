from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _app_js() -> str:
    return (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")


def test_operator_exit_levels_are_direction_aware_for_short_and_long() -> None:
    app_js = _app_js()

    assert "function operatorExitLevels(direction, killLower, killUpper)" in app_js
    assert 'if (dir === "short")' in app_js
    assert "takeProfitValue: killLower" in app_js
    assert "stopLossValue: killUpper" in app_js
    assert "short: TP ниже диапазона, SL выше диапазона" in app_js
    assert 'if (dir === "long")' in app_js
    assert "takeProfitValue: killUpper" in app_js
    assert "stopLossValue: killLower" in app_js
    assert "long: TP выше диапазона, SL ниже диапазона" in app_js


def test_operator_details_no_longer_use_one_sided_tp_sl_for_all_directions() -> None:
    app_js = _app_js()

    assert "const stopLossValue = killLower;" not in app_js
    assert "const takeProfitValue = killUpper;" not in app_js
    assert "const exits = operatorExitLevels((it || {}).direction, killLower, killUpper);" in app_js
    assert 'ov.takeProfitLabel || "Take Profit", value: ov.takeProfitValue, mono: true, help:' in app_js
    assert 'ov.stopLossLabel || "Stop Loss", value: ov.stopLossValue, mono: true, help:' in app_js


def test_static_asset_cache_key_bumped_after_short_tp_sl_fix() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v29" in index
    assert "app.js?v=manual-ui-v29" in index


def test_directional_grid_range_geometry_keeps_short_profit_side_below_reference() -> None:
    from app.recommender import _params

    features = {
        "price": 100.0,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.01,
        "range_score": 0.80,
        "_direction_agg": {"trendiness": 0.10, "coherence": 0.70, "regime": "range"},
    }
    common = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "f": features,
        "global_sent": 0.0,
        "taker_fee_bps": 6.0,
        "direction_bias_strength": 0.50,
        "atr_pct_for_grid": 0.01,
        "cost_model": {"execution_cost_bps": 12.0, "expected_funding_bps": 0.0},
    }

    long_params = _params(direction="long", direction_bias="long", **common)
    short_params = _params(direction="short", direction_bias="short", **common)

    long_upside = long_params["price_range_upper"] - long_params["price_ref"]
    long_downside = long_params["price_ref"] - long_params["price_range_lower"]
    short_downside = short_params["price_ref"] - short_params["price_range_lower"]
    short_upside = short_params["price_range_upper"] - short_params["price_ref"]

    assert long_upside > long_downside
    assert short_downside > short_upside
    assert short_params["price_range_lower"] < short_params["price_ref"] < short_params["price_range_upper"]
    assert long_params["economics"]["net_profit_bps"] > 0
    assert short_params["economics"]["net_profit_bps"] > 0
