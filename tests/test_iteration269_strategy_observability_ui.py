from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app import recommender
from conftest import safe_linear_grid_params


def _trend_params() -> dict:
    return {
        "strategy_family": "directional_trend",
        "entry_model": "single_position_no_pyramiding",
        "label_horizon_hours": 12,
        "trade_plan": {
            "reference_price": 100.0,
            "entry_model": "single_position_no_pyramiding",
            "levels": {
                "take_profit": {"price": 104.0},
                "stop_loss": {"price": 98.0},
            },
            "target_notional": 100.0,
        },
    }


def _row(rec_id: str, ts: int, bot_type: str) -> dict:
    params = safe_linear_grid_params({"grid_levels": 8}) if bot_type == "futures_grid" else _trend_params()
    reasons = {
        "outcome_policy": {
            "eligible": True,
            "sample_role": "shadow_no_trade" if bot_type == "directional_trend" else "actionable",
            "comparison_return_basis": "unlevered_net_return_on_committed_notional_v1",
        },
        "risk_checks": {"passed": True, "blocks": []},
    }
    if bot_type == "directional_trend":
        reasons["trend_event_model"] = {
            "ready": True,
            "tp_first_probability": 0.62,
            "sl_first_probability": 0.18,
            "horizon_exit_probability": 0.20,
            "event_expected_net_return": 0.006,
            "event_expected_net_return_lower_bound": 0.002,
        }
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": bot_type,
        "direction": "long",
        "account_mode": "one_way",
        "margin_mode": "cross",
        "score": 0.5,
        "confidence": 0.7,
        "expected_rr": 2.0,
        "risk_score": 0.2,
        "params": params,
        "reasons": reasons,
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "test-model",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "outcome_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


@pytest.fixture()
def client_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "obs.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(
        app_main,
        "_fetch_bybit_instrument_meta",
        lambda venue, symbol: {
            "category": "linear", "symbol": symbol, "status": "Trading",
            "contract_type": "LinearPerpetual", "quote_coin": "USDT", "settle_coin": "USDT",
            "tick_size": "0.1", "qty_step": "0.001", "min_order_qty": "0.001",
            "max_order_qty": "1000", "min_notional": "5", "min_leverage": "1",
            "max_leverage": "100", "leverage_step": "0.01",
        },
    )
    conn = db.connect(str(db_path))
    db.init_db(conn)
    client = TestClient(app_main.app)
    try:
        yield client, conn, app_main
    finally:
        client.close()
        conn.close()


def test_canonical_event_type_is_persisted_and_exposed_in_recent_outcomes(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "events.db"))
    db.init_db(conn)
    now = int(time.time()) - 50_000
    db.insert_recommendations(conn, [_row("R-trend", now, "directional_trend")])
    db.insert_outcome(conn, {
        "rec_id": "R-trend", "ts": now, "venue": "linear", "symbol": "BTCUSDT",
        "bot_type": "directional_trend", "direction": "long", "horizon_sec": 43_200,
        "label_available_ts": now + 43_260, "entry_close": 100.0, "exit_close": 104.0,
        "ret": 0.038, "success": 1, "event_type": "TP_FIRST",
        "diagnostics": {"event_type": "SL_FIRST", "exit_reason": "take_profit"},
    })
    row = conn.execute("SELECT event_type FROM reco_outcomes WHERE rec_id='R-trend'").fetchone()
    assert row["event_type"] == "TP_FIRST"
    obs = conn.execute("SELECT details_json FROM reco_outcome_observability WHERE rec_id='R-trend'").fetchone()
    assert json.loads(obs["details_json"])["event_type"] == "TP_FIRST"
    recent = db.get_outcomes_recent_enriched(conn, scope="archive", limit=10)
    assert recent[0]["event_type"] == "TP_FIRST"
    conn.close()


