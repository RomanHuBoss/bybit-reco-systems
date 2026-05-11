from __future__ import annotations

import json
import time
from types import SimpleNamespace

from app import db
from app.recommender import _mark_llm_reviews_async, run_llm_review_sweep_once


def _settings(**overrides):
    base = dict(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="advisory",
        llm_reviewer_provider="ollama",
        llm_reviewer_url="http://127.0.0.1:11434",
        llm_reviewer_model="",  # reviewer unavailable for sweep tests
        llm_reviewer_timeout_sec=5,
        llm_reviewer_tf_secs=[900, 3600],
        llm_reviewer_candles_per_tf=16,
        llm_reviewer_max_candidates=4,
        llm_reviewer_max_workers=1,
        llm_reviewer_min_confidence=0.65,
        llm_reviewer_cadence_sec=5,
        llm_reviewer_pending_timeout_sec=60,
        llm_reviewer_ttl_sec=120,
        llm_reviewer_keep_alive="90s",
        reco_ttl_sec=1800,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _rec(*, rec_id: str, ts: int, status: str = "recommended", mode: str = "advisory", queued_ts: int | None = None):
    reasons = {}
    if queued_ts is not None:
        reasons["llm_review"] = {
            "status": "pending",
            "mode": mode,
            "gate_decision": "pending",
            "queued_ts": queued_ts,
            "publish_target_status": "recommended",
        }
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "one_way",
        "margin_mode": "isolated",
        "score": 0.5,
        "confidence": 0.8,
        "expected_rr": 1.4,
        "risk_score": 0.2,
        "params": {"grid_levels": 8},
        "reasons": reasons,
        "blocks": [],
        "status": status,
        "ttl_sec": 1800,
        "model_version": "test",
        "features_ref_ts": ts,
    }


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_db(conn)
    return conn


def _load_row(conn, rec_id: str):
    row = conn.execute("SELECT status, reasons_json FROM recommendations WHERE rec_id=?", (rec_id,)).fetchone()
    assert row is not None
    return row["status"], json.loads(row["reasons_json"])


def test_advisory_async_llm_review_no_longer_holds_actionable_recommendation_in_pending(tmp_path):
    conn = _conn(tmp_path)
    rec = _rec(rec_id="R-advisory-nonblocking", ts=int(time.time()), status="recommended")

    stats = _mark_llm_reviews_async(conn, [rec], _settings(llm_reviewer_mode="advisory"), reviewer=None)

    assert stats["queued"] == 1
    assert rec["status"] == "recommended"
    assert rec["reasons"]["llm_review"]["status"] == "pending"
    assert rec["reasons"]["llm_review"]["hold_policy"] == "non_blocking_advisory"


def test_gate_async_llm_review_still_holds_launch_until_verdict(tmp_path):
    conn = _conn(tmp_path)
    rec = _rec(rec_id="R-gate-hold", ts=int(time.time()), status="recommended")

    stats = _mark_llm_reviews_async(conn, [rec], _settings(llm_reviewer_mode="gate"), reviewer=None)

    assert stats["queued"] == 1
    assert rec["status"] == "pending"
    assert rec["reasons"]["llm_review"]["hold_policy"] == "gate_hold"
    assert rec["reasons"]["llm_review"]["publish_target_status"] == "recommended"


def test_stale_gate_pending_review_fails_closed_to_no_trade_when_reviewer_unavailable(tmp_path):
    conn = _conn(tmp_path)
    now = int(time.time())
    db.insert_recommendations(
        conn,
        [_rec(rec_id="R-gate-timeout", ts=now - 120, status="pending", mode="gate", queued_ts=now - 120)],
    )

    stats = run_llm_review_sweep_once(conn, _settings(llm_reviewer_mode="gate", llm_reviewer_model=""))

    status, reasons = _load_row(conn, "R-gate-timeout")
    assert stats["stale_failed_closed"] == 1
    assert status == "no_trade"
    assert reasons["llm_review"]["status"] == "error"
    assert reasons["llm_review"]["gate_decision"] == "fail_closed"


def test_stale_advisory_pending_review_is_marked_skipped_without_blocking_engine_status(tmp_path):
    conn = _conn(tmp_path)
    now = int(time.time())
    db.insert_recommendations(
        conn,
        [_rec(rec_id="R-advisory-timeout", ts=now - 120, status="recommended", mode="advisory", queued_ts=now - 120)],
    )

    stats = run_llm_review_sweep_once(conn, _settings(llm_reviewer_mode="advisory", llm_reviewer_model=""))

    status, reasons = _load_row(conn, "R-advisory-timeout")
    assert stats["stale_restored"] == 1
    assert status == "recommended"
    assert reasons["llm_review"]["status"] == "skipped"
    assert reasons["llm_review"]["gate_decision"] == "skipped"
