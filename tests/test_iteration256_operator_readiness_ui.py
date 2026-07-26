from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import db

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"
INDEX_HTML = ROOT / "app" / "ui" / "static" / "index.html"


def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"function {name} not found"
    start = match.start()
    pos = match.end()
    depth = 1
    while pos < len(source) and depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


def _recommendation(rec_id: str, ts: int, status: str, reasons: dict) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT" if rec_id.endswith("1") else "ETHUSDT",
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
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def test_operator_decision_visual_contract_distinguishes_no_trade_from_blocked() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    fn = _extract_js_function(source, "operatorDecisionPresentation")
    harness = "\n".join([
        'function operatorEffectiveStatus(it) { return String(it?.effective_status || it?.status || "").trim().toLowerCase(); }',
        fn,
        'console.log(JSON.stringify({noTrade: operatorDecisionPresentation({status:"no_trade"}), blocked: operatorDecisionPresentation({status:"blocked"}), pending: operatorDecisionPresentation({status:"pending"})}));',
    ])
    result = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    assert payload["noTrade"]["className"] == "decision-no-trade"
    assert payload["blocked"]["className"] == "decision-blocked"
    assert payload["pending"]["className"] == "decision-pending"
    assert payload["noTrade"]["label"] == "НЕ ТОРГОВАТЬ"
    assert payload["blocked"]["label"] == "ЗАБЛОКИРОВАНО"


def test_details_action_is_the_rightmost_recommendation_table_column() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    render_fn = _extract_js_function(source, "renderRecoTable")
    index = INDEX_HTML.read_text(encoding="utf-8")
    status_cell = render_fn.index('data-cell="status"')
    details_cell = render_fn.index('data-cell="details"')
    assert details_cell > status_cell
    table_head = index.split('<table class="table" id="recoTable">', 1)[1].split("</thead>", 1)[0]
    assert table_head.rfind("Детали") > table_head.rfind("Решение")


def test_health_modal_combines_runtime_status_and_supports_diagnostic_export() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    fn = _extract_js_function(source, "loadHealth")
    assert 'fetch("/api/v1/status")' in fn
    assert "Promise.all" in fn
    assert 'data-act="copy-health-diagnostics"' in fn
    assert 'data-act="download-health-diagnostics"' in fn
    assert "recommendation_readiness" in fn
    assert "outcome_worker" in fn
    assert "database_schema" in fn


def test_outcome_schema_status_proves_automatic_migration(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "schema.db"))
    try:
        db.init_db(conn)
        status = db.get_outcome_policy_schema_status(conn)
        assert status["migration_applied"] is True
        assert status["missing_columns"] == []
        assert status["materialization_pending"] == 0
    finally:
        conn.close()


def test_api_status_explains_zero_actionable_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "status.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT,ETHUSDT")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "0")
    monkeypatch.setenv("REQUIRE_CONF_GATE", "1")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    now = int(time.time())
    calibration_reason = {
        "risk_checks": {"passed": True, "blocks": []},
        "decision_layers": {
            "no_trade_reasons": [
                {"code": "PROXY_MONETARY_EXPECTANCY_UNPROVEN", "msg": "not enough evidence"},
                {"code": "CALIBRATED_CONFIDENCE_UNAVAILABLE", "msg": "model not fitted"},
            ]
        },
        "outcome_policy": {
            "eligible": True,
            "policy_evaluation_eligible": True,
            "sample_role": "shadow_no_trade",
        },
    }
    blocked_reason = {
        "risk_checks": {
            "passed": False,
            "blocks": [{"code": "LIQUIDITY_TOO_LOW", "msg": "too thin"}],
        },
        "decision_layers": {"no_trade_reasons": []},
        "outcome_policy": {"eligible": False, "policy_evaluation_eligible": False, "sample_role": "excluded"},
    }
    conn = db.connect(str(db_path))
    try:
        db.insert_recommendations(conn, [
            _recommendation("R-1", now, "no_trade", calibration_reason),
            _recommendation("R-2", now, "no_trade", calibration_reason),
            _recommendation("R-3", now, "blocked", blocked_reason),
        ])
        conn.commit()
        status = app_main.api_status()
    finally:
        conn.close()
        sys.modules.pop("app.main", None)

    snapshot = status["recommendation_readiness"]
    assert snapshot["latest_snapshot_total"] == 3
    assert snapshot["status_counts"]["no_trade"] == 2
    assert snapshot["status_counts"]["blocked"] == 1
    assert snapshot["actionable_count"] == 0
    assert snapshot["calibration_only_no_trade_count"] == 2
    assert snapshot["no_trade_reason_counts"][0]["code"] == "PROXY_MONETARY_EXPECTANCY_UNPROVEN"
    assert status["database_schema"]["migration_applied"] is True
    assert status["operator_readiness"]["state"] in {"healthy_not_actionable", "degraded"}


def test_release_version_and_static_cache_bust_are_updated() -> None:
    main_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    index = INDEX_HTML.read_text(encoding="utf-8")
    assert 'version="1.4.9"' in main_source
    assert "build=1.4.9" in index


def test_operator_release_documents_match_the_v168_ui_contract() -> None:
    from html import unescape
    from zipfile import ZipFile

    docx = ROOT / "docs" / "instrukciya_operatora_bybit_recommender.docx"
    pdf = ROOT / "docs" / "instrukciya_operatora_bybit_recommender.pdf"
    png = ROOT / "how_to_trade.png"
    with ZipFile(docx) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    text = unescape(re.sub(r"<[^>]+>", " ", xml))
    text = re.sub(r"\s+", " ", text)
    assert "Версия документа: 1.4.8" in text
    assert "шесть фиксированных колонок" in text
    assert "Скачать диагностику JSON" in text
    assert "Исход по правилам стратегии" in text
    assert "Неуспех · kill-switch" in text
    assert "Кнопка «Детали» размещена в ячейке символа" not in text
    assert pdf.read_bytes().startswith(b"%PDF-")
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png.read_bytes()) > 100_000
