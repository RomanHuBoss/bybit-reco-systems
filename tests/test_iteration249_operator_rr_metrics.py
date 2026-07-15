from __future__ import annotations

from pathlib import Path

import pytest

from app.calibration import LogRegScaler, return_confidence_interval
from app.main import _operator_decision_context_for_reco
from app.recommender import _empirical_expectancy_metrics, _plan_rr_metrics


def test_plan_rr_uses_generated_plan_reward_and_kill_switch_loss() -> None:
    params = {
        "economics": {
            "qty_per_order": 1.0,
            "estimated_active_orders": 4,
            "fill_efficiency": 0.5,
            "net_profit_usdt": 10.0,
            "estimated_worst_case_total_order_notional_usdt": 1000.0,
            "cross_margin_stress": {
                "worst_side": "long",
                "long": {
                    "gross_loss": 50.0,
                    "execution_cost": 10.0,
                    "maintenance_reserve": 5.0,
                },
            },
        }
    }
    cost_model = {
        # Recurring pair fees are already included in net_profit_usdt; only
        # one-time spread/slippage is deducted at the plan horizon layer.
        "one_time_market_friction_bps": 20.0,
        "market_round_trip_cost_bps": 32.0,
        "expected_funding_bps": 10.0,
    }

    metrics = _plan_rr_metrics(params, cost_model)

    # Four active orders * 50% capture * 10 USDT per completed pair = 20 USDT.
    # One-time market friction = 2 USDT; adverse funding = 1 USDT.
    # Kill-switch loss = qty * (50 price loss + 10 exit cost) = 60 USDT.
    assert metrics["status"] == "available"
    assert metrics["projected_completed_pairs"] == pytest.approx(2.0)
    assert metrics["projected_net_reward_usdt"] == pytest.approx(17.0)
    assert metrics["kill_switch_loss_usdt"] == pytest.approx(60.0)
    assert metrics["rr"] == pytest.approx(17.0 / 60.0)
    assert metrics["is_empirical"] is False



def test_plan_rr_fails_closed_for_boolean_or_missing_cost_inputs() -> None:
    base_params = {
        "economics": {
            "qty_per_order": 1.0,
            "estimated_active_orders": 4,
            "fill_efficiency": 0.5,
            "net_profit_usdt": 10.0,
            "estimated_worst_case_total_order_notional_usdt": 1000.0,
            "cross_margin_stress": {
                "worst_side": "long",
                "long": {"gross_loss": 50.0, "execution_cost": 10.0},
            },
        }
    }
    malformed = _plan_rr_metrics(
        base_params,
        {"one_time_market_friction_bps": True, "expected_funding_bps": 0.0},
    )
    missing = _plan_rr_metrics(
        base_params,
        {"expected_funding_bps": 0.0},
    )
    assert malformed["status"] == "unavailable"
    assert missing["status"] == "unavailable"


def test_empirical_metrics_use_current_policy_temporal_cohorts_and_tail_loss() -> None:
    model = LogRegScaler(
        fitted=False,
        return_samples=80,
        expectancy_status="positive",
        weighted_mean_return=0.018,
        weighted_expected_shortfall=-0.04,
        weighted_return_std=0.03,
        weighted_effective_return_samples=64.0,
        weighted_mean_return_lower_bound=0.006,
        temporal_cluster_count=25,
        minimum_temporal_clusters=20,
        weighted_effective_temporal_clusters=25.0,
        weighted_temporal_mean_return=0.02,
        weighted_temporal_return_std=0.01,
        weighted_temporal_mean_return_lower_bound=0.015,
        expectancy_confidence_level=0.95,
        policy_fingerprint="a" * 64,
        policy_matured_total=80,
        policy_labeled_total=80,
        policy_censored_total=0,
        policy_unresolved_total=0,
        policy_invalid_labeled_total=0,
    )

    metrics = _empirical_expectancy_metrics(model)

    assert metrics["status"] == "positive"
    assert metrics["available"] is True
    assert metrics["decision_ready"] is True
    assert metrics["mean_basis"] == "non_overlapping_temporal_cohorts"
    assert metrics["mean_return"] == pytest.approx(0.02)
    assert metrics["expected_shortfall"] == pytest.approx(-0.04)
    assert metrics["empirical_rr"] == pytest.approx(0.5)
    assert metrics["confidence_interval"]["lower"] < 0.02
    assert metrics["confidence_interval"]["upper"] > 0.02



