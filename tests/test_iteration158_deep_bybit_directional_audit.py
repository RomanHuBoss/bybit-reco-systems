from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app.trading_semantics import (
    bybit_linear_protective_order_plan,
    validate_protective_trigger_geometry,
)


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration158.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration158_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _rec(direction: str = "short") -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": direction,
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "price_ref": 100.0,
            "price_range_lower": 90.0,
            "price_range_upper": 110.0,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 90.0, "upper": 110.0},
                    "kill_switch": {"lower": 80.0, "upper": 120.0},
                },
            },
        },
    }


def test_short_directional_exit_payload_exposes_executable_protective_order_intent(app_main) -> None:
    payload = app_main._directional_exit_payload_for_reco(_rec("short"))

    assert payload["direction"] == "short"
    assert payload["reference_price"] == 100.0
    assert payload["take_profit"] == 80.0
    assert payload["stop_loss"] == 120.0
    assert payload["geometry_valid"] is True
    assert payload["geometry_errors"] == []

    math_payload = payload["trade_math"]
    assert math_payload["gross_profit_usdt"] == pytest.approx(20.0)
    assert math_payload["gross_loss_usdt"] == pytest.approx(20.0)
    assert math_payload["risk_reward"] == pytest.approx(1.0)
    assert math_payload["take_profit_distance_pct"] == pytest.approx(20.0)
    assert math_payload["stop_loss_distance_pct"] == pytest.approx(20.0)

    tp_order = payload["bybit_protective_orders"]["take_profit"]
    sl_order = payload["bybit_protective_orders"]["stop_loss"]
    assert tp_order["side"] == "Buy"
    assert tp_order["triggerPrice"] == 80.0
    assert tp_order["triggerDirection"] == 2
    assert tp_order["reduceOnly"] is True
    assert tp_order["closeOnTrigger"] is True
    assert tp_order["geometry_valid"] is True
    assert sl_order["side"] == "Buy"
    assert sl_order["triggerPrice"] == 120.0
    assert sl_order["triggerDirection"] == 1
    assert sl_order["reduceOnly"] is True
    assert sl_order["closeOnTrigger"] is True
    assert sl_order["geometry_valid"] is True


def test_invalid_directional_geometry_does_not_publish_protective_bybit_orders(app_main) -> None:
    rec = _rec("short")
    rec["params"]["trade_plan"]["levels"]["kill_switch"] = {"lower": 105.0, "upper": 120.0}

    payload = app_main._directional_exit_payload_for_reco(rec)
    codes = {err["code"] for err in payload["geometry_errors"]}

    assert payload["geometry_valid"] is False
    assert payload["trade_math"] is None
    assert payload["bybit_protective_orders"] == {}
    assert "SHORT_TP_NOT_BELOW_ENTRY" in codes


@pytest.mark.parametrize(
    ("direction", "exit_kind", "reference", "trigger", "expected_trigger_direction"),
    [
        ("long", "take_profit", 100.0, 110.0, 1),
        ("long", "stop_loss", 100.0, 90.0, 2),
        ("short", "take_profit", 100.0, 90.0, 2),
        ("short", "stop_loss", 100.0, 110.0, 1),
    ],
)
def test_protective_order_plan_geometry_and_trigger_direction_are_canonical(
    direction: str,
    exit_kind: str,
    reference: float,
    trigger: float,
    expected_trigger_direction: int,
) -> None:
    plan = bybit_linear_protective_order_plan(direction, exit_kind, trigger, reference)

    assert plan["geometry_valid"] is True
    assert plan["geometry_errors"] == []
    assert plan["triggerPrice"] == trigger
    assert plan["reference_price"] == reference
    assert plan["triggerDirection"] == expected_trigger_direction
    assert plan["reduceOnly"] is True
    assert plan["closeOnTrigger"] is True


def test_protective_trigger_geometry_fails_closed_on_short_take_profit_above_reference() -> None:
    errors = validate_protective_trigger_geometry("short", "take_profit", 100.0, 105.0)
    codes = {err["code"] for err in errors}

    assert "SHORT_TP_TRIGGER_NOT_BELOW_REFERENCE" in codes
    assert "PROTECTIVE_TRIGGER_DIRECTION_MISMATCH" in codes


def test_operator_ui_surfaces_backend_directional_risk_reward_and_distances() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "function directionalExitMathForDisplay" in app_js
    assert "const exitMath = directionalExitMathForDisplay(it);" in app_js
    assert "exitLevels.geometry_valid === false" in app_js
    assert "TP/SL дистанция" in app_js
    assert "Risk/Reward TP/SL" in app_js
    assert "Для short TP считается вниз, SL — вверх" in app_js
