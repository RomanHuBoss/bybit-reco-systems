from __future__ import annotations

import pytest

from app import calibration, db


HORIZON_SEC = 12 * 3600
FIXED_NOW = 1_900_000_000


def _snapshot(*, signal: float) -> dict[str, float]:
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
        "score": 0.50,
        "oi_4h_norm": 0.0,
        "funding_norm": 0.0,
        "liq_tier_num": 1.0,
        "btc_corr": 0.0,
        "regime_conf": 0.80,
        "selection_confidence_raw": 0.50,
        "selection_confidence_adjustment": 1.0,
    }


def _terminal_reversal_rows() -> list[dict]:
    """Selected policy wins historically but loses in the terminal regime."""
    rows: list[dict] = []
    base = FIXED_NOW - 50 * HORIZON_SEC
    symbol_index = 0
    for cohort in range(40):
        ts = base + cohort * HORIZON_SEC
        terminal_cohort = cohort >= 35

        # Feature-selected group: stable 80% hit rate.  Its money return is
        # positive in the training/history periods but negative in every one of
        # the five whole terminal cohorts.
        for slot in range(20):
            success = int(slot < 16)
            if terminal_cohort:
                ret = 0.001 if success else -0.010
            else:
                ret = 0.004 if success else -0.002
            rows.append({
                "score": 0.50,
                "success": success,
                "ret": ret,
                "ts": ts,
                "label_available_ts": ts + HORIZON_SEC,
                "horizon_sec": HORIZON_SEC,
                "symbol": f"A{symbol_index:04d}USDT",
                "reasons": {"feature_snapshot": _snapshot(signal=1.0)},
            })
            symbol_index += 1

        # Rejected group keeps the full candidate cohort economically positive
        # while its binary hit rate remains low enough for the feature model to
        # distinguish it from the selected group.
        for slot in range(10):
            success = int(slot < 2)
            rows.append({
                "score": 0.50,
                "success": success,
                "ret": 0.030 if success else -0.001,
                "ts": ts,
                "label_available_ts": ts + HORIZON_SEC,
                "horizon_sec": HORIZON_SEC,
                "symbol": f"B{symbol_index:04d}USDT",
                "reasons": {"feature_snapshot": _snapshot(signal=0.0)},
            })
            symbol_index += 1
    return rows


def test_terminal_selected_policy_loss_blocks_probability_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calibration.time, "time", lambda: FIXED_NOW)
    rows = _terminal_reversal_rows()
    terminal_start = FIXED_NOW - 15 * HORIZON_SEC
    terminal_selected_returns = [
        float(row["ret"])
        for row in rows
        if int(row["ts"]) >= terminal_start
        and str(row["symbol"]).startswith("A")
    ]

    assert sum(float(row["ret"]) for row in rows) / len(rows) == pytest.approx(
        0.0032666666666666664
    )
    assert len(terminal_selected_returns) == 100
    assert sum(terminal_selected_returns) / len(terminal_selected_returns) == pytest.approx(
        -0.0012
    )

    model = calibration.fit_logreg(
        rows,
        min_samples=80,
        logreg_min_samples=300,
        half_life_days=1_000_000_000.0,
    )

    assert model.oof_skill_status == "accepted"
    assert model.selected_policy_expectancy_status == "positive"
    assert model.selected_policy_weighted_mean_return_lower_bound is not None
    assert model.selected_policy_weighted_mean_return_lower_bound > 0.0
    assert model.fitted is False
    assert getattr(model, "terminal_selected_policy_expectancy_status", None) == "negative"
    assert getattr(model, "terminal_selected_policy_samples", 0) == 100
    assert getattr(model, "terminal_selected_policy_weighted_mean_return", None) == pytest.approx(
        -0.0012
    )


def test_fitted_cache_without_terminal_selected_policy_evidence_is_rejected(
    tmp_path,
) -> None:
    model = calibration.LogRegScaler(
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
        selected_policy_weighted_effective_return_samples=100.0,
        selected_policy_weighted_mean_return_lower_bound=0.003,
        selected_policy_temporal_cluster_count=20,
        selected_policy_minimum_temporal_clusters=20,
        selected_policy_weighted_effective_temporal_clusters=20.0,
        selected_policy_weighted_temporal_mean_return_lower_bound=0.002,
    )
    conn = db.connect(str(tmp_path / "iteration262-stale.sqlite"))
    db.init_db(conn)
    try:
        calibration.save_logreg_to_db(conn, "iteration262-stale", model)
        loaded = calibration.load_logreg_from_db(conn, "iteration262-stale")
    finally:
        conn.close()

    assert loaded is None
