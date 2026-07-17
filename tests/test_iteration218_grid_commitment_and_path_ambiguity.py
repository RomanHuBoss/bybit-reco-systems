from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome
from app.recommender import _build_trade_plan, _params as build_params


def _market_features() -> dict:
    return {
        "price": 100.0,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.01,
        "_direction_agg": {"trendiness": 0.10, "coherence": 0.75, "regime": "range"},
    }


def _cost_model() -> dict:
    return {"execution_cost_bps": 8.0, "total_cost_bps": 8.0, "expected_funding_bps": 0.0}


def _generated(direction: str) -> tuple[dict, dict]:
    features = _market_features()
    params = build_params(
        "futures_grid",
        "linear",
        features,
        global_sent=0.0,
        direction=direction,
        taker_fee_bps=4.0,
        direction_bias=direction,
        direction_bias_strength=0.50,
        atr_pct_for_grid=0.01,
        cost_model=_cost_model(),
        risk_limits={},
    )
    params["trade_plan"] = _build_trade_plan(
        "futures_grid", "linear", features, direction, params, cost_model=_cost_model()
    )
    return params, features


def _expected_commitment(params: dict, direction: str) -> tuple[int, float]:
    lower = float(params["price_range_lower"])
    upper = float(params["price_range_upper"])
    reference = float(params["price_ref"])
    grid_count = int(params["grid_count"])
    qty = float(params["sizing"]["qty_per_order"])
    step = (upper - lower) / grid_count
    levels = [lower + step * index for index in range(grid_count + 1)]
    position = (reference - lower) / step
    nearest = round(position)
    exact = math.isclose(position, nearest, rel_tol=0.0, abs_tol=1e-9)
    if exact:
        pivot = max(0, min(grid_count, int(nearest)))
        buy_indices = list(range(0, pivot))
        sell_indices = list(range(pivot + 1, grid_count + 1))
        initial_long = grid_count - pivot if direction == "long" else 0
        initial_short = pivot if direction == "short" else 0
    else:
        cell = max(0, min(grid_count - 1, math.floor(position)))
        if direction in {"neutral", "long"}:
            buy_indices = list(range(0, cell + 1))
            sell_indices = list(range(cell + 2, grid_count + 1))
        else:
            buy_indices = list(range(0, cell))
            sell_indices = list(range(cell + 1, grid_count + 1))
        initial_long = len(sell_indices) if direction == "long" else 0
        initial_short = len(buy_indices) if direction == "short" else 0

    active_orders = len(buy_indices) + len(sell_indices)
    if direction == "long":
        committed_price_sum = initial_long * reference + sum(levels[index] for index in buy_indices)
    elif direction == "short":
        committed_price_sum = initial_short * reference + sum(levels[index] for index in sell_indices)
    else:
        committed_price_sum = max(sum(levels[index] for index in buy_indices), sum(levels[index] for index in sell_indices))
    return active_orders, qty * committed_price_sum


def _outcome_params(*, lower: float, upper: float, grid_count: int, ks_lower: float, ks_upper: float) -> dict:
    return {
        "grid_count": grid_count,
        "grid_levels": grid_count,
        "price_range_lower": lower,
        "price_range_upper": upper,
        "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
        "trade_plan": {
            "grid_count": grid_count,
            "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
            "levels": {
                "range": {"lower": lower, "upper": upper},
                "kill_switch": {"lower": ks_lower, "upper": ks_upper},
                "tp_per_leg": {"abs": (upper - lower) / grid_count},
            },
        },
    }


def _seed(conn, *, base_ts: int, candle: tuple[float, float, float, float]) -> None:
    open_px, high_px, low_px, close_px = candle
    db.upsert_ohlcv(conn, [{
        "venue": "linear",
        "symbol": "BTCUSDT",
        "tf_sec": 60,
        "ts": base_ts,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "volume": 1_000.0,
    }])


@pytest.mark.parametrize("direction", ["long", "short"])
def test_directional_generated_sizing_counts_every_committed_grid_slot(direction: str) -> None:
    params, _ = _generated(direction)
    active_orders, committed_notional = _expected_commitment(params, direction)

    assert active_orders == int(params["grid_count"])  # one dynamic bridge price is initially idle
    assert params["sizing"]["estimated_active_orders"] == active_orders
    assert params["economics"]["estimated_active_orders"] == active_orders
    assert params["sizing"]["estimated_total_order_notional_usdt"] == pytest.approx(committed_notional)
    assert params["economics"]["estimated_total_order_notional_usdt"] == pytest.approx(committed_notional)


