from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


LABEL_DUE_GRACE_SEC = 120
VALID_DIRECTIONS = frozenset({"long", "short", "neutral"})


@dataclass(frozen=True)
class Observation:
    rec_id: str
    ts: int
    label_available_ts: int
    label_time_source: str
    horizon_sec: int
    direction: str
    score: float | None
    mean_reversion_score: float | None
    ret: float
    success: int


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def exact_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def nested_value(mapping: Any, *keys: str) -> Any:
    value = mapping
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def normalize_observations(payload: Any) -> tuple[list[Observation], dict[str, int]]:
    raw_rows = payload.get("recent") if isinstance(payload, dict) else payload
    if not isinstance(raw_rows, list):
        raise ValueError("input must be an outcomes API object with recent[] or a JSON list")

    observations: list[Observation] = []
    rejected: dict[str, int] = {}

    def reject(code: str) -> None:
        rejected[code] = rejected.get(code, 0) + 1

    for raw in raw_rows:
        if not isinstance(raw, dict):
            reject("not_an_object")
            continue
        ts = exact_integer(raw.get("ts"))
        horizon_sec = exact_integer(raw.get("horizon_sec"))
        ret = finite_number(raw.get("ret"))
        success = exact_integer(raw.get("success"))
        if ts is None or ts <= 0:
            reject("invalid_ts")
            continue
        if horizon_sec is None or horizon_sec <= 0:
            reject("invalid_horizon")
            continue
        if ret is None:
            reject("invalid_return")
            continue
        if success not in (0, 1):
            reject("invalid_success")
            continue

        label_available_ts = exact_integer(raw.get("label_available_ts"))
        label_time_source = "persisted"
        if label_available_ts is None:
            label_available_ts = ts + horizon_sec + LABEL_DUE_GRACE_SEC
            label_time_source = "derived_legacy_ts_plus_horizon_plus_grace"
        if label_available_ts < ts:
            reject("label_before_recommendation")
            continue

        direction = str(
            raw.get("execution_direction")
            or raw.get("raw_direction")
            or raw.get("direction")
            or "neutral"
        ).strip().lower()
        if direction not in VALID_DIRECTIONS:
            direction = "neutral"

        mean_reversion_score = finite_number(raw.get("mean_reversion_score"))
        if mean_reversion_score is None:
            mean_reversion_score = finite_number(
                nested_value(raw, "eligibility", "gates", "mean_reversion_score")
            )
        observations.append(
            Observation(
                rec_id=str(raw.get("rec_id") or f"row-{len(observations)}"),
                ts=ts,
                label_available_ts=label_available_ts,
                label_time_source=label_time_source,
                horizon_sec=horizon_sec,
                direction=direction,
                score=finite_number(raw.get("score")),
                mean_reversion_score=mean_reversion_score,
                ret=ret,
                success=int(success),
            )
        )

    observations.sort(key=lambda row: (row.ts, row.rec_id))
    return observations, dict(sorted(rejected.items()))


def metric_summary(rows: Iterable[Observation]) -> dict[str, Any]:
    values = list(rows)
    total = len(values)
    if not values:
        return {
            "total": 0,
            "wins": 0,
            "win_rate": None,
            "positive_return_total": 0,
            "avg_return_pct": None,
            "median_return_pct": None,
            "sum_return_pct": 0.0,
        }
    returns = [row.ret for row in values]
    wins = sum(row.success for row in values)
    return {
        "total": total,
        "wins": wins,
        "win_rate": round(wins / total, 6),
        "positive_return_total": sum(value > 0.0 for value in returns),
        "avg_return_pct": round(statistics.fmean(returns) * 100.0, 6),
        "median_return_pct": round(statistics.median(returns) * 100.0, 6),
        "sum_return_pct": round(sum(returns) * 100.0, 6),
    }


