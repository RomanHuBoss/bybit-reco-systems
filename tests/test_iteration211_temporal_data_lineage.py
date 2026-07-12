from __future__ import annotations

import math
from pathlib import Path

import pytest

from app import calibration, collector, db, features
from app.bybit_client import BybitPublicClient
from app.outcomes import compute_outcomes_once


def _recommendation(rec_id: str, ts: int) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.62,
        "confidence": 0.55,
        "expected_rr": 1.2,
        "risk_score": 0.2,
        "params": {
            "grid_count": 10,
            "grid_levels": 10,
            "grid_spacing_pct": 0.4,
            "price_range_lower": 95.0,
            "price_range_upper": 105.0,
            "cost_model": {"execution_cost_bps": 15.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "grid_count": 10,
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.0, "upper": 106.0},
                    "tp_per_leg": {"abs": 0.4},
                },
            },
        },
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": 0.7,
            },
            "risk_checks": {"passed": True, "blocks": []},
        },
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "bybit-taxonomy-v3-mean-reversion",
        "features_ref_ts": ts,
    }


def test_bybit_ticker_response_time_reaches_collector_freshness_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    response_time_ms = 1_700_000_000_123
    client = BybitPublicClient("https://example.invalid", max_retries=0)
    monkeypatch.setattr(
        client,
        "_get",
        lambda *_args, **_kwargs: {
            "retCode": 0,
            "time": response_time_ms,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "lastPrice": "100",
                        "bid1Price": "99.9",
                        "ask1Price": "100.1",
                        "volume24h": "1",
                        "turnover24h": "100",
                    }
                ]
            },
        },
    )
    try:
        rows = client.get_tickers("linear", "BTCUSDT")
    finally:
        client.close()

    assert rows[0]["time"] == response_time_ms
    assert collector._remote_ticker_ts(rows[0], fallback_ts=1_800_000_000) == 1_700_000_000


def test_kline_boundary_rejects_subsecond_or_timeframe_misaligned_start() -> None:
    aligned_ms = 1_700_000_040_000  # divisible by 60 seconds
    valid = [aligned_ms, "100", "101", "99", "100.5", "10"]
    assert collector._sanitize_ohlcv_row("linear", "BTCUSDT", 60, valid) is not None

    assert collector._sanitize_ohlcv_row(
        "linear", "BTCUSDT", 60, [aligned_ms + 123, "100", "101", "99", "100.5", "10"]
    ) is None
    assert collector._sanitize_ohlcv_row(
        "linear", "BTCUSDT", 60, [aligned_ms + 1_000, "100", "101", "99", "100.5", "10"]
    ) is None
    assert collector._sanitize_ohlcv_row("linear", "BTCUSDT", 60.5, valid) is None


def test_feature_layer_rejects_boolean_and_fractional_timestamps() -> None:
    start = 1_700_000_040
    rows = []
    for idx in range(30):
        close = 100.0 + idx * 0.1
        rows.append({
            "ts": start + idx * 60,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 10.0,
        })

    rows[0]["ts"] = True
    assert features.compute_features_from_ohlcv(rows, None) is None

    rows[0]["ts"] = start + 0.5
    assert features.compute_features_from_ohlcv(rows, None) is None


def test_outcome_worker_requires_exact_contiguous_horizon_and_exact_exit_candle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "outcome-gap.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_100_000
        db.insert_recommendations(conn, [_recommendation("R-gap", base_ts)])

        entry_ts = base_ts + 60
        horizon_sec = 12 * 3600
        exit_ts = entry_ts + horizon_sec
        missing_ts = entry_ts + 120 * 60
        rows = []
        for ts in range(entry_ts, exit_ts, 60):
            if ts == missing_ts:
                continue
            px = 100.0 + (0.4 if ((ts - entry_ts) // 60) % 2 else -0.4)
            rows.append({
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": ts,
                "open": px,
                "high": px + 0.5,
                "low": px - 0.5,
                "close": px,
                "volume": 100.0,
            })
        # No candle at the exact horizon. A later candle must not be used as the exit.
        rows.append({
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": exit_ts + 60,
            "open": 100.0,
            "high": 100.2,
            "low": 99.8,
            "close": 100.0,
            "volume": 100.0,
        })
        db.upsert_ohlcv(conn, rows)
        monkeypatch.setattr(db, "now_ts", lambda: exit_ts + 600)

        assert compute_outcomes_once(conn, max_to_process=10) == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM reco_outcomes").fetchone()["n"] == 0
    finally:
        conn.close()


def test_calibration_excludes_labels_not_demonstrably_available(monkeypatch: pytest.MonkeyPatch) -> None:
    as_of = 1_700_500_000
    monkeypatch.setattr(calibration.time, "time", lambda: as_of)
    rows = []
    for idx in range(80):
        rows.append({
            "score": 0.2 + (idx % 10) * 0.05,
            "success": idx % 2,
            "ts": as_of - 20_000 + idx * 60,
            # Half have no maturity proof; half claim maturity in the future.
            "label_available_ts": None if idx % 2 == 0 else as_of + 60 + idx,
        })

    model = calibration.fit_logreg(rows, min_samples=20, logreg_min_samples=100)
    assert model.fitted is False
    assert model.n_samples == 0


def test_outcome_join_decoder_does_not_crash_on_malformed_label_availability(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "malformed-outcome.db"))
    try:
        db.init_db(conn)
        rec = _recommendation("R-malformed-label", 1_700_000_000)
        db.insert_recommendations(conn, [rec])
        conn.execute(
            """INSERT INTO reco_outcomes(
                rec_id, ts, venue, symbol, bot_type, direction, horizon_sec,
                label_available_ts, entry_close, exit_close, ret, success
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec["rec_id"], rec["ts"], "linear", "BTCUSDT", "futures_grid", "neutral",
                43_200, "not-an-integer", 100.0, 101.0, 0.01, 1,
            ),
        )
        conn.commit()

        joined = db.get_outcomes_with_recs(conn)
        assert len(joined) == 1
        assert joined[0]["label_available_ts"] is None
        assert math.isfinite(joined[0]["ret"])
    finally:
        conn.close()


def test_temporal_outcome_contract_uses_new_label_version() -> None:
    from app import main as app_main

    assert app_main.OUTCOME_LABEL_VERSION == "grid_label_v4"
