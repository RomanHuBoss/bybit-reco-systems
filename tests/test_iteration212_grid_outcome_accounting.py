from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome


def _seed_closes(conn, *, base_ts: int, closes: list[float]) -> None:
    rows = []
    for idx, close in enumerate(closes):
        open_price = closes[idx - 1] if idx else closes[0]
        rows.append({
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts + idx * 60,
            "open": float(open_price),
            "high": float(max(open_price, close)),
            "low": float(min(open_price, close)),
            "close": float(close),
            "volume": 1_000.0,
        })
    db.upsert_ohlcv(conn, rows)


def _params(*, grid_count: int = 2, execution_cost_bps: float = 10.0) -> dict:
    return {
        "grid_count": grid_count,
        "grid_levels": grid_count,
        "grid_spacing_pct": 1.0,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "cost_model": {
            "execution_cost_bps": execution_cost_bps,
            "expected_funding_bps": 0.0,
        },
        "trade_plan": {
            "grid_count": grid_count,
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 95.0, "upper": 105.0},
                "tp_per_leg": {"abs": 10.0},
            },
        },
    }


def test_repeated_grid_trades_are_not_capped_by_number_of_grids(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "repeated-grid-trades.db"))
    try:
        db.init_db(conn)
        base_ts = 1_702_000_000
        # Three complete 100 -> 101 -> 100 grid trades using a two-grid range.
        closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0]
        _seed_closes(conn, base_ts=base_ts, closes=closes)

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts,
            base_ts + len(closes) * 60,
            "neutral",
            _params(grid_count=2, execution_cost_bps=10.0),
        )

        # Bybit arithmetic grid profit is interval * quantity per completed trade
        # minus trading costs. One-way neutral commitment is the more expensive
        # opening stack: the sell order at 101 USDT, not both opposite orders.
        assert ret_proxy == pytest.approx((3.0 * (1.0 - 0.0005 * (101.0 + 100.0))) / 101.0)
        assert success == 1
    finally:
        conn.close()


def test_neutral_grid_without_any_fill_has_zero_proxy_return(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "neutral-no-fill.db"))
    try:
        db.init_db(conn)
        base_ts = 1_702_100_000
        # All closes remain inside the same one-dollar grid cell. Neutral mode has
        # no initial position, so there is no fee or directional PnL to charge.
        closes = [100.10, 100.20, 100.15, 100.25]
        _seed_closes(conn, base_ts=base_ts, closes=closes)
        params = _params(grid_count=20, execution_cost_bps=15.0)
        params["price_range_lower"] = 90.0
        params["price_range_upper"] = 110.0
        params["trade_plan"]["levels"]["range"] = {"lower": 90.0, "upper": 110.0}
        params["trade_plan"]["levels"]["kill_switch"] = {"lower": 85.0, "upper": 115.0}

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.10,
            100.25,
            base_ts,
            base_ts + len(closes) * 60,
            "neutral",
            params,
        )

        assert success == 0
        assert ret_proxy == pytest.approx(0.0)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("direction", "exit_price", "expected"),
    [
        ("long", 102.0, 19.0 / 1945.0),
        ("short", 98.0, 19.0 / 2055.0),
    ],
)
def test_directional_grid_aligned_move_adds_unrealized_pnl(
    tmp_path: Path,
    direction: str,
    exit_price: float,
    expected: float,
) -> None:
    conn = db.connect(str(tmp_path / f"{direction}-aligned.db"))
    try:
        db.init_db(conn)
        base_ts = 1_702_200_000
        closes = [100.0, exit_price]
        _seed_closes(conn, base_ts=base_ts, closes=closes)
        params = _params(grid_count=20, execution_cost_bps=0.0)
        params["price_range_lower"] = 90.0
        params["price_range_upper"] = 110.0
        params["trade_plan"]["levels"]["range"] = {"lower": 90.0, "upper": 110.0}
        params["trade_plan"]["levels"]["kill_switch"] = {"lower": 85.0, "upper": 115.0}

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            exit_price,
            base_ts,
            base_ts + len(closes) * 60,
            direction,
            params,
        )

        # Monetary P&L is 19 USDT. The denominator is the exact committed
        # directional notional: 1945 for Long and 2055 for Short.
        assert success == 1
        assert ret_proxy == pytest.approx(expected)
    finally:
        conn.close()