def test_outcome_worker_liveness_counts_grid_and_trend_separately(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "live.db"))
    db.init_db(conn)
    now = int(time.time())
    due_ts = now - 60
    rows = [_row("R-grid", now - 50_000, "futures_grid"), _row("R-trend", now - 50_000, "directional_trend")]
    db.insert_recommendations(conn, rows)
    for rec_id in ("R-grid", "R-trend"):
        db.upsert_outcome_observability(
            conn, rec_id=rec_id, recommendation_ts=now - 50_000,
            label_due_ts=due_ts, state="waiting", reason="outcome_not_matured", details={}, commit=False,
        )
    conn.commit()
    status = db.get_outcome_worker_liveness(conn, now_ts_value=now)
    assert status["matured_pending_total"] == 2
    assert status["by_bot_type"]["futures_grid"]["matured_pending_total"] == 1
    assert status["by_bot_type"]["directional_trend"]["matured_pending_total"] == 1
    conn.close()


def test_history_contains_strategy_price_geometry_and_root_outcome(client_and_conn) -> None:
    client, conn, _app_main = client_and_conn
    now = int(time.time()) - 50_000
    db.insert_recommendations(conn, [_row("R-trend", now, "directional_trend")])
    db.insert_outcome(conn, {
        "rec_id": "R-trend", "ts": now, "venue": "linear", "symbol": "BTCUSDT",
        "bot_type": "directional_trend", "direction": "long", "horizon_sec": 43_200,
        "label_available_ts": now + 43_260, "entry_close": 100.0, "exit_close": 104.0,
        "ret": 0.038, "success": 1, "event_type": "TP_FIRST",
        "diagnostics": {"event_type": "TP_FIRST", "exit_reason": "take_profit"},
    })
    response = client.get("/api/v1/recommendations/history?venue=linear&symbol=BTCUSDT&bot_type=directional_trend")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["price_geometry"] == {
        "kind": "directional_trend",
        "reference_price": 100.0,
        "take_profit": 104.0,
        "stop_loss": 98.0,
    }
    assert item["outcome_tracking"]["event_type"] == "TP_FIRST"
    assert item["outcome_tracking"]["ret"] == pytest.approx(0.038)


def test_details_exposes_outcome_tracking_for_grid_and_trend(client_and_conn) -> None:
    client, conn, _app_main = client_and_conn
    now = int(time.time()) - 50_000
    db.insert_recommendations(conn, [_row("R-grid", now, "futures_grid"), _row("R-trend", now, "directional_trend")])
    for rec_id, bot_type, event_type in (("R-grid", "futures_grid", "GRID_OUTCOME"), ("R-trend", "directional_trend", "TP_FIRST")):
        db.insert_outcome(conn, {
            "rec_id": rec_id, "ts": now, "venue": "linear", "symbol": "BTCUSDT",
            "bot_type": bot_type, "direction": "long", "horizon_sec": 43_200,
            "label_available_ts": now + 43_260, "entry_close": 100.0, "exit_close": 102.0,
            "ret": 0.01, "success": 1, "event_type": event_type,
            "diagnostics": {"event_type": event_type},
        })
    for rec_id, event_type in (("R-grid", "GRID_OUTCOME"), ("R-trend", "TP_FIRST")):
        response = client.get(f"/api/v1/recommendations/{rec_id}")
        assert response.status_code == 200
        assert response.json()["outcome_tracking"]["event_type"] == event_type


def test_decision_journal_includes_strategy_identity(client_and_conn) -> None:
    client, conn, _app_main = client_and_conn
    now = int(time.time())
    db.insert_recommendations(conn, [_row("R-trend", now, "directional_trend")])
    db.log_decision(conn, "RECOMMENDATION_REVIEW", "R-trend", "operator", {"note": "ok"})
    response = client.get("/api/v1/decisions?limit=10")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["bot_type"] == "directional_trend"
    assert row["symbol"] == "BTCUSDT"
    assert row["direction"] == "long"


def test_database_continuity_has_per_strategy_and_event_counts(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "continuity.db"))
    db.init_db(conn)
    now = int(time.time()) - 50_000
    db.insert_recommendations(conn, [_row("R-grid", now, "futures_grid"), _row("R-trend", now, "directional_trend")])
    db.insert_outcome(conn, {
        "rec_id": "R-trend", "ts": now, "venue": "linear", "symbol": "BTCUSDT",
        "bot_type": "directional_trend", "direction": "long", "horizon_sec": 43_200,
        "label_available_ts": now + 43_260, "entry_close": 100.0, "exit_close": 104.0,
        "ret": 0.038, "success": 1, "event_type": "TP_FIRST", "diagnostics": {},
    })
    status = db.get_database_continuity_status(conn)
    assert status["recommendations_by_bot_type"] == {"directional_trend": 1, "futures_grid": 1}
    assert status["outcomes_by_bot_type"] == {"directional_trend": 1}
    assert status["outcomes_by_event_type"] == {"TP_FIRST": 1}
    conn.close()


