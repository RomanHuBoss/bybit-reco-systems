from __future__ import annotations

from pathlib import Path

from app import db
from app.outcomes import _grid_outcome


def _seed_1m_rows(conn, *, base_ts: int, symbol: str, venue: str, candles: list[dict[str, float]]) -> None:
    rows = []
    for idx, candle in enumerate(candles):
        rows.append(
            {
                "venue": venue,
                "symbol": symbol,
                "tf_sec": 60,
                "ts": base_ts + idx * 60,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": 1_000.0,
            }
        )
    db.upsert_ohlcv(conn, rows)


def test_grid_outcome_does_not_treat_long_per_leg_tp_as_whole_grid_success(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "grid-long-tp.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_100_000
        candles = [
            {"open": 100.0, "high": 100.04, "low": 99.98, "close": 100.01},
            {"open": 100.01, "high": 100.06, "low": 99.99, "close": 100.02},
            {"open": 100.02, "high": 100.30, "low": 99.99, "close": 100.05},
            {"open": 100.05, "high": 100.08, "low": 99.98, "close": 100.01},
            {"open": 100.01, "high": 100.04, "low": 99.97, "close": 100.00},
            {"open": 100.00, "high": 100.03, "low": 99.97, "close": 100.00},
        ]
        _seed_1m_rows(conn, base_ts=base_ts, symbol="BTCUSDT", venue="linear", candles=candles)

        params = {
            "grid_levels": 20,
            "grid_spacing_pct": 0.4,
            "trade_plan": {
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                    "tp_per_leg": {"abs": 0.25},
                }
            },
        }

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.05,
            base_ts,
            base_ts + len(candles) * 60,
            "long",
            params,
        )

        assert success == 0
        # Long mode pays both the opening leg and the liquidation-equivalent
        # terminal close of any residual inventory. The tiny move stays negative.
        assert -0.001 < ret_proxy < 0.0
    finally:
        conn.close()


def test_grid_outcome_does_not_treat_short_per_leg_tp_as_whole_grid_success(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "grid-short-tp.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_200_000
        candles = [
            {"open": 100.0, "high": 100.02, "low": 99.98, "close": 99.99},
            {"open": 99.99, "high": 100.01, "low": 99.70, "close": 99.95},
            {"open": 99.95, "high": 100.00, "low": 99.92, "close": 99.98},
            {"open": 99.98, "high": 100.02, "low": 99.96, "close": 100.00},
        ]
        _seed_1m_rows(conn, base_ts=base_ts, symbol="BTCUSDT", venue="linear", candles=candles)

        params = {
            "grid_levels": 20,
            "grid_spacing_pct": 0.4,
            "trade_plan": {
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                    "tp_per_leg": {"pct": 0.25},
                }
            },
        }

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            99.98,
            base_ts,
            base_ts + len(candles) * 60,
            "short",
            params,
        )

        assert success == 0
        # Short mode also pays both entry and terminal-close friction; the tiny
        # favorable move is not enough to cover those executable costs.
        assert -0.001 < ret_proxy < 0.0
    finally:
        conn.close()


def test_grid_outcome_keeps_neutral_one_sided_tp_touch_on_oscillation_pnl_logic(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "grid-neutral-one-sided-tp.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_300_000
        candles = [
            {"open": 100.0, "high": 100.01, "low": 99.99, "close": 100.00},
            {"open": 100.00, "high": 100.28, "low": 99.99, "close": 100.22},
            {"open": 100.22, "high": 100.24, "low": 100.18, "close": 100.21},
            {"open": 100.21, "high": 100.23, "low": 100.17, "close": 100.20},
        ]
        _seed_1m_rows(conn, base_ts=base_ts, symbol="BTCUSDT", venue="linear", candles=candles)

        params = {
            "grid_levels": 20,
            "grid_spacing_pct": 0.4,
            "trade_plan": {
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                    "tp_per_leg": {"abs": 0.25},
                }
            },
        }

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.20,
            base_ts,
            base_ts + len(candles) * 60,
            "neutral",
            params,
        )

        assert success == 0
        # Neutral starts flat. An intrabar high without a close crossing does not
        # prove a fill, so the conservative close-to-close ledger stays flat.
        assert ret_proxy == 0.0
    finally:
        conn.close()
