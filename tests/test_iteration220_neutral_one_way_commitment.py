from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app.grid_math import arithmetic_grid_commitment
from app.outcomes import _grid_outcome
from app.recommender import _build_trade_plan, _params as build_params


def _cost_model() -> dict:
    return {"execution_cost_bps": 8.0, "total_cost_bps": 8.0, "expected_funding_bps": 0.0}


def _generated_neutral() -> dict:
    features = {
        "price": 100.0,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.01,
        "_direction_agg": {"trendiness": 0.10, "coherence": 0.75, "regime": "range"},
    }
    params = build_params(
        "futures_grid",
        "linear",
        features,
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=4.0,
        direction_bias="neutral",
        direction_bias_strength=0.50,
        atr_pct_for_grid=0.01,
        cost_model=_cost_model(),
        risk_limits={},
    )
    params["trade_plan"] = _build_trade_plan(
        "futures_grid", "linear", features, "neutral", params, cost_model=_cost_model()
    )
    return params


def _independent_neutral_commitment(params: dict) -> tuple[int, int, float, float]:
    lower = float(params["price_range_lower"])
    upper = float(params["price_range_upper"])
    reference = float(params["price_ref"])
    count = int(params["grid_count"])
    qty = float(params["sizing"]["qty_per_order"])
    step = (upper - lower) / count
    levels = [lower + step * index for index in range(count + 1)]
    position = (reference - lower) / step
    nearest = round(position)
    if math.isclose(position, nearest, rel_tol=0.0, abs_tol=1e-9):
        pivot = max(0, min(count, int(nearest)))
        buys = list(range(0, pivot))
        sells = list(range(pivot + 1, count + 1))
    else:
        cell = max(0, min(count - 1, math.floor(position)))
        buys = list(range(0, cell + 1))
        sells = list(range(cell + 2, count + 1))
    buy_sum = sum(levels[index] for index in buys)
    sell_sum = sum(levels[index] for index in sells)
    committed_slots = len(buys) + len(sells)
    committed_per_qty = buy_sum + sell_sum
    max_position_notional = qty * upper * max(len(buys), len(sells))
    return len(buys) + len(sells), committed_slots, qty * committed_per_qty, max_position_notional


def _outcome_params() -> dict:
    cost_model = {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0}
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "cost_model": dict(cost_model),
        "trade_plan": {
            "grid_count": 2,
            "cost_model": dict(cost_model),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 102.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def _seed(conn, *, base_ts: int, candles: list[tuple[float, float, float, float]]) -> None:
    db.upsert_ohlcv(conn, [
        {
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts + index * 60,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": 1_000.0,
        }
        for index, (open_px, high_px, low_px, close_px) in enumerate(candles)
    ])


def test_neutral_exact_level_commitment_reserves_all_initial_opening_orders() -> None:
    topology = arithmetic_grid_commitment(
        lower=99.0, upper=101.0, grid_count=2, reference_price=100.0, direction="neutral"
    )
    assert topology is not None
    assert topology["active_order_count"] == 2
    assert topology["committed_slot_count"] == 2
    assert topology["max_abs_position_slots"] == 1
    assert topology["committed_notional_per_qty"] == pytest.approx(200.0)


def test_neutral_off_grid_commitment_reserves_all_initial_opening_orders() -> None:
    topology = arithmetic_grid_commitment(
        lower=99.0, upper=101.0, grid_count=2, reference_price=100.5, direction="neutral"
    )
    assert topology is not None
    assert topology["active_order_count"] == 2
    assert topology["committed_slot_count"] == 2
    assert topology["max_abs_position_slots"] == 2
    assert topology["committed_notional_per_qty"] == pytest.approx(199.0)


def test_generated_neutral_sizing_reserves_all_initial_opening_orders() -> None:
    params = _generated_neutral()
    active_orders, committed_slots, committed_notional, worst_notional = _independent_neutral_commitment(params)

    assert params["sizing"]["estimated_active_orders"] == active_orders
    assert params["sizing"]["estimated_committed_slots"] == committed_slots
    assert params["sizing"]["estimated_total_order_notional_usdt"] == pytest.approx(committed_notional)
    assert params["sizing"]["estimated_worst_case_total_order_notional_usdt"] == pytest.approx(worst_notional)
    assert params["economics"]["estimated_total_order_notional_usdt"] == pytest.approx(committed_notional)


def test_neutral_outcome_return_uses_full_initial_order_denominator(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "neutral-return.db"))
    try:
        db.init_db(conn)
        base_ts = 1_711_000_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.0, 100.0, 100.0, 100.0),
            (101.1, 101.1, 101.1, 101.1),
            (101.1, 101.1, 99.9, 99.9),
        ])
        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 99.9,
            base_ts, base_ts + 180, "neutral", _outcome_params(),
        )
        assert ret_proxy == pytest.approx(1.0 / 200.0)
        assert success == 1
    finally:
        conn.close()


