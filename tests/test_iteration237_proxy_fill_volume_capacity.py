from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome


def _params(*, qty: float = 1.0, direction: str = "neutral", grid_count: int = 2, lower: float = 99.0, upper: float = 101.0) -> dict:
    step = (upper - lower) / grid_count
    cost = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "market_round_trip_cost_bps": 0.0,
        "expected_funding_bps": 0.0,
        "expected_funding_events": 0,
    }
    sizing = {"qty_per_order": qty, "order_qty": qty, "grid_count": grid_count}
    return {
        "direction": direction,
        "grid_count": grid_count,
        "grid_levels": grid_count,
        "price_ref": 100.0,
        "price_range_lower": lower,
        "price_range_upper": upper,
        "sizing": dict(sizing),
        "cost_model": dict(cost),
        "trade_plan": {
            "grid_count": grid_count,
            "reference_price": 100.0,
            "sizing": dict(sizing),
            "cost_model": dict(cost),
            "levels": {
                "range": {"lower": lower, "upper": upper},
                "kill_switch": {"lower": lower - step, "upper": upper + step},
                "grid_step": {"step_abs": step},
                "tp_per_leg": {"abs": step},
            },
        },
    }


def _seed(conn, base: int, candles: list[tuple[float, float, float, float, float]]) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base + index * 60,
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "volume": volume,
            }
            for index, (open_px, high_px, low_px, close_px, volume) in enumerate(candles)
        ],
    )


def test_order_larger_than_total_candle_volume_cannot_be_a_full_proxy_fill(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "single-fill-volume.db"))
    try:
        db.init_db(conn)
        base = 1_741_000_000
        _seed(
            conn,
            base,
            [
                (100.0, 100.0, 98.9, 98.9, 1.0),
                (98.9, 100.1, 98.9, 100.1, 1.0),
            ],
        )
        diagnostics: dict[str, object] = {}
        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.1,
            base,
            base + 120,
            "neutral",
            _params(qty=10.0),
            diagnostics=diagnostics,
        )

        assert result is None
        assert diagnostics["reason"] == "insufficient_candle_volume_for_full_fill"
        assert diagnostics["required_fill_qty"] == pytest.approx(10.0)
        assert diagnostics["candle_volume"] == pytest.approx(1.0)
    finally:
        conn.close()


def test_multiple_crossed_orders_cannot_consume_more_than_total_candle_volume(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "cumulative-volume.db"))
    try:
        db.init_db(conn)
        base = 1_741_100_000
        _seed(conn, base, [(100.0, 100.0, 97.9, 97.9, 1.5)])
        diagnostics: dict[str, object] = {}
        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            97.9,
            base,
            base + 60,
            "neutral",
            _params(qty=1.0, grid_count=4, lower=98.0, upper=102.0),
            diagnostics=diagnostics,
        )

        assert result is None
        assert diagnostics["reason"] == "insufficient_candle_volume_for_full_fill"
        assert diagnostics["volume_used_before_fill"] == pytest.approx(1.0)
        assert diagnostics["required_fill_qty"] == pytest.approx(1.0)
        assert diagnostics["candle_volume"] == pytest.approx(1.5)
    finally:
        conn.close()


def test_directional_initial_inventory_must_fit_observed_entry_candle_volume(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "initial-inventory-volume.db"))
    try:
        db.init_db(conn)
        base = 1_741_200_000
        _seed(conn, base, [(100.0, 100.0, 100.0, 100.0, 5.0)])
        diagnostics: dict[str, object] = {}
        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base,
            base + 60,
            "long",
            _params(qty=10.0, direction="long"),
            diagnostics=diagnostics,
        )

        assert result is None
        assert diagnostics["reason"] == "insufficient_candle_volume_for_initial_inventory"
        assert diagnostics["required_fill_qty"] == pytest.approx(10.0)
        assert diagnostics["candle_volume"] == pytest.approx(5.0)
    finally:
        conn.close()


def test_sufficient_observed_volume_preserves_confirmed_grid_cycle(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "sufficient-volume.db"))
    try:
        db.init_db(conn)
        base = 1_741_300_000
        _seed(
            conn,
            base,
            [
                (100.0, 100.0, 98.9, 98.9, 100.0),
                (98.9, 100.1, 98.9, 100.1, 100.0),
            ],
        )
        diagnostics: dict[str, object] = {}
        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.1,
            base,
            base + 120,
            "neutral",
            _params(qty=10.0),
            diagnostics=diagnostics,
        )

        assert result is not None
        success, ret = result
        assert success == 1
        assert ret > 0.0
        assert diagnostics["fill_volume_confirmation"] == "aggregate_candle_and_liquidation_volume_cap_v2"
    finally:
        conn.close()


def test_outcome_contract_reset_removes_all_legacy_calibrator_keys() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    start = source.index("def _bootstrap_db")
    end = source.index("\n\n_bootstrap_db()", start)
    bootstrap = source[start:end]
    assert "logreg_%" in bootstrap
    assert "platt_direction_%" in bootstrap
