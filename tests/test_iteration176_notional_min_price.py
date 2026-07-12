from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration176.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration176_runtime_lock.db"))
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
        "min_order_qty": "0.001",
        "max_order_qty": "10",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "10",
        "leverage_step": "0.1",
    }
    meta.update(overrides)
    return meta


def _notional_only_rec(order_notional: float, *, lower: float = 80.0) -> dict:
    qty = order_notional / 100.0
    step = (120.0 - lower) / 5.0
    levels = [lower + step * index for index in range(6)]
    # Reference 100 lies between levels 2 and 3. Dynamic Long leaves the
    # nearest upper bridge level idle: two initial long slots plus three buys.
    committed_notional = qty * (2 * 100.0 + sum(levels[:3]))
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "grid_type": "arithmetic",
            "grid_count": 5,
            "grid_levels": 5,
            "leverage": 2,
            "margin_mode": "cross",
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "grid_count": 5,
                "sizing": {"order_notional_usdt": order_notional},
                "economics": {
                    "net_profit_bps": 4.5,
                    "gross_profit_bps": 20.0,
                    "execution_cost_bps": 5.0,
                    "funding_cost_bps": 0.0,
                    "estimated_active_orders": 5,
                    "estimated_total_order_notional_usdt": committed_notional,
                    "estimated_margin_required_usdt": committed_notional / 2,
                },
                "levels": {
                    "range": {"lower": lower, "upper": 120.0},
                    "kill_switch": {"lower": lower - 1.0, "upper": 121.0},
                    "grid_step": {"step_abs": (120.0 - lower) / 5.0, "step_pct": 8.0},
                    "tp_per_leg": {"abs": 1.0, "pct": 1.0},
                },
            },
        },
    }


def test_notional_only_payload_checks_min_notional_at_lowest_grid_price(app_main):
    rec = _notional_only_rec(5.1, lower=80.0)

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    errors = [item for item in validation["errors"] if item["code"] == "ORDER_NOTIONAL_BELOW_MIN"]
    assert validation["ok"] is False
    assert errors
    assert any("grid_min_price" in item["msg"] and "reference_price" in item["msg"] for item in errors)


def test_notional_only_payload_accepts_grid_min_adjusted_notional_above_bybit_floor(app_main):
    rec = _notional_only_rec(6.25, lower=80.0)

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    error_codes = {item["code"] for item in validation["errors"]}
    assert validation["ok"] is True
    assert "ORDER_NOTIONAL_BELOW_MIN" not in error_codes
