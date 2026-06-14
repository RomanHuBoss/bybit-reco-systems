from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration170.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration170_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def test_directional_exit_qty_prefers_explicit_grid_qty_over_worst_case_notional(app_main) -> None:
    """Worst-case notional is priced at the upper grid boundary, not at entry.

    For TP/SL gross PnL the canonical quantity is base qty: qty_per_order * grid_count.
    Dividing the upper-bound worst-case notional by reference_price overstates size.
    """
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "params": {
            "grid_count": 10,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 80.0, "upper": 150.0},
                    "kill_switch": {"lower": 70.0, "upper": 160.0},
                },
                "economics": {
                    "qty_per_order": 1.0,
                    "estimated_total_order_notional_usdt": 1000.0,
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


def test_directional_exit_qty_derives_worst_case_notional_with_worst_grid_price_when_qty_missing(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "params": {
            "grid_count": 10,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 80.0, "upper": 150.0},
                    "kill_switch": {"lower": 70.0, "upper": 160.0},
                },
                "economics": {
                    "estimated_worst_case_total_order_notional_usdt": 1500.0,
                },
            },
        },
    }

    payload = app_main._directional_exit_payload_for_reco(rec)

    assert payload["qty"] == pytest.approx(10.0)
    assert payload["qty_source"] == "estimated_worst_case_total_order_notional_usdt/max_grid_price"
    assert payload["trade_math"]["gross_profit_usdt"] == pytest.approx(600.0)
    assert payload["trade_math"]["gross_loss_usdt"] == pytest.approx(300.0)
