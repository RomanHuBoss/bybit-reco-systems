from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _extract_cost_components, _grid_outcome, _has_complete_1m_window


def _params(*, cost_bps: float = 0.0, funding: dict | None = None) -> dict:
    cost_model = {
        "execution_cost_bps": float(cost_bps),
        "expected_funding_bps": 0.0,
    }
    cost_model.update(funding or {})
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "label_horizon_hours": 6,
        "cost_model": cost_model,
        "trade_plan": {
            "grid_count": 2,
            "cost_model": dict(cost_model),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 95.0, "upper": 105.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def _seed_candles(conn, *, base_ts: int, candles: list[tuple[float, float, float, float]]) -> None:
    db.upsert_ohlcv(conn, [
        {
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts + index * 60,
            "open": float(open_px),
            "high": float(high_px),
            "low": float(low_px),
            "close": float(close_px),
            "volume": 1_000.0,
        }
        for index, (open_px, high_px, low_px, close_px) in enumerate(candles)
    ])


@pytest.mark.parametrize(
    ("direction", "close_px"),
    [("long", 100.08), ("short", 99.92)],
)
def test_small_positive_directional_total_pnl_is_a_win(
    tmp_path: Path,
    direction: str,
    close_px: float,
) -> None:
    conn = db.connect(str(tmp_path / f"small-{direction}.db"))
    try:
        db.init_db(conn)
        base_ts = 1_706_000_000
        _seed_candles(
            conn,
            base_ts=base_ts,
            candles=[(100.0, max(100.0, close_px), min(100.0, close_px), close_px)],
        )

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            close_px,
            base_ts,
            base_ts + 60,
            direction,
            _params(),
        )

        # The 8-cent P&L is normalized by exact directional commitment:
        # Long = 100 + 99; Short = 100 + 101.
        commitment = 199.0 if direction == "long" else 201.0
        assert ret_proxy == pytest.approx(0.08 / commitment)
        assert success == 1
    finally:
        conn.close()


def test_positive_neutral_residual_total_pnl_is_a_win(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "neutral-residual.db"))
    try:
        db.init_db(conn)
        base_ts = 1_706_100_000
        _seed_candles(
            conn,
            base_ts=base_ts,
            candles=[
                (100.0, 101.1, 100.0, 101.0),
                (101.0, 101.0, 100.5, 100.5),
            ],
        )

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.5,
            base_ts,
            base_ts + 120,
            "neutral",
            _params(),
        )

        # The sell at 101 leaves one short slot marked/closed at 100.5:
        # +0.5 USDT on the 200 USDT full initial neutral commitment. It is positive
        # total P&L even though a full adjacent pair has not yet completed.
        assert ret_proxy == pytest.approx(0.5 / 200.0)
        assert success == 1
    finally:
        conn.close()


def test_exact_funding_schedule_with_no_event_in_horizon_charges_zero(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "funding-after-horizon.db"))
    try:
        db.init_db(conn)
        base_ts = 1_706_200_000
        _seed_candles(
            conn,
            base_ts=base_ts,
            candles=[(100.0, 100.1, 99.9, 100.0), (100.0, 100.1, 99.9, 100.0)],
        )
        params = _params(funding={
            "funding_rate": 0.001,
            "next_funding_ts": base_ts + 3_600,
            "funding_interval_min": 480,
            # Deliberately stale aggregate estimate. Exact schedule must win.
            "expected_funding_events": 1,
            "directional_funding_bps_per_event": 10.0,
            "expected_funding_bps": 10.0,
        })

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts,
            base_ts + 120,
            "long",
            params,
        )

        assert ret_proxy == pytest.approx(0.0)
        assert success == 0
    finally:
        conn.close()


def test_exact_funding_event_inside_horizon_still_charges_actual_inventory(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "funding-inside-horizon.db"))
    try:
        db.init_db(conn)
        base_ts = 1_706_300_000
        _seed_candles(
            conn,
            base_ts=base_ts,
            candles=[(100.0, 100.1, 99.9, 100.0), (100.0, 100.1, 99.9, 100.0)],
        )
        params = _params(funding={
            "funding_rate": 0.001,
            "next_funding_ts": base_ts + 60,
            "funding_interval_min": 480,
            "expected_funding_events": 1,
            "directional_funding_bps_per_event": 10.0,
            "expected_funding_bps": 10.0,
        })
        db.upsert_funding_settlements(conn, [{
            "symbol": "BTCUSDT", "ts": base_ts + 60, "funding_rate": 0.001,
        }])

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts,
            base_ts + 120,
            "long",
            params,
        )

        # One long slot pays 0.1 USDT on exact 199-USDT commitment.
        assert ret_proxy == pytest.approx(-0.1 / 199.0)
        assert success == 0
    finally:
        conn.close()


@pytest.mark.parametrize("primary", [0.0, True])
def test_conflicting_cost_alias_cannot_hide_conservative_execution_cost(primary: object) -> None:
    execution_bps, funding_bps = _extract_cost_components({
        "cost_model": {"execution_cost_bps": primary, "expected_funding_bps": 0.0},
        "trade_plan": {"cost_model": {"execution_cost_bps": 20.0, "expected_funding_bps": 0.0}},
    })

    assert execution_bps == pytest.approx(20.0)
    assert funding_bps == pytest.approx(0.0)


def test_malformed_ohlcv_row_makes_horizon_incomplete(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "malformed-window.db"))
    try:
        db.init_db(conn)
        entry_ts = 1_706_400_000
        horizon_sec = 6 * 60
        rows = []
        for ts in range(entry_ts, entry_ts + horizon_sec, 60):
            rows.append({
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": ts,
                "open": 100.0,
                "high": 100.1,
                "low": 99.9,
                "close": 100.0,
                "volume": 1_000.0,
            })
        db.upsert_ohlcv(conn, rows)
        # Simulate a legacy/manual poisoned row that bypassed modern ingestion:
        # high is below both open and close, so this is not a valid candle.
        conn.execute(
            "UPDATE ohlcv SET high=99.0, low=98.0 WHERE venue=? AND symbol=? AND tf_sec=60 AND ts=?",
            ("linear", "BTCUSDT", entry_ts + 120),
        )
        conn.commit()

        assert _has_complete_1m_window(
            conn, "linear", "BTCUSDT", entry_ts, entry_ts + horizon_sec
        ) is False
    finally:
        conn.close()

def test_outcome_contract_is_bumped_for_label_integrity() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.5.1"' in source
