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
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", lambda venue, symbol: {"category":"linear","symbol":str(symbol or "BTCUSDT").upper(),"status":"Trading","contract_type":"LinearPerpetual","quote_coin":"USDT","settle_coin":"USDT","tick_size":"0.1","qty_step":"0.001","min_order_qty":"0.001","max_order_qty":"1000","min_notional":"5","min_leverage":"1","max_leverage":"100","leverage_step":"0.01"})

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
                            "grid_step": {"step_abs": 0.2},
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


# Validation должна ловить не только ценовую геометрию, но и режимные противоречия
# recommendation: неподдерживаемый margin_mode и leverage вне шага/границ Bybit не должны
# доходить до operator execution как будто это исполнимый futures-grid.
def test_validate_trade_plan_detects_mode_and_leverage_constraint_errors(isolated_app_and_conn):
    app_main, _client, _conn = isolated_app_and_conn

    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "leverage": 2.15,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.5},
                },
            },
        },
    }
    meta = {
        "category": "linear",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "min_notional": "5",
        "min_leverage": "2.5",
        "max_leverage": "10",
        "leverage_step": "0.1",
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta)
    error_codes = {item["code"] for item in validation["errors"]}

    assert validation["ok"] is False
    assert "MARGIN_MODE_UNSUPPORTED" in error_codes
    assert "LEVERAGE_BELOW_MIN" in error_codes
    assert "LEVERAGE_OFF_STEP" in error_codes
    assert validation["snapped_levels"]["leverage"] == "2.2"


# Execute-path не должен держать SQLite write-lock во время сетевого запроса за Bybit metadata.
# Иначе медленный upstream блокирует collector/recommender и создаёт ложные `database is locked`.
def test_materialize_prefetches_bybit_meta_before_begin_immediate(isolated_app_and_conn, monkeypatch: pytest.MonkeyPatch):
    app_main, _client, conn = isolated_app_and_conn
    now = int(time.time())

    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": now - 60,
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
                "ts": now - 15,
                "last": 100.5,
                "bid": 100.4,
                "ask": 100.6,
                "vol24h": 1000.0,
                "turnover24h": 100000.0,
            }
        ],
    )
    db.insert_features(conn, "linear", "BTCUSDT", now - 15, {"volume_z": 0.1})

    _insert_reco(conn, rec_id="R-prefetch", ts_now=now - 5, status="recommended")

    state = {"begin_called": False, "fetch_called": False}
    orig_begin = app_main.db.begin_immediate

    def tracked_begin(db_conn):
        state["begin_called"] = True
        return orig_begin(db_conn)

    def tracked_fetch(venue: str, symbol: str) -> dict[str, object]:
        state["fetch_called"] = True
        assert state["begin_called"] is False
        return {
            "category": "linear",
            "symbol": symbol,
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
        }

    monkeypatch.setattr(app_main.db, "begin_immediate", tracked_begin)
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", tracked_fetch)

    bot, idempotent = app_main._materialize_bot_from_rec(conn, "R-prefetch", operator="tester")

    assert state["fetch_called"] is True
    assert state["begin_called"] is True
    assert idempotent is False
    assert bot["origin_rec_id"] == "R-prefetch"


# Legacy/manual recommendation без margin_mode больше не проходит execution-time validation молча.
# Для recommendation-only сервиса безопаснее fail-closed, чем исполнить сетку в неявном режиме.
def test_validate_trade_plan_blocks_missing_margin_mode_for_futures_grid(isolated_app_and_conn):
    app_main, _client, _conn = isolated_app_and_conn

    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "params": {
            "leverage": 2,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.5},
                },
            },
        },
    }
    meta = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "min_leverage": "1",
        "max_leverage": "10",
        "leverage_step": "0.1",
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta)
    error_codes = {item["code"] for item in validation["errors"]}

    assert validation["ok"] is False
    assert "MARGIN_MODE_MISSING" in error_codes


# Если metadata Bybit относится к другому symbol, ею нельзя валидировать чужую рекомендацию.
# Такой mismatch должен блокировать execution preflight, иначе ограничения tick/leverage будут недостоверны.
def test_validate_trade_plan_blocks_bybit_meta_symbol_mismatch(isolated_app_and_conn):
    app_main, _client, _conn = isolated_app_and_conn

    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "leverage": 2,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.5},
                },
            },
        },
    }
    meta = {
        "category": "linear",
        "symbol": "ETHUSDT",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "min_leverage": "1",
        "max_leverage": "10",
        "leverage_step": "0.1",
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta)
    error_codes = {item["code"] for item in validation["errors"]}

    assert validation["ok"] is False
    assert "BYBIT_META_SYMBOL_MISMATCH" in error_codes


