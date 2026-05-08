from __future__ import annotations

import math

from app import db
from app.features import compute_features_from_ohlcv, liquidity_tier
from app.outcomes import compute_outcomes_once


def _make_recommendation(rec_id: str, ts: int, params: dict) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.41,
        "confidence": 0.73,
        "expected_rr": 1.5,
        "risk_score": 0.17,
        "params": params,
        "reasons": {
            "feature_snapshot": {"atr_pct": 0.01, "range_score": 0.76},
            "direction_agg": {"direction": "neutral", "raw_direction": "neutral", "regime": "range", "coherence": 0.7, "trendiness": 0.2},
            "execution_constraints": {"raw_direction": "neutral", "executable_direction": "neutral", "futures_neutral": False},
            "decision_layers": {"final_status": "recommended"},
            "symbol_sentiment": {"effective": 0.1, "global": 0.1},
            "market_shock": {"state": "normal"},
        },
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "test",
        "features_ref_ts": ts,
    }


def test_compute_outcomes_once_survives_nonfinite_grid_params(tmp_path, monkeypatch):
    db_path = tmp_path / "outcomes_bad_params.db"
    conn = db.connect(str(db_path))
    db.init_db(conn)

    base_ts = 1_700_000_000
    ohlcv_rows = []
    for i in range(1000):
        ts = base_ts + i * 60
        px = 100.0 + ((i % 6) - 3) * 0.4
        ohlcv_rows.append({
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts,
            "open": px,
            "high": px + 0.8,
            "low": px - 0.8,
            "close": px + (0.2 if i % 2 == 0 else -0.2),
            "volume": 1000.0 + i,
        })
    db.upsert_ohlcv(conn, ohlcv_rows)

    rec = _make_recommendation(
        "R-bad-grid-params",
        base_ts,
        {
            "grid_levels": "bad",
            "grid_spacing_pct": "bad",
            "price_range_lower": 98.0,
            "price_range_upper": 102.0,
            "trade_plan": {
                "levels": {
                    "range": {"lower": 98.0, "upper": 102.0},
                    "kill_switch": {"lower": 97.5, "upper": 102.5},
                }
            },
        },
    )
    db.insert_recommendations(conn, [rec])

    monkeypatch.setattr(db, "now_ts", lambda: base_ts + 24 * 3600)

    processed = compute_outcomes_once(conn, horizon_sec=30 * 60, max_to_process=10)
    row = conn.execute("SELECT success, ret, horizon_sec FROM reco_outcomes WHERE rec_id=?", ("R-bad-grid-params",)).fetchone()

    assert processed == 1
    assert row is not None
    assert row["horizon_sec"] == 12 * 3600
    assert row["success"] in (0, 1)
    assert math.isfinite(float(row["ret"]))

    conn.close()



def test_compute_features_discards_logically_impossible_last_bar():
    rows = []
    for i in range(40):
        rows.append({
            "ts": i + 1,
            "close": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "volume": 10.0 + i,
        })

    # impossible candle: close is outside [low, high], but the bar still looked
    # superficially finite and therefore previously contaminated feature math.
    rows[-1]["close"] = 139.0
    rows[-1]["high"] = 138.5
    rows[-1]["low"] = 138.0

    feat = compute_features_from_ohlcv(rows, None)

    assert feat is not None
    assert feat["ts_last"] == 39
    assert feat["price"] == 138.0
    assert math.isfinite(float(feat["atr"]))
    assert math.isfinite(float(feat["rv"]))



def test_liquidity_tier_treats_poisoned_turnover_as_unknown():
    assert liquidity_tier(float("nan")) == "unknown"
    assert liquidity_tier(float("inf")) == "unknown"
    assert liquidity_tier(-1.0) == "unknown"
    assert liquidity_tier("bad") == "unknown"
