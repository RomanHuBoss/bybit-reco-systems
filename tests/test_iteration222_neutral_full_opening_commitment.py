from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import pytest

from app import db
from app.grid_math import arithmetic_grid_commitment
from app.outcomes import _grid_outcome
from app.recommender import _build_trade_plan, _params as build_params


def _cost_model() -> dict:
    return {"execution_cost_bps": 0.0, "total_cost_bps": 0.0, "expected_funding_bps": 0.0}


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
        taker_fee_bps=0.0,
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


def _independent_neutral_initial_commitment(params: dict) -> tuple[int, int, int, float, float]:
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
        # Bybit dynamic topology leaves the adjacent upper bridge idle for neutral.
        buys = list(range(0, cell + 1))
        sells = list(range(cell + 2, count + 1))
    buy_sum = sum(levels[index] for index in buys)
    sell_sum = sum(levels[index] for index in sells)
    active_orders = len(buys) + len(sells)
    committed_slots = active_orders  # every initial neutral order is opening/margin-bearing
    max_position_slots = max(len(buys), len(sells))
    committed_notional = qty * (buy_sum + sell_sum)
    worst_position_notional = qty * upper * max_position_slots
    return active_orders, committed_slots, max_position_slots, committed_notional, worst_position_notional


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
            },
        },
    }


def _seed(conn, *, base_ts: int) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base_ts,
                "open": 100.0,
                "high": 101.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1_000.0,
            }
        ],
    )


def test_neutral_exact_level_reserves_both_initial_opening_sides() -> None:
    topology = arithmetic_grid_commitment(
        lower=99.0, upper=101.0, grid_count=2, reference_price=100.0, direction="neutral"
    )
    assert topology is not None
    assert topology["active_order_count"] == 2
    assert topology["committed_slot_count"] == 2
    assert topology["max_abs_position_slots"] == 1
    assert topology["committed_notional_per_qty"] == pytest.approx(99.0 + 101.0)


def test_neutral_bybit_n5_example_reserves_all_five_initial_orders() -> None:
    topology = arithmetic_grid_commitment(
        lower=10_000.0, upper=30_000.0, grid_count=5, reference_price=20_000.0, direction="neutral"
    )
    assert topology is not None
    assert topology["buy_indices"] == [0, 1, 2]
    assert topology["sell_indices"] == [4, 5]
    assert topology["active_order_count"] == 5
    assert topology["committed_slot_count"] == 5
    assert topology["max_abs_position_slots"] == 3
    assert topology["committed_notional_per_qty"] == pytest.approx(10_000 + 14_000 + 18_000 + 26_000 + 30_000)


def test_directional_commitment_is_not_changed_by_neutral_reservation_fix() -> None:
    long_topology = arithmetic_grid_commitment(
        lower=10_000.0, upper=30_000.0, grid_count=5, reference_price=20_000.0, direction="long"
    )
    short_topology = arithmetic_grid_commitment(
        lower=10_000.0, upper=30_000.0, grid_count=5, reference_price=20_000.0, direction="short"
    )
    assert long_topology is not None and short_topology is not None
    assert long_topology["committed_notional_per_qty"] == pytest.approx(82_000.0)
    assert short_topology["committed_notional_per_qty"] == pytest.approx(118_000.0)


def test_generated_neutral_sizing_reserves_every_initial_opening_order() -> None:
    params = _generated_neutral()
    active, committed, max_position, total, worst = _independent_neutral_initial_commitment(params)
    sizing = params["sizing"]
    assert sizing["estimated_active_orders"] == active
    assert sizing["estimated_committed_slots"] == committed
    assert sizing["estimated_max_position_slots"] == max_position
    assert sizing["estimated_total_order_notional_usdt"] == pytest.approx(total)
    assert sizing["estimated_worst_case_total_order_notional_usdt"] == pytest.approx(worst)
    assert sizing["grid_commitment_model"] == "neutral_all_initial_opening_orders"


def test_neutral_outcome_return_uses_full_initial_investment_floor(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "neutral-full-investment.db"))
    try:
        db.init_db(conn)
        base_ts = 1_731_000_000
        _seed(conn, base_ts=base_ts)
        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts,
            base_ts + 60,
            "neutral",
            _outcome_params(),
        )
        assert success == 1
        assert ret_proxy == pytest.approx(1.0 / 200.0)
    finally:
        conn.close()


def test_auto_snap_recomputes_full_neutral_opening_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        active, committed, max_position, total, worst = _independent_neutral_initial_commitment(snapped["params"])
        sizing = snapped["params"]["sizing"]
        assert sizing["estimated_active_orders"] == active
        assert sizing["estimated_committed_slots"] == committed
        assert sizing["estimated_max_position_slots"] == max_position
        assert sizing["estimated_total_order_notional_usdt"] == pytest.approx(total)
        assert sizing["estimated_worst_case_total_order_notional_usdt"] == pytest.approx(worst)
        assert sizing["grid_commitment_model"] == "neutral_all_initial_opening_orders"
    finally:
        sys.modules.pop("app.main", None)


def test_preflight_rejects_legacy_max_side_neutral_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "preflight.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "preflight-lock.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        params = _generated_neutral()
        lower = float(params["price_range_lower"])
        upper = float(params["price_range_upper"])
        reference = float(params["price_ref"])
        count = int(params["grid_count"])
        qty = float(params["sizing"]["qty_per_order"])
        step = (upper - lower) / count
        levels = [lower + step * index for index in range(count + 1)]
        cell = max(0, min(count - 1, math.floor((reference - lower) / step)))
        buys = list(range(0, cell + 1))
        sells = list(range(cell + 2, count + 1))
        legacy_slots = max(len(buys), len(sells))
        legacy_total = qty * max(sum(levels[i] for i in buys), sum(levels[i] for i in sells))
        leverage = float(params["leverage"])
        blocks = [params["sizing"], params["economics"], params["trade_plan"]["sizing"], params["trade_plan"]["economics"]]
        for block in blocks:
            block["estimated_committed_slots"] = legacy_slots
            block["estimated_total_order_notional_usdt"] = legacy_total
            block["estimated_margin_required_usdt"] = legacy_total / leverage
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
        assert "COMMITTED_SLOTS_TOPOLOGY_MISMATCH" in codes
        assert "TOTAL_NOTIONAL_GRID_COUNT_MISMATCH" in codes
        assert validation["ok"] is False
    finally:
        sys.modules.pop("app.main", None)


def test_release_contract_is_bumped_for_neutral_margin_reservation() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v18"' in source
    assert 'version="1.0.41"' in source
