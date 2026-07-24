from __future__ import annotations

from copy import deepcopy

import pytest

from app import main as main_module
from app import recommender as recommender_module
from app.bot_types import SHADOW_ONLY_BOT_TYPES, SINGLE_POSITION_BOT_TYPES
from app.strategy_router import evaluate_candidate, select_strategy


def _candidate(
    rec_id: str,
    bot_type: str,
    *,
    confidence: float,
    mean: float,
    lower: float,
    temporal_lower: float,
    terminal_lower: float,
    expected_shortfall: float,
    threshold: float = 0.60,
    status: str = "recommended",
) -> dict:
    candidate = {
        "rec_id": rec_id,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": bot_type,
        "direction": "long",
        "status": status,
        "confidence": confidence,
        "score": 0.5,
        "params": {"label_horizon_hours": 12},
        "reasons": {
            "outcome_policy": {
                "comparison_return_basis": "unlevered_net_return_on_committed_notional_v1",
            },
            "confidence_model": {
                "source": "bot_logreg",
                "fitted": True,
                "policy_fingerprint": "a" * 64,
                "selected_policy_expectancy_status": "positive",
                "selected_policy_confidence_threshold": threshold,
                "selected_policy_weighted_mean_return": mean,
                "selected_policy_weighted_expected_shortfall": expected_shortfall,
                "selected_policy_weighted_mean_return_lower_bound": lower,
                "selected_policy_weighted_temporal_mean_return_lower_bound": temporal_lower,
                "terminal_selected_policy_expectancy_status": "positive",
                "terminal_selected_policy_weighted_mean_return": mean * 0.9,
                "terminal_selected_policy_weighted_mean_return_lower_bound": terminal_lower,
                "terminal_selected_policy_weighted_temporal_mean_return_lower_bound": terminal_lower * 0.9,
            },
            "operator_metrics": {
                "empirical_expectancy": {
                    "decision_ready": True,
                    "gate_status": "positive",
                    "status": "positive",
                }
            },
        },
    }
    if bot_type == "directional_trend":
        candidate["reasons"]["trend_event_model"] = {
            "ready": True,
            "source": "trend_event_softmax",
            "model_version": "trend-first-touch-softmax-v3",
            "outcome_label_version": "directional_trend_label_v2",
            "policy_fingerprint": "a" * 64,
            "return_basis": "unlevered_net_return_on_committed_notional_v1",
            "tp_first_probability": 0.65,
            "sl_first_probability": 0.20,
            "horizon_exit_probability": 0.15,
            "tp_first_probability_lower_bound": 0.60,
            "sl_first_probability_upper_bound": 0.25,
            "event_expected_net_return": mean,
            "event_expected_net_return_lower_bound": min(lower, temporal_lower, terminal_lower),
        }
    return candidate


def test_meta_router_uses_risk_adjusted_money_not_raw_score() -> None:
    grid = _candidate(
        "R-grid", "futures_grid", confidence=0.82, mean=0.0040,
        lower=0.0012, temporal_lower=0.0010, terminal_lower=0.0009,
        expected_shortfall=-0.0100,
    )
    trend = _candidate(
        "R-trend", "directional_trend", confidence=0.71, mean=0.0060,
        lower=0.0024, temporal_lower=0.0021, terminal_lower=0.0019,
        expected_shortfall=-0.0060,
    )
    # Deliberately make the losing candidate's raw score much larger.
    grid["score"] = 0.99
    trend["score"] = 0.20
    decision = select_strategy([grid, trend])
    assert decision["status"] == "selected"
    assert decision["winner_rec_id"] == "R-trend"
    assert decision["winner_bot_type"] == "directional_trend"
    assert decision["candidates"]["R-trend"]["utility"] > decision["candidates"]["R-grid"]["utility"]


def test_meta_router_fails_closed_when_edge_is_too_small() -> None:
    grid = _candidate(
        "R-grid", "futures_grid", confidence=0.75, mean=0.0040,
        lower=0.0015, temporal_lower=0.0014, terminal_lower=0.0013,
        expected_shortfall=-0.0040,
    )
    trend = _candidate(
        "R-trend", "directional_trend", confidence=0.75, mean=0.0041,
        lower=0.00152, temporal_lower=0.00142, terminal_lower=0.00131,
        expected_shortfall=-0.0040,
    )
    decision = select_strategy([grid, trend])
    assert decision["status"] == "no_clear_winner"
    assert decision["winner_rec_id"] is None
    assert decision["reason_code"] == "STRATEGY_UTILITY_EDGE_INSUFFICIENT"


