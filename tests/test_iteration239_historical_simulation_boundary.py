from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app import db
from app import main as app_main
from app import recommender
from app.outcomes import compute_outcomes_once


def _seed_flat_window(conn, base: int, minutes: int) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base + index * 60,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1_000.0,
            }
            for index in range(minutes)
        ],
    )


def _historical_proxy_rec(base: int) -> dict:
    cost = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "market_round_trip_cost_bps": 0.0,
        "expected_funding_bps": 0.0,
        "expected_funding_events": 0,
    }
    params = {
        "label_horizon_hours": 6,
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "cost_model": dict(cost),
        "trade_plan": {
            "grid_count": 2,
            "reference_price": 100.0,
            "cost_model": dict(cost),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 102.0},
                "grid_step": {"step_abs": 1.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }
    return {
        "rec_id": "R-historical-proxy-no-runtime-meta",
        "ts": base + 10,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.8,
        "confidence": 0.8,
        "expected_rr": 1.2,
        "risk_score": 0.2,
        "params": params,
        "reasons": {
            "risk_checks": {"passed": True, "blocks": []},
            "outcome_policy": {
                "eligible": True,
                "sample_role": "shadow_no_trade",
                "reason": "historical_simulation",
            },
        },
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 3600,
        "model_version": recommender.RECOMMENDER_MODEL_VERSION,
        "features_ref_ts": base,
        "publication_root_rec_id": "R-historical-proxy-no-runtime-meta",
        "is_outcome_label_root": True,
    }


def test_historical_proxy_outcome_does_not_require_runtime_exchange_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(str(tmp_path / "historical-proxy.db"))
    try:
        db.init_db(conn)
        now = 1_740_400_000
        base = (now // 60 - 800) * 60
        _seed_flat_window(conn, base, 370)
        db.insert_recommendations(conn, [_historical_proxy_rec(base)])
        monkeypatch.setattr(db, "now_ts", lambda: now)

        processed = compute_outcomes_once(conn, max_to_process=10)

        assert processed == 1
        row = conn.execute(
            "SELECT success, ret FROM reco_outcomes WHERE rec_id=?",
            ("R-historical-proxy-no-runtime-meta",),
        ).fetchone()
        assert row is not None
        assert row["success"] == 0
        assert row["ret"] == pytest.approx(0.0)
        skipped = conn.execute(
            "SELECT COUNT(*) AS c FROM decision_log WHERE rec_id=? AND action='OUTCOME_SKIP_UNVERIFIED_EXCHANGE_GEOMETRY'",
            ("R-historical-proxy-no-runtime-meta",),
        ).fetchone()
        assert skipped["c"] == 0
    finally:
        conn.close()


def test_recommender_runtime_has_no_exchange_execution_normalizer_dependency() -> None:
    signature = inspect.signature(recommender.run_recommender_once)
    assert "exchange_normalizer" not in signature.parameters

    source = Path("app/main.py").read_text(encoding="utf-8")
    thread_block = source[source.index("def _reco_thread"):source.index("def _llm_reviewer_thread")]
    assert "exchange_normalizer=" not in thread_block
    assert "_normalize_recommendation_for_exchange_evidence" not in thread_block
    assert "recommendation-time instrument metadata prefetch" not in thread_block


def test_recommendation_metadata_declares_historical_proxy_boundary() -> None:
    rec = {
        "status": "recommended",
        "params": {},
        "reasons": {},
    }

    recommender._sync_recommendation_metadata(rec)

    scope = rec["reasons"]["simulation_scope"]
    assert scope["mode"] == "historical_proxy_only"
    assert scope["runtime_order_submission"] is False
    assert scope["runtime_execution_validation"] == "not_performed"
    assert scope["exchange_fill_attestation"] == "not_available"
    assert rec["status"] == "recommended"
