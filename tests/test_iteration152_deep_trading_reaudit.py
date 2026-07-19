from __future__ import annotations

import sqlite3
from pathlib import Path

from app import db
from app.main import _execution_recommendation_freshness_blocks, _operator_decision_context_for_reco
from app.risk import compute_risk_status, normalize_risk_limits


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _rec(rec_id: str, ts: int, *, root: str | None = None, ttl_sec: int = 900) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "UNIFIED",
        "margin_mode": "cross",
        "score": 0.8,
        "confidence": 0.7,
        "expected_rr": 1.5,
        "risk_score": 0.2,
        "params": {
            "price_range_lower": 95.0,
            "price_range_upper": 105.0,
            "leverage": 5,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.0, "upper": 106.0},
                },
                "economics": {"liquidation_buffer_pct": 100.0},
            },
        },
        "reasons": {},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": ttl_sec,
        "model_version": "test",
        "features_ref_ts": ts,
        "publication_root_rec_id": root or rec_id,
        "is_outcome_label_root": root is None,
    }


def test_loss_cooldown_query_is_valid_and_blocks_after_realised_loss() -> None:
    conn = _conn()
    now = db.now_ts()
    db.insert_trade(
        conn,
        {
            "trade_id": "T-loss",
            "bot_id": "B1",
            "ts": now - 30,
            "symbol": "BTCUSDT",
            "pnl": -5.0,
            "fee": 0.2,
            "meta": {},
        },
    )

    status = compute_risk_status(conn, {"cooldown_after_loss_min": 30, "max_daily_dd_usdt": 1000})

    assert status.cooldown_active is True
    assert status.daily_dd >= 5.2


def test_risk_limits_default_to_operator_3x_5x_interval_and_fail_closed_bounds() -> None:
    limits = normalize_risk_limits({}, {})

    assert limits["min_leverage"] == 3
    assert limits["max_leverage"] == 5
    assert limits["max_leverage"] >= limits["min_leverage"]


def test_operator_context_exposes_publication_chain_age_not_only_row_age() -> None:
    conn = _conn()
    now = db.now_ts()
    root = _rec("R-root", now - 3600, ttl_sec=7200)
    child = _rec("R-child", now - 60, root="R-root", ttl_sec=7200)
    child["reasons"] = {"publication_dedupe": {"previous_rec_id": "R-root", "decision": "reuse_active"}}
    db.insert_recommendations(conn, [root, child])

    ctx = _operator_decision_context_for_reco(child, conn=conn)

    assert ctx["recommendation_row_age_sec"] < 300
    assert ctx["publication_chain_age_sec"] >= 3500
    assert ctx["publication_chain_update_count"] == 2
    assert ctx["publication_root_rec_id"] == "R-root"


def test_execution_blocks_recommendation_chain_that_outlives_ttl_even_if_child_row_is_fresh() -> None:
    conn = _conn()
    now = db.now_ts()
    root = _rec("R-root", now - 3600, ttl_sec=900)
    child = _rec("R-child", now - 60, root="R-root", ttl_sec=900)
    child["reasons"] = {"publication_dedupe": {"previous_rec_id": "R-root", "decision": "reuse_active"}}
    db.insert_recommendations(conn, [root, child])

    blocks = _execution_recommendation_freshness_blocks(conn, child, now_ts=now)
    codes = {block["code"] for block in blocks}

    assert "PUBLICATION_CHAIN_TOO_OLD" in codes
    assert "RECOMMENDATION_ROW_EXPIRED" not in codes


def test_ui_exposes_publication_chain_age_in_details_panel() -> None:
    js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "publication_chain_age_sec" in js
    assert "Возраст идеи с первого сигнала" in js
    assert "Возраст текущей строки" in js

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.main import _filter_operator_items_by_effective_status, _materialize_bot_from_rec
from app.recommender import _apply_recent_publication_dedupe


def test_expire_stale_recommendations_expires_fresh_child_when_root_chain_ttl_elapsed() -> None:
    conn = _conn()
    now = db.now_ts()
    root = _rec("R-exp-root", now - 3600, ttl_sec=900)
    child = _rec("R-exp-child", now - 60, root="R-exp-root", ttl_sec=900)
    child["status"] = "active"
    db.insert_recommendations(conn, [root, child])

    expired = db.expire_stale_recommendations(conn)

    assert expired == 2
    assert db.get_recommendation_by_id(conn, "R-exp-root")["status"] == "expired"
    assert db.get_recommendation_by_id(conn, "R-exp-child")["status"] == "expired"


def test_recent_publication_dedupe_resets_operator_chain_but_reuses_open_outcome_root() -> None:
    conn = _conn()
    now = db.now_ts()
    root = _rec("R-old-root", now - 3600, ttl_sec=900)
    root["status"] = "active"
    db.insert_recommendations(conn, [root])
    candidate = _rec("R-new-candidate", now, ttl_sec=900)
    candidate["status"] = "recommended"

    settings = SimpleNamespace(reco_republish_cooldown_sec=7200, outcome_horizon_fallback_sec=7200)
    _apply_recent_publication_dedupe(conn, [candidate], settings, now)

    assert candidate["status"] == "recommended"
    assert candidate["publication_root_rec_id"] == "R-new-candidate"
    assert candidate["outcome_root_rec_id"] == "R-old-root"
    assert candidate["is_outcome_label_root"] is False
    dedupe = candidate["reasons"]["publication_dedupe"]
    assert dedupe["decision"] == "publish_fresh_operator_root"
    assert dedupe["operator_chain_reset"] is True
    assert dedupe["open_position_lock"] is True


def test_operator_filter_excludes_effectively_expired_chain_from_active_list() -> None:
    items = [
        {"rec_id": "R-exp", "status": "expired", "effective_status": "expired"},
        {"rec_id": "R-live", "status": "active"},
    ]

    active = _filter_operator_items_by_effective_status(items, ["recommended", "active"], 10)

    assert [item["rec_id"] for item in active] == ["R-live"]


def test_materialize_rejects_fresh_child_when_publication_chain_expired_even_with_running_root_bot() -> None:
    conn = _conn()
    now = db.now_ts()
    root = _rec("R-running-root", now - 3600, ttl_sec=900)
    root["status"] = "executed"
    child = _rec("R-running-child", now - 60, root="R-running-root", ttl_sec=900)
    child["status"] = "active"
    db.insert_recommendations(conn, [root, child])
    db.insert_bot_instance(
        conn,
        {
            "bot_id": "B-running-root",
            "started_ts": now - 3500,
            "stopped_ts": None,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "mode": {"account_mode": "UNIFIED", "margin_mode": "cross", "direction": "long"},
            "params": root["params"],
            "state": {"created_from_rec_id": "R-running-root"},
            "status": "running",
            "origin_rec_id": "R-running-root",
            "publication_root_rec_id": "R-running-root",
        },
    )

    with pytest.raises(HTTPException, match="publication chain already expired"):
        _materialize_bot_from_rec(conn, "R-running-child", "tester")

    assert db.get_recommendation_by_id(conn, "R-running-child")["status"] == "expired"
    assert db.get_bot_by_origin_rec(conn, "R-running-child") is None
