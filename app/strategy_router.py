from __future__ import annotations

import math
from typing import Any

from .policy import is_sha256_fingerprint

ROUTER_VERSION = "strategy-profitability-router-v2"
COMPARISON_RETURN_BASIS = "unlevered_net_return_on_committed_notional_v1"
ACTIONABLE_STATUSES: frozenset[str] = frozenset({"recommended", "active", "pending"})
MIN_POSITIVE_UTILITY = 0.00005  # 0.5 bp on the common unlevered return basis.
MIN_ABSOLUTE_UTILITY_EDGE = 0.00020  # 2 bps required to choose between two ready policies.
MIN_RELATIVE_UTILITY_EDGE = 0.10
TAIL_PENALTY_WEIGHT = 0.05
CONFIDENCE_UPSIDE_WEIGHT = 0.20


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def evaluate_candidate(rec: dict[str, Any]) -> dict[str, Any]:
    """Return a comparable risk-adjusted monetary utility for one strategy.

    Raw scores are intentionally excluded: grid and trend scores have different
    semantics. A candidate is comparable only when its own bot-specific model has
    passed purged OOF probability validation, selected-policy monetary validation,
    terminal holdout validation, and emits the shared unlevered net-return basis.
    """
    reasons = _mapping(rec.get("reasons"))
    confidence_model = _mapping(reasons.get("confidence_model"))
    outcome_policy = _mapping(reasons.get("outcome_policy"))
    operator_metrics = _mapping(reasons.get("operator_metrics"))
    empirical = _mapping(operator_metrics.get("empirical_expectancy"))
    params = _mapping(rec.get("params"))
    bot_type = str(rec.get("bot_type") or "")
    trend_event = _mapping(reasons.get("trend_event_model"))

    reason_codes: list[str] = []
    status = str(rec.get("status") or "")
    if status not in ACTIONABLE_STATUSES:
        reason_codes.append("CANDIDATE_NOT_ACTIONABLE")

    if str(confidence_model.get("source") or "") != "bot_logreg" or confidence_model.get("fitted") is not True:
        reason_codes.append("BOT_SPECIFIC_CALIBRATION_REQUIRED")
    fingerprint = str(confidence_model.get("policy_fingerprint") or "").strip().lower()
    if not is_sha256_fingerprint(fingerprint):
        reason_codes.append("POLICY_FINGERPRINT_INVALID")
    if str(confidence_model.get("selected_policy_expectancy_status") or "") != "positive":
        reason_codes.append("SELECTED_POLICY_EXPECTANCY_NOT_POSITIVE")
    if str(confidence_model.get("terminal_selected_policy_expectancy_status") or "") != "positive":
        reason_codes.append("TERMINAL_POLICY_EXPECTANCY_NOT_POSITIVE")
    if empirical.get("decision_ready") is not True or str(empirical.get("gate_status") or "") != "positive":
        reason_codes.append("EMPIRICAL_EXPECTANCY_NOT_DECISION_READY")
    if str(outcome_policy.get("comparison_return_basis") or "") != COMPARISON_RETURN_BASIS:
        reason_codes.append("RETURN_BASIS_NOT_COMPARABLE")
    horizon_hours = _finite(params.get("label_horizon_hours"))
    if horizon_hours is None or abs(horizon_hours - 12.0) > 1e-9:
        reason_codes.append("HORIZON_NOT_COMPARABLE")

    confidence = _finite(rec.get("confidence"))
    threshold = _finite(confidence_model.get("selected_policy_confidence_threshold"))
    if confidence is None or threshold is None or not (0.0 <= threshold < 1.0) or not (threshold <= confidence <= 1.0):
        reason_codes.append("CANDIDATE_CONFIDENCE_BELOW_VALIDATED_POLICY")

    selected_mean = _finite(confidence_model.get("selected_policy_weighted_mean_return"))
    terminal_mean = _finite(confidence_model.get("terminal_selected_policy_weighted_mean_return"))
    lower_bounds = [
        _finite(confidence_model.get("selected_policy_weighted_mean_return_lower_bound")),
        _finite(confidence_model.get("selected_policy_weighted_temporal_mean_return_lower_bound")),
        _finite(confidence_model.get("terminal_selected_policy_weighted_mean_return_lower_bound")),
        _finite(confidence_model.get("terminal_selected_policy_weighted_temporal_mean_return_lower_bound")),
    ]
    if selected_mean is None or terminal_mean is None:
        reason_codes.append("POLICY_MEAN_RETURN_UNAVAILABLE")
    if any(value is None for value in lower_bounds):
        reason_codes.append("POLICY_LOWER_BOUND_UNAVAILABLE")
    elif min(float(value) for value in lower_bounds if value is not None) <= 0.0:
        reason_codes.append("POLICY_LOWER_BOUND_NON_POSITIVE")

    expected_shortfall = _finite(confidence_model.get("selected_policy_weighted_expected_shortfall"))
    if expected_shortfall is None:
        reason_codes.append("EXPECTED_SHORTFALL_UNAVAILABLE")

    trend_event_expected = None
    trend_event_lower = None
    if bot_type == "directional_trend":
        if (
            trend_event.get("ready") is not True
            or str(trend_event.get("source") or "") != "trend_event_softmax"
            or str(trend_event.get("model_version") or "") != "trend-first-touch-softmax-v1"
            or str(trend_event.get("outcome_label_version") or "") != "directional_trend_label_v2"
            or str(trend_event.get("return_basis") or COMPARISON_RETURN_BASIS) != COMPARISON_RETURN_BASIS
            or not is_sha256_fingerprint(str(trend_event.get("policy_fingerprint") or "").strip().lower())
        ):
            reason_codes.append("TREND_FIRST_TOUCH_MODEL_REQUIRED")
        probabilities = [
            _finite(trend_event.get("tp_first_probability")),
            _finite(trend_event.get("sl_first_probability")),
            _finite(trend_event.get("horizon_exit_probability")),
        ]
        if any(value is None for value in probabilities) or abs(sum(float(value) for value in probabilities if value is not None) - 1.0) > 1e-6:
            reason_codes.append("TREND_FIRST_TOUCH_PROBABILITIES_INVALID")
        tp_lower = _finite(trend_event.get("tp_first_probability_lower_bound"))
        sl_upper = _finite(trend_event.get("sl_first_probability_upper_bound"))
        if tp_lower is None or sl_upper is None or tp_lower <= sl_upper:
            reason_codes.append("TREND_FIRST_TOUCH_ORDER_NOT_SUPPORTED")
        trend_event_expected = _finite(trend_event.get("event_expected_net_return"))
        trend_event_lower = _finite(trend_event.get("event_expected_net_return_lower_bound"))
        if (
            trend_event_expected is None
            or trend_event_lower is None
            or trend_event_expected <= 0.0
            or trend_event_lower <= 0.0
        ):
            reason_codes.append("TREND_FIRST_TOUCH_EXPECTANCY_NON_POSITIVE")

    if reason_codes:
        return {
            "eligible": False,
            "utility": None,
            "reason_codes": reason_codes,
            "router_version": ROUTER_VERSION,
            "bot_type": str(rec.get("bot_type") or ""),
            "confidence": confidence,
        }

    conservative_lower = min(float(value) for value in lower_bounds if value is not None)
    stable_mean = min(float(selected_mean), float(terminal_mean))
    if bot_type == "directional_trend" and trend_event_lower is not None and trend_event_expected is not None:
        conservative_lower = min(conservative_lower, float(trend_event_lower))
        stable_mean = min(stable_mean, float(trend_event_expected))
    confidence_margin = max(0.0, min(1.0, (float(confidence) - float(threshold)) / max(1e-12, 1.0 - float(threshold))))
    evidence_upside = max(0.0, stable_mean - conservative_lower)
    candidate_expectancy = conservative_lower + CONFIDENCE_UPSIDE_WEIGHT * confidence_margin * evidence_upside
    tail_loss = max(0.0, -float(expected_shortfall))
    tail_penalty = TAIL_PENALTY_WEIGHT * tail_loss
    utility = candidate_expectancy - tail_penalty
    eligible = utility > MIN_POSITIVE_UTILITY
    if not eligible:
        reason_codes.append("RISK_ADJUSTED_UTILITY_NON_POSITIVE")

    return {
        "eligible": bool(eligible),
        "utility": float(utility),
        "candidate_expectancy": float(candidate_expectancy),
        "conservative_lower_bound": float(conservative_lower),
        "stable_policy_mean": float(stable_mean),
        "expected_shortfall": float(expected_shortfall),
        "tail_penalty": float(tail_penalty),
        "confidence": float(confidence),
        "confidence_threshold": float(threshold),
        "confidence_margin": float(confidence_margin),
        "return_basis": COMPARISON_RETURN_BASIS,
        "horizon_hours": float(horizon_hours),
        "trend_event_expected_net_return": trend_event_expected,
        "trend_event_expected_net_return_lower_bound": trend_event_lower,
        "reason_codes": reason_codes,
        "router_version": ROUTER_VERSION,
        "bot_type": str(rec.get("bot_type") or ""),
    }


