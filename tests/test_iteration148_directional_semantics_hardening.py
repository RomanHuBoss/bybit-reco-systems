from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app.trading_semantics import (
    bybit_linear_order_semantics,
    directional_exit_levels,
    validate_directional_exit_geometry,
)


def test_directional_exit_levels_are_canonical_for_long_short_and_neutral() -> None:
    long_exits = directional_exit_levels("long", 95, 105)
    short_exits = directional_exit_levels("short", 95, 105)
    neutral_exits = directional_exit_levels("neutral", 95, 105)

    assert long_exits.take_profit == 105
    assert long_exits.stop_loss == 95
    assert "long" in long_exits.geometry

    assert short_exits.take_profit == 95
    assert short_exits.stop_loss == 105
    assert "short" in short_exits.geometry

    assert neutral_exits.take_profit is None
    assert neutral_exits.stop_loss is None
    assert neutral_exits.has_directional_take_profit is False
    assert neutral_exits.kill_switch_lower == 95
    assert neutral_exits.kill_switch_upper == 105


@pytest.mark.parametrize(
    ("direction", "entry", "tp", "sl"),
    [
        ("long", 100, 105, 95),
        ("short", 100, 95, 105),
    ],
)
def test_directional_exit_geometry_accepts_only_correct_profit_and_loss_sides(direction: str, entry: float, tp: float, sl: float) -> None:
    assert validate_directional_exit_geometry(direction, entry, tp, sl) == []


def test_directional_exit_geometry_rejects_swapped_short_tp_sl() -> None:
    errors = validate_directional_exit_geometry("short", 100, 105, 95)
    codes = {err["code"] for err in errors}

    assert "SHORT_TP_NOT_BELOW_ENTRY" in codes
    assert "SHORT_SL_NOT_ABOVE_ENTRY" in codes


def test_bybit_linear_order_semantics_is_symmetric_and_reduce_only_on_close() -> None:
    assert bybit_linear_order_semantics("long", "open") == {
        "category": "linear",
        "position_mode": "one_way",
        "positionIdx": 0,
        "direction": "long",
        "action": "open",
        "side": "Buy",
        "reduceOnly": False,
        "closeOnTrigger": False,
    }
    assert bybit_linear_order_semantics("long", "close")["side"] == "Sell"
    assert bybit_linear_order_semantics("long", "close")["reduceOnly"] is True
    assert bybit_linear_order_semantics("short", "open")["side"] == "Sell"
    assert bybit_linear_order_semantics("short", "close")["side"] == "Buy"
    assert bybit_linear_order_semantics("short", "close")["closeOnTrigger"] is True


def test_neutral_grid_cannot_be_silently_mapped_to_single_bybit_order() -> None:
    with pytest.raises(ValueError):
        bybit_linear_order_semantics("neutral", "open")


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration148.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration148_runtime_lock.db"))
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


def test_backend_augments_recommendation_with_directional_exit_payload(app_main, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: _meta())

    out = app_main._augment_reco_for_ui(_rec("short"))
    exits = out["directional_exit_levels"]

    assert exits["direction"] == "short"
    assert exits["take_profit"] == 88.0
    assert exits["stop_loss"] == 112.0
    assert out["bybit_operator_guard"]["ok"] is True


def test_execution_preflight_rejects_swapped_short_exit_geometry(app_main) -> None:
    rec = _rec("short")
    rec["params"]["trade_plan"]["levels"]["kill_switch"] = {"lower": 101.0, "upper": 112.0}

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True, require_execution_plan=True)
    codes = {err["code"] for err in validation["errors"]}

    assert "SHORT_TP_NOT_BELOW_ENTRY" in codes
    assert validation["ok"] is False


def test_operator_ui_uses_backend_directional_exit_payload_when_available() -> None:
    app_js = (Path(__file__).resolve().parent.parent / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "function operatorExitLevelsFromBackend(exitLevels, fallback, meta = {})" in app_js
    assert "directional_exit_levels" in app_js
    assert "const rawBackendExits = (it || {}).directional_exit_levels;" in app_js
    assert "operatorExitLevelsFromBackend(rawBackendExits, exits, meta)" in app_js
    assert "...canonicalExits" in app_js
