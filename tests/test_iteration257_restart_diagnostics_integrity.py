from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

from app import db

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"


def _recommendation(rec_id: str, ts: int, status: str, *, is_root: bool) -> dict:
    reasons = {
        "risk_checks": {
            "passed": status != "blocked",
            "blocks": ([{"code": "FUNDING_EXTREME", "msg": "funding"}] if status == "blocked" else []),
        },
        "decision_layers": {
            "no_trade_reasons": ([
                {"code": "PROXY_MONETARY_EXPECTANCY_UNPROVEN", "msg": "insufficient evidence"},
                {"code": "CALIBRATED_CONFIDENCE_UNAVAILABLE", "msg": "calibrator absent"},
            ] if status == "no_trade" else []),
        },
        "outcome_policy": {
            "eligible": status == "no_trade",
            "policy_evaluation_eligible": status == "no_trade",
            "sample_role": "shadow_no_trade" if status == "no_trade" else "excluded",
        },
    }
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": {"R-1": "BTCUSDT", "R-2": "ETHUSDT"}.get(rec_id, "SOLUSDT"),
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.2,
        "confidence": 0.6,
        "expected_rr": 1.1,
        "risk_score": 0.2,
        "params": {"trade_plan": {"reference_price": 100.0}},
        "reasons": reasons,
        "blocks": [],
        "status": status,
        "ttl_sec": 1800,
        "model_version": "bybit-taxonomy-v8-policy-conditioned-censor-aware",
        "features_ref_ts": ts,
        "publication_root_rec_id": "R-root" if not is_root else rec_id,
        "is_outcome_label_root": is_root,
    }


def _import_app(monkeypatch: pytest.MonkeyPatch, db_path: Path):
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT,ETHUSDT,SOLUSDT")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "0")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def test_latest_publication_readiness_includes_non_outcome_root_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "snapshot.db"
    app_main = _import_app(monkeypatch, db_path)
    now = int(time.time())
    conn = db.connect(str(db_path))
    try:
        db.init_db(conn)
        db.insert_recommendations(conn, [
            _recommendation("R-1", now, "no_trade", is_root=False),
            _recommendation("R-2", now, "no_trade", is_root=False),
            _recommendation("R-3", now, "blocked", is_root=True),
        ])
        snapshot = app_main._latest_recommendation_readiness(conn)
    finally:
        conn.close()
        sys.modules.pop("app.main", None)

    assert snapshot["latest_snapshot_total"] == 3
    assert snapshot["status_counts"]["no_trade"] == 2
    assert snapshot["status_counts"]["blocked"] == 1
    assert snapshot["dominant_state"] == "calibration_evidence_pending"
    assert snapshot["no_trade_reason_counts"][0]["count"] == 2


def test_status_stays_starting_until_current_process_has_own_cycle_and_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "restart.db"
    app_main = _import_app(monkeypatch, db_path)
    now = int(time.time())
    app_main.PROCESS_STARTED_TS = now
    app_main.RUNTIME_OWNER = "host:222"
    conn = db.connect(str(db_path))
    try:
        db.init_db(conn)
        db.insert_recommendations(conn, [_recommendation("R-3", now - 60, "blocked", is_root=True)])
        db.set_app_config_json(conn, "collector_last_cycle", {
            "started_ts": now - 30,
            "owner": "host:111",
            "duration_ms": 10,
        })
        db.set_app_config_json(conn, db.OUTCOME_WORKER_CYCLE_APP_KEY, {
            "state": "completed",
            "cycle_started_ts": now - 20,
            "cycle_finished_ts": now - 20,
            "updated_ts": now - 20,
            "rows_selected": 0,
            "rows_examined": 0,
            "rows_labeled": 0,
            "rows_waiting": 0,
            "rows_censored": 0,
            "rows_failed": 0,
            "matured_pending_before": 0,
            "matured_pending_after": 0,
        })
        for name in ("collector", "backfill", "futures_meta", "sentiment", "reco", "outcomes"):
            db.set_app_config_json(conn, f"runtime_thread_state:{name}", {
                "name": name,
                "state": "running",
                "updated_ts": now,
                "owner": "host:222",
            })
        status = app_main.api_status()
    finally:
        conn.close()
        sys.modules.pop("app.main", None)

    assert status["operator_readiness"]["state"] == "starting"
    assert status["operator_readiness"]["runtime_healthy"] is True
    codes = {item["code"] for item in status["operator_readiness"]["explanations"]}
    assert "CURRENT_PROCESS_CYCLE_PENDING" in codes
    assert status["runtime_provenance"]["collector_cycle_current_process"] is False
    assert status["runtime_provenance"]["publication_current_process"] is False
    assert status["runtime_provenance"]["collector_owner_matches_runtime"] is False


def test_database_continuity_id_is_stable_and_counts_are_visible(tmp_path: Path) -> None:
    db_path = tmp_path / "continuity.db"
    conn = db.connect(str(db_path))
    try:
        db.init_db(conn)
        first = db.get_database_continuity_status(conn)
        db.insert_recommendations(conn, [_recommendation("R-3", 1000, "blocked", is_root=True)])
        db.log_decision(conn, "PUBLISH", None, None, {"count_all": 1})
        second = db.get_database_continuity_status(conn)
    finally:
        conn.close()

    conn = db.connect(str(db_path))
    try:
        db.init_db(conn)
        third = db.get_database_continuity_status(conn)
    finally:
        conn.close()

    assert first["database_instance_id"]
    assert first["database_instance_id"] == second["database_instance_id"] == third["database_instance_id"]
    assert second["engine"] == "sqlite"
    assert second["recommendations_total"] == 1
    assert second["decision_log_total"] == 1
    assert second["first_recommendation_ts"] == 1000
    assert second["latest_recommendation_ts"] == 1000


def test_health_ui_exposes_restart_provenance_and_database_continuity() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    assert "runtime_provenance" in source
    assert "database_continuity" in source
    assert "Идентификатор БД" in source
    assert "Публикация текущего процесса" in source
    assert "Цикл сборщика текущего процесса" in source
