from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db


@pytest.fixture()
def isolated_app_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "iteration105.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("STALE_DATA_MAX_SEC", "120")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app, raise_server_exceptions=False)
    try:
        yield app_main, client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


def _insert_recommendation(conn, *, rec_id: str, ts_now: int) -> None:
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": rec_id,
                "ts": ts_now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "unified",
                "margin_mode": "isolated",
                "score": 0.44,
                "confidence": 0.71,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {
                    "grid_levels": 8,
                    "leverage": 2,
                    "trade_plan": {
                        "reference_price": 100.03,
                        "levels": {
                            "range": {"lower": 99.12, "upper": 101.12},
                            "kill_switch": {"lower": 98.52, "upper": 101.72},
                            "grid_step": {"step_abs": 0.18},
                        },
                    },
                },
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            }
        ],
    )


# Execute-path не должен подтверждать рекомендацию, если текущий market-data слой уже stale/missing.
def test_execute_recommendation_is_blocked_when_execution_time_market_data_is_missing(isolated_app_and_conn, monkeypatch: pytest.MonkeyPatch):
    app_main, client, conn = isolated_app_and_conn
    ts_now = int(time.time())
    _insert_recommendation(conn, rec_id="R-iteration105-missing-data", ts_now=ts_now)
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: {"category":"linear","symbol":str(symbol or "BTCUSDT").upper(),"status":"Trading","contract_type":"LinearPerpetual","quote_coin":"USDT","settle_coin":"USDT","tick_size":"0.1","qty_step":"0.001","min_order_qty":"0.001","max_order_qty":"1000","min_notional":"5","min_leverage":"1","max_leverage":"100","leverage_step":"0.01"})

    resp = client.post(
        "/api/v1/recommendations/R-iteration105-missing-data/action",
        json={"action": "executed", "operator": "tester"},
        headers={"X-API-Key": "test-admin-key"},
    )

    assert resp.status_code == 409
    assert "execution blocked by preflight checks" in resp.json()["detail"]
    assert "MISSING_CANDLE_DATA" in resp.json()["detail"]
    assert "MISSING_TICKER_DATA" in resp.json()["detail"]
    assert db.get_bot_by_origin_rec(conn, "R-iteration105-missing-data") is None


# Даже если рекомендация была опубликована ранее, execute-path должен повторно уважать текущий market-shock lockdown.
def test_execute_recommendation_is_blocked_by_current_market_shock_state(isolated_app_and_conn, monkeypatch: pytest.MonkeyPatch):
    app_main, client, conn = isolated_app_and_conn
    ts_now = int(time.time())
    _insert_recommendation(conn, rec_id="R-iteration105-market-lock", ts_now=ts_now)

    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts_now - 60,
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "volume": 10.0,
        }],
    )
    db.insert_tickers(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "ts": ts_now - 30,
            "last": 100.5,
            "bid": 100.4,
            "ask": 100.6,
            "vol24h": 1000.0,
            "turnover24h": 100000.0,
        }],
    )
    db.insert_features(conn, "linear", "BTCUSDT", ts_now - 30, {"volume_z": 0.1})
    db.set_app_config_json(
        conn,
        app_main.MARKET_SHOCK_APP_KEY,
        {
            "state": "red_down",
            "title": "Жёсткий риск-режим",
            "severity": "critical",
            "entry_mode": "blocked",
            "operator_note": "Новые входы запрещены.",
            "reasons": [{"code": "VOL_SPIKE", "msg": "резкий рост волатильности"}],
            "metrics": {},
        },
    )
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: {"category":"linear","symbol":str(symbol or "BTCUSDT").upper(),"status":"Trading","contract_type":"LinearPerpetual","quote_coin":"USDT","settle_coin":"USDT","tick_size":"0.1","qty_step":"0.001","min_order_qty":"0.001","max_order_qty":"1000","min_notional":"5","min_leverage":"1","max_leverage":"100","leverage_step":"0.01"})

    resp = client.post(
        "/api/v1/recommendations/R-iteration105-market-lock/action",
        json={"action": "executed", "operator": "tester"},
        headers={"X-API-Key": "test-admin-key"},
    )

    assert resp.status_code == 409
    assert "execution blocked by preflight checks" in resp.json()["detail"]
    assert "MARKET_LOCKDOWN" in resp.json()["detail"]
    assert db.get_bot_by_origin_rec(conn, "R-iteration105-market-lock") is None


# UI/API должны явно подсвечивать, что план не ложится на тик Bybit и что размер шага сетки схлопнется после округления.
def test_reco_details_exposes_bybit_plan_validation_warnings_and_errors(isolated_app_and_conn, monkeypatch: pytest.MonkeyPatch):
    app_main, client, conn = isolated_app_and_conn
    ts_now = int(time.time())
    _insert_recommendation(conn, rec_id="R-iteration105-bybit-validate", ts_now=ts_now)

    monkeypatch.setattr(
        app_main,
        "_fetch_bybit_instrument_meta",
        lambda venue, symbol: {
            "category": "linear",
            "symbol": symbol,
            "tick_size": "0.25",
            "min_price": "1",
            "max_price": "1000000",
            "min_order_qty": "0.001",
            "qty_step": "0.001",
            "min_notional": "5",
            "max_leverage": "3",
        },
    )

    resp = client.get("/api/v1/recommendations/R-iteration105-bybit-validate")

    assert resp.status_code == 200
    body = resp.json()
    validation = body["bybit_plan_validation"]
    assert validation["ok"] is False
    error_codes = {item["code"] for item in validation["errors"]}
    warning_codes = {item["code"] for item in validation["warnings"]}
    assert "GRID_STEP_BELOW_TICK" in error_codes
    assert "PRICE_OFF_TICK" in warning_codes
    assert "SIZE_INPUT_REQUIRED" in warning_codes
    assert validation["snapped_levels"]["reference_price"] == "100.00"
    assert validation["snapped_levels"]["grid_step_abs"] == "0.25"