def test_meta_router_rejects_raw_or_unready_probability() -> None:
    rec = _candidate(
        "R-trend", "directional_trend", confidence=0.90, mean=0.01,
        lower=0.005, temporal_lower=0.004, terminal_lower=0.003,
        expected_shortfall=-0.003,
    )
    rec["reasons"]["confidence_model"]["source"] = "raw"
    evaluation = evaluate_candidate(rec)
    assert evaluation["eligible"] is False
    assert "BOT_SPECIFIC_CALIBRATION_REQUIRED" in evaluation["reason_codes"]


def test_directional_trend_is_single_position_not_shadow_grid() -> None:
    assert "directional_trend" in SINGLE_POSITION_BOT_TYPES
    assert "directional_trend" not in SHADOW_ONLY_BOT_TYPES

    feature = {
        "price": 100.0,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.02,
        "_direction_agg": {"regime": "trend"},
    }
    params = recommender_module._directional_trend_params(
        venue="linear",
        f=feature,
        direction="long",
        global_sent=0.0,
        direction_bias="long",
        direction_bias_strength=0.8,
        atr_pct=0.02,
        cost_model={"execution_cost_bps": 10.0, "expected_funding_bps": 0.0},
        risk_limits={"min_leverage": 2, "max_leverage": 5, "max_position_notional_usdt": 100.0},
    )
    plan = recommender_module._build_trade_plan(
        "directional_trend", "linear", feature, "long", params,
        cost_model=params["cost_model"],
    )
    package = plan["external_execution_package"]
    assert package["recommendation_only"] is True
    assert package["exchange_order_submitted"] is False
    assert package["entry"]["side"] == "Buy"
    assert package["exit"]["side"] == "Sell"
    assert package["entry"]["reduce_only"] is False
    assert package["exit"]["reduce_only"] is True
    assert plan["entry_model"] == "single_position_no_pyramiding"
    assert "grid_step" not in plan["levels"]


def test_directional_trend_can_enter_internal_audit_lifecycle_but_not_place_exchange_order() -> None:
    assert main_module._is_supported_execution_direction("directional_trend", "linear", "long") is True
    assert main_module._is_supported_execution_direction("directional_trend", "linear", "short") is True
    assert main_module._is_supported_execution_direction("directional_trend", "linear", "neutral") is False


def test_directional_trend_bybit_preflight_validates_single_order_contract() -> None:
    feature = {
        "price": 100.0,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.02,
        "_direction_agg": {"regime": "trend"},
    }
    params = recommender_module._directional_trend_params(
        venue="linear", f=feature, direction="long", global_sent=0.0,
        direction_bias="long", direction_bias_strength=0.8, atr_pct=0.02,
        cost_model={"execution_cost_bps": 10.0, "expected_funding_bps": 0.0},
        risk_limits={"min_leverage": 2, "max_leverage": 5, "max_position_notional_usdt": 100.0},
    )
    params["trade_plan"] = recommender_module._build_trade_plan(
        "directional_trend", "linear", feature, "long", params,
        cost_model=params["cost_model"],
    )
    rec = {
        "venue": "linear", "symbol": "BTCUSDT", "bot_type": "directional_trend",
        "direction": "long", "account_mode": "unified", "margin_mode": "isolated",
        "params": deepcopy(params),
    }
    meta = {
        "category": "linear", "symbol": "BTCUSDT", "status": "Trading",
        "contract_type": "LinearPerpetual", "quote_coin": "USDT", "settle_coin": "USDT",
        "delivery_time": 0, "is_pre_listing": False, "unified_margin_trade": True,
        "tick_size": 0.1, "min_price": 0.1, "max_price": 1_000_000,
        "qty_step": 0.001, "min_order_qty": 0.001, "max_order_qty": 1000,
        "min_notional": 5.0, "min_leverage": 1.0, "max_leverage": 100.0,
        "leverage_step": 0.01,
    }
    result = main_module._validate_trade_plan_against_bybit_meta(
        rec, meta, require_meta=True, require_execution_plan=True,
    )
    assert result["ok"] is True, result
    assert not any(item["code"] == "DIRECTIONAL_TREND_SHADOW_ONLY" for item in result["errors"])


def test_router_annotations_preserve_loser_for_paired_outcome_learning() -> None:
    grid = _candidate(
        "R-grid", "futures_grid", confidence=0.75, mean=0.0030,
        lower=0.0010, temporal_lower=0.0009, terminal_lower=0.0008,
        expected_shortfall=-0.0060,
    )
    trend = _candidate(
        "R-trend", "directional_trend", confidence=0.80, mean=0.0060,
        lower=0.0025, temporal_lower=0.0023, terminal_lower=0.0020,
        expected_shortfall=-0.0040,
    )
    recs = [deepcopy(grid), deepcopy(trend)]
    recommender_module._apply_strategy_router(recs)
    by_id = {rec["rec_id"]: rec for rec in recs}
    assert by_id["R-trend"]["status"] == "recommended"
    assert by_id["R-grid"]["status"] == "suppressed"
    assert by_id["R-grid"]["reasons"]["outcome_policy"]["sample_role"] == "shadow_competitor"
    assert by_id["R-grid"]["reasons"]["strategy_router"]["winner_rec_id"] == "R-trend"


