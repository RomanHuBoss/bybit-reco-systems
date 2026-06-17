from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration149.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration149_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _rec(now: int) -> dict:
    return {
        "rec_id": "decision-context-rec",
        "ts": now - 120,
        "ttl_sec": 600,
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_count": 10,
            "grid_levels": 10,
            "grid_type": "arithmetic",
            "leverage": 3,
            "price_ref": 100.0,
            "price_range_lower": 95.0,
            "price_range_upper": 105.0,
            "economics": {
                "net_profit_bps": 7.5,
                "gross_profit_bps": 22.0,
                "execution_cost_bps": 8.0,
                "funding_cost_bps": 1.25,
                "liquidation_buffer_pct": 33.0,
                "estimated_liquidation_price": 67.0,
                "estimated_total_order_notional_usdt": 300.0,
                "estimated_margin_required_usdt": 100.0,
            },
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 93.0, "upper": 107.0},
                    "grid_step": {"step_abs": 1.0, "step_pct": 1.0},
                    "tp_per_leg": {"abs": 0.7, "pct": 0.7},
                },
                "economics": {
                    "net_profit_bps": 7.5,
                    "gross_profit_bps": 22.0,
                    "execution_cost_bps": 8.0,
                    "funding_cost_bps": 1.25,
                },
            },
        },
    }


def test_backend_operator_decision_context_exposes_price_freshness_risk_and_economics(app_main, tmp_path: Path) -> None:
    now = int(time.time())
    conn = app_main.db.connect(str(tmp_path / "decision-context.sqlite"))
    app_main.db.init_db(conn)
    app_main.db.insert_tickers(
        conn,
        [{"venue": "linear", "symbol": "BTCUSDT", "ts": now - 15, "last": 101.0, "bid": 100.9, "ask": 101.1, "vol24h": 1000.0, "turnover24h": 100000.0}],
    )

    ctx = app_main._operator_decision_context_for_reco(
        _rec(now),
        conn=conn,
        guard={"ok": True, "errors": [], "warnings": []},
    )

    assert ctx["entry_price"] == 100.0
    assert ctx["current_price"] == 101.0
    assert ctx["price_status"] == "inside_range"
    assert ctx["recommendation_age_sec"] >= 120
    assert ctx["expires_in_sec"] <= 480
    assert ctx["preflight_status"] == "ok"
    assert ctx["net_profit_bps"] == 7.5
    assert ctx["execution_cost_bps"] == 8.0
    assert ctx["funding_cost_bps"] == 1.25
    assert ctx["liquidation_buffer_pct"] == 33.0
    assert ctx["risk_profile"] == "moderate"


def test_details_panel_keeps_entry_price_and_adds_price_actuality_and_risk_blocks() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Цена и актуальность" in app_js
    assert "Риск и экономика запуска" in app_js
    assert "buildPriceFreshnessFields" in app_js
    assert "buildRiskEconomicsFields" in app_js
    assert "Цена входа" in app_js
    assert "Текущая цена" in app_js
    assert "Отклонение от входа" in app_js
    assert "Запас до ликвидации" in app_js
    assert "Чистая прибыль/сетка" in app_js
    assert "Издержки исполнения" in app_js
    assert "Funding-риск" in app_js


def test_details_panel_tooltips_explain_abbreviations_and_english_exchange_terms() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
    styles = (ROOT / "app/ui/static/styles.css").read_text(encoding="utf-8")

    assert "field-help" in app_js
    assert "field-help" in styles
    assert "bps = базисные пункты: 1 bps = 0,01%" in app_js
    assert "Funding — периодические платежи между long и short" in app_js
    assert "Take Profit — уровень фиксации прибыли" in app_js
    assert "Stop Loss / kill-switch — защитный уровень остановки убытка" in app_js
    assert "LLM — языковая модель" in app_js


def test_static_asset_cache_key_bumped_after_decision_panel_update() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v42" in index
    assert "app.js?v=manual-ui-v42" in index
