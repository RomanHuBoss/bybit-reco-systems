from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.grid_math import grid_leg_economics
from app.outcomes import _grid_outcome, _resolve_grid_tp_leg_abs
from app.recommender import _build_trade_plan


def _seed_path(conn, *, base_ts: int, closes: list[float]) -> None:
    rows = []
    previous = closes[0]
    for index, close in enumerate(closes):
        rows.append({
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts + index * 60,
            "open": float(previous),
            "high": float(max(previous, close)),
            "low": float(min(previous, close)),
            "close": float(close),
            "volume": 1_000.0,
        })
        previous = close
    db.upsert_ohlcv(conn, rows)


def _params(*, grid_count: int = 20, cost_bps: float = 0.0) -> dict:
    return {
        "grid_count": grid_count,
        "grid_levels": grid_count,
        "grid_spacing_pct": 1.0,
        "price_range_lower": 90.0 if grid_count == 20 else 99.0,
        "price_range_upper": 110.0 if grid_count == 20 else 101.0,
        "cost_model": {"execution_cost_bps": cost_bps, "expected_funding_bps": 0.0},
        "trade_plan": {
            "grid_count": grid_count,
            "levels": {
                "range": {
                    "lower": 90.0 if grid_count == 20 else 99.0,
                    "upper": 110.0 if grid_count == 20 else 101.0,
                },
                "kill_switch": {"lower": 85.0, "upper": 115.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def test_completed_grid_pair_uses_full_exchange_interval_not_fill_haircut() -> None:
    econ = grid_leg_economics(
        reference_price=100,
        step_pct=0.60,
        order_notional=25,
        taker_fee_bps=0,
        execution_cost_bps=16,
        expected_funding_bps=2,
        fill_efficiency=0.70,
    )

    assert econ["gross_profit_bps"] == pytest.approx(60.0)
    assert econ["net_profit_bps"] == pytest.approx(42.0)
    assert econ["net_profit_usdt"] == pytest.approx(0.105)
    assert econ["projected_capture_bps"] == pytest.approx(42.0)



def test_legacy_tp_fallback_preserves_full_adjacent_grid_interval() -> None:
    assert _resolve_grid_tp_leg_abs(100.0, {}, fallback_step_abs=1.25) == pytest.approx(1.25)

def test_trade_plan_tp_per_leg_matches_adjacent_arithmetic_grid_interval() -> None:
    params = {
        "grid_spacing_pct": 1.0,
        "price_range_lower": 90.0,
        "price_range_upper": 110.0,
        "grid_count": 20,
        "grid_levels": 20,
        "grid_type": "arithmetic",
        "leverage": 1,
        "sizing": {},
        "economics": {},
    }
    plan = _build_trade_plan(
        "futures_grid",
        "linear",
        {"price": 100.0, "atr_pct": 0.01, "_atr_pct_1h": 0.01, "_direction_agg": {}},
        "neutral",
        params,
        cost_model={"execution_cost_bps": 10.0},
    )

    assert plan["levels"]["grid_step"]["step_abs"] == pytest.approx(1.0)
    assert plan["levels"]["tp_per_leg"]["abs"] == pytest.approx(1.0)
    assert plan["levels"]["tp_per_leg"]["pct"] == pytest.approx(1.0)


def test_one_profitable_neutral_grid_pair_is_a_success(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "one-pair.db"))
    try:
        db.init_db(conn)
        base_ts = 1_703_000_000
        _seed_path(conn, base_ts=base_ts, closes=[101.0, 100.0])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "neutral", _params(grid_count=2),
        )

        assert ret_proxy == pytest.approx(0.005)
        assert success == 1
    finally:
        conn.close()


def test_monotonic_long_grid_accounts_for_initial_position_take_profit(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long-rise.db"))
    try:
        db.init_db(conn)
        base_ts = 1_703_100_000
        _seed_path(conn, base_ts=base_ts, closes=[110.0])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 110.0,
            base_ts, base_ts + 60, "long", _params(),
        )

        # Ten initial long grid units are closed at 101..110. On total reference
        # notional (20 equal grid slots), gross P&L is (1+...+10)/(100*20)=2.75%.
        assert ret_proxy == pytest.approx(0.0275)
        assert success == 1
    finally:
        conn.close()


def test_neutral_monotonic_inventory_loss_uses_actual_grid_entry_prices(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "neutral-rise.db"))
    try:
        db.init_db(conn)
        base_ts = 1_703_200_000
        _seed_path(conn, base_ts=base_ts, closes=[110.0])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 110.0,
            base_ts, base_ts + 60, "neutral", _params(),
        )

        # Shorts open at 101..110. Marking all ten at 110 loses
        # (9+...+0)/(100*20)=2.25%, not 10% * 50% = 5%.
        assert ret_proxy == pytest.approx(-0.0225)
        assert success == 0
    finally:
        conn.close()


def test_directional_adverse_inventory_uses_weighted_grid_entries(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long-fall.db"))
    try:
        db.init_db(conn)
        base_ts = 1_703_300_000
        _seed_path(conn, base_ts=base_ts, closes=[90.0])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 90.0,
            base_ts, base_ts + 60, "long", _params(),
        )

        # Ten initial units at 100 plus ten added units at 99..90 are marked at 90.
        assert ret_proxy == pytest.approx(-0.0725)
        assert success == 0
    finally:
        conn.close()


def test_outcome_uses_persisted_range_and_grid_count_not_cost_widened_step(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "canonical-step.db"))
    try:
        db.init_db(conn)
        base_ts = 1_703_400_000
        _seed_path(conn, base_ts=base_ts, closes=[101.0, 100.0])
        params = _params(grid_count=2, cost_bps=80.0)
        params["grid_spacing_pct"] = 0.1  # stale alias; canonical range/count step is 1%.

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "neutral", params,
        )

        # Gross 1% on one of two capital slots minus price-aware 40 bps per fill:
        # [1 - 0.004*(101+100)] / 200 = 0.098%.
        assert ret_proxy == pytest.approx(0.00098)
        assert success == 1
    finally:
        conn.close()


def test_outcome_contract_is_bumped_for_grid_ledger_semantics() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v10"' in source
    assert 'version="1.0.29"' in source
