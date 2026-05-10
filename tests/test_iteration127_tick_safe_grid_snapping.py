from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration127.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration127_runtime_lock.db"))
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
        "max_order_qty": "1000",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def _rec() -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_count": 4,
            "grid_levels": 4,
            "grid_type": "arithmetic",
            "leverage": 1,
            "price_ref": 100.04,
            "price_range_lower": 99.96,
            "price_range_upper": 100.46,
            "sizing": {
                "basis": "minimum_viable_operator_default",
                "qty_per_order": 0.05,
                "order_notional_usdt": 5.0,
                "estimated_total_order_notional_usdt": 20.0,
                "estimated_margin_required_usdt": 20.0,
            },
            "economics": {
                "qty_per_order": 0.05,
                "order_notional_usdt": 5.0,
                "estimated_total_order_notional_usdt": 20.0,
                "estimated_margin_required_usdt": 20.0,
                "estimated_max_position_notional_usdt": 20.0,
                "net_profit_bps": 4.0,
            },
            "operator_sheet": {
                "price_ref": 100.04,
                "range_lower": 99.96,
                "range_upper": 100.46,
                "grid_spacing_pct": 0.24,
                "kill_switch": {"lower": 99.94, "upper": 100.56},
                "tp_per_leg": {"abs": 0.21, "pct": 0.21},
                "sizing": {"basis": "minimum_viable_operator_default", "qty_per_order": 0.05},
                "economics": {"estimated_margin_required_usdt": 20.0},
            },
            "trade_plan": {
                "reference_price": 100.04,
                "sizing": {"basis": "minimum_viable_operator_default", "qty_per_order": 0.05},
                "levels": {
                    "range": {"lower": 99.96, "upper": 100.46},
                    "kill_switch": {"lower": 99.94, "upper": 100.56},
                    "grid_step": {"step_abs": 0.24, "step_pct": 0.24},
                    "tp_per_leg": {"abs": 0.21, "pct": 0.21},
                },
            },
        },
    }


def test_tick_snapping_preserves_grid_range_and_kill_switch_containment(app_main):
    snapped = app_main._snap_reco_payload_to_bybit_meta(_rec(), _meta())
    params = snapped["params"]
    levels = params["trade_plan"]["levels"]

    assert params["price_ref"] == pytest.approx(100.0)
    assert params["price_range_lower"] == pytest.approx(99.9)
    assert params["price_range_upper"] == pytest.approx(100.5)
    assert levels["range"]["lower"] == pytest.approx(99.9)
    assert levels["range"]["upper"] == pytest.approx(100.5)
    assert levels["kill_switch"]["lower"] == pytest.approx(99.9)
    assert levels["kill_switch"]["upper"] == pytest.approx(100.6)

    assert params["operator_sheet"]["range_lower"] == pytest.approx(99.9)
    assert params["operator_sheet"]["range_upper"] == pytest.approx(100.5)
    assert params["operator_sheet"]["kill_switch"]["lower"] == pytest.approx(99.9)
    assert params["operator_sheet"]["kill_switch"]["upper"] == pytest.approx(100.6)

    validation = app_main._validate_trade_plan_against_bybit_meta(snapped, _meta(), require_meta=True)
    assert validation["ok"] is True
    assert not {item["code"] for item in validation["errors"]} & {
        "KILL_SWITCH_INSIDE_MAIN_RANGE",
        "RANGE_COLLAPSES_AFTER_TICK_ROUNDING",
        "PRICE_OFF_TICK",
    }


def test_grid_step_and_tp_snap_up_so_economic_edge_is_not_thinned(app_main):
    snapped = app_main._snap_reco_payload_to_bybit_meta(_rec(), _meta())
    params = snapped["params"]
    levels = params["trade_plan"]["levels"]

    assert levels["grid_step"]["step_abs"] == pytest.approx(0.3)
    assert levels["grid_step"]["step_pct"] == pytest.approx(0.3)
    assert params["grid_spacing_pct"] == pytest.approx(0.3)
    assert levels["tp_per_leg"]["abs"] == pytest.approx(0.3)
    assert levels["tp_per_leg"]["pct"] == pytest.approx(0.3)
    assert params["operator_sheet"]["tp_per_leg"]["abs"] == pytest.approx(0.3)
