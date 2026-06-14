from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration171.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration171_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def test_directional_exit_qty_uses_nested_trade_plan_economics_grid_count(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "params": {
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 80.0, "upper": 150.0},
                    "kill_switch": {"lower": 70.0, "upper": 160.0},
                },
                "economics": {
                    "qty_per_order": 1.0,
                    "grid_count": 10,
                    "estimated_worst_case_total_order_notional_usdt": 1500.0,
                },
            },
        },
    }

    payload = app_main._directional_exit_payload_for_reco(rec)

    assert payload["qty"] == pytest.approx(10.0)
    assert payload["qty_source"] == "qty_per_order*grid_count"
    assert payload["trade_math"]["gross_profit_usdt"] == pytest.approx(300.0)
    assert payload["trade_math"]["gross_loss_usdt"] == pytest.approx(600.0)


def test_directional_exit_qty_uses_nested_sizing_estimated_active_orders(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "params": {
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 80.0, "upper": 120.0},
                    "kill_switch": {"lower": 70.0, "upper": 140.0},
                },
                "sizing": {
                    "qty_per_order": 0.25,
                    "estimated_active_orders": 8,
                },
            },
        },
    }

    payload = app_main._directional_exit_payload_for_reco(rec)

    assert payload["qty"] == pytest.approx(2.0)
    assert payload["qty_source"] == "qty_per_order*estimated_active_orders"
    assert payload["trade_math"]["gross_profit_usdt"] == pytest.approx(80.0)
    assert payload["trade_math"]["gross_loss_usdt"] == pytest.approx(60.0)
