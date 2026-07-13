from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app.outcomes import _grid_outcome


def _seed_1m(conn, base_ts: int, candles: list[dict[str, float]]) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
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


def test_directional_per_leg_tp_touch_does_not_override_unresolved_whole_grid_loss(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "tp-open-loss.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_900_000
        candles = [
            # Each candle has only one excursion beyond its open/close segment,
            # so the path is labelable under the strict intrabar contract.
            {"open": 100.0, "high": 100.35, "low": 100.0, "close": 100.10},
            {"open": 100.10, "high": 100.10, "low": 95.00, "close": 95.20},
            {"open": 95.20, "high": 95.20, "low": 95.00, "close": 95.10},
        ]
        _seed_1m(conn, base_ts, candles)
        params = {
            "grid_count": 20,
            "grid_levels": 20,
            "grid_spacing_pct": 0.4,
            "cost_model": {"execution_cost_bps": 15.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "grid_count": 20,
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.5, "upper": 105.5},
                    "tp_per_leg": {"abs": 0.25},
                },
            },
        }

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            95.10,
            base_ts,
            base_ts + len(candles) * 60,
            "long",
            params,
        )

        assert success == 0
        assert ret_proxy < 0.0
    finally:
        conn.close()


def test_grid_proxy_return_is_normalized_by_committed_grid_capital(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "capital-normalization.db"))
    try:
        db.init_db(conn)
        base_ts = 1_701_000_000
        closes = [100.0, 101.1, 99.9, 101.1, 99.9]
        candles = []
        for idx, close in enumerate(closes):
            open_price = 100.0 if idx == 0 else closes[idx - 1]
            candles.append(
                {
                    "open": open_price,
                    "high": max(open_price, close),
                    "low": min(open_price, close),
                    "close": close,
                }
            )
        _seed_1m(conn, base_ts, candles)
        common = {
            "grid_spacing_pct": 1.0,
            "cost_model": {"execution_cost_bps": 15.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "levels": {
                    "kill_switch": {"lower": 85.0, "upper": 115.0},
                    "tp_per_leg": {"abs": 20.0},
                },
            },
        }

        two_grid = {
            **common,
            "grid_count": 2,
            "grid_levels": 2,
            "price_range_lower": 99.0,
            "price_range_upper": 101.0,
            "trade_plan": {
                **common["trade_plan"],
                "levels": {**common["trade_plan"]["levels"], "range": {"lower": 99.0, "upper": 101.0}},
            },
        }
        twenty_grid = {
            **common,
            "grid_count": 20,
            "grid_levels": 20,
            "price_range_lower": 90.0,
            "price_range_upper": 110.0,
            "trade_plan": {
                **common["trade_plan"],
                "levels": {**common["trade_plan"]["levels"], "range": {"lower": 90.0, "upper": 110.0}},
            },
        }

        _, ret_two = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + len(candles) * 60, "neutral",
            two_grid,
        )
        _, ret_twenty = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + len(candles) * 60, "neutral",
            twenty_grid,
        )

        assert ret_two > ret_twenty > 0.0
        assert ret_two / ret_twenty == pytest.approx(10.0)
    finally:
        conn.close()


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration209.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration209-lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def test_execution_preflight_blocks_kill_switch_loss_above_remaining_daily_budget(
    app_main,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = {
        "rec_id": "R-daily-risk",
        "ts": 1_701_100_000,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "grid_count": 10,
            "grid_levels": 10,
            "leverage": 3,
            "cost_model": {"execution_cost_bps": 15.0},
            "sizing": {"estimated_max_position_notional_usdt": 500.0},
            "economics": {"estimated_max_position_notional_usdt": 500.0},
            "risk_report": {"decision": "recommended"},
            "trade_plan": {
                "reference_price": 100.0,
                "grid_count": 10,
                "levels": {
                    "range": {"lower": 96.0, "upper": 104.0},
                    "kill_switch": {"lower": 95.0, "upper": 105.0},
                },
            },
        },
    }
    limits = {
        "max_concurrent_bots": 1,
        "max_daily_dd_usdt": 10.0,
        "cooldown_after_loss_min": 90,
        "max_symbol_bots": 1,
        "min_leverage": 3,
        "max_leverage": 5,
        "max_position_notional_usdt": 500.0,
        "max_margin_per_bot_usdt": 100.0,
    }
    risk_status = SimpleNamespace(daily_dd=8.0)

    monkeypatch.setattr(app_main, "get_risk_limits", lambda *_args, **_kwargs: limits)
    monkeypatch.setattr(app_main, "compute_risk_status", lambda *_args, **_kwargs: risk_status)
    monkeypatch.setattr(app_main, "_compute_live_validation_strategy_health", lambda *_args, **_kwargs: {"blocks": []})
    monkeypatch.setattr(app_main, "_snap_reco_payload_to_bybit_meta", lambda payload, _meta: payload)
    monkeypatch.setattr(app_main, "_execution_recommendation_freshness_blocks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app_main, "_execution_market_data_blocks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app_main, "_execution_live_price_blocks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app_main, "_execution_funding_blocks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app_main, "_get_app_config_mapping", lambda *_args, **_kwargs: {"state": "normal"})
    monkeypatch.setattr(app_main, "apply_market_shock_gate", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(app_main.db, "get_latest_features", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(app_main, "compute_symbol_fast_veto", lambda *_args, **_kwargs: {"blocks": []})
    monkeypatch.setattr(
        app_main,
        "_validate_trade_plan_against_bybit_meta",
        lambda *_args, **_kwargs: {"ok": True, "errors": [], "warnings": []},
    )

    result = app_main._execution_preflight(object(), rec, now_ts=1_701_100_100, bybit_meta={})
    blocks = {item["code"]: item for item in result["blocks"]}

    assert "DAILY_LOSS_BUDGET_EXCEEDED" in blocks
    assert blocks["DAILY_LOSS_BUDGET_EXCEEDED"]["estimated_kill_switch_loss_usdt"] > 2.0
    assert blocks["DAILY_LOSS_BUDGET_EXCEEDED"]["remaining_daily_loss_budget_usdt"] == pytest.approx(2.0)


def test_outcome_semantics_bump_label_version_to_avoid_mixing_legacy_calibration(app_main) -> None:
    assert app_main.OUTCOME_LABEL_VERSION == "grid_label_v26"
