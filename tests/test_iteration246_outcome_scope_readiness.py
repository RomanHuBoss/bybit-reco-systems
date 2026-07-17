from __future__ import annotations

from pathlib import Path

from app import db
from app.policy import canonical_policy_fingerprint


def _recommendation(
    *,
    rec_id: str,
    ts: int,
    model_version: str,
    policy_contract: dict,
    policy_fingerprint: str,
    status: str,
    sample_role: str,
) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.25,
        "confidence": 0.55,
        "expected_rr": 0.2,
        "risk_score": 0.2,
        "params": {},
        "reasons": {
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": True,
                "policy_contract": policy_contract,
                "policy_fingerprint": policy_fingerprint,
                "sample_role": sample_role,
            }
        },
        "blocks": [],
        "status": status,
        "ttl_sec": 900,
        "model_version": model_version,
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _insert_outcome(conn, rec_id: str, ts: int, *, ret: float, success: int) -> None:
    db.insert_outcome(
        conn,
        {
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "neutral",
            "horizon_sec": 3600,
            "label_available_ts": ts + 3660,
            "entry_close": 100.0,
            "exit_close": 100.0 * (1.0 + ret),
            "ret": ret,
            "success": success,
        },
    )


def test_outcome_stats_scope_separates_current_policy_from_archive(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "outcome-scope.db"))
    try:
        db.init_db(conn)
        current_model = "model-current"
        current_contract = {"schema": "selection-v1", "threshold": 0.25}
        current_fingerprint = canonical_policy_fingerprint(current_contract)
        other_contract = {"schema": "selection-v1", "threshold": 0.30}
        other_fingerprint = canonical_policy_fingerprint(other_contract)
        rows = [
            _recommendation(
                rec_id="R-current-actionable",
                ts=1_710_000_000,
                model_version=current_model,
                policy_contract=current_contract,
                policy_fingerprint=current_fingerprint,
                status="recommended",
                sample_role="actionable_root",
            ),
            _recommendation(
                rec_id="R-current-shadow",
                ts=1_710_000_060,
                model_version=current_model,
                policy_contract=current_contract,
                policy_fingerprint=current_fingerprint,
                status="no_trade",
                sample_role="shadow_no_trade",
            ),
            _recommendation(
                rec_id="R-other-policy",
                ts=1_710_000_120,
                model_version=current_model,
                policy_contract=other_contract,
                policy_fingerprint=other_fingerprint,
                status="recommended",
                sample_role="actionable_root",
            ),
            _recommendation(
                rec_id="R-old-model",
                ts=1_710_000_180,
                model_version="model-old",
                policy_contract=current_contract,
                policy_fingerprint=current_fingerprint,
                status="recommended",
                sample_role="actionable_root",
            ),
        ]
        db.insert_recommendations(conn, rows)
        _insert_outcome(conn, "R-current-actionable", rows[0]["ts"], ret=0.01, success=1)
        _insert_outcome(conn, "R-current-shadow", rows[1]["ts"], ret=-0.01, success=0)
        _insert_outcome(conn, "R-other-policy", rows[2]["ts"], ret=0.02, success=1)
        _insert_outcome(conn, "R-old-model", rows[3]["ts"], ret=0.03, success=1)

        current = db.get_outcomes_stats(
            conn,
            scope="current_policy",
            current_model_version=current_model,
            policy_fingerprint=current_fingerprint,
        )
        assert current["scope"]["name"] == "current_policy"
        assert current["summary"]["total"] == 2
        assert current["cohorts"]["actionable"]["total"] == 1
        assert current["cohorts"]["shadow_no_trade"]["total"] == 1
        assert {row["rec_id"] for row in current["recent"]} == {
            "R-current-actionable",
            "R-current-shadow",
        }
        assert all(row["model_version"] == current_model for row in current["recent"])
        assert all(row["policy_fingerprint"] == current_fingerprint for row in current["recent"])

        model_scope = db.get_outcomes_stats(
            conn,
            scope="current_model",
            current_model_version=current_model,
            policy_fingerprint=current_fingerprint,
        )
        assert model_scope["summary"]["total"] == 3

        archive = db.get_outcomes_stats(
            conn,
            scope="archive",
            current_model_version=current_model,
            policy_fingerprint=current_fingerprint,
        )
        assert archive["summary"]["total"] == 4
        assert archive["scope"]["name"] == "archive"
    finally:
        conn.close()


def test_operator_ui_requests_current_policy_and_labels_archive_separately() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "/api/v1/outcomes/stats?scope=current_policy" in source
    assert "/api/v1/outcomes/stats?scope=archive" in source
    assert "Текущий набор правил" in source
    assert "Исторический архив" in source
    assert "Actionable roots" not in source


def test_calibration_ui_distinguishes_monetary_and_probability_sample_floors() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "logreg_min_samples" in source
    assert "Для денежной оценки" in source
    assert "вероятностной калибровки" in source
    assert "для вероятностной калибровки" in source


def test_current_policy_recent_is_not_hidden_by_newer_same_model_old_policies(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "outcome-recent-scope.db"))
    try:
        db.init_db(conn)
        current_model = "model-current"
        current_contract = {"schema": "selection-v1", "threshold": 0.25}
        current_fingerprint = canonical_policy_fingerprint(current_contract)
        current = _recommendation(
            rec_id="R-current-oldest",
            ts=1_710_000_000,
            model_version=current_model,
            policy_contract=current_contract,
            policy_fingerprint=current_fingerprint,
            status="recommended",
            sample_role="actionable_root",
        )
        rows = [current]
        for idx in range(9):
            contract = {"schema": "selection-v1", "threshold": 0.30 + idx / 100.0}
            rows.append(
                _recommendation(
                    rec_id=f"R-other-policy-{idx}",
                    ts=1_710_000_100 + idx,
                    model_version=current_model,
                    policy_contract=contract,
                    policy_fingerprint=canonical_policy_fingerprint(contract),
                    status="recommended",
                    sample_role="actionable_root",
                )
            )
        db.insert_recommendations(conn, rows)
        for row in rows:
            _insert_outcome(conn, row["rec_id"], row["ts"], ret=0.01, success=1)

        recent = db.get_outcomes_recent_enriched(
            conn,
            limit=1,
            scope="current_policy",
            current_model_version=current_model,
            policy_fingerprint=current_fingerprint,
        )
        assert [row["rec_id"] for row in recent] == ["R-current-oldest"]
    finally:
        conn.close()


def test_current_policy_scope_rejects_missing_or_malformed_lineage(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "outcome-invalid-scope.db"))
    try:
        db.init_db(conn)
        import pytest

        with pytest.raises(ValueError, match="current_model_version"):
            db.get_outcomes_stats(conn, scope="current_policy")
        with pytest.raises(ValueError, match="sha256 policy_fingerprint"):
            db.get_outcomes_stats(
                conn,
                scope="current_policy",
                current_model_version="model-current",
                policy_fingerprint="not-a-fingerprint",
            )
    finally:
        conn.close()


def test_outcomes_api_defaults_to_current_policy_scope() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert 'scope: str = "current_policy"' in source
    assert 'detail: str = "full"' in source
    assert 'scope=scope' in source
    assert 'status_code=400' in source


def test_status_and_ui_expose_theoretical_temporal_readiness_floor() -> None:
    backend = Path("app/main.py").read_text(encoding="utf-8")
    frontend = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert '"minimum_temporal_span_days"' in backend
    assert '"policy_fingerprint_change_starts_new_cohort": True' in backend
    assert "temporal floor" in frontend
    assert "Смена идентификатора набора правил начинает новую выборку наблюдений" in frontend
