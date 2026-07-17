from __future__ import annotations

import math
import statistics
from types import SimpleNamespace

import pytest

from app import calibration, db, recommender


CURRENT_MODEL = recommender.RECOMMENDER_MODEL_VERSION


def _feature_snapshot(*, mean_reversion_score: float, score: float = 0.70) -> dict:
    return {
        "mean_reversion_evidence_valid": 1.0,
        "mean_reversion_score": mean_reversion_score,
        "range_score": 0.35 + 0.65 * mean_reversion_score,
        "trend_strength": 0.10,
        "atr_pct_norm": 0.30,
        "effective_sentiment": 0.0,
        "dir_conf": 0.60,
        "coherence": 0.70,
        "spread_bps_norm": 0.50,
        "score": score,
        "oi_4h_norm": 0.0,
        "funding_norm": 0.0,
        "liq_tier_num": 1.0,
        "btc_corr": 0.0,
        "regime_conf": 0.80,
    }


def _row(*, cohort: int, slot: int, mean_reversion_score: float, ret: float) -> dict:
    horizon = 12 * 3600
    ts = 1_790_000_000 + cohort * horizon
    return {
        "model_version": CURRENT_MODEL,
        "symbol": f"S{slot:02d}USDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "ts": ts,
        "label_available_ts": ts + horizon,
        "horizon_sec": horizon,
        "score": 0.70,
        "success": int(ret > 0.0),
        "ret": ret,
        "reasons": {"feature_snapshot": _feature_snapshot(
            mean_reversion_score=mean_reversion_score
        )},
    }


def test_monetary_gate_uses_only_rows_admitted_by_candidate_floor(monkeypatch) -> None:
    monkeypatch.setattr(calibration.time, "time", lambda: 1_800_000_000)
    rows: list[dict] = []
    for cohort in range(20):
        for slot in range(4):
            rows.append(_row(
                cohort=cohort,
                slot=slot,
                mean_reversion_score=0.10,
                ret=0.02,
            ))
        rows.append(_row(
            cohort=cohort,
            slot=4,
            mean_reversion_score=0.35,
            ret=-0.01,
        ))

    fit_rows = recommender._current_range_edge_calibration_rows(rows)
    assert len(fit_rows) == 20
    assert all(
        row["reasons"]["feature_snapshot"]["mean_reversion_score"] >= 0.25
        for row in fit_rows
    )

    model = calibration.fit_logreg(
        fit_rows,
        min_samples=20,
        logreg_min_samples=300,
        half_life_days=100_000,
    )
    assert model.weighted_mean_return == pytest.approx(-0.01)
    assert model.expectancy_status == "negative"
    assert recommender._calibration_expectancy_no_trade_reason(model) is not None


def test_rejected_rows_cannot_veto_profitable_admitted_policy(monkeypatch) -> None:
    monkeypatch.setattr(calibration.time, "time", lambda: 1_800_000_000)
    rows: list[dict] = []
    for cohort in range(20):
        for slot in range(16):
            rows.append(_row(
                cohort=cohort,
                slot=slot,
                mean_reversion_score=0.10,
                ret=-0.01,
            ))
        for slot in range(16, 20):
            rows.append(_row(
                cohort=cohort,
                slot=slot,
                mean_reversion_score=0.35,
                ret=0.02,
            ))

    fit_rows = recommender._current_range_edge_calibration_rows(rows)
    assert len(fit_rows) == 80
    model = calibration.fit_logreg(
        fit_rows,
        min_samples=80,
        logreg_min_samples=1_000,
        half_life_days=100_000,
    )
    assert model.weighted_mean_return == pytest.approx(0.02)
    assert model.expectancy_status == "positive"
    assert recommender._calibration_expectancy_no_trade_reason(model) is None


def _no_skill_rows() -> list[dict]:
    rows: list[dict] = []
    horizon = 12 * 3600
    index = 0
    for cohort in range(40):
        ts = 1_770_000_000 + cohort * horizon
        for success, ret in ((1, 0.03),) * 4 + ((0, -0.005),) * 4:
            rows.append({
                "score": 0.50,
                "success": success,
                "ret": ret,
                "ts": ts,
                "label_available_ts": ts + horizon,
                "horizon_sec": horizon,
                "symbol": f"S{index:04d}USDT",
                "reasons": {"feature_snapshot": _feature_snapshot(
                    mean_reversion_score=0.30,
                    score=0.50,
                )},
            })
            index += 1
    return rows


def test_feature_model_requires_oof_skill_over_score_and_null_baselines(monkeypatch) -> None:
    monkeypatch.setattr(calibration.time, "time", lambda: 1_800_000_000)
    model = calibration.fit_logreg(
        _no_skill_rows(),
        min_samples=80,
        logreg_min_samples=300,
        half_life_days=100_000,
    )

    assert model.coef == []
    assert model.fitted is False
    assert model.oof_status == "no_skill"
    assert getattr(model, "oof_skill_status", None) == "rejected"