def _trend_rec_for_execution(*, direction: str = "long", price: float = 100.03) -> dict:
    feature = {
        "price": price,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.02,
        "_direction_agg": {"regime": "trend"},
    }
    params = recommender_module._directional_trend_params(
        venue="linear", f=feature, direction=direction, global_sent=0.0,
        direction_bias=direction, direction_bias_strength=0.8, atr_pct=0.02,
        cost_model={"execution_cost_bps": 10.0, "expected_funding_bps": 0.0, "horizon_sec": 12 * 3600},
        risk_limits={
            "min_leverage": 2, "max_leverage": 5,
            "max_position_notional_usdt": 100.0,
            "max_margin_per_bot_usdt": 100.0,
        },
    )
    params["symbol"] = "BTCUSDT"
    params["account_mode"] = "unified"
    params["trade_plan"] = recommender_module._build_trade_plan(
        "directional_trend", "linear", feature, direction, params,
        cost_model=params["cost_model"],
    )
    return {
        "rec_id": "R-trend-exec",
        "publication_root_rec_id": "R-trend-exec",
        "venue": "linear", "symbol": "BTCUSDT", "bot_type": "directional_trend",
        "direction": direction, "account_mode": "unified", "margin_mode": "isolated",
        "params": params, "status": "recommended",
    }


def _bybit_meta() -> dict:
    return {
        "category": "linear", "symbol": "BTCUSDT", "status": "Trading",
        "contract_type": "LinearPerpetual", "quote_coin": "USDT", "settle_coin": "USDT",
        "delivery_time": 0, "is_pre_listing": False, "unified_margin_trade": True,
        "tick_size": 0.1, "min_price": 0.1, "max_price": 1_000_000,
        "qty_step": 0.01, "min_order_qty": 0.01, "max_order_qty": 1000,
        "min_notional": 5.0, "min_leverage": 1.0, "max_leverage": 100.0,
        "leverage_step": 0.5,
    }


def test_trend_snap_is_exchange_aligned_and_never_increases_qty_or_leverage() -> None:
    rec = _trend_rec_for_execution()
    before_qty = rec["params"]["sizing"]["qty"]
    before_leverage = rec["params"]["leverage"]
    snapped = main_module._snap_reco_payload_to_bybit_meta(rec, _bybit_meta())
    params = snapped["params"]
    plan = params["trade_plan"]
    package = plan["external_execution_package"]
    qty = package["entry"]["qty"]
    assert qty <= before_qty
    assert abs(qty / 0.01 - round(qty / 0.01)) < 1e-9
    assert params["leverage"] <= before_leverage
    assert abs(params["leverage"] / 0.5 - round(params["leverage"] / 0.5)) < 1e-9
    assert package["symbol"] == "BTCUSDT"
    result = main_module._validate_trade_plan_against_bybit_meta(
        snapped, _bybit_meta(), require_meta=True, require_execution_plan=True,
    )
    assert result["ok"] is True, result


def test_trend_runtime_risk_caps_use_single_position_notional_and_margin() -> None:
    rec = main_module._snap_reco_payload_to_bybit_meta(_trend_rec_for_execution(), _bybit_meta())
    blocks = main_module._execution_runtime_size_risk_blocks(
        rec,
        {
            "min_leverage": 1, "max_leverage": 5,
            "max_position_notional_usdt": 10.0,
            "max_margin_per_bot_usdt": 3.0,
        },
    )
    codes = {item["code"] for item in blocks}
    assert "MAX_POSITION_NOTIONAL_AT_EXECUTION" in codes
    assert "MAX_MARGIN_PER_BOT_AT_EXECUTION" in codes


def test_trend_live_price_blocks_exhausted_or_stale_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _trend_rec_for_execution()
    tp = rec["params"]["trade_plan"]["levels"]["take_profit"]["price"]
    monkeypatch.setattr(main_module.db, "get_latest_ticker", lambda *_args, **_kwargs: {
        "last": tp, "bid": tp * 0.9999, "ask": tp * 1.0001,
    })
    monkeypatch.setattr(main_module, "_execution_live_cost_blocks", lambda *_args, **_kwargs: [])
    blocks = main_module._execution_live_price_blocks(None, rec)
    assert "CURRENT_PRICE_REACHED_TAKE_PROFIT" in {item["code"] for item in blocks}