def test_frontend_is_strategy_aware_for_details_outcomes_health_journal_and_history() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "it?.bybit_instrument_meta" not in js
    assert "function outcomeEventTypeRu" in js
    assert "TP раньше SL" in js
    assert "SL раньше TP" in js
    assert "function buildStrategyPriceTimelineSvg" in js
    assert "price_geometry" in js
    assert "outcome_tracking" in js
    assert "by_bot_type" in js
    assert 'showModalHtml("Журнал решений"' in js
    assert "Trend first-touch" in js

def test_trend_exact_policy_eligibility_does_not_require_mean_reversion() -> None:
    now = int(time.time())
    due = now - 10
    row = {
        "bot_type": "directional_trend",
        "ts": due - 43_320,
        "horizon_sec": 43_200,
        "label_available_ts": due,
        "score": 0.75,
        "policy_evaluation_eligible": 1,
        "outcome_eligible": 1,
        "outcome_sample_role": "actionable_root",
    }
    snapshot = {
        "policy_fingerprint": "a" * 64,
        "policy_contract_verified": True,
        "sample_role": "actionable_root",
        "reasons": {
            "feature_snapshot": {
                "trend_evidence_valid": True,
                "trend_strength": 0.72,
                "coherence": 0.66,
                "regime": "trend",
                "mean_reversion_score": None,
                "mean_reversion_evidence_valid": False,
            },
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": True,
                "sample_role": "actionable_root",
                "label_due_ts": due,
                "policy_contract": {
                    "selection": {"min_score_to_recommend": 0.5, "mean_reversion_min_score": 0.6},
                    "calibration": {"label_due_grace_sec": 120},
                },
            },
        },
    }
    result = db._outcome_eligibility_snapshot(
        row, snapshot, active_policy_fingerprint="a" * 64, now_ts_value=now
    )
    assert result["gates"]["strategy_evidence_kind"] == "trend"
    assert result["gates"]["strategy_evidence_passed"] is True
    assert "MEAN_REVERSION_EVIDENCE_INVALID" not in result["eligibility_reason_codes"]
    assert result["calibration_eligible"] is True


def test_archive_summary_preserves_strategy_and_event_distributions(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "archive-summary.db"))
    db.init_db(conn)
    now = int(time.time()) - 50_000
    db.insert_recommendations(conn, [
        _row("R-grid", now, "futures_grid"),
        _row("R-trend", now + 1, "directional_trend"),
    ])
    db.insert_outcome(conn, {
        "rec_id": "R-grid", "ts": now, "venue": "linear", "symbol": "BTCUSDT",
        "bot_type": "futures_grid", "direction": "long", "horizon_sec": 43_200,
        "label_available_ts": now + 43_260, "entry_close": 100.0, "exit_close": 101.0,
        "ret": 0.008, "success": 1, "event_type": "LEGACY_BINARY", "diagnostics": {},
    })
    db.insert_outcome(conn, {
        "rec_id": "R-trend", "ts": now + 1, "venue": "linear", "symbol": "BTCUSDT",
        "bot_type": "directional_trend", "direction": "long", "horizon_sec": 43_200,
        "label_available_ts": now + 43_261, "entry_close": 100.0, "exit_close": 98.0,
        "ret": -0.022, "success": 0, "event_type": "SL_FIRST", "diagnostics": {},
    })
    stats = db.get_outcomes_stats(conn, scope="archive", include_breakdowns=False, recent_limit=10)
    assert stats["event_type_counts"] == {"GRID_OUTCOME": 1, "SL_FIRST": 1}
    assert stats["event_type_counts_by_bot"] == {
        "directional_trend": {"SL_FIRST": 1},
        "futures_grid": {"GRID_OUTCOME": 1},
    }
    by_bot = {row["bot_type"]: row for row in stats["by_bot"]}
    assert by_bot["futures_grid"]["total"] == 1
    assert by_bot["directional_trend"]["total"] == 1
    assert by_bot["directional_trend"]["avg_ret"] == pytest.approx(-2.2)
    conn.close()


