from __future__ import annotations

import pytest

from app import calibration, db, recommender


HORIZON_SEC = 12 * 3600
FIXED_NOW = 1_900_000_000


def _snapshot(*, signal: float, score: float = 0.50) -> dict[str, float]:
    return {
        "range_score": signal,
        "mean_reversion_score": 0.35,
        "mean_reversion_evidence_valid": 1.0,
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
        # Exact inputs of the publication confidence policy.  With a mature
        # model the current 50/50 blend selects signal=1 and rejects signal=0.
        "selection_confidence_raw": 0.50,
        "selection_confidence_adjustment": 1.0,
    }


def _selected_policy_counterexample_rows() -> list[dict]:
    """Whole candidate cohort wins while the model-selected half loses."""
    rows: list[dict] = []
    base = FIXED_NOW - 50 * HORIZON_SEC
    index = 0
    for cohort in range(40):
        ts = base + cohort * HORIZON_SEC
        # High hit-rate group: 80% tiny wins, 20% large losses, mean -0.16%.
        for slot in range(15):
            success = int(slot < 12)
            ret = 0.001 if success else -0.012
            rows.append({
                "score": 0.50,
                "success": success,
                "ret": ret,
                "ts": ts,
                "label_available_ts": ts + HORIZON_SEC,
                "horizon_sec": HORIZON_SEC,
                "symbol": f"A{index:04d}USDT",
                "reasons": {"feature_snapshot": _snapshot(signal=1.0)},
            })
            index += 1
        # Low hit-rate group: 20% large wins, 80% tiny losses, mean +0.72%.
        for slot in range(15):
            success = int(slot < 3)
            ret = 0.040 if success else -0.001
            rows.append({
                "score": 0.50,
                "success": success,
                "ret": ret,
                "ts": ts,
                "label_available_ts": ts + HORIZON_SEC,
                "horizon_sec": HORIZON_SEC,
                "symbol": f"B{index:04d}USDT",
                "reasons": {"feature_snapshot": _snapshot(signal=0.0)},
            })
            index += 1
    return rows


def test_probability_selector_cannot_activate_when_selected_oof_policy_loses_money(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calibration.time, "time", lambda: FIXED_NOW)
    rows = _selected_policy_counterexample_rows()
    assert sum(float(row["ret"]) for row in rows) / len(rows) == pytest.approx(0.0028)

    model = calibration.fit_logreg(
        rows,
        min_samples=80,
        logreg_min_samples=300,
        half_life_days=1_000_000_000.0,
    )

    assert model.expectancy_status == "positive"
    assert model.selected_policy_expectancy_status == "negative"
    assert model.selected_policy_weighted_mean_return is not None
    assert model.selected_policy_weighted_mean_return < 0.0
    assert model.fitted is False