def test_first_candle_move_from_entry_participates_in_grid_cycle(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "first-candle-crossing.db"))
    try:
        db.init_db(conn)
        base_ts = 1_702_250_000
        db.upsert_ohlcv(conn, [
            {
                "venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60,
                "ts": base_ts, "open": 100.0, "high": 101.0, "low": 100.0,
                "close": 101.0, "volume": 1_000.0,
            },
            {
                "venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60,
                "ts": base_ts + 60, "open": 101.0, "high": 101.0, "low": 100.0,
                "close": 100.0, "volume": 1_000.0,
            },
        ])

        success, ret_proxy = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "neutral",
            _params(grid_count=2, execution_cost_bps=10.0),
        )

        # The grid starts at the entry/open, not at the first candle close. The
        # path 100 -> 101 -> 100 completes one trade. One-way neutral
        # commitment is the higher opening stack, here 101 USDT.
        assert success == 1
        assert ret_proxy == pytest.approx((1.0 - 0.0005 * (101.0 + 100.0)) / 101.0)
    finally:
        conn.close()

def _recommendation(*, rec_id: str, status: str, shadow: bool) -> dict:
    return {
        "rec_id": rec_id,
        "ts": 1_702_300_000,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.5,
        "confidence": 0.5,
        "expected_rr": 0.5,
        "risk_score": 0.2,
        "params": {},
        "reasons": {
            "outcome_policy": {
                "eligible": bool(shadow),
                "sample_role": "shadow_no_trade" if shadow else "actionable",
            }
        },
        "blocks": [],
        "status": status,
        "ttl_sec": 900,
        "model_version": "bybit-taxonomy-v3-mean-reversion",
        "features_ref_ts": 1_702_300_000,
    }


def test_outcome_stats_expose_actionable_and_shadow_cohorts_separately(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "cohort-stats.db"))
    try:
        db.init_db(conn)
        db.insert_recommendations(conn, [
            _recommendation(rec_id="R-actionable", status="recommended", shadow=False),
            _recommendation(rec_id="R-shadow-1", status="no_trade", shadow=True),
            _recommendation(rec_id="R-shadow-2", status="no_trade", shadow=True),
        ])
        db.insert_outcome(conn, {
            "rec_id": "R-actionable", "ts": 1_702_300_000, "venue": "linear",
            "symbol": "BTCUSDT", "bot_type": "futures_grid", "direction": "neutral",
            "horizon_sec": 3600, "label_available_ts": 1_702_303_600,
            "entry_close": 100.0, "exit_close": 100.0, "ret": 0.01, "success": 1,
        })
        for rec_id in ("R-shadow-1", "R-shadow-2"):
            db.insert_outcome(conn, {
                "rec_id": rec_id, "ts": 1_702_300_000, "venue": "linear",
                "symbol": "BTCUSDT", "bot_type": "futures_grid", "direction": "neutral",
                "horizon_sec": 3600, "label_available_ts": 1_702_303_600,
                "entry_close": 100.0, "exit_close": 100.0, "ret": -0.01, "success": 0,
            })

        stats = db.get_outcomes_stats(conn)

        assert stats["summary"]["win_rate"] == pytest.approx(0.333)
        assert stats["cohorts"]["actionable"]["win_rate"] == pytest.approx(1.0)
        assert stats["cohorts"]["actionable"]["avg_ret"] == pytest.approx(1.0)
        assert stats["cohorts"]["shadow_no_trade"]["win_rate"] == pytest.approx(0.0)
        assert stats["cohorts"]["shadow_no_trade"]["avg_ret"] == pytest.approx(-1.0)
    finally:
        conn.close()


def test_operator_headline_uses_actionable_outcome_cohort() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    assert "data.cohorts?.actionable" in source
    assert "Actionable win-rate" in source
