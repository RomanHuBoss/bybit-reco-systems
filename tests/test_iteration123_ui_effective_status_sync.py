from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db


@pytest.fixture()
def client_conn_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "effective-status.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    ts_now = int(time.time())
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": ts_now - 60,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10.0,
            }
        ],
    )
    db.insert_tickers(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "ts": ts_now - 30,
                "last": 100.5,
                "bid": 100.4,
                "ask": 100.6,
                "vol24h": 1000.0,
                "turnover24h": 100000.0,
            }
        ],
    )
    db.insert_features(conn, "linear", "BTCUSDT", ts_now - 30, {"volume_z": 0.1})
    client = TestClient(app_main.app)
    try:
        yield client, conn, app_main
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


def _meta_with_low_max_leverage(_venue: str, symbol: str) -> dict[str, str]:
    return {
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
        "max_leverage": "1",
        "leverage_step": "0.01",
    }


def test_operator_list_and_detail_use_same_effective_bybit_guard_status(client_conn_app, monkeypatch):
    client, conn, app_main = client_conn_app

    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", _meta_with_low_max_leverage)

    ts_now = int(time.time())
    db.insert_regime(conn, ts_now, {"vol_state": "low", "trend_state": "mixed", "risk_state": "risk_on", "confidence": 0.61})
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-effective-status-sync",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "unified",
                "margin_mode": "cross",
                "score": 0.82,
                "confidence": 0.91,
                "expected_rr": 1.4,
                "risk_score": 0.2,
                "params": {"grid_levels": 8, "grid_count": 8, "leverage": 3},
                "reasons": {},
                "blocks": [],
                "status": "active",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            }
        ],
    )

    default_resp = client.get("/api/v1/recommendations?snapshot=latest_operator&min_conf=0")
    assert default_resp.status_code == 200
    default_body = default_resp.json()
    assert default_body["items"] == []
    assert default_body["no_trade"] is True

    blocked_resp = client.get(
        "/api/v1/recommendations?snapshot=latest_operator&min_conf=0&show_recommended=false&show_blocked=true"
    )
    assert blocked_resp.status_code == 200
    blocked_items = blocked_resp.json()["items"]
    assert len(blocked_items) == 1
    assert blocked_items[0]["rec_id"] == "R-effective-status-sync"
    assert blocked_items[0]["status"] == "blocked"
    assert {block["code"] for block in blocked_items[0]["blocks"]} >= {"LEVERAGE_ABOVE_MAX"}

    details_resp = client.get("/api/v1/recommendations/R-effective-status-sync")
    assert details_resp.status_code == 200
    details = details_resp.json()
    assert details["status"] == blocked_items[0]["status"]
    assert {block["code"] for block in details["blocks"]} >= {"LEVERAGE_ABOVE_MAX"}
