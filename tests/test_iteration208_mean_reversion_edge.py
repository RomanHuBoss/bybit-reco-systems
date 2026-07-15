from __future__ import annotations

import math
import random

import pytest

from app.direction import TF_WEIGHTS, aggregate_direction, mean_reversion_diagnostics, vote_for_tf
from app.recommender import _stable_range_score


def _closes_from_returns(returns: list[float], start: float = 100.0) -> list[float]:
    closes = [start]
    for value in returns:
        closes.append(closes[-1] * math.exp(value))
    return closes


def _ohlc(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    highs = [value * 1.0004 for value in closes]
    lows = [value * 0.9996 for value in closes]
    return closes, highs, lows


def _iid_returns(n: int = 240) -> list[float]:
    rng = random.Random(208)
    raw = [rng.choice((-1.0, 1.0)) * rng.uniform(0.0005, 0.0015) for _ in range(n)]
    mean = sum(raw) / len(raw)
    return [value - mean for value in raw]


def _alternating_returns(n: int = 240) -> list[float]:
    return [((1.0 if index % 2 == 0 else -1.0) * (0.0010 + (index % 7) * 0.00003)) for index in range(n)]


def test_vote_for_tf_distinguishes_absence_of_trend_from_mean_reversion() -> None:
    iid = vote_for_tf(*_ohlc(_closes_from_returns(_iid_returns())))
    oscillating = vote_for_tf(*_ohlc(_closes_from_returns(_alternating_returns())))

    assert iid["mean_reversion_evidence_valid"] is True
    assert oscillating["mean_reversion_evidence_valid"] is True
    assert iid["mean_reversion_score"] < 0.45
    assert oscillating["mean_reversion_score"] > 0.75
    assert oscillating["mean_reversion_score"] > iid["mean_reversion_score"] + 0.35


def test_stable_range_score_does_not_treat_low_trend_as_grid_edge() -> None:
    weak = {
        "trendiness": 0.05,
        "coherence": 0.70,
        "regime": "range",
        "mean_reversion_score": 0.05,
        "mean_reversion_evidence_valid": True,
        "mean_reversion_tf_count": 5,
    }
    strong = dict(weak, mean_reversion_score=0.90)
    f = {"range_score": 0.95, "trend_strength": 0.05}

    weak_score, weak_meta = _stable_range_score(f, weak)
    strong_score, strong_meta = _stable_range_score(f, strong)

    assert weak_meta["mean_reversion_evidence_valid"] is True
    assert weak_score < 0.55
    assert strong_score > 0.80
    assert strong_score > weak_score + 0.30


def test_multi_tf_aggregate_exposes_independent_range_edge_evidence() -> None:
    iid_vote = vote_for_tf(*_ohlc(_closes_from_returns(_iid_returns())))
    tf_map = {tf: dict(iid_vote) for tf in TF_WEIGHTS}
    aggregate = aggregate_direction(tf_map)

    assert aggregate["mean_reversion_evidence_valid"] is True
    assert aggregate["mean_reversion_tf_count"] == len(TF_WEIGHTS)
    assert aggregate["mean_reversion_score"] < 0.45


def test_grid_publication_gate_blocks_random_walk_like_range() -> None:
    import app.recommender as recommender

    gate = getattr(recommender, "_mean_reversion_grid_blocks", None)
    assert callable(gate), "production grid gate must independently validate mean-reversion evidence"
    weak = gate({
        "mean_reversion_evidence_valid": True,
        "mean_reversion_score": 0.12,
        "mean_reversion_tf_count": 5,
    })
    strong = gate({
        "mean_reversion_evidence_valid": True,
        "mean_reversion_score": 0.82,
        "mean_reversion_tf_count": 5,
    })
    assert [item["code"] for item in weak] == ["MEAN_REVERSION_EDGE_UNCONFIRMED"]
    assert strong == []


def test_legacy_expected_rr_is_hidden_from_operator_ui_and_replaced_by_decision_metrics() -> None:
    from pathlib import Path

    html = Path("app/ui/static/index.html").read_text(encoding="utf-8")
    js = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    assert "Прокси capture/risk" not in html
    assert "RR плана" in html
    assert "Доходность по наблюдениям" in html
    assert 'label: "RR плана"' in js
    assert 'label: "Доходность по наблюдениям"' in js
    assert "heuristic_capture_score" in js or "heuristic_capture_score" in Path("app/recommender.py").read_text(encoding="utf-8")


def test_new_model_identity_and_calibrators_do_not_reuse_legacy_range_semantics() -> None:
    import app.calibration as calibration
    import app.recommender as recommender

    assert recommender.RECOMMENDER_MODEL_VERSION == "bybit-taxonomy-v8-policy-conditioned-censor-aware"
    assert calibration.GLOBAL_LOGREG_KEY.endswith("_v19")
    assert calibration.BOT_CALIB_KEYS["futures_grid"].endswith("_v19")
    assert recommender.DIRECTION_CALIBRATION_KEY == "platt_direction_v14"


def test_calibration_rows_require_current_model_and_mean_reversion_snapshot() -> None:
    import app.recommender as recommender

    current = {
        "model_version": recommender.RECOMMENDER_MODEL_VERSION,
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1.0,
                "mean_reversion_score": 0.72,
            }
        },
    }
    legacy_model = {
        **current,
        "model_version": "bybit-taxonomy-v2",
    }
    legacy_snapshot = {
        "model_version": recommender.RECOMMENDER_MODEL_VERSION,
        "reasons": {"feature_snapshot": {"range_score": 0.95}},
    }

    assert recommender._current_range_edge_calibration_rows([legacy_model, legacy_snapshot, current]) == [current]


def test_mean_reversion_threshold_rejects_iid_paths_and_detects_material_antipersistence() -> None:
    def ar_returns(seed: int, phi: float, n: int = 160) -> list[float]:
        rng = random.Random(seed)
        value = 0.0
        out: list[float] = []
        for _ in range(n):
            value = phi * value + rng.gauss(0.0, 0.001)
            out.append(value)
        return out

    iid_hits = 0
    anti_hits = 0
    for seed in range(200):
        iid_score = mean_reversion_diagnostics(_closes_from_returns(ar_returns(seed, 0.0)))["mean_reversion_score"]
        anti_score = mean_reversion_diagnostics(_closes_from_returns(ar_returns(seed, -0.35)))["mean_reversion_score"]
        iid_hits += int(iid_score >= 0.55)
        anti_hits += int(anti_score >= 0.55)

    assert iid_hits <= 1
    assert anti_hits >= 150
