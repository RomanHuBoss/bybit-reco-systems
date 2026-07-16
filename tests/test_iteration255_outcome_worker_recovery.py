from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

from app import db
from app import main as app_main
from app import outcomes


def _eligible_recommendation(*, rec_id: str, ts: int) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.4,
        "confidence": 0.5,
        "expected_rr": 0.1,
        "risk_score": 0.2,
        "params": {},
        "reasons": {
            "risk_checks": {"passed": True, "blocks": []},
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": True,
                "sample_role": "shadow_no_trade",
            },
        },
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": "test-model",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _grid_params() -> dict:
    return {
        "grid_count": 4,
        "grid_levels": 4,
        "price_range_lower": 98.0,
        "price_range_upper": 102.0,
        "cost_model": {
            "execution_cost_bps": 0.0,
            "expected_funding_bps": 0.0,
        },
        "trade_plan": {
            "grid_count": 4,
            "cost_model": {
                "execution_cost_bps": 0.0,
                "expected_funding_bps": 0.0,
            },
            "levels": {
                "range": {"lower": 98.0, "upper": 102.0},
                "kill_switch": {"lower": 97.0, "upper": 103.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def test_outcome_processing_has_its_own_supervised_thread_and_lock() -> None:
    source = Path(app_main.__file__).read_text(encoding="utf-8")
    reco_source = inspect.getsource(app_main._reco_thread)

    assert '_start_background_thread("outcomes"' in source
    assert 'lock_key = "runtime:outcomes"' in source
    assert "progress_callback=persist_running_progress" in source
    assert "compute_outcomes_once" not in reco_source
    assert "compute_outcomes_cycle" not in reco_source


def test_recent_completed_cycle_with_progress_is_backlog_not_false_stall(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "liveness-progress.db"))
    statements: list[str] = []
    try:
        db.init_db(conn)
        now = 1_800_000_000
        rec_ts = now - 13 * 3600
        db.insert_recommendations(conn, [_eligible_recommendation(rec_id="R-progress", ts=rec_ts)])
        db.set_app_config_json(
            conn,
            "outcome_worker_cycle",
            {
                "state": "completed",
                "cycle_started_ts": now - 20,
                "cycle_finished_ts": now - 10,
                "updated_ts": now - 10,
                "rows_selected": 200,
                "rows_examined": 200,
                "rows_labeled": 0,
                "rows_waiting": 0,
                "rows_censored": 200,
                "rows_failed": 0,
                "matured_pending_before": 201,
                "matured_pending_after": 1,
                "last_processed_rec_id": "R-before",
            },
        )
        raw = getattr(conn, "_conn", conn)
        if hasattr(raw, "set_trace_callback"):
            raw.set_trace_callback(statements.append)

        status = db.get_outcome_worker_liveness(
            conn,
            now_ts_value=now,
            require_llm_verdict=True,
            worker_stale_after_sec=180,
        )

        assert status["state"] == "backlog"
        assert status["code"] == "OUTCOME_WORKER_BACKLOG"
        assert status["matured_pending_total"] == 1
        assert status["unattempted_total"] == 1
        assert status["worker_cycle"]["rows_examined"] == 200
        normalized = [" ".join(item.split()).lower() for item in statements]
        liveness_selects = [item for item in normalized if "reco_outcomes" in item and "recommendations" in item]
        assert any("count(" in item for item in liveness_selects)
        assert not any("select r.rec_id, r.ts, r.bot_type, r.status, r.reasons_json" in item for item in liveness_selects)
    finally:
        conn.close()


def test_recent_running_cycle_is_processing_not_stalled(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "liveness-running.db"))
    try:
        db.init_db(conn)
        now = 1_800_100_000
        rec_ts = now - 13 * 3600
        db.insert_recommendations(conn, [_eligible_recommendation(rec_id="R-running", ts=rec_ts)])
        db.set_app_config_json(
            conn,
            "outcome_worker_cycle",
            {
                "state": "running",
                "cycle_started_ts": now - 5,
                "updated_ts": now - 5,
                "rows_selected": 0,
                "rows_examined": 0,
            },
        )

        status = db.get_outcome_worker_liveness(
            conn,
            now_ts_value=now,
            require_llm_verdict=True,
            worker_stale_after_sec=180,
        )

        assert status["state"] == "processing"
        assert status["code"] == "OUTCOME_WORKER_PROCESSING"
    finally:
        conn.close()


def test_outcome_cycle_persists_operator_progress_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "cycle-metrics.db"))
    try:
        db.init_db(conn)
        snapshots = iter([
            {
                "state": "stalled",
                "code": "OUTCOME_WORKER_STALLED",
                "matured_pending_total": 12,
                "oldest_due_ts": 100,
            },
            {
                "state": "backlog",
                "code": "OUTCOME_WORKER_BACKLOG",
                "matured_pending_total": 7,
                "oldest_due_ts": 200,
            },
            {
                "state": "backlog",
                "code": "OUTCOME_WORKER_BACKLOG",
                "matured_pending_total": 7,
                "oldest_due_ts": 200,
            },
        ])
        monkeypatch.setattr(db, "get_outcome_worker_liveness", lambda *_args, **_kwargs: next(snapshots))
        monkeypatch.setattr(
            app_main,
            "compute_outcomes_cycle",
            lambda *_args, **_kwargs: {
                "rows_selected": 8,
                "rows_examined": 8,
                "rows_labeled": 2,
                "rows_waiting": 1,
                "rows_censored": 5,
                "rows_failed": 0,
                "last_processed_rec_id": "R-last",
                "duration_ms": 25,
            },
            raising=False,
        )

        result = app_main._run_outcome_cycle_once(conn, heartbeat=lambda: True)
        stored = db.get_app_config_json(conn, "outcome_worker_cycle")

        assert result["state"] == "completed"
        assert result["rows_examined"] == 8
        assert result["rows_labeled"] == 2
        assert result["rows_waiting"] == 1
        assert result["rows_censored"] == 5
        assert result["matured_pending_before"] == 12
        assert result["matured_pending_after"] == 7
        assert result["oldest_pending_before"] == 100
        assert result["oldest_pending_after"] == 200
        assert result["last_processed_rec_id"] == "R-last"
        assert stored == result
    finally:
        conn.close()


