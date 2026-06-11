from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.direction import vote_for_tf
from app.trading_semantics import (
    bybit_linear_protective_order_semantics,
    directional_trade_math,
)


@pytest.mark.parametrize(
    ("direction", "entry", "tp", "sl", "qty", "profit", "loss", "rr"),
    [
        ("long", 100.0, 110.0, 95.0, 0.5, 5.0, 2.5, 2.0),
        ("short", 100.0, 90.0, 105.0, 0.5, 5.0, 2.5, 2.0),
    ],
)
def test_directional_trade_math_uses_symmetric_long_short_pnl_and_rr(
    direction: str,
    entry: float,
    tp: float,
    sl: float,
    qty: float,
    profit: float,
    loss: float,
    rr: float,
) -> None:
    trade = directional_trade_math(direction, entry, tp, sl, qty)

    assert trade is not None
    assert trade.direction == direction
    assert math.isclose(trade.gross_profit_usdt, profit)
    assert math.isclose(trade.gross_loss_usdt, loss)
    assert math.isclose(trade.risk_reward or 0.0, rr)
    assert math.isclose(trade.reward_pct, 10.0)
    assert math.isclose(trade.risk_pct, 5.0)


@pytest.mark.parametrize(
    ("direction", "entry", "tp", "sl", "qty"),
    [
        ("long", 100.0, 95.0, 110.0, 1.0),
        ("short", 100.0, 110.0, 95.0, 1.0),
        ("neutral", 100.0, 110.0, 95.0, 1.0),
        ("short", 0.0, 90.0, 105.0, 1.0),
        ("short", 100.0, 90.0, 105.0, 0.0),
    ],
)
def test_directional_trade_math_rejects_invalid_or_swapped_geometry(
    direction: str,
    entry: float,
    tp: float,
    sl: float,
    qty: float,
) -> None:
    assert directional_trade_math(direction, entry, tp, sl, qty) is None


@pytest.mark.parametrize(
    ("direction", "expected_side"),
    [
        ("long", "Sell"),
        ("short", "Buy"),
    ],
)
def test_bybit_protective_tp_sl_orders_are_always_reduce_only_close_orders(direction: str, expected_side: str) -> None:
    tp = bybit_linear_protective_order_semantics(direction, "take_profit")
    sl = bybit_linear_protective_order_semantics(direction, "stop_loss")

    for order in (tp, sl):
        assert order["category"] == "linear"
        assert order["position_mode"] == "one_way"
        assert order["positionIdx"] == 0
        assert order["side"] == expected_side
        assert order["reduceOnly"] is True
        assert order["closeOnTrigger"] is True
        assert order["orderFilter"] == "StopOrder"

    assert tp["triggerPurpose"] == "takeProfit"
    assert sl["triggerPurpose"] == "stopLoss"


def test_vote_for_tf_fails_neutral_on_empty_or_malformed_ohlc_instead_of_crashing() -> None:
    empty_vote = vote_for_tf([], [], [])
    malformed_vote = vote_for_tf([100.0, float("nan"), -1.0], [101.0], [99.0, 98.0, 97.0])

    for vote in (empty_vote, malformed_vote):
        assert vote["score"] == 0.0
        assert vote["trend_strength"] == 0.0
        assert vote["neutral_veto"] >= 0.8
        assert vote["data_quality"] == "insufficient_or_invalid_ohlc"


def test_vote_for_tf_sanitizes_mismatched_vectors_without_using_future_or_bad_rows() -> None:
    closes = [100.0 + i for i in range(80)] + [float("inf"), -10.0]
    highs = [c + 1.0 for c in closes]
    lows = [max(0.01, c - 1.0) for c in closes]

    vote = vote_for_tf(closes, highs, lows)

    assert math.isfinite(vote["score"])
    assert -1.0 <= vote["score"] <= 1.0
    assert math.isfinite(vote["atr_pct"])
    assert "data_quality" not in vote


def test_operator_ui_documents_short_tp_sl_directional_distances() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "short: TP ниже диапазона, SL выше диапазона" in app_js
    assert "directional_exit_levels" in app_js
    assert "Для лонга он выше входа, для шорта ниже входа" in app_js
    assert "Для лонга ниже входа, для шорта выше входа" in app_js
