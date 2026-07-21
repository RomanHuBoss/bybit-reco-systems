from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app import db, outcomes, recommender


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration276.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration276_runtime_lock.db"))
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
        "max_order_qty": "1000",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def _grid_rec(*, generated: bool = True, qty: float = 0.00025, grid_count: int = 4) -> dict:
    reference = 100000.0
    lower = 99000.0
    upper = 101000.0
    sizing = {
        "qty_per_order": qty,
        "order_notional_usdt": qty * reference,
        "estimated_total_order_notional_usdt": qty * reference * grid_count,
        "estimated_margin_required_usdt": qty * reference * grid_count / 3.0,
    }
    if generated:
        sizing.update({
            "basis": "minimum_viable_operator_default",
            "exchange_filter_assumption": {
                "mode": "provisional_target_notional_until_bybit_preflight",
                "actual_bybit_filters_required": True,
            },
        })
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "params": {
            "grid_count": grid_count,
            "grid_levels": grid_count,
            "grid_type": "arithmetic",
            "grid_geometry_model": "bybit_arithmetic_range_width_div_grid_count",
            "leverage": 3,
            "price_ref": reference,
            "price_range_lower": lower,
            "price_range_upper": upper,
            "sizing": dict(sizing),
            "economics": dict(sizing),
            "trade_plan": {
                "reference_price": reference,
                "grid_count": grid_count,
                "grid_type": "arithmetic",
                "sizing": dict(sizing),
                "levels": {
                    "range": {"lower": lower, "upper": upper},
                    "kill_switch": {"lower": 98500.0, "upper": 101500.0},
                    "grid_step": {"step_abs": 500.0, "step_pct": 0.5},
                    "tp_per_leg": {"abs": 500.0, "pct": 0.5},
                },
            },
        },
    }


def test_generated_grid_qty_is_lifted_to_the_minimum_exchange_executable_step(app_main) -> None:
    rec = _grid_rec(generated=True)
    snapped = app_main._snap_reco_payload_to_bybit_meta(rec, _meta())

    sizing = snapped["params"]["sizing"]
    assert sizing["qty_per_order"] == pytest.approx(0.001)
    assert sizing["requested_qty_per_order"] == pytest.approx(0.00025)
    assert sizing["minimum_executable_qty"] == pytest.approx(0.001)
    assert sizing["qty_adjustment_reason"] == "exchange_minimum_for_generated_default"
    assert sizing["estimated_worst_case_total_order_notional_usdt"] > 0

    validation = app_main._validate_trade_plan_against_bybit_meta(
        snapped,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    codes = {item["code"] for item in validation["errors"]}
    assert "ORDER_QTY_BELOW_MIN" not in codes
    assert "ORDER_QTY_OFF_STEP" not in codes
    assert "ORDER_NOTIONAL_BELOW_MIN" not in codes


def test_manual_grid_qty_is_never_increased_and_does_not_emit_a_derivative_off_step_error(app_main) -> None:
    rec = _grid_rec(generated=False, qty=0.00025)
    snapped = app_main._snap_reco_payload_to_bybit_meta(rec, _meta())
    assert snapped["params"]["sizing"]["qty_per_order"] == pytest.approx(0.00025)

    validation = app_main._validate_trade_plan_against_bybit_meta(
        snapped,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    codes = [item["code"] for item in validation["errors"]]
    assert "ORDER_QTY_BELOW_MIN" in codes
    assert "ORDER_QTY_OFF_STEP" not in codes


def test_exchange_minimum_upsize_is_rechecked_against_full_grid_runtime_caps(app_main) -> None:
    snapped = app_main._snap_reco_payload_to_bybit_meta(_grid_rec(generated=True), _meta())
    blocks = app_main._execution_runtime_size_risk_blocks(
        snapped,
        {
            "max_concurrent_bots": 1,
            "max_daily_dd_usdt": 10.0,
            "cooldown_after_loss_min": 90,
            "max_symbol_bots": 1,
            "min_leverage": 1,
            "max_leverage": 5,
            "max_position_notional_usdt": 50.0,
            "max_margin_per_bot_usdt": 20.0,
        },
    )
    codes = {item["code"] for item in blocks}
    assert "MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION" in codes
    assert "MAX_MARGIN_PER_BOT_AT_EXECUTION" in codes


def test_ui_prefers_concrete_exchange_code_over_duplicate_generic_risk_message() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/ui/static/app.js").read_text(encoding="utf-8")
    start = source.index("function uniqueBlockerItems")
    end = source.index("function outcomeTrackingHtml", start)
    helper = source[start:end]
    payload = [
        {"code": "ORDER_QTY_BELOW_MIN", "msg": "qty=0.00025 ниже Bybit min_order_qty=0.001.", "critical": True},
        {"code": "RISK", "msg": "qty=0.00025 ниже Bybit min_order_qty=0.001.", "critical": True},
        {"code": "WARN", "msg": "сильный тренд ломает сетку", "critical": False},
    ]
    script = helper + "\nconsole.log(JSON.stringify(uniqueBlockerItems(" + json.dumps(payload, ensure_ascii=False) + ")));"
    completed = subprocess.run(["node", "-e", script], cwd=root, text=True, capture_output=True, check=True)
    result = json.loads(completed.stdout)
    assert [item["code"] for item in result] == ["ORDER_QTY_BELOW_MIN", "WARN"]


def _trend_params() -> dict:
    return {
        "strategy_family": "directional_trend",
        "entry_model": "single_position_no_pyramiding",
        "cost_model": {
            "execution_cost_bps": 0.0,
            "expected_funding_events": 0,
            "expected_funding_bps": 0.0,
        },
        "trade_plan": {
            "reference_price": 100.0,
            "entry_model": "single_position_no_pyramiding",
            "levels": {
                "take_profit": {"price": 110.0},
                "stop_loss": {"price": 95.0},
            },
        },
    }


def test_gap_exit_excursions_do_not_include_post_exit_candle_extremes(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "gap_excursion.db"))
    db.init_db(conn)
    ts0 = 1_700_000_000
    db.upsert_ohlcv(conn, [{
        "venue": "linear",
        "symbol": "BTCUSDT",
        "tf_sec": 60,
        "ts": ts0,
        "open": 90.0,
        "high": 120.0,
        "low": 80.0,
        "close": 100.0,
        "volume": 1000.0,
    }])
    diagnostics: dict[str, object] = {}
    result = outcomes._directional_trend_outcome(
        conn,
        "linear",
        "BTCUSDT",
        100.0,
        100.0,
        ts0,
        ts0 + 60,
        "long",
        _trend_params(),
        diagnostics=diagnostics,
    )
    conn.close()
    assert result is not None
    assert diagnostics["event_type"] == "SL_FIRST"
    assert diagnostics["exit_price"] == pytest.approx(90.0)
    assert diagnostics["mfe"] == pytest.approx(0.0)
    assert diagnostics["mae"] == pytest.approx(-0.10)


def test_empty_direction_fails_closed_without_name_error() -> None:
    params = recommender._directional_trend_params(
        venue="linear",
        f={"price": 100.0},
        direction="",
        global_sent=0.0,
        direction_bias="neutral",
        direction_bias_strength=0.0,
        atr_pct=0.01,
        cost_model={"execution_cost_bps": 10.0, "funding_cost_bps_for_approval": 0.0},
        risk_limits={},
    )
    assert params["candidate_kind"] == recommender.TREND_EVALUATION_REJECTED_KIND
    assert params["direction"] == "neutral"


def _directional_rec(*, qty: float = 0.00025, leverage: float = 3.06) -> dict:
    reference = 100000.0
    sizing = {
        "mode": "external_single_position_target_notional",
        "actual_bybit_filters_required": True,
        "qty": qty,
        "target_notional_usdt": qty * reference,
        "estimated_position_notional_usdt": qty * reference,
        "estimated_margin_required_usdt": qty * reference / leverage,
    }
    return {
        "bot_type": "directional_trend",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "price_ref": reference,
            "take_profit_price": 101000.0,
            "stop_loss_price": 99000.0,
            "leverage": leverage,
            "sizing": dict(sizing),
            "trade_plan": {
                "reference_price": reference,
                "entry_model": "single_position_no_pyramiding",
                "leverage": leverage,
                "sizing": dict(sizing),
                "levels": {
                    "take_profit": {"price": 101000.0},
                    "stop_loss": {"price": 99000.0},
                },
                "external_execution_package": {
                    "entry": {
                        "qty": qty,
                        "target_notional_usdt": qty * reference,
                    },
                    "exit": {
                        "take_profit_price": 101000.0,
                        "stop_loss_price": 99000.0,
                    },
                    "leverage": leverage,
                },
            },
        },
    }


