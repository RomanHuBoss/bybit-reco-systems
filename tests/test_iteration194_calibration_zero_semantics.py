from __future__ import annotations

import pytest

from app.calibration import extract_features


def test_feature_snapshot_preserves_valid_zero_values_instead_of_neutral_defaults() -> None:
    row = {
        "score": 0.0,
        "reasons": {
            "feature_snapshot": {
                "range_score": 0.0,
                "trend_strength": 0.0,
                "atr_pct_norm": 0.0,
                "effective_sentiment": 0.0,
                "dir_conf": 0.0,
                "coherence": 0.0,
                "spread_bps_norm": 0.0,
                "score": 0.0,
                "oi_4h_norm": 0.0,
                "funding_norm": 0.0,
                "liq_tier_num": 0.0,
                "btc_corr": 0.0,
                "regime_conf": 0.0,
            }
        },
    }

    features = extract_features(row)

    assert features is not None
    assert features == pytest.approx([0.0] * 13)


def test_legacy_feature_reconstruction_preserves_observed_zero_confidence_and_spread() -> None:
    row = {
        "score": 0.0,
        "reasons": {
            "direction_agg": {
                "direction_confidence": 0.0,
                "coherence": 0.0,
                "strength": {"all": 0.0},
                "regime_confidence": 0.0,
            },
            "cost_model": {"spread_bps": 0.0},
            "effective_sentiment": 0.0,
            "liquidity": {"tier": "micro"},
            "btc_beta": {"correlation": 0.0},
        },
    }

    features = extract_features(row)

    assert features is not None
    assert features[1] == pytest.approx(0.0)  # trend_strength
    assert features[4] == pytest.approx(0.0)  # dir_conf
    assert features[5] == pytest.approx(0.0)  # coherence
    assert features[6] == pytest.approx(0.0)  # spread_bps_norm
    assert features[10] == pytest.approx(0.0)  # micro liquidity tier
    assert features[11] == pytest.approx(0.0)  # btc correlation
    assert features[12] == pytest.approx(0.0)  # regime confidence
