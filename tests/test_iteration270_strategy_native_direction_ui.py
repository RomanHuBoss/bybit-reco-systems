from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from conftest import safe_linear_grid_params


def _trend_invalid_row(rec_id: str, ts: int) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "ONDOUSDT",
        "bot_type": "directional_trend",
        "direction": "neutral",
        "account_mode": "one_way",
        "margin_mode": "isolated",
        "score": 0.1,
        "confidence": 0.2,
        "expected_rr": 0.0,
        "risk_score": 1.0,
        "params": {
            "strategy_family": "directional_trend",
            "entry_model": "single_position_no_pyramiding",
            "trade_plan": {
                "reference_price": None,
                "entry_model": "single_position_no_pyramiding",
                "levels": {
                    "take_profit": {"price": None},
                    "stop_loss": {"price": None},
                },
            },
            "risk_report": {"decision": "not_recommended", "rejection_reasons": []},
        },
        "reasons": {
            "outcome_policy": {"eligible": False, "sample_role": "shadow_no_trade"},
            "top_negative_factors": [
                {"feature": "shadow_evidence", "msg": "Proxy outcome is not proof of live trend edge."}
            ],
        },
        "blocks": [],
        "status": "blocked",
        "ttl_sec": 900,
        "model_version": "test-model",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "outcome_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _grid_row(rec_id: str, ts: int) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "ONDOUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "one_way",
        "margin_mode": "cross",
        "score": 0.5,
        "confidence": 0.7,
        "expected_rr": 1.5,
        "risk_score": 0.2,
        "params": safe_linear_grid_params({"grid_levels": 8}),
        "reasons": {"outcome_policy": {"eligible": True, "sample_role": "actionable"}},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "test-model",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "outcome_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def test_strategy_direction_labels_do_not_call_invalid_trend_a_neutral_grid() -> None:
    root = Path(__file__).resolve().parents[1]
    app_js = root / "app" / "ui" / "static" / "app.js"
    js = app_js.read_text(encoding="utf-8")
    assert "function strategyDirectionRu" in js
    assert "function strategyDirectionBadge" in js
    assert "Направление не определено" in js
    assert "Trend-кандидат отклонён" in js
    assert "DIRECTIONAL_TREND_DIRECTION_INVALID" in js
    assert "Для trend-позиции не определено направление LONG или SHORT" in js

    script = f"""
const fs = require('fs');
const src = fs.readFileSync({str(app_js)!r}, 'utf8');
function extract(name, nextName) {{
  const start = src.indexOf('function ' + name);
  const end = src.indexOf('function ' + nextName, start + 1);
  if (start < 0 || end < 0) throw new Error('function not found: ' + name);
  return src.slice(start, end);
}}
let code = extract('directionRu', 'operatorStatusRu');
code += extract('strategyDirectionRu', 'strategyDirectionBadge');
code += extract('strategyDirectionBadge', 'operatorEffectiveStatus');
function escapeHtml(v) {{ return String(v); }}
const SUPPORTED_GRID_BOT_TYPE = 'futures_grid';
const DIRECTIONAL_TREND_BOT_TYPE = 'directional_trend';
eval(code);
const values = {{
  trendNeutral: strategyDirectionRu('directional_trend', 'neutral'),
  gridNeutral: strategyDirectionRu('futures_grid', 'neutral'),
  trendBadge: strategyDirectionBadge('directional_trend', 'neutral'),
  gridBadge: strategyDirectionBadge('futures_grid', 'neutral'),
}};
console.log(JSON.stringify(values));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    values = __import__("json").loads(result.stdout)
    assert values["trendNeutral"] == "Направление не определено"
    assert values["gridNeutral"] == "Нейтральная сетка"
    assert "Нейтральная сетка" not in values["trendBadge"]
    assert "Нейтральная сетка" in values["gridBadge"]


def test_details_api_keeps_grid_and_trend_validation_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "strategy-isolation.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "ONDOUSDT")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(
        app_main,
        "_fetch_bybit_instrument_meta",
        lambda venue, symbol: {
            "category": "linear", "symbol": symbol, "status": "Trading",
            "contract_type": "LinearPerpetual", "quote_coin": "USDT", "settle_coin": "USDT",
            "tick_size": "0.0001", "qty_step": "1", "min_order_qty": "1",
            "max_order_qty": "100000000", "min_notional": "5", "min_leverage": "1",
            "max_leverage": "50", "leverage_step": "0.01",
        },
    )
    conn = db.connect(str(db_path))
    db.init_db(conn)
    now = 1_800_000_000
    db.insert_recommendations(conn, [_grid_row("R-grid", now), _trend_invalid_row("R-trend", now)])
    client = TestClient(app_main.app)
    try:
        grid = client.get("/api/v1/recommendations/R-grid").json()
        trend = client.get("/api/v1/recommendations/R-trend").json()
    finally:
        client.close()
        conn.close()

    grid_codes = {str(item.get("code")) for item in grid.get("blocks", []) if isinstance(item, dict)}
    trend_codes = {str(item.get("code")) for item in trend.get("blocks", []) if isinstance(item, dict)}
    assert not any(code.startswith("DIRECTIONAL_TREND_") for code in grid_codes)
    assert "DIRECTIONAL_TREND_DIRECTION_INVALID" in trend_codes
    assert "DIRECTIONAL_TREND_LEVELS_MISSING" in trend_codes
    assert trend["bot_type"] == "directional_trend"
    assert trend["direction"] == "neutral"


def test_all_strategy_bound_ui_locations_use_strategy_aware_badges() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "strategyDirectionBadge(it.bot_type, it.direction)" in js
    assert "strategyDirectionBadge(row.bot_type || data?.bot_type, row.direction)" in js
    assert "strategyDirectionBadge(latest.bot_type || data?.bot_type, latest.direction)" in js
    assert "${directionBadge(it.direction)}" not in js
    assert "<td>${directionBadge(it.direction)}</td>" not in js
    assert 'humanizeOperatorText(a.title || "Действие")' not in js
    assert 'humanizeOperatorText(a.detail || "")' not in js


def test_next_actions_are_strategy_native_and_do_not_tell_trend_candidate_to_run_a_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "actions.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    rec = _trend_invalid_row("R-trend-actions", 1_800_000_000)
    actions = app_main._operator_next_actions_for_reco(
        rec,
        ctx={"liquidation_buffer_pct": None},
        guard_errors=[
            {"code": "DIRECTIONAL_TREND_DIRECTION_INVALID"},
            {"code": "DIRECTIONAL_TREND_LEVELS_MISSING"},
        ],
        guard_warnings=[],
    )
    codes = {item["code"] for item in actions}
    all_text = " ".join(str(item.get("title", "")) + " " + str(item.get("detail", "")) for item in actions).lower()
    assert "WAIT_FOR_CONFIRMED_TREND_DIRECTION" in codes
    assert "REBUILD_DIRECTIONAL_TREND_PLAN" in codes
    assert "AVOID_GRID_IN_STRONG_TREND" not in codes
    assert "не запускать сетку" not in all_text
    assert "ждать устойчивого бокового режима" not in all_text


def test_grid_strong_trend_remediation_remains_grid_specific(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "grid-actions.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    rec = _grid_row("R-grid-actions", 1_800_000_000)
    rec["reasons"]["top_negative_factors"] = [{"msg": "strong trend"}]
    actions = app_main._operator_next_actions_for_reco(
        rec,
        ctx={"liquidation_buffer_pct": None},
        guard_errors=[],
        guard_warnings=[],
    )
    assert "AVOID_GRID_IN_STRONG_TREND" in {item["code"] for item in actions}


def test_strategy_details_localize_common_grid_codes_and_deduplicate_concrete_codes() -> None:
    root = Path(__file__).resolve().parents[1]
    app_js = root / "app" / "ui" / "static" / "app.js"
    js = app_js.read_text(encoding="utf-8")
    assert "ACCOUNT_MODE_LEGACY_ALIAS" in js
    assert "MIN_LEVERAGE_PER_BOT_AT_EXECUTION" in js
    assert "GRID_STEP_LEVELS_MISMATCH" in js
    assert 'const genericCodes = new Set' in js
    assert "`code:${codeKey}`" in js
