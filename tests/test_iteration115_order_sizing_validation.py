from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration115.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration115_runtime_lock.db"))
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


def _rec_with_sizing(sizing):
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_levels": 5,
            "leverage": 2,
            "trade_plan": {
                "reference_price": 100.0,
                "sizing": dict(sizing),
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.5},
                    "tp_per_leg": {"abs": 0.3, "pct": 0.3},
                },
            },
        },
    }


# Если операторский payload уже содержит размер leg/order, preflight обязан валидировать
# qty_step/min_order_qty/min_notional, а не оставлять только предупреждение о неизвестном размере.
def test_bybit_plan_validation_blocks_explicit_order_qty_below_filters(app_main):
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _rec_with_sizing({"order_qty": 0.0045}),
        _meta(),
    )

    error_codes = {item["code"] for item in validation["errors"]}
    warning_codes = {item["code"] for item in validation["warnings"]}

    assert validation["ok"] is False
    assert "ORDER_QTY_BELOW_MIN" in error_codes
    assert "ORDER_QTY_OFF_STEP" in error_codes
    assert "ORDER_NOTIONAL_BELOW_MIN" in error_codes
    assert "SIZE_INPUT_REQUIRED" not in warning_codes
    assert validation["snapped_levels"]["order_qty"] == "0.004"


# При достаточном и выровненном размере не должно оставаться ложное предупреждение,
# что qty/min_notional якобы вообще не проверялись.
def test_bybit_plan_validation_accepts_explicit_aligned_qty_and_notional(app_main):
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _rec_with_sizing({"order_qty": 0.05, "order_notional_usdt": 5.5}),
        _meta(),
    )

    error_codes = {item["code"] for item in validation["errors"]}
    warning_codes = {item["code"] for item in validation["warnings"]}

    assert "ORDER_QTY_BELOW_MIN" not in error_codes
    assert "ORDER_QTY_OFF_STEP" not in error_codes
    assert "ORDER_NOTIONAL_BELOW_MIN" not in error_codes
    assert "SIZE_INPUT_REQUIRED" not in warning_codes
    assert "MIN_NOTIONAL_NOT_CHECKED" not in warning_codes
    assert validation["snapped_levels"]["order_qty"] == "0.050"
