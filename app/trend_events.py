from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from .calibration import FEATURE_NAMES, _one_sided_student_t_critical, extract_features
from .policy import is_sha256_fingerprint
from .trading_semantics import normalize_execution_direction

TREND_EVENT_TYPES: tuple[str, ...] = ("TP_FIRST", "SL_FIRST", "HORIZON_EXIT")
TREND_CENSORED_EVENT_TYPES: frozenset[str] = frozenset({"AMBIGUOUS"})
TREND_EVENT_MODEL_VERSION = "trend-first-touch-softmax-v2"
TREND_EVENT_MODEL_KEY = "trend_event_softmax_v2"
TREND_EVENT_RETURN_BASIS = "unlevered_net_return_on_committed_notional_v1"


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _softmax(values: list[float]) -> list[float]:
    if not values:
        return []
    safe = [max(-80.0, min(80.0, float(value))) for value in values]
    peak = max(safe)
    exp_values = [math.exp(value - peak) for value in safe]
    total = sum(exp_values)
    if not math.isfinite(total) or total <= 0.0:
        return [1.0 / len(values)] * len(values)
    return [value / total for value in exp_values]


def _mean_lower_bound(values: list[float]) -> tuple[float | None, float | None]:
    finite = [float(value) for value in values if _finite(value) is not None]
    if not finite:
        return None, None
    mean = sum(finite) / len(finite)
    if len(finite) < 2:
        return mean, None
    variance = sum((value - mean) ** 2 for value in finite) / (len(finite) - 1)
    std = math.sqrt(max(0.0, variance))
    critical = _one_sided_student_t_critical(0.95, float(len(finite)))
    lower = mean - critical * std / math.sqrt(len(finite))
    return float(mean), float(lower)


def _strict_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    result = int(number)
    return result if result > 0 else None


def _probability_uncertainty_bound(
    labels: list[int],
    probabilities: list[list[float]],
    *,
    class_count: int,
    alpha: float = 0.05,
) -> tuple[float | None, float | None]:
    """Return a conservative finite-sample validation uncertainty proxy.

    The bound combines the largest observed class-frequency calibration gap on
    the untouched terminal holdout with a simultaneous Hoeffding sampling term.
    It is deliberately not capped at a cosmetically small value: sparse evidence
    must keep the first-touch router fail-closed.
    """
    if not labels or len(labels) != len(probabilities) or class_count <= 1:
        return None, None
    n = len(labels)
    observed = [sum(1 for label in labels if label == idx) / n for idx in range(class_count)]
    predicted = [sum(row[idx] for row in probabilities) / n for idx in range(class_count)]
    gap = max(abs(observed[idx] - predicted[idx]) for idx in range(class_count))
    sampling = math.sqrt(math.log((2.0 * class_count) / max(1e-12, alpha)) / (2.0 * n))
    return float(gap), float(min(1.0, gap + sampling))


def _multiclass_log_loss(labels: list[int], probabilities: list[list[float]]) -> float | None:
    if not labels or len(labels) != len(probabilities):
        return None
    total = 0.0
    for label, probs in zip(labels, probabilities):
        if label < 0 or label >= len(probs):
            return None
        probability = max(1e-12, min(1.0, float(probs[label])))
        total -= math.log(probability)
    return total / len(labels)


def _fit_softmax(
    xs: list[list[float]],
    ys: list[int],
    *,
    class_count: int,
    iterations: int = 600,
    learning_rate: float = 0.08,
    l2: float = 0.002,
) -> tuple[list[list[float]], list[float]]:
    if not xs or len(xs) != len(ys):
        return [], []
    feature_count = len(FEATURE_NAMES)
    coef = [[0.0] * feature_count for _ in range(class_count)]
    intercept = [0.0] * class_count
    n = float(len(xs))
    for iteration in range(max(1, int(iterations))):
        grad_coef = [[0.0] * feature_count for _ in range(class_count)]
        grad_intercept = [0.0] * class_count
        for features, label in zip(xs, ys):
            logits = [
                intercept[class_index]
                + sum(coef[class_index][j] * features[j] for j in range(feature_count))
                for class_index in range(class_count)
            ]
            probs = _softmax(logits)
            for class_index in range(class_count):
                error = probs[class_index] - (1.0 if label == class_index else 0.0)
                grad_intercept[class_index] += error
                for j in range(feature_count):
                    grad_coef[class_index][j] += error * features[j]
        step = learning_rate / math.sqrt(1.0 + iteration / 100.0)
        for class_index in range(class_count):
            intercept[class_index] -= step * grad_intercept[class_index] / n
            for j in range(feature_count):
                gradient = grad_coef[class_index][j] / n + l2 * coef[class_index][j]
                coef[class_index][j] -= step * gradient
    return coef, intercept


