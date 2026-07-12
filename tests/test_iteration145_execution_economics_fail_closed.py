from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration145.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration145_runtime_lock.db"))
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
        "delivery_time": "0",
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


def _rec(economics: dict | None = None, sizing: dict | None = None) -> dict:
    econ = {
        "gross_profit_bps": 25.0,
        "execution_cost_bps": 10.0,
        "funding_cost_bps": 0.0,
        "net_profit_bps": 15.0,
        "estimated_active_orders": 8,
        "estimated_committed_slots": 4,
        "estimated_max_position_slots": 4,
        "estimated_total_order_notional_usdt": 24.3,
        "estimated_margin_required_usdt": 12.15,
        "liquidation_buffer_pct": 35.0,
    }
    if economics:
        econ.update(economics)
    size = {"order_qty": 0.06, "order_notional_usdt": 6.0}
    if sizing:
        size.update(sizing)
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_count": 8,
            "grid_levels": 8,
            "grid_type": "arithmetic",
            "grid_geometry_model": "bybit_arithmetic_range_width_div_grid_count",
            "actual_grid_step_abs": 0.5,
            "leverage": 2,
            "margin_mode": "isolated",
            "economics": econ,
            "sizing": size,
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "sizing": size,
                "economics": econ,
                "levels": {
                    "range": {"lower": 98.0, "upper": 102.0},
                    "kill_switch": {"lower": 96.0, "upper": 104.0},
                    "grid_step": {"step_abs": 0.5, "step_pct": 0.5},
                    "tp_per_leg": {"abs": 0.4, "pct": 0.4},
                },
            },
        },
    }


def _codes(validation: dict, key: str = "errors") -> set[str]:
    return {str(item.get("code")) for item in validation.get(key, []) if isinstance(item, dict)}


def test_execution_preflight_blocks_non_positive_net_grid_edge(app_main):
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _rec({"net_profit_bps": -0.1}), _meta(), require_meta=True, require_execution_plan=True
    )
    assert "GRID_NET_PROFIT_NON_POSITIVE" in _codes(validation)


def test_execution_preflight_blocks_gross_edge_that_barely_covers_costs(app_main):
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _rec({"gross_profit_bps": 10.5, "execution_cost_bps": 10.0, "net_profit_bps": 3.0}),
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    assert "GRID_GROSS_EDGE_BELOW_COSTS" in _codes(validation)


def test_execution_preflight_blocks_notional_margin_mismatch(app_main):
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _rec({"estimated_margin_required_usdt": 5.0}), _meta(), require_meta=True, require_execution_plan=True
    )
    assert "MARGIN_NOTIONAL_LEVERAGE_MISMATCH" in _codes(validation)


def test_execution_preflight_reads_generated_params_sizing_when_trade_plan_sizing_missing(app_main):
    rec = _rec()
    rec["params"]["trade_plan"].pop("sizing")
    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec, _meta(), require_meta=True, require_execution_plan=True
    )
    assert "SIZE_INPUT_REQUIRED" not in _codes(validation, "warnings")
    assert "MIN_NOTIONAL_NOT_CHECKED" not in _codes(validation, "warnings")
    assert validation["ok"] is True
