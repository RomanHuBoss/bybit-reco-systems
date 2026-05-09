from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration117.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration117_runtime_lock.db"))
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


def _base_rec() -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_levels": 5,
            "leverage": 1,
            "trade_plan": {
                "reference_price": 100.0,
                "sizing": {"order_qty": 0.051, "order_notional_usdt": 5.1},
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.5},
                    "tp_per_leg": {"abs": 0.3, "pct": 0.3},
                },
            },
        },
    }


def _codes(validation: dict, key: str = "errors") -> set[str]:
    return {str(item.get("code")) for item in validation.get(key, [])}


def test_bybit_preflight_blocks_any_non_futures_grid_payload(app_main):
    rec = _base_rec()
    rec["bot_type"] = "invalid_bot_type"

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True)

    assert validation["ok"] is False
    assert "BOT_TYPE_UNSUPPORTED" in _codes(validation)


def test_bybit_preflight_blocks_any_non_linear_venue_payload(app_main):
    rec = _base_rec()
    rec["venue"] = "unsupported_venue"

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True)

    assert validation["ok"] is False
    assert "VENUE_UNSUPPORTED" in _codes(validation)


def test_execution_preflight_treats_off_tick_price_and_step_as_errors(app_main):
    rec = _base_rec()
    rec["params"]["trade_plan"]["reference_price"] = 100.03
    rec["params"]["trade_plan"]["levels"]["range"]["lower"] = 99.03
    rec["params"]["trade_plan"]["levels"]["range"]["upper"] = 101.03
    rec["params"]["trade_plan"]["levels"]["grid_step"]["step_abs"] = 0.33
    rec["params"]["trade_plan"]["levels"]["tp_per_leg"]["abs"] = 0.31

    execution_validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True)
    detail_validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=False)

    assert execution_validation["ok"] is False
    assert {"PRICE_OFF_TICK", "GRID_STEP_OFF_TICK", "TP_PER_LEG_OFF_TICK"} <= _codes(execution_validation)
    assert not ({"PRICE_OFF_TICK", "GRID_STEP_OFF_TICK", "TP_PER_LEG_OFF_TICK"} & _codes(detail_validation))
    assert {"PRICE_OFF_TICK", "GRID_STEP_OFF_TICK", "TP_PER_LEG_OFF_TICK"} <= _codes(detail_validation, "warnings")


def test_bybit_preflight_blocks_grid_count_above_bybit_futures_grid_limit(app_main):
    rec = _base_rec()
    rec["params"]["grid_count"] = 401
    rec["params"]["grid_levels"] = 401

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True)

    assert validation["ok"] is False
    assert "GRID_COUNT_ABOVE_BYBIT_MAX" in _codes(validation)


def test_bybit_preflight_blocks_unknown_grid_spacing_type(app_main):
    rec = _base_rec()
    rec["params"]["grid_type"] = "unsupported_spacing"

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True)

    assert validation["ok"] is False
    assert "GRID_TYPE_UNSUPPORTED" in _codes(validation)


def test_bybit_preflight_requires_tick_lot_min_notional_and_leverage_filters(app_main):
    rec = _base_rec()
    meta = _meta()
    for key in (
        "tick_size",
        "qty_step",
        "min_order_qty",
        "max_order_qty",
        "min_notional",
        "min_leverage",
        "max_leverage",
        "leverage_step",
    ):
        meta.pop(key, None)

    execution_validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta, require_meta=True)
    detail_validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta, require_meta=False)

    required_codes = {
        "BYBIT_TICK_SIZE_MISSING",
        "BYBIT_QTY_STEP_MISSING",
        "BYBIT_MIN_ORDER_QTY_MISSING",
        "BYBIT_MAX_ORDER_QTY_MISSING",
        "BYBIT_MIN_NOTIONAL_MISSING",
        "BYBIT_MIN_LEVERAGE_MISSING",
        "BYBIT_MAX_LEVERAGE_MISSING",
        "BYBIT_LEVERAGE_STEP_MISSING",
    }
    assert execution_validation["ok"] is False
    assert required_codes <= _codes(execution_validation)
    assert not (required_codes & _codes(detail_validation))
    assert required_codes <= _codes(detail_validation, "warnings")


def test_bybit_preflight_blocks_delivery_contracts_even_in_linear_category(app_main):
    rec = _base_rec()
    meta = _meta()
    meta["delivery_time"] = "1893456000000"

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta, require_meta=True)

    assert validation["ok"] is False
    assert "BYBIT_DELIVERY_TIME_NOT_PERPETUAL" in _codes(validation)


def test_bybit_preflight_warns_and_defaults_legacy_missing_leverage_to_one(app_main):
    rec = _base_rec()
    rec["params"].pop("leverage", None)

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True)

    assert "LEVERAGE_DEFAULTED_TO_ONE" in _codes(validation, "warnings")
