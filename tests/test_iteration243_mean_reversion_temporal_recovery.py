from __future__ import annotations

import random

import pytest

from app import calibration
from app.recommender import _mean_reversion_grid_blocks


def _calibration_row(*, ts: int, available: int, success: int, ret: float, index: int) -> dict:
    score = 0.68 if success else 0.32
    return {
        "score": score,
        "success": success,
        "ret": ret,
        "ts": ts,
        "label_available_ts": available,
        "horizon_sec": available - ts,
        "symbol": f"SYM{index:03d}USDT",
        "reasons": {
            "feature_snapshot": {
                "range_score": 0.70,
                "mean_reversion_score": 0.30,
                "mean_reversion_evidence_valid": 1.0,
                "trend_strength": 0.20,
                "atr_pct_norm": 0.40,
                "effective_sentiment": 0.0,
                "dir_conf": 0.60,
                "coherence": 0.70,
                "spread_bps_norm": 0.50,
                "score": score,
                "oi_4h_norm": 0.0,
                "funding_norm": 0.0,
                "liq_tier_num": 0.67,
                "btc_corr": 0.80,
                "regime_conf": 0.80,
            }
        },
    }


def _overlap_chain_rows(fixed_now: int) -> list[dict]:
    horizon = 12 * 3600
    step = 6 * 3600
    base = fixed_now - (42 * step + horizon + 600)
    rows: list[dict] = []
    index = 0
    for cohort in range(42):
        ts = base + cohort * step
        available = ts + horizon
        # One market decision, two cross-sectional outcomes. The cohort mean is
        # positive, while class balance remains suitable for score calibration.
        for success, ret in ((1, 0.030), (0, -0.005)):
            rows.append(
                _calibration_row(
                    ts=ts,
                    available=available,
                    success=success,
                    ret=ret,
                    index=index,
                )
            )
            index += 1
    return rows


def test_observed_upper_tail_mean_reversion_candidate_is_not_rejected_by_obsolete_055_cutoff() -> None:
    # 0.351 is the observed maximum in the supplied 10k recommendation export.
    # It is a high-tail candidate, not proof of profit; monetary expectancy remains
    # a separate fail-closed gate.
    reasons = _mean_reversion_grid_blocks({
        "mean_reversion_evidence_valid": True,
        "mean_reversion_score": 0.351,
        "mean_reversion_tf_count": 5,
    })

    assert reasons == []


def test_weak_mean_reversion_message_does_not_claim_proven_negative_expectancy() -> None:
    reasons = _mean_reversion_grid_blocks({
        "mean_reversion_evidence_valid": True,
        "mean_reversion_score": 0.20,
        "mean_reversion_tf_count": 5,
    })

    assert [item["code"] for item in reasons] == ["MEAN_REVERSION_EDGE_UNCONFIRMED"]
    message = reasons[0]["msg"].lower()
    assert "комиссии дают отрицательное ожидание" not in message
    assert "negative expectancy" not in message
    assert "не доказ" in message or "не подтверж" in message


def test_transitive_overlap_chain_yields_maximal_non_overlapping_decision_cohorts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = 1_800_000_000
    monkeypatch.setattr(calibration.time, "time", lambda: fixed_now)
    rows = _overlap_chain_rows(fixed_now)

    model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)

    # 42 six-hour decision cohorts with twelve-hour horizons contain a maximum
    # of 21 pairwise non-overlapping cohorts. Connected-component merging wrongly
    # collapses the whole transitive chain to one observation forever.
    assert model.return_samples == 84
    assert model.temporal_cluster_count == 21
    assert model.weighted_effective_temporal_clusters >= 20.0
    assert model.weighted_temporal_mean_return_lower_bound is not None
    assert model.weighted_temporal_mean_return_lower_bound > 0.0
    assert model.expectancy_status == "positive"


def test_same_timestamp_cross_section_remains_one_temporal_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = 1_800_000_000
    monkeypatch.setattr(calibration.time, "time", lambda: fixed_now)
    horizon = 12 * 3600
    ts = fixed_now - 2 * horizon
    rows = [
        _calibration_row(
            ts=ts,
            available=ts + horizon,
            success=1 if index < 40 else 0,
            ret=0.03 if index < 40 else -0.01,
            index=index,
        )
        for index in range(80)
    ]

    model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)

    assert model.temporal_cluster_count == 1
    assert model.expectancy_status == "insufficient"


def test_temporal_thinning_is_deterministic_under_input_reordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = 1_800_000_000
    monkeypatch.setattr(calibration.time, "time", lambda: fixed_now)
    rows = _overlap_chain_rows(fixed_now)
    shuffled = list(rows)
    random.Random(243).shuffle(shuffled)

    ordered_model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)
    shuffled_model = calibration.fit_logreg(shuffled, min_samples=80, logreg_min_samples=300)

    assert shuffled_model.temporal_cluster_count == ordered_model.temporal_cluster_count
    assert shuffled_model.weighted_effective_temporal_clusters == pytest.approx(
        ordered_model.weighted_effective_temporal_clusters
    )
    assert shuffled_model.weighted_temporal_mean_return_lower_bound == pytest.approx(
        ordered_model.weighted_temporal_mean_return_lower_bound
    )


def test_mean_reversion_candidate_floor_is_explicitly_configurable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from app.settings import load_settings

    monkeypatch.setenv("DB_ENGINE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "settings.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "locks.db"))
    monkeypatch.setenv("MEAN_REVERSION_MIN_SCORE", "0.31")

    loaded = load_settings()

    assert loaded.mean_reversion_min_score == pytest.approx(0.31)
