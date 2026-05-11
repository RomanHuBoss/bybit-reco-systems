from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from tests.conftest import safe_linear_grid_params


def _active_rec_without_llm(ts_now: int) -> dict:
    return {
        "rec_id": "R-llm-legacy-active-no-verdict",
        "ts": ts_now,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "one_way",
        "margin_mode": "isolated",
        "score": 0.78,
        "confidence": 0.86,
        "expected_rr": 1.4,
        "risk_score": 0.2,
        "params": safe_linear_grid_params({"grid_levels": 8}),
        "reasons": {},
        "blocks": [],
        "status": "active",
        "ttl_sec": 1800,
        "model_version": "test",
        "features_ref_ts": ts_now,
    }


def _import_app_with_llm_enabled(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "llm_guard.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "1")
    monkeypatch.setenv("LLM_REVIEWER_MODE", "advisory")
    monkeypatch.setenv("LLM_REVIEWER_MODEL", "fake-llm")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(
        app_main,
        "_fetch_bybit_instrument_meta",
        lambda venue, symbol: {
            "category": "linear",
            "symbol": str(symbol or "BTCUSDT").upper(),
            "status": "Trading",
            "contract_type": "LinearPerpetual",
            "quote_coin": "USDT",
            "settle_coin": "USDT",
            "tick_size": "0.1",
            "qty_step": "0.001",
            "min_order_qty": "0.001",
            "max_order_qty": "1000",
            "min_notional": "5",
            "min_leverage": "1",
            "max_leverage": "100",
            "leverage_step": "0.01",
        },
    )
    conn = db.connect(str(db_path))
    db.init_db(conn)
    return app_main, conn


def test_api_demotes_legacy_active_without_llm_verdict_to_pending_effective_status(tmp_path, monkeypatch):
    app_main, conn = _import_app_with_llm_enabled(tmp_path, monkeypatch)
    ts_now = int(time.time())
    db.insert_recommendations(conn, [_active_rec_without_llm(ts_now)])

    client = TestClient(app_main.app)
    try:
        resp = client.get("/api/v1/recommendations?venue=linear&show_recommended=false&show_pending=true&top_n=10&min_conf=0")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["status"] == "pending"
        assert item["stored_status"] == "active"
        assert item["reasons"]["llm_review"]["status"] == "pending"
        assert item["reasons"]["llm_review"]["publish_target_status"] == "active"
        assert item["reasons"]["llm_review"]["requires_ok_verdict"] is True
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


def test_api_does_not_show_legacy_active_without_llm_verdict_as_actionable(tmp_path, monkeypatch):
    app_main, conn = _import_app_with_llm_enabled(tmp_path, monkeypatch)
    ts_now = int(time.time())
    db.insert_recommendations(conn, [_active_rec_without_llm(ts_now)])

    client = TestClient(app_main.app)
    try:
        resp = client.get("/api/v1/recommendations?venue=linear&show_recommended=true&show_pending=false&top_n=10&min_conf=0")
        assert resp.status_code == 200
        body = resp.json()
        assert all(item["status"] != "active" for item in body["items"])
        assert body["no_trade"] is True
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)
