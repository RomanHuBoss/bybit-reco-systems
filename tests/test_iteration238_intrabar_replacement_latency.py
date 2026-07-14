from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome


def _params() -> dict:
    cost_model = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "expected_funding_bps": 0.0,
    }
    sizing = {"qty_per_order": 1.0}
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "sizing": dict(sizing),
        "cost_model": dict(cost_model),
        "trade_plan": {
            "grid_count": 2,
            "sizing": dict(sizing),
            "cost_model": dict(cost_model),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 103.0},
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
                "volume": 10.0,
            }
            for index, (open_px, high_px, low_px, close_px) in enumerate(candles)
        ],
    )


def test_same_candle_replacement_fill_is_unavailable_without_order_timestamps(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "same-candle-replacement.db"))
    try:
        db.init_db(conn)
        base_ts = 1_715_000_000
        _seed(
            conn,
            base_ts,
            [
                # Sell 101 is an initial resting order. The later move below 100
                # can fill its replacement Buy only if the bot submitted that new
                # order before the reversal. One-minute OHLCV cannot prove that.
                (100.0, 101.1, 99.9, 99.9),
                (101.5, 101.5, 101.5, 101.5),
            ],
        )
        diagnostics: dict[str, object] = {}

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            101.5,
            base_ts,
            base_ts + 120,
            "neutral",
            _params(),
            diagnostics=diagnostics,
        )

        assert result is None
        assert diagnostics["reason"] == "intrabar_replacement_fill_timing_unobservable"
        assert diagnostics["event_ts"] == base_ts
        assert diagnostics["fill_price"] == pytest.approx(100.0)
    finally:
        conn.close()


def test_replacement_becomes_labelable_on_the_next_candle(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "next-candle-replacement.db"))
    try:
        db.init_db(conn)
        base_ts = 1_715_100_000
        _seed(
            conn,
            base_ts,
            [
                (100.0, 101.1, 100.0, 101.1),
                (101.1, 101.1, 99.9, 99.9),
            ],
        )

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            99.9,
            base_ts,
            base_ts + 120,
            "neutral",
            _params(),
        )

        assert result is not None
        success, ret_proxy = result
        assert success == 1
        assert ret_proxy == pytest.approx(1.0 / 200.0)
    finally:
        conn.close()


def test_outcome_contract_is_bumped_for_intrabar_order_latency() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.0.57"' in source