def test_history_geometry_supports_legacy_exact_top_level_fields(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "legacy-geometry.db"))
    db.init_db(conn)
    now = int(time.time())
    trend = _row("R-trend-legacy", now, "directional_trend")
    trend["params"] = {
        "price_ref": 101.0,
        "take_profit_price": 106.0,
        "stop_loss_price": 99.0,
    }
    grid = _row("R-grid-legacy", now + 1, "futures_grid")
    grid["params"] = {
        "reference_price": 100.0,
        "price_range_lower": 96.0,
        "price_range_upper": 104.0,
        "kill_switch_lower": 94.0,
        "kill_switch_upper": 106.0,
        "grid_count": 9,
    }
    db.insert_recommendations(conn, [trend, grid])
    trend_rows, _ = db.get_recommendation_history(
        conn, venue="linear", symbol="BTCUSDT", bot_type="directional_trend"
    )
    grid_rows, _ = db.get_recommendation_history(
        conn, venue="linear", symbol="BTCUSDT", bot_type="futures_grid"
    )
    assert trend_rows[0]["price_geometry"] == {
        "kind": "directional_trend", "reference_price": 101.0,
        "take_profit": 106.0, "stop_loss": 99.0,
    }
    assert grid_rows[0]["price_geometry"] == {
        "kind": "futures_grid", "reference_price": 100.0,
        "range_lower": 96.0, "range_upper": 104.0,
        "kill_lower": 94.0, "kill_upper": 106.0, "grid_count": 9,
    }
    conn.close()


