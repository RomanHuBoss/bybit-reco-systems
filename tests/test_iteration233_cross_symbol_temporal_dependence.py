from __future__ import annotations

import time

import pytest

from app import calibration


def _row(*, index: int, cluster: int, success: int, ret: float) -> dict:
    now = int(time.time())
    horizon = 12 * 3600
    # Four cross-sectional symbols share each 12-hour market block.
    label_available_ts = now - (24 - cluster) * horizon
    ts = label_available_ts - horizon
    return {
        "score": 0.65 if success else 0.35,
        "success": success,
        "ret": ret,
        "ts": ts,
        "label_available_ts": label_available_ts,
        "horizon_sec": horizon,
        "symbol": f"SYM{index:03d}USDT",
        "reasons": {
            "feature_snapshot": {
                "range_score": 0.7,
                "trend_strength": 0.2,
                "atr_pct_norm": 0.4,
                "effective_sentiment": 0.0,
                "dir_conf": 0.6,
                "coherence": 0.7,
                "spread_bps_norm": 0.5,
                "score": 0.65 if success else 0.35,
                "oi_4h_norm": 0.0,
                "funding_norm": 0.0,
                "liq_tier_num": 0.67,
                "btc_corr": 0.8,
                "regime_conf": 0.8,
            }
        },
    }


def test_eighty_correlated_symbols_in_one_horizon_are_not_eighty_independent_returns() -> None:
    rows = [
        _row(
            index=i,
            cluster=23,
            success=1 if i < 40 else 0,
            ret=0.03 if i < 40 else -0.01,
        )
        for i in range(80)
    ]

    model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)

    assert model.return_samples == 80
    assert model.temporal_cluster_count == 1
    assert model.weighted_effective_temporal_clusters == pytest.approx(1.0)
    assert model.expectancy_status == "insufficient"
    assert model.fitted is False


def test_twenty_separate_horizons_can_establish_temporally_independent_positive_evidence() -> None:
    rows: list[dict] = []
    index = 0
    for cluster in range(3, 24):
        # Every independent block has three +3% and two -1% cross-sectional outcomes.
        # Positive evidence comes from 21 separate market windows rather than a
        # large number of correlated symbols in one window.
        for success, ret in ((1, 0.03), (1, 0.03), (1, 0.03), (0, -0.01), (0, -0.01)):
            rows.append(_row(index=index, cluster=cluster, success=success, ret=ret))
            index += 1

    model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)

    assert model.return_samples == 105
    assert model.temporal_cluster_count == 21
    assert model.weighted_effective_temporal_clusters >= 20.0
    assert model.weighted_temporal_mean_return_lower_bound is not None
    assert model.weighted_temporal_mean_return_lower_bound > 0.0
    assert model.expectancy_status == "positive"
    assert model.fitted is True



def test_overlapping_horizons_straddling_a_clock_bucket_remain_one_cluster() -> None:
    horizon = 12 * 3600
    base = 20 * horizon
    rows = [
        {
            **_row(index=0, cluster=20, success=1, ret=0.02),
            "ts": base - 1,
            "label_available_ts": base + horizon - 1,
        },
        {
            **_row(index=1, cluster=21, success=1, ret=0.02),
            "ts": base + horizon - 2,
            "label_available_ts": base + 2 * horizon - 2,
        },
    ]

    model = calibration.fit_logreg(rows, min_samples=2, logreg_min_samples=300)

    assert model.temporal_cluster_count == 1
    assert model.expectancy_status == "insufficient"
    assert model.fitted is False

def test_temporal_cluster_diagnostics_survive_calibrator_persistence(tmp_path) -> None:
    rows: list[dict] = []
    index = 0
    for cluster in range(3, 24):
        for success, ret in ((1, 0.03), (1, 0.03), (1, 0.03), (0, -0.01), (0, -0.01)):
            rows.append(_row(index=index, cluster=cluster, success=success, ret=ret))
            index += 1
    model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)

    from app import db

    conn = db.connect(str(tmp_path / "iteration233.db"))
    db.init_db(conn)
    try:
        calibration.save_logreg_to_db(conn, "iteration233", model)
        loaded = calibration.load_logreg_from_db(conn, "iteration233")
    finally:
        conn.close()

    assert loaded is not None
    assert loaded.temporal_cluster_count == 21
    assert loaded.weighted_effective_temporal_clusters == pytest.approx(
        model.weighted_effective_temporal_clusters
    )
    assert loaded.weighted_temporal_mean_return_lower_bound == pytest.approx(
        model.weighted_temporal_mean_return_lower_bound
    )
