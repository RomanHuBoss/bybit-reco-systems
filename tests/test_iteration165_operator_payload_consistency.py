from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration165.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration165_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _meta(**overrides):
    meta = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.005",
        "max_order_qty": "10",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "10",
        "leverage_step": "0.1",
    }
    meta.update(overrides)
    return meta


def _operator_sheet_rec(*, operator_sheet: dict, params_extra: dict | None = None) -> dict:
    params = {
        "grid_type": "arithmetic",
        "grid_count": 5,
        "grid_levels": 5,
        "margin_mode": "isolated",
        "operator_sheet": dict(operator_sheet),
        "trade_plan": {
            "reference_price": 100.0,
            "grid_type": "arithmetic",
            "grid_count": 5,
            "margin_mode": "isolated",
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.5, "upper": 101.5},
                "grid_step": {"step_abs": 0.4, "step_pct": 0.4},
                "tp_per_leg": {"abs": 0.3, "pct": 0.3},
            },
        },
    }
    if params_extra:
        params.update(params_extra)
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": params,
    }


def test_bybit_preflight_validates_operator_sheet_sizing_not_only_params(app_main):
    rec = _operator_sheet_rec(operator_sheet={"sizing": {"order_qty": 0.0045}, "leverage": 2})
    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True)

    error_codes = {item["code"] for item in validation["errors"]}
    warning_codes = {item["code"] for item in validation["warnings"]}

    assert validation["ok"] is False
    assert "ORDER_QTY_BELOW_MIN" in error_codes
    assert "ORDER_QTY_OFF_STEP" in error_codes
    assert "ORDER_NOTIONAL_BELOW_MIN" in error_codes
    assert "SIZE_INPUT_REQUIRED" not in warning_codes
    assert validation["snapped_levels"]["order_qty"] == "0.005"


def test_execution_preflight_accepts_operator_sheet_leverage_and_economics(app_main):
    rec = _operator_sheet_rec(
        operator_sheet={
            "leverage": 2,
            "sizing": {"order_qty": 0.051, "order_notional_usdt": 5.1},
            "economics": {
                "net_profit_bps": 4.5,
                "gross_profit_bps": 20.0,
                "execution_cost_bps": 5.0,
                "funding_cost_bps": 0.0,
                "estimated_active_orders": 5,
                "estimated_total_order_notional_usdt": 25.4082,
                "estimated_margin_required_usdt": 12.7041,
            },
        },
        params_extra={"leverage": None},
    )
    rec["params"].pop("leverage", None)

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    error_codes = {item["code"] for item in validation["errors"]}
    warning_codes = {item["code"] for item in validation["warnings"]}

    assert validation["ok"] is True
    assert "LEVERAGE_MISSING_FOR_EXECUTION" not in error_codes
    assert "GRID_ECONOMICS_MISSING" not in warning_codes
    assert "SIZE_INPUT_REQUIRED" not in warning_codes
    assert "MIN_NOTIONAL_NOT_CHECKED" not in warning_codes


def test_operator_ui_uses_operator_sheet_sizing_and_leverage_for_position_math() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert 'const operatorSheet = params.operator_sheet || {};' in app_js
    assert 'const operatorSizing = operatorSheet.sizing || {};' in app_js
    assert 'const operatorEconomics = operatorSheet.economics || {};' in app_js
    assert 'const leverageRaw = firstFiniteValue([params, plan, operatorSheet], ["leverage"]);' in app_js
    assert '[sizing, economics, operatorSizing, operatorEconomics, params, operatorSheet]' in app_js
