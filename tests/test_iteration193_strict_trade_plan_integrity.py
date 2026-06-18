from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration193.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration193_runtime_lock.db"))
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


def _legacy_alias_complete_rec() -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_count": 4,
            "grid_type": "arithmetic",
            "leverage": 1,
            "reference_price": 100.0,
            "price_range_lower": 99.0,
            "price_range_upper": 101.0,
            "operator_sheet": {
                "kill_switch": {"lower": 98.5, "upper": 101.5},
                "grid_step_abs": 0.5,
                "sizing": {"order_qty": 0.051, "order_notional_usdt": 5.1},
                "economics": {
                    "net_profit_bps": 4.0,
                    "gross_profit_bps": 20.0,
                    "execution_cost_bps": 5.0,
                    "funding_cost_bps": 0.0,
                    "estimated_active_orders": 4,
                    "estimated_total_order_notional_usdt": 20.4,
                    "estimated_margin_required_usdt": 20.4,
                },
            },
        },
    }


def _codes(validation: dict, key: str = "errors") -> set[str]:
    return {str(item.get("code")) for item in validation.get(key, [])}


def test_strict_execution_rejects_noncanonical_nonempty_trade_plan_even_with_complete_aliases(app_main):
    rec = _legacy_alias_complete_rec()
    rec["params"]["trade_plan"] = {"marker": "not-an-execution-plan"}

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    assert validation["ok"] is False
    assert "TRADE_PLAN_MISSING" in _codes(validation)


def test_strict_execution_rejects_partial_trade_plan_when_operator_sheet_fills_missing_geometry(app_main):
    rec = _legacy_alias_complete_rec()
    rec["params"]["trade_plan"] = {
        "reference_price": 100.0,
        "levels": {"range": {"lower": 99.0}},
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    assert validation["ok"] is False
    assert "TRADE_PLAN_MISSING" in _codes(validation)
