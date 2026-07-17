from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome


def _params(*, lower: float = 99.0, upper: float = 101.0, grid_count: int = 2,
            ks_lower: object = 98.0, ks_upper: object = 102.0,
            include_kill_switch: bool = True) -> dict:
    levels: dict[str, object] = {
        "range": {"lower": lower, "upper": upper},
        "tp_per_leg": {"abs": (upper - lower) / grid_count},
    }
    if include_kill_switch:
        levels["kill_switch"] = {"lower": ks_lower, "upper": ks_upper}
    return {
        "grid_count": grid_count,
        "grid_levels": grid_count,
        "price_range_lower": lower,
        "price_range_upper": upper,
        "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
        "trade_plan": {
            "grid_count": grid_count,
            "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
            "levels": levels,
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


def test_long_entry_between_levels_waits_for_dynamic_bridge_then_closes_at_next_sell(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long-between.db"))
    try:
        db.init_db(conn)
        base_ts = 1_708_000_000
        _seed(conn, base_ts=base_ts, candles=[(100.5, 102.0, 100.5, 102.0)])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.5, 102.0,
            base_ts, base_ts + 60, "long",
            _params(lower=99.0, upper=102.0, grid_count=3, ks_lower=98.0, ks_upper=103.0),
        )

        # Level 101 is the idle bridge. One initial long slot closes at 102;
        # commitment is entry 100.5 plus opening buys at 99 and 100.
        assert ret_proxy == pytest.approx(1.5 / 299.5)
        assert success == 1
    finally:
        conn.close()

def test_short_entry_between_levels_waits_for_dynamic_bridge_then_closes_at_next_buy(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "short-between.db"))
    try:
        db.init_db(conn)
        base_ts = 1_708_100_000
        _seed(conn, base_ts=base_ts, candles=[(100.5, 100.5, 99.0, 99.0)])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.5, 99.0,
            base_ts, base_ts + 60, "short",
            _params(lower=99.0, upper=102.0, grid_count=3, ks_lower=98.0, ks_upper=103.0),
        )

        # Level 100 is the idle bridge. One initial short slot closes at 99;
        # commitment is entry 100.5 plus opening sells at 101 and 102.
        assert ret_proxy == pytest.approx(1.5 / 303.5)
        assert success == 1
    finally:
        conn.close()

def test_minute_open_gap_and_next_candle_return_counts_grid_cycle(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "open-gap-cycle.db"))
    try:
        db.init_db(conn)
        base_ts = 1_708_200_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.0, 100.0, 100.0, 100.0),
            (101.1, 101.1, 101.1, 101.1),
            (101.1, 101.1, 99.9, 99.9),
        ])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 99.9,
            base_ts, base_ts + 180, "neutral", _params(),
        )

        # Previous close -> next open crosses the initial sell at 101. The
        # replacement buy becomes active at the following candle boundary and
        # is then crossed below 100.
        assert ret_proxy == pytest.approx(1.0 / 200.0)
        assert success == 1
    finally:
        conn.close()


def test_single_sided_move_across_two_candles_counts_confirmed_cycle(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "intraminute-cycle.db"))
    try:
        db.init_db(conn)
        base_ts = 1_708_300_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.0, 101.1, 100.0, 101.1),
            (101.1, 101.1, 99.9, 99.9),
        ])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 99.9,
            base_ts, base_ts + 120, "neutral", _params(),
        )

        # The initial sell and its replacement buy are confirmed in separate
        # candles, so no zero-latency placement assumption is required.
        assert ret_proxy == pytest.approx(1.0 / 200.0)
        assert success == 1
    finally:
        conn.close()


def test_kill_switch_breach_stops_ledger_at_adverse_observed_bound_not_after_recovery(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "kill-stop.db"))
    try:
        db.init_db(conn)
        base_ts = 1_708_400_000
        _seed(conn, base_ts=base_ts, candles=[
            (100.0, 102.5, 100.0, 102.5),
            (102.5, 102.5, 100.0, 100.0),
        ])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "neutral", _params(),
        )

        # Sell at 101, then the upper kill-switch triggers at 102 while the
        # same candle trades to 102.5. The adverse observed extreme is used as
        # the conservative market-stop bound; later recovery is irrelevant.
        assert ret_proxy == pytest.approx(-1.5 / 200.0)
        assert success == 0
    finally:
        conn.close()


def test_intraminute_kill_switch_breach_cannot_be_erased_by_same_candle_close(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "intraminute-kill.db"))
    try:
        db.init_db(conn)
        base_ts = 1_708_500_000
        _seed(conn, base_ts=base_ts, candles=[(100.0, 102.5, 100.0, 100.0)])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 60, "neutral", _params(),
        )

        assert ret_proxy == pytest.approx(-1.5 / 200.0)
        assert success == 0
    finally:
        conn.close()


def test_kill_switch_must_strictly_contain_grid_range(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "invalid-kill.db"))
    try:
        db.init_db(conn)
        base_ts = 1_708_600_000
        _seed(conn, base_ts=base_ts, candles=[(100.0, 100.0, 100.0, 100.0)])

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 60, "neutral",
            _params(ks_lower=99.5, ks_upper=102.0),
        ) is None
    finally:
        conn.close()


def test_missing_kill_switch_is_not_a_labelable_executable_grid(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "missing-kill.db"))
    try:
        db.init_db(conn)
        base_ts = 1_708_700_000
        _seed(conn, base_ts=base_ts, candles=[(100.0, 100.0, 100.0, 100.0)])

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 60, "neutral",
            _params(include_kill_switch=False),
        ) is None
    finally:
        conn.close()


def test_outcome_contract_is_bumped_for_topology_and_stop_semantics() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.0.72"' in source
