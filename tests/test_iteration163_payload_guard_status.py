from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration163.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration163_runtime_lock.db"))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    mod = importlib.import_module("app.main")
    try:
        yield mod
    finally:
        sys.modules.pop("app.main", None)


def _meta() -> dict:
    return {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "delivery_time": 0,
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "max_order_qty": "100",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def _recommended_empty_payload_rec() -> dict:
    return {
        "rec_id": "R-iteration163-empty-payload",
        "ts": 1_700_000_000,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "short",
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.81,
        "confidence": 0.77,
        "risk_score": 0.19,
        "status": "recommended",
        "blocks": [],
        "reasons": {},
        "params": {},
    }


def _codes(payload: dict, key: str = "errors") -> set[str]:
    return {str(item.get("code")) for item in payload.get(key, []) if isinstance(item, dict)}


def test_empty_params_recommended_row_is_effectively_blocked_without_rebuilding_legacy_payload(
    app_main,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: _meta())

    body = app_main._augment_reco_for_ui(_recommended_empty_payload_rec())

    assert body["stored_status"] == "recommended"
    assert body["status"] == "blocked"
    assert body["effective_status"] == "blocked"
    assert body["params"] == {}
    assert body["reasons"] == {}
    assert body["blocks"] == []
    assert body["bybit_operator_guard"]["ok"] is False
    assert "PAYLOAD_UNAVAILABLE_FOR_OPERATOR_GUARD" in _codes(body["bybit_operator_guard"])
    assert "TRADE_PLAN_MISSING" in _codes(body["bybit_operator_guard"])
    assert body["operator_decision_context"]["preflight_status"] == "blocked"


def test_how_to_trade_source_mentions_complete_trade_plan_payload() -> None:
    source = (Path(__file__).resolve().parent.parent / "docs" / "HOW_TO_TRADE_INFOGRAPHIC.md").read_text(encoding="utf-8")

    assert "Complete `params.trade_plan` exists; no empty/corrupted payload." in source
