from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration174.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration174_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def test_low_liquidation_buffer_details_expose_next_safe_actions(app_main) -> None:
    now = int(time.time())
    rec = {
        "rec_id": "low-liq-buffer-rec",
        "ts": now - 30,
        "ttl_sec": 600,
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "params": {
            "leverage": 5,
            "grid_count": 10,
            "grid_levels": 10,
            "price_ref": 100.0,
            "economics": {
                "liquidation_buffer_pct": 5.53,
                "liquidation_buffer_adverse_boundary_price": 85.3,
            },
            "trade_plan": {
                "reference_price": 100.0,
                "grid_count": 10,
                "levels": {
                    "range": {"lower": 88.0, "upper": 112.0},
                    "kill_switch": {"lower": 85.3, "upper": 115.0},
                    "grid_step": {"step_abs": 1.0, "step_pct": 1.0},
                    "tp_per_leg": {"abs": 0.7, "pct": 0.7},
                },
            },
        },
    }
    guard = {
        "ok": False,
        "errors": [
            {
                "code": "LIQUIDATION_BUFFER_TOO_LOW",
                "msg": "Оценочный worst-side liquidation buffer=5.53% слишком мал для запуска futures grid с leverage=5.",
            }
        ],
        "warnings": [],
    }

    ctx = app_main._operator_decision_context_for_reco(rec, guard=guard)
    actions = ctx["operator_next_actions"]

    assert actions[0]["code"] == "DO_NOT_LAUNCH_LOW_LIQUIDATION_BUFFER"
    assert "5.53%" in actions[0]["detail"]
    assert "≤3x" in actions[0]["detail"]
    assert actions[1]["code"] == "RECALCULATE_WITH_LOWER_LEVERAGE_OR_NARROWER_RANGE"
    assert "Не снижайте 12%" in actions[1]["detail"]


def test_frontend_renders_next_actions_after_blockers_before_rank_diagnostics() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "function operatorNextActionsHtml" in app_js
    assert "Что делать дальше" in app_js
    assert "operator_next_actions" in app_js

    decision_idx = app_js.index('<div class="operator-card operator-decision-card ${decisionClass}">')
    blockers_idx = app_js.index('${blockersHtml}', decision_idx)
    actions_idx = app_js.index('${nextActionsHtml}', decision_idx)
    diagnostics_idx = app_js.index('${launchDecisionDiagnosticsHtml(it, scoreMeta)}', decision_idx)

    assert blockers_idx < actions_idx < diagnostics_idx


def test_static_asset_cache_key_bumped_after_next_actions_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v45" in index
    assert "app.js?v=manual-ui-v45" in index


def test_no_trade_profile_reason_exposes_next_safe_actions(app_main) -> None:
    rec = {
        "rec_id": "profile-not-actionable-rec",
        "ts": int(time.time()) - 20,
        "ttl_sec": 600,
        "status": "no_trade",
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "FILUSDT",
        "direction": "short",
        "params": {
            "leverage": 3,
            "price_ref": 2.0,
            "risk_report": {
                "decision": "not_recommended",
                "no_trade_reasons": [
                    "идея не проходит текущий 3-5x leverage profile без ослабления risk policy; evaluated_leverage=3x, reason=signal_quality_too_low_for_operator_minimum"
                ],
                "warnings": [
                    "издержки исполнения и adverse funding давят на net result",
                    "сильный тренд ломает grid",
                    "высокая волатильность повышает риск range break",
                    "спред ухудшает fills",
                ],
            },
        },
        "reasons": {
            "decision_layers": {
                "final_status": "no_trade",
                "execution_status": "not_actionable",
                "no_trade_reasons": [
                    {
                        "code": "OPERATOR_LEVERAGE_PROFILE_NOT_ACTIONABLE",
                        "msg": "reason=signal_quality_too_low_for_operator_minimum",
                    }
                ],
            }
        },
    }

    ctx = app_main._operator_decision_context_for_reco(rec, guard={"ok": True, "errors": [], "warnings": []})
    codes = [item["code"] for item in ctx["operator_next_actions"]]

    assert "DO_NOT_LAUNCH_PROFILE_NOT_ACTIONABLE" in codes
    assert "WAIT_FOR_STRONGER_SIGNAL_OR_RANGE" in codes
    assert "WAIT_FOR_LOWER_VOLATILITY" in codes
    assert "ручной запуск" in ctx["operator_next_actions"][0]["detail"]
