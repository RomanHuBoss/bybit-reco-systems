from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome
from app.recommender import DIRECTION_CALIBRATION_KEY


def _seed_flat(conn, base_ts: int, minutes: int = 2) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base_ts + index * 60,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1_000.0,
            }
            for index in range(minutes)
        ],
    )


def _params(base_ts: int, direction: str) -> dict:
    rate = 0.001
    signed_bps = rate * 10_000 if direction == "long" else -rate * 10_000
    cost = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "market_round_trip_cost_bps": 0.0,
        "funding_rate": rate,
        "next_funding_ts": base_ts + 60,
        "funding_interval_min": 60,
        "expected_funding_events": 1,
        "directional_funding_bps_per_event": signed_bps,
        "expected_funding_bps": signed_bps,
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
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def test_positive_settled_funding_receipt_cannot_create_short_grid_edge(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "short_receipt.db"))
    try:
        db.init_db(conn)
        base = 1_730_000_000
        _seed_flat(conn, base)
        db.upsert_funding_settlements(
            conn,
            [{"symbol": "BTCUSDT", "ts": base + 60, "funding_rate": 0.001}],
        )

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base,
            base + 120,
            "short",
            _params(base, "short"),
        )

        assert result is not None
        success, ret = result
        assert success == 0
        assert ret == pytest.approx(0.0)
    finally:
        conn.close()


def test_negative_settled_funding_receipt_cannot_create_long_grid_edge(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long_receipt.db"))
    try:
        db.init_db(conn)
        base = 1_730_100_000
        _seed_flat(conn, base)
        db.upsert_funding_settlements(
            conn,
            [{"symbol": "BTCUSDT", "ts": base + 60, "funding_rate": -0.001}],
        )

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base,
            base + 120,
            "long",
            _params(base, "long"),
        )

        assert result is not None
        success, ret = result
        assert success == 0
        assert ret == pytest.approx(0.0)
    finally:
        conn.close()


def test_adverse_settled_funding_is_still_charged(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "adverse.db"))
    try:
        db.init_db(conn)
        base = 1_730_200_000
        _seed_flat(conn, base)
        db.upsert_funding_settlements(
            conn,
            [{"symbol": "BTCUSDT", "ts": base + 60, "funding_rate": 0.001}],
        )

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base,
            base + 120,
            "long",
            _params(base, "long"),
        )

        assert result is not None
        success, ret = result
        assert success == 0
        assert ret < 0.0
    finally:
        conn.close()


def test_outcome_contract_reset_deletes_current_direction_calibrator() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    start = source.index("def _bootstrap_db")
    end = source.index("\n\n_bootstrap_db()", start)
    reset_block = source[start:end]
    assert DIRECTION_CALIBRATION_KEY.startswith("platt_direction_")
    assert "platt_direction_%" in reset_block
