from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app import db
from app import outcomes
from app import recommender


def _seed_candles(conn, *, symbol: str, base_ts: int, count: int = 370) -> None:
    rows = []
    for idx in range(count):
        ts = base_ts + idx * 60
        px = 100.0 + ((idx % 8) - 4) * 0.18
        close = px + (0.08 if idx % 2 == 0 else -0.08)
        rows.append(
            {
                "venue": "linear",
                "symbol": symbol,
                "tf_sec": 60,
                "ts": ts,
                "open": px,
                "high": max(px, close),
                "low": min(px, close),
                "close": close,
                "volume": 1_000.0,
            }
        )
    db.upsert_ohlcv(conn, rows)


def _shadow_exploration_recommendation(*, rec_id: str, symbol: str, ts: int) -> dict:
    reasons = {
        "feature_snapshot": {
            "mean_reversion_score": 0.13,
            "mean_reversion_evidence_valid": 1,
        },
        "risk_checks": {"passed": True, "blocks": []},
        "decision_layers": {
            "final_status": "no_trade",
            "no_trade_reasons": [
                {
                    "code": "MEAN_REVERSION_EDGE_UNCONFIRMED",
                    "msg": "candidate screen is not passed",
                },
                {
                    "code": "PROXY_MONETARY_EXPECTANCY_UNPROVEN",
                    "msg": "current-policy expectancy is not proven",
                },
            ],
        },
        "outcome_policy": {
            "eligible": True,
            "policy_evaluation_eligible": False,
            "calibration_role": "shadow_exploration",
            "sample_role": "shadow_no_trade",
        },
    }
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": symbol,
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.13,
        "confidence": 0.50,
        "expected_rr": 0.10,
        "risk_score": 0.20,
        "params": {
            "label_horizon_hours": 6,
            "grid_count": 8,
            "grid_levels": 8,
            "grid_spacing_pct": 0.5,
            "price_range_lower": 98.0,
            "price_range_upper": 102.0,
            "cost_model": {
                "execution_cost_bps": 10.0,
                "expected_funding_bps": 0.0,
            },
            "trade_plan": {
                "grid_count": 8,
                "levels": {
                    "range": {"lower": 98.0, "upper": 102.0},
                    "kill_switch": {"lower": 97.0, "upper": 103.0},
                    "tp_per_leg": {"abs": 0.5},
                },
            },
        },
        "reasons": reasons,
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": recommender.RECOMMENDER_MODEL_VERSION + "+llm-review-v1",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def test_advisory_llm_does_not_starve_explicit_shadow_exploration_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "shadow-exploration.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_500_000
        _seed_candles(conn, symbol="BTCUSDT", base_ts=base_ts)
        db.insert_recommendations(
            conn,
            [
                _shadow_exploration_recommendation(
                    rec_id="R-shadow-exploration",
                    symbol="BTCUSDT",
                    ts=base_ts,
                )
            ],
        )
        monkeypatch.setattr(
            outcomes,
            "settings",
            replace(outcomes.settings, llm_reviewer_enabled=True),
        )
        monkeypatch.setattr(db, "now_ts", lambda: base_ts + 24 * 3600)

        processed = outcomes.compute_outcomes_once(conn, max_to_process=10)

        assert processed == 1
        assert db.outcome_exists(conn, "R-shadow-exploration") is True
    finally:
        conn.close()


def test_liveness_counts_matured_shadow_exploration_when_llm_is_enabled(
    tmp_path: Path,
) -> None:
    conn = db.connect(str(tmp_path / "shadow-exploration-liveness.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_500_000
        db.insert_recommendations(
            conn,
            [
                _shadow_exploration_recommendation(
                    rec_id="R-shadow-exploration-pending",
                    symbol="BTCUSDT",
                    ts=base_ts,
                )
            ],
        )

        status = db.get_outcome_worker_liveness(
            conn,
            now_ts_value=base_ts + 24 * 3600,
            require_llm_verdict=True,
        )

        assert status["matured_pending_total"] == 1
        assert status["unattempted_total"] == 1
        assert status["state"] == "stalled"
    finally:
        conn.close()


def test_shadow_exploration_outcome_remains_outside_current_policy_calibration() -> None:
    row = _shadow_exploration_recommendation(
        rec_id="R-shadow-exploration-lineage",
        symbol="BTCUSDT",
        ts=1_700_500_000,
    )
    row["reasons"] = dict(row["reasons"])

    lineage = recommender.calibration_lineage_diagnostics(
        [row],
        mean_reversion_min_score=0.25,
        retain_rows=False,
    )

    assert lineage["historical_total"] == 1
    assert lineage["feature_eligible_total"] == 1
    assert lineage["policy_eligible_total"] == 0
    assert lineage["dropped_candidate_policy"] == 1
