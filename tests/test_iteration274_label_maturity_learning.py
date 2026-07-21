from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import db
from app import outcomes as outcomes_module
from app.outcomes import compute_outcomes_once
from app.policy import canonical_policy_fingerprint
from app.recommender import (
    RECOMMENDER_MODEL_VERSION,
    calibration_lineage_diagnostics,
    calibration_policy_label_due_ts,
)


def _grid_params() -> dict:
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 103.0,
        "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
        "trade_plan": {
            "grid_count": 2,
            "geometry_valid": True,
            "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
            "levels": {
                "range": {"lower": 99.0, "upper": 103.0},
                "kill_switch": {"lower": 94.0, "upper": 108.0},
                "tp_per_leg": {"abs": 2.0},
            },
        },
    }


def _recommendation(rec_id: str, ts: int, *, contract: dict | None = None) -> dict:
    reasons: dict = {"risk_checks": {"passed": True, "blocks": []}}
    if contract is not None:
        fingerprint = canonical_policy_fingerprint(contract)
        due = ts + 120 + 120
        reasons.update({
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": 0.7,
            },
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": True,
                "policy_contract": contract,
                "policy_fingerprint": fingerprint,
                "label_due_ts": due,
                "sample_role": "shadow_no_trade",
            },
        })
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.7,
        "confidence": 0.6,
        "expected_rr": 1.2,
        "risk_score": 0.2,
        "params": _grid_params(),
        "reasons": reasons,
        "blocks": [],
        "status": "no_trade" if contract is not None else "recommended",
        "ttl_sec": 900,
        "model_version": RECOMMENDER_MODEL_VERSION,
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "outcome_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _seed_flat_window(conn, timestamps: list[int]) -> None:
    db.upsert_ohlcv(conn, [
        {
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts,
            "open": 101.0,
            "high": 101.0,
            "low": 101.0,
            "close": 101.0,
            "volume": 1_000.0,
        }
        for ts in timestamps
    ])


def test_outcome_worker_never_persists_label_before_policy_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "label-due.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_000_000
        published_ts = base_ts + 90
        monkeypatch.setitem(outcomes_module.BOT_HORIZONS, "futures_grid", 120)
        monkeypatch.setattr(db, "now_ts", lambda: base_ts + 10_000)
        recommendation = _recommendation("R-label-due", published_ts)
        recommendation["features_ref_ts"] = base_ts
        db.insert_recommendations(conn, [recommendation])
        _seed_flat_window(conn, [base_ts + 120, base_ts + 180, base_ts + 240])

        assert compute_outcomes_once(conn, max_to_process=10) == 1
        row = conn.execute(
            "SELECT horizon_sec, label_available_ts FROM reco_outcomes WHERE rec_id=?",
            ("R-label-due",),
        ).fetchone()
        assert row["horizon_sec"] == 120
        assert row["label_available_ts"] == published_ts + 120 + 120
    finally:
        conn.close()


def _lineage_row(*, label_available_ts: int, due: int, contract: dict, fingerprint: str) -> dict:
    return {
        "rec_id": "R-lineage",
        "ts": due - 3_720,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "horizon_sec": 3_600,
        "label_available_ts": label_available_ts,
        "success": 1,
        "ret": 0.01,
        "score": 0.7,
        "model_version": RECOMMENDER_MODEL_VERSION,
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": 0.7,
            },
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": True,
                "policy_contract": contract,
                "policy_fingerprint": fingerprint,
                "label_due_ts": due,
                "sample_role": "shadow_no_trade",
            },
        },
    }


def test_calibration_lineage_rejects_premature_label_metadata() -> None:
    contract = {
        "selection": {"mean_reversion_min_score": 0.25},
        "calibration": {"label_due_grace_sec": 120},
    }
    fingerprint = canonical_policy_fingerprint(contract)
    due = int(time.time()) - 10

    premature = calibration_lineage_diagnostics(
        [_lineage_row(label_available_ts=due - 1, due=due, contract=contract, fingerprint=fingerprint)],
        policy_fingerprint=fingerprint,
    )
    assert premature["policy_eligible_total"] == 0
    assert premature["dropped_invalid_policy_maturity"] == 1

    valid = calibration_lineage_diagnostics(
        [_lineage_row(label_available_ts=due, due=due, contract=contract, fingerprint=fingerprint)],
        policy_fingerprint=fingerprint,
    )
    assert valid["policy_eligible_total"] == 1


def test_init_db_repairs_conservative_label_availability_for_legacy_rows(tmp_path: Path) -> None:
    path = tmp_path / "repair.db"
    conn = db.connect(str(path))
    try:
        db.init_db(conn)
        contract = {
            "selection": {"mean_reversion_min_score": 0.25},
            "calibration": {"label_due_grace_sec": 120},
        }
        now = int(time.time())
        recommendation_ts = now - 10_000
        recommendation = _recommendation("R-repair", recommendation_ts, contract=contract)
        due = recommendation_ts + 240
        db.insert_recommendations(conn, [recommendation])
        db.insert_outcome(conn, {
            "rec_id": "R-repair",
            "ts": recommendation_ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "horizon_sec": 120,
            "label_available_ts": due - 60,
            "entry_close": 101.0,
            "exit_close": 101.0,
            "ret": 0.0,
            "success": 1,
            "event_type": "GRID_OUTCOME",
            "diagnostics": {},
        })

        db.init_db(conn)
        row = conn.execute(
            "SELECT label_available_ts FROM reco_outcomes WHERE rec_id=?",
            ("R-repair",),
        ).fetchone()
        assert row["label_available_ts"] == due
    finally:
        conn.close()


def test_label_due_helper_accepts_explicit_effective_horizon() -> None:
    assert calibration_policy_label_due_ts(1_000, "futures_grid", horizon_sec=600) == 1_720
    assert calibration_policy_label_due_ts(True, "futures_grid", horizon_sec=600) is None
