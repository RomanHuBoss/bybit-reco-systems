from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from app import db
from app.calibration import BOT_CALIB_KEYS, GLOBAL_LOGREG_KEY
from app.recommender import (
    DIRECTION_CALIBRATION_KEY,
    RECOMMENDER_MODEL_VERSION,
    _apply_recent_publication_dedupe,
    _current_range_edge_calibration_rows,
)


def _shadow_rec(rec_id: str, ts: int, *, direction: str = "neutral") -> dict:
    return {
        "rec_id": rec_id,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": direction,
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.05,
        "confidence": 0.50,
        "expected_rr": 0.10,
        "risk_score": 0.20,
        "params": {
            "grid_count": 8,
            "grid_levels": 8,
            "label_horizon_hours": 12,
            "trade_plan": {"reference_price": 100.0},
        },
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_score": 0.40,
                "mean_reversion_evidence_valid": 1,
            },
            "outcome_policy": {
                "eligible": True,
                "sample_role": "shadow_no_trade",
                "reason": "model_thesis_or_launch_gate",
            },
        },
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": RECOMMENDER_MODEL_VERSION,
        "features_ref_ts": ts,
    }


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        reco_republish_cooldown_sec=3600,
        outcome_horizon_fallback_sec=12 * 3600,
    )


def test_shadow_no_trade_reuses_open_outcome_root(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "shadow-root.db"))
    db.init_db(conn)
    try:
        now = int(time.time())
        previous = _shadow_rec("R-shadow-root", now - 600)
        db.insert_recommendations(conn, [previous])

        candidate = _shadow_rec("R-shadow-child", now)
        _apply_recent_publication_dedupe(conn, [candidate], _settings(), now)

        assert candidate["status"] == "no_trade"
        assert candidate["publication_root_rec_id"] == "R-shadow-root"
        assert candidate["is_outcome_label_root"] is False
        dedupe = candidate["reasons"]["publication_dedupe"]
        assert dedupe["decision"] == "reuse_shadow_root"
        assert dedupe["open_position_lock"] is True
    finally:
        conn.close()


def test_shadow_no_trade_opens_new_root_after_previous_outcome_is_settled(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "shadow-settled.db"))
    db.init_db(conn)
    try:
        now = int(time.time())
        previous = _shadow_rec("R-shadow-settled", now - 13 * 3600)
        db.insert_recommendations(conn, [previous])
        db.insert_outcome(conn, {
            "rec_id": previous["rec_id"],
            "ts": previous["ts"],
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "neutral",
            "horizon_sec": 12 * 3600,
            "label_available_ts": previous["ts"] + 12 * 3600,
            "entry_close": 100.0,
            "exit_close": 100.1,
            "ret": 0.001,
            "success": 1,
        })

        candidate = _shadow_rec("R-shadow-new", now)
        _apply_recent_publication_dedupe(conn, [candidate], _settings(), now)

        assert candidate["publication_root_rec_id"] == "R-shadow-new"
        assert candidate["is_outcome_label_root"] is True
        assert "publication_dedupe" not in candidate["reasons"]
    finally:
        conn.close()


def test_shadow_no_trade_does_not_reuse_opposite_direction(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "shadow-direction.db"))
    db.init_db(conn)
    try:
        now = int(time.time())
        previous = _shadow_rec("R-shadow-short", now - 600, direction="short")
        db.insert_recommendations(conn, [previous])

        candidate = _shadow_rec("R-shadow-long", now, direction="long")
        _apply_recent_publication_dedupe(conn, [candidate], _settings(), now)

        assert candidate["publication_root_rec_id"] == "R-shadow-long"
        assert candidate["is_outcome_label_root"] is True
        assert "publication_dedupe" not in candidate["reasons"]
    finally:
        conn.close()


def test_calibration_identity_changes_when_shadow_sampling_contract_changes() -> None:
    assert RECOMMENDER_MODEL_VERSION == "bybit-taxonomy-v4-independent-shadow-roots"
    assert BOT_CALIB_KEYS["futures_grid"] == "logreg_futures_grid_v7"
    assert GLOBAL_LOGREG_KEY == "logreg_global_v7"
    assert DIRECTION_CALIBRATION_KEY == "platt_direction_v6"


def test_old_overlapping_shadow_rows_are_excluded_from_new_calibration() -> None:
    common = {
        "bot_type": "futures_grid",
        "score": 0.2,
        "success": 1,
        "ret": 0.001,
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_score": 0.7,
                "mean_reversion_evidence_valid": 1,
            }
        },
    }
    rows = [
        {**common, "model_version": "bybit-taxonomy-v3-mean-reversion"},
        {**common, "model_version": RECOMMENDER_MODEL_VERSION},
    ]

    accepted = _current_range_edge_calibration_rows(rows)

    assert len(accepted) == 1
    assert accepted[0]["model_version"] == RECOMMENDER_MODEL_VERSION


def test_eighty_recommender_cycles_create_one_independent_shadow_root(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "shadow-eighty.db"))
    db.init_db(conn)
    try:
        base_ts = int(time.time()) - 80 * 60
        for index in range(80):
            ts = base_ts + index * 60
            candidate = _shadow_rec(f"R-shadow-{index:03d}", ts)
            _apply_recent_publication_dedupe(conn, [candidate], _settings(), ts)
            db.insert_recommendations(conn, [candidate])

        root_count = conn.execute(
            """SELECT COUNT(*) AS n FROM recommendations
               WHERE status='no_trade'
                 AND COALESCE(is_outcome_label_root, 1)=1
                 AND json_extract(reasons_json, '$.outcome_policy.sample_role')='shadow_no_trade'"""
        ).fetchone()["n"]
        child_count = conn.execute(
            """SELECT COUNT(*) AS n FROM recommendations
               WHERE status='no_trade'
                 AND COALESCE(is_outcome_label_root, 1)=0"""
        ).fetchone()["n"]

        assert root_count == 1
        assert child_count == 79
    finally:
        conn.close()
