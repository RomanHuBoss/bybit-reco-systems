from __future__ import annotations

import math
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import db
from .features import compute_features_from_ohlcv, liquidity_tier, funding_signal, oi_trend, btc_beta
from .regime import classify_regime
from .risk import gate_candidate, compute_risk_status as _compute_risk_status, normalize_risk_limits
from .direction import vote_for_tf, aggregate_direction
from .sentiment_features import compute_sentiment_agg, compute_symbol_sentiment_map
from .shock_guard import compute_market_shock, apply_market_shock_gate, compute_symbol_fast_veto, APP_CONFIG_KEY as MARKET_SHOCK_APP_KEY
from .outcomes import BOT_HORIZONS, HORIZON_SEC_DEFAULT, _resolve_effective_horizon
from .bot_types import SUPPORTED_BOT_TYPES
from .strategy_router import (
    COMPARISON_RETURN_BASIS,
    ROUTER_VERSION as STRATEGY_ROUTER_VERSION,
    select_strategy,
)
from .llm_review import OllamaCandleReviewer, build_review_payload, normalize_direction, PROMPT_VERSION
from .grid_math import (
    arithmetic_grid_commitment,
    arithmetic_grid_cross_margin_stress,
    grid_leg_economics,
    margin_required_usdt,
    quantize_step,
    resolve_integer_aliases,
    strict_integer,
)
from .collector import RuntimeLockLostError
from .settings import load_settings
from .policy import (
    CALIBRATION_LABEL_GRACE_SEC,
    canonical_policy_fingerprint,
    is_sha256_fingerprint,
    policy_label_due_ts,
)
from .calibration import (
    fit_platt, PlattScaler, save_platt_to_db, load_platt_from_db, BOT_CALIB_KEYS,
    LogRegScaler, fit_logreg, save_logreg_to_db, load_logreg_from_db,
    extract_features, FEATURE_NAMES, GLOBAL_LOGREG_KEY, CALIB_REFIT_INTERVAL_SEC,
    return_confidence_interval, selected_policy_confidence,
)
from .trend_events import (
    TREND_EVENT_MODEL_VERSION,
    TrendEventModel,
    build_trend_event_assessment,
    fit_trend_event_model,
    load_trend_event_model,
    save_trend_event_model,
    trend_event_storage_key,
)
# Note: calibrators use db.get_outcomes_with_recs (single JOIN query) to avoid N+1 pattern

BOT_TYPES_BYBIT = list(SUPPORTED_BOT_TYPES)
MAX_FUNDING_STALENESS_SEC = 60 * 60
MAX_OI_STALENESS_SEC = 3 * 60 * 60
UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS: frozenset[str] = frozenset()
RECOMMENDER_MODEL_VERSION = "bybit-taxonomy-v13-log-symmetric-direction"
DIRECTION_CALIBRATION_KEY = "platt_direction_v15"
CALIBRATION_POLICY_SCHEMA_VERSION = "candidate-policy-v3"
POLICY_OUTCOME_LABEL_VERSION = "grid_label_v26"
TREND_STRATEGY_CONTRACT_VERSION = "directional_trend_v2"
TREND_OUTCOME_LABEL_VERSION = "directional_trend_label_v2"
TREND_RECOMMENDER_MODEL_VERSION = RECOMMENDER_MODEL_VERSION + "+directional-trend-v6"
TREND_EVALUATION_REJECTED_KIND = "trend_evaluation_rejected"
TREND_STRATEGY_RECOMMENDATION_KIND = "strategy_recommendation"
CALIBRATION_EVIDENCE_REASON_CODES: frozenset[str] = frozenset({
    "PROXY_MONETARY_EXPECTANCY_UNPROVEN",
    "PROXY_MONETARY_EXPECTANCY_NON_POSITIVE",
    "PROXY_OUTCOME_CENSORING_UNBOUNDED",
    "CALIBRATED_CONFIDENCE_UNAVAILABLE",
    "STRATEGY_ROUTER_NO_CLEAR_WINNER",
    "TREND_FIRST_TOUCH_MODEL_UNAVAILABLE",
    "TREND_FIRST_TOUCH_EXPECTANCY_NON_POSITIVE",
    "TREND_FIRST_TOUCH_ORDER_UNCERTAIN",
})
LLM_REVIEW_CACHE_APP_KEY = "llm_review_cache_v1"
LLM_REVIEW_ASYNC_STATUS_APP_KEY = "llm_review_async_status_v1"
LLM_REVIEWER_DEFAULT_CANDLES_PER_TF = 32
LLM_REVIEWER_DEFAULT_MAX_CANDIDATES = 24
LLM_REVIEWER_DEFAULT_MAX_WORKERS = 2
LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE = 0.65
LLM_REVIEWER_DEFAULT_CADENCE_SEC = 300
LLM_REVIEWER_DEFAULT_TTL_SEC = 900
LLM_REVIEWER_DEFAULT_PENDING_TIMEOUT_SEC = 900
settings = load_settings()


def _fmt_tf(tf_sec: int) -> str:
    if tf_sec % 86400 == 0:
        d = tf_sec // 86400
        return f"{d}d"
    if tf_sec % 3600 == 0:
        h = tf_sec // 3600
        return f"{h}h"
    if tf_sec % 60 == 0:
        m = tf_sec // 60
        return f"{m}m"
    return f"{tf_sec}s"

def _round_price(x: float | None, decimals: int = 6) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        num = float(x)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    try:
        return float(round(num, decimals))
    except Exception:
        return None

def _pct_dist(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    try:
        if b == 0:
            return None
        return float((a - b) / b * 100.0)
    except Exception:
        return None


def _finite_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        num = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(num):
        return float(default)
    return num


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _safe_int_or_none(value: Any) -> int | None:
    return strict_integer(value)


def _first_tradeable_1m_candle_ts(conn, venue: str, symbol: str, ts_after: int) -> int | None:
    """Возвращает ts первой доступной 1m-candle строго после signal ref ts.

    Publication-chain lock должен жить до фактического pseudo-entry, а не до
    приближённого `features_ref_ts + 60`. При длинной дырке в 1m-данных слишком
    ранний unlock создаёт ложный второй outcome-root поверх ещё незавершённой
    идеи. Если candle пока нет, вызывающий код деградирует к консервативной
    аппроксимации, но при наличии данных используем реальный tradeable ts.
    """
    cur = conn.execute(
        """SELECT ts FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts>?
           ORDER BY ts ASC LIMIT 1""",
        (venue, symbol, int(ts_after)),
    )
    row = cur.fetchone()
    if not row:
        return None
    return _safe_int_or_none(row["ts"])


def _sanitize_json_numbers(value: Any) -> Any:
    """Рекурсивно убирает non-finite числа из operator/audit payload'ов.

    Это нужно не только ради красивого UI. В проекте JSON сериализуется в strict-mode,
    поэтому один `NaN`/`Infinity` внутри trade-plan или cost-model превращает целую
    рекомендацию в несериализуемую и может сорвать публикацию snapshot'а.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _sanitize_json_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_numbers(v) for v in value]
    try:
        num = float(value)
    except Exception:
        return value
    return num if math.isfinite(num) else None


def _llm_reviewer_context_signature(settings) -> str:
    tf_secs = []
    for tf in (getattr(settings, "llm_reviewer_tf_secs", []) or []):
        try:
            tf_i = int(tf)
        except Exception:
            continue
        if tf_i > 0:
            tf_secs.append(tf_i)
    tf_secs = sorted(set(tf_secs))
    candles_per_tf = max(1, int(getattr(settings, "llm_reviewer_candles_per_tf", LLM_REVIEWER_DEFAULT_CANDLES_PER_TF) or LLM_REVIEWER_DEFAULT_CANDLES_PER_TF))
    return f"tf={','.join(str(tf) for tf in tf_secs)}|candles={candles_per_tf}"



def _serialize_llm_candles(rows_oldest_first: list[dict[str, Any]] | list[Any], limit: int) -> list[list[float | int]]:
    out: list[list[float | int]] = []
    take = max(1, int(limit or 1))
    for row in list(rows_oldest_first)[-take:]:
        try:
            out.append([
                int(row["ts"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row.get("volume") or 0.0),
            ])
        except Exception:
            continue
    return out


def _make_llm_reviewer(settings) -> OllamaCandleReviewer | None:
    if not bool(getattr(settings, "llm_reviewer_enabled", False)):
        return None
    provider = str(getattr(settings, "llm_reviewer_provider", "ollama") or "ollama").strip().lower()
    if provider != "ollama":
        raise ValueError(f"unsupported llm reviewer provider: {provider}")
    model = str(getattr(settings, "llm_reviewer_model", "") or "").strip()
    if not model:
        return None
    return OllamaCandleReviewer(
        base_url=str(getattr(settings, "llm_reviewer_url", "http://127.0.0.1:11434") or "http://127.0.0.1:11434"),
        model=model,
        timeout_sec=int(getattr(settings, "llm_reviewer_timeout_sec", 60) or 60),
        keep_alive=str(getattr(settings, "llm_reviewer_keep_alive", "90s") or "90s"),
    )


def _risk_report_decision_for_status(status: Any) -> str:
    status_norm = str(status or "").strip().lower()
    return "recommended" if status_norm in {"recommended", "active"} else "not_recommended"


def _llm_review_is_completed_ok(rec: dict[str, Any]) -> bool:
    reasons = rec.get("reasons") if isinstance(rec.get("reasons"), dict) else {}
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else {}
    return str(llm_review.get("status") or "").strip().lower() == "ok"


def _hold_recommendation_until_llm_verdict(rec: dict[str, Any], settings, reviewer: OllamaCandleReviewer | None = None, *, reason: str = "queued_async") -> bool:
    """Move an actionable recommendation to pending while LLM reviewer has no OK verdict.

    Operator-facing recommendations must never be launchable as recommended/active
    when LLM review is enabled but still absent, pending, errored or timed out.
    This keeps the database, API and UI fail-closed for the LLM gate contract.
    """
    if not bool(getattr(settings, "llm_reviewer_enabled", False)):
        return False
    current_status = str(rec.get("status") or "").strip().lower()
    if current_status not in LLM_REVIEW_ELIGIBLE_STATUSES:
        return False
    if _llm_review_is_completed_ok(rec):
        return False
    mode = str(getattr(settings, "llm_reviewer_mode", "advisory") or "advisory").strip().lower()
    if mode not in {"advisory", "gate"}:
        mode = "advisory"
    reasons = rec.setdefault("reasons", {})
    if not isinstance(reasons, dict):
        reasons = {}
        rec["reasons"] = reasons
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else None
    if not isinstance(llm_review, dict) or str(llm_review.get("status") or "").strip().lower() not in {"pending", "error"}:
        llm_review = _make_pending_llm_review(rec, mode, reviewer, reason=reason)
    else:
        llm_review = dict(llm_review)
        llm_review.setdefault("mode", mode)
        llm_review.setdefault("gate_decision", "pending")
        llm_review.setdefault("queued_ts", int(time.time()))
        llm_review.setdefault("reason", reason)
        if current_status in LLM_REVIEW_ELIGIBLE_STATUSES:
            llm_review["publish_target_status"] = current_status
    llm_review["hold_policy"] = "llm_verdict_required"
    llm_review["requires_ok_verdict"] = True
    reasons["llm_review"] = llm_review
    rec["status"] = "pending"
    return True


def _sync_recommendation_metadata(rec: dict[str, Any]) -> None:
    reasons = rec.setdefault("reasons", {})
    decision_layers = reasons.get("decision_layers")
    if not isinstance(decision_layers, dict):
        decision_layers = {}
        reasons["decision_layers"] = decision_layers
    decision_layers["final_status"] = rec.get("status")
    publication_gate = reasons.get("publication_gate")
    if isinstance(publication_gate, dict):
        decision_layers["publication_gate"] = {
            "required_hits": publication_gate.get("required_hits"),
            "observed_hits": publication_gate.get("observed_hits"),
            "mode": publication_gate.get("mode"),
            "bypassed": publication_gate.get("bypassed"),
            "passed": publication_gate.get("passed"),
        }
    llm_review = reasons.get("llm_review")
    if isinstance(llm_review, dict):
        decision_layers["llm_reviewer"] = {
            "status": llm_review.get("status"),
            "mode": llm_review.get("mode"),
            "gate_decision": llm_review.get("gate_decision"),
            "agree_with_engine": llm_review.get("agree_with_engine"),
            "confidence": llm_review.get("confidence"),
            "execution_direction": llm_review.get("execution_direction"),
            "cached": llm_review.get("cached"),
            "cache_age_sec": llm_review.get("cache_age_sec"),
        }

    simulation_scope = reasons.get("simulation_scope")
    if not isinstance(simulation_scope, dict):
        simulation_scope = {}
    simulation_scope.update({
        "mode": "historical_proxy_only",
        "runtime_order_submission": False,
        "runtime_execution_validation": "not_performed",
        "exchange_fill_attestation": "not_available",
        "fill_model": "conservative_ohlcv_proxy",
    })
    reasons["simulation_scope"] = simulation_scope

    params = rec.get("params")
    if (
        str(rec.get("bot_type") or "").strip().lower() == "directional_trend"
        and str(rec.get("direction") or "").strip().lower() not in {"long", "short"}
    ):
        candidate_kind = TREND_EVALUATION_REJECTED_KIND
    else:
        candidate_kind = str(
            rec.get("candidate_kind")
            or (params.get("candidate_kind") if isinstance(params, dict) else "")
            or reasons.get("candidate_kind")
            or TREND_STRATEGY_RECOMMENDATION_KIND
        ).strip().lower()
        if candidate_kind not in {TREND_EVALUATION_REJECTED_KIND, TREND_STRATEGY_RECOMMENDATION_KIND}:
            candidate_kind = TREND_STRATEGY_RECOMMENDATION_KIND
    rec["candidate_kind"] = candidate_kind
    reasons["candidate_kind"] = candidate_kind
    if isinstance(params, dict):
        params["candidate_kind"] = candidate_kind
        params["simulation_model"] = {
            "scope": "historical_proxy_only",
            "runtime_execution_validation": "not_performed",
            "instrument_constraints": "model_inputs_only_when_historically_available",
            "fill_attestation": "not_exchange_attested",
        }
        risk_report = params.get("risk_report")
        if isinstance(risk_report, dict):
            risk_report["decision"] = _risk_report_decision_for_status(rec.get("status"))
            params["risk_report"] = risk_report


def _llm_review_hold_target_status(rec: dict[str, Any]) -> str | None:
    reasons = rec.get("reasons") if isinstance(rec.get("reasons"), dict) else {}
    llm_review = reasons.get("llm_review") if isinstance(reasons, dict) and isinstance(reasons.get("llm_review"), dict) else {}
    target = str(llm_review.get("publish_target_status") or "").strip().lower()
    if target in LLM_REVIEW_ELIGIBLE_STATUSES:
        return target
    return None


def _restore_llm_held_status(rec: dict[str, Any]) -> str | None:
    current = str(rec.get("status") or "").strip().lower()
    if current != "pending":
        return None
    target = _llm_review_hold_target_status(rec)
    if target is None:
        return None
    rec["status"] = target
    return target


def _carry_forward_llm_hold_target(rec: dict[str, Any], review_dict: dict[str, Any]) -> dict[str, Any]:
    target = _llm_review_hold_target_status(rec)
    if target in LLM_REVIEW_ELIGIBLE_STATUSES:
        review_dict["publish_target_status"] = target
    return review_dict


def _llm_review_pending_timeout_sec(settings) -> int:
    try:
        explicit = int(getattr(settings, "llm_reviewer_pending_timeout_sec", LLM_REVIEWER_DEFAULT_PENDING_TIMEOUT_SEC) or LLM_REVIEWER_DEFAULT_PENDING_TIMEOUT_SEC)
    except Exception:
        explicit = LLM_REVIEWER_DEFAULT_PENDING_TIMEOUT_SEC
    cadence_sec = max(5, int(getattr(settings, "llm_reviewer_cadence_sec", LLM_REVIEWER_DEFAULT_CADENCE_SEC) or LLM_REVIEWER_DEFAULT_CADENCE_SEC))
    timeout_sec = max(5, int(getattr(settings, "llm_reviewer_timeout_sec", 60) or 60))
    # Pending is an operator-visible execution hold. It must never be indefinite:
    # at minimum allow one cadence and one model timeout, then fail safely.
    return max(60, int(explicit), cadence_sec + timeout_sec)


def _llm_review_ttl_sec(settings) -> int:
    explicit_ttl = getattr(settings, "llm_reviewer_ttl_sec", None)
    if explicit_ttl is not None:
        try:
            return max(60, int(explicit_ttl))
        except Exception:
            pass
    reco_ttl = _recommendation_ttl_sec(settings)
    cadence_sec = max(5, int(getattr(settings, "llm_reviewer_cadence_sec", LLM_REVIEWER_DEFAULT_CADENCE_SEC) or LLM_REVIEWER_DEFAULT_CADENCE_SEC))
    return max(int(reco_ttl), cadence_sec, LLM_REVIEWER_DEFAULT_TTL_SEC)


def _load_llm_review_cache(conn) -> dict[str, dict[str, Any]]:
    raw = db.get_app_config_json(conn, LLM_REVIEW_CACHE_APP_KEY, default={}) or {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, state in raw.items():
        if not isinstance(key, str) or not isinstance(state, dict):
            continue
        try:
            out[key] = {
                "ts": int(state.get("ts", 0) or 0),
                "provider": str(state.get("provider") or "ollama"),
                "model": str(state.get("model") or ""),
                "prompt_version": str(state.get("prompt_version") or ""),
                "thesis_direction": normalize_direction(state.get("thesis_direction"), allow_short=True),
                "execution_direction": normalize_direction(state.get("execution_direction"), allow_short=True),
                "confidence": _finite_float(state.get("confidence", 0.0), 0.0),
                "context_signature": str(state.get("context_signature") or ""),
                "regime_view": str(state.get("regime_view") or "unknown"),
                "summary": state.get("summary"),
                "risk_flags": [str(x) for x in (state.get("risk_flags") or [])[:8]],
            }
        except Exception:
            continue
    return out


def _save_llm_review_cache(conn, state: dict[str, dict[str, Any]], fresh_ttl_sec: int) -> None:
    now = int(time.time())
    ttl = max(int(fresh_ttl_sec) * 4, 1800)
    payload: dict[str, dict[str, Any]] = {}
    for key, meta in (state or {}).items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        ts = int(_safe_int_or_none(meta.get("ts")) or 0)
        if ts <= 0 or now - ts > ttl:
            continue
        payload[key] = {
            "ts": ts,
            "provider": str(meta.get("provider") or "ollama"),
            "model": str(meta.get("model") or ""),
            "prompt_version": str(meta.get("prompt_version") or ""),
            "thesis_direction": normalize_direction(meta.get("thesis_direction"), allow_short=True),
            "execution_direction": normalize_direction(meta.get("execution_direction"), allow_short=True),
            "confidence": _finite_float(meta.get("confidence", 0.0), 0.0),
            "context_signature": str(meta.get("context_signature") or ""),
            "regime_view": str(meta.get("regime_view") or "unknown"),
            "summary": meta.get("summary"),
            "risk_flags": [str(x) for x in (meta.get("risk_flags") or [])[:8]],
        }
    db.set_app_config_json(conn, LLM_REVIEW_CACHE_APP_KEY, payload)


def _llm_cache_direction_signature(rec: dict[str, Any]) -> str:
    reasons = rec.get("reasons") if isinstance(rec.get("reasons"), dict) else {}
    execution_constraints = reasons.get("execution_constraints") if isinstance(reasons, dict) and isinstance(reasons.get("execution_constraints"), dict) else {}
    direction_agg = reasons.get("direction_agg") if isinstance(reasons, dict) and isinstance(reasons.get("direction_agg"), dict) else {}
    exec_direction = normalize_direction(rec.get("direction"), allow_short=True)
    raw_direction = normalize_direction(
        execution_constraints.get("raw_direction") or direction_agg.get("raw_direction"),
        allow_short=True,
    )
    if raw_direction != exec_direction:
        return f"{raw_direction}->{exec_direction}"
    return exec_direction


def _llm_cache_key(rec: dict[str, Any]) -> str:
    return "|".join([
        str(rec.get("venue") or "").lower(),
        str(rec.get("symbol") or "").upper(),
        str(rec.get("bot_type") or "").lower(),
        _llm_cache_direction_signature(rec),
    ])


def _cache_meta_from_result(result: Any, *, context_signature: str = "") -> dict[str, Any] | None:
    if str(getattr(result, "status", "")) != "ok":
        return None
    return {
        "provider": getattr(result, "provider", "ollama"),
        "model": getattr(result, "model", ""),
        "prompt_version": getattr(result, "prompt_version", ""),
        "thesis_direction": normalize_direction(getattr(result, "thesis_direction", None), allow_short=True),
        "execution_direction": normalize_direction(getattr(result, "execution_direction", None), allow_short=True),
        "confidence": _finite_float(getattr(result, "confidence", 0.0), 0.0),
        "regime_view": str(getattr(result, "regime_view", "unknown") or "unknown"),
        "summary": getattr(result, "summary", None),
        "risk_flags": [str(x) for x in (getattr(result, "risk_flags", []) or [])[:8]],
        "context_signature": str(context_signature or ""),
    }


def _build_cached_review_dict(
    meta: dict[str, Any],
    rec: dict[str, Any],
    mode: str,
    age_sec: int,
    *,
    source: str = "cache",
    inherited_from_rec_id: str | None = None,
) -> dict[str, Any]:
    thesis_direction = normalize_direction(meta.get("thesis_direction"), allow_short=True)
    allow_short_exec = True
    execution_direction_raw = meta.get("execution_direction")
    if execution_direction_raw in (None, ""):
        execution_direction_raw = thesis_direction
    execution_direction = normalize_direction(execution_direction_raw, allow_short=allow_short_exec)
    if thesis_direction == "neutral":
        execution_direction = "neutral"
    review_ts_raw = meta.get("ts")
    try:
        review_ts = int(review_ts_raw) if review_ts_raw is not None else None
    except Exception:
        review_ts = None
    out = {
        "provider": meta.get("provider") or "ollama",
        "model": meta.get("model") or "",
        "prompt_version": meta.get("prompt_version") or "",
        "status": "ok",
        "mode": mode,
        "gate_decision": "pass",
        "thesis_direction": thesis_direction,
        "execution_direction": execution_direction,
        "confidence": _finite_float(meta.get("confidence", 0.0), 0.0),
        "regime_view": str(meta.get("regime_view") or "unknown"),
        "summary": meta.get("summary"),
        "risk_flags": [str(x) for x in (meta.get("risk_flags") or [])[:8]],
        "agree_with_engine": execution_direction == normalize_direction(rec.get("direction"), allow_short=allow_short_exec),
        "cached": True,
        "cache_age_sec": int(max(0, age_sec)),
        "source": source,
        "review_ts": review_ts,
    }
    if inherited_from_rec_id:
        out["inherited_from_rec_id"] = str(inherited_from_rec_id)
    return out


def _llm_cache_age_sec(meta: dict[str, Any]) -> int | None:
    if not isinstance(meta, dict) or not meta:
        return None
    try:
        ts = int(meta.get("ts", 0) or 0)
    except Exception:
        return None
    if ts <= 0:
        return None
    return max(0, int(time.time()) - ts)


def _is_fresh_llm_cache_entry(
    meta: dict[str, Any],
    reviewer: OllamaCandleReviewer | None,
    fresh_ttl_sec: int,
    *,
    context_signature: str = "",
) -> tuple[bool, int | None]:
    cache_age = _llm_cache_age_sec(meta)
    if not isinstance(meta, dict) or not meta or cache_age is None:
        return False, cache_age
    if cache_age > max(5, int(fresh_ttl_sec or 5)):
        return False, cache_age
    provider = str(meta.get("provider") or "ollama")
    model = str(meta.get("model") or "")
    prompt_version = str(meta.get("prompt_version") or "")
    reviewer_provider = str(getattr(reviewer, "provider", "ollama") or "ollama")
    reviewer_model = str(getattr(reviewer, "model", "") or "")
    reviewer_prompt_version = str(getattr(reviewer, "prompt_version", PROMPT_VERSION) or PROMPT_VERSION)
    if provider != reviewer_provider or model != reviewer_model:
        return False, cache_age
    if prompt_version and prompt_version != reviewer_prompt_version:
        return False, cache_age
    cached_context_signature = str(meta.get("context_signature") or "")
    if context_signature and cached_context_signature and cached_context_signature != context_signature:
        return False, cache_age
    if context_signature and not cached_context_signature:
        return False, cache_age
    return True, cache_age

LLM_REVIEW_ELIGIBLE_STATUSES = frozenset({"recommended", "active"})


def _is_llm_review_eligible_status(status: Any) -> bool:
    return str(status or "").strip().lower() in LLM_REVIEW_ELIGIBLE_STATUSES


def _llm_candidate_sort_key(rec: dict[str, Any]) -> tuple[float, float]:
    # Исторические/manual записи могут содержать строки, NaN/Infinity или иной мусор
    # в `score/confidence`. LLM-очередь не должна падать из-за одной битой записи.
    return (_finite_float(rec.get("confidence"), 0.0), _finite_float(rec.get("score"), 0.0))


def _make_pending_llm_review(rec: dict[str, Any], mode: str, reviewer: OllamaCandleReviewer | None, *, reason: str = "queued") -> dict[str, Any]:
    target_status = str(rec.get("status") or "").strip().lower()
    review = {
        "provider": getattr(reviewer, "provider", "ollama") if reviewer is not None else "ollama",
        "model": getattr(reviewer, "model", "") if reviewer is not None else "",
        "prompt_version": getattr(reviewer, "prompt_version", None) or "ohlcv_multitf_v1",
        "status": "pending",
        "mode": mode,
        "gate_decision": "pending",
        "reason": reason,
        "queued_ts": int(time.time()),
    }
    if target_status in LLM_REVIEW_ELIGIBLE_STATUSES:
        review["publish_target_status"] = target_status
    review["hold_policy"] = "llm_verdict_required"
    review["requires_ok_verdict"] = True
    return review


def _sanitize_llm_review_dict(rec: dict[str, Any], review_dict: dict[str, Any]) -> dict[str, Any]:
    out = dict(review_dict or {})
    allow_short_exec = True
    thesis_direction = normalize_direction(out.get("thesis_direction"), allow_short=True)
    execution_direction = normalize_direction(out.get("execution_direction"), allow_short=allow_short_exec)
    if thesis_direction == "neutral":
        execution_direction = "neutral"
    out["thesis_direction"] = thesis_direction
    out["execution_direction"] = execution_direction
    out["confidence"] = _finite_float(out.get("confidence", 0.0), 0.0)
    out["agree_with_engine"] = execution_direction == normalize_direction(rec.get("direction"), allow_short=allow_short_exec)
    return out


def _mark_llm_reviews_async(conn, recs: list[dict[str, Any]], settings, reviewer: OllamaCandleReviewer | None = None) -> dict[str, int]:
    stats = {"queued": 0, "skipped": 0, "deferred": 0, "reviewed": 0, "cached": 0, "vetoed": 0, "errors": 0, "inherited": 0}
    if not bool(getattr(settings, "llm_reviewer_enabled", False)):
        return stats
    mode = str(getattr(settings, "llm_reviewer_mode", "advisory") or "advisory").strip().lower()
    if mode not in {"advisory", "gate"}:
        mode = "advisory"
    fresh_ttl_sec = _llm_review_ttl_sec(settings)
    min_conf = float(getattr(settings, "llm_reviewer_min_confidence", LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE) or LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE)
    context_signature = _llm_reviewer_context_signature(settings)
    llm_cache = _load_llm_review_cache(conn)
    max_candidates = max(1, int(getattr(settings, "llm_reviewer_max_candidates", LLM_REVIEWER_DEFAULT_MAX_CANDIDATES) or LLM_REVIEWER_DEFAULT_MAX_CANDIDATES))
    candidates = [r for r in recs if _is_llm_review_eligible_status(r.get("status"))]
    candidates.sort(key=_llm_candidate_sort_key, reverse=True)
    queued_ids = {str(r.get("rec_id")) for r in candidates[:max_candidates]}
    for rec in recs:
        if not _is_llm_review_eligible_status(rec.get("status")):
            _sync_recommendation_metadata(rec)
            continue
        reasons = rec.setdefault("reasons", {})
        cache_key = _llm_cache_key(rec)
        cache_meta = llm_cache.get(cache_key) or {}
        cache_is_fresh, cache_age = _is_fresh_llm_cache_entry(cache_meta, reviewer, fresh_ttl_sec, context_signature=context_signature)
        if cache_is_fresh:
            review_dict = _build_cached_review_dict(
                cache_meta,
                rec,
                mode,
                int(cache_age or 0),
                source="cache_inherited",
            )
            reasons["llm_review"] = review_dict
            stats["cached"] += 1
            stats["inherited"] += 1
            vetoed, errored = _apply_llm_review_decision(
                conn,
                rec,
                review_dict,
                min_conf=min_conf,
                source="cache_inherited",
                diagnostics={"cache_key": cache_key, "phase": "publish_annotation"},
                persist_decision_log=False,
            )
            if not vetoed and not errored:
                _restore_llm_held_status(rec)
            if vetoed:
                stats["vetoed"] += 1
            if errored:
                stats["errors"] += 1
            _sync_recommendation_metadata(rec)
            continue
        if str(rec.get("rec_id") or "") in queued_ids:
            reasons["llm_review"] = _make_pending_llm_review(rec, mode, reviewer, reason="queued_async")
            stats["queued"] += 1
            _hold_recommendation_until_llm_verdict(rec, settings, reviewer, reason="queued_async")
        else:
            reasons["llm_review"] = _make_pending_llm_review(rec, mode, reviewer, reason=f"deferred_candidate_cap ({max_candidates})")
            stats["deferred"] += 1
            _hold_recommendation_until_llm_verdict(rec, settings, reviewer, reason=f"deferred_candidate_cap ({max_candidates})")
        _sync_recommendation_metadata(rec)
    return stats


def _load_llm_candles_for_symbol(conn, venue: str, symbol: str, tf_secs: list[int], candle_limit: int, *, ts_now: int | None = None) -> dict[int, list[list[float | int]]]:
    out: dict[int, list[list[float | int]]] = {}
    if ts_now is None:
        ts_now = int(time.time())
    for tf_sec in tf_secs:
        try:
            rows_newest = db.get_latest_ohlcv(conn, venue, symbol, tf_sec=tf_sec, limit=candle_limit)
        except Exception:
            continue
        rows_newest = _drop_open_candle(rows_newest, tf_sec=int(tf_sec), ts_now=int(ts_now))
        if not rows_newest:
            continue
        out[int(tf_sec)] = _serialize_llm_candles([dict(r) for r in reversed(rows_newest)], candle_limit)
    return out


def _should_enqueue_llm_review(rec: dict[str, Any]) -> bool:
    current_status = str(rec.get("status") or "").strip().lower()
    hold_target_status = _llm_review_hold_target_status(rec)
    if current_status != "pending":
        if not _is_llm_review_eligible_status(current_status):
            return False
    elif hold_target_status not in LLM_REVIEW_ELIGIBLE_STATUSES:
        return False
    llm_review = ((rec.get("reasons") or {}).get("llm_review") if isinstance(rec.get("reasons"), dict) else None) or {}
    llm_status = str(llm_review.get("status") or "").lower()
    if llm_status in {"ok", "skipped"}:
        return False
    return True


def _llm_review_recent_sec(settings) -> int:
    ttl_sec = _llm_review_ttl_sec(settings)
    reco_ttl_sec = _recommendation_ttl_sec(settings)
    cadence_sec = max(5, int(getattr(settings, "llm_reviewer_cadence_sec", LLM_REVIEWER_DEFAULT_CADENCE_SEC) or LLM_REVIEWER_DEFAULT_CADENCE_SEC))
    return max(int(ttl_sec), int(reco_ttl_sec), cadence_sec * 4, 3600)


def _llm_pending_sort_key(rec: dict[str, Any]) -> tuple[int, float, float]:
    return (int(rec.get("ts") or 0), _finite_float(rec.get("confidence"), 0.0), _finite_float(rec.get("score"), 0.0))



def _llm_pending_first_seen_ts(rec: dict[str, Any]) -> int:
    llm_review = ((rec.get("reasons") or {}).get("llm_review") if isinstance(rec.get("reasons"), dict) else None) or {}
    queued_ts = llm_review.get("queued_ts") if isinstance(llm_review, dict) else None
    try:
        queued_val = int(queued_ts)
    except Exception:
        queued_val = 0
    if queued_val > 0:
        return queued_val
    return int(rec.get("ts") or 0)



def _selected_recent_pending_llm_pool(conn, settings, *, snapshot_ts: int | None = None, limit: int = 4000) -> list[dict[str, Any]]:
    recent_sec = _llm_review_recent_sec(settings)
    recent_pool = db.get_recent_llm_review_candidates(
        conn,
        recent_sec=recent_sec,
        limit=max(1, int(limit)),
        snapshot_ts=None,
    )
    pending = [r for r in recent_pool if _should_enqueue_llm_review(r)]
    if not pending:
        return []
    if snapshot_ts is None:
        return pending
    latest_pending = [r for r in pending if int(r.get("ts") or 0) == int(snapshot_ts)]
    if not latest_pending:
        return pending
    selected_keys = {_llm_cache_key(r) for r in latest_pending}
    return [r for r in pending if int(r.get("ts") or 0) == int(snapshot_ts) or _llm_cache_key(r) in selected_keys]



def _recent_pending_llm_candidates(conn, settings, max_candidates: int, *, snapshot_ts: int | None = None) -> list[dict[str, Any]]:
    limit = max(max_candidates * 12, max_candidates)
    if snapshot_ts is not None:
        limit = max(limit * 6, 200)
    pending = _selected_recent_pending_llm_pool(conn, settings, snapshot_ts=snapshot_ts, limit=limit)
    if not pending:
        return []

    grouped_pending: dict[str, list[dict[str, Any]]] = {}
    for rec in pending:
        grouped_pending.setdefault(_llm_cache_key(rec), []).append(rec)

    ordered_groups: list[tuple[int, list[dict[str, Any]]]] = []
    for peers in grouped_pending.values():
        peers.sort(key=_llm_pending_sort_key, reverse=True)
        first_seen_ts = min(_llm_pending_first_seen_ts(peer) for peer in peers)
        ordered_groups.append((first_seen_ts, peers))
    ordered_groups.sort(key=lambda item: (item[0], -_llm_pending_sort_key(item[1][0])[0], -_llm_pending_sort_key(item[1][0])[1], -_llm_pending_sort_key(item[1][0])[2]))

    selected: list[dict[str, Any]] = []
    for _, peers in ordered_groups[:max(1, int(max_candidates))]:
        selected.extend(peers)
    return selected



def _count_recent_pending_llm_candidates(conn, settings, *, snapshot_ts: int | None = None) -> int:
    pool = _selected_recent_pending_llm_pool(conn, settings, snapshot_ts=snapshot_ts, limit=4000 if snapshot_ts is not None else 2000)
    return len(pool)


def _llm_pending_age_sec(rec: dict[str, Any], *, now_ts: int) -> int:
    first_seen = _llm_pending_first_seen_ts(rec)
    if first_seen <= 0:
        return 0
    return max(0, int(now_ts) - int(first_seen))


def _resolve_stale_llm_pending(
    conn,
    recs: list[dict[str, Any]],
    settings,
    *,
    now_ts_value: int | None = None,
    reason: str = "timeout",
) -> dict[str, int]:
    """Resolve async LLM holds that exceeded the operator-visible SLA.

    If the external reviewer did not return an OK verdict in time, the row becomes
    `no_trade` and cannot be launched manually. This prevents stale async holds
    from reappearing as actionable recommendations without a real LLM verdict.
    """
    now_val = int(now_ts_value if now_ts_value is not None else time.time())
    pending_timeout_sec = _llm_review_pending_timeout_sec(settings)
    stats = {"resolved": 0, "restored": 0, "failed_closed": 0}
    for rec in list(recs or []):
        current_status = str(rec.get("status") or "").strip().lower()
        reasons = rec.setdefault("reasons", {})
        if not isinstance(reasons, dict):
            reasons = {}
            rec["reasons"] = reasons
        llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else {}
        llm_status = str((llm_review or {}).get("status") or "").strip().lower()
        mode = str((llm_review or {}).get("mode") or getattr(settings, "llm_reviewer_mode", "advisory") or "advisory").strip().lower()
        target = _llm_review_hold_target_status(rec)
        is_pending_hold = current_status == "pending"
        is_nonblocking_advisory_review = (
            mode == "advisory"
            and current_status in LLM_REVIEW_ELIGIBLE_STATUSES
            and llm_status not in {"ok", "skipped"}
        )
        if not (is_pending_hold or is_nonblocking_advisory_review):
            continue
        age_sec = _llm_pending_age_sec(rec, now_ts=now_val)
        if age_sec < pending_timeout_sec:
            continue
        new_status = "no_trade"
        stats["failed_closed"] += 1
        terminal_review = {
            **(llm_review or {}),
            "status": "error",
            "mode": mode if mode in {"advisory", "gate"} else "gate",
            "gate_decision": "fail_closed",
            "source": "async_timeout",
            "reason": "llm_timeout_no_trade",
            "error": f"LLM reviewer did not finish within {pending_timeout_sec}s; launch was blocked fail-closed because an OK LLM verdict is required.",
            "resolved_ts": now_val,
            "age_sec": age_sec,
            "pending_timeout_sec": pending_timeout_sec,
            "hold_policy": "llm_verdict_required",
            "requires_ok_verdict": True,
        }
        if target in LLM_REVIEW_ELIGIBLE_STATUSES:
            terminal_review["publish_target_status"] = target
        reasons["llm_review"] = terminal_review
        rec["status"] = new_status
        _sync_recommendation_metadata(rec)
        db.update_recommendation_review(conn, rec["rec_id"], reasons=reasons, status=new_status)
        db.log_decision(conn, "LLM_REVIEW_PENDING_TIMEOUT", rec.get("rec_id"), None, {
            "new_status": new_status,
            "mode": mode,
            "reason": reason,
            "age_sec": age_sec,
            "pending_timeout_sec": pending_timeout_sec,
            "publish_target_status": target,
        })
        stats["resolved"] += 1
    return stats


def run_llm_review_sweep_once(conn, settings, *, heartbeat=None) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "enabled": bool(getattr(settings, "llm_reviewer_enabled", False)),
        "queued": 0,
        "completed": 0,
        "cached": 0,
        "inherited": 0,
        "vetoed": 0,
        "errors": 0,
        "skipped": 0,
        "snapshot_ts": None,
        "recent_window_sec": _llm_review_recent_sec(settings),
        "pending_before": 0,
        "pending_after": 0,
        "pending_timeout_sec": _llm_review_pending_timeout_sec(settings),
        "stale_resolved": 0,
        "stale_restored": 0,
        "stale_failed_closed": 0,
        "duration_ms": 0,
        "max_workers": int(getattr(settings, "llm_reviewer_max_workers", LLM_REVIEWER_DEFAULT_MAX_WORKERS) or LLM_REVIEWER_DEFAULT_MAX_WORKERS),
        "max_candidates": int(getattr(settings, "llm_reviewer_max_candidates", LLM_REVIEWER_DEFAULT_MAX_CANDIDATES) or LLM_REVIEWER_DEFAULT_MAX_CANDIDATES),
    }
    if not stats["enabled"]:
        return stats

    def _check_heartbeat() -> None:
        if heartbeat is not None and not heartbeat():
            raise RuntimeLockLostError("llm reviewer runtime lock lost")

    t0 = time.time()
    _check_heartbeat()
    snapshot_ts = db.get_latest_reco_ts(conn)
    stats["snapshot_ts"] = snapshot_ts
    if snapshot_ts is None:
        return stats

    _check_heartbeat()
    pending_scope = _selected_recent_pending_llm_pool(conn, settings, snapshot_ts=snapshot_ts, limit=4000)
    stats["pending_before"] = len(pending_scope)
    stale_stats = _resolve_stale_llm_pending(conn, pending_scope, settings, reason="pre_sweep")
    if stale_stats["resolved"]:
        stats["stale_resolved"] += stale_stats["resolved"]
        stats["stale_restored"] += stale_stats["restored"]
        stats["stale_failed_closed"] += stale_stats["failed_closed"]
        pending_scope = _selected_recent_pending_llm_pool(conn, settings, snapshot_ts=snapshot_ts, limit=4000)
    scope_keys = {_llm_cache_key(r) for r in pending_scope if int(r.get("ts") or 0) == int(snapshot_ts)}
    scope_has_latest_pending = bool(scope_keys)

    try:
        reviewer = _make_llm_reviewer(settings)
    except Exception as exc:
        stats["errors"] += 1
        stats["error"] = str(exc)
        stats["pending_after"] = _count_recent_pending_llm_candidates(conn, settings, snapshot_ts=snapshot_ts)
        stats["duration_ms"] = int((time.time() - t0) * 1000)
        db.set_app_config_json(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, {**stats, "updated_ts": int(time.time())})
        db.log_decision(conn, "LLM_REVIEW_SWEEP_ERROR", None, None, {"err": str(exc), "stage": "reviewer_init", **stats})
        return stats
    if reviewer is None:
        stats["pending_after"] = _count_recent_pending_llm_candidates(conn, settings, snapshot_ts=snapshot_ts)
        stats["duration_ms"] = int((time.time() - t0) * 1000)
        db.set_app_config_json(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, {**stats, "updated_ts": int(time.time())})
        return stats

    mode = str(getattr(settings, "llm_reviewer_mode", "advisory") or "advisory").strip().lower()
    min_conf = float(getattr(settings, "llm_reviewer_min_confidence", LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE) or LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE)
    fresh_ttl_sec = _llm_review_ttl_sec(settings)
    context_signature = _llm_reviewer_context_signature(settings)
    llm_cache = _load_llm_review_cache(conn)
    cache_dirty = False

    _check_heartbeat()
    # Candidate selection must scan beyond the live-call cap. Otherwise fresh-cache keys can
    # consume the whole selection budget and permanently starve lower-ranked uncached symbols.
    # Scan the whole latest pending snapshot (capped) and only apply the live-call cap after
    # cache hits have been resolved.
    candidate_scan_budget = max(stats["max_candidates"], min(300, int(stats.get("pending_before", 0) or 0)))
    candidates = _recent_pending_llm_candidates(conn, settings, candidate_scan_budget, snapshot_ts=snapshot_ts)
    _check_heartbeat()
    if not candidates:
        stats["duration_ms"] = int((time.time() - t0) * 1000)
        stats["pending_after"] = 0
        return stats

    grouped_candidates: dict[str, list[dict[str, Any]]] = {}
    for rec in candidates:
        cache_key = _llm_cache_key(rec)
        cache_meta = llm_cache.get(cache_key) or {}
        cache_is_fresh, cache_age = _is_fresh_llm_cache_entry(cache_meta, reviewer, fresh_ttl_sec, context_signature=context_signature)
        if cache_is_fresh:
            reasons = rec.setdefault("reasons", {})
            review_dict = _build_cached_review_dict(
                cache_meta,
                rec,
                mode,
                int(cache_age or 0),
                source="async_cache",
            )
            review_dict = _carry_forward_llm_hold_target(rec, review_dict)
            reasons["llm_review"] = review_dict
            vetoed, errored = _apply_llm_review_decision(
                conn,
                rec,
                review_dict,
                min_conf=min_conf,
                source="async_cache",
                diagnostics={"cache_key": cache_key, "phase": "sweep"},
            )
            restored_status = None if (vetoed or errored) else _restore_llm_held_status(rec)
            _sync_recommendation_metadata(rec)
            db.update_recommendation_review(conn, rec["rec_id"], reasons=reasons, status=rec.get("status") if (vetoed or restored_status is not None) else None)
            stats["cached"] += 1
            if vetoed:
                stats["vetoed"] += 1
            if errored:
                stats["errors"] += 1
            continue
        grouped_candidates.setdefault(cache_key, []).append(rec)

    tf_secs = list(getattr(settings, "llm_reviewer_tf_secs", []) or [])
    candle_limit = int(getattr(settings, "llm_reviewer_candles_per_tf", LLM_REVIEWER_DEFAULT_CANDLES_PER_TF) or LLM_REVIEWER_DEFAULT_CANDLES_PER_TF)
    jobs: list[tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    for cache_key, peers in grouped_candidates.items():
        if len(jobs) >= int(stats["max_candidates"]):
            break
        rec = peers[0]
        venue = str(rec.get("venue") or "")
        symbol = str(rec.get("symbol") or "")
        reasons = rec.setdefault("reasons", {})
        direction_agg = reasons.get("direction_agg") if isinstance(reasons, dict) else {}
        sentiment_summary = reasons.get("symbol_sentiment") if isinstance(reasons, dict) else {}
        market_shock = reasons.get("market_shock") if isinstance(reasons, dict) else {}
        candles_by_tf = _load_llm_candles_for_symbol(conn, venue, symbol, tf_secs, candle_limit, ts_now=int(time.time()))
        payload = build_review_payload(
            rec=rec,
            feature_snapshot=(reasons.get("feature_snapshot") if isinstance(reasons, dict) else None) or {},
            direction_agg=direction_agg or {},
            market_shock=market_shock or {},
            sentiment_summary=sentiment_summary or {},
            candles_by_tf=candles_by_tf,
        )
        jobs.append((cache_key, rec, peers, payload))

    stats["queued"] = len(jobs)
    if not jobs:
        if scope_has_latest_pending:
            recent_pool = db.get_recent_llm_review_candidates(conn, recent_sec=_llm_review_recent_sec(settings), limit=4000, snapshot_ts=None)
            stats["pending_after"] = sum(
                1 for r in recent_pool
                if _should_enqueue_llm_review(r)
                and (int(r.get("ts") or 0) == int(snapshot_ts) or _llm_cache_key(r) in scope_keys)
            )
        else:
            stats["pending_after"] = _count_recent_pending_llm_candidates(conn, settings, snapshot_ts=snapshot_ts)
        stats["duration_ms"] = int((time.time() - t0) * 1000)
        db.set_app_config_json(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, {**stats, "updated_ts": int(time.time())})
        db.log_decision(conn, "LLM_REVIEW_SWEEP", None, None, stats)
        return stats

    max_workers = max(1, min(int(getattr(settings, "llm_reviewer_max_workers", LLM_REVIEWER_DEFAULT_MAX_WORKERS) or LLM_REVIEWER_DEFAULT_MAX_WORKERS), len(jobs)))
    stats["max_workers"] = max_workers
    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="llm-review") as pool:
        for cache_key, rec, peers, payload in jobs:
            futures[pool.submit(reviewer.review, payload)] = (cache_key, rec, peers)
        for fut in as_completed(futures):
            _check_heartbeat()
            cache_key, rec, peers = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                for peer in peers:
                    reasons = peer.setdefault("reasons", {})
                    review_dict = {
                        "provider": getattr(reviewer, "provider", "ollama"),
                        "model": getattr(reviewer, "model", ""),
                        "status": "error",
                        "mode": mode,
                        "gate_decision": "pass",
                        "error": str(exc),
                        "source": "async_live",
                    }
                    reasons["llm_review"] = review_dict
                    _sync_recommendation_metadata(peer)
                    db.update_recommendation_review(conn, peer["rec_id"], reasons=reasons, status=None)
                    db.log_decision(conn, "LLM_REVIEW_ERROR", peer.get("rec_id"), None, {"err": str(exc), "source": "async_sweep_exception", "cache_key": cache_key})
                stats["errors"] += len(peers)
                continue

            review_ts = int(time.time())
            result_meta = _cache_meta_from_result(result, context_signature=context_signature)
            if result_meta is not None:
                llm_cache[cache_key] = {"ts": review_ts, **result_meta}
                cache_dirty = True
            inherited_meta = llm_cache.get(cache_key) or ({"ts": review_ts, **(result_meta or {})} if result_meta is not None else {})

            for idx, peer in enumerate(peers):
                reasons = peer.setdefault("reasons", {})
                if idx == 0:
                    review_dict = _sanitize_llm_review_dict(peer, result.to_dict())
                    review_dict = _carry_forward_llm_hold_target(peer, review_dict)
                    review_dict["mode"] = mode
                    review_dict["gate_decision"] = "pass"
                    review_dict["source"] = "async_live"
                    review_dict["review_ts"] = review_ts
                    reasons["llm_review"] = review_dict
                    vetoed, errored = _apply_llm_review_decision(
                        conn,
                        peer,
                        review_dict,
                        min_conf=min_conf,
                        source="async_live",
                        latency_ms=getattr(result, "latency_ms", None),
                        diagnostics=getattr(result, "diagnostics", {}),
                    )
                else:
                    review_dict = _build_cached_review_dict(
                        inherited_meta,
                        peer,
                        mode,
                        0,
                        source="async_inherited",
                        inherited_from_rec_id=rec.get("rec_id"),
                    )
                    review_dict = _carry_forward_llm_hold_target(peer, review_dict)
                    reasons["llm_review"] = review_dict
                    vetoed, errored = _apply_llm_review_decision(
                        conn,
                        peer,
                        review_dict,
                        min_conf=min_conf,
                        source="async_inherited",
                        diagnostics={"cache_key": cache_key, "parent_rec_id": rec.get("rec_id")},
                    )
                    stats["inherited"] += 1
                restored_status = None if (vetoed or errored) else _restore_llm_held_status(peer)
                _sync_recommendation_metadata(peer)
                db.update_recommendation_review(conn, peer["rec_id"], reasons=reasons, status=peer.get("status") if (vetoed or restored_status is not None) else None)
                stats["completed"] += 1
                _check_heartbeat()
                if vetoed:
                    stats["vetoed"] += 1
                if errored:
                    stats["errors"] += 1
    if cache_dirty:
        _save_llm_review_cache(conn, llm_cache, fresh_ttl_sec)
    if scope_has_latest_pending:
        recent_pool = db.get_recent_llm_review_candidates(conn, recent_sec=_llm_review_recent_sec(settings), limit=4000, snapshot_ts=None)
        stats["pending_after"] = sum(
            1 for r in recent_pool
            if _should_enqueue_llm_review(r)
            and (int(r.get("ts") or 0) == int(snapshot_ts) or _llm_cache_key(r) in scope_keys)
        )
    else:
        stats["pending_after"] = _count_recent_pending_llm_candidates(conn, settings, snapshot_ts=snapshot_ts)
    stats["duration_ms"] = int((time.time() - t0) * 1000)
    db.set_app_config_json(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, {**stats, "updated_ts": int(time.time())})
    db.log_decision(conn, "LLM_REVIEW_SWEEP", None, None, stats)
    return stats


def _apply_llm_review_decision(
    conn,
    rec: dict[str, Any],
    review_dict: dict[str, Any],
    *,
    min_conf: float,
    source: str,
    latency_ms: int | None = None,
    diagnostics: dict[str, Any] | None = None,
    persist_decision_log: bool = True,
) -> tuple[bool, bool]:
    mode = str(review_dict.get("mode") or "advisory")
    venue = str(rec.get("venue") or "")
    symbol = str(rec.get("symbol") or "")
    bot_type = str(rec.get("bot_type") or "")
    status = str(review_dict.get("status") or "unknown")

    if status == "error":
        if persist_decision_log:
            db.log_decision(conn, "LLM_REVIEW_ERROR", rec.get("rec_id"), None, {
                "venue": venue,
                "symbol": symbol,
                "bot_type": bot_type,
                "model": review_dict.get("model"),
                "source": source,
                "error": review_dict.get("error"),
                "latency_ms": latency_ms,
                "diagnostics": diagnostics or {},
            })
        return False, True

    llm_confidence = _finite_float(review_dict.get("confidence") or 0.0, 0.0)
    if mode == "gate" and llm_confidence >= float(min_conf) and str(review_dict.get("execution_direction") or "neutral") != str(rec.get("direction") or "neutral"):
        prev_status = str(rec.get("status") or "")
        rec["status"] = "no_trade"
        review_dict["gate_decision"] = "veto"
        review_dict["gate_reason"] = "execution_direction_mismatch"
        if persist_decision_log:
            db.log_decision(conn, "LLM_REVIEW_VETO", rec.get("rec_id"), None, {
                "venue": venue,
                "symbol": symbol,
                "bot_type": bot_type,
                "prev_status": prev_status,
                "new_status": rec["status"],
                "engine_direction": rec.get("direction"),
                "llm_execution_direction": review_dict.get("execution_direction"),
                "llm_thesis_direction": review_dict.get("thesis_direction"),
                "llm_confidence": llm_confidence,
                "model": review_dict.get("model"),
                "source": source,
                "latency_ms": latency_ms,
                "diagnostics": diagnostics or {},
            })
        return True, False

    if persist_decision_log:
        db.log_decision(conn, "LLM_REVIEW_OK", rec.get("rec_id"), None, {
            "venue": venue,
            "symbol": symbol,
            "bot_type": bot_type,
            "engine_direction": rec.get("direction"),
            "llm_execution_direction": review_dict.get("execution_direction"),
            "llm_thesis_direction": review_dict.get("thesis_direction"),
            "llm_confidence": llm_confidence,
            "mode": mode,
            "gate_decision": review_dict.get("gate_decision"),
            "model": review_dict.get("model"),
            "source": source,
            "cached": bool(review_dict.get("cached")),
            "cache_age_sec": review_dict.get("cache_age_sec"),
            "latency_ms": latency_ms,
            "diagnostics": diagnostics or {},
        })
    return False, False


def _apply_llm_reviewer(
    conn,
    recs: list[dict[str, Any]],
    settings,
    *,
    symbol_feature_map: dict[tuple[str, str], dict[str, Any]],
    symbol_llm_candle_map: dict[tuple[str, str], dict[int, list[list[float | int]]]],
    sent_agg: dict[str, Any],
    market_shock: dict[str, Any],
    reviewer: OllamaCandleReviewer | None = None,
) -> dict[str, int]:
    stats = {"reviewed": 0, "cached": 0, "vetoed": 0, "errors": 0, "skipped": 0}
    if not bool(getattr(settings, "llm_reviewer_enabled", False)):
        return stats
    mode = str(getattr(settings, "llm_reviewer_mode", "advisory") or "advisory").strip().lower()
    if mode not in {"advisory", "gate"}:
        mode = "advisory"
    try:
        reviewer = reviewer or _make_llm_reviewer(settings)
    except Exception as exc:
        db.log_decision(conn, "LLM_REVIEW_ERROR", None, None, {
            "error": str(exc),
            "stage": "reviewer_init",
            "provider": getattr(settings, "llm_reviewer_provider", None),
            "model": getattr(settings, "llm_reviewer_model", None),
        })
        stats["errors"] += 1
        return stats
    if reviewer is None:
        return stats

    max_candidates = max(1, int(getattr(settings, "llm_reviewer_max_candidates", LLM_REVIEWER_DEFAULT_MAX_CANDIDATES) or LLM_REVIEWER_DEFAULT_MAX_CANDIDATES))
    min_conf = float(getattr(settings, "llm_reviewer_min_confidence", LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE) or LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE)
    fresh_ttl_sec = _llm_review_ttl_sec(settings)
    context_signature = _llm_reviewer_context_signature(settings)
    llm_cache = _load_llm_review_cache(conn)
    cache_dirty = False
    live_calls = 0

    candidates = [r for r in recs if _is_llm_review_eligible_status(r.get("status"))]
    candidates.sort(key=_llm_candidate_sort_key, reverse=True)

    for rec in candidates:
        reasons = rec.setdefault("reasons", {})
        venue = str(rec.get("venue") or "")
        symbol = str(rec.get("symbol") or "")
        cache_key = _llm_cache_key(rec)
        cache_meta = llm_cache.get(cache_key) or {}
        cache_is_fresh, cache_age = _is_fresh_llm_cache_entry(cache_meta, reviewer, fresh_ttl_sec, context_signature=context_signature)
        if cache_is_fresh:
            review_dict = _build_cached_review_dict(cache_meta, rec, mode, int(cache_age or 0), source="cache")
            reasons["llm_review"] = review_dict
            stats["cached"] += 1
            vetoed, errored = _apply_llm_review_decision(conn, rec, review_dict, min_conf=min_conf, source="cache", diagnostics={"cache_key": cache_key})
            if vetoed:
                stats["vetoed"] += 1
            if errored:
                stats["errors"] += 1
            _sync_recommendation_metadata(rec)
            continue

        if live_calls >= max_candidates:
            reasons["llm_review"] = {
                "provider": getattr(reviewer, "provider", "ollama"),
                "model": getattr(reviewer, "model", ""),
                "status": "skipped",
                "mode": mode,
                "error": f"candidate cap reached ({max_candidates})",
                "gate_decision": "skipped",
            }
            stats["skipped"] += 1
            _sync_recommendation_metadata(rec)
            continue

        f = symbol_feature_map.get((venue, symbol)) or {}
        direction_agg = (reasons.get("direction_agg") if isinstance(reasons, dict) else None) or f.get("_direction_agg") or {}
        sentiment_summary = (reasons.get("symbol_sentiment") if isinstance(reasons, dict) else None) or {
            "effective": sent_agg.get("effective_score", sent_agg.get("ewma", {}).get("6h", 0.0)),
            "global": sent_agg.get("effective_score", sent_agg.get("ewma", {}).get("6h", 0.0)),
        }
        payload = build_review_payload(
            rec=rec,
            feature_snapshot=(reasons.get("feature_snapshot") if isinstance(reasons, dict) else None) or {},
            direction_agg=direction_agg,
            market_shock=market_shock or {},
            sentiment_summary=sentiment_summary,
            candles_by_tf=symbol_llm_candle_map.get((venue, symbol), {}),
        )
        result = reviewer.review(payload)
        review_dict = _sanitize_llm_review_dict(rec, result.to_dict())
        review_dict["mode"] = mode
        review_dict["gate_decision"] = "pass"
        review_dict["source"] = "live"
        review_dict["review_ts"] = int(time.time())
        reasons["llm_review"] = review_dict
        stats["reviewed"] += 1
        live_calls += 1

        cache_meta_new = _cache_meta_from_result(result, context_signature=context_signature)
        if cache_meta_new is not None:
            llm_cache[cache_key] = {"ts": int(time.time()), **cache_meta_new}
            cache_dirty = True

        vetoed, errored = _apply_llm_review_decision(
            conn,
            rec,
            review_dict,
            min_conf=min_conf,
            source="live",
            latency_ms=getattr(result, "latency_ms", None),
            diagnostics=getattr(result, "diagnostics", {}),
        )
        if vetoed:
            stats["vetoed"] += 1
        if errored:
            stats["errors"] += 1
        _sync_recommendation_metadata(rec)

    if cache_dirty:
        _save_llm_review_cache(conn, llm_cache, fresh_ttl_sec)

    return stats


def _drop_open_candle(rows: list[dict[str, Any]] | list[Any], tf_sec: int, ts_now: int) -> list[Any]:
    """Return only fully closed candles, preserving the input order.

    REST storage can contain more than one still-forming/future row after clock skew,
    retries or malformed upstream data. Removing only the newest row is insufficient:
    a second open candle would leak unavailable prices into features. Invalid temporal
    metadata is therefore discarded and every row must prove ``ts + tf <= now``.
    """
    if not rows:
        return []
    tf_value = strict_integer(tf_sec)
    now_value = strict_integer(ts_now)
    if tf_value is None or tf_value <= 0 or now_value is None or now_value <= 0:
        return []

    closed: list[Any] = []
    for row in rows:
        try:
            candle_ts = strict_integer(row["ts"])
        except Exception:
            candle_ts = None
        if candle_ts is None or candle_ts <= 0:
            continue
        if candle_ts + tf_value <= now_value:
            closed.append(row)
    return closed


def _estimate_cost_model(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    taker_fee_bps: float,
    direction: str,
    funding_rate: float | None = None,
    next_funding_ts: int | None = None,
    ts_now: int | None = None,
    funding_interval_min: float | int | None = None,
) -> dict[str, Any]:
    """Approximate round-trip execution costs used in scoring/params/outcomes.

    Funding is direction-sensitive and event-based. We do not pro-rate abs(rate)
    over the whole horizon because that creates fake carry costs on trades that exit
    before the next funding timestamp and gets long/short economics backwards.

    Bybit exposes funding interval per linear contract. If the interval is missing
    from collected ticker/instrument metadata, keep a conservative explicit fallback
    source in the payload so approval logic can fail closed when funding is material.
    """
    spread_bps = _finite_or_none(f.get("spread_bps"))

    if spread_bps is None:
        fallback_spread = 10.0 if venue == "linear" else 8.0
        spread_bps_used = fallback_spread
        spread_missing = True
    else:
        spread_bps_used = max(0.0, float(spread_bps))
        spread_missing = False

    fee_bps_round_trip = max(0.0, _finite_float(taker_fee_bps, 0.0)) * 2.0
    slippage_bps = max(1.0 if venue == "linear" else 0.8, spread_bps_used * (0.35 if bot_type == "futures_grid" else 0.50))

    horizon_sec = BOT_HORIZONS.get(bot_type, 0)
    fr = _finite_or_none(funding_rate)
    directional_funding_bps_per_event = 0.0
    neutral_funding_model = None
    if fr is not None:
        if direction == "long":
            directional_funding_bps_per_event = fr * 10000.0
        elif direction == "short":
            directional_funding_bps_per_event = -fr * 10000.0
        elif direction == "neutral" and bot_type == "futures_grid" and venue == "linear":
            # Neutral futures grids can accumulate either long or short inventory as
            # price moves through the range. A signed funding estimate would mark one
            # side as free carry even though the bot may end up holding the adverse
            # side. Use abs(rate) as a conservative expected cost in approvals.
            directional_funding_bps_per_event = abs(fr) * 10000.0
            neutral_funding_model = "adverse_side_for_neutral_grid"

    expected_funding_events = 0
    expected_funding_bps = 0.0
    nfts_out: int | None = None
    funding_interval_source = "not_applicable"
    interval_was_provided = not (
        funding_interval_min is None
        or (isinstance(funding_interval_min, str) and not funding_interval_min.strip())
    )
    funding_interval_value = strict_integer(funding_interval_min)
    if funding_interval_value is not None and funding_interval_value > 0:
        funding_interval_sec = max(60, int(funding_interval_value) * 60)
        funding_interval_source = "ticker_or_instrument_info"
    else:
        # A fractional/boolean/non-positive interval is not equivalent to a
        # confirmed exchange schedule.  Keep the conservative 8h fallback, but
        # distinguish malformed evidence from a genuinely missing field so the
        # audit payload cannot claim that the rounded value came from Bybit.
        funding_interval_sec = 8 * 3600
        funding_interval_source = (
            "fallback_8h_invalid_interval"
            if interval_was_provided
            else "fallback_8h_missing_interval"
        )
    valid_next_funding_ts = _safe_int_or_none(next_funding_ts)
    if valid_next_funding_ts is not None and valid_next_funding_ts <= 0:
        valid_next_funding_ts = None
    valid_now_ts = _safe_int_or_none(ts_now)
    if valid_now_ts is not None and valid_now_ts <= 0:
        valid_now_ts = None

    if venue == "linear" and fr is not None and horizon_sec > 0:
        now = int(valid_now_ts or 0)
        nfts = int(valid_next_funding_ts or 0)
        # Defensive normalization for legacy/state payloads that may still carry
        # Bybit's millisecond timestamp even if the client was already fixed.
        if nfts > 10**11:
            nfts //= 1000
        if now > 0 and nfts > 0:
            # If the stored next_funding_ts is already in the past (e.g. collector has
            # not refreshed yet after a funding event), roll it forward to the next
            # actual future event instead of charging a stale funding event immediately.
            while nfts <= now:
                nfts += funding_interval_sec
            horizon_end = now + horizon_sec
            if horizon_end >= nfts:
                expected_funding_events = 1 + max(0, (horizon_end - nfts) // funding_interval_sec)
            nfts_out = nfts
        else:
            # Without a valid next_funding_ts we cannot know whether the first
            # funding event is minutes away or almost a full interval away. A
            # futures grid can hold inventory across the boundary, so approval
            # economics must use a conservative event count instead of assuming
            # zero carry for horizons shorter than the interval.
            expected_funding_events = max(1, int(math.ceil(float(horizon_sec) / float(funding_interval_sec))))
            nfts_out = nfts if nfts > 0 else None
        expected_funding_bps = directional_funding_bps_per_event * expected_funding_events

    grid_round_trip_fee_bps = max(0.0, fee_bps_round_trip)
    one_time_market_friction_bps = max(0.0, spread_bps_used + slippage_bps)
    market_round_trip_cost_bps = grid_round_trip_fee_bps + one_time_market_friction_bps
    # Backward-compatible alias: ``execution_cost_bps`` remains the conservative
    # market round-trip estimate used for setup/terminal-exit stress. Recurring
    # grid-pair economics must use ``grid_round_trip_fee_bps`` instead.
    execution_cost_bps = market_round_trip_cost_bps
    # Conservative approval/scoring cost: funding receipts are not durable edge.
    # Keep the signed funding-aware value as a diagnostic, but any gate, score or
    # expected-RR default must use only adverse carry. Otherwise a short/long can
    # look better solely because the current funding snapshot pays it, even though
    # the rate may flip before inventory is accumulated.
    funding_cost_bps_for_approval = max(0.0, expected_funding_bps)
    net_cost_bps = execution_cost_bps + funding_cost_bps_for_approval
    signed_net_cost_bps = execution_cost_bps + expected_funding_bps

    return {
        "spread_bps": spread_bps_used,
        "spread_missing": spread_missing,
        "fee_bps_round_trip": fee_bps_round_trip,
        "grid_round_trip_fee_bps": float(grid_round_trip_fee_bps),
        "slippage_bps": float(slippage_bps),
        "one_time_market_friction_bps": float(one_time_market_friction_bps),
        "market_round_trip_cost_bps": float(market_round_trip_cost_bps),
        "execution_cost_bps": float(execution_cost_bps),
        "funding_rate": fr,
        "direction": direction,
        "directional_funding_bps_per_event": float(directional_funding_bps_per_event),
        "directional_funding_bps_interval": float(directional_funding_bps_per_event),
        # Backward-compatible alias for old consumers. For non-8h contracts
        # this is per funding event, not annualized/normalized to 8h.
        "directional_funding_bps_8h": float(directional_funding_bps_per_event),
        "neutral_funding_model": neutral_funding_model,
        "next_funding_ts": int(nfts_out) if nfts_out else None,
        "expected_funding_events": int(expected_funding_events),
        "expected_funding_bps": float(expected_funding_bps),
        "funding_interval_min": int(round(funding_interval_sec / 60.0)) if venue == "linear" else None,
        "funding_interval_source": funding_interval_source if venue == "linear" else "not_applicable",
        "funding_interval_uncertain": bool(venue == "linear" and funding_interval_source.startswith("fallback") and fr is not None),
        "funding_event_schedule_assumption": (
            "bybit_next_funding_ts"
            if (venue == "linear" and fr is not None and valid_next_funding_ts is not None)
            else ("conservative_unknown_next_funding_ts" if venue == "linear" and fr is not None else "not_applicable")
        ),
        # Canonical cost floor for scoring / RR / labels reflects execution
        # friction plus adverse funding only. Potential funding receipts stay
        # diagnostic and must not increase approval edge, score or expected RR.
        "total_cost_bps": float(execution_cost_bps),
        "funding_cost_bps_for_approval": float(funding_cost_bps_for_approval),
        "signed_net_cost_bps": float(signed_net_cost_bps),
        "net_cost_bps": float(net_cost_bps),
        "horizon_sec": int(horizon_sec),
    }


def _funding_score_adjustment(direction: str, fr_sig: dict[str, Any], cost_model: dict[str, Any]) -> float:
    """Event-aware funding adjustment for the heuristic score.

    Funding should only affect the score if the trade horizon is actually expected to
    cross one or more funding events. Otherwise we create an economic signal that the
    execution model never realises. The adjustment is also direction-aware: expensive
    carry penalises the side that is expected to *pay* funding. Received funding is
    intentionally not rewarded because it can flip or disappear before inventory is
    accumulated; it is displayed only as a diagnostic.
    """
    if direction not in ("long", "short"):
        return 0.0
    if int(cost_model.get("expected_funding_events") or 0) <= 0:
        return 0.0

    expected_bps = float(cost_model.get("expected_funding_bps") or 0.0)
    if expected_bps >= 8.0:
        return -0.08
    if expected_bps >= 4.0:
        return -0.05
    if expected_bps >= 1.5:
        return -0.02
    # Funding receipt is not a reliable alpha source for grid approval. Do not add
    # a positive score adjustment for negative expected funding or for a bullish /
    # bearish funding signal; the UI/risk report still exposes the signed carry.
    return 0.0


def _extreme_funding_block(direction: str, fr_sig: dict[str, Any], cost_model: dict[str, Any]) -> dict[str, Any] | None:
    """Return a feasibility block when expected funding carry is too expensive.

    `expected_funding_bps` is already direction-aware:
      * positive  -> this side is expected to PAY funding over the label horizon;
      * negative  -> this side is expected to RECEIVE funding.

    The previous implementation hard-blocked only expensive longs, which created a
    directional asymmetry: positive funding could suppress many long ideas, while an
    equally expensive short under negative funding was still allowed through. The gate
    must be keyed off *who pays*, not off the semantic label long/short.
    """
    if direction not in ("long", "short"):
        return None
    if fr_sig.get("value") is None:
        return None
    if int(cost_model.get("expected_funding_events") or 0) <= 0:
        return None

    expected_bps = float(cost_model.get("expected_funding_bps") or 0.0)
    if expected_bps < 6.0:
        return None

    side = "long" if direction == "long" else "short"
    return {
        "code": "FUNDING_EXTREME",
        "msg": f"expected_funding_bps={expected_bps:.2f} over horizon — {side} pays too much funding carry",
    }


def _build_feature_snapshot(
    score: float,
    atr_pct: float,
    effective_sent: float,
    cost_model: dict[str, Any],
    direction_agg: dict[str, Any],
    oi_sig: dict[str, Any],
    liq_tier: str,
    beta_info: dict[str, Any],
    direction: str = "",
) -> dict[str, float]:
    def _value_or_default(value: Any, default: float) -> float:
        return float(default if value is None else value)

    liq_map = {"micro": 0.0, "low": 0.33, "medium": 0.67, "high": 1.0, "unknown": 0.5}
    trendiness = abs(float(direction_agg.get("trendiness") or 0.0))
    dir_conf = direction_agg.get("direction_confidence_feature")
    if dir_conf is None:
        dir_conf = direction_agg.get("direction_confidence")
    spread_bps = cost_model.get("spread_bps")
    if spread_bps is None:
        spread_bps = cost_model.get("execution_cost_bps") or cost_model.get("total_cost_bps")
    direction_norm = str(direction or direction_agg.get("direction") or "").strip().lower()
    direction_sign = 1.0 if direction_norm == "long" else (-1.0 if direction_norm == "short" else 0.0)
    sentiment_alignment = (
        direction_sign * float(effective_sent)
        if direction_sign
        else -abs(float(effective_sent))
    )
    return {
        "range_score": _clamp(
            0.35 * (1.0 - trendiness)
            + 0.65 * _value_or_default(direction_agg.get("mean_reversion_score"), 0.0)
            if direction_agg.get("mean_reversion_evidence_valid") is True
            else 1.0 - trendiness,
            0.0,
            1.0,
        ),
        "mean_reversion_score": _clamp(_value_or_default(direction_agg.get("mean_reversion_score"), 0.0), 0.0, 1.0),
        "mean_reversion_evidence_valid": 1.0 if direction_agg.get("mean_reversion_evidence_valid") is True else 0.0,
        "trend_strength": _clamp(trendiness, 0.0, 1.0),
        "atr_pct_norm": _clamp(float(atr_pct) / 0.10, 0.0, 2.0),
        "effective_sentiment": _clamp(float(effective_sent), -1.0, 1.0),
        "dir_conf": _clamp(_value_or_default(dir_conf, 0.5), 0.0, 1.0),
        "coherence": _clamp(_value_or_default(direction_agg.get("coherence"), 0.5), 0.0, 1.0),
        "spread_bps_norm": _clamp(_value_or_default(spread_bps, 8.0) / 10.0, 0.0, 5.0),
        "score": _clamp(float(score), -1.0, 1.0),
        "oi_4h_norm": _clamp(_value_or_default(oi_sig.get("oi_4h_chg_pct"), 0.0) / 10.0, -3.0, 3.0),
        # Calibration/features should see only the adverse funding burden used for
        # approvals. A signed funding receipt is diagnostic UI data, not durable
        # alpha, and must not make a grid candidate look less costly.
        "funding_cost_norm": _clamp(_value_or_default(cost_model.get("funding_cost_bps_for_approval"), max(0.0, _value_or_default(cost_model.get("expected_funding_bps"), 0.0))) / 20.0, 0.0, 2.0),
        # Backward-compatible feature name. Now also canonicalized to adverse cost
        # only, so negative funding does not improve the feature vector.
        "funding_norm": _clamp(_value_or_default(cost_model.get("funding_cost_bps_for_approval"), max(0.0, _value_or_default(cost_model.get("expected_funding_bps"), 0.0))) / 20.0, 0.0, 2.0),
        "liq_tier_num": float(liq_map.get(str(liq_tier).lower(), 0.67)),
        "btc_corr": _clamp(_value_or_default(beta_info.get("correlation"), 0.0), -1.0, 1.0),
        "regime_conf": _clamp(_value_or_default(direction_agg.get("regime_confidence"), 0.5), 0.0, 1.0),
        "direction_sign": float(direction_sign),
        "sentiment_alignment": _clamp(float(sentiment_alignment), -1.0, 1.0),
    }


def _fallback_order_qty_for_linear_grid(price: float, target_notional_usdt: float = 25.0) -> tuple[float, float, dict[str, Any]]:
    """Return a provisional target-notional size before Bybit filters are known.

    A recommendation-time estimate must not invent a quantity step or silently
    increase exposure to satisfy an assumed minimum. The raw quantity therefore
    remains ``target_notional / price``. Execution preflight later aligns it
    *down* to the live ``qtyStep`` and blocks the plan if the result is below
    ``minOrderQty`` or ``minNotionalValue``.
    """
    px = max(0.0, float(price or 0.0))
    target = max(0.0, float(target_notional_usdt or 0.0))
    if px <= 0.0 or target <= 0.0:
        return 0.0, 0.0, {"mode": "invalid_price_or_target"}
    provisional_qty = target / px
    return provisional_qty, target, {
        "mode": "provisional_target_notional_until_bybit_preflight",
        "target_notional_usdt": float(target),
        "actual_bybit_filters_required": True,
        "qty_rounding_policy": "down_only_at_live_preflight; block_if_below_exchange_minimum",
    }

def _build_trade_plan(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    direction: str,
    params: dict[str, Any],
    cost_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Human/actionable execution guide shown in the UI 'Details' panel."""

    price_raw = _finite_or_none(f.get("price"))
    price = price_raw if (price_raw is not None and price_raw > 0) else None
    atr_pct_1m = max(0.0, _finite_float(f.get("atr_pct"), 0.0))
    atr_pct_15m = max(0.0, _finite_float(f.get("_atr_pct_15m"), 0.0))
    atr_pct_1h = max(0.0, _finite_float(f.get("_atr_pct_1h"), 0.0))
    atr_pct_4h = max(0.0, _finite_float(f.get("_atr_pct_4h"), 0.0))
    atr_pct_slow = atr_pct_1h if atr_pct_1h > 0 else atr_pct_1m
    atr_source = "1h" if atr_pct_1h > 0 else "1m"

    atr_abs_used = (price * atr_pct_slow) if (price is not None and atr_pct_slow > 0) else None

    if bot_type == "directional_trend":
        if _is_rejected_trend_evaluation_params(params):
            return {}
        take_profit = _finite_or_none(params.get("take_profit_price"))
        stop_loss = _finite_or_none(params.get("stop_loss_price"))
        direction_norm = str(direction or direction_agg.get("direction") or "").strip().lower()
        geometry_valid = bool(
            price is not None
            and take_profit is not None
            and stop_loss is not None
            and (
                (direction_norm == "long" and stop_loss < price < take_profit)
                or (direction_norm == "short" and take_profit < price < stop_loss)
            )
        )
        entry_side = "Buy" if direction_norm == "long" else "Sell"
        exit_side = "Sell" if direction_norm == "long" else "Buy"
        sizing = dict(params.get("sizing") or {})
        return {
            "strategy_family": "directional_trend",
            "strategy_contract_version": TREND_STRATEGY_CONTRACT_VERSION,
            "outcome_label_version": TREND_OUTCOME_LABEL_VERSION,
            "reference_price": _round_price(price, decimals=10),
            "direction": direction_norm,
            "entry_model": "single_position_no_pyramiding",
            "averaging_allowed": False,
            "pyramiding_allowed": False,
            "decision_timeframes": {"macro": "4h/1h", "entry": "15m", "monitor": "1m"},
            "expected_horizon": {
                "min_hours": 3,
                "max_hours": int(BOT_HORIZONS.get("directional_trend", 12 * 3600) // 3600),
                "label_horizon_hours": int(BOT_HORIZONS.get("directional_trend", 12 * 3600) // 3600),
                "basis": "fixed_directional_trend_target_v1",
            },
            "label_horizon_hours": int(BOT_HORIZONS.get("directional_trend", 12 * 3600) // 3600),
            "levels": {
                "take_profit": {
                    "price": _round_price(take_profit, decimals=10),
                    "pct": _round_price(params.get("take_profit_pct"), decimals=4),
                },
                "stop_loss": {
                    "price": _round_price(stop_loss, decimals=10),
                    "pct": _round_price(params.get("stop_loss_pct"), decimals=4),
                },
            },
            "sizing": sizing,
            "economics": dict(params.get("economics") or {}),
            "geometry_valid": geometry_valid,
            "external_execution_package": {
                "schema_version": "directional-single-order-package-v1",
                "recommendation_only": True,
                "exchange_order_submitted": False,
                "requires_external_executor_or_manual_operator": True,
                "category": "linear",
                "symbol": str(params.get("symbol") or ""),
                "account_mode": "unified",
                "margin_mode": "isolated",
                "leverage": params.get("leverage"),
                "entry": {
                    "side": entry_side,
                    "order_type": "MarketOrProtectedLimit",
                    "reference_price": _round_price(price, decimals=10),
                    "qty": _round_price(sizing.get("qty"), decimals=12),
                    "target_notional_usdt": _round_price(sizing.get("target_notional_usdt"), decimals=8),
                    "reduce_only": False,
                },
                "exit": {
                    "side": exit_side,
                    "take_profit_price": _round_price(take_profit, decimals=10),
                    "stop_loss_price": _round_price(stop_loss, decimals=10),
                    "reduce_only": True,
                },
            },
            "close_conditions": [
                "Первое однозначно наблюдаемое касание take_profit или stop_loss.",
                "Истечение 12-часового label horizon с закрытием по boundary open.",
                "Никакого усреднения или добавления позиции против движения.",
            ],
            "notes": (
                "Recommendation-only single-position contract. Сервис не отправляет Bybit order; "
                "оператор или внешний execution-layer использует проверенный package и затем "
                "фиксирует фактическое исполнение в audit lifecycle."
            ),
        }

    decision_tfs = {"macro": "1h", "entry": "15m", "monitor": "1m"}
    horizon = {"min_hours": 6, "max_hours": 48}

    d = f.get("_direction_agg") or {}
    regime = str(d.get("regime") or "unknown")
    regime_conf = _finite_float(d.get("regime_confidence"), 0.0)
    if regime_conf >= 0.75:
        horizon = {"min_hours": max(1, int(horizon["min_hours"] * 0.8)), "max_hours": int(horizon["max_hours"] * 0.85)}
    elif regime_conf <= 0.35:
        horizon = {"min_hours": int(horizon["min_hours"] * 1.0), "max_hours": int(horizon["max_hours"] * 0.6)}

    lower = _finite_or_none(params.get("price_range_lower"))
    if lower is not None and lower <= 0:
        lower = None
    upper = _finite_or_none(params.get("price_range_upper"))
    if upper is not None and upper <= 0:
        upper = None
    ks_pad = (0.6 * atr_abs_used) if (atr_abs_used is not None and atr_abs_used > 0) else None
    lower_ks = (lower - ks_pad) if (lower is not None and ks_pad is not None) else None
    upper_ks = (upper + ks_pad) if (upper is not None and ks_pad is not None) else None

    step_pct = _finite_or_none(params.get("grid_spacing_pct"))
    if step_pct is not None and step_pct <= 0:
        step_pct = None
    step_abs = (price * step_pct / 100.0) if (price is not None and step_pct is not None) else None
    # In an arithmetic futures grid, one completed pair spans two adjacent grid
    # prices. The per-leg TP/reference distance therefore equals the executable
    # grid interval; 70% was a heuristic capture haircut, not exchange geometry.
    tp_leg_abs = step_abs if step_abs is not None else (0.25 * atr_abs_used if atr_abs_used else None)
    grid_count_resolution = resolve_integer_aliases([
        ("params.grid_count", params.get("grid_count")),
        ("params.grid_levels", params.get("grid_levels")),
    ])
    grid_count = grid_count_resolution.get("value") if grid_count_resolution.get("ok") else None

    plan: dict[str, Any] = {
        "reference_price": _round_price(price, decimals=10),
        "decision_timeframes": decision_tfs,
        "expected_horizon": {**horizon, "basis": "heuristics(bot_type)+regime_confidence", "label_horizon_hours": max(1, int(_safe_int_or_none(params.get("label_horizon_hours")) or (BOT_HORIZONS.get(bot_type, 12 * 3600) // 3600)))},
        "volatility": {
            "atr_pct_1m": atr_pct_1m,
            "atr_pct_15m": atr_pct_15m if atr_pct_15m > 0 else None,
            "atr_pct_1h": atr_pct_1h if atr_pct_1h > 0 else None,
            "atr_pct_4h": atr_pct_4h if atr_pct_4h > 0 else None,
            "atr_pct_used": atr_pct_slow,
            "atr_abs_used": _round_price(atr_abs_used, decimals=10) if atr_abs_used is not None else None,
            "atr_source": atr_source,
        },
        "regime": {
            "name": regime,
            "confidence": _round_price(regime_conf, decimals=4),
            "trendiness": _round_price(_finite_or_none(d.get("trendiness")), decimals=4),
            "coherence": _round_price(_finite_or_none(d.get("coherence")), decimals=4),
        },
        "bot_type": bot_type,
        "venue": venue,
        "direction": direction,
        "account_mode": "unified" if venue == "linear" else venue,
        "margin_mode": "cross" if venue == "linear" else "default",
        "position_mode": "one_way" if venue == "linear" else "default",
        "cost_model": _sanitize_json_numbers(dict(cost_model or {})),
        "sizing": _sanitize_json_numbers(dict(params.get("sizing") or {})),
        "economics": _sanitize_json_numbers(dict(params.get("economics") or {})),
        "grid_type": str(params.get("grid_type") or "arithmetic"),
        "grid_count": int(grid_count) if grid_count is not None else 0,
        "levels": {
            "range": {
                "lower": _round_price(float(lower), decimals=10) if lower is not None else None,
                "upper": _round_price(float(upper), decimals=10) if upper is not None else None,
            },
            "kill_switch": {
                "lower": _round_price(lower_ks, decimals=10),
                "upper": _round_price(upper_ks, decimals=10),
                "pad_abs": _round_price(ks_pad, decimals=10),
                "comment": "Если цена выходит за kill_switch — сетку лучше остановить (признак пробоя диапазона).",
            },
            "grid_step": {
                "step_pct": float(step_pct) if step_pct is not None else None,
                "step_abs": _round_price(step_abs, decimals=10),
                "comment": "Рекомендованный шаг сетки (ориентир).",
            },
            "tp_per_leg": {
                "abs": _round_price(tp_leg_abs, decimals=10),
                "pct": _round_price((tp_leg_abs / price * 100.0) if (tp_leg_abs is not None and price) else None, decimals=4),
                "comment": "Расстояние между соседними уровнями arithmetic-grid; завершённая пара использует полный интервал.",
            },
        },
        "close_conditions": [
            "Выход цены за kill_switch (признак пробоя диапазона).",
            "Истечение expected_horizon.max_hours без возврата в диапазон/без набора прибыли.",
            "Рост trendiness/regime='trend' (по direction_agg) — сетку лучше остановить.",
        ],
        "notes": "Ориентиры уровней масштабируются по ATR старшего ТФ (предпочтительно 1h, fallback = 1m). Это подсказка для запуска/контроля бота, а не обещание результата.",
    }

    leverage = max(1, int(_safe_int_or_none(params.get("leverage")) or 1))
    if venue == "linear" and leverage > 1:
        ks = plan["levels"].get("kill_switch") or {}
        span_note = _finite_or_none(params.get("range_span_pct_total"))
        span_str = f"{span_note:.2f}" if span_note is not None else "n/a"
        plan["notes"] += (
            f" Для futures_grid с leverage={leverage} и span≈{span_str}% проверьте cross-margin equity stress на kill_switch "
            f"[{ks.get('lower')}, {ks.get('upper')}]; одиночная isolated liquidation price к Bybit Grid Bot неприменима."
        )

    return plan


def _clamp(x: float, lo: float, hi: float) -> float:
    """Безопасный clamp без превращения NaN в «идеальный» bound.

    Обычная конструкция max(lo, min(hi, x)) плохо ведёт себя на NaN: для диапазона
    [0, 1] она возвращает 1.0, то есть испорченное значение может незаметно стать
    максимально «хорошим». Для рекомендаций это опасно: NaN в confidence/score не
    должен эскалироваться в bullish signal. Поэтому NaN уводим в нейтральный ноль,
    если ноль лежит внутри диапазона; иначе — в нижнюю границу.
    """
    if isinstance(x, bool):
        num = float('nan')
    else:
        try:
            num = float(x)
        except Exception:
            num = float('nan')
    if math.isnan(num):
        neutral = 0.0 if float(lo) <= 0.0 <= float(hi) else float(lo)
        return float(neutral)
    if num <= float(lo):
        return float(lo)
    if num >= float(hi):
        return float(hi)
    return float(num)

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def _make_factor(feature: str, value: Any, weight: float, msg: str) -> dict[str, Any]:
    return {
        "feature": feature,
        "value": None if isinstance(value, bool) else (float(value) if isinstance(value, (int, float)) and value is not None else value),
        "weight": float(weight),
        "msg": msg,
        "text": msg,
    }


def _direction(bot_type: str, agg: dict[str, Any]) -> str:
    raw_direction = str((agg or {}).get("direction") or "neutral").lower()
    if bot_type == "futures_grid" and raw_direction in ("long", "short", "neutral"):
        return raw_direction
    if bot_type == "directional_trend" and raw_direction in ("long", "short"):
        return raw_direction
    return "neutral"


def _is_rejected_trend_evaluation_params(params: Any) -> bool:
    return bool(
        isinstance(params, dict)
        and str(params.get("candidate_kind") or "").strip().lower()
        == TREND_EVALUATION_REJECTED_KIND
    )


def _stable_range_score(f: dict[str, Any], agg: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    raw_range = _clamp(_finite_float(f.get("range_score"), 0.0), 0.0, 1.0)
    trendiness_raw = (agg or {}).get("trendiness")
    if trendiness_raw is None:
        trendiness_raw = f.get("trend_strength")
    trendiness = _clamp(_finite_float(trendiness_raw, 0.0), 0.0, 1.0)
    coherence = _clamp(_finite_float((agg or {}).get("coherence"), 0.5), 0.0, 1.0)
    regime = str((agg or {}).get("regime") or "unknown")

    absence_of_trend = _clamp(1.0 - trendiness, 0.0, 1.0)
    if regime == "range":
        absence_of_trend = _clamp(absence_of_trend + 0.06 * coherence, 0.0, 1.0)
    elif regime == "trend":
        absence_of_trend = _clamp(absence_of_trend - 0.04 * max(0.0, coherence - 0.5), 0.0, 1.0)

    mean_reversion_raw = (agg or {}).get("mean_reversion_score")
    mean_reversion_score = _clamp(_finite_float(mean_reversion_raw, 0.0), 0.0, 1.0)
    mean_reversion_valid = bool((agg or {}).get("mean_reversion_evidence_valid") is True)
    if mean_reversion_valid:
        # Independent oscillation evidence must dominate the grid suitability metric.
        # A near-zero trend alone describes both ranges and martingales and therefore
        # cannot be treated as positive edge after transaction costs.
        stable = _clamp(
            0.15 * raw_range + 0.30 * absence_of_trend + 0.55 * mean_reversion_score,
            0.0,
            1.0,
        )
        range_model = "trend_absence_plus_mean_reversion"
    else:
        # Legacy/manual payloads retain diagnostic compatibility, while the production
        # publication gate below blocks execution when this evidence is absent.
        stable = _clamp(0.20 * raw_range + 0.80 * absence_of_trend, 0.0, 1.0)
        range_model = "legacy_trend_absence_only_unconfirmed"
    return stable, {
        "raw_range_score_1m": float(raw_range),
        "multi_tf_range_score": float(absence_of_trend),
        "stable_range_score": float(stable),
        "absence_of_trend_score": float(absence_of_trend),
        "mean_reversion_score": float(mean_reversion_score),
        "mean_reversion_evidence_valid": mean_reversion_valid,
        "mean_reversion_tf_count": int(_safe_int_or_none((agg or {}).get("mean_reversion_tf_count")) or 0),
        "mean_reversion_tf_coverage": _clamp(_finite_float((agg or {}).get("mean_reversion_tf_coverage"), 0.0), 0.0, 1.0),
        "range_model": range_model,
        "trendiness": float(trendiness),
        "coherence": float(coherence),
        "regime": regime,
    }


MEAN_REVERSION_MIN_SCORE_DEFAULT = 0.25


def _mean_reversion_grid_blocks(
    range_meta: dict[str, Any],
    min_score: float = MEAN_REVERSION_MIN_SCORE_DEFAULT,
) -> list[dict[str, str]]:
    """Classify independent oscillation evidence for the grid publication gate.

    Missing evidence is a hard data-quality block. A valid but weak score is a
    strategy ``no_trade`` decision, not proof of negative expectancy. The score
    floor is only a selective candidate screen; the separate retained-outcome
    monetary gate remains the authority for positive/negative proxy expectancy.
    """
    meta = dict(range_meta or {})
    valid = bool(meta.get("mean_reversion_evidence_valid") is True)
    score = _clamp(_finite_float(meta.get("mean_reversion_score"), 0.0), 0.0, 1.0)
    threshold = _clamp(
        _finite_float(min_score, MEAN_REVERSION_MIN_SCORE_DEFAULT),
        0.0,
        1.0,
    )
    tf_count = int(_safe_int_or_none(meta.get("mean_reversion_tf_count")) or 0)
    if not valid or tf_count < 3:
        return [{
            "code": "MEAN_REVERSION_EVIDENCE_INSUFFICIENT",
            "decision": "blocked",
            "msg": (
                f"mean-reversion evidence доступен только на {tf_count} timeframes; "
                "отсутствие тренда не считается самостоятельным grid edge"
            ),
        }]
    if score < threshold:
        return [{
            "code": "MEAN_REVERSION_EDGE_UNCONFIRMED",
            "decision": "no_trade",
            "msg": (
                f"mean_reversion_score={score:.2f} < configured candidate floor={threshold:.2f}; "
                "повторяемая anti-persistence недостаточно выражена, а положительное monetary "
                "expectancy не доказано до отдельной проверки matured proxy outcomes"
            ),
        }]
    return []


def _score(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    taker_fee_bps: float,
    global_sent: float,
    cost_model: dict[str, Any] | None = None,
    sentiment_has_data: bool = True,
) -> tuple[float, float, dict[str, Any]]:
    cost_model = dict(cost_model or {})
    agg = dict(f.get("_direction_agg") or {})
    direction = _direction(bot_type, agg)

    def _num(value: Any, default: float = 0.0) -> float:
        return _finite_float(value, default)

    strengths = agg.get("strength") or {}
    if isinstance(strengths, dict):
        direction_strength = abs(_num(strengths.get("all"), 0.0))
    else:
        direction_strength = abs(_num(strengths, 0.0))

    range_score, range_meta = _stable_range_score(f, agg)
    trend_strength = _clamp(_num(agg.get("trendiness"), _num(f.get("trend_strength"), 0.0)), 0.0, 1.0)
    coherence = _clamp(_num(agg.get("coherence"), 0.5), 0.0, 1.0)
    regime_conf = _clamp(_num(agg.get("regime_confidence"), 0.0), 0.0, 1.0)
    atr_pct = max(0.0, _num(f.get("_atr_pct_1h"), _num(f.get("atr_pct"), 0.0)))
    atr_penalty = _clamp(atr_pct / 0.06, 0.0, 2.0)
    effective_sent = _clamp(_num(global_sent, 0.0), -1.0, 1.0)
    spread = max(0.0, _num(cost_model.get("spread_bps"), _num(f.get("spread_bps"), 0.0)))
    execution_cost_bps = max(
        0.0,
        _num(
            cost_model.get("execution_cost_bps"),
            _num(cost_model.get("total_cost_bps"), max(0.0, spread + 2.0 * float(taker_fee_bps))),
        ),
    )
    adverse_funding_cost_bps = max(
        0.0,
        _num(
            cost_model.get("funding_cost_bps_for_approval"),
            max(0.0, _num(cost_model.get("expected_funding_bps"), 0.0)),
        ),
    )
    economic_cost_bps = max(
        execution_cost_bps + adverse_funding_cost_bps,
        _num(cost_model.get("net_cost_bps"), 0.0),
    )
    cost_penalty = _clamp(economic_cost_bps / 20.0, 0.0, 2.5)

    pos: list[dict[str, Any]] = []
    neg: list[dict[str, Any]] = []

    def add_pos(feature: str, value: Any, weight: float, msg: str) -> None:
        pos.append(_make_factor(feature, value, weight, msg))

    def add_neg(feature: str, value: Any, weight: float, msg: str) -> None:
        neg.append(_make_factor(feature, value, weight, msg))

    raw = 0.0
    summary = "Неизвестная стратегия не должна получать положительный score."
    if bot_type == "futures_grid":
        summary = (
            "Рекомендация оценивает пригодность символа для grid-стратегии: "
            "ищется диапазонный рынок с контролируемой волатильностью, "
            "приемлемыми издержками и исполнимым bias по направлению."
        )
        raw += 1.35 * range_score
        raw += 0.22 * coherence
        raw += 0.16 * regime_conf
        raw -= 1.00 * trend_strength
        raw -= 0.75 * atr_penalty
        raw -= 0.40 * cost_penalty

        if direction == "long":
            raw += 0.12 * effective_sent
            raw += 0.10 * direction_strength
        elif direction == "short":
            raw -= 0.12 * effective_sent
            raw += 0.10 * direction_strength
        elif sentiment_has_data:
            raw += 0.05 * (1.0 - min(1.0, abs(effective_sent) * 1.5))
        else:
            raw -= 0.05

        if range_score > 0.0:
            add_pos("range_score", range_score, 1.35 * range_score, "диапазон подходит для futures grid")
        if coherence > 0.0:
            add_pos("coherence", coherence, 0.22 * coherence, "таймфреймы согласованы")
        if regime_conf > 0.0:
            add_pos("regime_confidence", regime_conf, 0.16 * regime_conf, "режим оценён с приемлемой уверенностью")
        if direction in ("long", "short") and direction_strength > 0.0:
            add_pos("direction_strength", direction_strength, 0.10 * direction_strength, "есть исполнимый directional bias для futures grid")
        if direction == "long" and effective_sent > 0.0:
            add_pos("effective_sentiment", effective_sent, 0.12 * effective_sent, "сентимент поддерживает long bias")
        elif direction == "long" and effective_sent < 0.0:
            add_neg("effective_sentiment", abs(effective_sent), 0.12 * effective_sent, "сентимент против long bias")
        elif direction == "short" and effective_sent < 0.0:
            add_pos("effective_sentiment", abs(effective_sent), 0.12 * abs(effective_sent), "сентимент поддерживает short bias")
        elif direction == "short" and effective_sent > 0.0:
            add_neg("effective_sentiment", effective_sent, -0.12 * effective_sent, "сентимент против short bias")
        elif direction == "neutral" and sentiment_has_data:
            add_pos("effective_sentiment", 1.0 - min(1.0, abs(effective_sent) * 1.5), 0.05 * (1.0 - min(1.0, abs(effective_sent) * 1.5)), "сентимент не мешает нейтральной сетке")
        elif direction == "neutral":
            add_neg("sentiment_data_availability", 0, -0.05, "нет сентимент-данных — нейтральный futures bias менее надёжен")

        if trend_strength > 0.0:
            add_neg("trend_strength", trend_strength, -1.00 * trend_strength, "сильный тренд ломает grid")
        if atr_pct > 0.0:
            add_neg("atr_pct", atr_pct, -0.75 * atr_penalty, "высокая волатильность повышает риск range break")
        if economic_cost_bps > 0.0:
            add_neg("economic_cost_bps", economic_cost_bps, -0.40 * cost_penalty, "издержки исполнения и adverse funding давят на net result")
        if adverse_funding_cost_bps > 0.0:
            add_neg("adverse_funding_cost_bps", adverse_funding_cost_bps, -0.18 * min(1.0, adverse_funding_cost_bps / 12.0), "ожидаемый funding-carry ухудшает экономику grid")
        if spread > 0.0:
            add_neg("spread_bps", spread, -0.18 * min(1.0, spread / 5.0), "спред ухудшает fills")
    elif bot_type == "directional_trend":
        summary = (
            "Shadow-рекомендация оценивает самостоятельный directional trend edge: "
            "нужны согласованный multi-timeframe тренд, подтверждённое направление, "
            "приемлемая волатильность и издержки. Mean-reversion не является gate."
        )
        direction_sign = 1.0 if direction == "long" else (-1.0 if direction == "short" else 0.0)
        signed_sentiment = direction_sign * effective_sent
        regime_is_trend = 1.0 if str(agg.get("regime") or "") == "trend" else 0.0
        range_penalty = _clamp((range_score - 0.55) / 0.45, 0.0, 1.0)

        raw += 1.20 * trend_strength
        raw += 0.72 * direction_strength
        raw += 0.52 * coherence
        raw += 0.30 * regime_conf
        raw += 0.22 * regime_is_trend
        raw += 0.12 * signed_sentiment
        raw -= 0.38 * atr_penalty
        raw -= 0.48 * cost_penalty
        raw -= 0.25 * range_penalty
        if direction == "neutral":
            raw -= 1.25

        if trend_strength > 0.0:
            add_pos("trend_strength", trend_strength, 1.20 * trend_strength, "сила тренда поддерживает directional strategy")
        if direction_strength > 0.0:
            add_pos("direction_strength", direction_strength, 0.72 * direction_strength, "направление выражено на агрегате таймфреймов")
        if coherence > 0.0:
            add_pos("coherence", coherence, 0.52 * coherence, "таймфреймы подтверждают одно направление")
        if regime_conf > 0.0:
            add_pos("regime_confidence", regime_conf, 0.30 * regime_conf, "режим trend определён уверенно")
        if regime_is_trend:
            add_pos("trend_regime", regime_is_trend, 0.22, "агрегатор классифицирует рынок как trend")
        if signed_sentiment > 0.0:
            add_pos("effective_sentiment", abs(effective_sent), 0.12 * signed_sentiment, "сентимент совпадает с направлением тренда")
        elif signed_sentiment < 0.0:
            add_neg("effective_sentiment", abs(effective_sent), 0.12 * signed_sentiment, "сентимент против направления тренда")
        if direction == "neutral":
            add_neg("direction", "neutral", -1.25, "directional trend требует явный long или short")
        if atr_pct > 0.0:
            add_neg("atr_pct", atr_pct, -0.38 * atr_penalty, "волатильность увеличивает stop distance и tail risk")
        if economic_cost_bps > 0.0:
            add_neg("economic_cost_bps", economic_cost_bps, -0.48 * cost_penalty, "издержки уменьшают directional payoff")
        if range_penalty > 0.0:
            add_neg("range_score", range_score, -0.25 * range_penalty, "выраженная диапазонность ослабляет trend thesis")
    else:
        raw = 0.0

    score = float(_clamp(raw / 2.2, -1.0, 1.0))
    conf0 = float(_clamp(_sigmoid(raw * 2.1), 0.0, 1.0))
    reasons = {
        "summary": summary,
        "top_positive_factors": sorted(pos, key=lambda x: abs(float(x.get("weight") or 0.0)), reverse=True)[:5],
        "top_negative_factors": sorted(neg, key=lambda x: abs(float(x.get("weight") or 0.0)), reverse=True)[:5],
        "cost_model": {
            **cost_model,
            "spread_bps": spread,
            "taker_fee_bps": float(taker_fee_bps),
            "execution_cost_bps": float(cost_model.get("execution_cost_bps") or execution_cost_bps),
            "adverse_funding_cost_bps": float(adverse_funding_cost_bps),
            "funding_cost_bps_for_approval": float(adverse_funding_cost_bps),
            "economic_cost_bps": float(economic_cost_bps),
            "total_cost_bps": float(cost_model.get("total_cost_bps") or execution_cost_bps),
            "net_cost_bps": float(cost_model.get("net_cost_bps") or economic_cost_bps),
        },
        "score_components": {
            "range_score": float(range_score),
            "range_score_meta": dict(range_meta),
            "trend_strength": float(trend_strength),
            "coherence": float(coherence),
            "regime_confidence": float(regime_conf),
            "atr_penalty": float(atr_penalty),
            "execution_cost_bps": float(execution_cost_bps),
            "adverse_funding_cost_bps": float(adverse_funding_cost_bps),
            "economic_cost_bps": float(economic_cost_bps),
            "cost_penalty": float(cost_penalty),
        },
        "effective_sentiment": effective_sent,
        "expected_rr_semantics": {
            "basis": "heuristic_capture_to_volatility_proxy",
            "is_trade_reward_risk": False,
            "is_profitability_evidence": False,
            "note": "The legacy expected_rr field is a bounded heuristic ranking proxy, not realised or geometric trade reward/risk.",
        },
    }
    return score, conf0, reasons


def _expected_rr(bot_type: str, f: dict[str, Any], cost_model: dict[str, Any] | None = None) -> float:
    cost_model = dict(cost_model or {})
    agg = dict(f.get("_direction_agg") or {})
    range_score, _ = _stable_range_score(f, agg)
    trendiness_raw = agg.get("trendiness")
    if trendiness_raw is None:
        trendiness_raw = f.get("trend_strength")
    trend_strength = _clamp(_finite_float(trendiness_raw, 0.0), 0.0, 1.0)
    coherence = _clamp(_finite_float(agg.get("coherence"), 0.5), 0.0, 1.0)
    atr_raw = f.get("_atr_pct_1h")
    if atr_raw is None:
        atr_raw = f.get("atr_pct")
    atr_pct = max(0.0, _finite_float(atr_raw, 0.0))
    # RR must reflect conservative approval economics: execution costs are always paid
    # and adverse funding carry can hurt the setup, but funding receipts must not raise
    # RR because they can flip before inventory is accumulated.
    net_cost_pct = float(
        cost_model.get("net_cost_bps")
        or cost_model.get("total_cost_bps")
        or cost_model.get("execution_cost_bps")
        or 0.0
    ) / 10000.0
    execution_cost_pct = max(
        0.0,
        float(cost_model.get("execution_cost_bps") or cost_model.get("total_cost_bps") or 0.0) / 10000.0,
    )

    if bot_type == "directional_trend":
        strengths = agg.get("strength") or {}
        direction_strength = _clamp(
            abs(
                _finite_float(
                    strengths.get("all") if isinstance(strengths, dict) else strengths,
                    0.0,
                )
            ),
            0.0,
            1.0,
        )
        gross_capture = max(
            0.0,
            (1.15 * trend_strength + 0.55 * direction_strength + 0.30 * coherence)
            * max(atr_pct, 0.0025),
        )
        net_capture = gross_capture - net_cost_pct
        risk_proxy = max(max(atr_pct, 0.0025) * 1.25, execution_cost_pct * 2.0, 1e-6)
        return float(_clamp(net_capture / risk_proxy, 0.0, 3.0))

    gross_capture = max(0.0, (0.55 * range_score + 0.15 * coherence - 0.20 * trend_strength) * max(atr_pct, 0.0025))
    net_capture = gross_capture - net_cost_pct
    risk_proxy = max(max(atr_pct, 0.0025) * 1.5, execution_cost_pct * 2.0, 1e-6)
    return float(_clamp(net_capture / risk_proxy, 0.0, 3.0))


def _plan_rr_metrics(
    params: dict[str, Any] | None,
    cost_model: dict[str, Any] | None,
) -> dict[str, Any]:
    """Scenario reward/risk for the concrete generated grid plan.

    Reward is the projected net cash result from the plan's executable grid
    opportunities over the recommendation horizon. It uses the same per-pair
    fee model as the published grid economics, then subtracts one-time market
    entry/terminal friction and adverse funding. Risk is the monotonic
    kill-switch loss for the worst applicable inventory side, including the
    corresponding exit execution cost. Maintenance reserve is deliberately not
    called a loss, and no funding benefit or historical outcome is credited.
    """
    params = params if isinstance(params, dict) else {}
    economics = params.get("economics") if isinstance(params.get("economics"), dict) else {}
    cost_model = cost_model if isinstance(cost_model, dict) else {}

    if str(params.get("strategy_family") or "") == "directional_trend":
        reward_bps = _finite_or_none(economics.get("projected_net_reward_bps"))
        risk_bps = _finite_or_none(economics.get("projected_stop_loss_bps"))
        target_notional = _finite_or_none((params.get("sizing") or {}).get("target_notional_usdt")) if isinstance(params.get("sizing"), dict) else None
        if reward_bps is None or risk_bps is None or risk_bps <= 0.0:
            return {
                "status": "unavailable",
                "rr": None,
                "projected_net_reward_usdt": None,
                "kill_switch_loss_usdt": None,
                "basis": "generated_directional_trend_single_position_plan",
                "reason": "incomplete_directional_trend_economics",
                "is_empirical": False,
                "is_heuristic_capture_score": False,
            }
        rr = max(0.0, float(reward_bps)) / float(risk_bps)
        reward_usdt = None
        risk_usdt = None
        if target_notional is not None and target_notional > 0.0:
            reward_usdt = float(target_notional) * float(reward_bps) / 10_000.0
            risk_usdt = float(target_notional) * float(risk_bps) / 10_000.0
        return {
            "status": "available",
            "rr": float(rr),
            "projected_net_reward_usdt": reward_usdt,
            "kill_switch_loss_usdt": risk_usdt,
            "basis": "projected_net_directional_reward_to_stop_loss",
            "is_empirical": False,
            "is_heuristic_capture_score": False,
            "note": "Shadow plan geometry only; no live execution evidence is implied.",
        }

    def first_finite(*values: Any) -> float | None:
        for value in values:
            parsed = _finite_or_none(value)
            if parsed is not None:
                return parsed
        return None

    stress = economics.get("cross_margin_stress") if isinstance(economics.get("cross_margin_stress"), dict) else {}
    worst_side = str(stress.get("worst_side") or "").strip().lower()
    worst = stress.get(worst_side) if worst_side in {"long", "short"} else None
    worst = worst if isinstance(worst, dict) else {}

    qty = _finite_or_none(economics.get("qty_per_order"))
    active_orders = _safe_int_or_none(economics.get("estimated_active_orders"))
    fill_efficiency = _finite_or_none(economics.get("fill_efficiency"))
    net_profit_per_pair = _finite_or_none(economics.get("net_profit_usdt"))
    max_position_notional = first_finite(
        economics.get("estimated_worst_case_total_order_notional_usdt"),
        economics.get("estimated_max_position_notional_usdt"),
    )
    gross_kill_loss_per_qty = _finite_or_none(worst.get("gross_loss"))
    kill_execution_cost_per_qty = _finite_or_none(worst.get("execution_cost"))
    # Recurring grid-pair fees are already deducted in ``net_profit_usdt``.
    # Only the distinct one-time spread/slippage layer may be deducted again
    # here; using ``market_round_trip_cost_bps`` would double-count one pair of
    # trading fees. Current generated plans always persist this explicit field.
    one_time_market_friction_bps = _finite_or_none(
        cost_model.get("one_time_market_friction_bps")
    )
    signed_funding_bps = _finite_or_none(cost_model.get("expected_funding_bps"))

    required = (
        qty,
        fill_efficiency,
        net_profit_per_pair,
        max_position_notional,
        gross_kill_loss_per_qty,
        kill_execution_cost_per_qty,
        one_time_market_friction_bps,
        signed_funding_bps,
    )
    if (
        any(value is None for value in required)
        or qty is None
        or qty <= 0.0
        or active_orders is None
        or active_orders <= 0
        or max_position_notional is None
        or max_position_notional <= 0.0
    ):
        return {
            "status": "unavailable",
            "rr": None,
            "projected_net_reward_usdt": None,
            "kill_switch_loss_usdt": None,
            "projected_completed_pairs": None,
            "basis": "generated_grid_plan",
            "reason": "incomplete_plan_economics_or_kill_switch_stress",
            "is_empirical": False,
            "is_heuristic_capture_score": False,
        }

    fill_eff = _clamp(float(fill_efficiency), 0.0, 1.0)
    projected_pairs = float(active_orders) * fill_eff
    recurring_grid_reward = max(0.0, float(net_profit_per_pair)) * projected_pairs
    one_time_execution_cost = (
        float(max_position_notional)
        * max(0.0, float(one_time_market_friction_bps))
        / 10000.0
    )
    adverse_funding_cost = (
        float(max_position_notional)
        * max(0.0, float(signed_funding_bps))
        / 10000.0
    )
    projected_net_reward = recurring_grid_reward - one_time_execution_cost - adverse_funding_cost
    kill_switch_loss = float(qty) * (
        max(0.0, float(gross_kill_loss_per_qty))
        + max(0.0, float(kill_execution_cost_per_qty))
    )
    rr = None
    if kill_switch_loss > 0.0:
        rr = max(0.0, projected_net_reward) / kill_switch_loss

    return {
        "status": "available" if rr is not None else "unavailable",
        "rr": float(rr) if rr is not None and math.isfinite(rr) else None,
        "projected_net_reward_usdt": float(projected_net_reward),
        "projected_grid_reward_before_horizon_costs_usdt": float(recurring_grid_reward),
        "one_time_execution_cost_usdt": float(one_time_execution_cost),
        "one_time_market_friction_bps": float(one_time_market_friction_bps),
        "adverse_funding_cost_usdt": float(adverse_funding_cost),
        "kill_switch_loss_usdt": float(kill_switch_loss),
        "projected_completed_pairs": float(projected_pairs),
        "fill_efficiency": float(fill_eff),
        "worst_side": worst_side or None,
        "basis": "projected_net_grid_reward_to_monotonic_kill_switch_loss",
        "is_empirical": False,
        "is_heuristic_capture_score": False,
        "note": (
            "Scenario metric for the generated plan, not a probability forecast: "
            "projected completed grid pairs net of recurring fees, one-time market friction "
            "and adverse funding divided by price/exit loss at the worst kill-switch side."
        ),
    }


def _empirical_expectancy_metrics(model: LogRegScaler | None) -> dict[str, Any]:
    """Current-policy matured-outcome expectancy with uncertainty and tail risk."""
    if model is None:
        return {
            "status": "insufficient",
            "available": False,
            "decision_ready": False,
            "mean_return": None,
            "confidence_interval": {"lower": None, "upper": None, "level": 0.95},
            "expected_shortfall": None,
            "empirical_rr": None,
            "return_samples": 0,
            "temporal_cluster_count": 0,
            "policy_fingerprint": None,
            "basis": "current_policy_matured_outcomes",
        }

    confidence_level = _finite_or_none(getattr(model, "expectancy_confidence_level", None)) or 0.95
    temporal_mean = _finite_or_none(getattr(model, "weighted_temporal_mean_return", None))
    temporal_std = _finite_or_none(getattr(model, "weighted_temporal_return_std", None))
    temporal_eff = _finite_or_none(getattr(model, "weighted_effective_temporal_clusters", None))
    mean_return = temporal_mean
    return_std = temporal_std
    effective_samples = temporal_eff
    mean_basis = "non_overlapping_temporal_cohorts"
    if mean_return is None or return_std is None or effective_samples is None or effective_samples <= 1.0:
        mean_return = _finite_or_none(getattr(model, "weighted_mean_return", None))
        return_std = _finite_or_none(getattr(model, "weighted_return_std", None))
        effective_samples = _finite_or_none(getattr(model, "weighted_effective_return_samples", None))
        mean_basis = "recency_weighted_outcomes"

    ci_lower, ci_upper = return_confidence_interval(
        mean_return,
        return_std,
        effective_samples,
        confidence_level=float(confidence_level),
    )
    expected_shortfall = _finite_or_none(getattr(model, "weighted_expected_shortfall", None))
    empirical_rr = None
    if mean_return is not None and mean_return > 0.0 and expected_shortfall is not None and expected_shortfall < 0.0:
        empirical_rr = float(mean_return) / abs(float(expected_shortfall))

    gate_status = str(
        getattr(model, "expectancy_status", "insufficient") or "insufficient"
    ).strip().lower()
    if ci_lower is not None and ci_upper is not None:
        if ci_lower > 0.0:
            status = "positive"
        elif ci_upper < 0.0:
            status = "negative"
        else:
            status = "uncertain"
    else:
        status = gate_status
    unresolved = int(getattr(model, "policy_unresolved_total", 0) or 0)
    invalid = int(getattr(model, "policy_invalid_labeled_total", 0) or 0)
    fingerprint = str(getattr(model, "policy_fingerprint", "") or "").strip().lower()
    available = mean_return is not None and ci_lower is not None and ci_upper is not None
    decision_ready = bool(
        available
        and gate_status in {"positive", "negative", "uncertain"}
        and unresolved == 0
        and invalid == 0
        and is_sha256_fingerprint(fingerprint)
    )
    return {
        "status": status,
        "gate_status": gate_status,
        "available": bool(available),
        "decision_ready": decision_ready,
        "mean_return": float(mean_return) if mean_return is not None else None,
        "mean_basis": mean_basis,
        "confidence_interval": {
            "lower": float(ci_lower) if ci_lower is not None else None,
            "upper": float(ci_upper) if ci_upper is not None else None,
            "level": float(confidence_level),
        },
        "conservative_lower_bound": min(
            value
            for value in (
                _finite_or_none(getattr(model, "weighted_mean_return_lower_bound", None)),
                _finite_or_none(getattr(model, "weighted_temporal_mean_return_lower_bound", None)),
            )
            if value is not None
        ) if any(
            value is not None
            for value in (
                _finite_or_none(getattr(model, "weighted_mean_return_lower_bound", None)),
                _finite_or_none(getattr(model, "weighted_temporal_mean_return_lower_bound", None)),
            )
        ) else None,
        "expected_shortfall": float(expected_shortfall) if expected_shortfall is not None else None,
        "empirical_rr": float(empirical_rr) if empirical_rr is not None and math.isfinite(empirical_rr) else None,
        "return_samples": int(getattr(model, "return_samples", 0) or 0),
        "effective_samples": float(effective_samples) if effective_samples is not None else None,
        "temporal_cluster_count": int(getattr(model, "temporal_cluster_count", 0) or 0),
        "minimum_temporal_clusters": int(getattr(model, "minimum_temporal_clusters", 0) or 0),
        "policy_matured_total": int(getattr(model, "policy_matured_total", 0) or 0),
        "policy_labeled_total": int(getattr(model, "policy_labeled_total", 0) or 0),
        "policy_censored_total": int(getattr(model, "policy_censored_total", 0) or 0),
        "policy_unresolved_total": unresolved,
        "policy_invalid_labeled_total": invalid,
        "policy_fingerprint": fingerprint or None,
        "basis": "current_policy_matured_outcomes",
        "is_live_edge_proof": False,
        "note": (
            "Proxy outcome evidence from the exact current policy. Confidence interval and tail loss "
            "describe retained matured outcomes; they do not prove future live execution edge."
        ),
    }


def _mode(bot_type: str, venue: str, direction: str) -> tuple[str, str]:
    if venue == "linear":
        if bot_type == "directional_trend":
            return "unified", "isolated"
        return "unified", "cross"
    return venue, "default"


def _adaptive_grid_leverage_from_quality(
    *,
    min_leverage: int,
    max_leverage: int,
    setup_quality: float,
    projected_net_bps: float,
    atr_pct: float,
    execution_cost_bps: float,
) -> tuple[int, dict[str, Any]]:
    """Return a conservative leverage inside the operator-approved interval.

    The operator interval is not a permission to always use the top leverage.
    Promotion above the minimum requires three independent conditions:
    signal/range quality, net edge after execution/funding costs, and low ATR.
    Execution/preflight liquidation and risk caps still run downstream.
    """
    min_lev = max(1, int(min_leverage or 1))
    max_lev = max(min_lev, int(max_leverage or min_lev))
    span = max(0, max_lev - min_lev)
    setup_quality = _clamp(float(setup_quality or 0.0), 0.0, 1.0)
    projected_net_bps = float(projected_net_bps or 0.0)
    atr_pct = max(0.0, float(atr_pct or 0.0))
    execution_cost_bps = max(0.0, float(execution_cost_bps or 0.0))

    # Quality is intentionally conservative: high leverage needs a setup that is
    # simultaneously good, economically thick after costs, low-volatility and not
    # execution-cost stressed. This only selects within an already-approved
    # operator interval; it never bypasses lower/higher runtime guards.
    edge_quality = _clamp((projected_net_bps - 2.0) / 8.0, 0.0, 1.0)
    volatility_quality = _clamp((0.025 - atr_pct) / 0.020, 0.0, 1.0)
    execution_quality = _clamp((45.0 - execution_cost_bps) / 30.0, 0.0, 1.0)
    adaptive_quality_score = _clamp(
        0.45 * setup_quality + 0.35 * edge_quality + 0.15 * volatility_quality + 0.05 * execution_quality,
        0.0,
        1.0,
    )

    selected = min_lev
    accepted_promotions: list[dict[str, Any]] = []
    rejected_promotions: list[dict[str, Any]] = []
    for lev in range(min_lev + 1, max_lev + 1):
        frac = (lev - min_lev) / max(1, span)
        required_quality = 0.58 + 0.22 * frac
        required_net_bps = 3.0 + 4.0 * frac
        required_atr_pct = 0.023 - 0.006 * frac
        passed = (
            adaptive_quality_score >= required_quality
            and projected_net_bps >= required_net_bps
            and atr_pct <= required_atr_pct
        )
        row = {
            "leverage": int(lev),
            "interval_fraction": float(frac),
            "required_quality_score": float(required_quality),
            "required_projected_net_profit_bps": float(required_net_bps),
            "required_max_atr_pct": float(required_atr_pct),
            "passed": bool(passed),
        }
        if passed:
            selected = lev
            accepted_promotions.append(row)
        else:
            rejected_promotions.append(row)

    return int(selected), {
        "interval_mode": "fixed" if span == 0 else "adaptive",
        "adaptive_quality_score": float(adaptive_quality_score),
        "setup_quality": float(setup_quality),
        "edge_quality": float(edge_quality),
        "volatility_quality": float(volatility_quality),
        "execution_quality": float(execution_quality),
        "accepted_leverage_promotions": accepted_promotions,
        "rejected_leverage_promotions": rejected_promotions,
    }


def _select_operator_grid_leverage(
    *,
    direction: str,
    dir_strength: float,
    range_score: float,
    trendiness: float,
    atr_pct: float,
    execution_cost_bps: float,
    funding_cost_bps: float,
    gross_profit_bps_est: float,
    min_operator_leverage: int,
    max_operator_leverage: int,
    liquidation_safe_max_leverage: int | None = None,
) -> tuple[int, str, dict[str, Any]]:
    """Choose the recommendation leverage used by the grid payload.

    The old gate used a fixed ``execution_cost_bps <= 10`` condition before
    allowing the operator minimum leverage. With the default linear taker fee
    (6 bps per side), the round-trip fee floor is already 12 bps and the cost
    model adds at least minimal slippage, so the condition was practically
    unreachable. That made every otherwise viable setup fall back to 1x and then
    get blocked by ``MIN_LEVERAGE_PER_BOT`` when the operator minimum was 3x within the 3x..5x interval.

    Leverage selection must be based on *net grid edge after costs*, not on a
    hard-coded cost ceiling that can be below the configured fee floor. When the
    operator sets an interval such as 3x..5x, the minimum remains the base
    actionable leverage and promotion toward the maximum is adaptive: higher
    leverage requires stronger setup quality, thicker net edge and lower ATR.
    """
    min_lev = max(1, int(min_operator_leverage or 1))
    max_lev = max(1, int(max_operator_leverage or min_lev))
    if max_lev < min_lev:
        max_lev = min_lev

    liq_safe_max = None
    if liquidation_safe_max_leverage is not None:
        try:
            liq_safe_max = max(1, int(liquidation_safe_max_leverage))
        except Exception:
            liq_safe_max = None
    effective_max_lev = max_lev if liq_safe_max is None else max(min_lev, min(max_lev, liq_safe_max))

    dir_norm = str(direction or "neutral").strip().lower()
    dir_strength = _clamp(float(dir_strength or 0.0), 0.0, 1.0)
    range_score = _clamp(float(range_score or 0.0), 0.0, 1.0)
    trendiness = _clamp(float(trendiness or 0.0), 0.0, 1.0)
    atr_pct = max(0.0, float(atr_pct or 0.0))
    exec_bps = max(0.0, float(execution_cost_bps or 0.0))
    funding_bps = max(0.0, float(funding_cost_bps or 0.0))
    gross_bps = max(0.0, float(gross_profit_bps_est or 0.0))
    projected_net_bps = gross_bps - exec_bps - funding_bps

    directional_setup_quality = dir_strength if dir_norm in {"long", "short"} else 0.0
    neutral_setup_quality = range_score * (1.0 - min(0.75, trendiness * 0.75)) if dir_norm == "neutral" else 0.0
    setup_quality = _clamp(max(directional_setup_quality, neutral_setup_quality), 0.0, 1.0)
    selected_leverage, adaptive_diag = _adaptive_grid_leverage_from_quality(
        min_leverage=min_lev,
        max_leverage=effective_max_lev,
        setup_quality=setup_quality,
        projected_net_bps=projected_net_bps,
        atr_pct=atr_pct,
        execution_cost_bps=exec_bps,
    )

    diagnostics = {
        "min_operator_leverage": int(min_lev),
        "max_operator_leverage": int(max_lev),
        "effective_max_operator_leverage": int(effective_max_lev),
        "liquidation_safe_max_leverage": int(liq_safe_max) if liq_safe_max is not None else None,
        "target_leverage": int(selected_leverage),
        "selected_leverage": int(selected_leverage),
        "direction": dir_norm,
        "direction_bias_strength": float(dir_strength),
        "range_score": float(range_score),
        "trendiness": float(trendiness),
        "atr_pct": float(atr_pct),
        "gross_profit_bps_est": float(gross_bps),
        "execution_cost_bps": float(exec_bps),
        "funding_cost_bps": float(funding_bps),
        "projected_net_profit_bps_est": float(projected_net_bps),
        "min_projected_net_profit_bps": 2.0,
        "directional_setup_quality": float(directional_setup_quality),
        "neutral_setup_quality": float(neutral_setup_quality),
        **adaptive_diag,
    }

    def _approve(note: str) -> tuple[int, str, dict[str, Any]]:
        diagnostics["operator_minimum_approved"] = True
        diagnostics["not_actionable_reason"] = None
        return int(selected_leverage), note, diagnostics

    def _decline(note: str) -> tuple[int, str, dict[str, Any]]:
        # Do not publish a synthetic 1x payload under a fixed higher operator
        # profile.  The recommendation is later marked no_trade/not_actionable,
        # while the payload still records the active profile it was evaluated
        # against.  Legacy 1x rows remain blocked by execution-time guards.
        diagnostics["operator_minimum_approved"] = False
        diagnostics["not_actionable_reason"] = note
        return int(selected_leverage), note, diagnostics

    if min_lev <= 1 and max_lev <= 1:
        return _approve("operator_minimum_is_one")
    if atr_pct >= 0.05 or exec_bps >= 45.0:
        return _decline("unsafe_volatility_or_execution_cost")
    if atr_pct > 0.025:
        return _decline("atr_too_high_for_operator_minimum")
    if projected_net_bps < 2.0:
        return _decline("insufficient_net_edge_for_operator_minimum")

    directional_quality = dir_norm in {"long", "short"} and dir_strength >= 0.45
    neutral_range_quality = dir_norm == "neutral" and range_score >= 0.70 and trendiness <= 0.35
    diagnostics["directional_quality"] = bool(directional_quality)
    diagnostics["neutral_range_quality"] = bool(neutral_range_quality)

    # The operator minimum is the base actionable floor after hard safety and
    # economics checks above.  Directional/range quality is still recorded and
    # remains required by `_adaptive_grid_leverage_from_quality()` for promotion
    # above the floor; otherwise score/conf-favoured grid ideas can be trapped
    # forever between the thesis gate and the leverage-profile gate.
    if min_lev == max_lev or selected_leverage == min_lev:
        return _approve("operator_minimum_selected")
    return _approve("adaptive_interval_selected")



def _max_liquidation_safe_grid_leverage(
    *,
    direction: str,
    reference_price: float,
    lower: float,
    upper: float,
    grid_count: int,
    kill_switch_lower: float,
    kill_switch_upper: float,
    execution_cost_bps: float,
    min_leverage: int,
    max_leverage: int,
    min_buffer_pct: float = 12.0,
) -> int | None:
    """Highest leverage whose cross-margin bot-equity stress clears the floor."""
    min_lev = max(1, int(min_leverage or 1))
    max_lev = max(min_lev, int(max_leverage or min_lev))
    safe: int | None = None
    for lev in range(min_lev, max_lev + 1):
        stress = arithmetic_grid_cross_margin_stress(
            lower=lower,
            upper=upper,
            grid_count=grid_count,
            reference_price=reference_price,
            direction=direction,
            leverage=lev,
            kill_switch_lower=kill_switch_lower,
            kill_switch_upper=kill_switch_upper,
            execution_cost_bps=execution_cost_bps,
        )
        buffer_pct = stress.get("equity_buffer_pct") if isinstance(stress, dict) else None
        try:
            buffer_f = float(buffer_pct)
        except (TypeError, ValueError):
            continue
        if math.isfinite(buffer_f) and buffer_f >= float(min_buffer_pct):
            safe = lev
    return safe


def _directional_trend_params(
    *,
    venue: str,
    f: dict[str, Any],
    direction: str,
    global_sent: float,
    direction_bias: str,
    direction_bias_strength: float,
    atr_pct: float | None,
    cost_model: dict[str, Any],
    risk_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a recommendation-only single-position directional contract.

    The contract intentionally contains no grid levels, averaging or pyramiding.
    It is sufficient for deterministic proxy-outcome labeling and for an external
    executor/operator package, but this service still does not submit Bybit orders.
    """
    price_raw = _finite_or_none(f.get("price"))
    price_valid = price_raw is not None and price_raw > 0.0
    price = float(price_raw) if price_valid else 0.0
    direction_norm = str(direction or "").strip().lower()
    direction_valid = direction_norm in {"long", "short"}
    if not direction_valid:
        return {
            "bot_type": "directional_trend",
            "candidate_kind": TREND_EVALUATION_REJECTED_KIND,
            "strategy_family": "trend_evaluation",
            "evaluation_only": True,
            "venue": venue,
            "direction": "neutral",
            "direction_bias": direction_bias,
            "direction_bias_strength": float(_clamp(abs(_finite_float(direction_bias_strength, 0.0)), 0.0, 1.0)),
            "effective_sentiment": float(_clamp(_finite_float(global_sent, 0.0), -1.0, 1.0)),
            "price_input_valid": bool(price_valid),
            "invalid_price_fail_closed": not bool(price_valid),
            "direction_input_valid": False,
            "price_ref": _round_price(price, decimals=10),
            "evaluation_reason_code": "TREND_DIRECTION_UNCONFIRMED",
            "evaluation_note": (
                "Предварительная оценка не подтвердила LONG или SHORT. "
                "Позиция, TP/SL, outcome-root и обучающая метка не создаются."
            ),
            "trade_plan": {},
            "sizing": {},
            "economics": {},
            "operator_metrics": {},
        }
    atr_value = _finite_or_none(atr_pct)
    if atr_value is None or atr_value <= 0.0:
        atr_value = _finite_or_none(f.get("_atr_pct_1h"))
    if atr_value is None or atr_value <= 0.0:
        atr_value = _finite_or_none(f.get("atr_pct"))
    atr_value = max(float(atr_value or 0.0), 0.0015)

    execution_cost_bps = max(
        0.0,
        _finite_float(
            cost_model.get("execution_cost_bps")
            or cost_model.get("total_cost_bps")
            or cost_model.get("net_cost_bps"),
            0.0,
        ),
    )
    adverse_funding_bps = max(
        0.0,
        _finite_float(
            cost_model.get("funding_cost_bps_for_approval")
            or cost_model.get("expected_funding_bps"),
            0.0,
        ),
    )
    cost_floor_pct = (execution_cost_bps + adverse_funding_bps) / 10_000.0
    stop_pct = _clamp(max(1.25 * atr_value, 4.0 * cost_floor_pct, 0.0040), 0.0040, 0.0800)
    reward_pct = _clamp(max(1.80 * stop_pct, 6.0 * cost_floor_pct, 0.0080), 0.0080, 0.1500)

    take_profit = None
    stop_loss = None
    if price_valid and direction_valid:
        if direction_norm == "long":
            take_profit = price * (1.0 + reward_pct)
            stop_loss = price * (1.0 - stop_pct)
        else:
            take_profit = price * (1.0 - reward_pct)
            stop_loss = price * (1.0 + stop_pct)

    normalized_limits = normalize_risk_limits(risk_limits or {}, risk_limits or {})
    min_leverage = max(1, int(_safe_int_or_none(normalized_limits.get("min_leverage")) or 1))
    max_leverage = max(min_leverage, int(_safe_int_or_none(normalized_limits.get("max_leverage")) or min_leverage))
    selected_leverage = min(max_leverage, min_leverage)
    max_notional = _finite_or_none(normalized_limits.get("max_position_notional_usdt"))
    target_notional = min(25.0, float(max_notional)) if max_notional is not None and max_notional > 0 else 25.0
    provisional_qty = (target_notional / price) if price_valid else 0.0
    gross_reward_bps = reward_pct * 10_000.0
    gross_risk_bps = stop_pct * 10_000.0
    projected_net_reward_bps = gross_reward_bps - execution_cost_bps - adverse_funding_bps
    projected_stop_loss_bps = gross_risk_bps + execution_cost_bps + adverse_funding_bps
    plan_rr = (
        max(0.0, projected_net_reward_bps) / projected_stop_loss_bps
        if projected_stop_loss_bps > 0.0
        else None
    )

    return {
        "bot_type": "directional_trend",
        "candidate_kind": TREND_STRATEGY_RECOMMENDATION_KIND,
        "strategy_family": "directional_trend",
        "strategy_contract_version": TREND_STRATEGY_CONTRACT_VERSION,
        "outcome_label_version": TREND_OUTCOME_LABEL_VERSION,
        "venue": venue,
        "direction": direction_norm if direction_valid else "neutral",
        "direction_bias": direction_bias,
        "direction_bias_strength": float(_clamp(abs(_finite_float(direction_bias_strength, 0.0)), 0.0, 1.0)),
        "effective_sentiment": float(_clamp(_finite_float(global_sent, 0.0), -1.0, 1.0)),
        "price_input_valid": bool(price_valid),
        "invalid_price_fail_closed": not bool(price_valid),
        "direction_input_valid": bool(direction_valid),
        "price_ref": _round_price(price, decimals=10),
        "entry_model": "single_position_no_pyramiding",
        "averaging_allowed": False,
        "pyramiding_allowed": False,
        "take_profit_price": _round_price(take_profit, decimals=10),
        "stop_loss_price": _round_price(stop_loss, decimals=10),
        "take_profit_pct": float(reward_pct * 100.0),
        "stop_loss_pct": float(stop_pct * 100.0),
        "label_horizon_hours": int(BOT_HORIZONS.get("directional_trend", 12 * 3600) // 3600),
        "leverage": int(selected_leverage),
        "margin_mode": "isolated",
        "leverage_policy": {
            "min_operator_leverage": int(min_leverage),
            "max_operator_leverage": int(max_leverage),
            "selected_leverage": int(selected_leverage),
            "operator_minimum_approved": True,
            "note": "single-position trend plan uses the minimum operator-approved leverage",
        },
        "sizing": {
            "mode": "external_single_position_target_notional",
            "qty": float(provisional_qty),
            "target_notional_usdt": float(target_notional),
            "estimated_margin_required_usdt": float(target_notional / max(1, selected_leverage)),
            "actual_bybit_filters_required": True,
        },
        "economics": {
            "projected_gross_reward_bps": float(gross_reward_bps),
            "projected_stop_distance_bps": float(gross_risk_bps),
            "execution_cost_bps": float(execution_cost_bps),
            "funding_cost_bps": float(adverse_funding_bps),
            "projected_net_reward_bps": float(projected_net_reward_bps),
            "projected_stop_loss_bps": float(projected_stop_loss_bps),
            "plan_rr": float(plan_rr) if plan_rr is not None and math.isfinite(plan_rr) else None,
            "risk_profile": "directional_single_position",
        },
        "cost_model": dict(cost_model),
    }

def _params(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    global_sent: float,
    direction: str,
    taker_fee_bps: float,
    direction_bias: str,
    direction_bias_strength: float,
    atr_pct_for_grid: float | None,
    cost_model: dict[str, Any] | None = None,
    risk_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cost_model = dict(cost_model or {})
    if bot_type == "directional_trend":
        return _directional_trend_params(
            venue=venue,
            f=f,
            direction=direction,
            global_sent=global_sent,
            direction_bias=direction_bias,
            direction_bias_strength=direction_bias_strength,
            atr_pct=atr_pct_for_grid,
            cost_model=cost_model,
            risk_limits=risk_limits,
        )
    price_input = _finite_or_none(f.get("price"))
    price_input_valid = price_input is not None and price_input > 0.0
    price = float(price_input) if price_input_valid else 0.0

    atr_raw = _finite_or_none(atr_pct_for_grid)
    if atr_raw is None:
        atr_raw = _finite_or_none(f.get("atr_pct"))
    atr_pct = max(float(atr_raw or 0.0), 0.0015)
    agg = dict(f.get("_direction_agg") or {})
    range_score, _ = _stable_range_score(f, agg)
    dir_strength = _clamp(abs(_finite_float(direction_bias_strength, 0.0)), 0.0, 1.0)

    if not price_input_valid:
        # Fail closed on missing/non-positive/non-finite market price.  A synthetic
        # $1 reference price is dangerous for linear futures because it can create
        # apparently valid TP/SL, liquidation and min-notional geometry for an
        # instrument whose live price was actually unavailable.
        funding_cost_bps_for_spacing = 0.0
        execution_cost_bps = max(
            0.0,
            _finite_float(
                cost_model.get("grid_round_trip_fee_bps")
                or cost_model.get("fee_bps_round_trip")
                or max(0.0, _finite_float(taker_fee_bps, 0.0) * 2.0),
                0.0,
            ),
        )
        params: dict[str, Any] = {
            "bot_type": bot_type,
            "venue": venue,
            "direction": direction,
            "direction_bias": direction_bias,
            "direction_bias_strength": float(dir_strength),
            "effective_sentiment": float(_clamp(_finite_float(global_sent, 0.0), -1.0, 1.0)),
            "price_input_valid": False,
            "invalid_price_fail_closed": True,
            "price_ref": 0.0,
            "price_range_lower": 0.0,
            "price_range_upper": 0.0,
            "range_span_pct_total": 0.0,
            "grid_spacing_pct": 0.0,
            "actual_grid_step_abs": 0.0,
            "actual_grid_spacing_pct": 0.0,
            "economic_min_grid_spacing_pct": 0.0,
            "grid_spacing_cost_floor_bps": float(execution_cost_bps + funding_cost_bps_for_spacing),
            "grid_spacing_funding_cost_bps": float(funding_cost_bps_for_spacing),
            "grid_density_economic_cost_bps": float(execution_cost_bps + funding_cost_bps_for_spacing),
            "grid_geometry_model": "invalid_price_fail_closed",
            "grid_type": "arithmetic",
            "grid_count": 0,
            "grid_levels": 0,
            "label_horizon_hours": int(BOT_HORIZONS.get(bot_type, 12 * 3600) // 3600),
            "cost_model": dict(cost_model),
            "leverage": 1,
            "margin_mode": "cross",
            "leverage_policy": {
                "min_operator_leverage": 1,
                "max_operator_leverage": 1,
                "selected_leverage": 1,
                "note": "invalid_price_fail_closed",
                "diagnostics": {"price_input_valid": False},
            },
            "economics": {
                "step_abs": 0.0,
                "gross_profit_bps": 0.0,
                "execution_cost_bps": float(execution_cost_bps),
                "expected_funding_bps": _finite_float(cost_model.get("expected_funding_bps"), 0.0),
                "funding_cost_bps": float(funding_cost_bps_for_spacing),
                "funding_benefit_excluded_bps": 0.0,
                "net_profit_bps": 0.0,
                "net_profit_with_signed_funding_bps": 0.0,
                "gross_profit_usdt": 0.0,
                "execution_cost_usdt": 0.0,
                "expected_funding_usdt": 0.0,
                "funding_cost_usdt": 0.0,
                "funding_benefit_excluded_usdt": 0.0,
                "net_profit_usdt": 0.0,
                "net_profit_with_signed_funding_usdt": 0.0,
                "breakeven": False,
                "order_notional_usdt": 0.0,
                "qty_per_order": 0.0,
                "grid_type": "arithmetic",
                "grid_count": 0,
                "estimated_active_orders": 0,
                "estimated_total_order_notional_usdt": 0.0,
                "estimated_margin_required_usdt": 0.0,
                "estimated_max_position_notional_usdt": 0.0,
                "estimated_liquidation_price": None,
                "liquidation_buffer_pct_reference": None,
                "liquidation_buffer_pct_adverse_boundary": None,
                "liquidation_buffer_adverse_boundary_price": None,
                "liquidation_buffer_pct": None,
                "liquidation_model": "invalid_price_fail_closed",
                "risk_profile": "blocked",
            },
            "sizing": {
                "basis": "invalid_price_fail_closed",
                "order_notional_usdt": 0.0,
                "qty_per_order": 0.0,
                "grid_type": "arithmetic",
                "grid_count": 0,
                "estimated_active_orders": 0,
                "estimated_total_order_notional_usdt": 0.0,
                "estimated_margin_required_usdt": 0.0,
                "exchange_filter_assumption": {"mode": "invalid_price"},
                "note": "Market reference price is missing/non-positive/non-finite; no actionable Bybit grid sizing is published.",
            },
        }
        return _sanitize_json_numbers(params)

    market_execution_cost_bps = max(
        0.0,
        float(cost_model.get("market_round_trip_cost_bps") or cost_model.get("total_cost_bps") or cost_model.get("execution_cost_bps") or max(0.0, float(taker_fee_bps) * 2.0)),
    )
    grid_round_trip_fee_bps = max(
        0.0,
        float(cost_model.get("grid_round_trip_fee_bps") or cost_model.get("fee_bps_round_trip") or max(0.0, float(taker_fee_bps) * 2.0)),
    )
    # A completed Bybit grid pair earns one interval minus the fees of the two
    # resting limit fills. Bid/ask spread and slippage belong to one-time market
    # setup/terminal liquidation, while funding belongs to position-time Total P&L.
    # Charging either layer to every pair widens the grid and depresses outcomes
    # in proportion to the number of completed cycles.
    funding_cost_bps_for_spacing = 0.0
    cost_floor_bps_for_spacing = grid_round_trip_fee_bps
    economic_cost_bps_for_density = grid_round_trip_fee_bps
    execution_cost_bps = market_execution_cost_bps
    cost_floor_pct = max(cost_floor_bps_for_spacing / 10000.0, 0.0001)
    # A completed pair earns the full adjacent interval. Keep a 25% economic
    # buffer over costs, but do not divide the interval by a synthetic 70%
    # fill-efficiency coefficient: execution frequency and per-trade P&L are
    # different dimensions.
    min_spacing_pct = max(cost_floor_pct * 1.25, 0.0008)
    vol_spacing_pct = max(atr_pct * (0.45 + 0.25 * range_score), 0.0010)
    grid_spacing_pct_frac = max(min_spacing_pct, vol_spacing_pct)

    base_levels = 10
    if range_score >= 0.80:
        base_levels += 2
    elif range_score <= 0.45:
        base_levels -= 2
    if economic_cost_bps_for_density >= 18.0:
        base_levels -= 2
    elif economic_cost_bps_for_density <= 8.0:
        base_levels += 1
    if dir_strength >= 0.60:
        base_levels -= 1
    grid_levels = max(4, min(14, int(base_levels)))

    # ``grid_count`` is documented and validated as Bybit's Number of Grids,
    # i.e. the number of price intervals. The range span therefore must scale
    # with ``grid_levels`` itself, not ``grid_levels - 1``. Using points instead
    # of intervals silently compressed the range and made the displayed
    # step/range geometry inconsistent for manual operator setup.
    economic_min_spacing_pct_frac = grid_spacing_pct_frac
    range_span_pct_total = max(economic_min_spacing_pct_frac * max(grid_levels, 4) * 1.15, atr_pct * (3.0 + 2.0 * range_score))
    half_span = range_span_pct_total / 2.0

    down_mult = 1.0
    up_mult = 1.0
    if direction == "long":
        down_mult = 0.90
        up_mult = 1.10
    elif direction == "short":
        down_mult = 1.10
        up_mult = 0.90

    lower = price * max(0.01, 1.0 - half_span * down_mult)
    upper = price * (1.0 + half_span * up_mult)
    if upper <= lower:
        lower = price * (1.0 - half_span)
        upper = price * (1.0 + half_span)

    # Bybit arithmetic Futures Grid uses lower/upper plus Number of Grids, so the
    # executable step is exactly range_width / grid_count. The earlier minimum
    # economic spacing is only a floor for building the range. Publishing it as
    # grid_spacing_pct understated the actual exchange geometry, TP hint and
    # per-grid economics whenever the range had ATR/padding expansion.
    actual_grid_step_abs = max(0.0, (upper - lower) / max(1, int(grid_levels)))
    actual_grid_spacing_pct_frac = (actual_grid_step_abs / price) if price > 0 else economic_min_spacing_pct_frac
    grid_spacing_pct_frac = max(actual_grid_spacing_pct_frac, economic_min_spacing_pct_frac)
    if actual_grid_spacing_pct_frac < economic_min_spacing_pct_frac:
        # Should only happen after defensive clamps; widen the range to preserve
        # the minimum net-edge floor instead of publishing an over-dense grid.
        target_span = economic_min_spacing_pct_frac * max(1, int(grid_levels))
        half_span = target_span / 2.0
        lower = price * max(0.01, 1.0 - half_span * down_mult)
        upper = price * (1.0 + half_span * up_mult)
        range_span_pct_total = (upper - lower) / price if price > 0 else target_span
        actual_grid_step_abs = max(0.0, (upper - lower) / max(1, int(grid_levels)))
        actual_grid_spacing_pct_frac = (actual_grid_step_abs / price) if price > 0 else economic_min_spacing_pct_frac
        grid_spacing_pct_frac = actual_grid_spacing_pct_frac

    # Use the same ATR padding as trade_plan.kill_switch so liquidation checks are
    # performed at the worst grid boundary the operator is instructed to tolerate,
    # not only at the benign reference price. Reference-only buffers overstate safety
    # for leveraged grids that accumulate inventory near the range edge.
    atr_abs_for_liq = max(0.0, price * atr_pct)
    kill_switch_pad = 0.6 * atr_abs_for_liq if atr_abs_for_liq > 0 else 0.0
    lower_kill_switch = lower - kill_switch_pad if lower > 0 else lower
    upper_kill_switch = upper + kill_switch_pad if upper > 0 else upper
    adverse_long_ref = lower_kill_switch if lower_kill_switch > 0 else lower
    adverse_short_ref = upper_kill_switch if upper_kill_switch > 0 else upper

    params: dict[str, Any] = {
        "bot_type": bot_type,
        "venue": venue,
        "direction": direction,
        "direction_bias": direction_bias,
        "direction_bias_strength": float(dir_strength),
        "effective_sentiment": float(_clamp(_finite_float(global_sent, 0.0), -1.0, 1.0)),
        "price_input_valid": True,
        "invalid_price_fail_closed": False,
        "price_ref": _round_price(price, decimals=10),
        "price_range_lower": _round_price(lower, decimals=10),
        "price_range_upper": _round_price(upper, decimals=10),
        "range_span_pct_total": float(range_span_pct_total * 100.0),
        "grid_spacing_pct": float(grid_spacing_pct_frac * 100.0),
        "actual_grid_step_abs": _round_price(actual_grid_step_abs, decimals=10),
        "actual_grid_spacing_pct": float(actual_grid_spacing_pct_frac * 100.0),
        "economic_min_grid_spacing_pct": float(economic_min_spacing_pct_frac * 100.0),
        "grid_spacing_cost_floor_bps": float(cost_floor_bps_for_spacing),
        "grid_spacing_funding_cost_bps": float(funding_cost_bps_for_spacing),
        "grid_density_economic_cost_bps": float(economic_cost_bps_for_density),
        "grid_geometry_model": "bybit_arithmetic_range_width_div_grid_count",
        # Bybit UI calls this value "Number of Grids"; it is the number of
        # price intervals, not the number of displayed price points. Keep the
        # legacy key ``grid_levels`` for API compatibility and add explicit
        # aliases for new UI/API consumers.
        "grid_type": "arithmetic",
        "grid_count": int(grid_levels),
        "grid_levels": int(grid_levels),
        "label_horizon_hours": int(BOT_HORIZONS.get(bot_type, 12 * 3600) // 3600),
        "cost_model": dict(cost_model),
    }

    if venue == "linear":
        try:
            # Recommendation leverage must be tied to the exact runtime/operator risk
            # profile used for publication blocks.  Using only settings.risk_limits
            # here makes DB/runtime overrides invisible inside params and can publish
            # a 1x payload while /risk/status already requires the 3x..5x operator interval.
            effective_limits = normalize_risk_limits(risk_limits, getattr(settings, "risk_limits", {}) or {})
        except Exception:
            effective_limits = {"min_leverage": 3, "max_leverage": 5}
        min_operator_leverage = int(effective_limits.get("min_leverage") or 1)
        max_operator_leverage = int(effective_limits.get("max_leverage") or max(1, min_operator_leverage))

        gross_profit_bps_est = float(actual_grid_spacing_pct_frac * 10000.0)
        trendiness = _clamp(float(agg.get("trendiness") or f.get("trend_strength") or 0.0), 0.0, 1.0)
        liquidation_safe_max_leverage = _max_liquidation_safe_grid_leverage(
            direction=direction,
            reference_price=price,
            lower=lower,
            upper=upper,
            grid_count=grid_levels,
            kill_switch_lower=adverse_long_ref,
            kill_switch_upper=adverse_short_ref,
            execution_cost_bps=execution_cost_bps,
            min_leverage=min_operator_leverage,
            max_leverage=max_operator_leverage,
            min_buffer_pct=12.0,
        )
        leverage, leverage_policy_note, leverage_policy_diag = _select_operator_grid_leverage(
            direction=direction,
            dir_strength=dir_strength,
            range_score=range_score,
            trendiness=trendiness,
            atr_pct=atr_pct,
            execution_cost_bps=execution_cost_bps,
            funding_cost_bps=funding_cost_bps_for_spacing,
            gross_profit_bps_est=gross_profit_bps_est,
            min_operator_leverage=min_operator_leverage,
            max_operator_leverage=max_operator_leverage,
            liquidation_safe_max_leverage=liquidation_safe_max_leverage,
        )
        params["leverage"] = int(leverage)
        params["margin_mode"] = "cross"
        params["leverage_policy"] = {
            "min_operator_leverage": int(min_operator_leverage),
            "max_operator_leverage": int(max_operator_leverage),
            "selected_leverage": int(leverage),
            "operator_minimum_approved": bool(leverage_policy_diag.get("operator_minimum_approved", leverage_policy_note in {"operator_minimum_is_one", "operator_minimum_selected"})),
            "not_actionable_reason": leverage_policy_diag.get("not_actionable_reason"),
            "note": leverage_policy_note,
            "diagnostics": leverage_policy_diag,
        }
    else:
        params["leverage"] = 1
        params["margin_mode"] = "cross"

    # Conservative minimum viable sizing. The project does not know the operator's
    # wallet balance, so this is an exchange-preflightable default, not a position
    # sizing promise. Execution preflight revalidates against live minNotional/qtyStep.
    order_qty, order_notional_usdt, sizing_assumption = _fallback_order_qty_for_linear_grid(price, target_notional_usdt=25.0)
    commitment = arithmetic_grid_commitment(
        lower=lower,
        upper=upper,
        grid_count=grid_levels,
        reference_price=price,
        direction=direction,
    )
    if commitment is None:
        # Generated geometry should always resolve. Keep a conservative fail-closed
        # estimate if defensive clamps ever produce an unexpected topology.
        active_grid_orders = max(1, int(grid_levels))
        committed_grid_slots = active_grid_orders
        max_position_slots = active_grid_orders
        committed_notional_per_qty = float(price) * float(active_grid_orders)
        commitment_model = "fallback_grid_count_initial_orders"
    else:
        active_grid_orders = int(commitment["active_order_count"])
        committed_grid_slots = int(commitment["committed_slot_count"])
        max_position_slots = int(commitment["max_abs_position_slots"])
        committed_notional_per_qty = float(commitment["committed_notional_per_qty"])
        if direction == "neutral":
            commitment_model = "neutral_all_initial_opening_orders"
        else:
            commitment_model = (
                "grid_count_orders_reference_on_level"
                if commitment.get("exact_grid_line")
                else "grid_count_orders_dynamic_bridge_reference_between_levels"
            )
    total_order_notional = float(order_qty) * committed_notional_per_qty
    # Runtime risk caps use the largest position that can exist in one-way mode,
    # not the total count of simultaneous opposite-side resting orders.
    worst_case_order_notional = float(order_qty) * max(float(price), float(lower), float(upper))
    worst_case_total_notional = worst_case_order_notional * max_position_slots
    leverage_used = max(1, int(params.get("leverage") or 1))
    margin_required = float(margin_required_usdt(total_order_notional, leverage_used))
    worst_case_margin_required = float(margin_required_usdt(worst_case_total_notional, leverage_used))
    grid_econ = grid_leg_economics(
        reference_price=price,
        step_pct=params.get("grid_spacing_pct"),
        order_notional=order_notional_usdt,
        taker_fee_bps=taker_fee_bps,
        execution_cost_bps=grid_round_trip_fee_bps,
        expected_funding_bps=cost_model.get("expected_funding_bps") or 0.0,
        fill_efficiency="0.70",
    )

    cross_margin_stress = arithmetic_grid_cross_margin_stress(
        lower=lower,
        upper=upper,
        grid_count=grid_levels,
        reference_price=price,
        direction=direction,
        leverage=leverage_used,
        kill_switch_lower=adverse_long_ref,
        kill_switch_upper=adverse_short_ref,
        execution_cost_bps=execution_cost_bps,
    )
    stress_buffer_pct = (
        float(cross_margin_stress.get("equity_buffer_pct"))
        if isinstance(cross_margin_stress, dict)
        and cross_margin_stress.get("equity_buffer_pct") is not None
        else None
    )
    stress_long = cross_margin_stress.get("long") if isinstance(cross_margin_stress, dict) else {}
    stress_short = cross_margin_stress.get("short") if isinstance(cross_margin_stress, dict) else {}
    stress_fields = {
        # Backward fields remain present but no longer pretend an isolated
        # liquidation price applies to a cross-margin Futures Grid bot.
        "estimated_liquidation_price": None,
        "estimated_liquidation_price_long": None,
        "estimated_liquidation_price_short": None,
        "liquidation_buffer_pct_reference": None,
        "liquidation_buffer_pct_adverse_boundary": None,
        "liquidation_buffer_pct_long_reference": None,
        "liquidation_buffer_pct_short_reference": None,
        "liquidation_buffer_pct_long_adverse_boundary": None,
        "liquidation_buffer_pct_short_adverse_boundary": None,
        "liquidation_buffer_adverse_boundary_price": None,
        "liquidation_buffer_adverse_boundary_long": float(adverse_long_ref),
        "liquidation_buffer_adverse_boundary_short": float(adverse_short_ref),
        "liquidation_buffer_pct": stress_buffer_pct,
        "cross_margin_stress_buffer_pct": stress_buffer_pct,
        "cross_margin_stress_buffer_pct_long": (
            stress_long.get("equity_buffer_pct") if isinstance(stress_long, dict) else None
        ),
        "cross_margin_stress_buffer_pct_short": (
            stress_short.get("equity_buffer_pct") if isinstance(stress_short, dict) else None
        ),
        "cross_margin_initial_margin_per_qty": (
            cross_margin_stress.get("initial_margin_per_qty") if isinstance(cross_margin_stress, dict) else None
        ),
        "cross_margin_worst_loss_per_qty": (
            cross_margin_stress.get("worst_loss_per_qty") if isinstance(cross_margin_stress, dict) else None
        ),
        "cross_margin_equity_buffer_per_qty": (
            cross_margin_stress.get("equity_buffer_per_qty") if isinstance(cross_margin_stress, dict) else None
        ),
        "cross_margin_worst_side": (
            cross_margin_stress.get("worst_side") if isinstance(cross_margin_stress, dict) else None
        ),
        "cross_margin_stress": (
            _sanitize_json_numbers(cross_margin_stress)
            if isinstance(cross_margin_stress, dict)
            else None
        ),
        "liquidation_model": "bybit_futures_grid_cross_margin_equity_stress",
        "risk_profile": (
            "conservative"
            if leverage_used <= 1 and float(grid_econ.get("net_profit_bps") or 0.0) >= 4.0 and (stress_buffer_pct is None or stress_buffer_pct >= 20.0)
            else ("moderate" if leverage_used <= 2 and (stress_buffer_pct is None or stress_buffer_pct >= 12.0) else "aggressive")
        ),
    }
    params["economics"] = {
        **grid_econ,
        "order_notional_usdt": float(order_notional_usdt),
        "qty_per_order": _round_price(order_qty, decimals=10),
        "grid_type": "arithmetic",
        "grid_count": int(grid_levels),
        "estimated_active_orders": int(active_grid_orders),
        "estimated_committed_slots": int(committed_grid_slots),
        "estimated_max_position_slots": int(max_position_slots),
        "estimated_total_order_notional_usdt": float(total_order_notional),
        "estimated_worst_case_order_notional_usdt": float(worst_case_order_notional),
        "estimated_worst_case_total_order_notional_usdt": float(worst_case_total_notional),
        "estimated_margin_required_usdt": float(margin_required),
        "estimated_worst_case_margin_required_usdt": float(worst_case_margin_required),
        "grid_commitment_model": commitment_model,
        "reference_on_grid_level": bool(commitment.get("exact_grid_line")) if commitment is not None else None,
        "estimated_max_position_notional_usdt": float(max(total_order_notional, worst_case_total_notional)),
        **stress_fields,
    }
    params["sizing"] = {
        "basis": "minimum_viable_operator_default",
        "order_notional_usdt": float(order_notional_usdt),
        "qty_per_order": _round_price(order_qty, decimals=10),
        "grid_type": "arithmetic",
        "grid_count": int(grid_levels),
        "estimated_active_orders": int(active_grid_orders),
        "estimated_committed_slots": int(committed_grid_slots),
        "estimated_max_position_slots": int(max_position_slots),
        "estimated_total_order_notional_usdt": float(total_order_notional),
        "estimated_worst_case_order_notional_usdt": float(worst_case_order_notional),
        "estimated_worst_case_total_order_notional_usdt": float(worst_case_total_notional),
        "estimated_margin_required_usdt": float(margin_required),
        "estimated_worst_case_margin_required_usdt": float(worst_case_margin_required),
        "grid_commitment_model": commitment_model,
        "reference_on_grid_level": bool(commitment.get("exact_grid_line")) if commitment is not None else None,
        "exchange_filter_assumption": sizing_assumption,
        "note": "Размер заявки — provisional ориентир по target notional без повышения qty. Live preflight округляет qty только вниз по фактическому qtyStep и блокирует план ниже minQty/minNotional; оператор должен сверить доступную маржу.",
    }

    return params

# ── Persistence gate state ───────────────────────────────────────────────────
# Tracks consecutive recommended cycles for the SAME logical signal.
# The original implementation keyed only by (venue, symbol, bot_type) and therefore
# could accidentally confirm a freshly flipped short using a previous long signal.
# We include direction in the signature and require confirmation from distinct,
# forward-moving closed-candle evidence within an interval-derived freshness window.
_prev_recommended: dict[tuple, dict[str, int]] = {}
PERSISTENCE_BOTS: set[str] = {"futures_grid", "directional_trend"}
PERSISTENCE_STATE_APP_KEY = "reco_persistence_gate_v1"
DIRECTION_STATE_APP_KEY = "reco_direction_stability_v1"


def _load_prev_recommended(conn) -> dict[tuple, dict[str, int]]:
    raw = db.get_app_config_json(conn, PERSISTENCE_STATE_APP_KEY, default={}) or {}
    out: dict[tuple, dict[str, int]] = {}
    if not isinstance(raw, dict):
        return out
    for key, state in raw.items():
        if not isinstance(key, str) or not isinstance(state, dict):
            continue
        parts = key.split("|")
        if len(parts) != 4:
            continue
        venue, sym, bot_type, direction = parts
        ts = _safe_int_or_none(state.get("ts"))
        count = _safe_int_or_none(state.get("count"))
        evidence_ts = _safe_int_or_none(state.get("evidence_ts"))
        if ts is None or count is None or ts <= 0 or count <= 0:
            continue
        out[(venue, sym, bot_type, direction)] = {
            "ts": int(ts),
            "count": int(count),
            # Older persisted state did not have an evidence timestamp. Keeping
            # zero forces one fresh closed-candle observation before it can pass.
            "evidence_ts": int(evidence_ts or 0),
        }
    return out


def _save_prev_recommended(conn, state: dict[tuple, dict[str, int]], fresh_gap: int, *, commit: bool = True) -> None:
    now = int(time.time())
    payload: dict[str, dict[str, int]] = {}
    ttl = max(int(fresh_gap) * 3, 600)
    for key, meta in (state or {}).items():
        if not isinstance(key, tuple) or len(key) != 4 or not isinstance(meta, dict):
            continue
        ts = _safe_int_or_none(meta.get("ts"))
        count = _safe_int_or_none(meta.get("count"))
        evidence_ts = _safe_int_or_none(meta.get("evidence_ts"))
        if (
            ts is None
            or count is None
            or evidence_ts is None
            or ts <= 0
            or count <= 0
            or evidence_ts <= 0
            or now - ts > ttl
        ):
            continue
        payload["|".join(str(x) for x in key)] = {
            "ts": int(ts),
            "count": int(count),
            "evidence_ts": int(evidence_ts),
        }
    db.set_app_config_json(conn, PERSISTENCE_STATE_APP_KEY, payload, commit=commit)


def _advance_persistence_gate(
    venue: str,
    sym: str,
    bot_type: str,
    direction: str,
    now_ts: int,
    fresh_gap: int,
    *,
    evidence_ts: int | None,
) -> int:
    """Count only distinct, forward-moving closed-candle evidence snapshots.

    Recommender cycles can run several times while ``features_ref_ts`` still
    points to the same closed 1m candle. Counting those retries as independent
    confirmation creates a false persistence signal. Duplicate or out-of-order
    evidence therefore cannot advance the publication gate.
    """
    global _prev_recommended
    pkey = (venue, sym, bot_type, direction)
    observed_at = _safe_int_or_none(now_ts)
    evidence = _safe_int_or_none(evidence_ts)
    gap = _safe_int_or_none(fresh_gap)
    if observed_at is None or evidence is None or gap is None or observed_at <= 0 or evidence <= 0 or gap <= 0:
        _prev_recommended.pop(pkey, None)
        return 0

    state = _prev_recommended.get(pkey) or {"ts": 0, "count": 0, "evidence_ts": 0}
    prior_ts = _safe_int_or_none(state.get("ts")) or 0
    prior_count = _safe_int_or_none(state.get("count")) or 0
    prior_evidence = _safe_int_or_none(state.get("evidence_ts")) or 0
    is_fresh = prior_ts > 0 and observed_at - prior_ts <= gap

    if is_fresh and evidence == prior_evidence:
        # Same closed candle is the same evidence, not a second confirmation.
        return int(prior_count)
    if is_fresh and prior_evidence > 0 and evidence > prior_evidence:
        next_state = {"ts": observed_at, "count": prior_count + 1, "evidence_ts": evidence}
    else:
        # First observation, stale state, legacy state without evidence_ts, or
        # out-of-order evidence all restart the confirmation sequence.
        next_state = {"ts": observed_at, "count": 1, "evidence_ts": evidence}

    _prev_recommended[pkey] = next_state
    for other_dir in ("long", "short", "neutral"):
        other_key = (venue, sym, bot_type, other_dir)
        if other_key != pkey:
            _prev_recommended.pop(other_key, None)
    return int(next_state["count"])


def _reset_persistence_gate(venue: str, sym: str, bot_type: str) -> None:
    global _prev_recommended
    for other_dir in ("long", "short", "neutral"):
        _prev_recommended.pop((venue, sym, bot_type, other_dir), None)


def _persistence_fresh_gap(settings) -> int:
    reco_interval = max(15, int(getattr(settings, "reco_interval_sec", 20) or 20))
    return max(180, min(600, reco_interval * 15))


def _recommendation_ttl_sec(settings) -> int:
    explicit_ttl = getattr(settings, "reco_ttl_sec", None)
    if explicit_ttl is not None:
        try:
            return max(180, int(explicit_ttl))
        except Exception:
            pass
    reco_interval = max(20, int(getattr(settings, "reco_interval_sec", 20) or 20))
    # Recommendations should survive materially longer than a single cycle.
    # Using collect_interval here was inconsistent with the actual publish cadence
    # and made operational gaps far more likely when the recommender loop slowed down.
    return max(900, reco_interval * 15)


def _persistence_gate_requirements(rec: dict[str, Any], settings) -> tuple[int, str]:
    # A high score is not independent evidence. Every grid recommendation must
    # survive at least one new closed-candle snapshot before it is actionable.
    # ``rec`` and ``settings`` remain parameters to preserve the internal contract
    # and allow future evidence-based policies without changing call sites.
    _ = rec, settings
    return 2, "distinct_evidence_confirmation"


_direction_state_cache: dict[tuple[str, str], dict[str, Any]] = {}


def _load_direction_state(conn) -> dict[tuple[str, str], dict[str, Any]]:
    raw = db.get_app_config_json(conn, DIRECTION_STATE_APP_KEY, default={}) or {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, state in raw.items():
        if not isinstance(key, str) or not isinstance(state, dict):
            continue
        parts = key.split("|", 1)
        if len(parts) != 2:
            continue
        venue, sym = parts
        out[(venue, sym)] = {
            "ts": int(_safe_int_or_none(state.get("ts")) or 0),
            "direction": str(state.get("direction") or "neutral"),
            "bias": str(state.get("bias") or "neutral"),
            "score_all": _finite_float(state.get("score_all"), 0.0),
            "trendiness": _finite_float(state.get("trendiness"), 0.0),
            "coherence": _finite_float(state.get("coherence"), 0.0),
        }
    return out


def _save_direction_state(conn, state: dict[tuple[str, str], dict[str, Any]], fresh_gap: int, *, commit: bool = True) -> None:
    now = int(time.time())
    ttl = max(int(fresh_gap) * 8, 1800)
    payload: dict[str, dict[str, Any]] = {}
    for key, meta in (state or {}).items():
        if not isinstance(key, tuple) or len(key) != 2 or not isinstance(meta, dict):
            continue
        ts = int(meta.get("ts", 0) or 0)
        if ts <= 0 or now - ts > ttl:
            continue
        payload[f"{key[0]}|{key[1]}"] = {
            "ts": ts,
            "direction": str(meta.get("direction") or "neutral"),
            "bias": str(meta.get("bias") or "neutral"),
            "score_all": _finite_float(meta.get("score_all"), 0.0),
            "trendiness": _finite_float(meta.get("trendiness"), 0.0),
            "coherence": _finite_float(meta.get("coherence"), 0.0),
        }
    db.set_app_config_json(conn, DIRECTION_STATE_APP_KEY, payload, commit=commit)


def _stabilize_direction_agg(
    agg: dict[str, Any],
    prev_state: dict[str, Any] | None,
    now_ts: int,
    fresh_gap: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stable = dict(agg or {})
    scores = dict(stable.get("scores") or {})
    strengths = dict(stable.get("strength") or {})
    raw_direction = str(stable.get("direction") or "neutral")
    raw_bias = str(stable.get("bias") or "neutral")
    score_all = _finite_float(scores.get("all"), 0.0)
    strength_all = _finite_float(strengths.get("all"), 0.0)
    trendiness = _finite_float(stable.get("trendiness"), 0.0)
    coherence = _finite_float(stable.get("coherence"), 0.0)
    regime = str(stable.get("regime") or "unknown")

    enter_thr = 0.14
    exit_thr = 0.09
    flip_thr = 0.18
    trend_enter_thr = 0.28
    range_dir_score_thr = 0.15
    range_dir_strength_thr = 0.14
    range_dir_coh_thr = 0.62

    stable["raw_direction"] = raw_direction
    stable["raw_bias"] = raw_bias

    prev = dict(prev_state or {})
    prev_ts = int(_safe_int_or_none(prev.get("ts")) or 0)
    prev_fresh = prev_ts > 0 and now_ts - prev_ts <= max(int(fresh_gap), 60)
    prev_direction = str(prev.get("direction") or "neutral") if prev_fresh else "neutral"

    applied = False
    mode = "pass_through"
    note = None

    directional_range_ok = (
        regime == "range"
        and raw_direction in ("long", "short")
        and raw_bias == raw_direction
        and abs(score_all) >= range_dir_score_thr
        and strength_all >= range_dir_strength_thr
        and coherence >= range_dir_coh_thr
    )

    if raw_direction in ("long", "short") and not directional_range_ok and (abs(score_all) < enter_thr or trendiness < trend_enter_thr):
        stable["direction"] = "neutral"
        applied = True
        mode = "enter_deadband"
        note = "Directional thesis is not strong enough to leave neutral state yet."

    if regime == "range" and not directional_range_ok and abs(score_all) < flip_thr:
        stable["direction"] = "neutral"
        if raw_direction != "neutral":
            applied = True
            mode = "range_neutrality_hold"
            note = "Range regime keeps the longer-horizon thesis neutral until the break becomes clearer."

    if prev_direction in ("long", "short"):
        current_direction = str(stable.get("direction") or "neutral")
        same_sign_but_weaker = current_direction == "neutral" and abs(score_all) >= exit_thr and regime != "range"
        weak_opposite_flip = current_direction in ("long", "short") and current_direction != prev_direction and (
            abs(score_all) < flip_thr or strength_all < 0.18 or coherence < 0.58
        )
        if same_sign_but_weaker or weak_opposite_flip:
            stable["direction"] = prev_direction
            stable["bias"] = prev_direction
            applied = True
            mode = "hysteresis_hold"
            note = "Previous directional thesis is held until the opposite move proves itself across cycles."

    final_direction = str(stable.get("direction") or "neutral")
    if final_direction == "neutral" and raw_bias in ("long", "short") and abs(score_all) >= exit_thr:
        stable["bias"] = raw_bias
    elif final_direction in ("long", "short"):
        stable["bias"] = final_direction

    stable["direction_stability"] = {
        "applied": bool(applied),
        "mode": mode,
        "note": note,
        "previous_direction": prev_direction,
        "fresh_gap_sec": int(max(int(fresh_gap), 60)),
        "enter_threshold": float(enter_thr),
        "exit_threshold": float(exit_thr),
        "flip_threshold": float(flip_thr),
        "range_direction_score_threshold": float(range_dir_score_thr),
        "range_direction_strength_threshold": float(range_dir_strength_thr),
        "range_direction_coherence_threshold": float(range_dir_coh_thr),
        "directional_range_allowed": bool(directional_range_ok),
    }

    state_out = {
        "ts": int(now_ts),
        "direction": str(stable.get("direction") or "neutral"),
        "bias": str(stable.get("bias") or "neutral"),
        "score_all": float(score_all),
        "trendiness": float(trendiness),
        "coherence": float(coherence),
    }
    return stable, state_out


def calibration_policy_contract(settings_obj: Any, risk_limits: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable decision-policy contract attached to each root.

    The model version tracks code semantics.  This descriptor additionally tracks
    deployment settings and active risk limits, so an environment-only threshold
    change cannot silently reuse outcomes or cached coefficients from a different
    selection policy.
    """
    return {
        "schema_version": CALIBRATION_POLICY_SCHEMA_VERSION,
        "recommender_model_version": RECOMMENDER_MODEL_VERSION,
        "outcome_label_version": POLICY_OUTCOME_LABEL_VERSION,
        "feature_schema": list(FEATURE_NAMES),
        "selection": {
            "mean_reversion_min_score": float(
                _clamp(
                    _finite_float(
                        getattr(settings_obj, "mean_reversion_min_score", MEAN_REVERSION_MIN_SCORE_DEFAULT),
                        MEAN_REVERSION_MIN_SCORE_DEFAULT,
                    ),
                    0.0,
                    1.0,
                )
            ),
            "min_score_to_recommend": float(
                _finite_float(getattr(settings_obj, "min_score_to_recommend", 0.08), 0.08)
            ),
            "min_conf_to_recommend": float(
                _clamp(
                    _finite_float(getattr(settings_obj, "min_conf_to_recommend", 0.52), 0.52),
                    0.0,
                    1.0,
                )
            ),
            "require_conf_gate": bool(getattr(settings_obj, "require_conf_gate", True)),
            "calib_min_samples": int(
                max(1, _safe_int_or_none(getattr(settings_obj, "calib_min_samples", 80)) or 80)
            ),
            "taker_fee_bps_linear": float(
                max(0.0, _finite_float(getattr(settings_obj, "taker_fee_bps_linear", 6.0), 6.0))
            ),
            "stale_data_max_sec": int(
                max(1, _safe_int_or_none(getattr(settings_obj, "stale_data_max_sec", 300)) or 300)
            ),
            "reco_republish_cooldown_sec": int(
                max(
                    0,
                    _safe_int_or_none(
                        getattr(settings_obj, "reco_republish_cooldown_sec", 3600)
                    ) or 0,
                )
            ),
            "reco_ttl_sec": (
                _safe_int_or_none(getattr(settings_obj, "reco_ttl_sec", None))
            ),
            "outcome_horizon_fallback_sec": int(
                max(
                    1,
                    _safe_int_or_none(
                        getattr(settings_obj, "outcome_horizon_fallback_sec", 1800)
                    ) or 1800,
                )
            ),
            "venues": sorted(
                str(value).strip().lower()
                for value in (getattr(settings_obj, "venues", ["linear"]) or [])
                if str(value).strip()
            ),
            "symbols_linear": sorted(
                str(value).strip().upper()
                for value in (getattr(settings_obj, "symbols_linear", []) or [])
                if str(value).strip()
            ),
        },
        "calibration": {
            "monetary_cohort": "pre-calibration-candidate-policy-v1",
            "selected_policy_expectancy": "purged-oof-exact-confidence-subset-v2-terminal-money",
            "uncertainty": "student-t-temporal-v1",
            "oof_activation": "purged-whole-timestamp-terminal-v2",
            "terminal_holdout_min_samples": int(
                max(1, _safe_int_or_none(getattr(settings_obj, "calib_min_samples", 80)) or 80)
            ),
            "terminal_holdout_min_decision_cohorts": 5,
            "terminal_selected_policy_min_samples": int(
                max(1, _safe_int_or_none(getattr(settings_obj, "calib_min_samples", 80)) or 80)
            ),
            "terminal_selected_policy_min_decision_cohorts": 5,
            "confidence_selection": "adaptive-blend-context-adjusted-v1",
            "direction_target": "horizon-price-direction-audit-only-v1",
            "label_due_grace_sec": CALIBRATION_LABEL_GRACE_SEC,
        },
        "llm_review": {
            "enabled": bool(getattr(settings_obj, "llm_reviewer_enabled", False)),
            "mode": str(getattr(settings_obj, "llm_reviewer_mode", "advisory") or "advisory"),
            "provider": str(getattr(settings_obj, "llm_reviewer_provider", "ollama") or "ollama"),
            "model": str(getattr(settings_obj, "llm_reviewer_model", "") or ""),
            "prompt_version": PROMPT_VERSION,
            "tf_secs": sorted(
                int(value)
                for value in (getattr(settings_obj, "llm_reviewer_tf_secs", []) or [])
                if _safe_int_or_none(value) is not None and int(value) > 0
            ),
            "candles_per_tf": int(
                max(
                    1,
                    _safe_int_or_none(
                        getattr(
                            settings_obj,
                            "llm_reviewer_candles_per_tf",
                            LLM_REVIEWER_DEFAULT_CANDLES_PER_TF,
                        )
                    ) or LLM_REVIEWER_DEFAULT_CANDLES_PER_TF,
                )
            ),
            "min_confidence": float(
                _clamp(
                    _finite_float(
                        getattr(
                            settings_obj,
                            "llm_reviewer_min_confidence",
                            LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE,
                        ),
                        LLM_REVIEWER_DEFAULT_MIN_CONFIDENCE,
                    ),
                    0.0,
                    1.0,
                )
            ),
            "max_candidates": int(
                max(
                    1,
                    _safe_int_or_none(
                        getattr(
                            settings_obj,
                            "llm_reviewer_max_candidates",
                            LLM_REVIEWER_DEFAULT_MAX_CANDIDATES,
                        )
                    ) or LLM_REVIEWER_DEFAULT_MAX_CANDIDATES,
                )
            ),
            "timeout_sec": int(
                max(
                    1,
                    _safe_int_or_none(
                        getattr(settings_obj, "llm_reviewer_timeout_sec", 60)
                    ) or 60,
                )
            ),
            "cadence_sec": int(
                max(
                    1,
                    _safe_int_or_none(
                        getattr(
                            settings_obj,
                            "llm_reviewer_cadence_sec",
                            LLM_REVIEWER_DEFAULT_CADENCE_SEC,
                        )
                    ) or LLM_REVIEWER_DEFAULT_CADENCE_SEC,
                )
            ),
            "pending_timeout_sec": int(
                max(
                    1,
                    _safe_int_or_none(
                        getattr(
                            settings_obj,
                            "llm_reviewer_pending_timeout_sec",
                            LLM_REVIEWER_DEFAULT_PENDING_TIMEOUT_SEC,
                        )
                    ) or LLM_REVIEWER_DEFAULT_PENDING_TIMEOUT_SEC,
                )
            ),
            "ttl_sec": _safe_int_or_none(
                getattr(settings_obj, "llm_reviewer_ttl_sec", None)
            ),
        },
        "risk_limits": dict(risk_limits or {}),
    }


def calibration_policy_fingerprint(settings_obj: Any, risk_limits: dict[str, Any]) -> str:
    return canonical_policy_fingerprint(
        calibration_policy_contract(settings_obj, risk_limits)
    )


def calibration_policy_contract_fingerprint(contract: Any) -> str:
    """Public verifier for contracts persisted in recommendation audit rows."""
    return canonical_policy_fingerprint(contract)


def calibration_policy_label_due_ts(
    recommendation_ts: Any,
    bot_type: Any,
    *,
    horizon_sec: Any | None = None,
) -> int | None:
    ts = _safe_int_or_none(recommendation_ts)
    horizon = _safe_int_or_none(horizon_sec)
    if horizon is None:
        horizon = BOT_HORIZONS.get(str(bot_type or "").strip())
    if ts is None or ts <= 0 or horizon is None or int(horizon) <= 0:
        return None
    return policy_label_due_ts(
        ts,
        int(horizon),
        grace_sec=CALIBRATION_LABEL_GRACE_SEC,
    )


def policy_calibration_storage_key(base_key: str, policy_fingerprint: str) -> str:
    fingerprint = str(policy_fingerprint or "").strip().lower()
    if not is_sha256_fingerprint(fingerprint):
        raise ValueError("policy_fingerprint must be a sha256 hex digest")
    # Display code may abbreviate the digest; cache correctness may not.
    return f"{base_key}:{fingerprint}"


# Compatibility alias for internal callers and focused regression tests.
_policy_calibration_storage_key = policy_calibration_storage_key


def _matches_current_recommender_model(model_version: Any) -> bool:
    normalized = str(model_version or "").strip()
    return bool(
        normalized == RECOMMENDER_MODEL_VERSION
        or normalized.startswith(RECOMMENDER_MODEL_VERSION + "+")
    )


def _lineage_stats_add(
    stats: dict[str, dict[str, int]],
    row: dict[str, Any],
) -> None:
    bot_type = str(row.get("bot_type") or "").strip()
    if not bot_type:
        return
    success = _safe_int_or_none(row.get("success"))
    stat = stats.setdefault(bot_type, {"total": 0, "wins": 0})
    stat["total"] += 1
    stat["wins"] += 1 if success == 1 else 0


def _lineage_stats_finalize(
    stats: dict[str, dict[str, int]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for bot_type, raw in stats.items():
        total = int(raw.get("total") or 0)
        wins = int(raw.get("wins") or 0)
        losses = max(0, total - wins)
        minority_class_count = min(wins, losses)
        effective_samples = max(0, 2 * minority_class_count)
        win_rate = float(wins / total) if total else None
        if win_rate is None or win_rate <= 0.0 or win_rate >= 1.0:
            class_entropy_bits = 0.0
        else:
            class_entropy_bits = float(
                -(
                    win_rate * math.log2(win_rate)
                    + (1.0 - win_rate) * math.log2(1.0 - win_rate)
                )
            )
        result[bot_type] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "minority_class_count": minority_class_count,
            "effective_samples": effective_samples,
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "class_entropy_bits": round(class_entropy_bits, 4),
        }
    return result


def calibration_lineage_diagnostics(
    rows: Any,
    *,
    policy_fingerprint: str | None = None,
    mean_reversion_min_score: float | None = None,
    retain_rows: bool = True,
    recent_cutoff_ts: int | None = None,
) -> dict[str, Any]:
    """Separate immutable history from the current calibration dataset.

    ``retain_rows=False`` is the bounded status/health mode: lineage counters and
    per-bot statistics are computed in one pass over a streaming iterator without
    retaining every decoded recommendation payload in the API process.
    """
    historical_rows: list[dict[str, Any]] = []
    current_model_rows: list[dict[str, Any]] = []
    feature_eligible_rows: list[dict[str, Any]] = []
    policy_eligible_rows: list[dict[str, Any]] = []
    historical_total = 0
    current_model_total = 0
    feature_eligible_total = 0
    policy_eligible_total = 0
    dropped_old_model = 0
    dropped_invalid_feature_evidence = 0
    dropped_candidate_policy = 0
    dropped_invalid_policy_maturity = 0
    dropped_invalid_policy_contract = 0
    dropped_not_matured = 0
    raw_stats: dict[str, dict[str, dict[str, int]]] = {
        "historical": {},
        "current_model": {},
        "feature_eligible": {},
        "policy_eligible": {},
        "policy_recent": {},
    }
    cutoff = _safe_int_or_none(recent_cutoff_ts)
    threshold = _clamp(
        _finite_float(
            mean_reversion_min_score,
            _finite_float(
                getattr(
                    settings,
                    "mean_reversion_min_score",
                    MEAN_REVERSION_MIN_SCORE_DEFAULT,
                ),
                MEAN_REVERSION_MIN_SCORE_DEFAULT,
            ),
        ),
        0.0,
        1.0,
    )

    source_rows = rows if rows is not None else ()
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        historical_total += 1
        if retain_rows:
            historical_rows.append(row)
        _lineage_stats_add(raw_stats["historical"], row)

        if not _matches_current_recommender_model(row.get("model_version")):
            dropped_old_model += 1
            continue
        current_model_total += 1
        if retain_rows:
            current_model_rows.append(row)
        _lineage_stats_add(raw_stats["current_model"], row)

        reasons = row.get("reasons") or {}
        if not isinstance(reasons, dict):
            dropped_invalid_feature_evidence += 1
            continue
        snapshot = reasons.get("feature_snapshot") or {}
        if not isinstance(snapshot, dict):
            dropped_invalid_feature_evidence += 1
            continue
        bot_type = str(row.get("bot_type") or "").strip()
        if bot_type == "directional_trend":
            trend_flag_raw = snapshot.get("trend_evidence_valid")
            trend_flag = trend_flag_raw is True or _safe_int_or_none(trend_flag_raw) == 1
            trend_strength = _finite_or_none(snapshot.get("trend_strength"))
            trend_coherence = _finite_or_none(snapshot.get("coherence"))
            trend_regime = str(snapshot.get("regime") or "").strip().lower()
            trend_contract = str(snapshot.get("strategy_contract_version") or "").strip()
            trend_label = str(snapshot.get("outcome_label_version") or "").strip()
            if (
                not trend_flag
                or trend_strength is None
                or not (0.0 <= trend_strength <= 1.0)
                or trend_coherence is None
                or not (0.0 <= trend_coherence <= 1.0)
                or trend_regime != "trend"
                or trend_contract != TREND_STRATEGY_CONTRACT_VERSION
                or trend_label != TREND_OUTCOME_LABEL_VERSION
            ):
                dropped_invalid_feature_evidence += 1
                continue
            score = trend_strength
        else:
            evidence_flag = _safe_int_or_none(snapshot.get("mean_reversion_evidence_valid"))
            score = _finite_or_none(snapshot.get("mean_reversion_score"))
            if evidence_flag != 1 or score is None or not (0.0 <= score <= 1.0):
                dropped_invalid_feature_evidence += 1
                continue
        feature_eligible_total += 1
        if retain_rows:
            feature_eligible_rows.append(row)
        _lineage_stats_add(raw_stats["feature_eligible"], row)

        outcome_policy = reasons.get("outcome_policy") or {}
        if not isinstance(outcome_policy, dict):
            outcome_policy = {}
        if bot_type == "directional_trend":
            if (
                str(outcome_policy.get("strategy_family") or "").strip() != "directional_trend"
                or str(outcome_policy.get("bot_outcome_label_version") or "").strip() != TREND_OUTCOME_LABEL_VERSION
                or str(outcome_policy.get("strategy_contract_version") or "").strip() != TREND_STRATEGY_CONTRACT_VERSION
            ):
                dropped_invalid_policy_contract += 1
                continue
        explicit_policy_eligibility = outcome_policy.get("policy_evaluation_eligible")
        if bot_type != "directional_trend" and score < threshold:
            dropped_candidate_policy += 1
            continue
        if explicit_policy_eligibility is not None and explicit_policy_eligibility is not True:
            dropped_candidate_policy += 1
            continue
        if policy_fingerprint is not None:
            stored_fingerprint = str(
                outcome_policy.get("policy_fingerprint") or ""
            ).strip().lower()
            try:
                verified_fingerprint = calibration_policy_contract_fingerprint(
                    outcome_policy.get("policy_contract")
                )
            except ValueError:
                verified_fingerprint = ""
            if (
                not is_sha256_fingerprint(stored_fingerprint)
                or verified_fingerprint != stored_fingerprint
            ):
                dropped_invalid_policy_contract += 1
                continue
            if (
                stored_fingerprint != str(policy_fingerprint)
                or explicit_policy_eligibility is not True
            ):
                dropped_candidate_policy += 1
                continue
            expected_label_due_ts = calibration_policy_label_due_ts(
                row.get("recommendation_ts", row.get("ts")),
                row.get("bot_type"),
                horizon_sec=row.get("horizon_sec"),
            )
            stored_label_due_ts = _safe_int_or_none(outcome_policy.get("label_due_ts"))
            label_available_ts = _safe_int_or_none(row.get("label_available_ts"))
            if (
                expected_label_due_ts is None
                or stored_label_due_ts != expected_label_due_ts
                or label_available_ts is None
                or label_available_ts < expected_label_due_ts
            ):
                dropped_invalid_policy_maturity += 1
                continue
            if max(expected_label_due_ts, label_available_ts) > int(time.time()):
                dropped_not_matured += 1
                continue

        policy_eligible_total += 1
        if retain_rows:
            policy_eligible_rows.append(row)
        _lineage_stats_add(raw_stats["policy_eligible"], row)
        row_ts = _safe_int_or_none(row.get("ts"))
        if cutoff is not None and row_ts is not None and row_ts >= cutoff:
            _lineage_stats_add(raw_stats["policy_recent"], row)

    return {
        "calibration_model_version": RECOMMENDER_MODEL_VERSION,
        "historical_total": historical_total,
        "current_model_total": current_model_total,
        "feature_eligible_total": feature_eligible_total,
        "policy_eligible_total": policy_eligible_total,
        "dropped_old_model": dropped_old_model,
        "dropped_invalid_feature_evidence": dropped_invalid_feature_evidence,
        "dropped_candidate_policy": dropped_candidate_policy,
        "dropped_invalid_policy_maturity": dropped_invalid_policy_maturity,
        "dropped_invalid_policy_contract": dropped_invalid_policy_contract,
        "dropped_not_matured": dropped_not_matured,
        "current_model_rows": current_model_rows,
        "feature_eligible_rows": feature_eligible_rows,
        "policy_eligible_rows": policy_eligible_rows,
        "stats_by_bot": {
            stage: _lineage_stats_finalize(stage_stats)
            for stage, stage_stats in raw_stats.items()
        },
    }


def _current_range_edge_calibration_rows(
    rows: list[dict[str, Any]],
    *,
    policy_fingerprint: str | None = None,
    mean_reversion_min_score: float | None = None,
) -> list[dict[str, Any]]:
    """Return outcomes from the exact pre-calibration candidate policy."""
    return list(calibration_lineage_diagnostics(
        rows,
        policy_fingerprint=policy_fingerprint,
        mean_reversion_min_score=mean_reversion_min_score,
    )["policy_eligible_rows"])


def _apply_outcome_observability_gate(
    model: LogRegScaler,
    diagnostics: dict[str, Any],
) -> LogRegScaler:
    """Attach the full matured-root denominator and apply bounded censoring sensitivity.

    Unresolved or contract-invalid roots remain a hard fail-closed condition.  A
    small number of terminally censored roots no longer destroys a validated
    model automatically: they are assigned a deliberately adverse return and
    the monetary conclusion must remain positive under that sensitivity case.
    This prevents both survivorship bias and permanent liveness loss from one
    unobservable fill path.
    """
    model.policy_fingerprint = str(diagnostics.get("policy_fingerprint") or "")
    model.policy_matured_total = max(0, _safe_int_or_none(diagnostics.get("matured_total")) or 0)
    model.policy_labeled_total = max(0, _safe_int_or_none(diagnostics.get("labeled_total")) or 0)
    model.policy_censored_total = max(0, _safe_int_or_none(diagnostics.get("censored_total")) or 0)
    model.policy_unresolved_total = max(0, _safe_int_or_none(diagnostics.get("unresolved_total")) or 0)
    model.policy_invalid_labeled_total = max(
        max(0, _safe_int_or_none(diagnostics.get("invalid_labeled_total")) or 0),
        max(0, model.policy_labeled_total - max(0, int(model.return_samples or 0))),
    )
    missing_support = (
        max(0, max(0, int(model.return_samples or 0)) - model.policy_matured_total)
        if str(getattr(model, "expectancy_status", "unknown")) == "positive"
        else 0
    )
    model.policy_unresolved_total += missing_support

    matured = max(0, model.policy_matured_total)
    censored = max(0, model.policy_censored_total)
    model.censoring_rate = float(censored / matured) if matured > 0 else 0.0
    model.censoring_sensitivity_status = "not_evaluated"
    model.censoring_assumed_return = None
    model.censoring_adjusted_mean_return = None

    hard_invalid = (
        model.policy_unresolved_total > 0
        or model.policy_invalid_labeled_total > 0
    )
    if hard_invalid:
        model.censoring_sensitivity_status = "hard_block"
    elif censored > 0 and str(getattr(model, "expectancy_status", "unknown")) == "positive":
        mean_ret = _finite_or_none(model.weighted_mean_return)
        lower_bound = _finite_or_none(model.weighted_mean_return_lower_bound)
        temporal_lower = _finite_or_none(model.weighted_temporal_mean_return_lower_bound)
        expected_shortfall = _finite_or_none(model.weighted_expected_shortfall)
        std = _finite_or_none(model.weighted_return_std)
        effective_n = max(1.0, float(model.weighted_effective_return_samples or model.return_samples or 1))
        adverse_candidates = [-0.01]
        if expected_shortfall is not None:
            adverse_candidates.append(float(expected_shortfall))
        if mean_ret is not None and std is not None:
            adverse_candidates.append(float(mean_ret - 3.0 * std))
        assumed = max(-1.0, min(adverse_candidates))
        adjusted = (
            ((float(mean_ret) * effective_n) + (assumed * censored)) / (effective_n + censored)
            if mean_ret is not None else None
        )
        model.censoring_assumed_return = assumed
        model.censoring_adjusted_mean_return = adjusted
        # Censoring above 5% is not treated as ignorable.  Below that threshold,
        # the conclusion must remain positive after assigning every censored root
        # the adverse sensitivity return and all existing lower bounds must pass.
        sensitivity_pass = bool(
            model.censoring_rate <= 0.05
            and adjusted is not None and adjusted > 0.0
            and lower_bound is not None and lower_bound > 0.0
            and temporal_lower is not None and temporal_lower > 0.0
        )
        model.censoring_sensitivity_status = "passed" if sensitivity_pass else "failed"
        hard_invalid = not sensitivity_pass

    if hard_invalid:
        model.expectancy_status = "censored"
        model.fitted = False
        model.coef = []
        model.intercept = 0.0
        model.platt = PlattScaler(fitted=False, saved_ts=int(model.saved_ts or time.time()))
    return model


def _probability_calibration_no_trade_reason(
    model: LogRegScaler | None,
    *,
    require_conf_gate: bool,
) -> dict[str, str] | None:
    if not require_conf_gate:
        return None
    if (
        model is not None
        and bool(model.fitted)
        and len(model.coef) == len(FEATURE_NAMES)
        and bool(model.platt.fitted)
        and str(model.oof_status) == "sufficient"
        and str(model.oof_skill_status) == "accepted"
        and int(model.oof_final_samples) >= int(model.oof_required_final_samples) > 0
        and int(model.oof_final_decision_cohorts)
        >= int(model.oof_required_final_decision_cohorts) > 0
        and str(model.selected_policy_expectancy_status) == "positive"
        and str(model.terminal_selected_policy_expectancy_status) == "positive"
    ):
        return None
    oof_status = str(getattr(model, "oof_status", "not_evaluated") if model else "not_evaluated")
    skill_status = str(
        getattr(model, "oof_skill_status", "not_evaluated") if model else "not_evaluated"
    )
    selected_policy_status = str(
        getattr(model, "selected_policy_expectancy_status", "not_evaluated")
        if model
        else "not_evaluated"
    )
    terminal_selected_policy_status = str(
        getattr(
            model,
            "terminal_selected_policy_expectancy_status",
            "not_evaluated",
        )
        if model
        else "not_evaluated"
    )
    terminal_selected_samples = (
        int(getattr(model, "terminal_selected_policy_samples", 0) or 0)
        if model
        else 0
    )
    terminal_selected_required = (
        int(getattr(model, "terminal_selected_policy_required_samples", 0) or 0)
        if model
        else 0
    )
    final_samples = int(getattr(model, "oof_final_samples", 0) or 0) if model else 0
    final_required = (
        int(getattr(model, "oof_required_final_samples", 0) or 0) if model else 0
    )
    final_cohorts = (
        int(getattr(model, "oof_final_decision_cohorts", 0) or 0) if model else 0
    )
    final_cohorts_required = (
        int(getattr(model, "oof_required_final_decision_cohorts", 0) or 0)
        if model
        else 0
    )
    return {
        "code": "CALIBRATED_CONFIDENCE_UNAVAILABLE",
        "msg": (
            "REQUIRE_CONF_GATE=1, but no bot-specific probability model has "
            "validated held-out skill and positive selected-policy expectancy "
            f"(oof_status={oof_status}, skill={skill_status}, "
            f"terminal={final_samples}/{final_required} rows, "
            f"terminal_cohorts={final_cohorts}/{final_cohorts_required}, "
            f"selected_policy={selected_policy_status}, "
            f"terminal_selected_policy={terminal_selected_policy_status}, "
            f"terminal_selected_rows={terminal_selected_samples}/"
            f"{terminal_selected_required}); "
            "raw confidence remains audit-only"
        ),
    }


def _calibration_expectancy_no_trade_reason(model: LogRegScaler | None) -> dict[str, str] | None:
    """Require positive, uncertainty-bounded monetary evidence before actionability.

    The calibration target is still a proxy outcome rather than live exchange PnL,
    but raw heuristic confidence is not evidence of profitability.  Until the
    bot-specific matured cohort has both the effective sample floor and a positive
    one-sided lower confidence bound, the recommendation remains a shadow
    ``no_trade`` candidate.  This preserves outcome accumulation without exposing
    an unproven strategy as actionable.
    """
    status = str(getattr(model, "expectancy_status", "unknown") if model is not None else "unknown")
    if status == "positive":
        return None
    if status == "censored":
        censored = _safe_int_or_none(
            getattr(model, "policy_censored_total", 0) if model is not None else 0
        ) or 0
        unresolved = _safe_int_or_none(
            getattr(model, "policy_unresolved_total", 0) if model is not None else 0
        ) or 0
        matured = _safe_int_or_none(
            getattr(model, "policy_matured_total", 0) if model is not None else 0
        ) or 0
        invalid_labeled = _safe_int_or_none(
            getattr(model, "policy_invalid_labeled_total", 0) if model is not None else 0
        ) or 0
        return {
            "code": "PROXY_OUTCOME_CENSORING_UNBOUNDED",
            "msg": (
                "current policy cohort contains matured roots without a bounded outcome "
                f"(censored={censored}, unresolved={unresolved}, "
                f"invalid_labeled={invalid_labeled}, matured={matured}); "
                "positive expectancy fails the bounded censoring sensitivity analysis"
            ),
        }
    if status != "negative":
        return_samples = _safe_int_or_none(getattr(model, "return_samples", 0) if model is not None else 0) or 0
        effective_samples = _finite_or_none(
            getattr(model, "weighted_effective_return_samples", None) if model is not None else None
        )
        mean_return = _finite_or_none(
            getattr(model, "weighted_mean_return", None) if model is not None else None
        )
        lower_bound = _finite_or_none(
            getattr(model, "weighted_mean_return_lower_bound", None) if model is not None else None
        )
        temporal_clusters = _safe_int_or_none(
            getattr(model, "temporal_cluster_count", 0) if model is not None else 0
        ) or 0
        minimum_temporal_clusters = _safe_int_or_none(
            getattr(model, "minimum_temporal_clusters", 0) if model is not None else 0
        ) or 0
        temporal_lower_bound = _finite_or_none(
            getattr(model, "weighted_temporal_mean_return_lower_bound", None)
            if model is not None else None
        )
        diagnostics = [f"status={status}", f"n={return_samples}"]
        if effective_samples is not None:
            diagnostics.append(f"n_eff={effective_samples:.1f}")
        if mean_return is not None:
            diagnostics.append(f"mean={mean_return:.4%}")
        if lower_bound is not None:
            diagnostics.append(f"row_lower_bound={lower_bound:.4%}")
        if temporal_clusters or minimum_temporal_clusters:
            diagnostics.append(
                f"time_clusters={temporal_clusters}/{minimum_temporal_clusters}"
            )
        if temporal_lower_bound is not None:
            diagnostics.append(f"time_cluster_lower_bound={temporal_lower_bound:.4%}")
        return {
            "code": "PROXY_MONETARY_EXPECTANCY_UNPROVEN",
            "msg": (
                "bot-specific monetary expectancy is not proven positive under the current "
                f"independent retained sample ({', '.join(diagnostics)}); recommendation remains shadow no-trade"
            ),
        }
    mean_return = _finite_or_none(getattr(model, "weighted_mean_return", None))
    return_samples = _safe_int_or_none(getattr(model, "return_samples", 0)) or 0
    if mean_return is None or return_samples <= 0:
        return {
            "code": "PROXY_MONETARY_EXPECTANCY_NON_POSITIVE",
            "msg": "matured proxy cohort has non-positive monetary expectancy; recommendation remains no-trade",
        }
    expected_shortfall = _finite_or_none(getattr(model, "weighted_expected_shortfall", None))
    tail_note = (
        f", lower-tail expected shortfall={expected_shortfall:.4%}"
        if expected_shortfall is not None
        else ""
    )
    return {
        "code": "PROXY_MONETARY_EXPECTANCY_NON_POSITIVE",
        "msg": (
            f"matured proxy cohort has recency-weighted mean return={mean_return:.4%} "
            f"across n={return_samples}{tail_note}; binary hit rate cannot make this strategy actionable"
        ),
    }


def _calibration_state_persistable(model: LogRegScaler | None) -> bool:
    return bool(
        model is not None
        and (
            model.fitted
            or str(getattr(model, "expectancy_status", "unknown")) in {
                "negative",
                "uncertain",
                "positive",
                "censored",
            }
        )
    )


def _policy_observability_diagnostics(
    conn,
    *,
    policy_fingerprint: str,
    settings_obj: Any,
    bot_type: str | None,
    evidence_context: "_CalibrationEvidenceContext | None" = None,
) -> dict[str, Any]:
    if evidence_context is not None:
        return evidence_context.observability(bot_type)
    return db.get_policy_outcome_observability(
        conn,
        model_version=RECOMMENDER_MODEL_VERSION,
        policy_fingerprint=policy_fingerprint,
        bot_type=bot_type,
        require_llm_verdict=bool(getattr(settings_obj, "llm_reviewer_enabled", False)),
    )


def _apply_strategy_router(recs: list[dict[str, Any]]) -> None:
    """Select one strategy per symbol using comparable monetary evidence.

    Non-winning candidates remain in persistence as paired shadow competitors so
    both bot-specific models keep learning from the same future market. When the
    utility edge is not material, all otherwise-actionable candidates are held as
    ``no_trade`` rather than picking a winner from noise.
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in recs:
        key = (str(rec.get("venue") or ""), str(rec.get("symbol") or ""))
        grouped.setdefault(key, []).append(rec)

    for peers in grouped.values():
        decision = select_strategy(peers)
        winner_rec_id = str(decision.get("winner_rec_id") or "")
        router_status = str(decision.get("status") or "unknown")
        for rec in peers:
            rec_id = str(rec.get("rec_id") or "")
            reasons = rec.setdefault("reasons", {})
            candidate_eval = (decision.get("candidates") or {}).get(rec_id, {})
            reasons["strategy_router"] = {
                "router_version": STRATEGY_ROUTER_VERSION,
                "status": router_status,
                "reason_code": decision.get("reason_code"),
                "winner_rec_id": winner_rec_id or None,
                "winner_bot_type": decision.get("winner_bot_type"),
                "winner_direction": decision.get("winner_direction"),
                "winner_utility": decision.get("winner_utility"),
                "utility_edge": decision.get("utility_edge"),
                "relative_utility_edge": decision.get("relative_utility_edge"),
                "candidate": candidate_eval,
                "selection_basis": (
                    "bot-specific calibrated probability + conservative monetary lower bounds "
                    "+ terminal holdout + expected-shortfall penalty"
                ),
                "raw_score_used": False,
            }
            outcome_policy = reasons.get("outcome_policy") if isinstance(reasons.get("outcome_policy"), dict) else None
            if router_status == "selected":
                if rec_id != winner_rec_id and rec.get("status") in {"recommended", "active", "pending"}:
                    rec["status"] = "suppressed"
                    reasons["suppression"] = {
                        "reason": "strategy_profitability_router",
                        "winner_rec_id": winner_rec_id,
                        "winner_bot_type": decision.get("winner_bot_type"),
                        "winner_utility": decision.get("winner_utility"),
                    }
                    if outcome_policy is not None:
                        outcome_policy["calibration_role"] = "paired_strategy_evaluation"
                        outcome_policy["sample_role"] = "shadow_competitor"
                        outcome_policy["reason"] = "non_winning_strategy_kept_for_paired_outcome"
            elif rec.get("status") in {"recommended", "active", "pending"}:
                rec["status"] = "no_trade"
                reasons.setdefault("no_trade_reasons", []).append({
                    "code": "STRATEGY_ROUTER_NO_CLEAR_WINNER",
                    "msg": (
                        "meta-router не нашёл стратегии с доказанно положительной и достаточно "
                        "лучшей risk-adjusted monetary utility; публикация удержана как no_trade"
                    ),
                })
                if outcome_policy is not None:
                    outcome_policy["calibration_role"] = "paired_strategy_evaluation"
                    outcome_policy["sample_role"] = "shadow_competitor"
                    outcome_policy["reason"] = "router_no_clear_winner_kept_for_paired_outcome"


class _CalibrationEvidenceContext:
    """Lazy, per-recommender-cycle cache for heavy calibration evidence.

    Observability scans remain fail-closed and current, but identical consumers
    no longer repeat them inside one publication cycle.  The 200k-row joined
    outcome dataset is loaded and policy-filtered at most once, with compact
    reason payloads, then explicitly released after calibrators are resolved.
    """

    def __init__(
        self,
        *,
        conn: Any,
        min_samples: int,
        policy_fingerprint: str,
        settings_obj: Any,
    ) -> None:
        self.conn = conn
        self.min_samples = int(min_samples)
        self.policy_fingerprint = str(policy_fingerprint)
        self.settings_obj = settings_obj
        self._policy_rows: list[dict[str, Any]] | None = None
        self._observability: dict[str, dict[str, Any]] = {}

    def observability(self, bot_type: str | None) -> dict[str, Any]:
        key = "*" if bot_type is None else str(bot_type)
        cached = self._observability.get(key)
        if cached is not None:
            return cached
        value = db.get_policy_outcome_observability(
            self.conn,
            model_version=RECOMMENDER_MODEL_VERSION,
            policy_fingerprint=self.policy_fingerprint,
            bot_type=bot_type,
            require_llm_verdict=bool(
                getattr(self.settings_obj, "llm_reviewer_enabled", False)
            ),
        )
        self._observability[key] = value
        return value

    def policy_rows(self) -> list[dict[str, Any]]:
        if self._policy_rows is None:
            rows = db.get_outcomes_with_recs(
                self.conn,
                limit=200_000,
                require_llm_verdict=bool(
                    getattr(self.settings_obj, "llm_reviewer_enabled", False)
                ),
                calibration_compact=True,
            )
            self._policy_rows = _current_range_edge_calibration_rows(
                rows,
                policy_fingerprint=self.policy_fingerprint,
                mean_reversion_min_score=getattr(
                    self.settings_obj,
                    "mean_reversion_min_score",
                    MEAN_REVERSION_MIN_SCORE_DEFAULT,
                ),
            )
        return self._policy_rows

    def release_rows(self) -> None:
        self._policy_rows = None


def _fit_global_logreg(
    conn,
    min_samples: int,
    *,
    policy_fingerprint: str,
    settings_obj: Any,
    observability: dict[str, Any] | None = None,
    policy_rows: list[dict[str, Any]] | None = None,
) -> LogRegScaler:
    """Fit diagnostics only on outcomes admitted by this immutable policy."""
    if policy_rows is None:
        rows = db.get_outcomes_with_recs(
            conn,
            limit=200_000,
            require_llm_verdict=bool(
                getattr(settings_obj, "llm_reviewer_enabled", False)
            ),
            calibration_compact=True,
        )
        selected = _current_range_edge_calibration_rows(
            rows,
            policy_fingerprint=policy_fingerprint,
            mean_reversion_min_score=getattr(
                settings_obj,
                "mean_reversion_min_score",
                MEAN_REVERSION_MIN_SCORE_DEFAULT,
            ),
        )
    else:
        selected = policy_rows
    # The legacy "global" calibrator is a cross-direction diagnostic for the
    # executable futures_grid family, not a pooled model across incompatible
    # mechanics. directional_trend has its own bot-specific calibrator and label.
    selected = [row for row in selected if str(row.get("bot_type") or "") == "futures_grid"]
    model = fit_logreg(
        selected,
        min_samples=min_samples,
        selection_confidence_threshold=(
            float(getattr(settings_obj, "min_conf_to_recommend", 0.52))
            if bool(getattr(settings_obj, "require_conf_gate", True))
            else 0.0
        ),
    )
    evidence = observability or _policy_observability_diagnostics(
        conn,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings_obj,
        bot_type="futures_grid",
    )
    return _apply_outcome_observability_gate(model, evidence)


def _fit_bot_logregs(
    conn,
    min_samples: int,
    *,
    policy_fingerprint: str,
    settings_obj: Any,
    observability_by_bot: dict[str, dict[str, Any]] | None = None,
    policy_rows: list[dict[str, Any]] | None = None,
) -> dict[str, LogRegScaler]:
    """Fit one LogReg+Platt per bot_type."""
    from collections import defaultdict
    if policy_rows is None:
        rows = db.get_outcomes_with_recs(
            conn,
            limit=200_000,
            require_llm_verdict=bool(
                getattr(settings_obj, "llm_reviewer_enabled", False)
            ),
            calibration_compact=True,
        )
        rows = _current_range_edge_calibration_rows(
            rows,
            policy_fingerprint=policy_fingerprint,
            mean_reversion_min_score=getattr(
                settings_obj,
                "mean_reversion_min_score",
                MEAN_REVERSION_MIN_SCORE_DEFAULT,
            ),
        )
    else:
        rows = policy_rows
    data: dict[str, list] = defaultdict(list)
    for row in rows:
        data[row["bot_type"]].append(row)

    result: dict[str, LogRegScaler] = {}
    for bt, bt_rows in data.items():
        if bt in UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS:
            result[bt] = LogRegScaler(fitted=False)
            continue
        model = fit_logreg(
            bt_rows,
            min_samples=min_samples,
            selection_confidence_threshold=(
                float(getattr(settings_obj, "min_conf_to_recommend", 0.52))
                if bool(getattr(settings_obj, "require_conf_gate", True))
                else 0.0
            ),
        )
        evidence = (observability_by_bot or {}).get(bt) or _policy_observability_diagnostics(
            conn,
            policy_fingerprint=policy_fingerprint,
            settings_obj=settings_obj,
            bot_type=bt,
        )
        model = _apply_outcome_observability_gate(model, evidence)
        if _calibration_state_persistable(model):
            key = _policy_calibration_storage_key(
                BOT_CALIB_KEYS.get(bt, f"logreg_{bt}_v1"),
                policy_fingerprint,
            )
            save_logreg_to_db(conn, key, model)
        result[bt] = model
    return result


def _load_or_fit_global_logreg(
    conn,
    min_samples: int,
    *,
    policy_fingerprint: str,
    settings_obj: Any,
    evidence_context: _CalibrationEvidenceContext | None = None,
) -> LogRegScaler:
    """Load global diagnostics, but never keep stale positive evidence fail-open.

    A negative monetary-expectancy state is a conservative veto and may survive a
    temporarily sparse refit until current positive evidence replaces it.  A
    positive/fitted calibrator is different: once its hourly refresh cannot
    reconstruct the required sample, it must become unavailable rather than
    continuing indefinitely after the underlying 14-day rows were pruned.
    """
    import time as _time
    evidence = evidence_context or _CalibrationEvidenceContext(
        conn=conn,
        min_samples=min_samples,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings_obj,
    )
    storage_key = _policy_calibration_storage_key(GLOBAL_LOGREG_KEY, policy_fingerprint)
    observability = _policy_observability_diagnostics(
        conn,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings_obj,
        bot_type="futures_grid",
        evidence_context=evidence,
    )
    saved = load_logreg_from_db(conn, storage_key)
    if saved is not None and str(saved.policy_fingerprint or "") != policy_fingerprint:
        saved = None
    now = int(_time.time())
    if saved is not None and int(saved.saved_ts) > 0:
        if now - int(saved.saved_ts) < CALIB_REFIT_INTERVAL_SEC:
            return _apply_outcome_observability_gate(saved, observability)
    model = _fit_global_logreg(
        conn,
        min_samples=min_samples,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings_obj,
        observability=observability,
        policy_rows=evidence.policy_rows(),
    )
    if (
        saved is not None
        and str(getattr(saved, "expectancy_status", "unknown")) == "negative"
        and str(getattr(model, "expectancy_status", "unknown")) != "positive"
    ):
        return _apply_outcome_observability_gate(saved, observability)
    if _calibration_state_persistable(model):
        save_logreg_to_db(conn, storage_key, model)
        return model
    # Persist the current insufficient state so a restart cannot resurrect the
    # stale positive coefficients from app_config before the next refit.
    save_logreg_to_db(conn, storage_key, model)
    return model


def _load_or_fit_bot_logregs(
    conn,
    min_samples: int,
    *,
    policy_fingerprint: str,
    settings_obj: Any,
    evidence_context: _CalibrationEvidenceContext | None = None,
) -> dict[str, LogRegScaler]:
    """Load per-bot calibrators and expire unreconstructable positive evidence.

    Stale negative expectancy remains a conservative no-trade veto.  Stale
    positive/fitted evidence may not survive when the current retained outcome
    window is insufficient, because doing so turns a bounded cache into an
    immortal probability model detached from its supporting rows.
    """
    import time as _time
    evidence = evidence_context or _CalibrationEvidenceContext(
        conn=conn,
        min_samples=min_samples,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings_obj,
    )
    now = int(_time.time())
    calibrators: dict[str, LogRegScaler] = {}
    saved_by_bot: dict[str, LogRegScaler | None] = {}
    storage_keys: dict[str, str] = {}
    observability_by_bot: dict[str, dict[str, Any]] = {}
    needs_refit: list[str] = []

    for bt, base_key in BOT_CALIB_KEYS.items():
        key = _policy_calibration_storage_key(base_key, policy_fingerprint)
        storage_keys[bt] = key
        observability = _policy_observability_diagnostics(
            conn,
            policy_fingerprint=policy_fingerprint,
            settings_obj=settings_obj,
            bot_type=bt,
            evidence_context=evidence,
        )
        observability_by_bot[bt] = observability
        if bt in UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS:
            calibrators[bt] = LogRegScaler(fitted=False)
            continue
        saved = load_logreg_from_db(conn, key)
        if saved is not None and str(saved.policy_fingerprint or "") != policy_fingerprint:
            saved = None
        saved_by_bot[bt] = saved
        if saved is not None and int(saved.saved_ts) > 0:
            if now - int(saved.saved_ts) < CALIB_REFIT_INTERVAL_SEC:
                calibrators[bt] = _apply_outcome_observability_gate(saved, observability)
                continue
        calibrators[bt] = LogRegScaler(
            fitted=False,
            saved_ts=now,
            expectancy_status="insufficient",
        )
        needs_refit.append(bt)

    if needs_refit:
        policy_rows = evidence.policy_rows()
        bots_with_evidence = {
            str(row.get("bot_type") or "").strip()
            for row in policy_rows
            if isinstance(row, dict)
        }
        refittable_bots = [
            bt
            for bt in needs_refit
            if (
                bt != "directional_trend"
                or saved_by_bot.get(bt) is not None
                or bt in bots_with_evidence
            )
        ]
        fitted: dict[str, LogRegScaler] = {}
        if refittable_bots:
            refittable_set = set(refittable_bots)
            fitted = _fit_bot_logregs(
                conn,
                min_samples,
                policy_fingerprint=policy_fingerprint,
                settings_obj=settings_obj,
                observability_by_bot=observability_by_bot,
                policy_rows=[
                    row
                    for row in policy_rows
                    if str(row.get("bot_type") or "").strip() in refittable_set
                ],
            )
        for bt in needs_refit:
            candidate = fitted.get(bt) or LogRegScaler(
                fitted=False,
                saved_ts=now,
                expectancy_status="insufficient",
            )
            saved = saved_by_bot.get(bt)
            if (
                saved is not None
                and str(getattr(saved, "expectancy_status", "unknown")) == "negative"
                and str(getattr(candidate, "expectancy_status", "unknown")) != "positive"
            ):
                calibrators[bt] = _apply_outcome_observability_gate(
                    saved,
                    observability_by_bot[bt],
                )
                continue
            if _calibration_state_persistable(candidate):
                calibrators[bt] = candidate
                continue
            calibrators[bt] = candidate
            save_logreg_to_db(conn, storage_keys[bt], candidate)

    return calibrators


def _load_or_fit_trend_event_model(
    conn,
    min_samples: int,
    *,
    policy_fingerprint: str,
    evidence_context: _CalibrationEvidenceContext,
) -> TrendEventModel:
    """Load or fit the v2 first-touch event model on exact trend lineage only."""
    key = trend_event_storage_key(policy_fingerprint)
    now = int(time.time())
    saved = load_trend_event_model(conn, key)
    if (
        saved is not None
        and str(saved.policy_fingerprint or "") == policy_fingerprint
        and str(saved.outcome_label_version or "") == TREND_OUTCOME_LABEL_VERSION
        and int(saved.saved_ts or 0) > 0
        and now - int(saved.saved_ts) < CALIB_REFIT_INTERVAL_SEC
    ):
        return saved
    rows = [
        row
        for row in evidence_context.policy_rows()
        if str(row.get("bot_type") or "") == "directional_trend"
        and str(row.get("event_type") or "").strip().upper()
        in {"TP_FIRST", "SL_FIRST", "HORIZON_EXIT"}
    ]
    model = fit_trend_event_model(
        rows,
        min_samples=min_samples,
        policy_fingerprint=policy_fingerprint,
        outcome_label_version=TREND_OUTCOME_LABEL_VERSION,
        horizon_sec=12 * 3600,
    )
    save_trend_event_model(conn, key, model)
    return model


def _raw_direction_confidence(direction_agg: dict[str, Any]) -> float:
    """Monotonic signal for directional success probability.

    Use raw direction_confidence (0..1) rather than the signed aggregate score.
    A signed score is unsuitable for 1D Platt calibration because successful shorts
    naturally have negative scores and get mixed together with failed longs.
    """
    x = direction_agg.get("direction_confidence")
    if x is None:
        # Fallback: derive from unsigned strength if raw confidence is absent.
        x = (direction_agg.get("strength") or {}).get("all", 0.0)
    return float(_clamp(float(x), 0.0, 1.0))


def _direction_confidence_projection(
    direction_agg: dict[str, Any],
    scaler: PlattScaler | None,
) -> dict[str, Any]:
    """Keep an in-sample directional Platt fit outside decision features.

    The standalone directional model has no chronological skill gate.  Letting it
    rewrite ``dir_conf`` would make the feature distribution depend on a mutable
    cache fitted with later outcomes.  The raw pre-decision confidence is therefore
    the only inference feature; the Platt value is retained for audit comparison.
    """
    raw = _raw_direction_confidence(direction_agg)
    audit_probability = (
        float(scaler.predict(raw))
        if scaler is not None and bool(scaler.fitted)
        else None
    )
    return {
        "feature_value": raw,
        "audit_probability": audit_probability,
        "used_for_inference": False,
        "reason": "direction_platt_has_no_chronological_skill_gate",
    }


def _fit_direction_calibrator(
    conn,
    min_samples: int,
    *,
    policy_fingerprint: str | None = None,
    settings_obj: Any | None = None,
    policy_rows: list[dict[str, Any]] | None = None,
) -> PlattScaler:
    """Fit direction calibrator on supported directional outcomes.

    We calibrate the *raw direction confidence* (or unsigned strength fallback),
    not the signed aggregate score. This preserves symmetry between strong longs
    and strong shorts and makes the resulting value a true probability-like metric.
    """
    active_settings = settings_obj or settings
    if policy_rows is None:
        rows = db.get_outcomes_with_recs(
            conn,
            limit=200_000,
            require_llm_verdict=bool(
                getattr(active_settings, "llm_reviewer_enabled", False)
            ),
            calibration_compact=True,
        )
        rows = _current_range_edge_calibration_rows(
            rows,
            policy_fingerprint=policy_fingerprint,
            mean_reversion_min_score=getattr(
                active_settings,
                "mean_reversion_min_score",
                MEAN_REVERSION_MIN_SCORE_DEFAULT,
            ),
        )
    else:
        rows = policy_rows
    xs, ys = [], []
    for row in rows:
        if row["bot_type"] != "futures_grid":
            continue
        d = (row.get("reasons") or {}).get("direction_agg") or {}
        direction = str(d.get("direction") or "neutral").strip().lower()
        if direction not in {"long", "short"}:
            continue
        entry = _finite_or_none(row.get("entry_close"))
        exit_price = _finite_or_none(row.get("exit_close"))
        if entry is None or exit_price is None or entry <= 0.0 or exit_price <= 0.0:
            continue
        xs.append(_raw_direction_confidence(d))
        ys.append(int(exit_price > entry) if direction == "long" else int(exit_price < entry))
    scaler = (
        fit_platt(xs, ys, min_samples=min_samples)
        if len(xs) >= min_samples
        else PlattScaler(fitted=False)
    )
    scaler.policy_fingerprint = str(policy_fingerprint or "")
    return scaler


def _load_or_fit_direction_calibrator(
    conn,
    min_samples: int,
    *,
    policy_fingerprint: str,
    settings_obj: Any,
    evidence_context: _CalibrationEvidenceContext | None = None,
) -> PlattScaler:
    """Load direction calibration without resurrecting stale fitted coefficients."""
    import time as _time
    evidence = evidence_context or _CalibrationEvidenceContext(
        conn=conn,
        min_samples=min_samples,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings_obj,
    )
    key = _policy_calibration_storage_key(DIRECTION_CALIBRATION_KEY, policy_fingerprint)
    observability = _policy_observability_diagnostics(
        conn,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings_obj,
        bot_type="futures_grid",
        evidence_context=evidence,
    )
    if (
        int(observability.get("censored_total") or 0) > 0
        or int(observability.get("unresolved_total") or 0) > 0
        or int(observability.get("invalid_labeled_total") or 0) > 0
    ):
        scaler = PlattScaler(
            fitted=False,
            saved_ts=int(_time.time()),
            policy_fingerprint=policy_fingerprint,
        )
        save_platt_to_db(conn, key, scaler)
        return scaler
    saved = load_platt_from_db(conn, key)
    if saved is not None and str(saved.policy_fingerprint or "") != policy_fingerprint:
        saved = None
    if saved is not None and int(saved.saved_ts) > 0:
        if int(_time.time()) - int(saved.saved_ts) < CALIB_REFIT_INTERVAL_SEC:
            return saved
    scaler = _fit_direction_calibrator(
        conn,
        min_samples=min_samples,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings_obj,
        policy_rows=evidence.policy_rows(),
    )
    scaler.policy_fingerprint = policy_fingerprint
    # Persist both fitted and current unfitted states.  Otherwise a restart can
    # reload the old fitted payload even though the latest evidence was sparse.
    save_platt_to_db(conn, key, scaler)
    return scaler


def _recent_publication_dedupe_material_upgrade(prev: dict[str, Any] | None, rec: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    prev = prev or {}
    prev_score = _finite_float(prev.get("score"), 0.0)
    prev_conf = _finite_float(prev.get("confidence"), 0.0)
    prev_rr = _finite_float(prev.get("expected_rr"), 0.0)
    new_score = _finite_float(rec.get("score"), 0.0)
    new_conf = _finite_float(rec.get("confidence"), 0.0)
    new_rr = _finite_float(rec.get("expected_rr"), 0.0)

    prev_entry = (((prev.get("params") or {}).get("trade_plan") or {}).get("entry_price"))
    new_entry = (((rec.get("params") or {}).get("trade_plan") or {}).get("entry_price"))
    try:
        prev_entry_f = _finite_or_none(prev_entry) if prev_entry not in (None, "") else None
    except Exception:
        prev_entry_f = None
    try:
        new_entry_f = _finite_or_none(new_entry) if new_entry not in (None, "") else None
    except Exception:
        new_entry_f = None
    entry_shift_pct = None
    if prev_entry_f and prev_entry_f > 0 and new_entry_f and new_entry_f > 0:
        entry_shift_pct = abs(new_entry_f - prev_entry_f) / prev_entry_f * 100.0

    diagnostics = {
        "score_delta": round(new_score - prev_score, 6),
        "confidence_delta": round(new_conf - prev_conf, 6),
        "expected_rr_delta": round(new_rr - prev_rr, 6),
        "entry_shift_pct": round(entry_shift_pct, 4) if entry_shift_pct is not None else None,
    }
    material = (
        diagnostics["score_delta"] >= 0.08
        or diagnostics["confidence_delta"] >= 0.05
        or diagnostics["expected_rr_delta"] >= 0.08
        or ((entry_shift_pct or 0.0) >= 1.0)
    )
    return bool(material), diagnostics


def _recommendation_row_is_publication_actionable(row: Any) -> bool:
    """Treat async-LLM held `pending` rows as actionable publication-chain members.

    Without this, each recommender cycle that runs before the async reviewer finishes
    sees the previous signal as non-actionable and opens a brand new outcome-root.
    When the reviewer later restores those rows back to recommended/active, the UI
    and outcome stats show a train of near-identical roots every 1–3 minutes.
    """
    status_value = row.get("status") if isinstance(row, dict) else row["status"]
    status_norm = str(status_value or "").strip().lower()
    if status_norm in {"recommended", "active", "executed"}:
        return True
    if status_norm != "pending":
        return False
    reasons_json = row.get("reasons_json") if isinstance(row, dict) else row["reasons_json"]
    reasons = db._json_loads_mapping_or_default(reasons_json, {})
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else {}
    target = str(llm_review.get("publish_target_status") or "").strip().lower()
    return target in LLM_REVIEW_ELIGIBLE_STATUSES


def _recommendation_row_is_open_outcome_root(row: Any) -> bool:
    """Return whether a persisted root still represents an actionable label sample.

    Operator TTL may have changed the row status to ``expired`` while its proxy
    position is still inside the label horizon.  That expiration must prevent
    execution, but it must not create a second overlapping outcome root.
    """
    if _recommendation_row_is_publication_actionable(row):
        return True
    status_value = row.get("status") if isinstance(row, dict) else row["status"]
    if str(status_value or "").strip().lower() != "expired":
        return False
    # ``expired`` is written only by the TTL worker from recommended/active/pending
    # states.  Legacy rows may predate materialized outcome-policy columns, so the
    # status transition itself is the durable evidence that this was an operator
    # publication rather than a blocked/no-trade sample.
    return True


def _publication_row_payload(row: Any) -> dict[str, Any]:
    rec_id = str(row["rec_id"] or "")
    publication_root_rec_id = str(
        row["publication_root_rec_id"] or rec_id
    ).strip() or rec_id
    outcome_root_rec_id = str(
        row["outcome_root_rec_id"] or publication_root_rec_id
    ).strip() or publication_root_rec_id
    return {
        "rec_id": rec_id,
        "ts": row["ts"],
        "score": row["score"],
        "confidence": row["confidence"],
        "expected_rr": row["expected_rr"],
        "status": row["status"],
        "params": db._json_loads_or_default(row["params_json"], {}),
        "publication_root_rec_id": publication_root_rec_id,
        "outcome_root_rec_id": outcome_root_rec_id,
        "is_outcome_label_root": bool(int(row["is_outcome_label_root"] or 0)),
    }


def _find_latest_live_publication(
    conn, rec: dict[str, Any], ts_now: int
) -> dict[str, Any] | None:
    """Find the newest unexpired operator publication chain for the same idea."""
    cur = conn.execute(
        """SELECT * FROM recommendations
           WHERE venue=? AND symbol=? AND bot_type=? AND direction=?
             AND status IN ('recommended','active','executed','pending') AND ts < ?
           ORDER BY ts DESC LIMIT 64""",
        (
            str(rec.get("venue") or ""),
            str(rec.get("symbol") or ""),
            str(rec.get("bot_type") or ""),
            str(rec.get("direction") or "neutral"),
            int(ts_now),
        ),
    )
    for row in cur.fetchall():
        if not _recommendation_row_is_publication_actionable(row):
            continue
        payload = _publication_row_payload(row)
        expiry = db.recommendation_chain_expiry_context(
            conn,
            rec_id=str(row["rec_id"]),
            publication_root_rec_id=payload["publication_root_rec_id"],
            row_ts=row["ts"],
            ttl_sec=row["ttl_sec"],
            ts_now=ts_now,
        )
        if not expiry.get("is_publication_chain_expired"):
            return payload
    return None



def _find_recent_publication(conn, rec: dict[str, Any], ts_now: int, cooldown_sec: int) -> dict[str, Any] | None:
    if cooldown_sec <= 0:
        return None
    cutoff_ts = max(0, int(ts_now) - int(cooldown_sec))
    cur = conn.execute(
        """SELECT * FROM recommendations
           WHERE venue=? AND symbol=? AND bot_type=? AND direction=?
             AND status IN ('recommended','active','executed','pending') AND ts >= ? AND ts < ?
           ORDER BY ts DESC LIMIT 32""",
        (
            str(rec.get("venue") or ""),
            str(rec.get("symbol") or ""),
            str(rec.get("bot_type") or ""),
            str(rec.get("direction") or "neutral"),
            cutoff_ts,
            int(ts_now),
        ),
    )
    for row in cur.fetchall():
        if not _recommendation_row_is_publication_actionable(row):
            continue
        payload = _publication_row_payload(row)
        expiry = db.recommendation_chain_expiry_context(
            conn,
            rec_id=str(row["rec_id"]),
            publication_root_rec_id=payload["publication_root_rec_id"],
            row_ts=row["ts"],
            ttl_sec=row["ttl_sec"],
            ts_now=ts_now,
        )
        if expiry.get("is_publication_chain_expired"):
            continue
        return payload
    return None


def _is_shadow_no_trade_outcome_candidate(rec: Any) -> bool:
    """Return True only for explicit counterfactual no-trade sampling rows.

    These rows are research observations, not actionable publications, but their
    label horizons still represent one pseudo-position.  Treating every recommender
    cycle as an independent root creates dozens of overlapping labels from the same
    market path and invalidates calibration sample counts.
    """
    if not isinstance(rec, dict) or str(rec.get("status") or "").strip().lower() != "no_trade":
        return False
    reasons = rec.get("reasons") or {}
    if not isinstance(reasons, dict):
        return False
    policy = reasons.get("outcome_policy") or {}
    return bool(
        isinstance(policy, dict)
        and policy.get("eligible") is True
        and str(policy.get("sample_role") or "").strip().lower() == "shadow_no_trade"
    )


def _stored_row_is_shadow_no_trade_outcome_root(row: Any) -> bool:
    status = row.get("status") if isinstance(row, dict) else row["status"]
    if str(status or "").strip().lower() != "no_trade":
        return False
    reasons_json = row.get("reasons_json") if isinstance(row, dict) else row["reasons_json"]
    reasons = db._json_loads_mapping_or_default(reasons_json, {})
    policy = reasons.get("outcome_policy") if isinstance(reasons, dict) else {}
    return bool(
        isinstance(policy, dict)
        and policy.get("eligible") is True
        and str(policy.get("sample_role") or "").strip().lower() == "shadow_no_trade"
    )


def _find_open_shadow_outcome_root(
    conn, rec: dict[str, Any], ts_now: int, fallback_horizon_sec: int
) -> dict[str, Any] | None:
    """Find the one still-open counterfactual root for this exact shadow cohort.

    Recommendation TTL is intentionally irrelevant here: the research observation
    remains open until its label horizon ends.  A new root is allowed after the
    horizon or after the previous root already has an outcome.
    """
    cur = conn.execute(
        """SELECT r.rec_id, r.ts, r.features_ref_ts, r.bot_type, r.status,
                  r.params_json, r.reasons_json, r.model_version,
                  r.publication_root_rec_id, r.outcome_root_rec_id,
                  r.is_outcome_label_root
           FROM recommendations r
           LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
           WHERE r.venue=? AND r.symbol=? AND r.bot_type=? AND r.direction=?
             AND r.model_version=?
             AND COALESCE(r.is_outcome_label_root, 1) = 1
             AND o.rec_id IS NULL
             AND r.status='no_trade'
             AND r.ts < ?
           ORDER BY r.ts DESC LIMIT 16""",
        (
            str(rec.get("venue") or ""),
            str(rec.get("symbol") or ""),
            str(rec.get("bot_type") or ""),
            str(rec.get("direction") or "neutral"),
            str(rec.get("model_version") or ""),
            int(ts_now),
        ),
    )
    for row in cur.fetchall():
        if not _stored_row_is_shadow_no_trade_outcome_root(row):
            continue
        params = db._json_loads_or_default(row["params_json"], {})
        effective_horizon_sec, _ = _resolve_effective_horizon(
            str(row["bot_type"] or rec.get("bot_type") or ""),
            params if isinstance(params, dict) else {},
            int(fallback_horizon_sec),
        )
        signal_ref_ts = max(
            _safe_int_or_none(row["ts"]) or 0,
            _safe_int_or_none(row["features_ref_ts"]) or 0,
        )
        tradeable_ts = _first_tradeable_1m_candle_ts(
            conn,
            str(rec.get("venue") or ""),
            str(rec.get("symbol") or ""),
            int(signal_ref_ts),
        )
        pseudo_entry_ts = int(tradeable_ts) if tradeable_ts is not None else int(signal_ref_ts) + 60
        lock_until_ts = int(pseudo_entry_ts) + int(effective_horizon_sec)
        if int(ts_now) >= lock_until_ts:
            continue
        publication_root_id = str(
            row["publication_root_rec_id"] or row["rec_id"]
        ).strip() or str(row["rec_id"])
        outcome_root_id = str(
            row["outcome_root_rec_id"] or row["rec_id"]
        ).strip() or str(row["rec_id"])
        return {
            "rec_id": row["rec_id"],
            "publication_root_rec_id": publication_root_id,
            "outcome_root_rec_id": outcome_root_id,
            "effective_horizon_sec": int(effective_horizon_sec),
            "lock_until_ts": int(lock_until_ts),
        }
    return None


def _find_open_publication_position(conn, rec: dict[str, Any], ts_now: int, fallback_horizon_sec: int) -> dict[str, Any] | None:
    """Находит незавершённую псевдо-сделку по той же execution-chain.

    Для outcome-labeling важно имитировать реальную торговлю, а не поток идей.
    Поэтому same-direction сигнал по тому же `(venue, symbol, bot_type)` не должен
    открывать новый root, пока предыдущая корневая идея ещё находится внутри
    своего окна label-horizon.

    Важная деталь: нельзя блокировать только по отсутствию записи в `reco_outcomes`.
    Если цикл labeler временно не смог проставить outcome из-за дырки в market-data,
    бесконечный lock сломает публикацию навсегда. Поэтому lock держим до horizon, а не
    до бесконечности: после истечения окна система сможет открыть новый root даже если
    старый outcome так и не был вычислен.
    """
    cur = conn.execute(
        """SELECT r.rec_id, r.ts, r.ttl_sec, r.features_ref_ts, r.bot_type, r.status,
                  r.score, r.confidence, r.expected_rr,
                  r.params_json, r.reasons_json, r.publication_root_rec_id,
                  r.outcome_root_rec_id, r.is_outcome_label_root,
                  r.outcome_eligible, r.outcome_sample_role
           FROM recommendations r
           LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
           WHERE r.venue=? AND r.symbol=? AND r.bot_type=? AND r.direction=?
             AND COALESCE(r.is_outcome_label_root, 1) = 1
             AND o.rec_id IS NULL
             AND r.status NOT IN ('blocked', 'no_trade', 'suppressed', 'ignored')
             AND r.ts < ?
           ORDER BY r.ts DESC LIMIT 16""",
        (
            str(rec.get("venue") or ""),
            str(rec.get("symbol") or ""),
            str(rec.get("bot_type") or ""),
            str(rec.get("direction") or "neutral"),
            int(ts_now),
        ),
    )
    rows = cur.fetchall()
    if not rows:
        return None

    for row in rows:
        if not _recommendation_row_is_open_outcome_root(row):
            continue
        params = db._json_loads_or_default(row["params_json"], {})
        effective_horizon_sec, _ = _resolve_effective_horizon(
            str(row["bot_type"] or rec.get("bot_type") or ""),
            params if isinstance(params, dict) else {},
            int(fallback_horizon_sec),
        )
        # Для grid-логики pseudo-entry начинается на первом tradeable 1m candle после
        # signal ref ts. Если такая candle уже есть в БД, используем её реальный ts;
        # иначе временно деградируем к прежней консервативной аппроксимации +60s.
        signal_ref_ts = max(
            _safe_int_or_none(row["ts"]) or 0,
            _safe_int_or_none(row["features_ref_ts"]) or 0,
        )
        tradeable_ts = _first_tradeable_1m_candle_ts(
            conn,
            str(rec.get("venue") or ""),
            str(rec.get("symbol") or ""),
            int(signal_ref_ts),
        )
        pseudo_entry_ts = int(tradeable_ts) if tradeable_ts is not None else int(signal_ref_ts) + 60
        lock_until_ts = int(pseudo_entry_ts) + int(effective_horizon_sec)
        if int(ts_now) >= lock_until_ts:
            continue
        publication_root_rec_id = str(
            row["publication_root_rec_id"] or row["rec_id"]
        ).strip() or str(row["rec_id"])
        outcome_root_rec_id = str(
            row["outcome_root_rec_id"] or row["rec_id"]
        ).strip() or str(row["rec_id"])
        return {
            "rec_id": row["rec_id"],
            "ts": row["ts"],
            "score": row["score"],
            "confidence": row["confidence"],
            "expected_rr": row["expected_rr"],
            "status": row["status"],
            "params": params if isinstance(params, dict) else {},
            "publication_root_rec_id": publication_root_rec_id,
            "outcome_root_rec_id": outcome_root_rec_id,
            "is_outcome_label_root": True,
            "open_position_lock": True,
            "effective_horizon_sec": int(effective_horizon_sec),
            "lock_until_ts": int(lock_until_ts),
        }
    return None


def _apply_recent_publication_dedupe(conn, recs: list[dict[str, Any]], settings, ts_now: int) -> None:
    cooldown_sec = max(0, int(getattr(settings, "reco_republish_cooldown_sec", 0) or 0))
    fallback_horizon_sec = max(
        300,
        int(getattr(settings, "outcome_horizon_fallback_sec", min(BOT_HORIZONS.values())) or min(BOT_HORIZONS.values())),
    )
    for rec in recs:
        if _is_shadow_no_trade_outcome_candidate(rec):
            prev_shadow_root = _find_open_shadow_outcome_root(
                conn, rec, ts_now, fallback_horizon_sec
            )
            if prev_shadow_root is not None:
                reasons = rec.setdefault("reasons", {})
                previous_root_rec_id = str(
                    prev_shadow_root.get("publication_root_rec_id")
                    or prev_shadow_root.get("rec_id")
                    or ""
                ).strip()
                reasons["publication_dedupe"] = {
                    "previous_rec_id": prev_shadow_root.get("rec_id"),
                    "previous_root_rec_id": previous_root_rec_id,
                    "decision": "reuse_shadow_root",
                    "active_reuse": False,
                    "shadow_reuse": True,
                    "suppressed": False,
                    "material_upgrade": False,
                    "open_position_lock": True,
                    "lock_reason": "existing_shadow_pseudo_position",
                    "effective_horizon_sec": int(prev_shadow_root.get("effective_horizon_sec") or 0),
                    "lock_until_ts": int(prev_shadow_root.get("lock_until_ts") or 0),
                }
                rec["status"] = "no_trade"
                rec["publication_root_rec_id"] = previous_root_rec_id
                rec["outcome_root_rec_id"] = str(
                    prev_shadow_root.get("outcome_root_rec_id")
                    or prev_shadow_root.get("rec_id")
                    or previous_root_rec_id
                ).strip() or previous_root_rec_id
                rec["is_outcome_label_root"] = False
            continue

        if not _is_llm_review_eligible_status(rec.get("status")):
            continue

        prev_open_root = _find_open_publication_position(
            conn, rec, ts_now, fallback_horizon_sec
        )
        if prev_open_root is not None:
            live_publication = _find_latest_live_publication(conn, rec, ts_now)
            comparison_base = live_publication or prev_open_root
            _material_upgrade_ignored, diagnostics = _recent_publication_dedupe_material_upgrade(
                comparison_base, rec
            )
            reasons = rec.setdefault("reasons", {})
            outcome_root_rec_id = str(
                prev_open_root.get("outcome_root_rec_id")
                or prev_open_root.get("rec_id")
                or ""
            ).strip() or str(prev_open_root.get("rec_id") or "")

            if live_publication is not None:
                previous_publication_root = str(
                    live_publication.get("publication_root_rec_id")
                    or live_publication.get("rec_id")
                    or ""
                ).strip() or str(live_publication.get("rec_id") or "")
                reasons["publication_dedupe"] = {
                    "cooldown_sec": int(cooldown_sec),
                    "previous_rec_id": live_publication.get("rec_id"),
                    "previous_root_rec_id": previous_publication_root,
                    "previous_outcome_root_rec_id": outcome_root_rec_id,
                    "previous_ts": live_publication.get("ts"),
                    "previous_status": live_publication.get("status"),
                    "decision": "reuse_active",
                    "active_reuse": True,
                    "operator_chain_reset": False,
                    "suppressed": False,
                    "material_upgrade": False,
                    "open_position_lock": True,
                    "lock_reason": "existing_same_direction_pseudo_position",
                    "effective_horizon_sec": int(prev_open_root.get("effective_horizon_sec") or 0),
                    "lock_until_ts": int(prev_open_root.get("lock_until_ts") or 0),
                    **diagnostics,
                }
                rec["status"] = "active"
                rec["publication_root_rec_id"] = previous_publication_root
                rec["outcome_root_rec_id"] = outcome_root_rec_id
                rec["is_outcome_label_root"] = False
                continue

            # The prior operator publication has expired, but its statistical
            # pseudo-position has not. Publish a fresh actionable operator root
            # without manufacturing a second overlapping label sample.
            current_rec_id = str(rec.get("rec_id") or "").strip()
            reasons["publication_dedupe"] = {
                "cooldown_sec": int(cooldown_sec),
                "previous_rec_id": prev_open_root.get("rec_id"),
                "previous_root_rec_id": prev_open_root.get("publication_root_rec_id"),
                "previous_outcome_root_rec_id": outcome_root_rec_id,
                "previous_ts": prev_open_root.get("ts"),
                "previous_status": prev_open_root.get("status"),
                "decision": "publish_fresh_operator_root",
                "active_reuse": False,
                "operator_chain_reset": True,
                "suppressed": False,
                "material_upgrade": False,
                "open_position_lock": True,
                "lock_reason": "existing_same_direction_pseudo_position",
                "effective_horizon_sec": int(prev_open_root.get("effective_horizon_sec") or 0),
                "lock_until_ts": int(prev_open_root.get("lock_until_ts") or 0),
                **diagnostics,
            }
            rec["publication_root_rec_id"] = current_rec_id
            rec["outcome_root_rec_id"] = outcome_root_rec_id
            rec["is_outcome_label_root"] = False
            continue

        if cooldown_sec <= 0:
            continue

        prev = _find_recent_publication(conn, rec, ts_now, cooldown_sec)
        if prev is None:
            continue
        material_upgrade, diagnostics = _recent_publication_dedupe_material_upgrade(prev, rec)
        reasons = rec.setdefault("reasons", {})
        previous_root_rec_id = str(prev.get("publication_root_rec_id") or prev.get("rec_id") or "").strip() or str(prev.get("rec_id") or "")
        reasons["publication_dedupe"] = {
            "cooldown_sec": int(cooldown_sec),
            "previous_rec_id": prev.get("rec_id"),
            "previous_root_rec_id": previous_root_rec_id,
            "previous_ts": prev.get("ts"),
            "previous_status": prev.get("status"),
            "decision": "publish_new" if material_upgrade else "reuse_active",
            "active_reuse": not material_upgrade,
            "suppressed": False,
            "material_upgrade": bool(material_upgrade),
            "open_position_lock": False,
            **diagnostics,
        }
        if material_upgrade:
            rec["publication_root_rec_id"] = str(rec.get("rec_id") or "")
            rec["outcome_root_rec_id"] = str(rec.get("rec_id") or "")
            rec["is_outcome_label_root"] = True
        else:
            rec["status"] = "active"
            rec["publication_root_rec_id"] = previous_root_rec_id
            rec["outcome_root_rec_id"] = str(
                prev.get("outcome_root_rec_id")
                or prev.get("rec_id")
                or previous_root_rec_id
            ).strip() or previous_root_rec_id
            rec["is_outcome_label_root"] = False



def run_recommender_once(conn, settings, *, heartbeat=None) -> dict[str, Any]:
    global _prev_recommended, _direction_state_cache

    def _check_heartbeat() -> None:
        if heartbeat is not None and not heartbeat():
            raise RuntimeLockLostError("reco runtime lock lost")
    _fresh_gap = _persistence_fresh_gap(settings)
    _prev_recommended = _load_prev_recommended(conn)
    _direction_state_cache = _load_direction_state(conn)
    limits = normalize_risk_limits(db.get_active_risk_limits(conn), settings.risk_limits)
    policy_contract = calibration_policy_contract(settings, limits)
    policy_fingerprint = calibration_policy_fingerprint(settings, limits)
    sent_agg = compute_sentiment_agg(conn, scope="global", key="crypto")
    # Primary sentiment for scoring: adaptive blend from compute_sentiment_agg.
    # Falls back to 6h EWMA for backward compatibility with older snapshots.
    global_sent = _finite_float(sent_agg.get("effective_score", sent_agg.get("ewma", {}).get("6h", 0.0)), 0.0)
    # Per-symbol sentiment map: {SYMBOL: float} blended from RSS/Reddit/CoinGecko
    symbol_sent_map: dict[str, tuple[float, int]] = compute_symbol_sentiment_map(conn)

    # LogReg+Platt calibrators — one bounded evidence context per publication
    # cycle.  Global, bot and direction models reuse the same compact outcome
    # rows and memoized observability scans instead of materializing them three
    # independent times.
    calibration_evidence = _CalibrationEvidenceContext(
        conn=conn,
        min_samples=settings.calib_min_samples,
        policy_fingerprint=policy_fingerprint,
        settings_obj=settings,
    )
    try:
        global_calibrator = _load_or_fit_global_logreg(
            conn,
            min_samples=settings.calib_min_samples,
            policy_fingerprint=policy_fingerprint,
            settings_obj=settings,
            evidence_context=calibration_evidence,
        )
        bot_calibrators = _load_or_fit_bot_logregs(
            conn,
            min_samples=settings.calib_min_samples,
            policy_fingerprint=policy_fingerprint,
            settings_obj=settings,
            evidence_context=calibration_evidence,
        )
        dir_calibrator = _load_or_fit_direction_calibrator(
            conn,
            min_samples=settings.calib_min_samples,
            policy_fingerprint=policy_fingerprint,
            settings_obj=settings,
            evidence_context=calibration_evidence,
        )
        trend_event_model = _load_or_fit_trend_event_model(
            conn,
            min_samples=settings.calib_min_samples,
            policy_fingerprint=policy_fingerprint,
            evidence_context=calibration_evidence,
        )
    finally:
        calibration_evidence.release_rows()
    # Legacy alias — used in PUBLISH log and UI status endpoint
    calibrator = global_calibrator

    features_all: list[dict[str, Any]] = []
    symbol_feature_map: dict[tuple[str,str], dict[str, Any]] = {}
    symbol_ticker_map: dict[tuple[str,str], Any] = {}  # stores trow per (venue,sym)
    symbol_llm_candle_map: dict[tuple[str, str], dict[int, list[list[float | int]]]] = {}
    llm_tf_set = set(getattr(settings, "llm_reviewer_tf_secs", []) or []) if bool(getattr(settings, "llm_reviewer_enabled", False)) else set()
    llm_candle_limit = int(getattr(settings, "llm_reviewer_candles_per_tf", LLM_REVIEWER_DEFAULT_CANDLES_PER_TF) or LLM_REVIEWER_DEFAULT_CANDLES_PER_TF)

    ts_now = db.now_ts()  # set here for stale gate use inside feature loop

    # Load BTC 1h closes once — used for beta/correlation calculation per symbol
    btc_1h_rows = db.get_latest_ohlcv(conn, "linear", "BTCUSDT", tf_sec=3600, limit=50)
    btc_1h_rows = _drop_open_candle(btc_1h_rows, tf_sec=3600, ts_now=ts_now)
    if not btc_1h_rows:
        btc_1h_rows = db.get_latest_ohlcv(conn, "linear", "BTCUSDT", tf_sec=3600, limit=50)
        btc_1h_rows = _drop_open_candle(btc_1h_rows, tf_sec=3600, ts_now=ts_now)
    # Reverse to oldest-first for log-return calculations in btc_beta
    btc_1h_closes = [float(r["close"]) for r in reversed(btc_1h_rows)] if btc_1h_rows else []

    for venue in settings.venues:
        symbols = settings.symbols_linear if venue == "linear" else settings.symbols_linear
        for sym in symbols:
            _check_heartbeat()
            rows = db.get_latest_ohlcv(conn, venue, sym, tf_sec=60, limit=220)
            rows = _drop_open_candle(rows, tf_sec=60, ts_now=ts_now)
            if not rows or len(rows) < 80:
                continue
            trow = db.get_latest_ticker(conn, venue, sym)
            ticker = dict(trow) if trow else None
            ticker_ts = db.get_latest_ticker_ts(conn, venue, sym)
            ticker_age_sec = None if not ticker_ts else max(0, ts_now - ticker_ts)
            # get_latest_ohlcv returns newest-first (ORDER BY ts DESC).
            # compute_features_from_ohlcv and all indicator functions
            # (ma_slope, EMA, RSI, MACD, BB) require oldest-first order.
            f = compute_features_from_ohlcv([dict(r) for r in reversed(rows)], ticker)
            if not f:
                continue

            llm_candles: dict[int, list[list[float | int]]] = {}
            if 60 in llm_tf_set:
                llm_candles[60] = _serialize_llm_candles([dict(r) for r in reversed(rows)], llm_candle_limit)

            # ── Stale data gate ──────────────────────────────────────────
            # A recommendation is only as fresh as its slowest required market input.
            # Fresh candles with stale/missing ticker snapshots create inconsistent
            # spread/cost assumptions and can silently misprice execution risk.
            candle_age_sec = max(0, ts_now - int(f["ts_last"]))
            data_age_sec = candle_age_sec
            stale_source = "candle"
            if ticker_age_sec is None:
                data_age_sec = max(data_age_sec, settings.stale_data_max_sec + 1)
                stale_source = "ticker_missing"
            elif ticker_age_sec > data_age_sec:
                data_age_sec = ticker_age_sec
                stale_source = "ticker"
            if data_age_sec > settings.stale_data_max_sec:
                db.log_decision(conn, "STALE_DATA_SKIP", None, None, {
                    "venue": venue, "symbol": sym,
                    "age_sec": data_age_sec, "max_sec": settings.stale_data_max_sec,
                    "candle_age_sec": candle_age_sec,
                    "ticker_age_sec": ticker_age_sec,
                    "source": stale_source,
                })
                continue

            # Multi-timeframe direction voting (15m/30m/1h/4h/1d)
            tf_secs = [15*60, 30*60, 60*60, 240*60, 24*60*60]
            tf_map = {}
            atr_15m = None
            atr_30m = None
            atr_1h = None
            atr_4h = None
            atr_1d = None
            for tf in tf_secs:
                rows_tf = db.get_latest_ohlcv(conn, venue, sym, tf_sec=tf, limit=260 if tf<=3600 else 420)
                rows_tf = _drop_open_candle(rows_tf, tf_sec=tf, ts_now=ts_now)
                if not rows_tf or len(rows_tf) < 80:
                    continue
                # Reverse to oldest-first — get_latest_ohlcv returns newest-first.
                rows_tf_ord = list(reversed(rows_tf))
                if tf in llm_tf_set:
                    llm_candles[tf] = _serialize_llm_candles([dict(r) for r in rows_tf_ord], llm_candle_limit)
                closes_tf = [float(r["close"]) for r in rows_tf_ord]
                highs_tf = [float(r["high"]) for r in rows_tf_ord]
                lows_tf = [float(r["low"]) for r in rows_tf_ord]
                info = vote_for_tf(closes_tf, highs_tf, lows_tf)
                tf_map[tf] = info
                if tf == 60*60:
                    atr_1h = float(info.get("atr_pct") or 0.0)
                elif tf == 15*60:
                    atr_15m = float(info.get("atr_pct") or 0.0)
                elif tf == 30*60:
                    atr_30m = float(info.get("atr_pct") or 0.0)
                elif tf == 240*60:
                    atr_4h = float(info.get("atr_pct") or 0.0)
                elif tf == 24*60*60:
                    atr_1d = float(info.get("atr_pct") or 0.0)

            agg = aggregate_direction(tf_map) if tf_map else {"direction":"neutral","bias":"neutral","direction_confidence":0.5,"scores":{"tactical":0,"structural":0,"all":0},"strength":{"tactical":0,"structural":0,"all":0},"coherence":0.5,"regime":"unknown","regime_confidence":0.0,"structural_veto_applied":False,"tf_used":[]}
            agg, _dir_state = _stabilize_direction_agg(agg, _direction_state_cache.get((venue, sym)), ts_now, _fresh_gap)
            _direction_state_cache[(venue, sym)] = _dir_state
            f["_direction_agg"] = agg
            f["_atr_pct_1h"] = atr_1h
            f["_atr_pct_15m"] = atr_15m
            f["_atr_pct_30m"] = atr_30m
            f["_atr_pct_4h"] = atr_4h
            f["_atr_pct_1d"] = atr_1d

            # ── BTC beta ─────────────────────────────────────────────────
            if sym != "BTCUSDT" and btc_1h_closes:
                # tf_map stores vote_for_tf dicts, not raw rows — always fetch closes from DB
                _sym_rows = db.get_latest_ohlcv(conn, venue, sym, tf_sec=3600, limit=50)
                _sym_rows = _drop_open_candle(_sym_rows, tf_sec=3600, ts_now=ts_now)
                sym_1h_closes = [float(r["close"]) for r in reversed(_sym_rows)] if _sym_rows else []
                beta_info = btc_beta(sym_1h_closes, btc_1h_closes, window=24)
            else:
                beta_info = {"correlation": None, "beta": None,
                             "is_btc_driven": False, "independent_signal": True, "window": 0}
            f["_btc_beta"] = beta_info

            ts_f = int(f["ts_last"])
            db.insert_features(conn, venue, sym, ts_f, f)
            features_all.append(f)
            symbol_feature_map[(venue, sym)] = f
            symbol_ticker_map[(venue, sym)] = trow  # save for reco loop
            if llm_candles:
                symbol_llm_candle_map[(venue, sym)] = llm_candles

    regime = classify_regime(features_all)
    db.insert_regime(conn, db.now_ts(), regime)

    market_shock = compute_market_shock(conn, settings, sent_agg, symbol_feature_map, ts_now)
    db.set_app_config_json(conn, MARKET_SHOCK_APP_KEY, market_shock, commit=False)

    model_version = RECOMMENDER_MODEL_VERSION
    if bool(getattr(settings, "llm_reviewer_enabled", False)):
        model_version += "+llm-review-v1"
    # ts_now already set above for stale gate — reuse it

    recs: list[dict[str, Any]] = []

    # Cache risk status once per cycle — avoids 450+ extra DB queries/cycle with 30 symbols.
    _cached_risk_status = _compute_risk_status(conn, limits)

    for (venue, sym), f in symbol_feature_map.items():
        taker_fee_bps = settings.taker_fee_bps_linear if venue == "linear" else settings.taker_fee_bps_linear

        for bot_type in BOT_TYPES_BYBIT:
            if bot_type in {"futures_grid", "directional_trend"} and venue != "linear":
                continue
            candidate_model_version = (
                TREND_RECOMMENDER_MODEL_VERSION
                if bot_type == "directional_trend"
                else RECOMMENDER_MODEL_VERSION
            )
            if bool(getattr(settings, "llm_reviewer_enabled", False)):
                candidate_model_version += "+llm-review-v1"

            spread_raw = f.get("spread_bps")
            spread = _finite_or_none(spread_raw)
            # Risk/scoring volatility proxy: prefer 1h ATR% (from multi-TF direction pass).
            atr_pct_1m = _finite_float(f.get("atr_pct"), 0.0)
            atr_pct_1h = _finite_float(f.get("_atr_pct_1h"), 0.0)
            atr_pct = atr_pct_1h if atr_pct_1h > 0 else atr_pct_1m

            # ── Liquidity tier — use per-(venue,sym) cached ticker ──
            _trow = symbol_ticker_map.get((venue, sym))
            turnover = _finite_or_none(_trow["turnover24h"]) if _trow else None
            liq_tier = liquidity_tier(turnover)

            # ── Funding rate + OI (futures only) ──
            fr_data  = db.get_latest_funding_rate(conn, sym) if venue == "linear" else None
            if fr_data and (ts_now - int(fr_data.get("ts") or 0) > MAX_FUNDING_STALENESS_SEC):
                fr_data = None
            oi_rows  = db.get_oi_series(conn, sym, limit=48)  if venue == "linear" else []
            if oi_rows:
                latest_oi_ts = int((oi_rows[0] or {}).get("ts") or 0)
                if latest_oi_ts <= 0 or (ts_now - latest_oi_ts > MAX_OI_STALENESS_SEC):
                    oi_rows = []
            fr_sig   = funding_signal(fr_data["funding_rate"] if fr_data else None, fr_data.get("funding_interval_min") if isinstance(fr_data, dict) else None)
            oi_sig   = oi_trend(oi_rows)
            raw_direction = str((f.get('_direction_agg', {}) or {}).get('direction') or 'neutral')
            direction = _direction(bot_type, f.get('_direction_agg', {}))
            trend_direction_rejected = bool(
                bot_type == "directional_trend" and direction not in {"long", "short"}
            )
            cost_model = _estimate_cost_model(
                bot_type=bot_type,
                venue=venue,
                f=f,
                taker_fee_bps=taker_fee_bps,
                direction=direction,
                funding_rate=(fr_data["funding_rate"] if fr_data else None),
                next_funding_ts=(fr_data["next_funding_ts"] if fr_data else None),
                ts_now=ts_now,
                funding_interval_min=(fr_data.get("funding_interval_min") if isinstance(fr_data, dict) else None),
            )

            # Compute calibrated direction confidence once and reuse it everywhere
            # in this cycle (gates, feature snapshot, stored reasons, UI details).
            # Using raw confidence in one branch and calibrated confidence elsewhere
            # creates contradictory allow/block decisions for the same signal.
            _dir_agg_raw = dict(f.get("_direction_agg", {}))
            _direction_projection = _direction_confidence_projection(
                _dir_agg_raw,
                dir_calibrator,
            )
            _dir_conf_pre = float(_direction_projection["feature_value"])
            _dir_agg_cal = dict(_dir_agg_raw)
            _dir_agg_cal["direction_confidence_feature"] = _dir_conf_pre
            # Backward-compatible display alias. The accompanying model metadata
            # explicitly marks this as raw/audit-only rather than calibrated.
            _dir_agg_cal["direction_confidence_calibrated"] = _dir_conf_pre
            _dir_agg_cal["direction_confidence_audit"] = _direction_projection.get(
                "audit_probability"
            )

            # Combine OI trend with price direction for final signal
            if oi_sig["trend"] == "growing":
                dir_agg_tmp = f.get("_direction_agg", {})
                price_dir = dir_agg_tmp.get("direction", "neutral")
                if price_dir == "long":
                    oi_sig["signal"] = "bullish"   # price up + OI up → healthy long
                elif price_dir == "short":
                    oi_sig["signal"] = "bearish"   # price down + OI up → shorts piling in
                else:
                    oi_sig["signal"] = "neutral"
            elif oi_sig["trend"] == "falling":
                oi_sig["signal"] = "caution"       # unwinding → reduced conviction
            else:
                oi_sig["signal"] = "neutral"

            # Adaptive sentiment blend: symbol weight grows with number of data points.
            # Few points (< 5) → 90% global / 10% symbol — don't amplify noisy signal.
            # Many points (≥ 20) → 50% global / 50% symbol — full trust.
            # MUST be computed before feasibility checks that reference effective_sent
            _global_sent_has_data = bool((sent_agg.get("data_quality") or {}).get("has_data"))
            _sym_entry = symbol_sent_map.get(sym)
            if _sym_entry is not None:
                sym_sent, _sym_n = _sym_entry
                _sym_weight = float(_clamp(_sym_n / 20.0, 0.1, 0.5))
                effective_sent = (1.0 - _sym_weight) * global_sent + _sym_weight * sym_sent
            else:
                sym_sent = None
                _sym_n = 0
                _sym_weight = 0.0
                effective_sent = global_sent
            sentiment_has_data = bool(_global_sent_has_data or _sym_n > 0)


            feasibility_blocks = []
            thesis_no_trade_reasons: list[dict[str, str]] = []

            # ── Data completeness / liquidity gates ──
            if turnover is None:
                feasibility_blocks.append({"code": "LIQUIDITY_UNKNOWN",
                    "msg": "нет turnover24h — ликвидность не подтверждена, cost-model ненадёжен"})
            elif liq_tier == "micro":
                feasibility_blocks.append({"code": "LIQUIDITY_TOO_LOW",
                    "msg": f"turnover24h={turnover} USD < $500K — торговля на неликвидном символе искажает fills/статистику"})
                if venue == "linear":
                    feasibility_blocks.append({"code": "LIQUIDITY_LOW_FUTURES",
                        "msg": f"turnover24h={turnover} USD < $2M — для futures grid нужна повышенная осторожность по ликвидности"})
            elif venue == "linear" and liq_tier == "low":
                feasibility_blocks.append({"code": "LIQUIDITY_LOW_FUTURES",
                    "msg": f"turnover24h={turnover} USD < $2M — для futures grid нужна повышенная осторожность по ликвидности"})
            if spread is None:
                feasibility_blocks.append({"code": "SPREAD_UNKNOWN",
                    "msg": "bid/ask отсутствуют — нельзя надёжно оценить execution cost"})

            # ── Market-regime gates specific to grid ──
            # Grid must be allowed only when the current multi-timeframe context is plausibly range-like.
            # A high-confidence trend with weak range score is not just lower-scoring; it is a domain veto.
            if bot_type == "futures_grid":
                _range_score_now, _range_meta_now = _stable_range_score(f, f.get("_direction_agg", {}) or {})
                _trendiness_now = float(_range_meta_now.get("trendiness") or 0.0)
                _regime_now = str(_range_meta_now.get("regime") or "unknown")
                _tf_used_now = list((f.get("_direction_agg") or {}).get("tf_used") or [])
                if len(_tf_used_now) < 3:
                    feasibility_blocks.append({
                        "code": "INSUFFICIENT_MTF_HISTORY_FOR_GRID",
                        "msg": f"использовано только {len(_tf_used_now)} timeframes для direction/regime; futures grid не публикуется без минимум 3 закрытых TF-историй",
                    })
                for _mr_decision in _mean_reversion_grid_blocks(
                    _range_meta_now,
                    min_score=settings.mean_reversion_min_score,
                ):
                    if _mr_decision.get("decision") == "no_trade":
                        thesis_no_trade_reasons.append(dict(_mr_decision))
                    else:
                        feasibility_blocks.append(dict(_mr_decision))
                if _range_score_now < 0.42 and _trendiness_now >= 0.58:
                    feasibility_blocks.append({
                        "code": "RANGE_EDGE_TOO_WEAK_FOR_GRID",
                        "msg": f"range_score={_range_score_now:.2f}, trendiness={_trendiness_now:.2f}; futures grid требует выраженного диапазонного edge, а не только отсутствия hard-trend veto",
                    })
                if _regime_now == "trend" and _trendiness_now >= 0.80 and _range_score_now < 0.35:
                    feasibility_blocks.append({
                        "code": "MARKET_TOO_TRENDY_FOR_GRID",
                        "msg": f"regime=trend, trendiness={_trendiness_now:.2f}, range_score={_range_score_now:.2f}; grid запускается только при диапазонном или слабонаправленном режиме",
                    })
                if atr_pct >= 0.10:
                    feasibility_blocks.append({
                        "code": "VOLATILITY_TOO_HIGH_FOR_GRID",
                        "msg": f"ATR≈{atr_pct * 100.0:.2f}% слишком высок для безопасного grid-рекомендования; риск пробоя диапазона и ликвидации повышен",
                    })
            elif bot_type == "directional_trend":
                _agg_now = dict(f.get("_direction_agg") or {})
                _trendiness_now = _clamp(_finite_float(_agg_now.get("trendiness"), 0.0), 0.0, 1.0)
                _coherence_now = _clamp(_finite_float(_agg_now.get("coherence"), 0.0), 0.0, 1.0)
                _regime_now = str(_agg_now.get("regime") or "unknown")
                _tf_used_now = list(_agg_now.get("tf_used") or [])
                _strengths_now = _agg_now.get("strength") or {}
                _all_strength_now = _clamp(
                    abs(_finite_float(_strengths_now.get("all") if isinstance(_strengths_now, dict) else _strengths_now, 0.0)),
                    0.0,
                    1.0,
                )
                _structural_strength_now = _clamp(
                    abs(_finite_float(_strengths_now.get("structural") if isinstance(_strengths_now, dict) else 0.0, 0.0)),
                    0.0,
                    1.0,
                )
                if len(_tf_used_now) < 3:
                    feasibility_blocks.append({
                        "code": "INSUFFICIENT_MTF_HISTORY_FOR_TREND",
                        "msg": f"использовано только {len(_tf_used_now)} timeframes; directional trend требует минимум 3 закрытых TF-истории",
                    })
                if trend_direction_rejected:
                    thesis_no_trade_reasons.append({
                        "code": "TREND_DIRECTION_UNCONFIRMED",
                        "msg": (
                            "предварительная trend-оценка не подтвердила LONG или SHORT; "
                            "позиция и TP/SL не формируются"
                        ),
                    })
                else:
                    if _regime_now != "trend":
                        thesis_no_trade_reasons.append({
                            "code": "TREND_REGIME_UNCONFIRMED",
                            "msg": f"regime={_regime_now}; trend strategy исследуется только в подтверждённом trend-режиме",
                        })
                    if _trendiness_now < 0.48:
                        thesis_no_trade_reasons.append({
                            "code": "TREND_STRENGTH_INSUFFICIENT",
                            "msg": f"trendiness={_trendiness_now:.2f} < 0.48",
                        })
                    if _all_strength_now < 0.18 or _structural_strength_now < 0.12:
                        thesis_no_trade_reasons.append({
                            "code": "DIRECTIONAL_MTF_STRENGTH_INSUFFICIENT",
                            "msg": f"all_strength={_all_strength_now:.2f}, structural_strength={_structural_strength_now:.2f}",
                        })
                    if _coherence_now < 0.55:
                        thesis_no_trade_reasons.append({
                            "code": "TREND_TIMEFRAME_COHERENCE_INSUFFICIENT",
                            "msg": f"coherence={_coherence_now:.2f} < 0.55",
                        })
                if atr_pct >= 0.15:
                    feasibility_blocks.append({
                        "code": "VOLATILITY_TOO_HIGH_FOR_DIRECTIONAL_TREND",
                        "msg": f"ATR≈{atr_pct * 100.0:.2f}% превышает shadow trend safety bound",
                    })

            # ── Funding rate gate (futures only) ──
            # Gate must be keyed off the *payer* side, not only off semantic longs.
            # `expected_funding_bps` is direction-aware already, so positive values mean
            # this exact setup is expected to pay funding over the label horizon.
            if venue == "linear":
                if fr_sig.get("value") is None:
                    feasibility_blocks.append({
                        "code": "FUNDING_RATE_UNKNOWN",
                        "msg": "нет актуального funding rate для Linear USDT perpetual; рекомендация блокируется, чтобы не показывать net-profit без funding",
                    })
                funding_block = _extreme_funding_block(direction, fr_sig, cost_model)
                if funding_block is not None:
                    feasibility_blocks.append(funding_block)
                if bool(cost_model.get("funding_interval_uncertain")) and abs(float(cost_model.get("expected_funding_bps") or 0.0)) >= 3.0:
                    feasibility_blocks.append({
                        "code": "FUNDING_INTERVAL_UNCONFIRMED",
                        "msg": "funding interval не подтверждён Bybit ticker/instrument metadata; funding impact может быть недооценён",
                    })

            if bot_type == "futures_grid" and spread is not None and spread > 14.0:
                feasibility_blocks.append({"code":"SPREAD_TOO_WIDE", "msg": f"spread_bps={spread:.2f} слишком широкий для grid"})
            if bot_type == "directional_trend" and spread is not None and spread > 20.0:
                feasibility_blocks.append({"code":"SPREAD_TOO_WIDE_FOR_TREND", "msg": f"spread_bps={spread:.2f} слишком широкий для directional trend"})
            # If symbol is highly correlated to BTC, direction is less independent
            beta_info = f.get("_btc_beta", {})
            if beta_info.get("is_btc_driven") and sym != "BTCUSDT":
                _dir_conf_pre = float(_clamp(_dir_conf_pre * 0.88, 0.0, 0.99))
                _dir_agg_cal["direction_confidence_feature"] = _dir_conf_pre
                _dir_agg_cal["direction_confidence_calibrated"] = _dir_conf_pre
            # Block threshold 0.05 = 5% 1h ATR. Old value 0.018 was calibrated for 1m ATR
            # and blocked ALL symbols since typical 1h ATR for small caps is 3–8%.
            # Portfolio capacity, drawdown and post-loss cooldown are evaluated
            # for both actionable strategy families. The losing router candidate
            # is persisted as a paired outcome sample only after policy evaluation;
            # it never bypasses market or portfolio safety gates.
            risk_blocks = gate_candidate(
                conn,
                venue,
                sym,
                limits,
                cached_status=_cached_risk_status,
            )
            feasibility_blocks.extend(risk_blocks)
            feasibility_blocks.extend(apply_market_shock_gate(market_shock, venue, bot_type, direction))
            fast_veto = compute_symbol_fast_veto(conn, venue, sym, ts_now, direction, feature_row=f)
            feasibility_blocks.extend(fast_veto.get("blocks") or [])

            f_for_score = dict(f)
            f_for_score["_direction_agg"] = dict(_dir_agg_cal)
            score, conf0, reasons = _score(
                bot_type,
                venue,
                f_for_score,
                taker_fee_bps=taker_fee_bps,
                global_sent=effective_sent,
                cost_model=cost_model,
                sentiment_has_data=sentiment_has_data,
            )

            # ── Funding + OI score adjustments ──
            if venue == "linear":
                score = _clamp(score + _funding_score_adjustment(direction, fr_sig, cost_model), -1.0, 1.0)

            if str((market_shock or {}).get("severity") or "normal") == "guarded" and not feasibility_blocks:
                score = float(_clamp(score * 0.92, -1.0, 1.0))

            conf_raw = float(conf0)
            # ── Two-stage calibration: LogReg(features) → Platt ──────────────────
            # Build a temporary reasons-like dict so extract_features() can work
            # with the current (not yet stored) feature set.
            # ── Compute dir_conf_cal FIRST so extract_features gets the calibrated
            # value — matching what was stored in reasons_json during training.
            # (Previously dir_conf_cal was computed after extract_features, causing
            # a train/inference skew: model trained on calibrated conf, inferred on raw.)
            _dir_agg_for_cal = dict(_dir_agg_cal)
            feature_snapshot = _build_feature_snapshot(
                score=score,
                atr_pct=atr_pct,
                effective_sent=effective_sent,
                cost_model=cost_model,
                direction_agg=_dir_agg_for_cal,
                oi_sig=oi_sig,
                liq_tier=liq_tier,
                beta_info=beta_info,
                direction=direction,
            )
            _snapshot_agg = dict(_dir_agg_for_cal)
            feature_snapshot["strategy_family"] = (
                "directional_trend" if bot_type == "directional_trend" else "futures_grid"
            )
            feature_snapshot["regime"] = str(_snapshot_agg.get("regime") or "unknown")
            feature_snapshot["trend_evidence_valid"] = bool(
                bot_type == "directional_trend"
                and direction in {"long", "short"}
                and str(_snapshot_agg.get("regime") or "") == "trend"
                and _finite_float(_snapshot_agg.get("trendiness"), 0.0) >= 0.48
                and _finite_float(_snapshot_agg.get("coherence"), 0.0) >= 0.55
                and len(_snapshot_agg.get("tf_used") or []) >= 3
            )
            feature_snapshot["strategy_contract_version"] = (
                TREND_STRATEGY_CONTRACT_VERSION if bot_type == "directional_trend" else "futures_grid_v26"
            )
            feature_snapshot["outcome_label_version"] = (
                TREND_OUTCOME_LABEL_VERSION if bot_type == "directional_trend" else POLICY_OUTCOME_LABEL_VERSION
            )

            _reasons_for_cal = {
                "effective_sentiment": effective_sent,
                "cost_model": cost_model,
                "direction_agg": _dir_agg_for_cal,  # includes calibrated dir_conf
                "feature_snapshot": dict(feature_snapshot),
                "top_positive_factors": (reasons.get("top_positive_factors") or []),
                "top_negative_factors": (reasons.get("top_negative_factors") or []),
            }
            _row_for_cal = {
                "score": score,
                "reasons": _reasons_for_cal,
                "success": 0,
                "direction": direction,
            }
            _fv = extract_features(_row_for_cal)

            bot_cal = bot_calibrators.get(bot_type)
            if not trend_direction_rejected:
                _expectancy_no_trade = _calibration_expectancy_no_trade_reason(bot_cal)
                if _expectancy_no_trade is not None:
                    thesis_no_trade_reasons.append(_expectancy_no_trade)
                _probability_no_trade = _probability_calibration_no_trade_reason(
                    bot_cal,
                    require_conf_gate=bool(settings.require_conf_gate),
                )
                if _probability_no_trade is not None:
                    thesis_no_trade_reasons.append(_probability_no_trade)
            if (
                bot_cal
                and bot_cal.fitted
                and len(bot_cal.coef) == len(FEATURE_NAMES)
                and bot_cal.platt.fitted
                and str(bot_cal.oof_status) == "sufficient"
                and str(bot_cal.oof_skill_status) == "accepted"
                and int(bot_cal.oof_final_samples) >= int(bot_cal.oof_required_final_samples) > 0
                and int(bot_cal.oof_final_decision_cohorts)
                >= int(bot_cal.oof_required_final_decision_cohorts) > 0
                and str(bot_cal.selected_policy_expectancy_status) == "positive"
                and str(bot_cal.terminal_selected_policy_expectancy_status)
                == "positive"
                and _fv is not None
            ):
                conf_cal = float(bot_cal.predict(_fv))
                _cal_source = "bot_logreg"
                _active_cal = bot_cal
            else:
                # Do NOT fall back to a cross-bot/global calibrator for inference.
                # Outcome labels are bot-mechanics-specific (grid/range), so a pooled probability creates pseudo-statistical confidence.
                conf_cal = float(conf0)
                _cal_source = "raw"
                _active_cal = None
            # Adaptive blend: calibration weight grows with n_samples.
            # Raw-only mode keeps weight=0. Once a bot-specific calibrator exists,
            # the blend ramps up gradually so a freshly-fitted model does not fully
            # override the heuristic score on a still-small sample.
            _heur_cap = None
            if _active_cal is not None and _active_cal.fitted:
                _n_cal = int(_active_cal.n_samples)
                _cal_weight = float(_clamp(_n_cal / 300.0, 0.0, 1.0)) * 0.40 + 0.10
            else:
                _n_cal = 0
                _cal_weight = 0.0
            conf = float(_clamp((1.0 - _cal_weight) * conf_raw + _cal_weight * conf_cal, 0.0, 1.0))

            # Heuristic-only confidence must stay visibly conservative.
            if _active_cal is None:
                _heur_cap = {"futures_grid": 0.70}.get(bot_type, 0.70)
                conf = float(min(conf, _heur_cap))

            # Context completeness penalty — reduce confidence when key signals are missing.
            # The system already falls back gracefully; this makes the uncertainty explicit.
            _ctx_mult = 1.0
            if not f.get("_atr_pct_1h"):
                _ctx_mult *= 0.92  # no 1h ATR
            if not sentiment_has_data:
                _ctx_mult *= 0.94  # missing sentiment is uncertainty, not true neutral
            if venue == "linear" and oi_sig.get("oi_now") is None:
                _ctx_mult *= 0.96  # no OI data
            if venue == "linear" and fr_sig.get("value") is None:
                _ctx_mult *= 0.98  # no funding data
            _dir_tf_count = len((f.get("_direction_agg") or {}).get("tf_used") or [])
            if _dir_tf_count < 3:
                _ctx_mult *= 0.93  # sparse TF coverage
            if _ctx_mult < 1.0:
                conf = float(_clamp(conf * _ctx_mult, 0.0, 1.0))
            _selection_confidence_adjustment = float(_ctx_mult)

            # OI unwinding → reduce confidence
            if venue == "linear" and oi_sig["signal"] == "caution":
                conf = float(_clamp(conf * 0.88, 0.0, 1.0))
                _selection_confidence_adjustment *= 0.88
            if str((market_shock or {}).get("severity") or "normal") == "guarded":
                conf = float(_clamp(conf * 0.93, 0.0, 1.0))
                _selection_confidence_adjustment *= 0.93

            # Recompute active-model confidence through the shared transform used
            # by purged OOF monetary validation. Any future formula drift must now
            # fail tests instead of changing the publication subset silently.
            if _active_cal is not None:
                _policy_confidence = selected_policy_confidence(
                    conf_raw,
                    conf_cal,
                    _n_cal,
                    _selection_confidence_adjustment,
                )
                conf = float(_policy_confidence) if _policy_confidence is not None else 0.0
            feature_snapshot["selection_confidence_raw"] = float(conf_raw)
            feature_snapshot["selection_confidence_adjustment"] = float(
                _selection_confidence_adjustment
            )

            expected_rr = _expected_rr(bot_type, f, cost_model=cost_model)
            account_mode, margin_mode = _mode(bot_type, venue, direction)

            blocks = list(feasibility_blocks)  # risk_blocks already included via feasibility_blocks.extend()

            confidence_gate_applied = bool(
                settings.require_conf_gate and _cal_source == "bot_logreg"
            )

            status = "recommended"
            if blocks:
                status = "blocked"
            elif score < settings.min_score_to_recommend:
                status = "no_trade"
            elif confidence_gate_applied and conf < settings.min_conf_to_recommend:
                status = "no_trade"

            risk_score = float(_clamp(atr_pct/0.10, 0.0, 1.0))

            params = _params(
                bot_type,
                venue,
                f,
                global_sent=effective_sent,
                direction=direction,
                taker_fee_bps=taker_fee_bps,
                direction_bias=str(_dir_agg_cal.get("bias", "neutral")),
                direction_bias_strength=float((_dir_agg_cal.get("strength", {}) or {}).get("all", 0.0) if isinstance(_dir_agg_cal.get("strength"), dict) else float(_dir_agg_cal.get("strength", 0.0))),
                atr_pct_for_grid=f.get("_atr_pct_1h"),
                cost_model=cost_model,
                risk_limits=limits,
            )
            if trend_direction_rejected:
                params["evaluation_diagnostics"] = {
                    "raw_direction": raw_direction,
                    "normalized_direction": direction,
                    "regime": str((f.get("_direction_agg") or {}).get("regime") or "unknown"),
                    "trendiness": _finite_or_none((f.get("_direction_agg") or {}).get("trendiness")),
                    "coherence": _finite_or_none((f.get("_direction_agg") or {}).get("coherence")),
                    "suppressed_feasibility_observations": list(blocks),
                    "note": (
                        "Диагностические наблюдения сохранены, но не являются блокировками позиции: "
                        "позиция не существует без LONG/SHORT."
                    ),
                }
                # An unresolved direction is a rejected evaluation, not a malformed
                # position.  Do not cascade missing TP/SL or exchange-plan errors.
                blocks = []
                status = "no_trade"

            no_trade_reasons: list[dict[str, str]] = list(thesis_no_trade_reasons)
            leverage_policy = params.get("leverage_policy") if isinstance(params.get("leverage_policy"), dict) else {}
            if (
                bot_type == "futures_grid"
                and venue == "linear"
                and leverage_policy
                and leverage_policy.get("operator_minimum_approved") is False
            ):
                selected_leverage = _finite_or_none(params.get("leverage"))
                policy_note = str(leverage_policy.get("not_actionable_reason") or leverage_policy.get("note") or "operator_minimum_not_approved")
                no_trade_reasons.append({
                    "code": "OPERATOR_LEVERAGE_PROFILE_NOT_ACTIONABLE",
                    "msg": (
                        f"идея не проходит текущий 3-5x leverage profile без ослабления risk policy; "
                        f"evaluated_leverage={selected_leverage:.0f}x, reason={policy_note}"
                        if selected_leverage is not None
                        else f"идея не проходит текущий 3-5x leverage profile без ослабления risk policy; reason={policy_note}"
                    ),
                })
            if params.get("price_input_valid") is False:
                blocks.append({
                    "code": "INVALID_MARKET_REFERENCE_PRICE",
                    "msg": f"market reference price is missing/non-positive/non-finite; {bot_type} recommendation is blocked fail-closed",
                })
            # Add execution guide for UI "Details" panel.
            params["symbol"] = sym
            params["account_mode"] = account_mode
            params["trade_plan"] = _build_trade_plan(bot_type, venue, f, direction, params, cost_model=cost_model)
            trend_event_assessment: dict[str, Any] | None = None
            if bot_type == "directional_trend" and trend_direction_rejected:
                params["operator_sheet"] = {
                    "mode": "evaluation_rejected",
                    "venue": venue,
                    "bot_type": bot_type,
                    "symbol": sym,
                    "candidate_kind": TREND_EVALUATION_REJECTED_KIND,
                    "strategy_family": "trend_evaluation",
                    "recommendation_only": True,
                    "exchange_order_submitted": False,
                    "price_ref": params.get("price_ref"),
                    "entry_model": None,
                    "take_profit": None,
                    "stop_loss": None,
                    "operator_note": (
                        "Проверка направления отклонена: LONG/SHORT не подтверждён. "
                        "Для этой строки позиция и outcome не существуют."
                    ),
                }
            elif bot_type == "directional_trend":
                trend_event_assessment = build_trend_event_assessment(
                    {"direction": direction, "params": params},
                    _fv,
                    trend_event_model,
                )
                params["trend_event_assessment"] = dict(trend_event_assessment)
                if trend_event_assessment.get("ready") is not True:
                    event_codes = set(trend_event_assessment.get("reason_codes") or [])
                    if "TREND_FIRST_TOUCH_EXPECTANCY_NON_POSITIVE" in event_codes:
                        code = "TREND_FIRST_TOUCH_EXPECTANCY_NON_POSITIVE"
                        msg = "first-touch event EV or its conservative lower bound is non-positive"
                    elif "TP_FIRST_NOT_MORE_LIKELY_THAN_SL_FIRST" in event_codes:
                        code = "TREND_FIRST_TOUCH_ORDER_UNCERTAIN"
                        msg = "conservative P(TP first) does not exceed P(SL first)"
                    else:
                        code = "TREND_FIRST_TOUCH_MODEL_UNAVAILABLE"
                        msg = "v2 TP_FIRST/SL_FIRST/HORIZON_EXIT probability model is not decision-ready"
                    no_trade_reasons.append({"code": code, "msg": msg})
                _trend_levels = (params.get("trade_plan") or {}).get("levels", {})
                params["operator_sheet"] = {
                    "mode": direction,
                    "venue": venue,
                    "bot_type": bot_type,
                    "symbol": sym,
                    "strategy_family": "directional_trend",
                    "recommendation_only": True,
                    "exchange_order_submitted": False,
                    "price_ref": params.get("price_ref"),
                    "take_profit": _trend_levels.get("take_profit", {}),
                    "stop_loss": _trend_levels.get("stop_loss", {}),
                    "entry_model": params.get("entry_model"),
                    "averaging_allowed": False,
                    "pyramiding_allowed": False,
                    "sizing": params.get("sizing"),
                    "economics": params.get("economics"),
                    "market_shock_state": (market_shock or {}).get("state"),
                    "market_shock_title": (market_shock or {}).get("title"),
                    "external_execution_package": (params.get("trade_plan") or {}).get("external_execution_package"),
                    "first_touch_assessment": trend_event_assessment,
                    "operator_note": "Single-position recommendation. Сервис создаёт audit instance, но не отправляет биржевой ордер.",
                }
            else:
                params["operator_sheet"] = {
                    "mode": direction,
                    "venue": venue,
                    "bot_type": bot_type,
                    "symbol": sym,
                    "price_ref": params.get("price_ref"),
                    "range_lower": params.get("price_range_lower"),
                    "range_upper": params.get("price_range_upper"),
                    "grid_levels": params.get("grid_levels"),
                    "grid_spacing_pct": params.get("grid_spacing_pct"),
                    "leverage": params.get("leverage"),
                    "margin_mode": params.get("margin_mode"),
                    "kill_switch": (params.get("trade_plan") or {}).get("levels", {}).get("kill_switch", {}),
                    "tp_per_leg": (params.get("trade_plan") or {}).get("levels", {}).get("tp_per_leg", {}),
                    "sizing": params.get("sizing"),
                    "economics": params.get("economics"),
                    "market_shock_state": (market_shock or {}).get("state"),
                    "market_shock_title": (market_shock or {}).get("title"),
                    "operator_note": (market_shock or {}).get("operator_note"),
                }
            params["decision_context"] = {
                "thesis_direction": raw_direction,
                "execution_direction": direction,
                "market_shock_state": (market_shock or {}).get("state"),
                "fast_veto_state": (fast_veto or {}).get("state"),
            }

            econ = params.get("economics") if isinstance(params.get("economics"), dict) else {}
            net_profit_bps = _finite_or_none(econ.get("net_profit_bps"))
            gross_profit_bps = _finite_or_none(econ.get("gross_profit_bps"))
            liq_buffer_pct = _finite_or_none(econ.get("liquidation_buffer_pct"))
            execution_cost_bps = (
                _finite_or_none(econ.get("grid_round_trip_fee_bps"))
                or _finite_or_none(cost_model.get("grid_round_trip_fee_bps"))
                or _finite_or_none(cost_model.get("fee_bps_round_trip"))
                or 0.0
            )
            if bot_type == "futures_grid":
                if net_profit_bps is None:
                    blocks.append({"code": "GRID_ECONOMICS_MISSING", "msg": "нет net profit per grid — рекомендация не исполнима без экономики сетки"})
                elif net_profit_bps <= 0.0:
                    blocks.append({"code": "GRID_NET_PROFIT_NON_POSITIVE", "msg": f"net_profit_per_grid={net_profit_bps:.2f} bps после комиссий двух grid fills <= 0"})
                elif net_profit_bps < 2.0:
                    blocks.append({"code": "GRID_NET_PROFIT_TOO_THIN", "msg": f"net_profit_per_grid={net_profit_bps:.2f} bps слишком мал; комиссии/проскальзывание легко съедят прибыль"})
                if gross_profit_bps is not None and execution_cost_bps > 0 and gross_profit_bps <= execution_cost_bps * 1.10:
                    blocks.append({"code": "GRID_GROSS_EDGE_BELOW_COSTS", "msg": f"gross_profit_per_grid={gross_profit_bps:.2f} bps почти не покрывает execution_cost={execution_cost_bps:.2f} bps"})
                if venue == "linear" and liq_buffer_pct is not None and liq_buffer_pct < 12.0:
                    blocks.append({"code": "LIQUIDATION_BUFFER_TOO_LOW", "msg": f"estimated liquidation buffer={liq_buffer_pct:.2f}% < 12%"})
                min_leverage = _finite_or_none(limits.get("min_leverage") if isinstance(limits, dict) else None)
                max_leverage = _finite_or_none(limits.get("max_leverage") if isinstance(limits, dict) else None)
                leverage_used = _finite_or_none(params.get("leverage"))
                if min_leverage is not None and min_leverage > 0 and leverage_used is not None and leverage_used < min_leverage:
                    blocks.append({"code": "MIN_LEVERAGE_PER_BOT", "msg": f"leverage={leverage_used:.0f}x < operator minimum={min_leverage:.0f}x"})
                if max_leverage is not None and max_leverage > 0 and leverage_used is not None and leverage_used > max_leverage:
                    blocks.append({"code": "MAX_LEVERAGE_PER_BOT", "msg": f"leverage={leverage_used:.0f}x > runtime cap={max_leverage:.0f}x"})

                max_notional = _finite_or_none(limits.get("max_position_notional_usdt") if isinstance(limits, dict) else None)
                estimated_notional = _finite_or_none(econ.get("estimated_max_position_notional_usdt"))
                if max_notional is not None and max_notional > 0 and estimated_notional is not None and estimated_notional > max_notional:
                    blocks.append({"code": "MAX_POSITION_NOTIONAL_PER_BOT", "msg": f"estimated_max_position_notional={estimated_notional:.2f} USDT > runtime cap={max_notional:.2f} USDT"})

                max_margin = _finite_or_none(limits.get("max_margin_per_bot_usdt") if isinstance(limits, dict) else None)
                sizing = params.get("sizing") if isinstance(params.get("sizing"), dict) else {}
                estimated_margin = _finite_or_none(sizing.get("estimated_worst_case_margin_required_usdt"))
                margin_label = "estimated_worst_case_margin_required"
                if estimated_margin is None:
                    estimated_margin = _finite_or_none(sizing.get("estimated_margin_required_usdt"))
                    margin_label = "estimated_margin_required"
                if max_margin is not None and max_margin > 0 and estimated_margin is not None and estimated_margin > max_margin:
                    blocks.append({"code": "MAX_MARGIN_PER_BOT", "msg": f"{margin_label}={estimated_margin:.2f} USDT > runtime cap={max_margin:.2f} USDT"})
            elif bot_type == "directional_trend" and not trend_direction_rejected:
                trend_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
                trend_econ = params.get("economics") if isinstance(params.get("economics"), dict) else {}
                if trend_plan.get("geometry_valid") is not True:
                    blocks.append({
                        "code": "DIRECTIONAL_TREND_GEOMETRY_INVALID",
                        "msg": "directional trend TP/SL geometry is incomplete or contradicts direction",
                    })
                projected_net_reward_bps = _finite_or_none(trend_econ.get("projected_net_reward_bps"))
                projected_stop_loss_bps = _finite_or_none(trend_econ.get("projected_stop_loss_bps"))
                if projected_net_reward_bps is None or projected_stop_loss_bps is None:
                    blocks.append({
                        "code": "DIRECTIONAL_TREND_ECONOMICS_MISSING",
                        "msg": "directional trend requires explicit net reward and stop-loss economics",
                    })
                elif projected_net_reward_bps <= 0.0 or projected_stop_loss_bps <= 0.0:
                    blocks.append({
                        "code": "DIRECTIONAL_TREND_ECONOMICS_NON_POSITIVE",
                        "msg": (
                            f"projected_net_reward_bps={projected_net_reward_bps:.2f}, "
                            f"projected_stop_loss_bps={projected_stop_loss_bps:.2f}"
                        ),
                    })

            if blocks:
                status = "blocked"
            elif no_trade_reasons:
                status = "no_trade"

            funding_benefit_excluded_bps = _finite_or_none(econ.get("funding_benefit_excluded_bps")) if isinstance(econ, dict) else None
            signed_net_profit_bps = _finite_or_none(econ.get("net_profit_with_signed_funding_bps")) if isinstance(econ, dict) else None
            risk_warnings = [str(x.get("msg") or x.get("code") or "") for x in (reasons.get("top_negative_factors") or [])[:5] if isinstance(x, dict)]
            if funding_benefit_excluded_bps is not None and funding_benefit_excluded_bps > 0:
                risk_warnings.append(
                    f"funding receipt {funding_benefit_excluded_bps:.2f} bps не засчитан в approval-edge: funding может измениться или стать расходом при накоплении inventory"
                )
            params["risk_report"] = {
                "decision": _risk_report_decision_for_status(status),
                "risk_profile": (econ.get("risk_profile") if isinstance(econ, dict) else None) or ("conservative" if risk_score < 0.35 else ("moderate" if risk_score < 0.70 else "aggressive")),
                "expected_net_profit_per_grid_bps": net_profit_bps,
                "expected_net_profit_per_grid_usdt": _finite_or_none(econ.get("net_profit_usdt")) if isinstance(econ, dict) else None,
                "net_profit_with_signed_funding_bps": signed_net_profit_bps,
                "estimated_execution_cost_bps": _finite_or_none(cost_model.get("execution_cost_bps")),
                "estimated_funding_impact_bps": _finite_or_none(cost_model.get("expected_funding_bps")),
                "funding_cost_bps_for_approval": _finite_or_none(econ.get("funding_cost_bps")) if isinstance(econ, dict) else None,
                "funding_benefit_excluded_bps": funding_benefit_excluded_bps,
                "funding_interval_min": cost_model.get("funding_interval_min"),
                "liquidation_buffer_pct": liq_buffer_pct,
                "capital_required_usdt": (
                    (
                        _finite_or_none((params.get("sizing") or {}).get("estimated_worst_case_margin_required_usdt"))
                        if _finite_or_none((params.get("sizing") or {}).get("estimated_worst_case_margin_required_usdt")) is not None
                        else _finite_or_none((params.get("sizing") or {}).get("estimated_margin_required_usdt"))
                    )
                    if isinstance(params.get("sizing"), dict)
                    else None
                ),
                "max_adverse_scenario": "цена выходит за range/kill-switch, сетка накапливает направленную позицию против движения; funding/fees продолжают ухудшать equity",
                "approval_reasons": [str(x.get("msg") or x.get("code") or "") for x in (reasons.get("top_positive_factors") or [])[:5] if isinstance(x, dict)],
                "rejection_reasons": [str(x.get("msg") or x.get("code") or "") for x in blocks[:8] if isinstance(x, dict)],
                "no_trade_reasons": [str(x.get("msg") or x.get("code") or "") for x in no_trade_reasons[:8] if isinstance(x, dict)],
                "warnings": risk_warnings,
            }
            if bot_type == "directional_trend" and trend_direction_rejected:
                params["risk_report"] = {
                    "decision": "not_recommended",
                    "risk_profile": "not_applicable",
                    "strategy_family": "trend_evaluation",
                    "candidate_kind": TREND_EVALUATION_REJECTED_KIND,
                    "recommendation_only": True,
                    "exchange_order_submitted": False,
                    "rejection_reasons": [
                        "TREND_DIRECTION_UNCONFIRMED: LONG/SHORT не подтверждён"
                    ],
                    "no_trade_reasons": [
                        "Для отклонённой проверки тренда позиция, TP и SL не формируются"
                    ],
                    "warnings": [],
                }
            elif bot_type == "directional_trend":
                _trend_econ = params.get("economics") if isinstance(params.get("economics"), dict) else {}
                params["risk_report"] = {
                    "decision": _risk_report_decision_for_status(status),
                    "risk_profile": "directional_single_position",
                    "strategy_family": "directional_trend",
                    "recommendation_only": True,
                    "exchange_order_submitted": False,
                    "projected_net_reward_bps": _finite_or_none(_trend_econ.get("projected_net_reward_bps")),
                    "projected_stop_loss_bps": _finite_or_none(_trend_econ.get("projected_stop_loss_bps")),
                    "plan_rr": _finite_or_none(_trend_econ.get("plan_rr")),
                    "estimated_execution_cost_bps": _finite_or_none(cost_model.get("execution_cost_bps")),
                    "estimated_funding_impact_bps": _finite_or_none(cost_model.get("expected_funding_bps")),
                    "capital_required_usdt": _finite_or_none((params.get("sizing") or {}).get("target_notional_usdt")) if isinstance(params.get("sizing"), dict) else None,
                    "max_adverse_scenario": "single directional position reaches stop_loss or gaps beyond it; no averaging is permitted",
                    "approval_reasons": [str(x.get("msg") or x.get("code") or "") for x in (reasons.get("top_positive_factors") or [])[:5] if isinstance(x, dict)],
                    "rejection_reasons": [str(x.get("msg") or x.get("code") or "") for x in blocks[:8] if isinstance(x, dict)],
                    "no_trade_reasons": [str(x.get("msg") or x.get("code") or "") for x in no_trade_reasons[:8] if isinstance(x, dict)],
                    "warnings": [
                        "Proxy outcome is not proof of live trend edge.",
                        "This service creates an audit package only and never submits a Bybit order.",
                    ],
                }

            rec_id = f"R-{ts_now}-{venue}-{sym}-{bot_type}-{secrets.token_hex(4)}"
            reasons2 = dict(reasons)
            reasons2["regime"] = regime
            reasons2["candidate_kind"] = str(params.get("candidate_kind") or TREND_STRATEGY_RECOMMENDATION_KIND)
            reasons2["risk_checks"] = {"passed": len(blocks)==0, "blocks": blocks}
            if bot_type == "directional_trend":
                reasons2["trend_economics"] = params.get("economics") if not trend_direction_rejected else None
                reasons2["trend_event_model"] = dict(trend_event_assessment or {})
                if trend_direction_rejected:
                    reasons2["trend_evaluation"] = dict(params.get("evaluation_diagnostics") or {})
            else:
                reasons2["grid_economics"] = params.get("economics")
            reasons2["sizing"] = params.get("sizing")
            probability_gate_ready = bool(
                not settings.require_conf_gate
                or (
                    confidence_gate_applied
                    and conf >= settings.min_conf_to_recommend
                )
            )
            thesis_ok = bool(
                score >= settings.min_score_to_recommend
                and probability_gate_ready
            )
            reasons2["decision_layers"] = {
                "thesis_status": "favored" if thesis_ok else "unfavorable",
                "execution_status": "blocked" if blocks else ("not_actionable" if no_trade_reasons else "allowed"),
                "final_status": status,
                "no_trade_reasons": no_trade_reasons,
                "score_threshold": float(settings.min_score_to_recommend),
                "confidence_threshold": float(settings.min_conf_to_recommend),
                "confidence_gate_required": bool(settings.require_conf_gate),
                "confidence_gate_applied": bool(confidence_gate_applied),
            }
            _trade_plan_complete = bool(
                not trend_direction_rejected
                and isinstance(params.get("trade_plan"), dict)
                and bool(params.get("trade_plan"))
                and (params.get("trade_plan") or {}).get("geometry_valid") is not False
            )
            _shadow_no_trade_eligible = bool(
                status == "no_trade"
                and not blocks
                and _trade_plan_complete
                and params.get("price_input_valid") is not False
            )
            non_calibration_no_trade_reasons = [
                reason
                for reason in no_trade_reasons
                if str(reason.get("code") or "") not in CALIBRATION_EVIDENCE_REASON_CODES
            ]
            policy_evaluation_eligible = bool(
                score >= settings.min_score_to_recommend
                and not blocks
                and not non_calibration_no_trade_reasons
                and _trade_plan_complete
                and params.get("price_input_valid") is not False
            )
            effective_label_horizon, _ = _resolve_effective_horizon(
                bot_type,
                params,
                BOT_HORIZONS.get(bot_type, HORIZON_SEC_DEFAULT),
            )
            label_due_ts = (
                None
                if trend_direction_rejected
                else calibration_policy_label_due_ts(
                    ts_now,
                    bot_type,
                    horizon_sec=effective_label_horizon,
                )
            )
            reasons2["outcome_policy"] = {
                "eligible": bool(status in {"recommended", "active", "executed"} or _shadow_no_trade_eligible),
                "policy_evaluation_eligible": policy_evaluation_eligible,
                "policy_fingerprint": policy_fingerprint,
                "policy_contract": policy_contract,
                "label_due_ts": label_due_ts,
                "calibration_role": (
                    "current_policy_evaluation"
                    if policy_evaluation_eligible
                    else (
                        "shadow_exploration"
                        if _shadow_no_trade_eligible
                        else "excluded"
                    )
                ),
                "sample_role": (
                    "shadow_no_trade" if _shadow_no_trade_eligible else (
                        "actionable_root" if status in {"recommended", "active", "executed"} else "excluded"
                    )
                ),
                "reason": (
                    "trend_direction_unconfirmed_evaluation_excluded"
                    if trend_direction_rejected
                    else (
                        "model_thesis_or_launch_gate" if _shadow_no_trade_eligible else (
                            "actionable_publication" if status in {"recommended", "active", "executed"} else "hard_or_incomplete_candidate"
                        )
                    )
                ),
                "strategy_family": (
                    "trend_evaluation" if trend_direction_rejected else (
                        "directional_trend" if bot_type == "directional_trend" else "futures_grid"
                    )
                ),
                "bot_outcome_label_version": (
                    None if trend_direction_rejected else (
                        TREND_OUTCOME_LABEL_VERSION if bot_type == "directional_trend" else POLICY_OUTCOME_LABEL_VERSION
                    )
                ),
                "strategy_contract_version": (
                    None if trend_direction_rejected else (
                        TREND_STRATEGY_CONTRACT_VERSION if bot_type == "directional_trend" else "futures_grid_v26"
                    )
                ),
                "comparison_return_basis": COMPARISON_RETURN_BASIS,
            }
            reasons2["sentiment_agg"] = sent_agg
            reasons2["market_shock"] = market_shock
            reasons2["fast_veto"] = fast_veto
            reasons2["btc_beta"] = f.get("_btc_beta", {})
            reasons2["liquidity"] = {
                "tier": liq_tier,
                "turnover24h_usd": turnover,
            }
            if venue == "linear":
                reasons2["funding"] = {
                    **fr_sig,
                    "direction": direction,
                    "directional_funding_bps_per_event": float(
                        cost_model.get("directional_funding_bps_per_event")
                        or cost_model.get("directional_funding_bps_interval")
                        or cost_model.get("directional_funding_bps_8h")
                        or 0.0
                    ),
                    # Legacy alias; prefer directional_funding_bps_per_event.
                    "directional_funding_bps_8h": float(cost_model.get("directional_funding_bps_8h") or 0.0),
                    "expected_funding_bps": float(cost_model.get("expected_funding_bps") or 0.0),
                    "expected_funding_events": int(cost_model.get("expected_funding_events") or 0),
                    "next_funding_ts": cost_model.get("next_funding_ts"),
                }
                reasons2["open_interest"] = oi_sig
            reasons2["feature_snapshot"] = dict(feature_snapshot)
            reasons2["symbol_sentiment"] = {
                "value": float(sym_sent) if sym_sent is not None else None,
                "effective": float(effective_sent),
                "global": float(global_sent),
                "global_has_data": bool(_global_sent_has_data),
                "any_data": bool(sentiment_has_data),
                "blended": sym_sent is not None,
                "symbol_weight": float(_sym_weight),
                "global_weight": float(1.0 - _sym_weight),
                "n_points": int(_sym_n),
            }
            # Reuse the already calibrated direction aggregate built before the bot loop.
            dtmp = dict(_dir_agg_cal)
            dtmp["direction_confidence_model"] = {
                "type": "platt_scaling_audit_only",
                "fitted": dir_calibrator.fitted,
                "a": getattr(dir_calibrator, "a", None),
                "b": getattr(dir_calibrator, "b", None),
                "policy_fingerprint": policy_fingerprint,
                "used_for_inference": False,
                "audit_probability": _direction_projection.get("audit_probability"),
                "reason": _direction_projection.get("reason"),
            }
            reasons2["direction_agg"] = dtmp
            reasons2["execution_constraints"] = {
                "raw_direction": raw_direction,
                "executable_direction": direction,
                "note": None,
            }
            reasons2["funding"] = {
                **(reasons2.get("funding") if isinstance(reasons2.get("funding"), dict) else {}),
                "funding_interval_min": cost_model.get("funding_interval_min"),
                "funding_interval_source": cost_model.get("funding_interval_source"),
                "funding_interval_uncertain": bool(cost_model.get("funding_interval_uncertain")),
                "expected_funding_events": cost_model.get("expected_funding_events"),
                "expected_funding_bps": cost_model.get("expected_funding_bps"),
            }
            # confidence_model reflects the calibrator ACTUALLY used (_cal_source set above).
            # Previously used _bot_cal_info presence to fill fields, which gave wrong
            # fitted/a/b when bot_cal existed-but-unfitted and global was used instead.
            reasons2["confidence_model"] = {
                "source": _cal_source,
                "type": "logreg_platt_v4_terminal_selected_policy_money" if _cal_source == "bot_logreg" else (
                    "raw_proxy" if _cal_source == "raw_proxy" else "raw"
                ),
                "fitted": _active_cal.fitted if _active_cal is not None else False,
                "n_samples": _active_cal.n_samples if _active_cal is not None and _active_cal.fitted else 0,
                "expectancy_status": str(getattr(bot_cal, "expectancy_status", "unknown")) if bot_cal is not None else "unknown",
                "return_samples": int(getattr(bot_cal, "return_samples", 0) or 0) if bot_cal is not None else 0,
                "weighted_mean_return": _finite_or_none(getattr(bot_cal, "weighted_mean_return", None)) if bot_cal is not None else None,
                "weighted_expected_shortfall": _finite_or_none(getattr(bot_cal, "weighted_expected_shortfall", None)) if bot_cal is not None else None,
                "weighted_return_std": _finite_or_none(getattr(bot_cal, "weighted_return_std", None)) if bot_cal is not None else None,
                "weighted_effective_return_samples": _finite_or_none(getattr(bot_cal, "weighted_effective_return_samples", None)) if bot_cal is not None else None,
                "weighted_mean_return_lower_bound": _finite_or_none(getattr(bot_cal, "weighted_mean_return_lower_bound", None)) if bot_cal is not None else None,
                "weighted_temporal_mean_return": _finite_or_none(getattr(bot_cal, "weighted_temporal_mean_return", None)) if bot_cal is not None else None,
                "weighted_temporal_return_std": _finite_or_none(getattr(bot_cal, "weighted_temporal_return_std", None)) if bot_cal is not None else None,
                "weighted_effective_temporal_clusters": _finite_or_none(getattr(bot_cal, "weighted_effective_temporal_clusters", None)) if bot_cal is not None else None,
                "weighted_temporal_mean_return_lower_bound": _finite_or_none(getattr(bot_cal, "weighted_temporal_mean_return_lower_bound", None)) if bot_cal is not None else None,
                "temporal_cluster_count": int(getattr(bot_cal, "temporal_cluster_count", 0) or 0) if bot_cal is not None else 0,
                "minimum_temporal_clusters": int(getattr(bot_cal, "minimum_temporal_clusters", 0) or 0) if bot_cal is not None else 0,
                "expectancy_confidence_level": _finite_or_none(getattr(bot_cal, "expectancy_confidence_level", None)) if bot_cal is not None else None,
                "policy_fingerprint": policy_fingerprint,
                "policy_matured_total": int(getattr(bot_cal, "policy_matured_total", 0) or 0) if bot_cal is not None else 0,
                "policy_labeled_total": int(getattr(bot_cal, "policy_labeled_total", 0) or 0) if bot_cal is not None else 0,
                "policy_censored_total": int(getattr(bot_cal, "policy_censored_total", 0) or 0) if bot_cal is not None else 0,
                "policy_unresolved_total": int(getattr(bot_cal, "policy_unresolved_total", 0) or 0) if bot_cal is not None else 0,
                "policy_invalid_labeled_total": int(getattr(bot_cal, "policy_invalid_labeled_total", 0) or 0) if bot_cal is not None else 0,
                "logreg_active": _cal_source in ("bot_logreg", "global_logreg"),
                "purged_oof_status": str(getattr(bot_cal, "oof_status", "not_evaluated")) if bot_cal is not None else "not_evaluated",
                "purged_oof_samples": int(getattr(bot_cal, "oof_samples", 0) or 0) if bot_cal is not None else 0,
                "purged_oof_required_samples": int(getattr(bot_cal, "oof_required_samples", 0) or 0) if bot_cal is not None else 0,
                "purged_oof_skill_status": str(getattr(bot_cal, "oof_skill_status", "not_evaluated")) if bot_cal is not None else "not_evaluated",
                "purged_oof_feature_log_loss": _finite_or_none(getattr(bot_cal, "oof_feature_log_loss", None)) if bot_cal is not None else None,
                "purged_oof_score_log_loss": _finite_or_none(getattr(bot_cal, "oof_score_log_loss", None)) if bot_cal is not None else None,
                "purged_oof_null_log_loss": _finite_or_none(getattr(bot_cal, "oof_null_log_loss", None)) if bot_cal is not None else None,
                "purged_oof_final_feature_log_loss": _finite_or_none(getattr(bot_cal, "oof_final_feature_log_loss", None)) if bot_cal is not None else None,
                "purged_oof_final_score_log_loss": _finite_or_none(getattr(bot_cal, "oof_final_score_log_loss", None)) if bot_cal is not None else None,
                "purged_oof_final_null_log_loss": _finite_or_none(getattr(bot_cal, "oof_final_null_log_loss", None)) if bot_cal is not None else None,
                "purged_oof_final_samples": int(getattr(bot_cal, "oof_final_samples", 0) or 0) if bot_cal is not None else 0,
                "purged_oof_required_final_samples": int(getattr(bot_cal, "oof_required_final_samples", 0) or 0) if bot_cal is not None else 0,
                "purged_oof_final_decision_cohorts": int(getattr(bot_cal, "oof_final_decision_cohorts", 0) or 0) if bot_cal is not None else 0,
                "purged_oof_required_final_decision_cohorts": int(getattr(bot_cal, "oof_required_final_decision_cohorts", 0) or 0) if bot_cal is not None else 0,
                "selected_policy_expectancy_status": str(getattr(bot_cal, "selected_policy_expectancy_status", "not_evaluated")) if bot_cal is not None else "not_evaluated",
                "selected_policy_confidence_threshold": _finite_or_none(getattr(bot_cal, "selected_policy_confidence_threshold", None)) if bot_cal is not None else None,
                "selected_policy_samples": int(getattr(bot_cal, "selected_policy_samples", 0) or 0) if bot_cal is not None else 0,
                "selected_policy_weighted_mean_return": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_mean_return", None)) if bot_cal is not None else None,
                "selected_policy_weighted_expected_shortfall": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_expected_shortfall", None)) if bot_cal is not None else None,
                "selected_policy_weighted_return_std": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_return_std", None)) if bot_cal is not None else None,
                "selected_policy_weighted_effective_return_samples": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_effective_return_samples", None)) if bot_cal is not None else None,
                "selected_policy_weighted_mean_return_lower_bound": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_mean_return_lower_bound", None)) if bot_cal is not None else None,
                "selected_policy_temporal_cluster_count": int(getattr(bot_cal, "selected_policy_temporal_cluster_count", 0) or 0) if bot_cal is not None else 0,
                "selected_policy_minimum_temporal_clusters": int(getattr(bot_cal, "selected_policy_minimum_temporal_clusters", 0) or 0) if bot_cal is not None else 0,
                "selected_policy_weighted_effective_temporal_clusters": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_effective_temporal_clusters", None)) if bot_cal is not None else None,
                "selected_policy_weighted_temporal_mean_return": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_temporal_mean_return", None)) if bot_cal is not None else None,
                "selected_policy_weighted_temporal_return_std": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_temporal_return_std", None)) if bot_cal is not None else None,
                "selected_policy_weighted_temporal_mean_return_lower_bound": _finite_or_none(getattr(bot_cal, "selected_policy_weighted_temporal_mean_return_lower_bound", None)) if bot_cal is not None else None,
                "terminal_selected_policy_expectancy_status": str(getattr(bot_cal, "terminal_selected_policy_expectancy_status", "not_evaluated")) if bot_cal is not None else "not_evaluated",
                "terminal_selected_policy_samples": int(getattr(bot_cal, "terminal_selected_policy_samples", 0) or 0) if bot_cal is not None else 0,
                "terminal_selected_policy_required_samples": int(getattr(bot_cal, "terminal_selected_policy_required_samples", 0) or 0) if bot_cal is not None else 0,
                "terminal_selected_policy_weighted_mean_return": _finite_or_none(getattr(bot_cal, "terminal_selected_policy_weighted_mean_return", None)) if bot_cal is not None else None,
                "terminal_selected_policy_weighted_mean_return_lower_bound": _finite_or_none(getattr(bot_cal, "terminal_selected_policy_weighted_mean_return_lower_bound", None)) if bot_cal is not None else None,
                "terminal_selected_policy_temporal_cluster_count": int(getattr(bot_cal, "terminal_selected_policy_temporal_cluster_count", 0) or 0) if bot_cal is not None else 0,
                "terminal_selected_policy_required_temporal_clusters": int(getattr(bot_cal, "terminal_selected_policy_required_temporal_clusters", 0) or 0) if bot_cal is not None else 0,
                "terminal_selected_policy_weighted_temporal_mean_return_lower_bound": _finite_or_none(getattr(bot_cal, "terminal_selected_policy_weighted_temporal_mean_return_lower_bound", None)) if bot_cal is not None else None,
                "a": getattr(getattr(_active_cal, "platt", None), "a", None) if _active_cal else None,
                "b": getattr(getattr(_active_cal, "platt", None), "b", None) if _active_cal else None,
                "heuristic_cap": float(_heur_cap) if _heur_cap is not None else None,
                "calibration_weight": float(_cal_weight),
                "confidence_gate_applied": bool(confidence_gate_applied),
                "confidence_gate_required": bool(settings.require_conf_gate),
                "note": (
                    "Raw heuristic confidence; treat it as an operator signal, not as calibrated probability."
                    if _active_cal is None
                    else "Bot-specific LogReg + Platt pipeline trained before the terminal holdout is active."
                ),
            }

            plan_rr = _plan_rr_metrics(params, cost_model)
            empirical = _empirical_expectancy_metrics(bot_cal)
            reasons2["operator_metrics"] = {
                "plan_rr": plan_rr,
                "empirical_expectancy": empirical,
                "heuristic_capture_score": {
                    "value": float(expected_rr),
                    "operator_visible": False,
                    "basis": "legacy_expected_rr_heuristic_capture_to_volatility_proxy",
                    "note": "Internal ranking diagnostic only; not rendered as operator reward/risk.",
                },
            }
            params["operator_metrics"] = {
                "plan_rr": plan_rr,
                "empirical_expectancy": empirical,
            }
            if isinstance(params.get("risk_report"), dict):
                params["risk_report"]["plan_rr"] = plan_rr.get("rr")
                params["risk_report"]["plan_projected_net_reward_usdt"] = plan_rr.get("projected_net_reward_usdt")
                params["risk_report"]["plan_kill_switch_loss_usdt"] = plan_rr.get("kill_switch_loss_usdt")
                params["risk_report"]["empirical_expectancy_status"] = empirical.get("status")
                params["risk_report"]["empirical_mean_return"] = empirical.get("mean_return")
                params["risk_report"]["empirical_rr"] = empirical.get("empirical_rr")

            recs.append({
                "rec_id": rec_id,
                "publication_root_rec_id": rec_id,
                "is_outcome_label_root": not trend_direction_rejected,
                "ts": ts_now,
                "venue": venue,
                "symbol": sym,
                "bot_type": bot_type,
                "direction": direction,
                "account_mode": account_mode,
                "margin_mode": margin_mode,
                "score": float(score),
                "confidence": float(conf),
                "expected_rr": float(expected_rr),
                "risk_score": float(risk_score),
                "params": params,
                "reasons": reasons2,
                "blocks": blocks,
                "status": status,
                "ttl_sec": _recommendation_ttl_sec(settings),
                "model_version": candidate_model_version,
                "features_ref_ts": int(f["ts_last"]),
            })

    status_counts = {"recommended": 0, "active": 0, "pending": 0, "blocked": 0, "no_trade": 0, "suppressed": 0}
    llm_review_stats = {"reviewed": 0, "vetoed": 0, "errors": 0, "skipped": 0}

    if recs:
        _apply_strategy_router(recs)

        # Apply persistence gate only to FINAL published recommendations.
        # This avoids confirming a bot that was internally recommended but then
        # suppressed by the cross-bot best-per-symbol selector.
        for r in recs:
            reasons = r.setdefault("reasons", {})
            if r.get("bot_type") not in PERSISTENCE_BOTS:
                reasons["publication_gate"] = {
                    "mode": "not_applicable",
                    "required_hits": 1,
                    "observed_hits": 1 if r.get("status") == "recommended" else 0,
                    "fresh_gap_sec": int(_fresh_gap),
                    "bypassed": False,
                    "passed": bool(r.get("status") == "recommended"),
                    "decision": "publish" if r.get("status") == "recommended" else "not_recommended",
                }
                continue
            if r.get("status") == "recommended":
                required_hits, gate_mode = _persistence_gate_requirements(r, settings)
                evidence_ref_ts = _safe_int_or_none(r.get("features_ref_ts"))
                count = _advance_persistence_gate(
                    str(r.get("venue") or ""),
                    str(r.get("symbol") or ""),
                    str(r.get("bot_type") or ""),
                    str(r.get("direction") or "neutral"),
                    ts_now,
                    _fresh_gap,
                    evidence_ts=evidence_ref_ts,
                )
                passed = count >= required_hits
                reasons["publication_gate"] = {
                    "mode": gate_mode,
                    "required_hits": int(required_hits),
                    "observed_hits": int(count),
                    "fresh_gap_sec": int(_fresh_gap),
                    "evidence_ref_ts": evidence_ref_ts,
                    "bypassed": False,
                    "passed": bool(passed),
                    "decision": "publish" if passed else "pending_confirmation",
                }
                if not passed:
                    r["status"] = "pending"
            else:
                reasons["publication_gate"] = {
                    "mode": "not_recommended",
                    "required_hits": 2,
                    "observed_hits": 0,
                    "fresh_gap_sec": int(_fresh_gap),
                    "bypassed": False,
                    "passed": False,
                    "decision": "not_recommended",
                }
                _reset_persistence_gate(
                    str(r.get("venue") or ""),
                    str(r.get("symbol") or ""),
                    str(r.get("bot_type") or ""),
                )

        _apply_recent_publication_dedupe(conn, recs, settings, ts_now)
        _check_heartbeat()
        _save_prev_recommended(conn, _prev_recommended, _fresh_gap, commit=False)
        _save_direction_state(conn, _direction_state_cache, _fresh_gap, commit=False)

        llm_reviewer = None
        if bool(getattr(settings, "llm_reviewer_enabled", False)):
            try:
                llm_reviewer = _make_llm_reviewer(settings)
            except Exception as exc:
                db.log_decision(conn, "LLM_REVIEW_ERROR", None, None, {"err": str(exc), "stage": "pending_annotation"}, commit=False)
        llm_review_stats = _mark_llm_reviews_async(conn, recs, settings, reviewer=llm_reviewer)

        for r in recs:
            _sync_recommendation_metadata(r)
            st = str(r.get("status") or "")
            if st in status_counts:
                status_counts[st] += 1
        db.insert_recommendations(conn, recs, commit=False)
        db.log_decision(
            conn,
            "PUBLISH",
            None,
            None,
            {
                "count_all": len(recs),
                "count_best": sum(
                    1
                    for r in recs
                    if str((((r.get("reasons") or {}).get("strategy_router") or {}).get("winner_rec_id") or ""))
                    == str(r.get("rec_id") or "")
                ),
                "count_recommended": status_counts["recommended"],
                "count_active": status_counts["active"],
                "count_pending": status_counts["pending"],
                "count_actionable": status_counts["recommended"] + status_counts["active"],
                "count_blocked": status_counts["blocked"],
                "count_no_trade": status_counts["no_trade"],
                "count_suppressed": status_counts["suppressed"],
                "count_strategy_recommendations": sum(
                    1 for r in recs
                    if str(r.get("candidate_kind") or "") == TREND_STRATEGY_RECOMMENDATION_KIND
                ),
                "count_trend_evaluations_rejected": sum(
                    1 for r in recs
                    if str(r.get("candidate_kind") or "") == TREND_EVALUATION_REJECTED_KIND
                ),
                "model_version": model_version,
                "model_versions": sorted({str(r.get("model_version") or "") for r in recs}),
                "regime": regime,
                "global_sentiment_6h": global_sent,
                "sentiment_regime": sent_agg.get("regime"),
                "sentiment_strength": sent_agg.get("strength"),
                "calibrator_fitted": calibrator.fitted,
                "llm_reviewer": {
                    "enabled": bool(getattr(settings, "llm_reviewer_enabled", False)),
                    "mode": getattr(settings, "llm_reviewer_mode", "advisory"),
                    "provider": getattr(settings, "llm_reviewer_provider", "ollama"),
                    "model": getattr(settings, "llm_reviewer_model", None),
                    **llm_review_stats,
                },
            },
            commit=False,
        )
        conn.commit()

    return {
        "regime": regime,
        "count": len(recs),
        "count_recommended": status_counts["recommended"],
        "count_active": status_counts["active"],
        "count_pending": status_counts["pending"],
        "count_actionable": status_counts["recommended"] + status_counts["active"],
        "count_blocked": status_counts["blocked"],
        "count_no_trade": status_counts["no_trade"],
        "count_suppressed": status_counts["suppressed"],
        "global_sentiment_6h": global_sent,
        "sentiment_regime": sent_agg.get("regime"),
        "sentiment_strength": sent_agg.get("strength"),
        "calibrator_fitted": calibrator.fitted,
        "llm_reviewer": {
            "enabled": bool(getattr(settings, "llm_reviewer_enabled", False)),
            "mode": getattr(settings, "llm_reviewer_mode", "advisory"),
            "provider": getattr(settings, "llm_reviewer_provider", "ollama"),
            "model": getattr(settings, "llm_reviewer_model", None),
            **llm_review_stats,
        },
    }
