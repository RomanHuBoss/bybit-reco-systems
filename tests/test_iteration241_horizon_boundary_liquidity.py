from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app import outcomes as outcomes_module
from app.outcomes import _grid_outcome, compute_outcomes_once


def _params(*, direction: str = "neutral", qty: float = 1.0) -> dict:
    cost = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "expected_funding_bps": 0.0,
        "expected_funding_events": 0,
    }
    sizing = {"qty_per_order": qty, "order_qty": qty, "grid_count": 2}
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "sizing": dict(sizing),
        "cost_model": dict(cost),
        "trade_plan": {
            "grid_count": 2,
            "sizing": dict(sizing),
            "cost_model": dict(cost),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 102.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def _seed(conn, rows: list[tuple[int, float, float, float, float, float]]) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": ts,
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "volume": volume,
            }
            for ts, open_px, high_px, low_px, close_px, volume in rows
        ],
    )


def test_horizon_gap_fill_uses_boundary_candle_volume_not_previous_minute(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "boundary-gap-volume.db"))
    try:
        db.init_db(conn)
        base = 1_753_000_000
        _seed(
            conn,
            [
                (base, 100.0, 100.0, 100.0, 100.0, 100.0),
                # Exact horizon candle. The open gaps through Buy 99, but the
                # whole boundary minute trades only 0.5 BTC versus a 1 BTC order.
                (base + 60, 98.9, 98.9, 98.9, 98.9, 0.5),
            ],
        )
        diagnostics: dict[str, object] = {}
        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            98.9,
            base,
            base + 60,
            "neutral",
            _params(),
            diagnostics=diagnostics,
        )

        assert result is None
        assert diagnostics["reason"] == "insufficient_candle_volume_for_full_fill"
        assert diagnostics["event_ts"] == base + 60
        assert diagnostics["candle_volume"] == pytest.approx(0.5)
    finally:
        conn.close()


def test_terminal_residual_close_must_fit_boundary_candle_volume(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "terminal-close-volume.db"))
    try:
        db.init_db(conn)
        base = 1_753_100_000
        _seed(
            conn,
            [
                # LONG initial inventory of one slot fits this entry minute.
                (base, 100.0, 100.0, 100.0, 100.0, 10.0),
                # At the horizon the model tries to close one slot, but the
                # entire next minute contains only 0.5 units of traded volume.
                (base + 60, 100.0, 100.0, 100.0, 100.0, 0.5),
            ],
        )
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
            _params(direction="long"),
            diagnostics=diagnostics,
        )

        assert result is None
        assert diagnostics["reason"] == "insufficient_candle_volume_for_terminal_liquidation"
        assert diagnostics["event_ts"] == base + 60
        assert diagnostics["required_fill_qty"] == pytest.approx(1.0)
        assert diagnostics["candle_volume"] == pytest.approx(0.5)
    finally:
        conn.close()



def test_kill_switch_residual_close_shares_breach_candle_volume(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "kill-switch-close-volume.db"))
    try:
        db.init_db(conn)
        base = 1_753_150_000
        _seed(
            conn,
            [
                # Sell 101 consumes one unit; closing the resulting short at the
                # upper stop requires another unit, but total candle volume is 1.5.
                (base, 100.0, 102.5, 100.0, 102.5, 1.5),
            ],
        )
        diagnostics: dict[str, object] = {}
        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            102.5,
            base,
            base + 60,
            "neutral",
            _params(),
            diagnostics=diagnostics,
        )

        assert result is None
        assert diagnostics["reason"] == "insufficient_candle_volume_for_kill_switch_liquidation"
        assert diagnostics["volume_used_before_fill"] == pytest.approx(1.0)
        assert diagnostics["required_fill_qty"] == pytest.approx(1.0)
        assert diagnostics["candle_volume"] == pytest.approx(1.5)
    finally:
        conn.close()

def test_outcome_waits_for_complete_boundary_candle_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "boundary-availability.db"))
    try:
        db.init_db(conn)
        base = 1_753_200_000
        published = base + 90
        monkeypatch.setitem(outcomes_module.BOT_HORIZONS, "futures_grid", 120)
        params = _params(direction="neutral")
        db.insert_recommendations(
            conn,
            [
                {
                    "rec_id": "R-boundary-availability",
                    "ts": published,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "futures_grid",
                    "direction": "neutral",
                    "account_mode": "unified",
                    "margin_mode": "cross",
                    "score": 0.7,
                    "confidence": 0.6,
                    "expected_rr": 1.2,
                    "risk_score": 0.2,
                    "params": params,
                    "reasons": {"risk_checks": {"passed": True, "blocks": []}},
                    "blocks": [],
                    "status": "recommended",
                    "ttl_sec": 900,
                    "model_version": "test-historical-proxy",
                    "features_ref_ts": base,
                }
            ],
        )
        _seed(
            conn,
            [
                (base + 60, 100.0, 100.0, 100.0, 100.0, 100.0),
                (base + 120, 100.0, 100.0, 100.0, 100.0, 100.0),
                (base + 180, 100.0, 100.0, 100.0, 100.0, 100.0),
                # Boundary candle starts at +240 and becomes complete at +300.
                (base + 240, 100.0, 100.0, 100.0, 100.0, 100.0),
            ],
        )

        monkeypatch.setattr(db, "now_ts", lambda: base + 299)
        assert compute_outcomes_once(conn, max_to_process=10) == 0

        # The boundary candle is complete at +300, but the exact calibration
        # contract matures at recommendation_ts + horizon + 120 = base + 330.
        monkeypatch.setattr(db, "now_ts", lambda: base + 300)
        assert compute_outcomes_once(conn, max_to_process=10) == 0

        monkeypatch.setattr(db, "now_ts", lambda: base + 330)
        assert compute_outcomes_once(conn, max_to_process=10) == 1
        row = conn.execute(
            "SELECT label_available_ts FROM reco_outcomes WHERE rec_id=?",
            ("R-boundary-availability",),
        ).fetchone()
        assert row["label_available_ts"] == base + 330
    finally:
        conn.close()