def pearson(rows: Iterable[Observation], attribute: str, target: str) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        feature = getattr(row, attribute)
        target_value = getattr(row, target)
        if feature is not None:
            pairs.append((float(feature), float(target_value)))
    if len(pairs) < 3:
        return {"n": len(pairs), "correlation": None, "status": "insufficient_samples"}
    xs = [pair[0] for pair in pairs]
    ys = [pair[1] for pair in pairs]
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    x_ss = sum((x - x_mean) ** 2 for x in xs)
    y_ss = sum((y - y_mean) ** 2 for y in ys)
    if x_ss <= 0.0 or y_ss <= 0.0:
        return {"n": len(pairs), "correlation": None, "status": "zero_variance"}
    return {
        "n": len(pairs),
        "correlation": round(numerator / math.sqrt(x_ss * y_ss), 6),
        "status": "descriptive_only_clustered_rows",
    }


def training_tertile_edges(values: Iterable[float | None]) -> tuple[float, float] | None:
    clean = sorted(float(value) for value in values if value is not None)
    if len(clean) < 6:
        return None
    lower_index = max(0, min(len(clean) - 1, math.ceil(len(clean) / 3) - 1))
    upper_index = max(0, min(len(clean) - 1, math.ceil(2 * len(clean) / 3) - 1))
    lower = clean[lower_index]
    upper = clean[upper_index]
    if lower >= upper:
        return None
    return lower, upper


def rank_segment(value: float | None, edges: tuple[float, float] | None, prefix: str) -> str | None:
    if value is None or edges is None:
        return None
    if value <= edges[0]:
        return f"{prefix}_training_tertile_low"
    if value <= edges[1]:
        return f"{prefix}_training_tertile_middle"
    return f"{prefix}_training_tertile_high"


def fixed_segments(
    row: Observation,
    *,
    score_floor: float | None,
    mean_reversion_floor: float | None,
) -> list[str]:
    segments = ["all", f"direction_{row.direction}"]
    if row.score is not None and score_floor is not None:
        segments.append(
            "score_at_or_above_policy_floor"
            if row.score >= score_floor
            else "score_below_policy_floor"
        )
    if row.mean_reversion_score is not None and mean_reversion_floor is not None:
        segments.append(
            "mean_reversion_at_or_above_policy_floor"
            if row.mean_reversion_score >= mean_reversion_floor
            else "mean_reversion_below_policy_floor"
        )
    if (
        row.score is not None
        and score_floor is not None
        and row.mean_reversion_score is not None
        and mean_reversion_floor is not None
        and row.score >= score_floor
        and row.mean_reversion_score >= mean_reversion_floor
    ):
        segments.append("combined_existing_policy_floors")
    return segments


def add_segments(
    buckets: dict[str, list[Observation]],
    row: Observation,
    *,
    score_floor: float | None,
    mean_reversion_floor: float | None,
    score_edges: tuple[float, float] | None = None,
    mean_reversion_edges: tuple[float, float] | None = None,
) -> None:
    names = fixed_segments(
        row,
        score_floor=score_floor,
        mean_reversion_floor=mean_reversion_floor,
    )
    score_rank = rank_segment(row.score, score_edges, "score")
    mean_reversion_rank = rank_segment(
        row.mean_reversion_score,
        mean_reversion_edges,
        "mean_reversion",
    )
    if score_rank:
        names.append(score_rank)
    if mean_reversion_rank:
        names.append(mean_reversion_rank)
    for name in names:
        buckets.setdefault(name, []).append(row)


def resolve_contract_floor(
    payload: Any,
    rows: list[Observation],
    *,
    override: float | None,
    gate_name: str,
) -> dict[str, Any]:
    if override is not None:
        return {"value": override, "source": "cli_existing_policy_override", "conflict": False}
    values: list[float] = []
    raw_rows = payload.get("recent") if isinstance(payload, dict) else payload
    if isinstance(raw_rows, list):
        for raw in raw_rows:
            value = finite_number(nested_value(raw, "eligibility", "gates", gate_name))
            if value is not None:
                values.append(value)
    distinct = sorted({round(value, 12) for value in values})
    if len(distinct) == 1:
        return {"value": distinct[0], "source": "outcome_api_policy_contract", "conflict": False}
    return {
        "value": None,
        "source": "unavailable" if not distinct else "conflicting_outcome_contracts",
        "conflict": len(distinct) > 1,
        "observed_values": distinct,
        "eligible_row_count": len(rows),
    }