def test_generated_directional_qty_uses_minimum_exchange_step_and_recomputes_notional(app_main) -> None:
    snapped = app_main._snap_reco_payload_to_bybit_meta(_directional_rec(), _meta())
    sizing = snapped["params"]["sizing"]
    entry = snapped["params"]["trade_plan"]["external_execution_package"]["entry"]

    assert sizing["qty"] == pytest.approx(0.001)
    assert sizing["requested_qty"] == pytest.approx(0.00025)
    assert sizing["minimum_executable_qty"] == pytest.approx(0.001)
    assert sizing["target_notional_usdt"] == pytest.approx(100.0)
    assert entry["qty"] == pytest.approx(0.001)
    assert entry["target_notional_usdt"] == pytest.approx(100.0)
    assert snapped["params"]["leverage"] == pytest.approx(3.06)

    validation = app_main._validate_trade_plan_against_bybit_meta(
        snapped,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    codes = {item["code"] for item in validation["errors"]}
    assert "ORDER_QTY_BELOW_MIN" not in codes
    assert "ORDER_QTY_OFF_STEP" not in codes
    assert "ORDER_NOTIONAL_BELOW_MIN" not in codes


def test_generated_grid_leverage_never_rounds_up_to_nearest_exchange_step(app_main) -> None:
    rec = _grid_rec(generated=True)
    rec["params"]["leverage"] = 3.06
    rec["params"]["trade_plan"]["leverage"] = 3.06
    meta = _meta()
    meta["leverage_step"] = "0.1"

    snapped = app_main._snap_reco_payload_to_bybit_meta(rec, meta)
    assert snapped["params"]["leverage"] == pytest.approx(3.0)
    assert snapped["params"]["trade_plan"]["leverage"] == pytest.approx(3.0)


def test_manual_off_step_leverage_reports_the_safe_down_aligned_value(app_main) -> None:
    rec = _grid_rec(generated=False)
    rec["params"]["leverage"] = 3.06
    rec["params"]["trade_plan"]["leverage"] = 3.06
    meta = _meta()
    meta["leverage_step"] = "0.1"

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec, meta, require_meta=True, require_execution_plan=True
    )
    codes = {item["code"] for item in validation["errors"]}
    assert "LEVERAGE_OFF_STEP" in codes
    assert validation["snapped_levels"]["leverage"] == "3.0"
