from __future__ import annotations

import pytest

from app import calibration, recommender


HORIZON_SEC = 12 * 3600
FIXED_NOW = 1_900_000_000


def _snapshot(*, sentiment: float) -> dict[str, float]:
    return {
        "range_score": 0.20,
        "trend_strength": 0.80,
        "atr_pct_norm": 0.40,
        "effective_sentiment": sentiment,
        "dir_conf": 0.80,
        "coherence": 0.80,
        "spread_bps_norm": 0.50,
        "score": 0.50,
        "oi_4h_norm": 0.20,
        "funding_norm": 0.10,
        "liq_tier_num": 1.00,
        "btc_corr": 0.70,
        "regime_conf": 0.90,
        "selection_confidence_raw": 0.90,
        "selection_confidence_adjustment": 1.00,
    }


def _row(*, direction: str, success: int, sentiment: float, ts: int, index: int) -> dict:
    return {
        "score": 0.50,
        "success": success,
        "ret": 0.020 if success else -0.005,
        "ts": ts,
        "label_available_ts": ts + HORIZON_SEC,
        "horizon_sec": HORIZON_SEC,
        "symbol": f"SYM{index:04d}USDT",
        "direction": direction,
        "bot_type": "directional_trend",
        "reasons": {"feature_snapshot": _snapshot(sentiment=sentiment)},
    }


def test_supportive_sentiment_has_one_direction_aligned_semantics() -> None:
    long_support = _row(direction="long", success=1, sentiment=0.90, ts=1, index=1)
    short_support = _row(direction="short", success=1, sentiment=-0.90, ts=1, index=2)
    long_oppose = _row(direction="long", success=0, sentiment=-0.90, ts=1, index=3)
    short_oppose = _row(direction="short", success=0, sentiment=0.90, ts=1, index=4)

    alignment_index = calibration.FEATURE_NAMES.index("sentiment_alignment")
    direction_index = calibration.FEATURE_NAMES.index("direction_sign")

    long_support_features = calibration.extract_features(long_support)
    short_support_features = calibration.extract_features(short_support)
    long_oppose_features = calibration.extract_features(long_oppose)
    short_oppose_features = calibration.extract_features(short_oppose)

    assert long_support_features is not None
    assert short_support_features is not None
    assert long_oppose_features is not None
    assert short_oppose_features is not None
    assert long_support_features[direction_index] == pytest.approx(1.0)
    assert short_support_features[direction_index] == pytest.approx(-1.0)
    assert long_support_features[alignment_index] == pytest.approx(0.90)
    assert short_support_features[alignment_index] == pytest.approx(0.90)
    assert long_oppose_features[alignment_index] == pytest.approx(-0.90)
    assert short_oppose_features[alignment_index] == pytest.approx(-0.90)


def test_pooled_directional_calibrator_learns_symmetric_long_short_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[dict] = []
    index = 0
    for cohort in range(50):
        ts = FIXED_NOW - (52 - cohort) * HORIZON_SEC
        # Raw sentiment alone is independent of success: every label contains
        # both +0.9 and -0.9.  Only direction-aligned sentiment is predictive.
        for direction, success, sentiment in (
            ("long", 1, 0.90),
            ("long", 0, -0.90),
            ("short", 1, -0.90),
            ("short", 0, 0.90),
        ) * 2:
            rows.append(
                _row(
                    direction=direction,
                    success=success,
                    sentiment=sentiment,
                    ts=ts,
                    index=index,
                )
            )
            index += 1

    monkeypatch.setattr(calibration.time, "time", lambda: FIXED_NOW)
    model = calibration.fit_logreg(
        rows,
        min_samples=20,
        logreg_min_samples=300,
        half_life_days=1_000_000_000.0,
    )

    assert model.oof_skill_status == "accepted"
    assert model.oof_feature_log_loss is not None
    assert model.oof_null_log_loss is not None
    assert model.oof_feature_log_loss < 0.10
    assert model.oof_feature_log_loss < model.oof_null_log_loss - 0.50
    assert model.selected_policy_expectancy_status == "positive"
    assert model.terminal_selected_policy_expectancy_status == "positive"
    assert model.fitted is True
    assert len(model.coef) == len(calibration.FEATURE_NAMES)



def test_first_touch_model_learns_the_same_long_short_alignment() -> None:
    from app.trend_events import fit_trend_event_model

    rows: list[dict] = []
    index = 0
    base_ts = 1_700_000_000
    for cohort in range(50):
        ts = base_ts + cohort * 13 * 3600
        for direction, event_type, sentiment, ret in (
            ("long", "TP_FIRST", 0.90, 0.020),
            ("short", "TP_FIRST", -0.90, 0.020),
            ("long", "SL_FIRST", -0.90, -0.010),
            ("short", "SL_FIRST", 0.90, -0.010),
            ("long", "HORIZON_EXIT", 0.00, 0.001),
            ("short", "HORIZON_EXIT", 0.00, 0.001),
        ):
            row = _row(
                direction=direction,
                success=1 if event_type == "TP_FIRST" else 0,
                sentiment=sentiment,
                ts=ts,
                index=index,
            )
            row.update(
                event_type=event_type,
                ret=ret,
                candidate_kind="strategy_recommendation",
                label_available_ts=ts + HORIZON_SEC + 60,
            )
            rows.append(row)
            index += 1

    model = fit_trend_event_model(
        rows,
        min_samples=80,
        policy_fingerprint="a" * 64,
    )

    assert model.fitted is True
    assert model.holdout_status == "accepted"
    assert model.holdout_log_loss is not None
    assert model.holdout_null_log_loss is not None
    assert model.holdout_log_loss < 0.40
    assert model.holdout_log_loss < model.holdout_null_log_loss - 0.50

def test_persisted_direction_features_must_match_recommendation_direction() -> None:
    row = _row(direction="long", success=1, sentiment=0.90, ts=1, index=1)
    snapshot = row["reasons"]["feature_snapshot"]
    snapshot["direction_sign"] = -1.0
    snapshot["sentiment_alignment"] = -0.90

    assert calibration.extract_features(row) is None


def test_runtime_feature_snapshot_contains_direction_aware_fields() -> None:
    snapshot = recommender._build_feature_snapshot(
        score=0.50,
        atr_pct=0.04,
        effective_sent=-0.80,
        cost_model={"spread_bps": 5.0, "funding_cost_bps_for_approval": 1.0},
        direction_agg={
            "direction": "short",
            "trendiness": 0.80,
            "direction_confidence_feature": 0.80,
            "coherence": 0.80,
            "mean_reversion_evidence_valid": False,
            "regime_confidence": 0.90,
        },
        oi_sig={"oi_4h_chg_pct": 2.0},
        liq_tier="high",
        beta_info={"correlation": 0.70},
        direction="short",
    )

    assert snapshot["direction_sign"] == pytest.approx(-1.0)
    assert snapshot["sentiment_alignment"] == pytest.approx(0.80)


def test_direction_feature_schema_starts_a_new_model_lineage() -> None:
    assert calibration.FEATURE_NAMES[-2:] == ["direction_sign", "sentiment_alignment"]
    assert recommender.RECOMMENDER_MODEL_VERSION == "bybit-taxonomy-v13-log-symmetric-direction"
    assert calibration.BOT_CALIB_KEYS["futures_grid"] == "logreg_futures_grid_v23"
    assert calibration.BOT_CALIB_KEYS["directional_trend"] == "logreg_directional_trend_v4"
