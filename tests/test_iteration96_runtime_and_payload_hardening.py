from __future__ import annotations

import importlib
import math
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from conftest import safe_linear_grid_params
from app.recommender import _build_trade_plan, _estimate_cost_model


@pytest.fixture()
def isolated_app_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "iteration96.db"
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
    client = TestClient(app_main.app, raise_server_exceptions=False)
    try:
        yield app_main, client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


# Идемпотентный execute-path не должен оставлять открытую write-транзакцию,
# иначе следующий writer может упереться в лишний SQLite lock даже без реальных изменений.
def test_materialize_existing_bot_closes_transaction_on_idempotent_reuse(isolated_app_and_conn):
    app_main, _client, conn = isolated_app_and_conn
    ts_now = int(time.time())

    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-iteration96-idempotent",
                "ts": ts_now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "cross",
                "score": 0.44,
                "confidence": 0.71,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": safe_linear_grid_params({"grid_levels": 8}),
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
            }
        ],
    )

    first_bot, first_existed = app_main._materialize_bot_from_rec(conn, "R-iteration96-idempotent", "tester")
    assert first_existed is False
    assert conn.in_transaction is False

    second_bot, second_existed = app_main._materialize_bot_from_rec(conn, "R-iteration96-idempotent", "tester")
    assert second_existed is True
    assert second_bot["bot_id"] == first_bot["bot_id"]
    assert conn.in_transaction is False


# Операторские теги не должны принимать NUL-байт: такой payload трудно диагностировать
# и он создаёт несогласованность между UI, экспортом и SQL-фильтрами.
def test_api_sentiment_put_rejects_nul_in_tags(isolated_app_and_conn):
    _app_main, client, conn = isolated_app_and_conn

    resp = client.post(
        "/api/v1/sentiment",
        headers={"X-API-Key": "test-admin-key"},
        json={
            "scope": "global",
            "key": "crypto",
            "ts": 1_700_400_000,
            "sentiment": 0.25,
            "velocity": 0.01,
            "volume": 2,
            "sources": {"manual": True},
            "tags": ["desk\u0000ops", "clean"],
        },
    )

    assert resp.status_code == 422
    assert "NUL byte" in resp.text
    assert db.get_sentiment_series(conn, "global", "crypto", limit=10) == []


# GET-фильтры должны нормализоваться так же строго, как mutating API; иначе оператор
# получает тихий miss вместо понятной ошибки на испорченный query string.
def test_api_sentiment_get_rejects_nul_in_scope_or_key(isolated_app_and_conn):
    _app_main, client, conn = isolated_app_and_conn
    db.insert_sentiment_point(conn, "global", "crypto", 1_700_400_100, 0.1, 0.0, 1, {"manual": True}, ["desk"])

    resp = client.get("/api/v1/sentiment?scope=global%00&key=crypto")

    assert resp.status_code == 422
    assert "NUL byte" in resp.text


# Non-finite funding/spread не должны превращать cost-model в NaN-пayload, иначе
# scorer/trade-plan/json-serialization получают битую рекомендацию вместо safe fallback.
def test_estimate_cost_model_falls_back_from_non_finite_spread_and_funding() -> None:
    cost_model = _estimate_cost_model(
        "futures_grid",
        "linear",
        {"spread_bps": float("nan")},
        0.06,
        "long",
        funding_rate=float("nan"),
        next_funding_ts=123_456,
        ts_now=123_000,
    )

    assert cost_model["spread_missing"] is True
    assert cost_model["spread_bps"] == 10.0
    assert cost_model["funding_rate"] is None
    assert cost_model["directional_funding_bps_8h"] == 0.0
    assert cost_model["expected_funding_bps"] == 0.0
    assert cost_model["net_cost_bps"] == pytest.approx(13.62)
    assert math.isfinite(float(cost_model["net_cost_bps"]))


# Trade-plan — operator-facing и audit-facing payload одновременно. Он не должен
# содержать NaN/Infinity даже если в legacy/manual params просочился мусор.
def test_build_trade_plan_strips_non_finite_bounds_and_cost_model_values() -> None:
    plan = _build_trade_plan(
        "futures_grid",
        "linear",
        {
            "price": 100.0,
            "atr_pct": 0.01,
            "_direction_agg": {
                "regime": "range",
                "regime_confidence": 0.8,
                "trendiness": float("nan"),
                "coherence": float("inf"),
            },
        },
        "long",
        {
            "price_range_lower": "NaN",
            "price_range_upper": "Infinity",
            "grid_spacing_pct": "NaN",
            "leverage": "2",
            "range_span_pct_total": "NaN",
        },
        cost_model={"spread_bps": float("nan"), "nested": {"expected_funding_bps": float("inf")}},
    )

    assert plan["cost_model"] == {"spread_bps": None, "nested": {"expected_funding_bps": None}}
    assert plan["levels"]["range"] == {"lower": None, "upper": None}
    assert plan["levels"]["kill_switch"]["lower"] is None
    assert plan["levels"]["kill_switch"]["upper"] is None
    assert plan["levels"]["grid_step"]["step_pct"] is None
    assert plan["levels"]["grid_step"]["step_abs"] is None
    assert plan["levels"]["tp_per_leg"]["abs"] == pytest.approx(0.25)
    assert plan["levels"]["tp_per_leg"]["pct"] == pytest.approx(0.25)
    assert plan["regime"]["trendiness"] is None
    assert plan["regime"]["coherence"] is None
    assert "span≈n/a%" in plan["notes"]
    assert "nan" not in plan["notes"].lower()
    assert "inf" not in plan["notes"].lower()
