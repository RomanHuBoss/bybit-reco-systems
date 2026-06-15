from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration181.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration181_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _limits() -> dict[str, float | int]:
    return {
        "max_concurrent_bots": 4,
        "max_daily_dd_usdt": 200.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 1,
        "min_leverage": 1,
        "max_leverage": 5,
        "max_position_notional_usdt": 1000.0,
        "max_margin_per_bot_usdt": 100.0,
    }


def test_runtime_risk_caps_use_operator_sheet_leverage_notional_and_margin(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "params": {
            "operator_sheet": {
                "leverage": 8,
                "economics": {
                    "estimated_max_position_notional_usdt": 1200.0,
                    "estimated_margin_required_usdt": 150.0,
                },
            },
            "trade_plan": {
                "reference_price": 100.0,
                "grid_count": 5,
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 90.0, "upper": 110.0},
                },
            },
        },
    }

    blocks = app_main._execution_runtime_size_risk_blocks(rec, _limits())
    codes = {block["code"] for block in blocks}

    assert "LEVERAGE_MISSING_AT_EXECUTION" not in codes
    assert "MAX_LEVERAGE_PER_BOT_AT_EXECUTION" in codes
    assert "MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION" in codes
    assert "MAX_MARGIN_PER_BOT_AT_EXECUTION" in codes


def test_runtime_risk_caps_fail_closed_when_operator_sheet_size_context_is_unpriced(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "params": {
            "leverage": 3,
            "operator_sheet": {"sizing": {"qty_per_order": 0.02}},
        },
    }

    blocks = app_main._execution_runtime_size_risk_blocks(rec, _limits())
    codes = {block["code"] for block in blocks}

    assert "POSITION_SIZE_MISSING_AT_EXECUTION" in codes