def test_neutral_daily_loss_guard_uses_max_one_way_position_not_all_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "daily.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "daily-lock.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        rec = {
            "bot_type": "futures_grid",
            "venue": "linear",
            "direction": "neutral",
            "params": {
                "grid_count": 2,
                "grid_levels": 2,
                "price_ref": 100.0,
                "price_range_lower": 99.0,
                "price_range_upper": 101.0,
                "sizing": {"qty_per_order": 1.0},
                "trade_plan": {
                    "grid_count": 2,
                    "levels": {
                        "range": {"lower": 99.0, "upper": 101.0},
                        "kill_switch": {"lower": 98.0, "upper": 102.0},
                    },
                },
            },
        }
        result = app_main._execution_daily_loss_budget_guard(
            rec, {"max_daily_dd_usdt": 1_000.0}, SimpleNamespace(daily_dd=0.0)
        )
        assert result["estimated_position_notional_usdt"] == pytest.approx(101.0)
        assert result["estimated_position_notional_source"] == "qty*max_grid_price*committed_slots"
    finally:
        sys.modules.pop("app.main", None)


def test_neutral_auto_snap_keeps_max_position_separate_from_full_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "snap.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "snap-lock.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        params = _generated_neutral()
        rec = {"bot_type": "futures_grid", "venue": "linear", "symbol": "BTCUSDT", "direction": "neutral", "params": params}
        meta = {
            "category": "linear",
            "symbol": "BTCUSDT",
            "tick_size": "0.01",
            "qty_step": "0.001",
            "min_order_qty": "0.001",
            "min_notional": "5",
            "leverage_step": "0.01",
        }
        snapped = app_main._snap_reco_payload_to_bybit_meta(rec, meta)
        sizing = snapped["params"]["sizing"]
        active_orders, committed_slots, committed_notional, worst_notional = _independent_neutral_commitment(snapped["params"])
        assert sizing["estimated_active_orders"] == active_orders
        assert sizing["estimated_committed_slots"] == committed_slots
        assert sizing["estimated_total_order_notional_usdt"] == pytest.approx(committed_notional)
        assert sizing["estimated_worst_case_total_order_notional_usdt"] == pytest.approx(worst_notional)
    finally:
        sys.modules.pop("app.main", None)


def test_neutral_preflight_accepts_full_initial_opening_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "preflight.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "preflight-lock.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        params = _generated_neutral()
        active_orders, committed_slots, committed_notional, worst_notional = _independent_neutral_commitment(params)
        leverage = float(params["leverage"])
        for block in (params["sizing"], params["economics"], params["trade_plan"]["sizing"], params["trade_plan"]["economics"]):
            block["estimated_active_orders"] = active_orders
            block["estimated_committed_slots"] = committed_slots
            block["estimated_total_order_notional_usdt"] = committed_notional
            block["estimated_worst_case_total_order_notional_usdt"] = worst_notional
            block["estimated_margin_required_usdt"] = committed_notional / leverage
            block["estimated_worst_case_margin_required_usdt"] = worst_notional / leverage
        rec = {
            "bot_type": "futures_grid",
            "venue": "linear",
            "symbol": "BTCUSDT",
            "direction": "neutral",
            "account_mode": "unified",
            "margin_mode": "cross",
            "params": params,
        }
        validation = app_main._validate_trade_plan_against_bybit_meta(
            rec, {}, require_meta=False, require_execution_plan=True
        )
        codes = {str(item.get("code")) for item in validation.get("errors", [])}
        assert "TOTAL_NOTIONAL_GRID_COUNT_MISMATCH" not in codes
        assert "COMMITTED_SLOTS_TOPOLOGY_MISMATCH" not in codes
        assert validation["ok"] is True
    finally:
        sys.modules.pop("app.main", None)


def test_contract_bumped_for_neutral_one_way_commitment() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.0.77"' in source
