from __future__ import annotations

import importlib
import json
import math
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.outcomes import _grid_outcome


@pytest.fixture()
def client_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "iter94.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app)
    try:
        yield client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


def test_bootstrap_persists_effective_normalized_risk_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap не должен писать в БД raw risk JSON, который runtime потом молча clamp'ит."""
    db_path = tmp_path / "bootstrap-risk.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv(
        "RISK_LIMITS_JSON",
        '{"max_concurrent_bots":"oops","max_daily_dd_usdt":-15,"cooldown_after_loss_min":"bad","max_symbol_bots":0,"ignored":123}',
    )

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    try:
        assert db.get_active_risk_limits(conn) == {
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 0.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
            "max_leverage": 3,
            "max_position_notional_usdt": 5000.0,
            "max_margin_per_bot_usdt": 1000.0,
        }
    finally:
        conn.close()
        sys.modules.pop("app.main", None)


def test_api_update_risk_limits_persists_and_returns_effective_limits(client_and_conn) -> None:
    """Mutating API должен синхронно возвращать и сохранять те же effective limits, что и runtime."""
    client, conn = client_and_conn

    payload = {
        "version": "iter94-normalized",
        "limits": {
            "max_concurrent_bots": "oops",
            "max_daily_dd_usdt": -15.0,
            "cooldown_after_loss_min": "bad",
            "max_symbol_bots": 0,
            "ignored": 999,
        },
    }
    resp = client.post(
        "/api/v1/risk/limits",
        json=payload,
        headers={"X-API-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "ok": True,
        "version": "iter94-normalized",
        "limits": {
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 0.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
            "max_leverage": 3,
            "max_position_notional_usdt": 5000.0,
            "max_margin_per_bot_usdt": 1000.0,
        },
    }

    assert db.get_active_risk_limits(conn) == body["limits"]

    row = conn.execute(
        "SELECT details_json FROM decision_log WHERE action='UPDATE_LIMITS' ORDER BY ts DESC, rowid DESC LIMIT 1"
    ).fetchone()
    details = json.loads(row["details_json"])
    assert details["limits"] == body["limits"]
    assert details["raw_limits"] == payload["limits"]


def test_grid_outcome_ignores_poisoned_top_level_range_bounds_and_uses_trade_plan_fallback(tmp_path: Path) -> None:
    """NaN/Infinity в верхнеуровневых границах не должны отключать валидные range/kill-switch из trade_plan."""
    conn = db.connect(str(tmp_path / "grid-bounds.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_000_000
        rows = []
        for i in range(360):
            ts = base_ts + 60 + i * 60
            close = 100.5 if i % 2 == 0 else 99.5
            rows.append(
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "tf_sec": 60,
                    "ts": ts,
                    "open": close,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": 1_000.0,
                }
            )
        db.upsert_ohlcv(conn, rows)

        good_params = {
            "grid_levels": 20,
            "grid_spacing_pct": 0.4,
            "trade_plan": {
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                }
            },
        }
        poisoned_params = {
            **good_params,
            "price_range_lower": "NaN",
            "price_range_upper": "Infinity",
        }

        good_success, good_ret = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts + 60,
            base_ts + 60 + 360 * 60,
            "neutral",
            good_params,
        )
        poisoned_success, poisoned_ret = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts + 60,
            base_ts + 60 + 360 * 60,
            "neutral",
            poisoned_params,
        )

        assert good_success == 1
        assert good_ret > 0.0
        assert math.isfinite(good_ret)
        assert poisoned_success == good_success
        assert poisoned_ret == pytest.approx(good_ret)
    finally:
        conn.close()
