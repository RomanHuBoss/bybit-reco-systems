from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app import grid_math
from app.recommender import _build_trade_plan, _mode, _params as build_params


def _cost_model() -> dict:
    return {"execution_cost_bps": 0.0, "total_cost_bps": 0.0, "expected_funding_bps": 0.0}


def _generated(direction: str = "long") -> dict:
    features = {
        "price": 100.0,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.01,
        "_direction_agg": {"trendiness": 0.15, "coherence": 0.8, "regime": "range"},
    }
    params = build_params(
        "futures_grid",
        "linear",
        features,
        global_sent=0.0,
        direction=direction,
        taker_fee_bps=0.0,
        direction_bias=direction,
        direction_bias_strength=0.8,
        atr_pct_for_grid=0.01,
        cost_model=_cost_model(),
        risk_limits={"min_leverage": 1, "max_leverage": 3},
    )
    params["trade_plan"] = _build_trade_plan(
        "futures_grid", "linear", features, direction, params, cost_model=_cost_model()
    )
    return params


def _meta() -> dict[str, str]:
    return {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "tick_size": "0.01",
        "min_price": "1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "max_order_qty": "100",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
        "unified_margin_trade": True,
    }


def test_generated_linear_grid_uses_bybit_cross_margin() -> None:
    params = _generated("neutral")
    assert params["margin_mode"] == "cross"
    assert _mode("futures_grid", "linear", "neutral") == ("unified", "cross")


def test_trade_plan_publishes_cross_margin_and_one_way_position_mode() -> None:
    params = _generated("long")
    plan = params["trade_plan"]
    assert plan["margin_mode"] == "cross"
    assert plan["position_mode"] == "one_way"


def test_cross_margin_preflight_is_supported_and_isolated_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cross.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "cross-lock.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        params = _generated("long")
        rec = {
            "bot_type": "futures_grid",
            "venue": "linear",
            "symbol": "BTCUSDT",
            "direction": "long",
            "account_mode": "unified",
            "margin_mode": "cross",
            "params": params,
        }
        validation = app_main._validate_trade_plan_against_bybit_meta(
            rec, _meta(), require_meta=True, require_execution_plan=True
        )
        codes = {item["code"] for item in validation["errors"]}
        assert "MARGIN_MODE_UNSUPPORTED" not in codes

        rec["margin_mode"] = "isolated"
        rec["params"]["margin_mode"] = "isolated"
        rec["params"]["trade_plan"]["margin_mode"] = "isolated"
        blocked = app_main._validate_trade_plan_against_bybit_meta(
            rec, _meta(), require_meta=True, require_execution_plan=True
        )
        assert "MARGIN_MODE_UNSUPPORTED" in {item["code"] for item in blocked["errors"]}
    finally:
        sys.modules.pop("app.main", None)


def test_cross_margin_stress_has_independent_cash_invariant() -> None:
    helper = getattr(grid_math, "arithmetic_grid_cross_margin_stress", None)
    assert callable(helper)
    stress = helper(
        lower=90,
        upper=110,
        grid_count=2,
        reference_price=100,
        direction="long",
        leverage=3,
        kill_switch_lower=80,
        kill_switch_upper=120,
        execution_cost_bps=0,
        maintenance_margin_rate=0,
    )
    assert stress is not None
    assert stress["committed_notional_per_qty"] == pytest.approx(190.0)
    assert stress["initial_margin_per_qty"] == pytest.approx(190.0 / 3.0)
    assert stress["worst_loss_per_qty"] == pytest.approx(30.0)
    assert stress["equity_buffer_per_qty"] == pytest.approx(190.0 / 3.0 - 30.0)
    assert stress["equity_buffer_pct"] == pytest.approx((190.0 / 3.0 - 30.0) / (190.0 / 3.0) * 100.0)


def test_generated_economics_do_not_publish_isolated_liquidation_price() -> None:
    params = _generated("long")
    economics = params["economics"]
    assert economics["estimated_liquidation_price"] is None
    assert economics["cross_margin_stress_buffer_pct"] is not None
    assert economics["liquidation_model"] == "bybit_futures_grid_cross_margin_equity_stress"


def test_ui_describes_cross_margin_equity_stress_not_isolated_liquidation() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8").lower()
    assert "общая маржа" in source or "общая маржа" in source
    assert "запас капитала" in source
    assert "общая маржа — не поддерживается" not in source


def test_release_docs_use_cross_margin_contract() -> None:
    readme = Path("README.md").read_text(encoding="utf-8").lower()
    logic = Path("docs/TRADING_LOGIC.md").read_text(encoding="utf-8").lower()
    assert "margin_mode=cross" in readme
    assert "margin_mode=cross" in logic
    assert "directional_trend" in readme
    assert "margin_mode=isolated" in readme
    assert "single-position" in logic


def test_release_version_and_outcome_contract_are_bumped() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.4.9"' in source
    assert _generated("long")["margin_mode"] == "cross"
