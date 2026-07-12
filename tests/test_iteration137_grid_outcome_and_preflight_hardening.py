from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome


def _seed_1m_rows(conn, *, base_ts: int, symbol: str, venue: str, candles: list[dict[str, float]]) -> None:
    db.upsert_ohlcv(
        conn,
        [
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
            for idx, candle in enumerate(candles)
        ],
    )


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration137.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration137_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _meta() -> dict[str, str]:
    return {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "max_order_qty": "100",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def _base_rec() -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "grid_count": 5,
            "grid_levels": 5,
            "grid_type": "arithmetic",
            "grid_geometry_model": "bybit_arithmetic_range_width_div_grid_count",
            "actual_grid_step_abs": 0.4,
            "leverage": 1,
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "sizing": {"order_qty": 0.051, "order_notional_usdt": 5.1},
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.4, "step_pct": 0.4},
                    "tp_per_leg": {"abs": 0.3, "pct": 0.3},
                },
            },
        },
    }


def _codes(validation: dict, key: str = "errors") -> set[str]:
    return {str(item.get("code")) for item in validation.get(key, [])}


def test_grid_outcome_does_not_label_tp_touch_success_when_tp_is_below_costs(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "grid-thin-tp.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_400_000
        candles = [
            {"open": 100.0, "high": 100.015, "low": 99.995, "close": 100.005},
            {"open": 100.005, "high": 100.030, "low": 99.995, "close": 100.010},
            {"open": 100.010, "high": 100.025, "low": 99.995, "close": 100.005},
        ]
        _seed_1m_rows(conn, base_ts=base_ts, symbol="BTCUSDT", venue="linear", candles=candles)

        params = {
            "grid_levels": 20,
            "grid_spacing_pct": 0.4,
            "cost_model": {"execution_cost_bps": 12.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                    "tp_per_leg": {"abs": 0.02},
                }
            },
        }

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.005,
            base_ts,
            base_ts + len(candles) * 60,
            "long",
            params,
        )

        assert success == 0
        assert ret_proxy < 0.0
    finally:
        conn.close()


def test_execution_preflight_blocks_grid_count_step_interval_mismatch(app_main) -> None:
    rec = _base_rec()
    # Span 2.0 / step 0.5 = 4 intervals, but payload says Bybit grid_count=5.
    rec["params"]["trade_plan"]["levels"]["grid_step"]["step_abs"] = 0.5

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=True)

    assert validation["ok"] is False
    assert "GRID_STEP_LEVELS_MISMATCH" in _codes(validation)


def test_detail_preflight_warns_on_grid_count_step_interval_mismatch(app_main) -> None:
    rec = _base_rec()
    rec["params"]["trade_plan"]["levels"]["grid_step"]["step_abs"] = 0.5

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, _meta(), require_meta=False)

    assert "GRID_STEP_LEVELS_MISMATCH" in _codes(validation, "warnings")
