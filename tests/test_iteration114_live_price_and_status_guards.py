from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

from app import db


@pytest.fixture()
def isolated_app_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "iteration114.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration114_runtime_lock.db"))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("STALE_DATA_MAX_SEC", "120")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    try:
        yield app_main, conn
    finally:
        conn.close()
        sys.modules.pop("app.main", None)


def _meta(**overrides):
    meta = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "min_order_qty": "0.001",
        "qty_step": "0.001",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "10",
        "leverage_step": "0.1",
    }
    meta.update(overrides)
    return meta


def _insert_reco(conn, *, rec_id: str, ts_now: int, lower: float = 99.0, upper: float = 101.0) -> None:
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
                        "reference_price": 100.0,
                        "levels": {
                            "range": {"lower": lower, "upper": upper},
                            "kill_switch": {"lower": lower - 0.5, "upper": upper + 0.5},
                            "grid_step": {"step_abs": 0.25},
                            "tp_per_leg": {"abs": 0.30, "pct": 0.30},
                        },
                    },
                },
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_now,
                "publication_root_rec_id": rec_id,
                "is_outcome_label_root": True,
            }
        ],
    )


# Instrument metadata со status != Trading нельзя считать исполнимой, даже если price/qty-фильтры корректны.
def test_bybit_instrument_status_is_fail_closed(isolated_app_and_conn):
    app_main, _conn = isolated_app_and_conn
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_levels": 20,
            "leverage": 2,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.5},
                    "tp_per_leg": {"abs": 0.33, "pct": 0.33},
                },
            },
        },
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(status="PreLaunch"))

    error_codes = {item["code"] for item in validation["errors"]}
    warning_codes = {item["code"] for item in validation["warnings"]}
    assert validation["ok"] is False
    assert "BYBIT_INSTRUMENT_NOT_TRADING" in error_codes
    assert "TP_PER_LEG_OFF_TICK" in warning_codes
    assert "GRID_STEP_LEVELS_MISMATCH" in warning_codes
    assert validation["snapped_levels"]["tp_per_leg_abs"] == "0.3"


# Execute-path обязан сверять текущий ticker с сохранённым диапазоном сетки, а не только freshness данных.
def test_execution_preflight_blocks_live_price_outside_grid_range(isolated_app_and_conn):
    app_main, conn = isolated_app_and_conn
    now = int(time.time())
    _insert_reco(conn, rec_id="R-live-price-drift", ts_now=now - 10)
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": now - 30,
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
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
                "ts": now - 10,
                "last": 103.0,
                "bid": 102.9,
                "ask": 103.1,
                "vol24h": 1000.0,
                "turnover24h": 100000.0,
            }
        ],
    )
    db.insert_features(conn, "linear", "BTCUSDT", now - 10, {"volume_z": 0.1})
    db.set_app_config_json(
        conn,
        app_main.MARKET_SHOCK_APP_KEY,
        {
            "state": "normal",
            "title": "Нормальный режим",
            "severity": "normal",
            "entry_mode": "normal",
            "operator_note": "Новые входы разрешены.",
            "reasons": [],
            "metrics": {},
        },
    )

    rec = db.get_recommendation_by_id(conn, "R-live-price-drift")
    preflight = app_main._execution_preflight(conn, rec, now_ts=now, bybit_meta=_meta())

    codes = {block["code"] for block in preflight["blocks"]}
    assert "MISSING_CANDLE_DATA" not in codes
    assert "MISSING_TICKER_DATA" not in codes
    assert "STALE_CANDLE_DATA" not in codes
    assert "STALE_TICKER_DATA" not in codes
    assert "CURRENT_PRICE_OUTSIDE_GRID_RANGE" in codes
    assert "CURRENT_PRICE_OUTSIDE_KILL_SWITCH" in codes
