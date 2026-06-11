from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app.trading_semantics import bybit_linear_protective_order_semantics


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration155.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration155_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _meta() -> dict[str, str]:
    return {
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
        "min_order_qty": "0.001",
        "max_order_qty": "100",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def _rec(direction: str = "short") -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": direction,
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_count": 5,
            "grid_levels": 5,
            "grid_type": "arithmetic",
            "grid_geometry_model": "bybit_arithmetic_range_width_div_grid_count",
            "actual_grid_step_abs": 4.0,
            "leverage": 1,
            "price_ref": 100.0,
            "price_range_lower": 90.0,
            "price_range_upper": 110.0,
            "sizing": {"qty_per_order": 0.1, "order_notional_usdt": 10.0},
            "economics": {
                "order_notional_usdt": 10.0,
                "qty_per_order": 0.1,
                "net_profit_bps": 8.0,
                "gross_profit_bps": 30.0,
                "execution_cost_bps": 12.0,
                "funding_cost_bps": 0.0,
                "estimated_active_orders": 5,
                "estimated_total_order_notional_usdt": 50.0,
                "estimated_margin_required_usdt": 50.0,
            },
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "sizing": {"order_qty": 0.1, "order_notional_usdt": 10.0},
                "economics": {
                    "net_profit_bps": 8.0,
                    "gross_profit_bps": 30.0,
                    "execution_cost_bps": 12.0,
                    "funding_cost_bps": 0.0,
                    "estimated_active_orders": 5,
                    "estimated_total_order_notional_usdt": 50.0,
                    "estimated_margin_required_usdt": 50.0,
                },
                "levels": {
                    "range": {"lower": 90.0, "upper": 110.0},
                    "kill_switch": {"lower": 88.0, "upper": 112.0},
                    "grid_step": {"step_abs": 4.0, "step_pct": 4.0},
                    "tp_per_leg": {"abs": 2.8, "pct": 2.8},
                },
            },
        },
    }


@pytest.mark.parametrize(
    ("direction", "exit_kind", "expected_side", "expected_trigger_direction"),
    [
        ("long", "take_profit", "Sell", 1),
        ("long", "stop_loss", "Sell", 2),
        ("short", "take_profit", "Buy", 2),
        ("short", "stop_loss", "Buy", 1),
    ],
)
def test_bybit_protective_orders_include_directional_trigger_direction(
    direction: str,
    exit_kind: str,
    expected_side: str,
    expected_trigger_direction: int,
) -> None:
    order = bybit_linear_protective_order_semantics(direction, exit_kind)

    assert order["category"] == "linear"
    assert order["position_mode"] == "one_way"
    assert order["positionIdx"] == 0
    assert order["side"] == expected_side
    assert order["reduceOnly"] is True
    assert order["closeOnTrigger"] is True
    assert "orderFilter" not in order
    assert order["triggerBy"] == "LastPrice"
    assert order["orderType"] == "Market"
    assert order["triggerDirection"] == expected_trigger_direction


def test_backend_directional_exit_payload_includes_geometry_status_for_short(app_main) -> None:
    payload = app_main._directional_exit_payload_for_reco(_rec("short"))

    assert payload["direction"] == "short"
    assert payload["reference_price"] == 100.0
    assert payload["take_profit"] == 88.0
    assert payload["stop_loss"] == 112.0
    assert payload["geometry_valid"] is True
    assert payload["geometry_errors"] == []


def test_backend_directional_exit_payload_marks_invalid_short_geometry(app_main) -> None:
    rec = _rec("short")
    rec["params"]["trade_plan"]["levels"]["kill_switch"] = {"lower": 101.0, "upper": 112.0}

    payload = app_main._directional_exit_payload_for_reco(rec)
    codes = {err["code"] for err in payload["geometry_errors"]}

    assert payload["geometry_valid"] is False
    assert "SHORT_TP_NOT_BELOW_ENTRY" in codes


def test_execution_preflight_requires_explicit_leverage_for_materialised_bot(app_main) -> None:
    rec = _rec("long")
    rec["params"].pop("leverage")

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    codes = {err["code"] for err in validation["errors"]}

    assert validation["ok"] is False
    assert "LEVERAGE_MISSING_FOR_EXECUTION" in codes


def test_runtime_risk_blocks_missing_position_size_when_payload_claims_sizing_context(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "params": {"leverage": 3, "sizing": {"qty_per_order": 0.01}},
    }
    limits = {
        "max_leverage": 10,
        "max_position_notional_usdt": 1000,
        "max_margin_per_bot_usdt": 100,
    }

    blocks = app_main._execution_runtime_size_risk_blocks(rec, limits)
    codes = {block["code"] for block in blocks}

    assert "POSITION_SIZE_MISSING_AT_EXECUTION" in codes


def test_execution_preflight_rejects_negative_grid_economics_components(app_main) -> None:
    rec = _rec("long")
    rec["params"]["economics"]["gross_profit_bps"] = -1.0
    rec["params"]["economics"]["execution_cost_bps"] = -0.5
    rec["params"]["economics"]["funding_cost_bps"] = -0.1

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    codes = {err["code"] for err in validation["errors"]}

    assert "GRID_GROSS_PROFIT_NEGATIVE" in codes
    assert "GRID_EXECUTION_COST_NEGATIVE" in codes
    assert "GRID_FUNDING_COST_NEGATIVE" in codes


def test_operator_ui_rejects_invalid_backend_exit_payload_before_rendering_short_tp_sl() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "function directionalExitGeometryOk(direction, takeProfit, stopLoss, referencePrice = null)" in app_js
    assert "backend directional TP/SL invalid; using local kill-switch mapping" in app_js
    assert "Directional TP unavailable" in app_js
