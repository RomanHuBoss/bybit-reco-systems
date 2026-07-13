from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome


def _params() -> dict:
    cost = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "market_round_trip_cost_bps": 0.0,
        "expected_funding_bps": 0.0,
        "expected_funding_events": 0,
    }
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "cost_model": dict(cost),
        "trade_plan": {
            "grid_count": 2,
            "cost_model": dict(cost),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 102.0},
                "grid_step": {"step_abs": 1.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def _seed(conn, base_ts: int, candles: list[tuple[float, float, float, float]]) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base_ts + index * 60,
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "volume": 1_000.0,
            }
            for index, (open_px, high_px, low_px, close_px) in enumerate(candles)
        ],
    )


def test_exact_ohlc_touch_does_not_prove_two_resting_limit_fills(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "touch-only.db"))
    try:
        db.init_db(conn)
        base = 1_740_000_000
        _seed(conn, base, [(100.0, 100.0, 99.0, 100.0)])

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base,
            base + 60,
            "neutral",
            _params(),
        )

        assert result is not None
        success, ret = result
        assert success == 0
        assert ret == pytest.approx(0.0)
    finally:
        conn.close()


def test_trade_through_below_buy_and_above_replacement_sell_confirms_cycle(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "trade-through.db"))
    try:
        db.init_db(conn)
        base = 1_740_100_000
        _seed(
            conn,
            base,
            [
                (100.0, 100.0, 98.9, 98.9),
                (98.9, 100.1, 98.9, 100.1),
            ],
        )

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.1,
            base,
            base + 120,
            "neutral",
            _params(),
        )

        assert result is not None
        success, ret = result
        assert success == 1
        assert ret > 0.0
    finally:
        conn.close()


def test_touch_then_later_trade_through_keeps_resting_order(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "touch-then-through.db"))
    try:
        db.init_db(conn)
        base = 1_740_200_000
        _seed(
            conn,
            base,
            [
                (100.0, 100.0, 99.0, 99.0),
                (99.0, 99.0, 98.9, 98.9),
                (98.9, 100.1, 98.9, 100.1),
            ],
        )

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.1,
            base,
            base + 180,
            "neutral",
            _params(),
        )

        assert result is not None
        success, ret = result
        assert success == 1
        assert ret > 0.0
    finally:
        conn.close()
