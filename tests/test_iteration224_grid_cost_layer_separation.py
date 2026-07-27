from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.grid_math import grid_leg_economics
from app.main import _execution_live_cost_blocks
from app.outcomes import _grid_outcome
from app.recommender import _estimate_cost_model, _params


def _feature() -> dict:
    return {
        "price": 100.0,
        "atr_pct": 0.01,
        "spread_bps": 10.0,
        "_direction_agg": {
            "direction": "neutral",
            "trendiness": 0.10,
            "coherence": 0.80,
            "regime": "range",
            "regime_confidence": 0.80,
            "mean_reversion_score": 0.80,
            "mean_reversion_evidence_valid": True,
            "mean_reversion_tf_count": 3,
        },
    }


def _cost_model() -> dict:
    return {
        "spread_bps": 10.0,
        "fee_bps_round_trip": 10.0,
        "grid_round_trip_fee_bps": 10.0,
        "slippage_bps": 5.0,
        "market_round_trip_cost_bps": 30.0,
        "execution_cost_bps": 30.0,
        "total_cost_bps": 30.0,
        "expected_funding_bps": 40.0,
        "funding_rate": None,
        "next_funding_ts": None,
        "funding_interval_min": None,
        "expected_funding_events": 0,
        "directional_funding_bps_per_event": 0.0,
    }


def _grid_params(direction: str) -> dict:
    cm = _cost_model()
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "cost_model": dict(cm),
        "trade_plan": {
            "grid_count": 2,
            "cost_model": dict(cm),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 102.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def _seed(conn, base_ts: int, candles: list[tuple[float, float, float, float]]) -> None:
    db.upsert_ohlcv(conn, [
        {
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts + idx * 60,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": 1000.0,
        }
        for idx, (o, h, l, c) in enumerate(candles)
    ])


def test_cost_model_separates_recurring_grid_fees_from_market_friction() -> None:
    model = _estimate_cost_model(
        "futures_grid",
        "linear",
        {"spread_bps": 10.0},
        5.5,
        "neutral",
        funding_rate=0.002,
        next_funding_ts=None,
        ts_now=1_000,
        funding_interval_min=480,
    )

    assert model["grid_round_trip_fee_bps"] == pytest.approx(11.0)
    assert model["market_round_trip_cost_bps"] == pytest.approx(24.5)
    assert model["one_time_market_friction_bps"] == pytest.approx(13.5)
    assert model["expected_funding_bps"] == pytest.approx(40.0)


def test_grid_leg_profit_excludes_horizon_funding_from_each_completed_pair() -> None:
    econ = grid_leg_economics(
        reference_price=20_000,
        step_pct=20.0,
        order_notional=3_040.0,
        taker_fee_bps=5.5,
        execution_cost_bps=11.0,
        expected_funding_bps=40.0,
    )

    assert econ["gross_profit_bps"] == pytest.approx(2_000.0)
    assert econ["grid_round_trip_fee_bps"] == pytest.approx(11.0)
    assert econ["net_profit_bps"] == pytest.approx(1_989.0)
    assert econ["funding_cost_bps"] == pytest.approx(40.0)
    assert econ["total_pnl_stress_after_one_grid_bps"] == pytest.approx(1_949.0)
    assert econ["funding_allocated_to_grid_leg"] is False


def test_generated_grid_spacing_uses_recurring_grid_fee_not_horizon_cost() -> None:
    model = _estimate_cost_model(
        "futures_grid", "linear", _feature(), 5.5, "neutral",
        funding_rate=0.002, next_funding_ts=None, ts_now=1_000, funding_interval_min=480,
    )
    params = _params(
        "futures_grid", "linear", _feature(), 0.0, "neutral", 5.5,
        "neutral", 0.0, 0.01, model, {"min_leverage": 1, "max_leverage": 1},
    )

    assert params["grid_spacing_cost_floor_bps"] == pytest.approx(11.0)
    assert params["grid_spacing_funding_cost_bps"] == pytest.approx(0.0)
    assert params["economics"]["execution_cost_bps"] == pytest.approx(11.0)
    assert params["economics"]["funding_cost_bps"] == pytest.approx(40.0)
    assert params["economics"]["net_profit_bps"] == pytest.approx(
        params["economics"]["gross_profit_bps"] - 11.0
    )


def test_neutral_completed_pair_is_not_charged_market_spread_and_slippage(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "neutral.db"))
    try:
        db.init_db(conn)
        base = 1_720_000_000
        _seed(conn, base, [(100.0, 101.1, 100.0, 101.1), (101.1, 101.1, 99.9, 99.9)])
        success, ret = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base, base + 120, "neutral", _grid_params("neutral"),
        )
        expected = (1.0 - (101.0 + 100.0) * 0.0005) / 200.0
        assert ret == pytest.approx(expected)
        assert success == 1
    finally:
        conn.close()


def test_directional_initial_market_entry_and_grid_tp_use_different_cost_layers(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long.db"))
    try:
        db.init_db(conn)
        base = 1_720_100_000
        _seed(conn, base, [(100.0, 101.1, 100.0, 101.0)])
        success, ret = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 101.0,
            base, base + 60, "long", _grid_params("long"),
        )
        # Initial long is a market entry: 15 bps. Resting TP at 101 is a grid
        # limit fill: 5 bps. The recurring spread/slippage layer is not charged.
        expected = (1.0 - 100.0 * 0.0015 - 101.0 * 0.0005) / 199.0
        assert ret == pytest.approx(expected)
        assert success == 1
    finally:
        conn.close()


def test_live_grid_edge_uses_recurring_fee_while_spread_remains_liquidity_gate() -> None:
    rec = {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "params": {
            "cost_model": _cost_model(),
            "economics": {
                "gross_profit_bps": 20.0,
                "execution_cost_bps": 10.0,
                "grid_round_trip_fee_bps": 10.0,
                "funding_cost_bps": 40.0,
            },
        },
    }
    ticker = {"bid": 99.95, "ask": 100.05, "last": 100.0}
    codes = {item["code"] for item in _execution_live_cost_blocks(ticker, rec)}

    assert "LIVE_SPREAD_TOO_WIDE" not in codes
    assert "LIVE_EXECUTION_EDGE_NON_POSITIVE" not in codes
    assert "LIVE_EXECUTION_EDGE_TOO_THIN" not in codes
    assert "LIVE_GROSS_EDGE_BELOW_COSTS" not in codes


def test_outcome_contract_bumped_for_cost_layer_separation() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'version="1.4.12"' in source
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