def test_intrabar_path_ambiguity_has_explicit_machine_reason(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "explicit-grid-reason.db"))
    try:
        db.init_db(conn)
        base_ts = 1_709_200_000
        db.upsert_ohlcv(conn, [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts,
            "open": 100.0,
            "high": 103.0,
            "low": 97.0,
            "close": 100.0,
            "volume": 1_000.0,
        }])
        diagnostics: dict[str, object] = {}

        result = outcomes._grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts,
            base_ts + 60,
            "neutral",
            {
                **_grid_params(),
                "price_range_lower": 99.0,
                "price_range_upper": 101.0,
                "grid_count": 2,
                "grid_levels": 2,
                "trade_plan": {
                    **_grid_params()["trade_plan"],
                    "grid_count": 2,
                    "levels": {
                        "range": {"lower": 99.0, "upper": 101.0},
                        "kill_switch": {"lower": 98.0, "upper": 102.0},
                        "tp_per_leg": {"abs": 1.0},
                    },
                },
            },
            diagnostics=diagnostics,
        )

        assert result is None
        assert diagnostics["reason"] == "dual_kill_switch_breach_order_unobservable"
        assert diagnostics["transient"] is False
        assert "unknown" not in json.dumps(diagnostics, ensure_ascii=False).lower()
    finally:
        conn.close()


def test_sql_liveness_preserves_strict_boolean_shadow_policy(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "strict-shadow-policy.db"))
    try:
        db.init_db(conn)
        now = 1_800_200_000
        rec_ts = now - 13 * 3600
        valid = _eligible_recommendation(rec_id="R-valid-bool", ts=rec_ts)
        numeric = _eligible_recommendation(rec_id="R-numeric-bool", ts=rec_ts)
        numeric["reasons"]["outcome_policy"]["eligible"] = 1
        textual = _eligible_recommendation(rec_id="R-text-bool", ts=rec_ts)
        textual["reasons"]["risk_checks"]["passed"] = "true"
        db.insert_recommendations(conn, [valid, numeric, textual])

        status = db.get_outcome_worker_liveness(conn, now_ts_value=now)

        assert status["matured_pending_total"] == 1
        assert status["sample_unattempted_rec_ids"] == ["R-valid-bool"]
    finally:
        conn.close()


