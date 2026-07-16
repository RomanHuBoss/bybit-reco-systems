from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from app import db

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"


def _import_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "runtime_locks.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("COLLECT_INTERVAL_SEC", "20")
    monkeypatch.setenv("STALE_DATA_MAX_SEC", "300")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "0")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def _shadow_exploration(rec_id: str, ts: int) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.2,
        "confidence": 0.5,
        "expected_rr": 0.1,
        "risk_score": 0.2,
        "params": {},
        "reasons": {
            "risk_checks": {"passed": True, "blocks": []},
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": False,
                "sample_role": "shadow_no_trade",
                "calibration_role": "shadow_exploration",
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


def test_shadow_exploration_is_visible_to_outcome_worker_under_advisory_llm(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "shadow.db"))
    try:
        db.init_db(conn)
        now = 1_800_000_000
        db.insert_recommendations(conn, [_shadow_exploration("R-shadow", now - 13 * 3600)])
        status = db.get_outcome_worker_liveness(
            conn,
            now_ts_value=now,
            require_llm_verdict=True,
            worker_stale_after_sec=300,
        )
        assert status["matured_pending_total"] == 1
        assert status["sample_unattempted_rec_ids"] == ["R-shadow"]
    finally:
        conn.close()


def test_supervisor_releases_owned_collector_lock_on_graceful_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_main = _import_app(monkeypatch, tmp_path)
    try:
        app_main.RUNTIME_OWNER = "RRMPC:19460"
        app_main._BACKGROUND_STOP_EVENT.clear()

        def target() -> None:
            with closing(app_main._get_lock_conn()) as conn:
                assert db.acquire_runtime_lock(conn, "runtime:collector", app_main.RUNTIME_OWNER, ttl_sec=400)

        app_main._run_supervised_background_target(
            "collector",
            target,
            treat_return_as_error=False,
        )
        with closing(app_main._get_lock_conn()) as conn:
            row = conn.execute(
                "SELECT owner FROM runtime_locks WHERE lock_key=?",
                ("runtime:collector",),
            ).fetchone()
        assert row is None
    finally:
        app_main._BACKGROUND_STOP_EVENT.set()
        sys.modules.pop("app.main", None)


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
        "score": 0.2,
        "confidence": 0.5,
        "expected_rr": 0.1,
        "risk_score": 0.2,
        "params": {},
        "reasons": {"risk_checks": {"passed": False, "blocks": [{"code": "TEST"}]}},
        "blocks": [],
        "status": "blocked",
        "ttl_sec": 900,
        "model_version": "test-model",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def test_status_reports_restart_handover_instead_of_false_collector_stall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_main = _import_app(monkeypatch, tmp_path)
    now = int(time.time())
    app_main.PROCESS_STARTED_TS = now - 350
    app_main.RUNTIME_OWNER = "RRMPC:19460"
    conn = db.connect(str(tmp_path / "app.db"))
    try:
        db.init_db(conn)
        db.insert_recommendations(conn, [_recommendation("R-old", now - 360)])
        db.set_app_config_json(conn, "collector_last_cycle", {
            "started_ts": now - 360,
            "owner": "RRMPC:11952",
            "duration_ms": 8498,
        })
        for name in ("collector", "backfill", "futures_meta", "sentiment", "reco", "outcomes"):
            db.set_app_config_json(conn, f"runtime_thread_state:{name}", {
                "name": name,
                "state": "running",
                "updated_ts": now,
                "owner": "RRMPC:19460",
            })
        with closing(app_main._get_lock_conn()) as lock_conn:
            db.acquire_runtime_lock(lock_conn, "runtime:collector", "RRMPC:11952", ttl_sec=400)
        status = app_main.api_status()
    finally:
        conn.close()
        sys.modules.pop("app.main", None)

    assert status["runtime_provenance"]["boot_grace_active"] is True
    assert status["runtime_provenance"]["boot_grace_sec"] >= 420
    assert status["collector"]["state"] == "handover"
    assert status["operator_readiness"]["state"] == "starting"
    codes = {item["code"] for item in status["operator_readiness"]["issues"]}
    assert "COLLECTOR_STALLED" not in codes
    assert status["runtime_provenance"]["collector_lock_owner"] == "RRMPC:11952"
    assert status["runtime_provenance"]["collector_lock_takeover_in_sec"] > 0


def _extract_js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    quote = None
    escape = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(name)


def test_decision_log_localization_preserves_codes_and_identifiers() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    functions = "\n".join(_extract_js_function(source, name) for name in (
        "humanizeOperatorText", "operatorStatusRu", "directionRu", "llmStatusRu",
        "sampleRoleRu", "gateDecisionRu", "timeframeRu", "botTypeLabel", "marginModeRu",
        "decisionActionRu", "outcomeObservabilityReasonRu", "isTechnicalIdentifierField",
        "localizeObjectForDisplay",
    ))
    payload = {
        "action": "OUTCOME_SKIP_INVALID_GRID_CONTRACT",
        "rec_id": "R-1783827888-linear-BTCUSDT-futures_grid-2920711a",
        "details": {"reason": "intrabar_extreme_order_unobservable", "entry_ts": 1783827900},
    }
    script = f"{functions}\nconsole.log(JSON.stringify(localizeObjectForDisplay({json.dumps(payload)})));"
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    localized = json.loads(result.stdout)

    assert "Идентификатор рекомендации" in localized
    assert localized["Идентификатор рекомендации"] == payload["rec_id"]
    assert "OUTCOME_SKIP_INVALID_GRID_CONTRACT" in localized["Действие"]
    assert "не удалось однозначно определить" in localized["Сведения"]["Причина"].lower()
    assert "intrabar_extreme_order_unobservable" in localized["Сведения"]["Причина"]
