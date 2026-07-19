from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.grid_math import arithmetic_grid_commitment
from app.outcomes import _grid_outcome


def _params() -> dict:
    cost_model = {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0}
    return {
        "grid_count": 5,
        "grid_levels": 5,
        "price_range_lower": 10_000.0,
        "price_range_upper": 30_000.0,
        "cost_model": dict(cost_model),
        "trade_plan": {
            "grid_count": 5,
            "cost_model": dict(cost_model),
            "levels": {
                "range": {"lower": 10_000.0, "upper": 30_000.0},
                "kill_switch": {"lower": 9_000.0, "upper": 31_000.0},
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


def test_neutral_off_grid_omits_upper_bridge_level() -> None:
    topology = arithmetic_grid_commitment(
        lower=10_000, upper=30_000, grid_count=5, reference_price=20_000, direction="neutral"
    )
    assert topology is not None
    assert topology["grid_prices"] == pytest.approx([10_000, 14_000, 18_000, 22_000, 26_000, 30_000])
    assert topology["buy_indices"] == [0, 1, 2]
    assert topology["sell_indices"] == [4, 5]
    assert topology["idle_grid_index"] == 3
    assert topology["active_order_count"] == 5
    assert topology["committed_slot_count"] == 5
    assert topology["max_abs_position_slots"] == 3
    assert topology["committed_notional_per_qty"] == pytest.approx(98_000.0)


def test_long_off_grid_omits_nearest_tp_and_initial_lot() -> None:
    topology = arithmetic_grid_commitment(
        lower=10_000, upper=30_000, grid_count=5, reference_price=20_000, direction="long"
    )
    assert topology is not None
    assert topology["buy_indices"] == [0, 1, 2]
    assert topology["sell_indices"] == [4, 5]
    assert topology["idle_grid_index"] == 3
    assert topology["initial_long_slots"] == 2
    assert topology["initial_position_slots"] == 2
    assert topology["active_order_count"] == 5
    assert topology["committed_slot_count"] == 5
    assert topology["committed_notional_per_qty"] == pytest.approx(82_000.0)


def test_short_off_grid_omits_nearest_close_and_initial_lot() -> None:
    topology = arithmetic_grid_commitment(
        lower=10_000, upper=30_000, grid_count=5, reference_price=20_000, direction="short"
    )
    assert topology is not None
    assert topology["buy_indices"] == [0, 1]
    assert topology["sell_indices"] == [3, 4, 5]
    assert topology["idle_grid_index"] == 2
    assert topology["initial_short_slots"] == 2
    assert topology["initial_position_slots"] == -2
    assert topology["active_order_count"] == 5
    assert topology["committed_slot_count"] == 5
    assert topology["committed_notional_per_qty"] == pytest.approx(118_000.0)


def test_neutral_first_move_to_idle_upper_level_does_not_open_short(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "neutral-idle.db"))
    try:
        db.init_db(conn)
        base_ts = 1_720_000_000
        _seed(conn, base_ts, [(20_000.0, 22_000.0, 20_000.0, 22_000.0)])
        result = _grid_outcome(
            conn, "linear", "BTCUSDT", 20_000.0, 20_000.0,
            base_ts, base_ts + 60, "neutral", _params(),
        )
        assert result == pytest.approx((0, 0.0))
    finally:
        conn.close()


def test_long_first_move_to_idle_upper_level_does_not_realise_profit(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long-idle.db"))
    try:
        db.init_db(conn)
        base_ts = 1_720_100_000
        _seed(conn, base_ts, [(20_000.0, 22_000.0, 20_000.0, 22_000.0)])
        result = _grid_outcome(
            conn, "linear", "BTCUSDT", 20_000.0, 20_000.0,
            base_ts, base_ts + 60, "long", _params(),
        )
        assert result == pytest.approx((0, 0.0))
    finally:
        conn.close()


def test_short_first_move_to_idle_lower_level_does_not_realise_profit(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "short-idle.db"))
    try:
        db.init_db(conn)
        base_ts = 1_720_200_000
        _seed(conn, base_ts, [(20_000.0, 20_000.0, 18_000.0, 18_000.0)])
        result = _grid_outcome(
            conn, "linear", "BTCUSDT", 20_000.0, 20_000.0,
            base_ts, base_ts + 60, "short", _params(),
        )
        assert result == pytest.approx((0, 0.0))
    finally:
        conn.close()


def test_neutral_bridge_order_is_created_only_after_adjacent_buy_fill(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "neutral-cycle.db"))
    try:
        db.init_db(conn)
        base_ts = 1_720_300_000
        _seed(conn, base_ts, [
            (20_000.0, 20_000.0, 17_900.0, 17_900.0),
            (17_900.0, 22_100.0, 17_900.0, 22_100.0),
        ])
        success, ret = _grid_outcome(
            conn, "linear", "BTCUSDT", 20_000.0, 20_000.0,
            base_ts, base_ts + 120, "neutral", _params(),
        )
        assert success == 1
        assert ret == pytest.approx(4_000.0 / 98_000.0)
    finally:
        conn.close()


def test_outcome_contract_is_bumped_for_dynamic_bridge_topology() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.1.0"' in source