def test_strategy_graph_breaks_lines_across_missing_persisted_geometry() -> None:
    js = (Path(__file__).resolve().parents[1] / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "A missing persisted level is an evidentiary gap" in js
    assert 'segmentOpen = false' in js
    assert 'd.trim()' in js


def test_init_db_materializes_legacy_grid_event_type_without_relabeling_trend(tmp_path: Path) -> None:
    path = tmp_path / "event-migration.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    now = int(time.time()) - 50_000
    db.insert_recommendations(conn, [
        _row("R-grid", now, "futures_grid"),
        _row("R-trend", now + 1, "directional_trend"),
    ])
    for rec_id, ts, bot_type in (
        ("R-grid", now, "futures_grid"),
        ("R-trend", now + 1, "directional_trend"),
    ):
        conn.execute(
            """INSERT INTO reco_outcomes(
                rec_id, ts, venue, symbol, bot_type, direction, horizon_sec,
                label_available_ts, entry_close, exit_close, ret, success, event_type
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec_id, ts, "linear", "BTCUSDT", bot_type, "long", 43_200,
             ts + 43_260, 100.0, 101.0, 0.008, 1, "LEGACY_BINARY"),
        )
    conn.commit()
    db.init_db(conn)
    values = {
        row["bot_type"]: row["event_type"]
        for row in conn.execute("SELECT bot_type, event_type FROM reco_outcomes").fetchall()
    }
    assert values == {
        "futures_grid": "GRID_OUTCOME",
        "directional_trend": "LEGACY_BINARY",
    }
    conn.close()


def test_health_semantic_integrity_detects_orphan_and_noncanonical_rows(client_and_conn) -> None:
    _client, conn, app_main = client_and_conn
    now = int(time.time()) - 50_000
    conn.execute(
        """INSERT INTO reco_outcomes(
            rec_id, ts, venue, symbol, bot_type, direction, horizon_sec,
            label_available_ts, entry_close, exit_close, ret, success, event_type
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("ORPHAN", now, "linear", "BTCUSDT", "directional_trend", "long", 43_200,
         now + 43_260, 100.0, 101.0, 0.008, 1, "AMBIGUOUS"),
    )
    conn.commit()
    continuity = db.get_database_continuity_status(conn)
    integrity = continuity["outcome_semantic_integrity"]
    assert integrity["ok"] is False
    assert integrity["orphan_outcome_total"] == 1
    assert integrity["missing_observability_total"] == 1
    assert integrity["invalid_event_type_total"] == 1
    assert integrity["persisted_ambiguous_total"] == 1
    readiness = app_main._operator_runtime_readiness(
        schema_status={"migration_applied": True, "materialization_pending": 0},
        recommendation_readiness={"actionable_count": 0, "latest_snapshot_total": 1},
        outcome_worker={"state": "idle"},
        collector_state="idle",
        background_threads={},
        database_continuity=continuity,
    )
    assert readiness["state"] == "degraded"
    assert any(item["code"] == "OUTCOME_SEMANTIC_INTEGRITY_FAILED" for item in readiness["explanations"])


def test_recommendation_list_batches_outcome_tracking_without_n_plus_one(client_and_conn, monkeypatch: pytest.MonkeyPatch) -> None:
    client, conn, app_main = client_and_conn
    now = int(time.time())
    db.insert_recommendations(conn, [
        _row("R-grid-batch", now, "futures_grid"),
        _row("R-trend-batch", now, "directional_trend"),
    ])
    db.upsert_outcome_observability(
        conn, rec_id="R-grid-batch", recommendation_ts=now,
        label_due_ts=now + 43_260, state="waiting", reason="outcome_not_matured",
        details={}, commit=False,
    )
    db.upsert_outcome_observability(
        conn, rec_id="R-trend-batch", recommendation_ts=now,
        label_due_ts=now + 43_260, state="waiting", reason="outcome_not_matured",
        details={}, commit=False,
    )
    conn.commit()

    def forbidden_single_lookup(*_args, **_kwargs):
        raise AssertionError("recommendation list must use get_outcome_tracking_many")

    monkeypatch.setattr(app_main.db, "get_outcome_tracking", forbidden_single_lookup)
    response = client.get(
        "/api/v1/recommendations?venue=linear&top_n=10&min_conf=0"
        "&show_recommended=true&snapshot=latest"
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    assert all(item["outcome_tracking"]["state"] == "waiting" for item in items)
    batch = db.get_outcome_tracking_many(conn, ["R-grid-batch", "R-trend-batch"])
    assert set(batch) == {"R-grid-batch", "R-trend-batch"}
    assert all(item["state"] == "waiting" for item in batch.values())


def test_backend_directional_exit_payload_uses_trend_tp_sl_not_grid_kill_switch(client_and_conn) -> None:
    _client, _conn, app_main = client_and_conn
    rec = _row("R-trend-exit", int(time.time()), "directional_trend")
    payload = app_main._directional_exit_payload_for_reco(rec)
    assert payload["direction"] == "long"
    assert payload["has_directional_take_profit"] is True
    assert payload["reference_price"] == pytest.approx(100.0)
    assert payload["take_profit"] == pytest.approx(104.0)
    assert payload["stop_loss"] == pytest.approx(98.0)
    assert payload["geometry_valid"] is True
    assert payload["risk_reward"] == pytest.approx(2.0)


def test_recommendation_insert_schedules_grid_and_trend_before_horizon(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "scheduled-ledger.db"))
    db.init_db(conn)
    now = int(time.time())
    db.insert_recommendations(conn, [
        _row("R-grid-scheduled", now, "futures_grid"),
        _row("R-trend-scheduled", now + 1, "directional_trend"),
    ])
    rows = conn.execute(
        """SELECT rec_id, recommendation_ts, label_due_ts, last_attempt_ts, state, reason
             FROM reco_outcome_observability ORDER BY rec_id"""
    ).fetchall()
    assert [row["rec_id"] for row in rows] == ["R-grid-scheduled", "R-trend-scheduled"]
    assert all(row["state"] == "waiting" for row in rows)
    assert all(row["reason"] == "scheduled_for_label_horizon" for row in rows)
    assert all(int(row["last_attempt_ts"]) == 0 for row in rows)
    assert int(rows[0]["label_due_ts"]) == now + 43_200 + db.POLICY_LABEL_GRACE_SEC
    assert int(rows[1]["label_due_ts"]) == now + 1 + 43_200 + db.POLICY_LABEL_GRACE_SEC

    health = db.get_outcome_worker_liveness(conn, now_ts_value=now)
    assert health["matured_pending_total"] == 0
    assert health["scheduled_waiting_total"] == 2
    assert health["by_bot_type"]["futures_grid"]["scheduled_waiting_total"] == 1
    assert health["by_bot_type"]["directional_trend"]["scheduled_waiting_total"] == 1
    assert health["by_bot_type"]["directional_trend"]["next_due_ts"] == now + 1 + 43_200 + db.POLICY_LABEL_GRACE_SEC
    conn.close()


def test_init_db_backfills_missing_observability_for_existing_outcome(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "legacy-observability.db"))
    db.init_db(conn)
    now = int(time.time()) - 50_000
    db.insert_recommendations(conn, [_row("R-trend-existing", now, "directional_trend")])
    db.insert_outcome(conn, {
        "rec_id": "R-trend-existing", "ts": now, "venue": "linear", "symbol": "BTCUSDT",
        "bot_type": "directional_trend", "direction": "long", "horizon_sec": 43_200,
        "label_available_ts": now + 43_260, "entry_close": 100.0, "exit_close": 104.0,
        "ret": 0.038, "success": 1, "event_type": "TP_FIRST", "diagnostics": {},
    })
    conn.execute("DELETE FROM reco_outcome_observability WHERE rec_id='R-trend-existing'")
    conn.commit()
    db.init_db(conn)
    row = conn.execute(
        "SELECT state, reason, last_attempt_ts, details_json FROM reco_outcome_observability WHERE rec_id='R-trend-existing'"
    ).fetchone()
    assert row["state"] == "labeled"
    assert row["reason"] == "existing_outcome_materialized"
    assert int(row["last_attempt_ts"]) == now + 43_260
    assert json.loads(row["details_json"])["event_type"] == "TP_FIRST"
    assert db.get_outcome_semantic_integrity(conn)["ok"] is True
    conn.close()


def test_details_api_exposes_scheduled_due_and_strategy_native_exit_levels(client_and_conn) -> None:
    client, conn, _app_main = client_and_conn
    now = int(time.time())
    db.insert_recommendations(conn, [
        _row("R-grid-detail-native", now, "futures_grid"),
        _row("R-trend-detail-native", now + 1, "directional_trend"),
    ])
    trend = client.get("/api/v1/recommendations/R-trend-detail-native")
    assert trend.status_code == 200
    trend_payload = trend.json()
    assert trend_payload["outcome_tracking"]["state"] == "waiting"
    assert trend_payload["outcome_tracking"]["reason"] == "scheduled_for_label_horizon"
    assert trend_payload["outcome_tracking"]["label_due_ts"] == now + 1 + 43_200 + db.POLICY_LABEL_GRACE_SEC
    assert trend_payload["directional_exit_levels"]["level_source"] == "directional_trend_trade_plan"
    assert trend_payload["directional_exit_levels"]["take_profit"] == pytest.approx(104.0)
    assert trend_payload["directional_exit_levels"]["stop_loss"] == pytest.approx(98.0)

    grid = client.get("/api/v1/recommendations/R-grid-detail-native")
    assert grid.status_code == 200
    grid_payload = grid.json()
    assert grid_payload["outcome_tracking"]["state"] == "waiting"
    assert grid_payload["directional_exit_levels"]["level_source"] == "futures_grid_kill_switch"
    assert grid_payload["directional_exit_levels"]["take_profit"] is not None
    assert grid_payload["directional_exit_levels"]["stop_loss"] is not None


def test_health_frontend_displays_immature_schedule_per_strategy() -> None:
    js = (Path(__file__).resolve().parents[1] / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Ожидают созревания горизонта" in js
    assert "Grid · ожидают горизонта" in js
    assert "Trend · ожидают горизонта" in js
    assert "next_due_ts" in js


def test_strategy_price_timeline_executes_for_multiple_trend_and_grid_rows() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    start = source.index("function buildStrategyPriceTimelineSvg")
    end = source.index("function sortRecommendationHistoryRowsNewestFirst", start)
    function_source = source[start:end]
    script = f"""
const toFiniteNumber = value => {{
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}};
const escapeHtml = value => String(value ?? '');
const formatDotNumber = value => String(value);
const formatTs = value => String(value);
{function_source}
const trend = buildStrategyPriceTimelineSvg([
  {{ts: 1000, timestamp_valid: true, bot_type: 'directional_trend', price_geometry: {{reference_price: 100, take_profit: 104, stop_loss: 98}}}},
  {{ts: 1100, timestamp_valid: true, bot_type: 'directional_trend', price_geometry: {{reference_price: 101, take_profit: 105, stop_loss: 99}}}},
]);
const grid = buildStrategyPriceTimelineSvg([
  {{ts: 1000, timestamp_valid: true, bot_type: 'futures_grid', price_geometry: {{reference_price: 100, range_lower: 95, range_upper: 105, kill_lower: 94, kill_upper: 106}}}},
  {{ts: 1100, timestamp_valid: true, bot_type: 'futures_grid', price_geometry: {{reference_price: 101, range_lower: 96, range_upper: 106, kill_lower: 95, kill_upper: 107}}}},
]);
if (!trend.includes('<svg') || !trend.includes('TP') || !grid.includes('<svg') || !grid.includes('Kill')) process.exit(2);
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_operator_shell_and_outcome_errors_are_strategy_neutral_and_fail_visible() -> None:
    root = Path(__file__).resolve().parents[1]
    index = (root / "app" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Bybit Recommender — Сетка и тренд" in index
    assert "Ответ API невозможно прочитать" in js
    assert "Данные не подменялись пустой статистикой" in js
    assert "История получена, но не может быть безопасно отображена" in js
    assert "Данные не подменялись вымышленной линией" in js
    assert "timeTickCount" in js
    assert "index, array" not in js
    assert "function operatorBlockMessageRu" in js
    assert "План одной направленной позиции неполон" in js


def test_production_generated_trend_plan_round_trips_through_db_and_details(client_and_conn) -> None:
    client, conn, _app_main = client_and_conn
    now = int(time.time())
    feature = {
        "price": 100.0,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.02,
        "_direction_agg": {"regime": "trend"},
    }
    params = recommender._directional_trend_params(
        venue="linear",
        f=feature,
        direction="long",
        global_sent=0.0,
        direction_bias="long",
        direction_bias_strength=0.8,
        atr_pct=0.02,
        cost_model={"execution_cost_bps": 10.0, "expected_funding_bps": 0.0},
        risk_limits={"min_leverage": 2, "max_leverage": 5, "max_position_notional_usdt": 100.0},
    )
    params["trade_plan"] = recommender._build_trade_plan(
        "directional_trend", "linear", feature, "long", params,
        cost_model=params["cost_model"],
    )
    row = _row("R-trend-production", now, "directional_trend")
    row.update({"account_mode": "unified", "margin_mode": "isolated", "params": params})
    db.insert_recommendations(conn, [row])

    response = client.get("/api/v1/recommendations/R-trend-production")
    assert response.status_code == 200
    payload = response.json()
    levels = params["trade_plan"]["levels"]
    assert payload["params"]["trade_plan"]["entry_model"] == "single_position_no_pyramiding"
    assert payload["directional_exit_levels"]["level_source"] == "directional_trend_trade_plan"
    assert payload["directional_exit_levels"]["take_profit"] == pytest.approx(levels["take_profit"]["price"])
    assert payload["directional_exit_levels"]["stop_loss"] == pytest.approx(levels["stop_loss"]["price"])
    assert payload["outcome_tracking"]["state"] == "waiting"
    assert payload["outcome_tracking"]["reason"] == "scheduled_for_label_horizon"
    persisted = conn.execute(
        "SELECT params_json, bot_type, account_mode, margin_mode FROM recommendations WHERE rec_id=?",
        ("R-trend-production",),
    ).fetchone()
    assert persisted["bot_type"] == "directional_trend"
    assert persisted["account_mode"] == "unified"
    assert persisted["margin_mode"] == "isolated"
    assert json.loads(persisted["params_json"])["trade_plan"]["levels"] == levels