def test_empirical_display_status_follows_two_sided_interval_not_only_gate_status() -> None:
    model = LogRegScaler(
        expectancy_status="positive",
        weighted_mean_return=0.01,
        weighted_expected_shortfall=-0.03,
        weighted_return_std=0.05,
        weighted_effective_return_samples=25.0,
        temporal_cluster_count=25,
        minimum_temporal_clusters=20,
        weighted_effective_temporal_clusters=25.0,
        weighted_temporal_mean_return=0.01,
        weighted_temporal_return_std=0.05,
        expectancy_confidence_level=0.95,
        policy_fingerprint="b" * 64,
        policy_matured_total=80,
        policy_labeled_total=80,
    )
    metrics = _empirical_expectancy_metrics(model)
    assert metrics["gate_status"] == "positive"
    assert metrics["confidence_interval"]["lower"] < 0.0
    assert metrics["status"] == "uncertain"


def test_two_sided_confidence_interval_is_symmetric_and_fail_closed() -> None:
    lower, upper = return_confidence_interval(0.02, 0.01, 25.0, confidence_level=0.95)
    assert lower is not None and upper is not None
    assert (lower + upper) / 2.0 == pytest.approx(0.02)
    assert return_confidence_interval(True, 0.01, 25.0) == (None, None)
    assert return_confidence_interval(0.02, 0.01, 1.0) == (None, None)


def test_operator_context_exposes_plan_and_empirical_metrics_without_legacy_proxy() -> None:
    rec = {
        "rec_id": "R-test",
        "ts": 1_700_000_000,
        "ttl_sec": 300,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "params": {
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 90.0, "upper": 110.0},
                },
            },
            "economics": {"net_profit_bps": 8.0, "cross_margin_stress_buffer_pct": 25.0},
        },
        "reasons": {
            "operator_metrics": {
                "plan_rr": {
                    "status": "available",
                    "rr": 0.75,
                    "projected_net_reward_usdt": 15.0,
                    "kill_switch_loss_usdt": 20.0,
                    "projected_completed_pairs": 5.0,
                },
                "empirical_expectancy": {
                    "status": "uncertain",
                    "available": True,
                    "decision_ready": True,
                    "mean_return": 0.01,
                    "expected_shortfall": -0.03,
                    "empirical_rr": 1 / 3,
                    "return_samples": 50,
                    "temporal_cluster_count": 20,
                    "minimum_temporal_clusters": 20,
                    "confidence_interval": {"lower": -0.005, "upper": 0.025, "level": 0.95},
                },
                "heuristic_capture_score": {"value": 0.12, "operator_visible": False},
            }
        },
        "blocks": [],
        "status": "no_trade",
    }

    ctx = _operator_decision_context_for_reco(rec, conn=None, guard=None)

    assert ctx["plan_rr"] == pytest.approx(0.75)
    assert ctx["plan_projected_net_reward_usdt"] == pytest.approx(15.0)
    assert ctx["plan_kill_switch_loss_usdt"] == pytest.approx(20.0)
    assert ctx["empirical_expectancy_status"] == "uncertain"
    assert ctx["empirical_mean_return"] == pytest.approx(0.01)
    assert "expected_rr" not in ctx


def test_operator_ui_replaces_capture_proxy_with_plan_and_empirical_metrics() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "app/ui/static/index.html").read_text(encoding="utf-8")
    js = (root / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Прокси capture/risk" not in html
    assert ">Plan RR<" in html
    assert ">Emp. expectancy<" in html
    assert ">Risk buffer<" in html
    assert 'data-sort="score"' not in html
    assert 'data-sort="confidence"' not in html
    assert 'data-sort="dir_conf"' not in html
    assert 'id="minConf" type="hidden" value="0"' in html
    assert "function planRrCell" in js
    assert "function empiricalExpectancyCell" in js
    assert "function riskBufferCell" in js
    assert "${fmt(it.expected_rr)}" not in js
    assert "expected_rr: it.expected_rr" not in js
    assert 'label: "Empirical expectancy"' in js
    assert 'label: "Empirical tail / RR"' in js
    assert 'label: "Risk/Reward TP/SL"' not in js
