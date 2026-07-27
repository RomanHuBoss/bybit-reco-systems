from __future__ import annotations

import time
from pathlib import Path

from app import db
from app.policy import canonical_policy_fingerprint
from scripts.offline_walk_forward import build_walk_forward_report


def _contract() -> dict:
    return {
        "schema_version": "candidate-policy-v3",
        "selection": {
            "min_score_to_recommend": 0.14,
            "mean_reversion_min_score": 0.25,
        },
        "calibration": {"label_due_grace_sec": 120},
    }


def _recommendation(
    rec_id: str,
    ts: int,
    *,
    contract: dict,
    fingerprint: str,
    score: float,
    mean_reversion_score: float,
    policy_eligible: bool,
    decision_code: str | None = None,
) -> dict:
    no_trade_reasons = []
    if decision_code:
        no_trade_reasons.append({"code": decision_code})
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "short",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": score,
        "confidence": 0.55,
        "expected_rr": 0.2,
        "risk_score": 0.1,
        "params": {},
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": mean_reversion_score,
            },
            "decision_layers": {"no_trade_reasons": no_trade_reasons},
            "risk_checks": {"passed": True, "blocks": []},
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": policy_eligible,
                "policy_contract": contract,
                "policy_fingerprint": fingerprint,
                "label_due_ts": ts + 3_720,
                "calibration_role": (
                    "current_policy_evaluation"
                    if policy_eligible
                    else "shadow_exploration"
                ),
                "sample_role": "shadow_no_trade",
                "reason": "model_thesis_or_launch_gate",
            },
        },
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": "model-264",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _insert_outcome(conn, rec_id: str, ts: int, ret: float) -> None:
    db.insert_outcome(
        conn,
        {
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "short",
            "horizon_sec": 3_600,
            "label_available_ts": ts + 3_720,
            "entry_close": 100.0,
            "exit_close": 100.0 * (1.0 + ret),
            "ret": ret,
            "success": int(ret > 0.0),
        },
    )


def test_outcome_api_separates_fingerprint_scope_from_eligibility_cohorts(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "eligibility.db"))
    try:
        db.init_db(conn)
        contract = _contract()
        fingerprint = canonical_policy_fingerprint(contract)
        base_ts = int(time.time()) - 10_000
        recommendations = [
            _recommendation(
                "R-calibration",
                base_ts,
                contract=contract,
                fingerprint=fingerprint,
                score=0.20,
                mean_reversion_score=0.30,
                policy_eligible=True,
            ),
            _recommendation(
                "R-policy-candidate",
                base_ts + 1,
                contract=contract,
                fingerprint=fingerprint,
                score=0.20,
                mean_reversion_score=0.20,
                policy_eligible=True,
            ),
            _recommendation(
                "R-shadow",
                base_ts + 2,
                contract=contract,
                fingerprint=fingerprint,
                score=0.10,
                mean_reversion_score=0.20,
                policy_eligible=False,
                decision_code="SCORE_BELOW_THRESHOLD",
            ),
        ]
        db.insert_recommendations(conn, recommendations)
        for index, recommendation in enumerate(recommendations):
            _insert_outcome(
                conn,
                recommendation["rec_id"],
                recommendation["ts"],
                0.01 if index == 0 else -0.01,
            )

        stats = db.get_outcomes_stats(
            conn,
            scope="current_policy",
            current_model_version="model-264",
            policy_fingerprint=fingerprint,
        )

        assert stats["summary"]["total"] == 3
        assert stats["eligibility_summary"]["fingerprint_scope_total"] == 3
        assert stats["eligibility_summary"]["policy_evaluation_eligible_total"] == 2
        assert stats["eligibility_summary"]["calibration_eligible_total"] == 1
        assert stats["eligibility_cohorts"]["calibration_eligible"]["total"] == 1
        assert stats["eligibility_cohorts"]["policy_evaluation_candidate"]["total"] == 1
        assert stats["eligibility_cohorts"]["shadow_exploration"]["total"] == 1
        assert sum(row["total"] for row in stats["eligibility_cohorts"].values()) == 3

        recent = {row["rec_id"]: row for row in stats["recent"]}
        assert recent["R-calibration"]["mean_reversion_score"] == 0.30
        assert recent["R-calibration"]["calibration_eligible"] is True
        assert recent["R-shadow"]["eligibility"]["cohort"] == "shadow_exploration"
        assert "SCORE_BELOW_POLICY_FLOOR" in recent["R-shadow"]["eligibility"]["reason_codes"]
        assert "SCORE_BELOW_THRESHOLD" in recent["R-shadow"]["eligibility"]["decision_reason_codes"]
    finally:
        conn.close()


