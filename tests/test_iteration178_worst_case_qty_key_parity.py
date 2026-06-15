from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration178.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration178_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


@pytest.mark.parametrize(
    ("notional_key", "container_name"),
    [
        ("worst_case_total_order_notional_usdt", "sizing"),
        ("max_position_notional_usdt", "economics"),
    ],
)
def test_directional_exit_qty_treats_all_ui_worst_case_notional_keys_as_max_grid_price(
    app_main,
    notional_key: str,
    container_name: str,
) -> None:
    """Backend TP/SL math must share the UI convention for worst-case exposure fields.

    With reference=100 and upper grid price=150, a 1500 USDT worst-case/max grid
    exposure represents 10 base units. Dividing by reference price would display
    and compute PnL for 15 base units, overstating TP/SL amounts by 50%.
    """
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
            },
        },
    }
    rec["params"][container_name] = {notional_key: 1500.0}

    payload = app_main._directional_exit_payload_for_reco(rec)

    assert payload["qty"] == pytest.approx(10.0)
    assert payload["qty_source"] == f"{notional_key}/max_grid_price"
    assert payload["trade_math"]["gross_profit_usdt"] == pytest.approx(600.0)
    assert payload["trade_math"]["gross_loss_usdt"] == pytest.approx(300.0)
