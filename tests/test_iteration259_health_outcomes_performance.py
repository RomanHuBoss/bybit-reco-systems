from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.policy import canonical_policy_fingerprint


def _recommendation(
    *,
    rec_id: str,
    ts: int,
    model_version: str,
    policy_contract: dict,
    policy_fingerprint: str,
    policy_eligible: bool,
    status: str = "no_trade",
) -> dict:
    sample_role = "exact_policy" if policy_eligible else "shadow_no_trade"
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.30 if policy_eligible else 0.10,
        "confidence": 0.55,
        "expected_rr": 0.2,
        "risk_score": 0.1,
        "params": {},
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": 0.30 if policy_eligible else 0.10,
                "padding": "x" * 2048,
            },
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": policy_eligible,
                "policy_contract": policy_contract,
                "policy_fingerprint": policy_fingerprint,
                "sample_role": sample_role,
                "label_due_ts": ts + 43_320,
            },
            "risk_checks": {"passed": True, "blocks": []},
        },
        "blocks": [],
        "status": status,
        "ttl_sec": 900,
        "model_version": model_version,
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _insert_outcome(conn, rec_id: str, ts: int, *, success: int) -> None:
    db.insert_outcome(
        conn,
        {
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "neutral",
            "horizon_sec": 43_200,
            "label_available_ts": ts + 43_320,
            "entry_close": 100.0,
            "exit_close": 101.0 if success else 99.0,
            "ret": 0.01 if success else -0.01,
            "success": success,
        },
    )


def test_status_lineage_decodes_only_current_model_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "bounded-status.db"))
    try:
        db.init_db(conn)
        contract = {"schema": "candidate-policy-v1", "threshold": 0.25}
        fingerprint = canonical_policy_fingerprint(contract)
        rows = []
        base_ts = 1_700_000_000
        for idx in range(400):
            rows.append(
                _recommendation(
                    rec_id=f"R-old-{idx}",
                    ts=base_ts + idx,
                    model_version="old-model",
                    policy_contract=contract,
                    policy_fingerprint=fingerprint,
                    policy_eligible=False,
                )
            )
        for idx in range(3):
            rows.append(
                _recommendation(
                    rec_id=f"R-current-{idx}",
                    ts=base_ts + 10_000 + idx,
                    model_version="current-model",
                    policy_contract=contract,
                    policy_fingerprint=fingerprint,
                    policy_eligible=False,
                )
            )
        db.insert_recommendations(conn, rows)
        for idx, row in enumerate(rows):
            _insert_outcome(conn, row["rec_id"], row["ts"], success=idx % 2)

        original = db._json_loads_mapping_or_default
        calls = 0

        def counted(value, default):
            nonlocal calls
            calls += 1
            return original(value, default)

        monkeypatch.setattr(db, "_json_loads_mapping_or_default", counted)
        history = db.get_outcome_history_summary(conn)
        current = list(
            db.iter_calibration_lineage_rows(
                conn,
                current_model_version="current-model",
            )
        )

        assert history["historical_total"] == 403
        assert len(current) == 3
        assert calls == 3
    finally:
        conn.close()


def test_archive_summary_is_bounded_and_keeps_headline_totals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "archive-summary.db"))
    try:
        db.init_db(conn)
        contract = {"schema": "candidate-policy-v1", "threshold": 0.25}
        fingerprint = canonical_policy_fingerprint(contract)
        rows = [
            _recommendation(
                rec_id=f"R-{idx}",
                ts=1_700_000_000 + idx,
                model_version="old-model",
                policy_contract=contract,
                policy_fingerprint=fingerprint,
                policy_eligible=False,
            )
            for idx in range(300)
        ]
        db.insert_recommendations(conn, rows)
        for idx, row in enumerate(rows):
            _insert_outcome(conn, row["rec_id"], row["ts"], success=idx % 2)

        original = db._json_loads_mapping_or_default
        calls = 0

        def counted(value, default):
            nonlocal calls
            calls += 1
            return original(value, default)

        monkeypatch.setattr(db, "_json_loads_mapping_or_default", counted)
        result = db.get_outcomes_stats(
            conn,
            scope="archive",
            include_breakdowns=False,
            recent_limit=2,
        )

        assert result["summary"]["total"] == 300
        assert result["summary"]["wins"] == 150
        assert result["cohorts"]["shadow_no_trade"]["total"] == 300
        assert len(result["recent"]) == 2
        # Only the bounded recent window is decoded; the 300-row archive is SQL-only.
        assert calls < 40
    finally:
        conn.close()


def test_policy_observability_skips_non_policy_rows_before_json_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "observability-filter.db"))
    try:
        db.init_db(conn)
        contract = {"schema": "candidate-policy-v1", "threshold": 0.25}
        fingerprint = canonical_policy_fingerprint(contract)
        base_ts = 1_700_000_000
        rows = [
            _recommendation(
                rec_id=f"R-shadow-{idx}",
                ts=base_ts + idx,
                model_version="current-model",
                policy_contract=contract,
                policy_fingerprint=fingerprint,
                policy_eligible=False,
            )
            for idx in range(250)
        ]
        exact = _recommendation(
            rec_id="R-exact",
            ts=base_ts,
            model_version="current-model",
            policy_contract=contract,
            policy_fingerprint=fingerprint,
            policy_eligible=True,
            status="recommended",
        )
        rows.append(exact)
        db.insert_recommendations(conn, rows)
        _insert_outcome(conn, exact["rec_id"], exact["ts"], success=1)

        original = db._json_loads_mapping_or_default
        calls = 0

        def counted(value, default):
            nonlocal calls
            calls += 1
            return original(value, default)

        monkeypatch.setattr(db, "_json_loads_mapping_or_default", counted)
        result = db.get_policy_outcome_observability(
            conn,
            model_version="current-model",
            policy_fingerprint=fingerprint,
            bot_type="futures_grid",
            now_ts_value=base_ts + 100_000,
        )

        assert result["matured_total"] == 1
        assert result["labeled_total"] == 1
        assert calls == 1
    finally:
        conn.close()


def test_operator_windows_open_immediately_and_archive_uses_summary_endpoint() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    health_start = source.index("async function loadHealth()")
    health_fetch = source.index('fetch("/api/v1/health/symbols")', health_start)
    health_modal = source.index('showModalHtml("Здоровье системы"', health_start)
    outcomes_start = source.index("async function loadOutcomes()")
    outcomes_fetch = source.index('fetch("/api/v1/outcomes/stats?scope=current_policy")', outcomes_start)
    outcomes_modal = source.index('showModalHtml("Результаты наблюдений"', outcomes_start)

    assert health_modal < health_fetch
    assert outcomes_modal < outcomes_fetch
    assert "/api/v1/outcomes/stats?scope=archive&detail=summary" in source
