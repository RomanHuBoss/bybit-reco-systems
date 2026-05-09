from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration121.db"))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    mod = importlib.import_module("app.main")
    try:
        yield mod
    finally:
        sys.modules.pop("app.main", None)


def _recommended_grid_rec() -> dict:
    return {
        "rec_id": "R-iteration121-operator-guard",
        "ts": 1_700_000_000,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.68,
        "confidence": 0.74,
        "risk_score": 0.30,
        "status": "recommended",
        "blocks": [],
        "reasons": {
            "risk_checks": {"passed": True, "blocks": []},
            "decision_layers": {"final_status": "recommended"},
        },
        "params": {
            "grid_levels": 8,
            "grid_count": 8,
            "grid_type": "arithmetic",
            "leverage": 2,
            "margin_mode": "isolated",
            "risk_report": {
                "decision": "recommended",
                "rejection_reasons": [],
            },
            "economics": {"liquidation_buffer_pct": 35.0},
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 98.0, "upper": 102.0},
                    "kill_switch": {"lower": 96.0, "upper": 104.0},
                    "grid_step": {"step_abs": 0.5},
                },
                "sizing": {
                    "order_qty": 0.001,
                    "order_notional_usdt": 0.10,
                },
            },
        },
    }


def _complete_linear_usdt_meta(**overrides) -> dict:
    meta = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "delivery_time": 0,
        "tick_size": "0.1",
        "min_price": "0.1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "max_order_qty": "1000",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }
    meta.update(overrides)
    return meta


def test_operator_view_blocks_recommended_grid_when_bybit_min_notional_fails(app_main, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: _complete_linear_usdt_meta())

    body = app_main._augment_reco_for_ui(_recommended_grid_rec())

    assert body["status"] == "blocked"
    assert body["bybit_operator_guard"]["ok"] is False
    codes = {item["code"] for item in body["blocks"]}
    risk_codes = [item["code"] for item in body["reasons"]["risk_checks"]["blocks"]]
    assert "ORDER_NOTIONAL_BELOW_MIN" in codes
    assert risk_codes.count("ORDER_NOTIONAL_BELOW_MIN") == 1
    assert body["params"]["risk_report"]["decision"] == "not_recommended"
    assert body["reasons"]["risk_checks"]["passed"] is False
    assert body["reasons"]["decision_layers"]["bybit_operator_guard"] == "blocked"


def test_operator_view_blocks_when_bybit_metadata_is_unavailable(app_main, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: {})

    body = app_main._augment_reco_for_ui(_recommended_grid_rec())

    assert body["status"] == "blocked"
    assert body["bybit_operator_guard"]["ok"] is False
    codes = {item["code"] for item in body["blocks"]}
    assert "BYBIT_META_UNAVAILABLE" in codes
    assert body["params"]["risk_report"]["decision"] == "not_recommended"