@dataclass
class TrendEventModel:
    classes: tuple[str, ...] = TREND_EVENT_TYPES
    coef: list[list[float]] = field(default_factory=list)
    intercept: list[float] = field(default_factory=list)
    fitted: bool = False
    saved_ts: int = 0
    n_samples: int = 0
    class_counts: dict[str, int] = field(default_factory=dict)
    fit_samples: int = 0
    fit_class_counts: dict[str, int] = field(default_factory=dict)
    holdout_status: str = "not_evaluated"
    holdout_samples: int = 0
    holdout_class_counts: dict[str, int] = field(default_factory=dict)
    holdout_log_loss: float | None = None
    holdout_null_log_loss: float | None = None
    holdout_calibration_gap: float | None = None
    probability_error_bound: float | None = None
    horizon_exit_mean_return: float | None = None
    horizon_exit_return_lower_bound: float | None = None
    policy_fingerprint: str = ""
    outcome_label_version: str = ""

    def predict_proba(self, features: list[float]) -> dict[str, float]:
        classes = tuple(self.classes or TREND_EVENT_TYPES)
        if (
            not self.fitted
            or len(self.coef) != len(classes)
            or len(self.intercept) != len(classes)
        ):
            return {name: 1.0 / len(classes) for name in classes}
        values = list(features or [])
        values += [0.0] * max(0, len(FEATURE_NAMES) - len(values))
        values = values[: len(FEATURE_NAMES)]
        if any(_finite(value) is None for value in values):
            return {name: 1.0 / len(classes) for name in classes}
        logits: list[float] = []
        for class_index in range(len(classes)):
            row = self.coef[class_index]
            if len(row) != len(FEATURE_NAMES) or any(_finite(value) is None for value in row):
                return {name: 1.0 / len(classes) for name in classes}
            intercept = _finite(self.intercept[class_index])
            if intercept is None:
                return {name: 1.0 / len(classes) for name in classes}
            logits.append(
                float(intercept)
                + sum(float(weight) * float(value) for weight, value in zip(row, values))
            )
        probs = _softmax(logits)
        return {name: float(probability) for name, probability in zip(classes, probs)}