def test_prune_preserves_compact_outcome_evidence_for_extended_windows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 1_800_000_000
    monkeypatch.setattr(db, "now_ts", lambda: now)
    conn = db.connect(str(tmp_path / "retention.db"))
    try:
        db.init_db(conn)
        contract = _contract()
        fingerprint = canonical_policy_fingerprint(contract)
        exact_20d = _recommendation(
            "R-exact-20d",
            now - 20 * 86_400,
            contract=contract,
            fingerprint=fingerprint,
            score=0.20,
            mean_reversion_score=0.30,
            policy_eligible=True,
        )
        shadow_20d = _recommendation(
            "R-shadow-20d",
            now - 20 * 86_400,
            contract=contract,
            fingerprint=fingerprint,
            score=0.10,
            mean_reversion_score=0.20,
            policy_eligible=False,
        )
        exact_100d = _recommendation(
            "R-exact-100d",
            now - 100 * 86_400,
            contract=contract,
            fingerprint=fingerprint,
            score=0.20,
            mean_reversion_score=0.30,
            policy_eligible=True,
        )
        rows = [exact_20d, shadow_20d, exact_100d]
        db.insert_recommendations(conn, rows)
        for row in rows:
            _insert_outcome(conn, row["rec_id"], row["ts"], 0.01)

        db.prune_old_data(conn, retain_days=7)

        recommendation_ids = {
            row["rec_id"] for row in conn.execute("SELECT rec_id FROM recommendations")
        }
        outcome_ids = {
            row["rec_id"] for row in conn.execute("SELECT rec_id FROM reco_outcomes")
        }
        observability_ids = {
            row["rec_id"]
            for row in conn.execute("SELECT rec_id FROM reco_outcome_observability")
        }
        assert recommendation_ids == {"R-exact-20d", "R-shadow-20d", "R-exact-100d"}
        assert outcome_ids == {"R-exact-20d", "R-shadow-20d", "R-exact-100d"}
        assert observability_ids == {"R-exact-20d", "R-shadow-20d", "R-exact-100d"}
    finally:
        conn.close()


def test_walk_forward_uses_only_labels_available_before_validation() -> None:
    rows = []
    for index, ts in enumerate((1_000, 1_100, 1_200, 1_300, 1_400)):
        rows.append({
            "rec_id": f"R-{index}",
            "ts": ts,
            "horizon_sec": 100,
            "label_available_ts": ts + 220,
            "ret": 0.01 if index % 2 == 0 else -0.01,
            "success": int(index % 2 == 0),
            "score": 0.10 + index * 0.02,
            "mean_reversion_score": 0.20 + index * 0.03,
            "execution_direction": ("long", "short", "neutral")[index % 3],
        })

    report = build_walk_forward_report(
        {"recent": rows},
        score_floor_override=0.14,
        mean_reversion_floor_override=0.25,
        min_training_cohorts=2,
    )

    assert report["coverage"]["mean_reversion_score"]["share"] == 1.0
    assert report["existing_policy_floors"]["changed_by_analysis"] is False
    assert report["walk_forward"]["fold_count"] == 1
    fold = report["walk_forward"]["folds"][0]
    assert fold["validation_ts"] == 1_400
    assert fold["training_rows"] == 2
    assert fold["latest_training_label_available_ts"] == 1_320
    assert report["walk_forward"]["aggregate_validation"]["direction_short"]["total"] == 1


def test_operator_ui_names_shadow_and_calibration_cohorts_separately() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    backend = Path("app/main.py").read_text(encoding="utf-8")

    assert "Когорты допуска (не пересекаются)" in source
    assert "учебные наблюдения, не калибровка" in source
    assert "mean_reversion_score" in source
    assert "outcomeEligibilityReasonsText" in source
    assert "recent_limit: int = 120" in backend
    assert "max_value=6000" in backend
