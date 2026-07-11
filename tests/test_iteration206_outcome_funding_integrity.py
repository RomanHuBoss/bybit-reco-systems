from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome, compute_outcomes_once
from app.recommender import _estimate_cost_model


def _seed_1m_rows(conn, *, base_ts: int, candles: list[dict[str, float]]) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base_ts + idx * 60,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": 1_000.0,
            }
            for idx, candle in enumerate(candles)
        ],
    )


def test_fractional_funding_interval_is_not_promoted_to_confirmed_schedule() -> None:
    # A malformed 720.5-minute interval must not be rounded to a plausible 12h
    # schedule.  For the canonical 12h grid horizon, the conservative unknown
    # schedule uses the 8h fallback and therefore charges two possible events.
    cost = _estimate_cost_model(
        "futures_grid",
        "linear",
        {"spread_bps": 1.0},
        taker_fee_bps=6.0,
        direction="long",
        funding_rate=0.001,
        next_funding_ts=None,
        ts_now=1_700_000_000,
        funding_interval_min=720.5,
    )

    assert cost["funding_interval_source"] == "fallback_8h_invalid_interval"
    assert cost["funding_interval_uncertain"] is True
    assert cost["expected_funding_events"] == 2
    assert cost["expected_funding_bps"] == pytest.approx(20.0)
    assert cost["funding_event_schedule_assumption"] == "conservative_unknown_next_funding_ts"


def test_directional_tp_touch_cannot_override_kill_switch_breach(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "tp-after-kill-switch.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_100_000
        candles = [
            # One profitable TP touch occurs early...
            {"open": 100.0, "high": 100.35, "low": 99.95, "close": 100.10},
            # ...but the same evaluation horizon later breaches the lower kill-switch.
            {"open": 100.10, "high": 100.12, "low": 94.00, "close": 94.20},
            {"open": 94.20, "high": 94.40, "low": 93.90, "close": 94.10},
        ]
        _seed_1m_rows(conn, base_ts=base_ts, candles=candles)

        params = {
            "grid_count": 20,
            "grid_levels": 20,
            "grid_spacing_pct": 0.4,
            "cost_model": {"execution_cost_bps": 15.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "grid_count": 20,
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                    "tp_per_leg": {"abs": 0.25},
                },
            },
        }

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            94.10,
            base_ts,
            base_ts + len(candles) * 60,
            "long",
            params,
        )

        assert success == 0
        assert ret_proxy < 0.0
    finally:
        conn.close()


def test_outcome_worker_rejects_fractional_recommendation_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "fractional-outcome-time.db"))
    try:
        db.init_db(conn)
        entry_ts = 1_700_200_000
        horizon_sec = 6 * 3600
        db.upsert_ohlcv(
            conn,
            [
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "tf_sec": 60,
                    "ts": entry_ts,
                    "open": 100.0,
                    "high": 100.2,
                    "low": 99.8,
                    "close": 100.0,
                    "volume": 1_000.0,
                },
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "tf_sec": 60,
                    "ts": entry_ts + horizon_sec,
                    "open": 100.1,
                    "high": 100.2,
                    "low": 100.0,
                    "close": 100.1,
                    "volume": 1_000.0,
                },
            ],
        )

        params = {
            "label_horizon_hours": 6,
            "grid_count": 8,
            "grid_levels": 8,
            "grid_spacing_pct": 0.4,
            "price_range_lower": 95.0,
            "price_range_upper": 105.0,
            "trade_plan": {
                "grid_count": 8,
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                },
            },
        }
        conn.execute(
            """INSERT INTO recommendations(
                   rec_id, ts, venue, symbol, bot_type, direction,
                   account_mode, margin_mode, score, confidence, expected_rr,
                   risk_score, params_json, reasons_json, blocks_json, status,
                   ttl_sec, model_version, features_ref_ts,
                   publication_root_rec_id, is_outcome_label_root
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "R-fractional-time",
                entry_ts - 120.5,
                "linear",
                "BTCUSDT",
                "futures_grid",
                "neutral",
                "unified",
                "isolated",
                0.5,
                0.6,
                1.0,
                0.2,
                json.dumps(params),
                "{}",
                "[]",
                "recommended",
                900,
                "test-v1",
                entry_ts - 60.5,
                "R-fractional-time",
                1,
            ),
        )
        conn.commit()
        monkeypatch.setattr(db, "now_ts", lambda: entry_ts + 24 * 3600)

        processed = compute_outcomes_once(conn, horizon_sec=30 * 60, max_to_process=10)

        assert processed == 0
        assert conn.execute(
            "SELECT 1 FROM reco_outcomes WHERE rec_id=?",
            ("R-fractional-time",),
        ).fetchone() is None
        decision = conn.execute(
            "SELECT action FROM decision_log WHERE rec_id=? ORDER BY id DESC LIMIT 1",
            ("R-fractional-time",),
        ).fetchone()
        assert decision is not None
        assert decision["action"] == "OUTCOME_SKIP_INVALID_TEMPORAL_FIELDS"
    finally:
        conn.close()
