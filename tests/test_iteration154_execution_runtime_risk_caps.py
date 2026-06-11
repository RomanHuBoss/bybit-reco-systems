from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration154.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration154_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _rec() -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "params": {
            "leverage": 8,
            "sizing": {"estimated_margin_required_usdt": 150.0},
            "economics": {"estimated_max_position_notional_usdt": 1200.0},
            "trade_plan": {
                "sizing": {"order_qty": 0.01},
                "economics": {"estimated_total_order_notional_usdt": 1200.0},
            },
        },
    }


def test_execution_runtime_risk_caps_block_tightened_leverage_notional_and_margin(app_main) -> None:
    limits = {
        "max_concurrent_bots": 4,
        "max_daily_dd_usdt": 200.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 1,
        "min_leverage": 1,
        "max_leverage": 5,
        "max_position_notional_usdt": 1000.0,
        "max_margin_per_bot_usdt": 100.0,
    }

    blocks = app_main._execution_runtime_size_risk_blocks(_rec(), limits)
    codes = {block["code"] for block in blocks}

    assert "MAX_LEVERAGE_PER_BOT_AT_EXECUTION" in codes
    assert "MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION" in codes
    assert "MAX_MARGIN_PER_BOT_AT_EXECUTION" in codes


def test_execution_runtime_risk_caps_use_snapped_payload_values(app_main) -> None:
    rec = _rec()
    rec["params"]["leverage"] = 5
    rec["params"]["sizing"] = {"estimated_margin_required_usdt": 99.0}
    rec["params"]["economics"] = {"estimated_max_position_notional_usdt": 990.0}
    limits = {
        "max_concurrent_bots": 4,
        "max_daily_dd_usdt": 200.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 1,
        "min_leverage": 1,
        "max_leverage": 5,
        "max_position_notional_usdt": 1000.0,
        "max_margin_per_bot_usdt": 100.0,
    }

    assert app_main._execution_runtime_size_risk_blocks(rec, limits) == []

    rec["params"]["economics"]["estimated_max_position_notional_usdt"] = 1000.1
    blocks = app_main._execution_runtime_size_risk_blocks(rec, limits)

    assert {block["code"] for block in blocks} == {"MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION"}


def test_execution_runtime_risk_caps_can_infer_missing_margin_or_notional(app_main) -> None:
    rec = _rec()
    rec["params"]["leverage"] = 4
    rec["params"].pop("economics", None)
    rec["params"]["sizing"] = {"estimated_margin_required_usdt": 260.0}
    limits = {
        "max_concurrent_bots": 4,
        "max_daily_dd_usdt": 200.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 1,
        "min_leverage": 1,
        "max_leverage": 5,
        "max_position_notional_usdt": 1000.0,
        "max_margin_per_bot_usdt": 300.0,
    }

    blocks = app_main._execution_runtime_size_risk_blocks(rec, limits)

    assert {block["code"] for block in blocks} == {"MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION"}
