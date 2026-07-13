from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app import main as app_main
from app.outcomes import compute_outcomes_once

CURRENT_MODEL_VERSION = "bybit-taxonomy-v6-historical-proxy-shadow-roots"


def _raw_recommendation() -> dict:
    sizing = {
        "basis": "minimum_viable_operator_default",
        "qty_per_order": 0.26,
        "order_notional_usdt": 26.0,
        "grid_count": 2,
        "exchange_filter_assumption": {"mode": "provisional_target_notional_until_bybit_preflight"},
    }
    economics = dict(sizing)
    return {
        "rec_id": "R-exchange-normalization",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "grid_count": 2,
            "grid_levels": 2,
            "grid_geometry_model": "bybit_arithmetic_range_width_div_grid_count",
            "price_ref": 100.0,
            "price_range_lower": 99.1,
            "price_range_upper": 100.9,
            "actual_grid_step_abs": 0.9,
            "actual_grid_spacing_pct": 0.9,
            "grid_spacing_pct": 0.9,
            "leverage": 1,
            "sizing": dict(sizing),
            "economics": dict(economics),
            "trade_plan": {
                "grid_count": 2,
                "reference_price": 100.0,
                "sizing": dict(sizing),
                "economics": dict(economics),
                "levels": {
                    "range": {"lower": 99.1, "upper": 100.9},
                    "kill_switch": {"lower": 98.6, "upper": 101.4},
                    "grid_step": {"step_abs": 0.9, "step_pct": 0.9},
                    "tp_per_leg": {"abs": 0.9, "pct": 0.9},
                },
            },
        },
    }


def _meta() -> dict:
    return {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "delivery_time": "0",
        "is_pre_listing": False,
        "tick_size": "0.5",
        "min_price": "0.5",
        "max_price": "1000000",
        "qty_step": "0.1",
        "min_order_qty": "0.1",
        "max_order_qty": "1000",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def test_explicit_preflight_snapping_remains_available_without_publication_coupling() -> None:
    rec = app_main._snap_reco_payload_to_bybit_meta(_raw_recommendation(), _meta())
    params = rec["params"]
    levels = params["trade_plan"]["levels"]
    assert levels["range"] == {"lower": 99.0, "upper": 101.0}
    assert levels["grid_step"]["step_abs"] == pytest.approx(1.0)
    assert params["sizing"]["qty_per_order"] == pytest.approx(0.2)
    assert "exchange_execution_snapshot" not in params


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


def _current_model_rec(base: int) -> dict:
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
        "rec_id": "R-unverified-current-model",
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
                "reason": "model_thesis_or_launch_gate",
            },
        },
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 3600,
        "model_version": CURRENT_MODEL_VERSION,
        "features_ref_ts": base,
        "publication_root_rec_id": "R-unverified-current-model",
        "is_outcome_label_root": True,
    }


def test_current_model_outcome_accepts_historical_geometry_without_runtime_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "unverified-geometry.db"))
    try:
        db.init_db(conn)
        now = 1_740_400_000
        base = (now // 60 - 800) * 60
        _seed_flat_window(conn, base, 370)
        db.insert_recommendations(conn, [_current_model_rec(base)])
        monkeypatch.setattr(db, "now_ts", lambda: now)

        processed = compute_outcomes_once(conn, max_to_process=10)

        assert processed == 1
        row = conn.execute(
            "SELECT success, ret FROM reco_outcomes WHERE rec_id=?",
            ("R-unverified-current-model",),
        ).fetchone()
        assert row is not None
        assert row["success"] == 0
        assert row["ret"] == pytest.approx(0.0)
    finally:
        conn.close()


def test_background_recommender_has_no_runtime_exchange_normalizer_dependency() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    thread_block = source[source.index("def _reco_thread"):source.index("def _llm_reviewer_thread")]
    assert "exchange_normalizer=" not in thread_block
    assert "_normalize_recommendation_for_exchange_evidence" not in thread_block
    assert "recommendation-time instrument metadata prefetch" not in thread_block