def independent_validation_timestamps(
    timestamps: Iterable[int],
    *,
    embargo_sec: int,
) -> list[int]:
    selected: list[int] = []
    for ts in sorted(set(timestamps)):
        if not selected or ts - selected[-1] >= embargo_sec:
            selected.append(ts)
    return selected


def build_walk_forward_report(
    payload: Any,
    *,
    score_floor_override: float | None = None,
    mean_reversion_floor_override: float | None = None,
    min_training_cohorts: int = 2,
) -> dict[str, Any]:
    observations, rejected = normalize_observations(payload)
    score_floor_info = resolve_contract_floor(
        payload,
        observations,
        override=score_floor_override,
        gate_name="score_floor",
    )
    mean_reversion_floor_info = resolve_contract_floor(
        payload,
        observations,
        override=mean_reversion_floor_override,
        gate_name="mean_reversion_floor",
    )
    score_floor = finite_number(score_floor_info.get("value"))
    mean_reversion_floor = finite_number(mean_reversion_floor_info.get("value"))

    descriptive: dict[str, list[Observation]] = {}
    for row in observations:
        add_segments(
            descriptive,
            row,
            score_floor=score_floor,
            mean_reversion_floor=mean_reversion_floor,
        )

    rows_by_ts: dict[int, list[Observation]] = {}
    for row in observations:
        rows_by_ts.setdefault(row.ts, []).append(row)

    aggregate_validation: dict[str, list[Observation]] = {}
    fold_results: list[dict[str, Any]] = []
    validation_timestamps: list[int] = []
    for validation_ts in sorted(rows_by_ts):
        training = [
            row for row in observations
            if row.label_available_ts <= validation_ts
        ]
        training_cohorts = sorted({row.ts for row in training})
        if len(training_cohorts) < max(1, int(min_training_cohorts)):
            continue
        validation = rows_by_ts[validation_ts]
        score_edges = training_tertile_edges(row.score for row in training)
        mean_reversion_edges = training_tertile_edges(
            row.mean_reversion_score for row in training
        )
        validation_timestamps.append(validation_ts)
        fold_segments: dict[str, list[Observation]] = {}
        for row in validation:
            add_segments(
                aggregate_validation,
                row,
                score_floor=score_floor,
                mean_reversion_floor=mean_reversion_floor,
                score_edges=score_edges,
                mean_reversion_edges=mean_reversion_edges,
            )
            add_segments(
                fold_segments,
                row,
                score_floor=score_floor,
                mean_reversion_floor=mean_reversion_floor,
                score_edges=score_edges,
                mean_reversion_edges=mean_reversion_edges,
            )
        fold_results.append({
            "validation_ts": validation_ts,
            "training_rows": len(training),
            "training_decision_cohorts": len(training_cohorts),
            "latest_training_label_available_ts": max(
                row.label_available_ts for row in training
            ),
            "validation_rows": len(validation),
            "score_training_tertile_edges": list(score_edges) if score_edges else None,
            "mean_reversion_training_tertile_edges": (
                list(mean_reversion_edges) if mean_reversion_edges else None
            ),
            "validation": {
                name: metric_summary(bucket)
                for name, bucket in sorted(fold_segments.items())
            },
        })

    max_horizon = max((row.horizon_sec for row in observations), default=0)
    independent_ts = independent_validation_timestamps(
        validation_timestamps,
        embargo_sec=max_horizon,
    ) if max_horizon > 0 else []
    independent_rows = [
        row for row in observations if row.ts in set(independent_ts)
    ]

    total = len(observations)
    score_n = sum(row.score is not None for row in observations)
    mean_reversion_n = sum(
        row.mean_reversion_score is not None for row in observations
    )
    persisted_label_n = sum(
        row.label_time_source == "persisted" for row in observations
    )
    temporal_span_sec = (
        observations[-1].ts - observations[0].ts if len(observations) >= 2 else 0
    )

    if not observations:
        status = "invalid_or_empty_input"
    elif not fold_results:
        status = "insufficient_temporal_span_for_walk_forward"
    elif mean_reversion_n < total:
        status = "partial_missing_mean_reversion"
    elif len(independent_ts) < 5:
        status = "exploratory_insufficient_independent_validation_cohorts"
    else:
        status = "completed_exploratory"

    limitations: list[str] = []
    if persisted_label_n < total:
        limitations.append(
            "Legacy rows lack label_available_ts; availability was conservatively derived as ts+horizon+120s."
        )
    if mean_reversion_n < total:
        limitations.append(
            "mean_reversion_score is absent from part or all of the input; no value was imputed."
        )
    if len(independent_ts) < 5:
        limitations.append(
            "Fewer than five horizon-separated validation cohorts; results are diagnostic, not threshold evidence."
        )
    limitations.append(
        "Rows inside a timestamp are cross-sectional and must not be treated as independent trials."
    )
    limitations.append(
        "The report evaluates existing floors and training-derived rank bands; it does not select or change production thresholds."
    )

    return {
        "schema_version": "offline-walk-forward-v1",
        "method": {
            "ordering": "recommendation_ts_then_rec_id",
            "purge_rule": "training.label_available_ts <= validation.ts",
            "validation_unit": "whole_decision_timestamp",
            "independence_embargo_sec": max_horizon,
            "minimum_training_decision_cohorts": max(1, int(min_training_cohorts)),
            "production_threshold_optimization": False,
        },
        "status": status,
        "input": {
            "accepted_rows": total,
            "rejected_rows": sum(rejected.values()),
            "rejected_by_reason": rejected,
            "decision_timestamps": len(rows_by_ts),
            "temporal_span_sec": temporal_span_sec,
            "temporal_span_days": round(temporal_span_sec / 86400.0, 6),
            "label_time_persisted_rows": persisted_label_n,
            "label_time_derived_legacy_rows": total - persisted_label_n,
        },
        "coverage": {
            "score": {"n": score_n, "share": round(score_n / total, 6) if total else 0.0},
            "mean_reversion_score": {
                "n": mean_reversion_n,
                "share": round(mean_reversion_n / total, 6) if total else 0.0,
                "imputed": False,
            },
            "direction": {"n": total, "share": 1.0 if total else 0.0},
        },
        "existing_policy_floors": {
            "score": score_floor_info,
            "mean_reversion": mean_reversion_floor_info,
            "changed_by_analysis": False,
        },
        "full_sample_descriptive": {
            name: metric_summary(bucket)
            for name, bucket in sorted(descriptive.items())
        },
        "feature_relationships_descriptive": {
            "score_vs_return": pearson(observations, "score", "ret"),
            "score_vs_strategy_success": pearson(observations, "score", "success"),
            "mean_reversion_vs_return": pearson(
                observations, "mean_reversion_score", "ret"
            ),
            "mean_reversion_vs_strategy_success": pearson(
                observations, "mean_reversion_score", "success"
            ),
        },
        "walk_forward": {
            "fold_count": len(fold_results),
            "validation_rows": sum(
                len(rows_by_ts[ts]) for ts in validation_timestamps
            ),
            "independent_validation_timestamps": independent_ts,
            "independent_validation_cohort_count": len(independent_ts),
            "independent_validation": metric_summary(independent_rows),
            "aggregate_validation": {
                name: metric_summary(bucket)
                for name, bucket in sorted(aggregate_validation.items())
            },
            "folds": fold_results,
        },
        "limitations": limitations,
        "decision": {
            "change_universe": False,
            "change_trading_thresholds": False,
            "reason": "insufficient independent evidence; preserve the pre-registered production contract",
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Purged offline walk-forward for outcome score, mean reversion and direction"
        )
    )
    parser.add_argument("input_json", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--score-floor", type=float)
    parser.add_argument("--mean-reversion-floor", type=float)
    parser.add_argument("--min-training-cohorts", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with args.input_json.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    report = build_walk_forward_report(
        payload,
        score_floor_override=args.score_floor,
        mean_reversion_floor_override=args.mean_reversion_floor,
        min_training_cohorts=args.min_training_cohorts,
    )
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
