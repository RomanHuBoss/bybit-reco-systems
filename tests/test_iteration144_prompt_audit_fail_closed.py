from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration144.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration144_runtime_lock.db"))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    mod = importlib.import_module("app.main")
    try:
        yield mod
    finally:
        sys.modules.pop("app.main", None)


def _meta(**overrides) -> dict:
    meta = {
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
    meta.update(overrides)
    return meta


def _base_rec() -> dict:
    return {
        "rec_id": "R-iteration144-fail-closed",
        "ts": 1_700_000_000,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.74,
        "confidence": 0.72,
        "risk_score": 0.24,
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
            "margin_mode": "cross",
            "risk_report": {"decision": "recommended", "rejection_reasons": []},
            "economics": {"liquidation_buffer_pct": 35.0},
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 98.0, "upper": 102.0},
                    "kill_switch": {"lower": 96.0, "upper": 104.0},
                    "grid_step": {"step_abs": 0.5},
                    "tp_per_leg": {"abs": 0.25, "pct": 0.25},
                },
                "sizing": {"order_qty": 0.051, "order_notional_usdt": 5.1},
            },
        },
    }


def _codes(payload: dict, key: str = "errors") -> set[str]:
    return {str(item.get("code")) for item in payload.get(key, []) if isinstance(item, dict)}


def test_operator_guard_blocks_recommended_grid_when_trade_plan_is_missing(app_main, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: _meta())
    rec = _base_rec()
    rec["params"].pop("trade_plan")

    body = app_main._augment_reco_for_ui(rec)

    assert body["status"] == "blocked"
    assert body["bybit_operator_guard"]["ok"] is False
    assert "TRADE_PLAN_MISSING" in _codes(body["bybit_operator_guard"])
    assert body["params"]["risk_report"]["decision"] == "not_recommended"
    assert body["reasons"]["risk_checks"]["passed"] is False


def test_empty_params_recommendation_gets_non_launchable_operator_guard_without_rebuilding_legacy_shape(app_main, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: _meta())
    rec = _base_rec()
    rec["params"] = {}
    rec["reasons"] = {}
    rec["blocks"] = []

    body = app_main._augment_reco_for_ui(rec)

    assert body["params"] == {}
    assert body["reasons"] == {}
    assert body["blocks"] == []
    assert body["bybit_operator_guard"]["ok"] is False
    assert "PAYLOAD_UNAVAILABLE_FOR_OPERATOR_GUARD" in _codes(body["bybit_operator_guard"])
    assert "TRADE_PLAN_MISSING" in _codes(body["bybit_operator_guard"])


def test_strict_meta_preflight_blocks_missing_bybit_category_symbol_and_status(app_main):
    rec = _base_rec()
    meta = _meta(category="", symbol="", status="")

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        meta,
        require_meta=True,
        require_execution_plan=True,
    )

    assert validation["ok"] is False
    assert {"BYBIT_META_CATEGORY_MISSING", "BYBIT_META_SYMBOL_MISSING", "BYBIT_STATUS_MISSING"} <= _codes(validation)


def test_fetched_bybit_meta_preserves_missing_symbol_category_for_fail_closed_validation(app_main, monkeypatch: pytest.MonkeyPatch):
    class FakeBybitClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_instrument_info(self, category, symbol):
            return {
                "status": "Trading",
                "contractType": "LinearPerpetual",
                "quoteCoin": "USDT",
                "settleCoin": "USDT",
                "deliveryTime": 0,
                "priceFilter": {"tickSize": "0.1", "minPrice": "1", "maxPrice": "1000000"},
                "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "100", "minNotionalValue": "5"},
                "leverageFilter": {"minLeverage": "1", "maxLeverage": "100", "leverageStep": "0.01"},
            }

        def close(self):
            pass

    monkeypatch.setattr(app_main, "BybitPublicClient", FakeBybitClient)
    app_main._instrument_meta_cache.clear()

    meta = app_main._fetch_bybit_instrument_meta("linear", "BTCUSDT")

    assert meta["category"] == ""
    assert meta["symbol"] == ""
    assert meta["status"] == "Trading"


def test_frontend_launch_gate_requires_strict_operator_guard_and_trade_plan():
    js = Path("app/ui/static/app.js").read_text()

    assert "!params.trade_plan" in js
    assert "riskDecision !== \"recommended\"" in js
    assert "guard.ok === true" in js
    assert "guard.meta_checked === true" in js
    assert "llmStatus === \"pending\"" in js
