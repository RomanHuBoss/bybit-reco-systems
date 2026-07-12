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
    db_path = tmp_path / "iteration164_runtime_profile.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "WLDUSDT")
    monkeypatch.setenv("RECO_INTERVAL_SEC", "60")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(app_main, "_fetch_bybit_instrument_meta", _meta)

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app)
    try:
        yield client, conn, app_main
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


def _meta(_venue: str, symbol: str) -> dict[str, str]:
    return {
        "category": "linear",
        "symbol": str(symbol or "WLDUSDT").upper(),
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "tick_size": "0.0001",
        "qty_step": "0.1",
        "min_order_qty": "0.1",
        "max_order_qty": "1000000",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def _risk_limits(min_lev: int = 3, max_lev: int = 3) -> dict:
    return {
        "max_concurrent_bots": 1,
        "max_daily_dd_usdt": 30.0,
        "cooldown_after_loss_min": 90,
        "max_symbol_bots": 1,
        "min_leverage": min_lev,
        "max_leverage": max_lev,
        "max_position_notional_usdt": 5000.0,
        "max_margin_per_bot_usdt": 1000.0,
    }


def _reco(ts: int, *, leverage: int = 1, policy_min: int = 5, policy_max: int = 10, ttl: int = 3600) -> dict:
    price = 0.5149
    lower = 0.4681
    upper = 0.5725
    grid_count = 12
    total_notional = 318.00924
    active_orders = 13
    order_notional = 48.6 * price
    margin = total_notional / max(1, leverage)
    step = (upper - lower) / grid_count
    return {
        "rec_id": f"R-iteration164-{ts}",
        "publication_root_rec_id": f"R-iteration164-{ts}",
        "is_outcome_label_root": True,
        "ts": ts,
        "venue": "linear",
        "symbol": "WLDUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.91,
        "confidence": 0.92,
        "expected_rr": 1.194,
        "risk_score": 0.2,
        "params": {
            "bot_type": "futures_grid",
            "venue": "linear",
            "direction": "long",
            "price_ref": price,
            "price_range_lower": lower,
            "price_range_upper": upper,
            "grid_type": "arithmetic",
            "grid_count": grid_count,
            "grid_levels": grid_count,
            "leverage": leverage,
            "margin_mode": "isolated",
            "leverage_policy": {
                "min_operator_leverage": policy_min,
                "max_operator_leverage": policy_max,
                "selected_leverage": leverage,
                "note": "signal_quality_too_low_for_operator_minimum",
                "diagnostics": {"target_leverage": policy_min},
            },
            "trade_plan": {
                "reference_price": price,
                "grid_type": "arithmetic",
                "grid_count": grid_count,
                "leverage": leverage,
                "margin_mode": "isolated",
                "levels": {
                    "range": {"lower": lower, "upper": upper},
                    "kill_switch": {"lower": 0.4613, "upper": 0.5789},
                    "grid_step": {"step_abs": step, "step_pct": step / price * 100.0},
                    "tp_per_leg": {"abs": step, "pct": step / price * 100.0},
                },
                "sizing": {
                    "qty_per_order": 48.6,
                    "order_notional_usdt": order_notional,
                    "estimated_active_orders": active_orders,
                    "estimated_active_orders": active_orders,
                "estimated_active_orders": active_orders,
            "estimated_total_order_notional_usdt": total_notional,
                    "estimated_margin_required_usdt": margin,
                    "estimated_max_position_notional_usdt": total_notional,
                },
                "economics": {
                    "estimated_active_orders": active_orders,
                    "estimated_active_orders": active_orders,
                "estimated_active_orders": active_orders,
            "estimated_total_order_notional_usdt": total_notional,
                    "estimated_margin_required_usdt": margin,
                    "estimated_max_position_notional_usdt": total_notional,
                },
            },
            "operator_sheet": {
                "leverage": leverage,
                "sizing": {
                    "estimated_active_orders": active_orders,
                    "estimated_active_orders": active_orders,
                "estimated_active_orders": active_orders,
            "estimated_total_order_notional_usdt": total_notional,
                    "estimated_margin_required_usdt": margin,
                    "estimated_max_position_notional_usdt": total_notional,
                },
            },
            "sizing": {
                "estimated_active_orders": active_orders,
                "estimated_active_orders": active_orders,
            "estimated_total_order_notional_usdt": total_notional,
                "estimated_margin_required_usdt": margin,
                "estimated_max_position_notional_usdt": total_notional,
            },
            "economics": {
                "net_profit_bps": 4.0,
                "gross_profit_bps": 20.0,
                "estimated_active_orders": active_orders,
                "estimated_active_orders": active_orders,
            "estimated_total_order_notional_usdt": total_notional,
                "estimated_margin_required_usdt": margin,
                "estimated_max_position_notional_usdt": total_notional,
                "liquidation_buffer_pct": 100.0,
            },
            "risk_report": {
                "decision": "recommended",
                "rejection_reasons": [],
                "warnings": [],
            },
        },
        "reasons": {
            "llm_review": {"status": "ok", "mode": "advisory"},
            "risk_checks": {"passed": True, "blocks": []},
            "decision_layers": {"final_status": "recommended"},
        },
        "blocks": [],
        "status": "recommended",
        "ttl_sec": ttl,
        "model_version": "test-iteration164",
        "features_ref_ts": ts,
    }


def test_execution_runtime_guard_blocks_leverage_below_operator_minimum(client_conn_app) -> None:
    _client, _conn, app_main = client_conn_app
    rec = _reco(int(time.time()), leverage=1)

    blocks = app_main._execution_runtime_size_risk_blocks(rec, _risk_limits(3, 3))

    assert {block["code"] for block in blocks} >= {"MIN_LEVERAGE_PER_BOT_AT_EXECUTION"}


def test_operator_api_blocks_legacy_one_x_recommendation_when_current_profile_is_fixed_three_x(client_conn_app) -> None:
    client, conn, _app_main = client_conn_app
    now = int(time.time())
    db.upsert_risk_limits(conn, "fixed-3x", _risk_limits(3, 3))
    db.insert_regime(conn, now, {"vol_state": "low", "trend_state": "ranging", "risk_state": "risk_on", "confidence": 0.8})
    db.insert_recommendations(conn, [_reco(now, leverage=1, policy_min=5, policy_max=10)])

    default = client.get("/api/v1/recommendations?snapshot=latest_operator&min_conf=0")
    assert default.status_code == 200
    assert default.json()["items"] == []
    assert default.json()["no_trade"] is True

    blocked = client.get(
        "/api/v1/recommendations?snapshot=latest_operator&min_conf=0&show_recommended=false&show_blocked=true"
    )
    assert blocked.status_code == 200
    items = blocked.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "blocked"
    assert item["effective_status"] == "blocked"
    codes = {block["code"] for block in item["blocks"]}
    assert "MIN_LEVERAGE_PER_BOT_AT_EXECUTION" in codes
    assert "RUNTIME_RISK_PROFILE_CHANGED" in codes
    assert item["params"]["risk_report"]["decision"] == "not_recommended"


def test_operator_api_blocks_stale_snapshot_even_when_stored_status_is_recommended(client_conn_app) -> None:
    client, conn, _app_main = client_conn_app
    now = int(time.time())
    old_ts = now - 600
    db.upsert_risk_limits(conn, "fixed-3x", _risk_limits(3, 3))
    db.insert_regime(conn, old_ts, {"vol_state": "low", "trend_state": "ranging", "risk_state": "risk_on", "confidence": 0.8})
    db.insert_recommendations(conn, [_reco(old_ts, leverage=3, policy_min=3, policy_max=3, ttl=3600)])

    resp = client.get(
        "/api/v1/recommendations?snapshot=latest_operator&min_conf=0&show_recommended=false&show_blocked=true"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_is_stale"] is True
    item = body["items"][0]
    assert item["status"] == "blocked"
    assert item["effective_status"] == "blocked"
    assert {block["code"] for block in item["blocks"]} >= {"SNAPSHOT_STALE_FOR_OPERATOR_LAUNCH"}