# Execution API не должен держать SQLite write-lock во время сетевого prefetch Bybit metadata.
# Иначе медленный upstream способен подвесить весь writer-контур (collector/recommender/operator actions).
def test_api_execute_prefetches_bybit_metadata_before_begin_immediate(isolated_app_and_conn, monkeypatch: pytest.MonkeyPatch):
    app_main, client, conn = isolated_app_and_conn
    now = int(time.time())

    _insert_reco(conn, rec_id="R-prefetch-lock-order", ts_now=now, status="recommended")

    begin_calls: list[bool] = []
    seen_in_transaction: list[bool] = []
    real_begin = app_main.db.begin_immediate

    def tracking_begin(tracked_conn):
        begin_calls.append(bool(getattr(tracked_conn, "in_transaction", False)))
        return real_begin(tracked_conn)

    def tracking_prefetch(tracked_conn, rec_id):
        seen_in_transaction.append(bool(getattr(tracked_conn, "in_transaction", False)))
        return {}

    monkeypatch.setattr(app_main.db, "begin_immediate", tracking_begin)
    monkeypatch.setattr(app_main, "_prefetch_execution_bybit_meta", tracking_prefetch)
    monkeypatch.setattr(
        app_main,
        "_execution_preflight",
        lambda *_args, **_kwargs: {
            "blocks": [],
            "market_shock": {"state": "normal"},
            "fast_veto": {"blocked": False},
            "bybit_validation": {"ok": True, "errors": [], "warnings": [], "snapped_levels": {}},
        },
    )

    response = client.post(
        "/api/v1/recommendations/R-prefetch-lock-order/action",
        json={"action": "executed", "operator": "tester"},
        headers={"X-API-Key": "test-admin-key"},
    )

    assert response.status_code == 200, response.text
    assert seen_in_transaction == [False]
    assert begin_calls == [False]


# Категория metadata Bybit должна совпадать с venue recommendation.
# Иначе linear-ограничения можно ошибочно применить к чужой категории инструмента.
def test_validate_trade_plan_blocks_bybit_category_mismatch(isolated_app_and_conn):
    app_main, _client, _conn = isolated_app_and_conn

    rec = {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
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
    }
    meta = {
        "category": "nonlinear",
        "symbol": "BTCUSDT",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "10",
        "leverage_step": "0.1",
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta)
    error_codes = {item["code"] for item in validation["errors"]}

    assert validation["ok"] is False
    assert "BYBIT_META_CATEGORY_MISMATCH" in error_codes



def test_execution_preflight_requires_bybit_meta_fail_closed(isolated_app_and_conn):
    app_main, _client, conn = isolated_app_and_conn
    ts_now = int(time.time())
    _insert_reco(conn, rec_id="R-missing-meta", ts_now=ts_now, status="recommended")
    rec = db.get_recommendation_by_id(conn, "R-missing-meta")

    preflight = app_main._execution_preflight(conn, rec, now_ts=ts_now, bybit_meta={})
    codes = {item["code"] for item in preflight["blocks"]}

    assert "BYBIT_META_UNAVAILABLE" in codes



def test_validate_trade_plan_rejects_non_usdt_linear_perpetual_domain(isolated_app_and_conn):
    app_main, _client, _conn = isolated_app_and_conn
    rec = {
        "venue": "linear",
        "symbol": "BTCUSDC",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {"grid_levels": 8, "leverage": 2, "margin_mode": "isolated"},
    }
    meta = {
        "category": "linear",
        "symbol": "BTCUSDC",
        "status": "Trading",
        "contract_type": "LinearFutures",
        "quote_coin": "USDC",
        "settle_coin": "USDC",
        "tick_size": "0.1",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta, require_meta=True)
    codes = {item["code"] for item in validation["errors"]}

    assert "USDT_PERPETUAL_SYMBOL_REQUIRED" in codes
    assert "BYBIT_CONTRACT_TYPE_UNSUPPORTED" in codes
    assert "BYBIT_QUOTE_COIN_UNSUPPORTED" in codes
    assert "BYBIT_SETTLE_COIN_UNSUPPORTED" in codes