def test_accepted_model_keeps_terminal_holdout_out_of_final_fit(monkeypatch) -> None:
    monkeypatch.setattr(calibration.time, "time", lambda: 1_800_000_000)
    rows = _no_skill_rows()
    candidate_coef = [0.25] * len(calibration.FEATURE_NAMES)

    def fake_oof(X, ys, ws, **_kwargs):
        logits = [2.0 if label else -2.0 for label in ys]
        return logits, list(ys), list(ws)

    def fake_skill(*_args, **_kwargs):
        return {
            "status": "accepted",
            "samples": 80,
            "final_samples": 80,
            "required_final_samples": 80,
            "final_decision_cohorts": 5,
            "required_final_decision_cohorts": 5,
            "feature_log_loss": 0.20,
            "score_log_loss": 0.60,
            "null_log_loss": 0.69,
            "final_feature_log_loss": 0.21,
            "final_score_log_loss": 0.61,
            "final_null_log_loss": 0.69,
            "candidate_coef": candidate_coef,
            "candidate_intercept": -0.10,
            "candidate_train_samples": 240,
            "selected_policy_status": "positive",
            "selected_policy_confidence_threshold": 0.52,
            "selected_policy_samples": 80,
            "selected_policy_weighted_mean_return": 0.01,
            "selected_policy_weighted_mean_return_lower_bound": 0.005,
            "selected_policy_temporal_cluster_count": 20,
            "selected_policy_minimum_temporal_clusters": 20,
            "selected_policy_weighted_effective_return_samples": 80.0,
            "selected_policy_weighted_effective_temporal_clusters": 20.0,
            "selected_policy_weighted_temporal_mean_return": 0.01,
            "selected_policy_weighted_temporal_mean_return_lower_bound": 0.005,
            "candidate_platt": calibration.PlattScaler(
                a=1.20,
                b=-0.05,
                fitted=True,
                saved_ts=1_799_000_000,
            ),
        }

    monkeypatch.setattr(calibration, "_time_series_oof_logits", fake_oof)
    monkeypatch.setattr(calibration, "_time_series_oof_skill_diagnostics", fake_skill)
    monkeypatch.setattr(
        calibration,
        "_fit_weighted_logreg_raw",
        lambda *_args, **_kwargs: ([99.0] * len(calibration.FEATURE_NAMES), 99.0),
    )

    model = calibration.fit_logreg(
        rows,
        min_samples=80,
        logreg_min_samples=300,
        half_life_days=100_000,
    )

    assert model.fitted is True
    assert model.coef == candidate_coef
    assert model.intercept == pytest.approx(-0.10)
    assert model.platt.a == pytest.approx(1.20)
    assert model.platt.b == pytest.approx(-0.05)


def test_twenty_cluster_lower_bound_uses_small_sample_critical_value(monkeypatch) -> None:
    monkeypatch.setattr(calibration.time, "time", lambda: 1_800_000_000)
    horizon = 12 * 3600
    mean = 0.00385
    spread = 0.01
    cluster_returns = [mean + spread] * 10 + [mean - spread] * 10
    rows: list[dict] = []
    for cohort, ret in enumerate(cluster_returns):
        ts = 1_770_000_000 + cohort * horizon
        for slot in range(4):
            rows.append({
                "score": 0.70 if ret > 0 else 0.30,
                "success": int(ret > 0),
                "ret": ret,
                "ts": ts,
                "label_available_ts": ts + horizon,
                "horizon_sec": horizon,
                "symbol": f"S{slot:02d}USDT",
                "reasons": {"feature_snapshot": _feature_snapshot(
                    mean_reversion_score=0.30,
                    score=0.70 if ret > 0 else 0.30,
                )},
            })

    model = calibration.fit_logreg(
        rows,
        min_samples=80,
        logreg_min_samples=300,
        half_life_days=1_000_000_000_000,
    )
    sample_mean = statistics.mean(cluster_returns)
    sample_std = statistics.stdev(cluster_returns)
    t_95_df19 = 1.729132811521367
    independent_lcb = sample_mean - t_95_df19 * sample_std / math.sqrt(20)

    assert independent_lcb < 0.0
    assert model.weighted_temporal_mean_return_lower_bound == pytest.approx(
        independent_lcb,
        abs=2e-8,
    )
    assert model.expectancy_status != "positive"


def test_direction_calibrator_uses_horizon_price_direction_not_grid_profit(monkeypatch) -> None:
    captured: dict[str, list[int]] = {}
    rows = [
        {
            "model_version": CURRENT_MODEL,
            "bot_type": "futures_grid",
            "success": 1,
            "entry_close": 100.0,
            "exit_close": 90.0,
            "reasons": {
                "feature_snapshot": _feature_snapshot(mean_reversion_score=0.30),
                "direction_agg": {"direction": "long", "direction_confidence": 0.80},
            },
        },
        {
            "model_version": CURRENT_MODEL,
            "bot_type": "futures_grid",
            "success": 0,
            "entry_close": 100.0,
            "exit_close": 110.0,
            "reasons": {
                "feature_snapshot": _feature_snapshot(mean_reversion_score=0.30),
                "direction_agg": {"direction": "long", "direction_confidence": 0.70},
            },
        },
    ]

    monkeypatch.setattr(db, "get_outcomes_with_recs", lambda *_a, **_kw: rows)

    def capture_fit(_xs, ys, **_kwargs):
        captured["ys"] = list(ys)
        return calibration.PlattScaler(fitted=False)

    monkeypatch.setattr(recommender, "fit_platt", capture_fit)
    recommender._fit_direction_calibrator(None, min_samples=1)

    assert captured["ys"] == [0, 1]


