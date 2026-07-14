from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome


def _params() -> dict:
    cost = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "expected_funding_bps": 0.0,
        "expected_funding_events": 0,
    }
    sizing = {"qty_per_order": 1.0}
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "sizing": dict(sizing),
        "cost_model": dict(cost),
        "trade_plan": {
            "grid_count": 2,
            "sizing": dict(sizing),
            "cost_model": dict(cost),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 102.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def _seed(conn, base_ts: int, candle: tuple[float, float, float, float]) -> None:
    open_px, high_px, low_px, close_px = candle
    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": 100.0,
        }],
    )


def test_upper_kill_switch_uses_adverse_observed_extreme_for_short_inventory(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "upper-stop-slippage.db"))
    try:
        db.init_db(conn)
        base = 1_752_000_000
        _seed(conn, base, (100.0, 102.5, 100.0, 102.5))
        diagnostics: dict[str, object] = {}

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            102.5,
            base,
            base + 60,
            "neutral",
            _params(),
            diagnostics=diagnostics,
        )

        assert result is not None
        success, ret_proxy = result
        assert success == 0
        # Sell 101 leaves one short slot. The candle traded to 102.5 after the
        # 102 kill-switch trigger, so 102 is not a conservative market-stop fill.
        assert ret_proxy == pytest.approx(-1.5 / 200.0)
        assert diagnostics["kill_switch_fill_confirmation"] == "adverse_observed_extreme_v1"
        assert diagnostics["kill_switch_boundary_price"] == pytest.approx(102.0)
        assert diagnostics["kill_switch_liquidation_price"] == pytest.approx(102.5)
    finally:
        conn.close()


def test_lower_kill_switch_uses_adverse_observed_extreme_for_long_inventory(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "lower-stop-slippage.db"))
    try:
        db.init_db(conn)
        base = 1_752_100_000
        _seed(conn, base, (100.0, 100.0, 97.5, 97.5))

        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            97.5,
            base,
            base + 60,
            "neutral",
            _params(),
        )

        assert result is not None
        success, ret_proxy = result
        assert success == 0
        # Buy 99 leaves one long slot. The candle traded to 97.5 after the 98
        # kill-switch trigger, so the conservative liquidation bound is 97.5.
        assert ret_proxy == pytest.approx(-1.5 / 200.0)
    finally:
        conn.close()


def test_outcome_contract_is_bumped_for_kill_switch_fill_bound() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.0.60"' in source
