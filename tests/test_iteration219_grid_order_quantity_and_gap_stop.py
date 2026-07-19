from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app.main import _execution_daily_loss_budget_guard
from app.outcomes import _grid_outcome


def _params(*, cost_bps: float = 0.0, funding: dict | None = None, lower: float = 99.0, upper: float = 101.0, grid_count: int = 2) -> dict:
    cost_model = {"execution_cost_bps": cost_bps, "expected_funding_bps": 0.0}
    if funding:
        cost_model.update(funding)
    return {
        "grid_count": grid_count,
        "grid_levels": grid_count,
        "price_range_lower": lower,
        "price_range_upper": upper,
        "cost_model": dict(cost_model),
        "trade_plan": {
            "grid_count": grid_count,
            "cost_model": dict(cost_model),
            "levels": {
                "range": {"lower": lower, "upper": upper},
                "kill_switch": {"lower": lower - 1.0, "upper": upper + 1.0},
                "tp_per_leg": {"abs": (upper - lower) / grid_count},
            },
        },
    }


def _seed(conn, *, base_ts: int, candles: list[tuple[float, float, float, float]]) -> None:
    db.upsert_ohlcv(conn, [
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
    ])


def test_long_same_level_replacement_order_keeps_full_quantity(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long-quantity.db"))
    try:
        db.init_db(conn)
        base_ts = 1_710_000_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.5, 100.5, 99.9, 99.9),
            (99.9, 102.1, 99.9, 102.1),
            (102.1, 102.1, 99.9, 99.9),
        ])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.5, 100.0,
            base_ts, base_ts + 180, "long", _params(lower=99.0, upper=102.0, grid_count=3),
        )

        # Level 101 is initially idle. Initial long closes at 102 (+1.5);
        # dynamic replacements preserve the realised PnL through the return to 100.
        assert ret_proxy == pytest.approx(1.5 / 299.5)
        assert success == 1
    finally:
        conn.close()


def test_short_same_level_replacement_order_keeps_full_quantity(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "short-quantity.db"))
    try:
        db.init_db(conn)
        base_ts = 1_710_100_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.5, 101.1, 100.5, 101.1),
            (101.1, 101.1, 98.9, 98.9),
            (98.9, 101.1, 98.9, 101.1),
        ])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.5, 101.0,
            base_ts, base_ts + 180, "short", _params(lower=99.0, upper=102.0, grid_count=3),
        )

        assert ret_proxy == pytest.approx(1.5 / 303.5)
        assert success == 1
    finally:
        conn.close()


def test_duplicate_level_quantity_charges_every_execution_leg(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "quantity-fees.db"))
    try:
        db.init_db(conn)
        base_ts = 1_710_200_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.5, 100.5, 99.9, 99.9),
            (99.9, 102.1, 99.9, 102.1),
            (102.1, 102.1, 99.9, 99.9),
        ])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.5, 100.0,
            base_ts, base_ts + 180, "long", _params(cost_bps=10.0, lower=99.0, upper=102.0, grid_count=3),
        )

        # 5 bps per leg on notionals: 100.5 initial + 100 buy + 101 sell
        # + 102 sell + 101/100 replacement buys + 200 terminal close = 804.5.
        expected = (1.5 - 804.5 * 0.0005) / 299.5
        assert ret_proxy == pytest.approx(expected)
        assert success == 1
    finally:
        conn.close()


def test_same_level_quantity_does_not_leave_phantom_inventory_at_funding(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "quantity-funding.db"))
    try:
        db.init_db(conn)
        base_ts = 1_710_300_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.5, 100.5, 99.9, 99.9),
            (99.9, 102.1, 99.9, 102.1),
            (102.1, 102.1, 102.1, 102.1),
        ])
        funding = {
            "funding_rate": 0.001,
            "next_funding_ts": base_ts + 120,
            "funding_interval_min": 480,
            "expected_funding_events": 1,
            "directional_funding_bps_per_event": 10.0,
            "expected_funding_bps": 10.0,
        }

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.5, 102.0,
            base_ts, base_ts + 180, "long", _params(funding=funding, lower=99.0, upper=102.0, grid_count=3),
        )

        # Both initial/opened long lots are closed before the funding timestamp.
        assert ret_proxy == pytest.approx(2.5 / 299.5)
        assert success == 1
    finally:
        conn.close()


def test_intrawindow_gap_through_upper_kill_switch_is_unlabelable(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "upper-gap.db"))
    try:
        db.init_db(conn)
        base_ts = 1_710_400_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.0, 100.0, 100.0, 100.0),
            (103.0, 103.0, 103.0, 103.0),
        ])

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 103.0,
            base_ts, base_ts + 120, "neutral", _params(),
        ) is None
    finally:
        conn.close()


def test_horizon_gap_through_lower_kill_switch_is_unlabelable(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "lower-gap.db"))
    try:
        db.init_db(conn)
        base_ts = 1_710_500_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.0, 100.0, 100.0, 100.0),
        ])

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 97.0,
            base_ts, base_ts + 60, "neutral", _params(),
        ) is None
    finally:
        conn.close()


def test_daily_loss_fallback_uses_off_grid_active_order_count() -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "direction": "long",
        "params": {
            "grid_count": 2,
            "grid_levels": 2,
            "price_ref": 100.5,
            "price_range_lower": 99.0,
            "price_range_upper": 101.0,
            "sizing": {"qty_per_order": 1.0},
            "cost_model": {"execution_cost_bps": 0.0},
            "trade_plan": {
                "grid_count": 2,
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.0, "upper": 102.0},
                },
            },
        },
    }
    result = _execution_daily_loss_budget_guard(
        rec,
        {"max_daily_dd_usdt": 1_000.0},
        SimpleNamespace(daily_dd=0.0),
    )

    assert result["estimated_position_notional_usdt"] == pytest.approx(202.0)
    assert result["estimated_position_notional_source"] == "qty*max_grid_price*committed_slots"


def test_outcome_contract_is_bumped_for_order_quantity_and_gap_semantics() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.0.78"' in source