def test_terminal_holdout_is_a_full_minimum_sample_of_whole_decision_timestamps() -> None:
    labels: list[int] = []
    tss: list[int] = []
    available: list[int] = []
    base = FIXED_NOW - 30 * HORIZON_SEC
    for index in range(301):
        cohort = min(19, index // 15)
        ts = base + cohort * HORIZON_SEC
        labels.append(index % 2)
        tss.append(ts)
        available.append(ts + HORIZON_SEC)

    result = calibration._time_series_oof_skill_diagnostics(
        [[1.0 if label else -1.0] for label in labels],
        [0.50] * len(labels),
        labels,
        [1.0] * len(labels),
        min_samples=80,
        tss=tss,
        label_available_tss=available,
    )

    assert result["status"] == "accepted"
    assert result["final_samples"] >= 80
    assert result["final_decision_cohorts"] >= 5


def test_runtime_and_oof_use_one_confidence_selection_formula() -> None:
    confidence = calibration.selected_policy_confidence(
        0.50,
        0.7990196856,
        300,
        0.80,
    )

    assert confidence == pytest.approx(0.51960787424)
    assert calibration.selected_policy_confidence(0.50, 0.80, 0, 1.0) is None
    assert calibration.selected_policy_confidence(0.50, 0.80, 300, 0.0) is None


def _persistable_positive_model() -> calibration.LogRegScaler:
    return calibration.LogRegScaler(
        coef=[0.1] * calibration.N_FEATURES,
        intercept=0.0,
        platt=calibration.PlattScaler(fitted=True),
        fitted=True,
        saved_ts=FIXED_NOW,
        n_samples=240,
        return_samples=400,
        expectancy_status="positive",
        weighted_mean_return=0.01,
        oof_status="sufficient",
        oof_samples=160,
        oof_required_samples=80,
        oof_skill_status="accepted",
        oof_final_samples=80,
        oof_required_final_samples=80,
        oof_final_decision_cohorts=5,
        oof_required_final_decision_cohorts=5,
        selected_policy_expectancy_status="positive",
        selected_policy_confidence_threshold=0.62,
        selected_policy_samples=100,
        selected_policy_weighted_mean_return=0.004,
        selected_policy_weighted_expected_shortfall=-0.002,
        selected_policy_weighted_return_std=0.003,
        selected_policy_weighted_effective_return_samples=100.0,
        selected_policy_weighted_mean_return_lower_bound=0.003,
        selected_policy_temporal_cluster_count=20,
        selected_policy_minimum_temporal_clusters=20,
        selected_policy_weighted_effective_temporal_clusters=20.0,
        selected_policy_weighted_temporal_mean_return=0.004,
        selected_policy_weighted_temporal_return_std=0.002,
        selected_policy_weighted_temporal_mean_return_lower_bound=0.002,
        terminal_selected_policy_expectancy_status="positive",
        terminal_selected_policy_samples=80,
        terminal_selected_policy_required_samples=80,
        terminal_selected_policy_weighted_mean_return=0.003,
        terminal_selected_policy_weighted_effective_return_samples=80.0,
        terminal_selected_policy_weighted_mean_return_lower_bound=0.002,
        terminal_selected_policy_temporal_cluster_count=5,
        terminal_selected_policy_required_temporal_clusters=5,
        terminal_selected_policy_weighted_effective_temporal_clusters=5.0,
        terminal_selected_policy_weighted_temporal_mean_return_lower_bound=0.001,
    )


def test_terminal_and_selected_policy_contract_round_trips(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "iteration261.sqlite"))
    db.init_db(conn)
    try:
        calibration.save_logreg_to_db(conn, "iteration261", _persistable_positive_model())
        loaded = calibration.load_logreg_from_db(conn, "iteration261")
    finally:
        conn.close()

    assert loaded is not None
    assert loaded.fitted is True
    assert loaded.oof_final_samples == 80
    assert loaded.oof_final_decision_cohorts == 5
    assert loaded.selected_policy_expectancy_status == "positive"
    assert loaded.selected_policy_confidence_threshold == pytest.approx(0.62)
    assert loaded.selected_policy_weighted_mean_return == pytest.approx(0.004)
    assert loaded.terminal_selected_policy_expectancy_status == "positive"
    assert loaded.terminal_selected_policy_samples == 80


def test_fitted_payload_without_selected_policy_evidence_is_rejected(tmp_path) -> None:
    model = _persistable_positive_model()
    model.selected_policy_expectancy_status = "not_evaluated"
    conn = db.connect(str(tmp_path / "iteration261-invalid.sqlite"))
    db.init_db(conn)
    try:
        calibration.save_logreg_to_db(conn, "iteration261-invalid", model)
        loaded = calibration.load_logreg_from_db(conn, "iteration261-invalid")
    finally:
        conn.close()

    assert loaded is None


def test_probability_gate_rejects_tiny_terminal_or_negative_selected_policy() -> None:
    model = _persistable_positive_model()
    model.oof_final_samples = 1
    reason = recommender._probability_calibration_no_trade_reason(
        model,
        require_conf_gate=True,
    )
    assert reason is not None
    assert "terminal=1/80" in reason["msg"]

    model = _persistable_positive_model()
    model.selected_policy_expectancy_status = "negative"
    reason = recommender._probability_calibration_no_trade_reason(
        model,
        require_conf_gate=True,
    )
    assert reason is not None
    assert "selected_policy=negative" in reason["msg"]
