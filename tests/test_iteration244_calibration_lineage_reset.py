from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from app import calibration, db, recommender

CURRENT_MODEL = "bybit-taxonomy-v8-policy-conditioned-censor-aware"


def _recommendation(
    rec_id: str,
    ts: int,
    model_version: str,
    *,
    evidence_valid: bool = True,
    policy_fingerprint: str | None = None,
    policy_contract: dict | None = None,
) -> dict:
    reasons = {
        "feature_snapshot": {
            "mean_reversion_evidence_valid": 1 if evidence_valid else 0,
            "mean_reversion_score": 0.31,
        }
    }
    if policy_fingerprint is not None:
        reasons["outcome_policy"] = {
            "eligible": True,
            "sample_role": "shadow_no_trade",
            "policy_evaluation_eligible": True,
            "policy_fingerprint": policy_fingerprint,
            "policy_contract": policy_contract,
            "label_due_ts": ts + 12 * 3600 + 120,
        }
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.31,
        "confidence": 0.31,
        "expected_rr": 1.1,
        "risk_score": 0.2,
        "params": {},
        "reasons": reasons,
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 1800,
        "model_version": model_version,
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _outcome(rec_id: str, ts: int, success: int) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "horizon_sec": 12 * 3600,
        "label_available_ts": ts + 12 * 3600,
        "entry_close": 100.0,
        "exit_close": 100.4 if success else 99.6,
        "ret": 0.004 if success else -0.004,
        "success": success,
    }


def test_model_and_calibrator_lineage_are_advanced() -> None:
    assert recommender.RECOMMENDER_MODEL_VERSION == CURRENT_MODEL
    assert calibration.BOT_CALIB_KEYS["futures_grid"] == "logreg_futures_grid_v19"
    assert calibration.GLOBAL_LOGREG_KEY == "logreg_global_v19"
    assert recommender.DIRECTION_CALIBRATION_KEY == "platt_direction_v14"


def test_lineage_diagnostics_preserve_archive_but_reject_old_model_rows() -> None:
    diagnostics = getattr(recommender, "calibration_lineage_diagnostics")
    rows = [
        {"model_version": "bybit-taxonomy-v6-historical-proxy-shadow-roots", "reasons": {"feature_snapshot": {"mean_reversion_evidence_valid": 1, "mean_reversion_score": 0.31}}},
        {"model_version": CURRENT_MODEL, "reasons": {"feature_snapshot": {"mean_reversion_evidence_valid": 1, "mean_reversion_score": 0.31}}},
        {"model_version": CURRENT_MODEL, "reasons": {"feature_snapshot": {"mean_reversion_evidence_valid": 0, "mean_reversion_score": 0.31}}},
    ]
    result = diagnostics(rows)
    assert result["historical_total"] == 3
    assert result["current_model_total"] == 2
    assert result["feature_eligible_total"] == 1
    assert result["dropped_old_model"] == 1
    assert result["dropped_invalid_feature_evidence"] == 1


def test_status_separates_historical_current_and_feature_eligible_outcomes(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "lineage_status.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        now = db.now_ts() - 13 * 3600
        with closing(db.connect(str(db_path))) as conn:
            db.init_db(conn)
            active_limits = recommender.normalize_risk_limits(
                db.get_active_risk_limits(conn),
                app_main.settings.risk_limits,
            )
            policy_fingerprint = recommender.calibration_policy_fingerprint(
                app_main.settings,
                active_limits,
            )
            policy_contract = recommender.calibration_policy_contract(
                app_main.settings,
                active_limits,
            )
            db.insert_recommendations(
                conn,
                [
                    _recommendation("R-old", now, "bybit-taxonomy-v6-historical-proxy-shadow-roots"),
                    _recommendation(
                        "R-new",
                        now + 1,
                        CURRENT_MODEL,
                        policy_fingerprint=policy_fingerprint,
                        policy_contract=policy_contract,
                    ),
                ],
            )
            db.insert_outcome(conn, _outcome("R-old", now, 1))
            db.insert_outcome(conn, _outcome("R-new", now + 1, 0))
            conn.commit()

        status = app_main.api_status()
        bot = status["bot_calibrators"]["futures_grid"]
        assert status["outcome_count"] == 2  # backward-compatible archive count
        assert status["historical_outcome_count"] == 2
        assert status["current_model_outcome_count"] == 1
        assert status["feature_eligible_outcome_count"] == 1
        assert status["calibration_eligible_outcome_count"] == 1
        assert status["calibration_model_version"] == CURRENT_MODEL
        assert status["global_calibrator_base_key"] == "logreg_global_v19"
        assert status["global_calibrator_key"].startswith("logreg_global_v19:")
        assert bot["historical_outcomes_total"] == 2
        assert bot["current_model_outcomes_total"] == 1
        assert bot["feature_eligible_outcomes_total"] == 1
        assert bot["policy_eligible_outcomes_total"] == 1
        assert bot["outcomes_total"] == 1
        assert bot["calibrator_base_key"] == "logreg_futures_grid_v19"
        assert bot["calibrator_key"].startswith("logreg_futures_grid_v19:")
        assert bot["calibration_model_version"] == CURRENT_MODEL
    finally:
        sys.modules.pop("app.main", None)


def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"function {name} not found"
    start = match.start()
    pos = match.end()
    depth = 1
    while pos < len(source) and depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


def test_frontend_explains_archive_vs_current_lineage_counts() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    harness = "\n".join([
        _extract_js_function(source, "fmt"),
        _extract_js_function(source, "botTypeLabel"),
        _extract_js_function(source, "buildBotCalibText"),
    ])
    code = 'const SUPPORTED_GRID_BOT_TYPE = "futures_grid";\n' + harness + """
const text = buildBotCalibText('futures_grid', {
  fitted: false,
  historical_outcomes_total: 120,
  current_model_outcomes_total: 0,
  feature_eligible_outcomes_total: 0,
  outcomes_total: 0,
  wins: 0,
  losses: 0,
  effective_samples: 0,
  min_samples: 80,
  calibration_model_version: 'bybit-taxonomy-v7-mr-floor-temporal-cohorts',
  calibrator_key: 'logreg_futures_grid_v18',
  expectancy_status: 'insufficient',
  temporal_cluster_count: 0,
  minimum_temporal_clusters: 20,
}, 120);
console.log(JSON.stringify({text}));
"""
    result = subprocess.run(["node", "-e", code], check=True, capture_output=True, text=True)
    text = json.loads(result.stdout)["text"]
    assert "Архив: 120" in text
    assert "текущая версия модели: 0" in text
    assert "Для денежной оценки: 0/80" in text
    assert "калибратор ещё не обучен" in text
