from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration175.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration175_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def test_funding_rate_unknown_details_expose_next_safe_action(app_main) -> None:
    rec = {
        "rec_id": "funding-missing-rec",
        "ts": int(time.time()) - 20,
        "ttl_sec": 600,
        "status": "blocked",
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "params": {
            "leverage": 5,
            "price_ref": 100.0,
            "risk_report": {"decision": "rejected", "rejection_reasons": ["FUNDING_RATE_UNKNOWN"]},
        },
        "blocks": [{"code": "FUNDING_RATE_UNKNOWN", "msg": "нет актуального funding rate"}],
    }
    guard = {"ok": False, "errors": [{"code": "FUNDING_RATE_UNKNOWN", "msg": "нет актуального funding rate"}], "warnings": []}

    ctx = app_main._operator_decision_context_for_reco(rec, guard=guard)
    actions = ctx["operator_next_actions"]

    assert actions[0]["code"] == "REFRESH_FUNDING_RATE_SNAPSHOT"
    assert "funding rate" in actions[0]["title"]
    assert "fail-closed" in actions[0]["detail"]


def test_common_data_quality_blockers_have_specific_next_actions(app_main) -> None:
    rec = {
        "rec_id": "mtf-liquidity-rec",
        "ts": int(time.time()) - 20,
        "ttl_sec": 600,
        "status": "blocked",
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "ETHUSDT",
        "direction": "neutral",
        "params": {"leverage": 5, "price_ref": 2000.0},
        "blocks": [
            {"code": "INSUFFICIENT_MTF_HISTORY_FOR_GRID", "msg": "used only two timeframes"},
            {"code": "RANGE_EDGE_TOO_WEAK_FOR_GRID", "msg": "range edge weak"},
            {"code": "LIQUIDITY_UNKNOWN", "msg": "turnover missing"},
        ],
    }
    guard = {"ok": False, "errors": rec["blocks"], "warnings": []}

    ctx = app_main._operator_decision_context_for_reco(rec, guard=guard)
    codes = [item["code"] for item in ctx["operator_next_actions"]]

    assert "WAIT_FOR_MTF_HISTORY" in codes
    assert "WAIT_FOR_RANGE_REGIME" in codes
    assert "WAIT_FOR_CONFIRMED_LIQUIDITY" in codes


def test_frontend_auto_expands_non_actionable_diagnostics_when_actionable_filter_is_empty() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "function shouldAutoExpandDiagnostics" in app_js
    assert "onlyActionableFilter" in app_js
    assert "$(\"showBlocked\").checked = true" in app_js
    assert "$(\"showNoTrade\").checked = true" in app_js
    assert "$(\"showPending\").checked = true" in app_js
    assert "UI автоматически раскрывает диагностические" in app_js


def test_static_asset_cache_key_bumped_after_diagnostics_visibility_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v37" in index
    assert "app.js?v=manual-ui-v37" in index
