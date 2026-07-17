from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import calibration, db


def _row(*, index: int, cluster: int, success: int, ret: float, total_clusters: int = 40) -> dict:
    now = int(time.time())
    horizon = 12 * 3600
    # Keep every cluster fully in the past and non-overlapping. Rows with the same
    # cluster intentionally share one market interval.
    label_available_ts = now - (total_clusters - cluster + 2) * horizon
    ts = label_available_ts - horizon
    score = 0.80 if success else 0.20
    return {
        "score": score,
        "success": success,
        "ret": ret,
        "ts": ts,
        "label_available_ts": label_available_ts,
        "horizon_sec": horizon,
        "symbol": f"SYM{index:04d}USDT",
        "reasons": {
            "feature_snapshot": {
                "range_score": 0.75 if success else 0.25,
                "trend_strength": 0.20 if success else 0.80,
                "atr_pct_norm": 0.40,
                "effective_sentiment": 0.0,
                "dir_conf": 0.70 if success else 0.30,
                "coherence": 0.75 if success else 0.25,
                "spread_bps_norm": 0.50,
                "score": score,
                "oi_4h_norm": 0.0,
                "funding_norm": 0.0,
                "liq_tier_num": 0.67,
                "btc_corr": 0.80,
                "regime_conf": 0.80,
                # Keep the whole terminal block inside the publication subset;
                # iteration 262 requires its monetary holdout to reach the same
                # 80-row floor as the aggregate calibration contract.
                "selection_confidence_raw": 0.90,
                "selection_confidence_adjustment": 1.0,
            }
        },
    }


def _concentrated_rows() -> list[dict]:
    rows: list[dict] = []
    index = 0
    # 280 rows share one interval. Every chronological fold boundary of the
    # 320-row dataset falls inside this cluster, so no training label is known
    # strictly before the validation timestamp.
    for _ in range(140):
        rows.append(_row(index=index, cluster=0, success=1, ret=0.03))
        index += 1
    for _ in range(140):
        rows.append(_row(index=index, cluster=0, success=0, ret=-0.01))
        index += 1
    # Twenty later independent clusters make the monetary temporal gate positive,
    # but contain too few rows to move any fixed OOF split past the first cluster.
    for cluster in range(1, 21):
        rows.append(_row(index=index, cluster=cluster, success=1, ret=0.03))
        index += 1
        rows.append(_row(index=index, cluster=cluster, success=0, ret=-0.01))
        index += 1
    return rows


def _distributed_rows() -> list[dict]:
    rows: list[dict] = []
    index = 0
    # 40 independent clusters × 8 rows. Later folds have more than 80 labels that
    # were fully observable before validation, so full feature LogReg is eligible.
    for cluster in range(40):
        for _ in range(4):
            rows.append(_row(index=index, cluster=cluster, success=1, ret=0.03))
            index += 1
        for _ in range(4):
            rows.append(_row(index=index, cluster=cluster, success=0, ret=-0.01))
            index += 1
    return rows


def test_probability_model_remains_unavailable_when_purged_oof_is_insufficient() -> None:
    model = calibration.fit_logreg(
        _concentrated_rows(),
        min_samples=80,
        logreg_min_samples=300,
        half_life_days=100_000,
    )

    assert model.expectancy_status == "positive"
    assert model.temporal_cluster_count == 21
    assert model.oof_status == "insufficient"
    assert model.oof_samples == 0
    assert model.oof_required_samples == 80
    assert model.coef == []
    assert model.platt.fitted is False
    assert model.fitted is False


def test_feature_logreg_activates_only_with_sufficient_purged_oof() -> None:
    model = calibration.fit_logreg(
        _distributed_rows(),
        min_samples=80,
        logreg_min_samples=300,
        half_life_days=100_000,
    )

    assert model.expectancy_status == "positive"
    assert model.temporal_cluster_count == 40
    assert model.oof_status == "sufficient"
    assert model.oof_samples >= 80
    assert model.oof_required_samples == 80
    assert len(model.coef) > 0
    assert model.platt.fitted is True
    assert model.fitted is True


def test_oof_activation_diagnostics_survive_persistence(tmp_path: Path) -> None:
    model = calibration.fit_logreg(
        _concentrated_rows(),
        min_samples=80,
        logreg_min_samples=300,
        half_life_days=100_000,
    )
    conn = db.connect(str(tmp_path / "oof-gate.db"))
    try:
        db.init_db(conn)
        calibration.save_logreg_to_db(conn, "iteration242", model)
        loaded = calibration.load_logreg_from_db(conn, "iteration242")
    finally:
        conn.close()

    assert loaded is not None
    assert loaded.oof_status == "insufficient"
    assert loaded.oof_samples == 0
    assert loaded.oof_required_samples == 80
    assert loaded.coef == []