def test_outcome_liveness_uses_materialized_indexable_policy_columns(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "materialized-outcome-policy.db"))
    try:
        db.init_db(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(recommendations)").fetchall()}
        assert {
            "outcome_eligible",
            "policy_evaluation_eligible",
            "outcome_sample_role",
            "risk_checks_passed",
            "risk_blocks_empty",
            "llm_review_status",
        }.issubset(columns)
        source = inspect.getsource(db.get_outcome_worker_liveness)
        assert "reasons_json" not in source
        assert "outcome_eligible" in source
    finally:
        conn.close()


def test_existing_sqlite_database_backfills_materialized_outcome_policy(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "legacy-upgrade.db"))
    try:
        conn.execute(
            """CREATE TABLE recommendations (
              rec_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, venue TEXT NOT NULL,
              symbol TEXT NOT NULL, bot_type TEXT NOT NULL, direction TEXT NOT NULL,
              account_mode TEXT NOT NULL, margin_mode TEXT NOT NULL, score REAL NOT NULL,
              confidence REAL NOT NULL, expected_rr REAL NOT NULL, risk_score REAL NOT NULL,
              params_json TEXT NOT NULL, reasons_json TEXT NOT NULL, blocks_json TEXT NOT NULL,
              status TEXT NOT NULL, ttl_sec INTEGER NOT NULL, model_version TEXT NOT NULL,
              features_ref_ts INTEGER NOT NULL, publication_root_rec_id TEXT,
              is_outcome_label_root INTEGER NOT NULL DEFAULT 1
            )"""
        )
        reasons = _eligible_recommendation(rec_id="R-legacy", ts=1_700_000_000)["reasons"]
        conn.execute(
            """INSERT INTO recommendations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "R-legacy", 1_700_000_000, "linear", "BTCUSDT", "futures_grid", "neutral",
                "unified", "isolated", 0.4, 0.5, 0.1, 0.2, "{}",
                json.dumps(reasons), "[]", "no_trade", 900, "legacy", 1_700_000_000,
                "R-legacy", 1,
            ),
        )
        conn.commit()

        db.init_db(conn)
        row = conn.execute(
            """SELECT outcome_eligible, policy_evaluation_eligible, outcome_sample_role,
                      risk_checks_passed, risk_blocks_empty, llm_review_status
                 FROM recommendations WHERE rec_id='R-legacy'"""
        ).fetchone()
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(recommendations)").fetchall()}

        assert dict(row) == {
            "outcome_eligible": 1,
            "policy_evaluation_eligible": 1,
            "outcome_sample_role": "shadow_no_trade",
            "risk_checks_passed": 1,
            "risk_blocks_empty": 1,
            "llm_review_status": None,
        }
        assert "idx_reco_outcome_liveness" in indexes
        assert "idx_reco_llm_outcome_liveness" in indexes
        db.init_db(conn)
        assert db.backfill_recommendation_outcome_policy_fields(conn) == 0
    finally:
        conn.close()


def test_review_update_keeps_materialized_llm_status_in_sync(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "review-sync.db"))
    try:
        db.init_db(conn)
        row = _eligible_recommendation(rec_id="R-review", ts=1_700_000_000)
        db.insert_recommendations(conn, [row])
        reasons = dict(row["reasons"])
        reasons["llm_review"] = {"status": "ok", "agree_with_engine": True}

        assert db.update_recommendation_review(conn, "R-review", reasons=reasons, status="no_trade") is True
        stored = conn.execute(
            """SELECT llm_review_status, outcome_eligible, risk_checks_passed
                 FROM recommendations WHERE rec_id='R-review'"""
        ).fetchone()

        assert dict(stored) == {
            "llm_review_status": "ok",
            "outcome_eligible": 1,
            "risk_checks_passed": 1,
        }
    finally:
        conn.close()


def test_postgres_reference_schema_contains_materialized_outcome_columns() -> None:
    sql = Path("migrations/init_postgres.sql").read_text(encoding="utf-8")
    for column in (
        "outcome_eligible",
        "policy_evaluation_eligible",
        "outcome_sample_role",
        "risk_checks_passed",
        "risk_blocks_empty",
        "llm_review_status",
    ):
        assert column in sql


def test_outcome_cycle_has_a_hard_scan_cap_while_preserving_wait_rotation() -> None:
    source = inspect.getsource(outcomes.compute_outcomes_cycle)
    assert "OUTCOME_MAX_ROWS_EXAMINED_PER_CYCLE" in source
    assert outcomes.OUTCOME_MAX_ROWS_EXAMINED_PER_CYCLE == 2000
