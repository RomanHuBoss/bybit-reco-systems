from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/ui/static/app.js"


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration183.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration183_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _operator_sheet_only_rec() -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "params": {
            "grid_count": 10,
            "operator_sheet": {
                "price_ref": 100.0,
                "range_lower": 80.0,
                "range_upper": 150.0,
                "kill_switch": {"lower": 70.0, "upper": 160.0},
                "tp_per_leg": {"abs": 3.0, "pct": 3.0},
                "estimated_position_qty": 5.0,
            },
        },
    }


def test_directional_exit_payload_reads_operator_sheet_price_context_fail_closed_display(app_main) -> None:
    """Backend display TP/SL must use the same operator-sheet price source as UI.

    Strict execution preflight can still block a legacy/manual payload for missing
    full trade_plan, but API/UI display must not invert or blank directional exits
    when the operator sheet already carries reference and kill-switch levels.
    """
    payload = app_main._directional_exit_payload_for_reco(_operator_sheet_only_rec())

    assert payload["reference_price"] == pytest.approx(100.0)
    assert payload["kill_switch_lower"] == pytest.approx(70.0)
    assert payload["kill_switch_upper"] == pytest.approx(160.0)
    assert payload["take_profit"] == pytest.approx(70.0)
    assert payload["stop_loss"] == pytest.approx(160.0)
    assert payload["geometry_valid"] is True
    assert payload["geometry_errors"] == []
    assert payload["trade_math"]["gross_profit_usdt"] == pytest.approx(150.0)
    assert payload["trade_math"]["gross_loss_usdt"] == pytest.approx(300.0)


def test_strict_preflight_still_blocks_operator_sheet_only_payload_without_full_trade_plan(app_main) -> None:
    meta = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.005",
        "max_order_qty": "100",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "10",
        "leverage_step": "0.1",
    }
    rec = _operator_sheet_only_rec()
    rec["account_mode"] = "unified"
    rec["margin_mode"] = "isolated"
    rec["params"]["operator_sheet"]["leverage"] = 2.0
    rec["params"]["operator_sheet"]["sizing"] = {"order_qty": 0.1, "order_notional_usdt": 10.0}
    rec["params"]["operator_sheet"]["economics"] = {
        "net_profit_bps": 4.0,
        "gross_profit_bps": 20.0,
        "execution_cost_bps": 5.0,
        "funding_cost_bps": 0.0,
        "estimated_active_orders": 10,
        "estimated_total_order_notional_usdt": 100.0,
        "estimated_margin_required_usdt": 50.0,
    }

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta, require_meta=True, require_execution_plan=True)
    error_codes = {item["code"] for item in validation["errors"]}

    assert "TRADE_PLAN_MISSING" in error_codes
    assert "TRADE_PLAN_REFERENCE_PRICE_MISSING" not in error_codes
    assert "TRADE_PLAN_KILL_SWITCH_LOWER_MISSING" not in error_codes
    assert "TRADE_PLAN_KILL_SWITCH_UPPER_MISSING" not in error_codes


def test_operator_ui_uses_operator_sheet_kill_switch_fallback_for_legacy_payloads() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "const operatorSheetKillSwitch =" in app_js
    assert re.search(r"firstFiniteValue\(\[ks,\s*operatorSheetKillSwitch\]", app_js)
