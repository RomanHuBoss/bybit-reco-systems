from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.outcomes import compute_outcomes_once


def _legacy_recommendation_sql_payload(*, rec_id: str, ts: int, status: str, reasons: dict) -> tuple:
    return (
        rec_id,
        ts,
        "linear",
        "BTCUSDT",
        "futures_grid",
        "neutral",
        "one_way",
        "isolated",
        0.25,
        0.65,
        0.30,
        0.20,
        json.dumps({"grid_levels": 5, "grid_spacing_pct": 1.0}, ensure_ascii=False),
        json.dumps(reasons, ensure_ascii=False),
        json.dumps([], ensure_ascii=False),
        status,
        1800,
        "test",
        ts,
        None,
        1,
    )


def test_init_db_backfills_publication_lineage_for_legacy_rows(tmp_path: Path):
    conn = db.connect(str(tmp_path / "legacy_lineage.db"))
    db.init_db(conn)
    try:
        ts_root = int(time.time()) - 7200
        ts_active = ts_root + 60
        conn.executemany(
            """INSERT INTO recommendations(
                   rec_id, ts, venue, symbol, bot_type, direction, account_mode, margin_mode,
                   score, confidence, expected_rr, risk_score,
                   params_json, reasons_json, blocks_json, status, ttl_sec, model_version,
                   features_ref_ts, publication_root_rec_id, is_outcome_label_root
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                _legacy_recommendation_sql_payload(rec_id="R-root", ts=ts_root, status="recommended", reasons={}),
                _legacy_recommendation_sql_payload(
                    rec_id="R-active",
                    ts=ts_active,
                    status="active",
                    reasons={
                        "publication_dedupe": {
                            "previous_rec_id": "R-root",
                            "decision": "reuse_active",
                            "active_reuse": True,
                            "material_upgrade": False,
                        }
                    },
                ),
            ],
        )
        conn.commit()

        db.init_db(conn)

        root = db.get_recommendation_by_id(conn, "R-root")
        active = db.get_recommendation_by_id(conn, "R-active")
        assert root is not None and active is not None
        assert root["publication_root_rec_id"] == "R-root"
        assert root["is_outcome_label_root"] is True
        assert active["publication_root_rec_id"] == "R-root"
        assert active["is_outcome_label_root"] is False
    finally:
        conn.close()


def test_compute_outcomes_only_labels_publication_root(tmp_path: Path):
    conn = db.connect(str(tmp_path / "lineage_outcomes.db"))
    db.init_db(conn)
    try:
        now = db.now_ts()
        ts_root = now - 15 * 3600
        ts_active = ts_root + 300
        db.insert_recommendations(
            conn,
            [
                {
                    "rec_id": "R-root",
                    "ts": ts_root,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "futures_grid",
                    "direction": "neutral",
                    "account_mode": "one_way",
                    "margin_mode": "isolated",
                    "score": 0.20,
                    "confidence": 0.62,
                    "expected_rr": 0.25,
                    "risk_score": 0.20,
                    "params": {"grid_levels": 5, "grid_spacing_pct": 1.0},
                    "reasons": {},
                    "blocks": [],
                    "status": "recommended",
                    "ttl_sec": 1800,
                    "model_version": "test",
                    "features_ref_ts": ts_root,
                    "publication_root_rec_id": "R-root",
                    "is_outcome_label_root": True,
                },
                {
                    "rec_id": "R-active",
                    "ts": ts_active,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "futures_grid",
                    "direction": "neutral",
                    "account_mode": "one_way",
                    "margin_mode": "isolated",
                    "score": 0.21,
                    "confidence": 0.63,
                    "expected_rr": 0.26,
                    "risk_score": 0.20,
                    "params": {"grid_levels": 5, "grid_spacing_pct": 1.0},
                    "reasons": {
                        "publication_dedupe": {
                            "previous_rec_id": "R-root",
                            "decision": "reuse_active",
                            "active_reuse": True,
                            "material_upgrade": False,
                        }
                    },
                    "blocks": [],
                    "status": "active",
                    "ttl_sec": 1800,
                    "model_version": "test",
                    "features_ref_ts": ts_active,
                    "publication_root_rec_id": "R-root",
                    "is_outcome_label_root": False,
                },
            ],
        )

        entry_ts = ts_root + 60
        exit_ts = entry_ts + 12 * 3600
        db.upsert_ohlcv(
            conn,
            [
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "tf_sec": 60,
                    "ts": entry_ts,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10.0,
                },
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "tf_sec": 60,
                    "ts": exit_ts,
                    "open": 100.2,
                    "high": 100.8,
                    "low": 99.7,
                    "close": 100.1,
                    "volume": 11.0,
                },
            ],
        )

        done = compute_outcomes_once(conn, horizon_sec=30 * 60, max_to_process=10)

        assert done == 1
        assert db.outcome_exists(conn, "R-root") is True
        assert db.outcome_exists(conn, "R-active") is False

        stats = db.get_outcomes_stats(conn)
        assert stats["summary"]["total"] == 1
        assert stats["summary"]["raw_total"] == 1
    finally:
        conn.close()


@pytest.fixture()
def client_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "status_lineage.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app)
    try:
        yield client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


def test_status_and_outcome_stats_ignore_historical_active_duplicates(client_and_conn):
    client, conn = client_and_conn

    now = db.now_ts() - 8 * 3600
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-root",
                "ts": now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.20,
                "confidence": 0.62,
                "expected_rr": 0.25,
                "risk_score": 0.20,
                "params": {"grid_levels": 5, "grid_spacing_pct": 1.0},
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": now,
                "publication_root_rec_id": "R-root",
                "is_outcome_label_root": True,
            },
            {
                "rec_id": "R-active",
                "ts": now + 60,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.21,
                "confidence": 0.63,
                "expected_rr": 0.26,
                "risk_score": 0.20,
                "params": {"grid_levels": 5, "grid_spacing_pct": 1.0},
                "reasons": {
                    "publication_dedupe": {
                        "previous_rec_id": "R-root",
                        "decision": "reuse_active",
                        "active_reuse": True,
                        "material_upgrade": False,
                    }
                },
                "blocks": [],
                "status": "active",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": now + 60,
                "publication_root_rec_id": "R-root",
                "is_outcome_label_root": False,
            },
        ],
    )
    db.insert_outcome(
        conn,
        {
            "rec_id": "R-root",
            "ts": now,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "neutral",
            "horizon_sec": 6 * 3600,
            "entry_close": 100.0,
            "exit_close": 100.4,
            "ret": 0.01,
            "success": 1,
        },
    )
    db.insert_outcome(
        conn,
        {
            "rec_id": "R-active",
            "ts": now + 60,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "neutral",
            "horizon_sec": 6 * 3600,
            "entry_close": 100.0,
            "exit_close": 100.3,
            "ret": 0.009,
            "success": 1,
        },
    )

    stats_resp = client.get("/api/v1/outcomes/stats")
    assert stats_resp.status_code == 200
    stats_body = stats_resp.json()
    assert stats_body["summary"]["total"] == 1
    assert stats_body["summary"]["raw_total"] == 2
    assert stats_body["summary"]["deduped_duplicates"] == 1

    status_resp = client.get("/api/v1/status")
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["outcome_count"] == 1
    assert status_body["bot_calibrators"]["futures_grid"]["outcomes_total"] == 1
