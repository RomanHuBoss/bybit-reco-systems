from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_tp_hit, _resolve_effective_horizon, _signed_return
from app.recommender import _expected_rr


def test_boolean_label_horizon_does_not_prematurely_mature_grid_outcomes() -> None:
    runtime_horizon, used_fallback = _resolve_effective_horizon(
        "futures_grid",
        {"label_horizon_hours": True},
        900,
    )
    backfill_horizon = db._backfill_effective_horizon_sec(
        "futures_grid",
        {"label_horizon_hours": True},
        900,
    )

    assert runtime_horizon == 12 * 3600
    assert used_fallback is False
    assert backfill_horizon == 12 * 3600


def test_expected_rr_preserves_observed_zero_coherence() -> None:
    features = {
        "range_score": 0.5,
        "atr_pct": 0.02,
        "_atr_pct_1h": 0.02,
        "_direction_agg": {
            "trendiness": 0.2,
            "coherence": 0.0,
            "regime": "unknown",
        },
    }

    # Independent expected value:
    # stable_range = 0.20*0.5 + 0.80*(1-0.2) = 0.74
    # gross_capture = (0.55*0.74 + 0.15*0.0 - 0.20*0.2) * 0.02 = 0.00734
    # risk_proxy = 0.02 * 1.5 = 0.03
    expected = 0.00734 / 0.03

    assert _expected_rr("futures_grid", features, cost_model={}) == pytest.approx(expected)


def test_history_sort_patch_has_distinct_frontend_cache_key() -> None:
    html = (Path(__file__).resolve().parents[1] / "app" / "ui" / "static" / "index.html").read_text(encoding="utf-8")

    assert 'app.js?v=manual-ui-v47-outcome-liveness-minimum-table' in html


def test_outcome_direction_helpers_use_canonical_normalization() -> None:
    assert _signed_return(100.0, 90.0, " SHORT ") == pytest.approx(0.10)
    assert _signed_return(100.0, 90.0, "unknown") == pytest.approx(0.0)
    assert _grid_tp_hit(89.0, 101.0, 100.0, " SHORT ", 10.0) is True
    assert _grid_tp_hit(89.0, 111.0, 100.0, "unknown", 10.0) is False