def test_fresh_schema_contains_persistent_outcome_observability_ledger() -> None:
    conn = db.connect(":memory:")
    try:
        db.init_db(conn)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(reco_outcome_observability)").fetchall()
        }
    finally:
        conn.close()

    assert {
        "rec_id",
        "recommendation_ts",
        "label_due_ts",
        "last_attempt_ts",
        "state",
        "reason",
        "details_json",
    }.issubset(columns)


def test_censored_matured_policy_root_invalidates_positive_expectancy() -> None:
    gate = getattr(recommender, "_apply_outcome_observability_gate", None)
    assert callable(gate)
    model = calibration.LogRegScaler(
        fitted=True,
        coef=[1.0],
        expectancy_status="positive",
        return_samples=80,
        weighted_mean_return=0.01,
        weighted_mean_return_lower_bound=0.005,
    )
    diagnostics = {
        "matured_total": 81,
        "labeled_total": 80,
        "censored_total": 1,
        "unresolved_total": 0,
    }

    gated = gate(model, diagnostics)

    assert gated.fitted is False
    assert gated.coef == []
    assert gated.expectancy_status == "censored"
    reason = recommender._calibration_expectancy_no_trade_reason(gated)
    assert reason is not None
    assert reason["code"] == "PROXY_OUTCOME_CENSORING_UNBOUNDED"


def test_policy_fingerprint_changes_with_threshold_and_risk_contract() -> None:
    fingerprint = getattr(recommender, "calibration_policy_fingerprint", None)
    assert callable(fingerprint)
    base = SimpleNamespace(
        mean_reversion_min_score=0.25,
        min_score_to_recommend=0.08,
        min_conf_to_recommend=0.52,
        require_conf_gate=True,
        calib_min_samples=80,
        taker_fee_bps_linear=6.0,
        llm_reviewer_enabled=False,
    )
    changed_floor = SimpleNamespace(**{**vars(base), "mean_reversion_min_score": 0.31})

    fp_base = fingerprint(base, {"max_leverage": 3})
    fp_floor = fingerprint(changed_floor, {"max_leverage": 3})
    fp_risk = fingerprint(base, {"max_leverage": 5})

    assert len(fp_base) == 64
    assert fp_base != fp_floor
    assert fp_base != fp_risk


def test_required_confidence_gate_fails_closed_without_probability_model() -> None:
    gate = getattr(recommender, "_probability_calibration_no_trade_reason", None)
    assert callable(gate)
    reason = gate(
        calibration.LogRegScaler(
            fitted=False,
            expectancy_status="positive",
            return_samples=80,
            weighted_mean_return=0.01,
            weighted_mean_return_lower_bound=0.005,
        ),
        require_conf_gate=True,
    )

    assert reason is not None
    assert reason["code"] == "CALIBRATED_CONFIDENCE_UNAVAILABLE"


def test_claimed_policy_digest_is_recomputed_from_persisted_contract(monkeypatch) -> None:
    monkeypatch.setattr(recommender.time, "time", lambda: 1_800_000_000)
    ts = 1_790_000_000
    contract = {"schema_version": "candidate-policy-v1", "threshold": 0.25}
    fingerprint = recommender.calibration_policy_contract_fingerprint(contract)
    row = {
        "model_version": CURRENT_MODEL,
        "bot_type": "futures_grid",
        "ts": ts,
        "reasons": {
            "feature_snapshot": _feature_snapshot(mean_reversion_score=0.35),
            "outcome_policy": {
                "policy_evaluation_eligible": True,
                "policy_fingerprint": fingerprint,
                "policy_contract": {**contract, "threshold": 0.99},
                "label_due_ts": ts + 12 * 3600 + 120,
            },
        },
    }

    diagnostics = recommender.calibration_lineage_diagnostics(
        [row],
        policy_fingerprint=fingerprint,
        mean_reversion_min_score=0.25,
    )

    assert diagnostics["policy_eligible_total"] == 0
    assert diagnostics["dropped_invalid_policy_contract"] == 1


def test_direction_platt_without_chronological_skill_is_audit_only() -> None:
    scaler = calibration.PlattScaler(a=20.0, b=5.0, fitted=True)
    projection = recommender._direction_confidence_projection(
        {"direction_confidence": 0.20},
        scaler,
    )

    assert projection["feature_value"] == pytest.approx(0.20)
    assert projection["audit_probability"] != pytest.approx(0.20)
    assert projection["used_for_inference"] is False
