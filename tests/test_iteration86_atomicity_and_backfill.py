from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db


class _DummyClient:
    pass




def test_collect_backfill_cycle_keeps_legacy_stub_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "backfill_legacy_stub.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    conn = db.connect(str(db_path))
    db.init_db(conn)
    try:
        def _legacy_collect_backfill_once(conn, client, venue, symbols):
            return {"venue": venue, "symbols_total": len(symbols)}

        monkeypatch.setattr(app_main, "collect_backfill_once", _legacy_collect_backfill_once)
        out = app_main._collect_backfill_cycle(
            conn,
            _DummyClient(),
            "linear",
            ["BTCUSDT"],
            heartbeat=lambda: True,
            max_workers=1,
        )
        assert out["legacy_stub"] is True
        assert out["venue"] == "linear"
        assert out["symbols_total"] == 1
    finally:
        conn.close()
        sys.modules.pop("app.main", None)

def test_collect_backfill_cycle_reraises_internal_typeerror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "backfill_typeerror.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    conn = db.connect(str(db_path))
    db.init_db(conn)
    try:
        def _broken_collect_backfill_once(*args, **kwargs):
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'int'")

        monkeypatch.setattr(app_main, "collect_backfill_once", _broken_collect_backfill_once)

        with pytest.raises(TypeError, match="unsupported operand type"):
            app_main._collect_backfill_cycle(
                conn,
                _DummyClient(),
                "linear",
                ["BTCUSDT"],
                heartbeat=lambda: True,
                max_workers=1,
            )
    finally:
        conn.close()
        sys.modules.pop("app.main", None)


@pytest.fixture()
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "atomicity.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

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


def test_api_update_risk_limits_rolls_back_when_audit_log_fails(isolated_app, monkeypatch: pytest.MonkeyPatch):
    app_main, client, conn = isolated_app

    def _boom(*args, **kwargs):
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(app_main.db, "log_decision", _boom)
    baseline_limits = db.get_active_risk_limits(conn)
    baseline_logs = conn.execute("SELECT COUNT(*) AS c FROM decision_log").fetchone()["c"]

    resp = client.post(
        "/api/v1/risk/limits",
        json={
            "version": "v-audit-fail",
            "limits": {
                "max_concurrent_bots": 3,
                "max_daily_dd_usdt": 150.0,
                "cooldown_after_loss_min": 15,
                "max_symbol_bots": 1,
            },
        },
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 500
    assert db.get_active_risk_limits(conn) == baseline_limits
    n_logs = conn.execute("SELECT COUNT(*) AS c FROM decision_log").fetchone()["c"]
    assert n_logs == baseline_logs


def test_api_sentiment_put_rolls_back_when_audit_log_fails(isolated_app, monkeypatch: pytest.MonkeyPatch):
    app_main, client, conn = isolated_app

    def _boom(*args, **kwargs):
        raise RuntimeError("audit sink unavailable")

    monkeypatch.setattr(app_main.db, "log_decision", _boom)
    baseline_logs = conn.execute("SELECT COUNT(*) AS c FROM decision_log").fetchone()["c"]

    resp = client.post(
        "/api/v1/sentiment",
        json={
            "scope": "global",
            "key": "crypto",
            "ts": 1_700_000_000,
            "sentiment": 0.25,
            "velocity": 0.1,
            "volume": 3,
            "sources": {"rss": 2},
            "tags": ["macro"],
        },
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 500
    items = db.get_sentiment_series(conn, "global", "crypto", limit=10)
    assert items == []
    n_logs = conn.execute("SELECT COUNT(*) AS c FROM decision_log").fetchone()["c"]
    assert n_logs == baseline_logs
