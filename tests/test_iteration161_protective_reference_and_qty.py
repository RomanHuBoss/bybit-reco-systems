from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app.trading_semantics import bybit_linear_protective_order_plan


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration161.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration161_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def test_protective_order_plan_fails_closed_without_reference_price() -> None:
    plan = bybit_linear_protective_order_plan("short", "take_profit", 90.0, None)
    codes = {err["code"] for err in plan["geometry_errors"]}

    assert plan["geometry_valid"] is False
    assert "PROTECTIVE_REFERENCE_PRICE_INVALID" in codes
    assert plan["side"] == "Buy"
    assert plan["reduceOnly"] is True
    assert plan["closeOnTrigger"] is True


def test_directional_exit_payload_uses_total_grid_exposure_for_pnl_math(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "params": {
            "grid_count": 5,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 90.0, "upper": 110.0},
                    "kill_switch": {"lower": 80.0, "upper": 120.0},
                    "grid_step": {"step_abs": 4.0},
                },
                "economics": {
                    "estimated_total_order_notional_usdt": 500.0,
                },
            },
        },
    }

    payload = app_main._directional_exit_payload_for_reco(rec)

    assert payload["geometry_valid"] is True
    assert payload["qty"] == pytest.approx(5.0)
    assert payload["qty_source"] == "estimated_total_order_notional_usdt/reference_price"
    assert payload["trade_math"]["gross_profit_usdt"] == pytest.approx(100.0)
    assert payload["trade_math"]["gross_loss_usdt"] == pytest.approx(100.0)
    assert payload["trade_math"]["risk_reward"] == pytest.approx(1.0)
