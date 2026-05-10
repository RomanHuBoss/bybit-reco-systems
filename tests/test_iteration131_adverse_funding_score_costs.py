from __future__ import annotations

import pytest

from app.recommender import _build_feature_snapshot, _params, _score


def _range_feature() -> dict:
    return {
        "price": 100.0,
        "atr_pct": 0.002,
        "_atr_pct_1h": 0.002,
        "spread_bps": 1.0,
        "range_score": 0.85,
        "_direction_agg": {
            "direction": "neutral",
            "trendiness": 0.05,
            "coherence": 0.85,
            "regime": "range",
            "regime_confidence": 0.85,
            "strength": {"all": 0.05},
        },
    }


def test_grid_score_penalizes_adverse_funding_for_neutral_grid() -> None:
    base_cost = {
        "spread_bps": 1.0,
        "execution_cost_bps": 8.0,
        "total_cost_bps": 8.0,
        "expected_funding_bps": 0.0,
        "funding_cost_bps_for_approval": 0.0,
        "net_cost_bps": 8.0,
    }
    expensive_cost = {
        **base_cost,
        "expected_funding_bps": 18.0,
        "funding_cost_bps_for_approval": 18.0,
        "net_cost_bps": 26.0,
    }

    base_score, base_conf, base_reasons = _score(
        "futures_grid",
        "linear",
        _range_feature(),
        taker_fee_bps=4.0,
        global_sent=0.0,
        cost_model=base_cost,
    )
    expensive_score, expensive_conf, expensive_reasons = _score(
        "futures_grid",
        "linear",
        _range_feature(),
        taker_fee_bps=4.0,
        global_sent=0.0,
        cost_model=expensive_cost,
    )

    assert expensive_score < base_score
    assert expensive_conf < base_conf
    negative_features = {item["feature"] for item in expensive_reasons["top_negative_factors"]}
    assert "economic_cost_bps" in negative_features
    assert "adverse_funding_cost_bps" in negative_features
    assert base_reasons["score_components"]["economic_cost_bps"] == pytest.approx(8.0)
    assert expensive_reasons["score_components"]["economic_cost_bps"] == pytest.approx(26.0)


def test_feature_snapshot_does_not_treat_funding_receipt_as_negative_cost() -> None:
    snapshot = _build_feature_snapshot(
        score=0.5,
        atr_pct=0.01,
        effective_sent=0.0,
        cost_model={
            "spread_bps": 1.0,
            "expected_funding_bps": -25.0,
            "funding_cost_bps_for_approval": 0.0,
        },
        direction_agg={"trendiness": 0.1, "coherence": 0.8, "regime_confidence": 0.7},
        oi_sig={"oi_4h_chg_pct": 0.0},
        liq_tier="medium",
        beta_info={"correlation": 0.0},
    )

    assert snapshot["funding_cost_norm"] == pytest.approx(0.0)
    assert snapshot["funding_norm"] == pytest.approx(0.0)


def test_grid_density_reduces_level_count_when_adverse_funding_makes_costs_high() -> None:
    base = _params(
        "futures_grid",
        "linear",
        _range_feature(),
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=4.0,
        direction_bias="neutral",
        direction_bias_strength=0.0,
        atr_pct_for_grid=0.002,
        cost_model={"execution_cost_bps": 8.0, "expected_funding_bps": 0.0, "net_cost_bps": 8.0},
    )
    expensive = _params(
        "futures_grid",
        "linear",
        _range_feature(),
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=4.0,
        direction_bias="neutral",
        direction_bias_strength=0.0,
        atr_pct_for_grid=0.002,
        cost_model={"execution_cost_bps": 8.0, "expected_funding_bps": 18.0, "net_cost_bps": 26.0},
    )

    assert expensive["grid_density_economic_cost_bps"] == pytest.approx(26.0)
    assert expensive["grid_count"] < base["grid_count"]
    assert expensive["grid_spacing_cost_floor_bps"] == pytest.approx(26.0)
