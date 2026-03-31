from __future__ import annotations

import importlib
from contextlib import closing
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.features import oi_trend


def test_oi_trend_requires_true_lookback_history() -> None:
    short_series = [
        {"ts": 5, "oi": 125.0},
        {"ts": 4, "oi": 120.0},
        {"ts": 3, "oi": 118.0},
        {"ts": 2, "oi": 115.0},
        {"ts": 1, "oi": 110.0},
    ]
    trend = oi_trend(short_series)
    assert trend["oi_now"] == 125.0
    assert trend["oi_4h_chg_pct"] == pytest.approx(round((125.0 - 110.0) / 110.0 * 100.0, 2), abs=1e-9)
    assert trend["oi_24h_chg_pct"] is None
    assert trend["trend"] == "unknown"

    full_series = [{"ts": 25 - idx, "oi": float(100 + idx)} for idx in range(25)]
    full_trend = oi_trend(full_series)
    assert full_trend["oi_24h_chg_pct"] is not None
    assert full_trend["trend"] in {"growing", "stable", "falling"}


def test_collector_thread_stops_when_post_collect_heartbeat_loses_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "collector_post_heartbeat_loss.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    monkeypatch.setenv("VENUES", "spot")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(
            app_main.settings,
            venues=["spot"],
            symbols_spot=["BTCUSDT"],
            symbols_linear=[],
            collect_interval_sec=5,
            futures_collect_interval_sec=3600,
        )

        class DummyClient:
            def __init__(self, *args, **kwargs):
                pass
            def close(self):
                return None

        futures_calls = {"count": 0}

        def fake_collect_once(conn, client, venue, symbols, heartbeat=None, *, max_workers=1):
            return {"venue": venue, "symbols_total": len(symbols)}

        def fake_collect_futures_once(*args, **kwargs):
            futures_calls["count"] += 1
            return {"venue": "linear"}

        def dead_heartbeat(*args, **kwargs):
            return lambda: False

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        monkeypatch.setattr(app_main, "BybitPublicClient", DummyClient)
        monkeypatch.setattr(app_main, "collect_once", fake_collect_once)
        monkeypatch.setattr(app_main, "collect_futures_once", fake_collect_futures_once)
        monkeypatch.setattr(app_main, "_make_runtime_lock_heartbeat", dead_heartbeat)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._collector_thread()

        conn = db.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT details_json FROM decision_log WHERE action='COLLECT_ERROR' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            details = app_main._json_loads_or_default(row["details_json"], {})
            assert details.get("field") == "runtime_lock"
            stats = db.get_app_config_json(conn, "collector_last_cycle", default={}) or {}
            assert stats.get("lock_lost") is True
        finally:
            conn.close()
        assert futures_calls["count"] == 0
    finally:
        sys.modules.pop("app.main", None)


def test_reco_thread_skips_followup_work_after_runtime_lock_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "reco_lock_loss.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("VENUES", "spot")
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(
            app_main.settings,
            venues=["spot"],
            symbols_spot=["BTCUSDT"],
            symbols_linear=[],
            reco_interval_sec=5,
            telegram_token=None,
        )
        post_calls = {"expire": 0, "prune": 0}

        def fake_run_recommender_once(conn, settings, heartbeat=None):
            raise app_main.RuntimeLockLostError("reco heartbeat lost")

        def fake_expire(conn):
            post_calls["expire"] += 1
            return 0

        def fake_prune(conn, retain_days=7):
            post_calls["prune"] += 1
            return {}

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        with closing(db.connect(str(db_path))) as conn:
            db.set_app_config_json(conn, "collector_warmup", {"ready": True, "symbols_total": 1, "ready_symbols": 1})

        monkeypatch.setattr(app_main, "run_recommender_once", fake_run_recommender_once)
        monkeypatch.setattr(app_main.db, "expire_stale_recommendations", fake_expire)
        monkeypatch.setattr(app_main.db, "prune_old_data", fake_prune)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._reco_thread()

        assert post_calls == {"expire": 0, "prune": 0}
        conn = db.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT details_json FROM decision_log WHERE action='RECO_ERROR' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            details = app_main._json_loads_or_default(row["details_json"], {})
            assert details.get("field") == "runtime_lock"
        finally:
            conn.close()
    finally:
        sys.modules.pop("app.main", None)


def test_metrics_only_counts_active_venues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "metrics_active_venues.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    conn = db.connect(str(db_path))
    client = TestClient(app_main.app)
    try:
        ts_now = int(time.time())
        db.upsert_ohlcv(
            conn,
            [
                {"venue": "spot", "symbol": "BTCUSDT", "tf_sec": 60, "ts": ts_now, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0},
                {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": ts_now, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0},
            ],
        )
        db.insert_tickers(
            conn,
            [
                {"venue": "spot", "symbol": "BTCUSDT", "ts": ts_now, "last": 100.5, "bid": 100.0, "ask": 101.0, "vol24h": 1000.0, "turnover24h": 100000.0},
                {"venue": "linear", "symbol": "BTCUSDT", "ts": ts_now, "last": 100.5, "bid": 100.0, "ask": 101.0, "vol24h": 1000.0, "turnover24h": 100000.0},
            ],
        )

        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        assert "bybit_reco_symbols_total 1" in body
        assert "bybit_reco_symbols_ok 1" in body
        assert "bybit_reco_symbols_missing 0" in body
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)
