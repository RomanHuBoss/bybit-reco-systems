from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration185.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration185_runtime_lock.db"))
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


def _rec(direction: str, *, economics: dict[str, Any] | None = None) -> dict[str, Any]:
    # Cross-margin stress must be recomputed from the actual grid commitment and
    # materially adverse external kill-switches; a manual stored buffer is not trusted.
    params_economics = {
        "net_profit_bps": 8.0,
        "gross_profit_bps": 30.0,
        "execution_cost_bps": 12.0,
        **(economics or {}),
    }
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": direction,
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "leverage": 5,
            "grid_count": 10,
            "grid_levels": 10,
            "grid_type": "arithmetic",
            "grid_geometry_model": "bybit_arithmetic_range_width_div_grid_count",
            "actual_grid_step_abs": 2.0,
            "price_ref": 100.0,
            "price_range_lower": 90.0,
            "price_range_upper": 110.0,
            "sizing": {"qty_per_order": 0.1, "order_notional_usdt": 10.0},
            "economics": params_economics,
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "grid_count": 10,
                "sizing": {"order_qty": 0.1, "order_notional_usdt": 10.0},
                "economics": params_economics,
                "levels": {
                    "range": {"lower": 90.0, "upper": 110.0},
                    "kill_switch": {"lower": 50.0, "upper": 150.0},
                    "grid_step": {"step_abs": 2.0, "step_pct": 2.0},
                    "tp_per_leg": {"abs": 1.4, "pct": 1.4},
                },
            },
        },
    }


@pytest.mark.parametrize("direction", ["long", "short"])
def test_execution_preflight_uses_cross_margin_stress_at_kill_switch(app_main, direction: str) -> None:
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _rec(direction),
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    assert validation["ok"] is False
    assert "LIQUIDATION_BUFFER_TOO_LOW" in {err["code"] for err in validation["errors"]}


def test_execution_preflight_does_not_trust_manual_high_liquidation_buffer_when_boundary_is_tight(app_main) -> None:
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _rec("long", economics={"liquidation_buffer_pct": 99.0}),
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    assert validation["ok"] is False
    assert "LIQUIDATION_BUFFER_TOO_LOW" in {err["code"] for err in validation["errors"]}
