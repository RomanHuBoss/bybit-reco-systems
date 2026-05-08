from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import _existing_trade_matches_request
from app.outcomes import _resolve_effective_horizon
from app.recommender import _build_trade_plan


@pytest.fixture()
def client_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "iter101.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: {"category":"linear","symbol":str(symbol or "BTCUSDT").upper(),"status":"Trading","contract_type":"LinearPerpetual","quote_coin":"USDT","settle_coin":"USDT","tick_size":"0.1","qty_step":"0.001","min_order_qty":"0.001","max_order_qty":"1000","min_notional":"5","min_leverage":"1","max_leverage":"100","leverage_step":"0.01"})

    conn = db.connect(str(db_path))
    ts_now = int(time.time())
    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts_now - 60,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
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
    client = TestClient(app_main.app)
    try:
        yield client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


# Outcome-cycle должен переживать legacy/manual JSON с неверной формой trade_plan.
def test_resolve_effective_horizon_ignores_non_mapping_trade_plan_shapes() -> None:
    effective_horizon, used_fallback = _resolve_effective_horizon(
        "futures_grid",
        {
            "trade_plan": "broken-shape",
            "label_horizon_hours": "NaN",
        },
        900,
    )

    assert effective_horizon == 12 * 3600
    assert used_fallback is False


# Trade plan не должен падать на мусорном label_horizon_hours из ручного/legacy payload.
def test_build_trade_plan_falls_back_to_builtin_label_horizon_on_invalid_input() -> None:
    plan = _build_trade_plan(
        "futures_grid",
        "linear",
        {
            "price": 100.0,
            "atr_pct": 0.01,
            "_direction_agg": {"regime": "range", "regime_confidence": 0.6},
        },
        "long",
        {
            "label_horizon_hours": "not-a-number",
            "grid_spacing_pct": 0.8,
            "price_range_lower": 95.0,
            "price_range_upper": 105.0,
        },
        cost_model={"execution_cost_bps": 8.0},
    )

    assert plan["expected_horizon"]["label_horizon_hours"] == 12


# Исполнение рекомендации не должно падать 500 из-за битого ttl/ts в legacy строке БД.
def test_api_execute_handles_poisoned_ttl_and_timestamp_values(client_and_conn) -> None:
    client, conn = client_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-iter101-poisoned-ttl",
                "ts": "broken-ts",
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.45,
                "confidence": 0.7,
                "expected_rr": 1.1,
                "risk_score": 0.2,
                "params": {"grid_levels": 8, "grid_spacing_pct": 0.7},
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": "broken-ttl",
                "model_version": "test",
                "features_ref_ts": ts_now,
            }
        ],
    )

    resp = client.post(
        "/api/v1/recommendations/R-iter101-poisoned-ttl/action",
        json={"action": "executed", "operator": "tester"},
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["new_status"] == "executed"
    assert body["idempotent"] is False


# Идемпотентная проверка trade_id не должна падать на повреждённой historical row.
def test_existing_trade_match_returns_false_for_poisoned_timestamp() -> None:
    assert _existing_trade_matches_request(
        {
            "trade_id": "T-1",
            "bot_id": "B-1",
            "symbol": "BTCUSDT",
            "ts": "broken-ts",
            "pnl": 10.0,
            "fee": 1.0,
            "meta": {"fill_count": 1},
        },
        bot_id="B-1",
        symbol="BTCUSDT",
        ts=1_700_000_000,
        pnl=10.0,
        fee=1.0,
        meta={"fill_count": 1},
    ) is False
