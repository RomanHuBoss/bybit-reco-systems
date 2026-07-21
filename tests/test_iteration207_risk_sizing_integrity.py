from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration207.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration207_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _meta() -> dict[str, str]:
    return {
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
        "min_order_qty": "0.001",
        "max_order_qty": "1000",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def _generated_small_qty_rec() -> dict:
    qty = 0.00025
    reference = 100000.0
    grid_count = 4
    notional = qty * reference
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "grid_count": grid_count,
            "grid_levels": grid_count,
            "grid_type": "arithmetic",
            "leverage": 3,
            "price_ref": reference,
            "price_range_lower": 99000.0,
            "price_range_upper": 101000.0,
            "sizing": {
                "basis": "minimum_viable_operator_default",
                "qty_per_order": qty,
                "order_notional_usdt": notional,
                "estimated_total_order_notional_usdt": notional * grid_count,
                "estimated_margin_required_usdt": notional * grid_count / 3,
                "exchange_filter_assumption": {
                    "mode": "provisional_target_notional_until_bybit_preflight",
                    "actual_bybit_filters_required": True,
                },
            },
            "economics": {
                "qty_per_order": qty,
                "order_notional_usdt": notional,
                "estimated_total_order_notional_usdt": notional * grid_count,
                "estimated_margin_required_usdt": notional * grid_count / 3,
                "estimated_max_position_notional_usdt": notional * grid_count,
                "net_profit_bps": 4.0,
            },
            "trade_plan": {
                "reference_price": reference,
                "grid_count": grid_count,
                "grid_type": "arithmetic",
                "sizing": {
                    "basis": "minimum_viable_operator_default",
                    "qty_per_order": qty,
                    "exchange_filter_assumption": {
                        "mode": "provisional_target_notional_until_bybit_preflight",
                        "actual_bybit_filters_required": True,
                    },
                },
                "levels": {
                    "range": {"lower": 99000.0, "upper": 101000.0},
                    "kill_switch": {"lower": 98500.0, "upper": 101500.0},
                    "grid_step": {"step_abs": 500.0, "step_pct": 0.5},
                    "tp_per_leg": {"abs": 350.0, "pct": 0.35},
                },
            },
        },
    }


def test_builtin_risk_defaults_match_the_shipped_small_account_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RISK_LIMITS_JSON", raising=False)
    from app.settings import load_settings

    limits = load_settings().risk_limits

    assert limits == {
        "max_concurrent_bots": 1,
        "max_daily_dd_usdt": 10.0,
        "cooldown_after_loss_min": 90,
        "max_symbol_bots": 1,
        "min_leverage": 3,
        "max_leverage": 5,
        "max_position_notional_usdt": 500.0,
        "max_margin_per_bot_usdt": 100.0,
    }


def test_provisional_sizing_keeps_the_target_notional_without_fake_qty_step_upsize() -> None:
    from app.recommender import _fallback_order_qty_for_linear_grid

    qty, notional, assumption = _fallback_order_qty_for_linear_grid(100000.0, target_notional_usdt=25.0)

    assert qty == pytest.approx(0.00025)
    assert notional == pytest.approx(25.0)
    assert assumption["mode"] == "provisional_target_notional_until_bybit_preflight"
    assert "fallback_qty_step" not in assumption


def test_exchange_alignment_uses_minimum_executable_qty_only_for_generated_default(app_main) -> None:
    rec = _generated_small_qty_rec()
    original_qty = rec["params"]["sizing"]["qty_per_order"]

    snapped = app_main._snap_reco_payload_to_bybit_meta(rec, _meta())
    sizing = snapped["params"]["sizing"]
    snapped_qty = sizing["qty_per_order"]

    assert snapped_qty == pytest.approx(0.001)
    assert snapped_qty > original_qty
    assert sizing["requested_qty_per_order"] == pytest.approx(original_qty)
    assert sizing["qty_adjustment_reason"] == "exchange_minimum_for_generated_default"
    validation = app_main._validate_trade_plan_against_bybit_meta(
        snapped,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    error_codes = {item["code"] for item in validation["errors"]}
    assert "ORDER_QTY_BELOW_MIN" not in error_codes
    assert "ORDER_QTY_OFF_STEP" not in error_codes
    assert "ORDER_NOTIONAL_BELOW_MIN" not in error_codes


def test_runtime_risk_fallback_uses_the_same_shipped_profile() -> None:
    from app.risk import normalize_risk_limits

    assert normalize_risk_limits({}, {}) == {
        "max_concurrent_bots": 1,
        "max_daily_dd_usdt": 10.0,
        "cooldown_after_loss_min": 90,
        "max_symbol_bots": 1,
        "min_leverage": 3,
        "max_leverage": 5,
        "max_position_notional_usdt": 500.0,
        "max_margin_per_bot_usdt": 100.0,
    }