def test_execution_preflight_accepts_off_grid_dynamic_bridge_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration218.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration218-lock.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        params, _ = _generated("long")
        active_orders, committed_notional = _expected_commitment(params, "long")
        leverage = float(params["leverage"])
        for block in (params["sizing"], params["economics"], params["trade_plan"]["sizing"], params["trade_plan"]["economics"]):
            block["estimated_active_orders"] = active_orders
            block["estimated_total_order_notional_usdt"] = committed_notional
            block["estimated_margin_required_usdt"] = committed_notional / leverage
        rec = {
            "bot_type": "futures_grid",
            "venue": "linear",
            "symbol": "BTCUSDT",
            "direction": "long",
            "account_mode": "unified",
            "margin_mode": "cross",
            "params": params,
        }
        validation = app_main._validate_trade_plan_against_bybit_meta(
            rec, {}, require_meta=False, require_execution_plan=True
        )
        codes = {str(item.get("code")) for item in validation.get("errors", [])}
        assert "ACTIVE_ORDERS_GRID_COUNT_MISMATCH" not in codes
        assert "TOTAL_NOTIONAL_GRID_COUNT_MISMATCH" not in codes
        assert validation["ok"] is True
    finally:
        sys.modules.pop("app.main", None)


def test_long_outcome_uses_actual_committed_notional_for_off_grid_entry(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long-capital.db"))
    try:
        db.init_db(conn)
        base_ts = 1_709_000_000
        _seed(conn, base_ts=base_ts, candle=(100.5, 102.0, 100.5, 102.0))
        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.5, 102.0,
            base_ts, base_ts + 60, "long",
            _outcome_params(lower=99.0, upper=102.0, grid_count=3, ks_lower=98.0, ks_upper=103.0),
        )
        # Level 101 is the idle bridge. Commitment is one initial long at
        # 100.5 plus opening buys at 99 and 100.
        assert ret_proxy == pytest.approx(1.5 / 299.5)
        assert success == 1
    finally:
        conn.close()

def test_short_outcome_uses_actual_committed_notional_for_off_grid_entry(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "short-capital.db"))
    try:
        db.init_db(conn)
        base_ts = 1_709_100_000
        _seed(conn, base_ts=base_ts, candle=(100.5, 100.5, 99.0, 99.0))
        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.5, 99.0,
            base_ts, base_ts + 60, "short",
            _outcome_params(lower=99.0, upper=102.0, grid_count=3, ks_lower=98.0, ks_upper=103.0),
        )
        # Level 100 is the idle bridge. Commitment is one initial short at
        # 100.5 plus opening sells at 101 and 102.
        assert ret_proxy == pytest.approx(1.5 / 303.5)
        assert success == 1
    finally:
        conn.close()

def test_two_sided_intrabar_path_with_different_valid_pnl_is_unlabelable(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "ambiguous-path.db"))
    try:
        db.init_db(conn)
        base_ts = 1_709_200_000
        _seed(conn, base_ts=base_ts, candle=(99.0, 99.5, 97.9, 98.5))
        result = _grid_outcome(
            conn, "linear", "BTCUSDT", 99.0, 98.5,
            base_ts, base_ts + 60, "neutral",
            _outcome_params(lower=98.0, upper=102.0, grid_count=4, ks_lower=97.0, ks_upper=103.0),
        )
        # O→H→L→C leaves a residual long with +0.5; O→L→H→C closes the
        # cycle with +1.0. OHLC cannot choose either path or fabricate zero.
        assert result is None
    finally:
        conn.close()


def test_stop_candle_with_opposite_grid_excursion_is_unlabelable(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "ambiguous-stop.db"))
    try:
        db.init_db(conn)
        base_ts = 1_709_300_000
        _seed(conn, base_ts=base_ts, candle=(100.0, 102.5, 98.9, 101.0))
        result = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 101.0,
            base_ts, base_ts + 60, "neutral",
            _outcome_params(lower=99.0, upper=101.0, grid_count=2, ks_lower=98.0, ks_upper=102.0),
        )
        # Upper stop first gives -1; lower excursion first completes a +1 grid
        # pair before the later stop and gives 0. OHLC does not reveal chronology.
        assert result is None
    finally:
        conn.close()


def test_outcome_contract_is_bumped_for_commitment_and_path_semantics() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.0.75"' in source


def test_auto_snap_preserves_off_grid_dynamic_bridge_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration218-snap.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration218-snap-lock.db"))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        params, _ = _generated("long")
        rec = {
            "bot_type": "futures_grid",
            "venue": "linear",
            "symbol": "BTCUSDT",
            "direction": "long",
            "params": params,
        }
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
        active_orders, committed_notional = _expected_commitment(snapped["params"], "long")
        upper = float(snapped["params"]["price_range_upper"])
        qty = float(sizing["qty_per_order"])

        assert active_orders == int(snapped["params"]["grid_count"])
        assert sizing["estimated_active_orders"] == active_orders
        assert sizing["estimated_total_order_notional_usdt"] == pytest.approx(committed_notional)
        assert sizing["estimated_worst_case_total_order_notional_usdt"] == pytest.approx(qty * upper * int(snapped["params"]["grid_count"]))
    finally:
        sys.modules.pop("app.main", None)