def fit_trend_event_model(
    rows: list[dict[str, Any]],
    *,
    min_samples: int = 80,
    policy_fingerprint: str = "",
    outcome_label_version: str = "directional_trend_label_v2",
    horizon_sec: int = 12 * 3600,
) -> TrendEventModel:
    # (decision_ts, label_available_ts, class_index, features, net_return)
    prepared: list[tuple[int, int, int, list[float], float]] = []
    counts = {event_type: 0 for event_type in TREND_EVENT_TYPES}
    event_index = {event_type: index for index, event_type in enumerate(TREND_EVENT_TYPES)}
    horizon_int = _strict_positive_int(horizon_sec)
    if horizon_int is None:
        horizon_int = 12 * 3600

    for row in rows or []:
        if str(row.get("bot_type") or "") != "directional_trend":
            continue
        direction = str(row.get("direction") or "").strip().lower()
        if direction and direction not in {"long", "short"}:
            # An explicitly neutral/unknown signal is an evaluation rejection,
            # not a single-position strategy. Missing direction remains accepted
            # only for bounded legacy/test rows; DB-backed rows always persist it.
            continue
        if str(row.get("candidate_kind") or "strategy_recommendation").strip().lower() == "trend_evaluation_rejected":
            continue
        event_type = str(row.get("event_type") or "").strip().upper()
        if event_type not in event_index:
            continue
        ts_int = _strict_positive_int(row.get("ts"))
        available_ts = _strict_positive_int(row.get("label_available_ts"))
        # A legacy/malformed label without exact availability may not be treated
        # as known before a validation decision. The expected +60s boundary is
        # allowed to be later, never earlier than the nominal horizon.
        if (
            ts_int is None
            or available_ts is None
            or available_ts < ts_int + horizon_int
        ):
            continue
        features = extract_features(row)
        ret = _finite(row.get("ret"))
        if features is None or ret is None:
            continue
        label = event_index[event_type]
        prepared.append((ts_int, available_ts, label, features, float(ret)))
        counts[event_type] += 1

    prepared.sort(key=lambda item: (item[0], item[1], item[2]))
    model = TrendEventModel(
        fitted=False,
        saved_ts=int(time.time()),
        n_samples=len(prepared),
        class_counts=counts,
        holdout_status="insufficient",
        policy_fingerprint=str(policy_fingerprint or ""),
        outcome_label_version=str(outcome_label_version or ""),
    )
    minimum = max(30, int(min_samples))
    minimum_per_class = max(5, minimum // 20)
    if len(prepared) < minimum or min(counts.values()) < minimum_per_class:
        return model

    # Build the terminal holdout at a whole decision-timestamp boundary so one
    # cross-sectional market decision cannot be split between train and test.
    target_holdout = max(18, int(math.ceil(len(prepared) * 0.20)))
    grouped_counts: dict[int, int] = {}
    for ts_int, _, _, _, _ in prepared:
        grouped_counts[ts_int] = grouped_counts.get(ts_int, 0) + 1
    holdout_count = 0
    holdout_timestamps = 0
    holdout_start: int | None = None
    for ts_int in sorted(grouped_counts, reverse=True):
        holdout_count += grouped_counts[ts_int]
        holdout_timestamps += 1
        holdout_start = ts_int
        if holdout_count >= target_holdout and holdout_timestamps >= 5:
            break
    if holdout_start is None:
        return model
    holdout = [item for item in prepared if item[0] >= holdout_start]
    pre_holdout = [item for item in prepared if item[0] < holdout_start]
    # Purge by actual label availability, not by an assumed horizon alone.
    train = [item for item in pre_holdout if item[1] < holdout_start]
    if len(holdout) < 18 or len({item[0] for item in holdout}) < 5:
        return model
    if len(train) < max(24, minimum // 2):
        return model

    train_counts = [sum(1 for _, _, label, _, _ in train if label == index) for index in range(len(TREND_EVENT_TYPES))]
    holdout_counts = [sum(1 for _, _, label, _, _ in holdout if label == index) for index in range(len(TREND_EVENT_TYPES))]
    model.fit_samples = len(train)
    model.fit_class_counts = {name: train_counts[idx] for idx, name in enumerate(TREND_EVENT_TYPES)}
    model.holdout_samples = len(holdout)
    model.holdout_class_counts = {name: holdout_counts[idx] for idx, name in enumerate(TREND_EVENT_TYPES)}
    if min(train_counts) < 3 or min(holdout_counts) < 2:
        return model

    train_x = [item[3] for item in train]
    train_y = [item[2] for item in train]
    validation_coef, validation_intercept = _fit_softmax(
        train_x,
        train_y,
        class_count=len(TREND_EVENT_TYPES),
    )
    if not validation_coef or not validation_intercept:
        return model
    validation_probs = []
    for _, _, _, features, _ in holdout:
        logits = [
            validation_intercept[class_index]
            + sum(validation_coef[class_index][j] * features[j] for j in range(len(FEATURE_NAMES)))
            for class_index in range(len(TREND_EVENT_TYPES))
        ]
        validation_probs.append(_softmax(logits))
    validation_labels = [item[2] for item in holdout]
    feature_loss = _multiclass_log_loss(validation_labels, validation_probs)
    frequencies = [max(1e-9, count / len(train)) for count in train_counts]
    frequency_total = sum(frequencies)
    null_probs = [[value / frequency_total for value in frequencies] for _ in holdout]
    null_loss = _multiclass_log_loss(validation_labels, null_probs)
    calibration_gap, uncertainty_bound = _probability_uncertainty_bound(
        validation_labels,
        validation_probs,
        class_count=len(TREND_EVENT_TYPES),
    )
    model.holdout_log_loss = feature_loss
    model.holdout_null_log_loss = null_loss
    model.holdout_calibration_gap = calibration_gap
    model.probability_error_bound = uncertainty_bound
    if (
        feature_loss is None
        or null_loss is None
        or uncertainty_bound is None
        or not math.isfinite(feature_loss)
        or not math.isfinite(null_loss)
        or feature_loss >= null_loss - 0.005
    ):
        model.holdout_status = "rejected"
        return model

    # The terminal holdout remains untouched. Refit only on the same purged
    # pre-holdout training set (a deterministic second fit is retained so tests
    # and persisted artifacts prove the deployment fit did not absorb holdout).
    deploy_coef, deploy_intercept = _fit_softmax(
        train_x,
        train_y,
        class_count=len(TREND_EVENT_TYPES),
    )
    if not deploy_coef or not deploy_intercept:
        return model
    train_horizon_returns = [item[4] for item in train if item[2] == event_index["HORIZON_EXIT"]]
    horizon_mean, horizon_lower = _mean_lower_bound(train_horizon_returns)
    model.horizon_exit_mean_return = horizon_mean
    model.horizon_exit_return_lower_bound = horizon_lower
    model.coef = deploy_coef
    model.intercept = deploy_intercept
    model.fitted = True
    model.holdout_status = "accepted"
    return model

def trend_event_storage_key(policy_fingerprint: str) -> str:
    fingerprint = str(policy_fingerprint or "").strip().lower()
    if not is_sha256_fingerprint(fingerprint):
        raise ValueError("policy_fingerprint must be a sha256 hex digest")
    return f"{TREND_EVENT_MODEL_KEY}:{fingerprint}"


def save_trend_event_model(conn, key: str, model: TrendEventModel) -> None:
    payload = {
        "type": TREND_EVENT_MODEL_VERSION,
        "classes": list(model.classes),
        "coef": model.coef,
        "intercept": model.intercept,
        "fitted": bool(model.fitted),
        "ts": int(model.saved_ts or time.time()),
        "n_samples": int(model.n_samples),
        "class_counts": dict(model.class_counts),
        "fit_samples": int(model.fit_samples),
        "fit_class_counts": dict(model.fit_class_counts),
        "holdout_status": str(model.holdout_status),
        "holdout_samples": int(model.holdout_samples),
        "holdout_class_counts": dict(model.holdout_class_counts),
        "holdout_log_loss": model.holdout_log_loss,
        "holdout_null_log_loss": model.holdout_null_log_loss,
        "holdout_calibration_gap": model.holdout_calibration_gap,
        "probability_error_bound": model.probability_error_bound,
        "horizon_exit_mean_return": model.horizon_exit_mean_return,
        "horizon_exit_return_lower_bound": model.horizon_exit_return_lower_bound,
        "policy_fingerprint": str(model.policy_fingerprint),
        "outcome_label_version": str(model.outcome_label_version),
    }
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
    conn.execute(
        "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES (?, ?, ?)",
        (key, raw, int(time.time())),
    )
    conn.commit()


def load_trend_event_model(conn, key: str) -> TrendEventModel | None:
    row = conn.execute("SELECT value_json FROM app_config WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["value_json"])
        if not isinstance(payload, dict) or payload.get("type") != TREND_EVENT_MODEL_VERSION:
            return None
        classes = tuple(str(value) for value in payload.get("classes") or ())
        if classes != TREND_EVENT_TYPES:
            return None
        fitted = payload.get("fitted")
        if not isinstance(fitted, bool):
            return None
        coef = payload.get("coef") or []
        intercept = payload.get("intercept") or []
        if fitted and (
            len(coef) != len(TREND_EVENT_TYPES)
            or len(intercept) != len(TREND_EVENT_TYPES)
            or any(len(row_values) != len(FEATURE_NAMES) for row_values in coef)
        ):
            return None
        model = TrendEventModel(
            classes=classes,
            coef=[[float(value) for value in row_values] for row_values in coef],
            intercept=[float(value) for value in intercept],
            fitted=fitted,
            saved_ts=int(payload.get("ts") or 0),
            n_samples=int(payload.get("n_samples") or 0),
            class_counts={str(k): int(v) for k, v in (payload.get("class_counts") or {}).items()},
            fit_samples=int(payload.get("fit_samples") or 0),
            fit_class_counts={str(k): int(v) for k, v in (payload.get("fit_class_counts") or {}).items()},
            holdout_status=str(payload.get("holdout_status") or "not_evaluated"),
            holdout_samples=int(payload.get("holdout_samples") or 0),
            holdout_class_counts={str(k): int(v) for k, v in (payload.get("holdout_class_counts") or {}).items()},
            holdout_log_loss=_finite(payload.get("holdout_log_loss")),
            holdout_null_log_loss=_finite(payload.get("holdout_null_log_loss")),
            holdout_calibration_gap=_finite(payload.get("holdout_calibration_gap")),
            probability_error_bound=_finite(payload.get("probability_error_bound")),
            horizon_exit_mean_return=_finite(payload.get("horizon_exit_mean_return")),
            horizon_exit_return_lower_bound=_finite(payload.get("horizon_exit_return_lower_bound")),
            policy_fingerprint=str(payload.get("policy_fingerprint") or ""),
            outcome_label_version=str(payload.get("outcome_label_version") or ""),
        )
        if model.fitted and (
            model.holdout_status != "accepted"
            or model.probability_error_bound is None
            or not is_sha256_fingerprint(model.policy_fingerprint)
        ):
            return None
        return model
    except Exception:
        return None


def _signed_return(entry: float, exit_price: float, direction: str) -> float:
    raw = (float(exit_price) - float(entry)) / float(entry)
    return raw if direction == "long" else -raw


def build_trend_event_assessment(
    rec: dict[str, Any],
    features: list[float] | None,
    model: TrendEventModel | None,
) -> dict[str, Any]:
    params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
    plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    levels = plan.get("levels") if isinstance(plan.get("levels"), dict) else {}
    tp = levels.get("take_profit") if isinstance(levels.get("take_profit"), dict) else {}
    sl = levels.get("stop_loss") if isinstance(levels.get("stop_loss"), dict) else {}
    entry = _finite(plan.get("reference_price") or params.get("price_ref"))
    tp_price = _finite(tp.get("price"))
    sl_price = _finite(sl.get("price"))
    direction = normalize_execution_direction(rec.get("direction"))
    base = {
        "ready": False,
        "source": "trend_event_softmax",
        "model_version": TREND_EVENT_MODEL_VERSION,
        "outcome_label_version": str(getattr(model, "outcome_label_version", "") or ""),
        "policy_fingerprint": str(getattr(model, "policy_fingerprint", "") or ""),
        "return_basis": TREND_EVENT_RETURN_BASIS,
        "reason_codes": [],
    }
    reasons = base["reason_codes"]
    if model is None or not model.fitted or model.holdout_status != "accepted":
        reasons.append("TREND_EVENT_MODEL_NOT_READY")
    if features is None or len(features) != len(FEATURE_NAMES):
        reasons.append("TREND_EVENT_FEATURES_INVALID")
    if direction not in {"long", "short"}:
        reasons.append("TREND_EVENT_DIRECTION_INVALID")
    if entry is None or tp_price is None or sl_price is None or entry <= 0.0:
        reasons.append("TREND_EVENT_GEOMETRY_MISSING")
    elif direction == "long" and not (sl_price < entry < tp_price):
        reasons.append("TREND_EVENT_GEOMETRY_INVALID")
    elif direction == "short" and not (tp_price < entry < sl_price):
        reasons.append("TREND_EVENT_GEOMETRY_INVALID")
    if reasons:
        return base

    probs = model.predict_proba(features or [])
    if set(probs) != set(TREND_EVENT_TYPES) or abs(sum(probs.values()) - 1.0) > 1e-8:
        reasons.append("TREND_EVENT_PROBABILITIES_INVALID")
        return base
    margin = _finite(model.probability_error_bound)
    timeout_mean = _finite(model.horizon_exit_mean_return)
    timeout_lower = _finite(model.horizon_exit_return_lower_bound)
    if margin is None or not (0.0 <= margin < 0.5):
        reasons.append("TREND_EVENT_UNCERTAINTY_UNAVAILABLE")
    if timeout_mean is None or timeout_lower is None:
        reasons.append("TREND_TIMEOUT_RETURN_UNAVAILABLE")
    if reasons:
        return base

    cost_model = params.get("cost_model") if isinstance(params.get("cost_model"), dict) else {}
    cost_bps = max(0.0, _finite(cost_model.get("execution_cost_bps")) or 0.0)
    funding_bps = max(0.0, _finite(cost_model.get("expected_funding_bps")) or 0.0)
    friction = (cost_bps + funding_bps) / 10_000.0
    tp_return = _signed_return(float(entry), float(tp_price), direction) - friction
    sl_return = _signed_return(float(entry), float(sl_price), direction) - friction
    p_tp = float(probs["TP_FIRST"])
    p_sl = float(probs["SL_FIRST"])
    p_horizon = float(probs["HORIZON_EXIT"])
    p_tp_lower = max(0.0, p_tp - float(margin))
    p_sl_upper = min(1.0, p_sl + float(margin))
    released_mass = max(0.0, p_tp - p_tp_lower)
    p_sl_conservative = p_sl
    p_horizon_conservative = p_horizon
    # Move all one-sided uncertainty removed from the favourable TP branch to
    # whichever adverse branch has the worse net payoff. This is strictly more
    # conservative than always assigning it to SL when timeout loss is larger.
    if float(timeout_lower) < sl_return:
        p_horizon_conservative += released_mass
    else:
        p_sl_conservative += released_mass
    expected_return = p_tp * tp_return + p_sl * sl_return + p_horizon * float(timeout_mean)
    lower_return = (
        p_tp_lower * tp_return
        + p_sl_conservative * sl_return
        + p_horizon_conservative * float(timeout_lower)
    )
    if tp_return <= 0.0 or sl_return >= 0.0:
        reasons.append("TREND_EVENT_PAYOFF_GEOMETRY_INVALID")
    if p_tp_lower <= p_sl_upper:
        reasons.append("TP_FIRST_NOT_MORE_LIKELY_THAN_SL_FIRST")
    if expected_return <= 0.0 or lower_return <= 0.0:
        reasons.append("TREND_FIRST_TOUCH_EXPECTANCY_NON_POSITIVE")

    base.update({
        "ready": not reasons,
        "holdout_status": model.holdout_status,
        "holdout_samples": int(model.holdout_samples),
        "holdout_log_loss": model.holdout_log_loss,
        "holdout_null_log_loss": model.holdout_null_log_loss,
        "probability_error_bound": float(margin),
        "tp_first_probability": p_tp,
        "sl_first_probability": p_sl,
        "horizon_exit_probability": p_horizon,
        "tp_first_probability_lower_bound": p_tp_lower,
        "sl_first_probability_upper_bound": p_sl_upper,
        "sl_first_probability_conservative": p_sl_conservative,
        "horizon_exit_probability_conservative": p_horizon_conservative,
        "tp_net_return": float(tp_return),
        "sl_net_return": float(sl_return),
        "horizon_exit_expected_net_return": float(timeout_mean),
        "horizon_exit_net_return_lower_bound": float(timeout_lower),
        "event_expected_net_return": float(expected_return),
        "event_expected_net_return_lower_bound": float(lower_return),
    })
    return base
