from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import db
from app.outcomes import BOT_HORIZONS
from app.recommender import _find_open_publication_position


@pytest.fixture()
def isolated_app_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "iteration107.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: {})

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app, raise_server_exceptions=False)
    try:
        yield app_main, client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


def _insert_reco(
    conn,
    *,
    rec_id: str,
    ts_now: int,
    status: str,
    publication_root_rec_id: str | None = None,
    ttl_sec: int = 1800,
    features_ref_ts: int | None = None,
) -> None:
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
                            "range": {"lower": 99.0, "upper": 101.0},
                            "kill_switch": {"lower": 98.5, "upper": 101.5},
                            "grid_step": {"step_abs": 0.25},
                        },
                    },
                },
                "reasons": {},
                "blocks": [],
                "status": status,
                "ttl_sec": ttl_sec,
                "model_version": "test",
                "features_ref_ts": ts_now if features_ref_ts is None else features_ref_ts,
                "publication_root_rec_id": publication_root_rec_id or rec_id,
                "is_outcome_label_root": publication_root_rec_id is None,
            }
        ],
    )


# Исполненная publication-chain не должна делать idempotent-reuse для уже протухшей active-записи.
# Иначе оператор может случайно "исполнить" старое обновление и потерять честную TTL-семантику.
def test_materialize_chain_reuse_rejects_expired_active_recommendation(isolated_app_and_conn):
    app_main, _client, conn = isolated_app_and_conn
    now = int(time.time())

    _insert_reco(conn, rec_id="R-root", ts_now=now - 120, status="executed")
    db.insert_bot_instance(
        conn,
        {
            "bot_id": "B-root-running",
            "started_ts": now - 110,
            "stopped_ts": None,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "mode": {"account_mode": "unified", "margin_mode": "isolated", "direction": "long"},
            "params": {"grid_levels": 8},
            "state": {"created_from_rec_id": "R-root"},
            "status": "running",
            "origin_rec_id": "R-root",
        },
    )
    _insert_reco(
        conn,
        rec_id="R-active-expired",
        ts_now=now - 7200,
        status="active",
        publication_root_rec_id="R-root",
        ttl_sec=300,
    )

    with pytest.raises(HTTPException, match="recommendation already expired"):
        app_main._materialize_bot_from_rec(conn, "R-active-expired", "tester")

    refreshed = db.get_recommendation_by_id(conn, "R-active-expired")
    assert refreshed["status"] == "expired"
    assert db.get_bot_by_origin_rec(conn, "R-active-expired") is None


# Same publication-chain не должна обходить status-machine: pending/ignored/suppressed остаются неисполняемыми,
# даже если по root уже есть живой бот.
def test_materialize_chain_reuse_respects_non_actionable_statuses(isolated_app_and_conn):
    app_main, _client, conn = isolated_app_and_conn
    now = int(time.time())

    _insert_reco(conn, rec_id="R-root-2", ts_now=now - 120, status="executed")
    db.insert_bot_instance(
        conn,
        {
            "bot_id": "B-root-running-2",
            "started_ts": now - 110,
            "stopped_ts": None,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "mode": {"account_mode": "unified", "margin_mode": "isolated", "direction": "long"},
            "params": {"grid_levels": 8},
            "state": {"created_from_rec_id": "R-root-2"},
            "status": "running",
            "origin_rec_id": "R-root-2",
        },
    )
    _insert_reco(
        conn,
        rec_id="R-pending-chain",
        ts_now=now - 60,
        status="pending",
        publication_root_rec_id="R-root-2",
        ttl_sec=1800,
    )

    with pytest.raises(HTTPException, match="status=pending cannot be executed"):
        app_main._materialize_bot_from_rec(conn, "R-pending-chain", "tester")

    refreshed = db.get_recommendation_by_id(conn, "R-pending-chain")
    assert refreshed["status"] == "pending"
    assert db.get_bot_by_origin_rec(conn, "R-pending-chain") is None


# Execution-time Bybit validation должна ловить не только off-tick, но и внутренне противоречивую геометрию сетки.
def test_validate_trade_plan_detects_reference_and_kill_switch_geometry_errors(isolated_app_and_conn):
    app_main, _client, _conn = isolated_app_and_conn

    rec = {
        "venue": "linear",
        "params": {
            "trade_plan": {
                "reference_price": 102.0,
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 99.2, "upper": 100.8},
                    "grid_step": {"step_abs": 0.5},
                },
            }
        },
    }
    meta = {
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "min_notional": "5",
        "max_leverage": "10",
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta)
    error_codes = {item["code"] for item in validation["errors"]}

    assert validation["ok"] is False
    assert "REFERENCE_OUTSIDE_RANGE" in error_codes
    assert "KILL_SWITCH_INSIDE_MAIN_RANGE" in error_codes


# После выравнивания по tick_size сетка может схлопнуться, даже если исходный payload выглядит "почти нормальным".
def test_validate_trade_plan_detects_grid_collapse_after_tick_rounding(isolated_app_and_conn):
    app_main, _client, _conn = isolated_app_and_conn

    rec = {
        "venue": "linear",
        "params": {
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 99.96, "upper": 100.04},
                    "kill_switch": {"lower": 99.80, "upper": 100.20},
                    "grid_step": {"step_abs": 0.09},
                },
            }
        },
    }
    meta = {
        "tick_size": "0.10",
        "min_price": "1",
        "max_price": "1000000",
        "min_notional": "5",
        "max_leverage": "10",
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta)
    error_codes = {item["code"] for item in validation["errors"]}

    assert validation["ok"] is False
    assert "GRID_STEP_BELOW_TICK" in error_codes
    assert "RANGE_COLLAPSES_AFTER_TICK_ROUNDING" in error_codes or "GRID_TOO_FEW_TICK_LEVELS" in error_codes


# Publication lock должен жить до реального pseudo-entry candle, а не до формального features_ref_ts+60.
def test_open_position_lock_uses_actual_first_tradeable_candle_after_signal(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "iteration107-open-lock.db"))
    try:
        db.init_db(conn)
        signal_ts = 1_700_500_000
        first_tradeable_ts = signal_ts + 3600
        horizon = int(BOT_HORIZONS["futures_grid"])

        _insert_reco(
            conn,
            rec_id="R-open-root",
            ts_now=signal_ts,
            status="recommended",
            ttl_sec=3600,
            features_ref_ts=signal_ts,
        )
        db.upsert_ohlcv(
            conn,
            [
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "tf_sec": 60,
                    "ts": first_tradeable_ts,
                    "open": 100.0,
                    "high": 100.2,
                    "low": 99.8,
                    "close": 100.1,
                    "volume": 10.0,
                }
            ],
        )

        ts_now = signal_ts + 60 + horizon + 300
        prev = _find_open_publication_position(
            conn,
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
            },
            ts_now,
            fallback_horizon_sec=horizon,
        )

        assert prev is not None
        assert prev["lock_until_ts"] == first_tradeable_ts + horizon
        assert prev["lock_until_ts"] > ts_now
    finally:
        conn.close()