def select_strategy(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    evaluations: dict[str, dict[str, Any]] = {}
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for rec in candidates:
        rec_id = str(rec.get("rec_id") or "").strip()
        if not rec_id:
            continue
        evaluation = evaluate_candidate(rec)
        evaluations[rec_id] = evaluation
        if evaluation.get("eligible") is True:
            eligible.append((rec, evaluation))

    if not eligible:
        return {
            "status": "no_eligible_strategy",
            "reason_code": "NO_STRATEGY_WITH_VALIDATED_POSITIVE_UTILITY",
            "winner_rec_id": None,
            "winner_bot_type": None,
            "utility_edge": None,
            "router_version": ROUTER_VERSION,
            "candidates": evaluations,
        }

    eligible.sort(
        key=lambda item: (
            float(item[1].get("utility") or -math.inf),
            float(item[1].get("conservative_lower_bound") or -math.inf),
            float(item[1].get("confidence") or 0.0),
            str(item[0].get("rec_id") or ""),
        ),
        reverse=True,
    )
    winner_rec, winner_eval = eligible[0]
    if len(eligible) > 1:
        runner_eval = eligible[1][1]
        edge = float(winner_eval["utility"]) - float(runner_eval["utility"])
        relative_edge = edge / max(abs(float(runner_eval["utility"])), MIN_POSITIVE_UTILITY)
        if edge < MIN_ABSOLUTE_UTILITY_EDGE and relative_edge < MIN_RELATIVE_UTILITY_EDGE:
            return {
                "status": "no_clear_winner",
                "reason_code": "STRATEGY_UTILITY_EDGE_INSUFFICIENT",
                "winner_rec_id": None,
                "winner_bot_type": None,
                "utility_edge": float(edge),
                "relative_utility_edge": float(relative_edge),
                "router_version": ROUTER_VERSION,
                "candidates": evaluations,
            }
    else:
        edge = None
        relative_edge = None

    return {
        "status": "selected",
        "reason_code": "RISK_ADJUSTED_UTILITY_WINNER",
        "winner_rec_id": str(winner_rec.get("rec_id") or ""),
        "winner_bot_type": str(winner_rec.get("bot_type") or ""),
        "winner_direction": str(winner_rec.get("direction") or ""),
        "winner_utility": float(winner_eval["utility"]),
        "utility_edge": None if edge is None else float(edge),
        "relative_utility_edge": None if relative_edge is None else float(relative_edge),
        "router_version": ROUTER_VERSION,
        "candidates": evaluations,
    }
