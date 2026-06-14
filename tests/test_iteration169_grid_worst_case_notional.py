from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration169.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration169_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _risk_limits(max_notional: float, max_margin: float = 1_000_000.0) -> dict[str, float | int]:
    return {
        "max_concurrent_bots": 4,
        "max_daily_dd_usdt": 200.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 1,
        "min_leverage": 1,
        "max_leverage": 100,
        "max_position_notional_usdt": float(max_notional),
        "max_margin_per_bot_usdt": float(max_margin),
    }


def _grid_rec() -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "params": {
            "leverage": 10,
            "grid_count": 10,
            "grid_levels": 10,
            "sizing": {
                "order_qty": 1.0,
                # Legacy/reference-price estimate: 1 qty * 100 reference * 10 grids.
                "estimated_total_order_notional_usdt": 1000.0,
                "estimated_margin_required_usdt": 100.0,
            },
            "economics": {
                "estimated_max_position_notional_usdt": 1000.0,
                "estimated_margin_required_usdt": 100.0,
            },
            "trade_plan": {
                "reference_price": 100.0,
                "grid_count": 10,
                "levels": {
                    "range": {"lower": 90.0, "upper": 150.0},
                    "kill_switch": {"lower": 85.0, "upper": 155.0},
                },
                "sizing": {"order_qty": 1.0},
                "economics": {"estimated_total_order_notional_usdt": 1000.0},
            },
        },
    }


def test_runtime_caps_use_worst_executable_grid_price_not_reference_price(app_main) -> None:
    blocks = app_main._execution_runtime_size_risk_blocks(_grid_rec(), _risk_limits(max_notional=1200.0))
    codes = {block["code"] for block in blocks}

    assert "POSITION_NOTIONAL_UNDERSTATED_BY_GRID_PRICE" in codes
    assert "MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION" in codes
    assert any("1500" in block["msg"] for block in blocks if block["code"] == "MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION")


def test_understated_legacy_estimate_does_not_block_when_worst_case_is_within_cap(app_main) -> None:
    blocks = app_main._execution_runtime_size_risk_blocks(_grid_rec(), _risk_limits(max_notional=2000.0))

    assert {block["code"] for block in blocks} == set()


def test_worst_case_total_notional_field_takes_precedence_over_legacy_reference_estimate(app_main) -> None:
    rec = _grid_rec()
    rec["params"]["sizing"]["estimated_worst_case_total_order_notional_usdt"] = 1500.0
    rec["params"]["sizing"]["estimated_worst_case_margin_required_usdt"] = 150.0

    blocks = app_main._execution_runtime_size_risk_blocks(rec, _risk_limits(max_notional=1200.0, max_margin=140.0))
    codes = {block["code"] for block in blocks}

    assert "MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION" in codes
    assert "MAX_MARGIN_PER_BOT_AT_EXECUTION" in codes
    assert "POSITION_NOTIONAL_UNDERSTATED_BY_GRID_PRICE" not in codes


def test_auto_snap_publishes_worst_case_grid_notional_and_margin(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "neutral",
        "params": {
            "grid_count": 10,
            "grid_levels": 10,
            "leverage": 5,
            "price_ref": 100.0,
            "sizing": {"basis": "minimum_viable_operator_default", "qty_per_order": 1.0},
            "economics": {
                "estimated_total_order_notional_usdt": 1000.0,
                "estimated_margin_required_usdt": 200.0,
                "estimated_max_position_notional_usdt": 1000.0,
            },
            "operator_sheet": {
                "sizing": {"basis": "minimum_viable_operator_default", "qty_per_order": 1.0},
                "economics": {"capital_required_usdt": 200.0},
            },
            "trade_plan": {
                "reference_price": 100.0,
                "grid_count": 10,
                "sizing": {"basis": "minimum_viable_operator_default", "qty_per_order": 1.0},
                "levels": {
                    "range": {"lower": 90.0, "upper": 150.0},
                    "kill_switch": {"lower": 85.0, "upper": 155.0},
                    "grid_step": {"step_abs": 1.0, "step_pct": 1.0},
                },
            },
        },
    }
    meta = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "tick_size": "0.1",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "min_notional": "5",
        "leverage_step": "0.01",
    }

    snapped = app_main._snap_reco_payload_to_bybit_meta(rec, meta)
    sizing = snapped["params"]["sizing"]
    economics = snapped["params"]["economics"]
    operator_economics = snapped["params"]["operator_sheet"]["economics"]

    assert sizing["estimated_worst_case_order_notional_usdt"] == pytest.approx(150.0)
    assert sizing["estimated_worst_case_total_order_notional_usdt"] == pytest.approx(1500.0)
    assert sizing["estimated_worst_case_margin_required_usdt"] == pytest.approx(300.0)
    assert economics["estimated_max_position_notional_usdt"] == pytest.approx(1500.0)
    assert operator_economics["capital_required_usdt"] == pytest.approx(300.0)
    assert operator_economics["estimated_worst_case_margin_required_usdt"] == pytest.approx(300.0)