def test_trend_funding_guard_uses_projected_directional_reward(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _trend_rec_for_execution()
    rec["params"]["economics"]["projected_net_reward_bps"] = 1.0
    rec["params"]["trade_plan"]["economics"]["projected_net_reward_bps"] = 1.0
    now = 1_900_000_000
    monkeypatch.setattr(main_module.db, "get_latest_funding_rate", lambda *_args, **_kwargs: {
        "ts": now - 10,
        "funding_rate": 0.001,
        "funding_interval_min": 480,
        "next_funding_ts": now + 60,
    })
    blocks = main_module._execution_funding_blocks(None, rec, now_ts=now)
    assert "FUNDING_EDGE_TURNED_NEGATIVE" in {item["code"] for item in blocks}


def test_symbol_conflict_blocks_grid_and_trend_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _trend_rec_for_execution()
    monkeypatch.setattr(main_module.db, "list_bot_instances", lambda *_args, **_kwargs: [{
        "bot_id": "B-grid", "bot_type": "futures_grid", "venue": "linear", "symbol": "BTCUSDT",
        "mode": {"direction": "long"}, "publication_root_rec_id": "R-grid",
    }])
    blocks = main_module._execution_symbol_direction_conflict_blocks(None, rec)
    assert blocks and blocks[0]["code"] == "SYMBOL_STRATEGY_ALREADY_RUNNING"


def test_trend_daily_stop_loss_respects_remaining_dd_budget() -> None:
    from types import SimpleNamespace
    rec = main_module._snap_reco_payload_to_bybit_meta(_trend_rec_for_execution(), _bybit_meta())
    result = main_module._execution_daily_loss_budget_guard(
        rec,
        {"max_daily_dd_usdt": 0.10},
        SimpleNamespace(daily_dd=0.0),
    )
    assert "DAILY_LOSS_BUDGET_EXCEEDED" in {item["code"] for item in result["blocks"]}


def test_materialize_directional_trend_creates_audit_instance_without_exchange_order(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from app import db
    import time

    conn = db.connect(str(tmp_path / "trend-audit.db"))
    try:
        db.init_db(conn)
        now = int(time.time())
        rec = _trend_rec_for_execution(direction="long", price=100.0)
        rec.update({
            "ts": now,
            "account_mode": "unified",
            "margin_mode": "isolated",
            "score": 0.7,
            "confidence": 0.8,
            "expected_rr": 1.5,
            "risk_score": 0.2,
            "reasons": {},
            "blocks": [],
            "ttl_sec": 900,
            "model_version": "test-router-v1",
            "features_ref_ts": now,
            "outcome_root_rec_id": rec["rec_id"],
            "is_outcome_label_root": True,
        })
        db.insert_recommendations(conn, [rec])

        monkeypatch.setattr(main_module, "_prefetch_execution_bybit_meta", lambda *_args, **_kwargs: _bybit_meta())
        monkeypatch.setattr(main_module, "get_risk_limits", lambda *_args, **_kwargs: {
            "min_leverage": 1,
            "max_leverage": 5,
            "max_position_notional_usdt": 1_000.0,
            "max_margin_per_bot_usdt": 1_000.0,
            "max_daily_dd_usdt": 1_000.0,
        })
        monkeypatch.setattr(main_module, "compute_risk_status", lambda *_args, **_kwargs: SimpleNamespace(daily_dd=0.0))
        monkeypatch.setattr(main_module, "gate_candidate", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(main_module, "_execution_symbol_direction_conflict_blocks", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(main_module, "_execution_preflight", lambda *_args, **_kwargs: {
            "blocks": [], "strategy_health": {}, "market_shock": {},
            "fast_veto": {}, "bybit_validation": {"ok": True, "errors": []},
        })
        monkeypatch.setattr(main_module, "_execution_runtime_size_risk_blocks", lambda *_args, **_kwargs: [])

        bot, existed = main_module._materialize_bot_from_rec(conn, rec["rec_id"], operator="tester")
        assert existed is False
        assert bot["bot_type"] == "directional_trend"
        assert bot["status"] == "running"
        assert bot["state"]["execution_kind"] == "external_single_order_audit"
        assert bot["state"]["recommendation_only"] is True
        assert bot["state"]["exchange_order_submitted"] is False
        package = bot["state"]["external_execution_package"]
        assert package["schema_version"] == "directional-single-order-package-v1"
        assert package["exchange_order_submitted"] is False
        refreshed = db.get_recommendation_by_id(conn, rec["rec_id"])
        assert refreshed["status"] == "executed"
    finally:
        conn.close()


def test_production_code_contains_no_private_bybit_order_submission_endpoint() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    production = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    for endpoint in (
        "/v5/order/create", "/v5/order/amend", "/v5/order/cancel",
        "/v5/order/create-batch", "/v5/order/amend-batch", "/v5/order/cancel-batch",
    ):
        assert endpoint not in production
