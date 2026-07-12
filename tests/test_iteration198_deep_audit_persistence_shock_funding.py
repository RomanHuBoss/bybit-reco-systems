from __future__ import annotations

from pathlib import Path

import pytest

from app import db, recommender, shock_guard
from app.outcomes import _extract_cost_components, _grid_outcome


def _recommendation(*, rec_id: str = "R-audit", ts: int = 1_700_800_000) -> dict:
    return {
        "rec_id": rec_id,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.25,
        "confidence": 0.70,
        "expected_rr": 1.10,
        "risk_score": 0.20,
        "params": {"grid_count": 8},
        "reasons": {},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "audit-v1",
        "features_ref_ts": ts - 60,
    }


def test_market_shock_filters_every_open_future_and_malformed_candle() -> None:
    now = 1_700_800_000
    rows = [
        {"ts": now + 120, "close": 150.0},
        {"ts": now + 60, "close": 140.0},
        {"ts": True, "close": 130.0},
        {"ts": now - 30.5, "close": 120.0},
        {"ts": now - 60, "close": 100.0},
        {"ts": now - 120, "close": 99.0},
    ]

    closed = shock_guard._drop_open_candle(rows, tf_sec=60, ts_now=now)

    assert [row["ts"] for row in closed] == [now - 60, now - 120]


def test_recommendation_insert_is_idempotent_but_cannot_overwrite_audit_row(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "immutable-recommendation.db"))
    try:
        db.init_db(conn)
        original = _recommendation()
        db.insert_recommendations(conn, [original])
        db.insert_recommendations(conn, [dict(original)])

        conflicting = dict(original)
        conflicting.update({
            "direction": "short",
            "score": 0.99,
            "confidence": 0.99,
            "status": "executed",
            "params": {"grid_count": 20},
        })
        with pytest.raises(ValueError, match="already exists with different payload"):
            db.insert_recommendations(conn, [conflicting])

        row = conn.execute(
            "SELECT direction, score, confidence, status, params_json FROM recommendations WHERE rec_id=?",
            (original["rec_id"],),
        ).fetchone()
        assert row is not None
        assert row["direction"] == "long"
        assert float(row["score"]) == pytest.approx(0.25)
        assert float(row["confidence"]) == pytest.approx(0.70)
        assert row["status"] == "recommended"
        assert '"grid_count":8' in str(row["params_json"]).replace(" ", "")
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("ts", True),
        ("confidence", True),
        ("score", False),
        ("expected_rr", True),
        ("risk_score", False),
        ("ttl_sec", True),
        ("features_ref_ts", True),
        ("is_outcome_label_root", "false"),
    ],
)
def test_recommendation_persistence_rejects_boolean_or_ambiguous_numeric_fields(
    tmp_path: Path,
    field: str,
    bad_value,
) -> None:
    conn = db.connect(str(tmp_path / f"bad-{field}.db"))
    try:
        db.init_db(conn)
        row = _recommendation(rec_id=f"R-{field}")
        row[field] = bad_value
        with pytest.raises(ValueError):
            db.insert_recommendations(conn, [row])
        assert conn.execute("SELECT COUNT(1) AS c FROM recommendations").fetchone()["c"] == 0
    finally:
        conn.close()


def _seed_1m_rows(conn, *, base_ts: int) -> None:
    candles = [
        {"open": 100.0, "high": 100.015, "low": 99.995, "close": 100.005},
        {"open": 100.005, "high": 100.030, "low": 99.995, "close": 100.010},
        {"open": 100.010, "high": 100.025, "low": 99.995, "close": 100.005},
    ]
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base_ts + idx * 60,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": 1_000.0,
            }
            for idx, candle in enumerate(candles)
        ],
    )


def test_negative_execution_cost_cannot_create_optimistic_outcome_label(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "negative-cost-label.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_900_000
        _seed_1m_rows(conn, base_ts=base_ts)
        params = {
            "grid_count": 20,
            "grid_levels": 20,
            "grid_spacing_pct": 0.4,
            "cost_model": {"execution_cost_bps": -12.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                    "tp_per_leg": {"abs": 0.02},
                }
            },
        }

        execution_bps, funding_bps = _extract_cost_components(params, fallback_execution_bps=15.0)
        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.005,
            base_ts,
            base_ts + 180,
            "long",
            params,
        )

        assert execution_bps == pytest.approx(15.0)
        assert funding_bps == pytest.approx(0.0)
        assert success == 0
        assert ret_proxy < 0.0
    finally:
        conn.close()


def test_boolean_funding_schedule_is_treated_as_unknown_and_charged_conservatively() -> None:
    cost = recommender._estimate_cost_model(
        bot_type="futures_grid",
        venue="linear",
        f={"spread_bps": 1.0},
        taker_fee_bps=6.0,
        direction="long",
        funding_rate=0.0001,
        next_funding_ts=True,
        ts_now=1_700_006_402,
        funding_interval_min=480,
    )

    assert cost["next_funding_ts"] is None
    assert cost["expected_funding_events"] == 2
    assert cost["expected_funding_bps"] == pytest.approx(2.0)
    assert cost["funding_event_schedule_assumption"] == "conservative_unknown_next_funding_ts"
