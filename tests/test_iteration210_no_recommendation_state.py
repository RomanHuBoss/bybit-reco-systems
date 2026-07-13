from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import db
from app.outcomes import compute_outcomes_once
from app.recommender import _mean_reversion_grid_blocks


def _seed_candles(conn, *, symbol: str, base_ts: int, count: int = 370) -> None:
    rows = []
    for idx in range(count):
        ts = base_ts + idx * 60
        px = 100.0 + ((idx % 8) - 4) * 0.18
        close = px + (0.08 if idx % 2 == 0 else -0.08)
        rows.append({
            "venue": "linear",
            "symbol": symbol,
            "tf_sec": 60,
            "ts": ts,
            "open": px,
            # Keep the cohort test path endpoint-only; intrabar ambiguity is
            # covered by iteration218 and should not suppress this sample.
            "high": max(px, close),
            "low": min(px, close),
            "close": close,
            "volume": 1_000.0,
        })
    db.upsert_ohlcv(conn, rows)


def _no_trade_recommendation(*, rec_id: str, symbol: str, ts: int, shadow_eligible: bool) -> dict:
    params = {
        "label_horizon_hours": 6,
        "grid_count": 8,
        "grid_levels": 8,
        "grid_spacing_pct": 0.5,
        "price_range_lower": 98.0,
        "price_range_upper": 102.0,
        "cost_model": {"execution_cost_bps": 10.0, "expected_funding_bps": 0.0},
        "trade_plan": {
            "grid_count": 8,
            "levels": {
                "range": {"lower": 98.0, "upper": 102.0},
                "kill_switch": {"lower": 97.0, "upper": 103.0},
                "tp_per_leg": {"abs": 0.5},
            },
        },
    }
    reasons = {
        "feature_snapshot": {
            "mean_reversion_score": 0.20,
            "mean_reversion_evidence_valid": 1.0,
        },
        "direction_agg": {
            "direction": "neutral",
            "raw_direction": "neutral",
            "regime": "range",
            "coherence": 0.70,
            "trendiness": 0.20,
        },
        "execution_constraints": {
            "raw_direction": "neutral",
            "executable_direction": "neutral",
            "futures_neutral": False,
        },
        "risk_checks": {"passed": True, "blocks": []},
        "decision_layers": {
            "execution_status": "not_actionable",
            "final_status": "no_trade",
        },
        "outcome_policy": {
            "eligible": shadow_eligible,
            "sample_role": "shadow_no_trade" if shadow_eligible else "excluded",
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
        "score": 0.40,
        "confidence": 0.50,
        "expected_rr": 0.50,
        "risk_score": 0.20,
        "params": params,
        "reasons": reasons,
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": "bybit-taxonomy-v4-independent-shadow-roots",
        "features_ref_ts": ts,
    }


def test_weak_mean_reversion_is_no_trade_not_hard_block() -> None:
    weak = _mean_reversion_grid_blocks({
        "mean_reversion_evidence_valid": True,
        "mean_reversion_score": 0.16,
        "mean_reversion_tf_count": 5,
    })
    missing = _mean_reversion_grid_blocks({
        "mean_reversion_evidence_valid": False,
        "mean_reversion_score": 0.0,
        "mean_reversion_tf_count": 1,
    })

    assert weak[0]["code"] == "MEAN_REVERSION_EDGE_UNCONFIRMED"
    assert weak[0]["decision"] == "no_trade"
    assert missing[0]["code"] == "MEAN_REVERSION_EVIDENCE_INSUFFICIENT"
    assert missing[0]["decision"] == "blocked"


def test_explicit_shadow_no_trade_outcome_matures_but_excluded_row_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "shadow-outcomes.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_300_000
        _seed_candles(conn, symbol="BTCUSDT", base_ts=base_ts)
        _seed_candles(conn, symbol="ETHUSDT", base_ts=base_ts)
        db.insert_recommendations(conn, [
            _no_trade_recommendation(
                rec_id="R-shadow-eligible",
                symbol="BTCUSDT",
                ts=base_ts,
                shadow_eligible=True,
            ),
            _no_trade_recommendation(
                rec_id="R-shadow-excluded",
                symbol="ETHUSDT",
                ts=base_ts,
                shadow_eligible=False,
            ),
        ])
        monkeypatch.setattr(db, "now_ts", lambda: base_ts + 24 * 3600)

        processed = compute_outcomes_once(conn, horizon_sec=30 * 60, max_to_process=10)

        assert processed == 1
        assert db.outcome_exists(conn, "R-shadow-eligible") is True
        assert db.outcome_exists(conn, "R-shadow-excluded") is False
        stats = db.get_outcomes_stats(conn)
        assert stats["summary"]["shadow_no_trade_total"] == 1
        assert stats["summary"]["actionable_total"] == 0
    finally:
        conn.close()


def test_operator_ui_does_not_call_proxy_outcomes_real_execution() -> None:
    js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Калибратор сам по себе не блокирует публикацию" in js
    assert "Proxy-исходы по кандидатам" in js
    assert "Что реально торговалось" not in js
