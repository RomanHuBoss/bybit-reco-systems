from __future__ import annotations

from pathlib import Path

import pytest

from app.trading_semantics import bybit_linear_protective_order_semantics


@pytest.mark.parametrize(
    ("direction", "exit_kind", "expected_side", "expected_trigger_direction"),
    [
        ("long", "take_profit", "Sell", 1),
        ("long", "stop_loss", "Sell", 2),
        ("short", "take_profit", "Buy", 2),
        ("short", "stop_loss", "Buy", 1),
    ],
)
def test_linear_protective_order_semantics_do_not_emit_spot_only_order_filter(
    direction: str,
    exit_kind: str,
    expected_side: str,
    expected_trigger_direction: int,
) -> None:
    order = bybit_linear_protective_order_semantics(direction, exit_kind)

    assert order["category"] == "linear"
    assert order["positionIdx"] == 0
    assert order["side"] == expected_side
    assert order["reduceOnly"] is True
    assert order["closeOnTrigger"] is True
    assert order["triggerDirection"] == expected_trigger_direction
    assert order["triggerBy"] == "LastPrice"
    assert order["orderType"] == "Market"
    assert "orderFilter" not in order


def test_operator_ui_validates_backend_tp_sl_against_reference_price() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "function directionalExitGeometryOk(direction, takeProfit, stopLoss, referencePrice = null)" in app_js
    assert "const ref = toFiniteNumber(referencePrice);" in app_js
    assert 'if (dir === "long") return tp > ref && sl < ref;' in app_js
    assert 'return tp < ref && sl > ref;' in app_js
    assert "directionalExitGeometryOk(dir, exitLevels.take_profit, exitLevels.stop_loss, exitLevels.reference_price)" in app_js
