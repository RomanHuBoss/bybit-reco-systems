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
from .outcomes import BOT_HORIZONS, _resolve_effective_horizon
from .bot_types import SUPPORTED_BOT_TYPES
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
from .calibration import (
    fit_platt, PlattScaler, save_platt_to_db, load_platt_from_db, BOT_CALIB_KEYS,
    LogRegScaler, fit_logreg, save_logreg_to_db, load_logreg_from_db,
    extract_features, GLOBAL_LOGREG_KEY, CALIB_REFIT_INTERVAL_SEC,
)
# Note: calibrators use db.get_outcomes_with_recs (single JOIN query) to avoid N+1 pattern

BOT_TYPES_BYBIT = list(SUPPORTED_BOT_TYPES)
MAX_FUNDING_STALENESS_SEC = 60 * 60
MAX_OI_STALENESS_SEC = 3 * 60 * 60
UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS: frozenset[str] = frozenset()
RECOMMENDER_MODEL_VERSION = "bybit-taxonomy-v3-mean-reversion"
DIRECTION_CALIBRATION_KEY = "platt_direction_v4"
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

    params = rec.get("params")
    if isinstance(params, dict):
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

    execution_cost_bps = max(0.0, fee_bps_round_trip + spread_bps_used + slippage_bps)
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
        "slippage_bps": float(slippage_bps),
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
) -> dict[str, float]:
    def _value_or_default(value: Any, default: float) -> float:
        return float(default if value is None else value)

    liq_map = {"micro": 0.0, "low": 0.33, "medium": 0.67, "high": 1.0, "unknown": 0.5}
    trendiness = abs(float(direction_agg.get("trendiness") or 0.0))
    dir_conf = direction_agg.get("direction_confidence_calibrated")
    if dir_conf is None:
        dir_conf = direction_agg.get("direction_confidence")
    spread_bps = cost_model.get("spread_bps")
    if spread_bps is None:
        spread_bps = cost_model.get("execution_cost_bps") or cost_model.get("total_cost_bps")
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
    return "neutral"


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


def _mean_reversion_grid_blocks(range_meta: dict[str, Any]) -> list[dict[str, str]]:
    """Classify independent oscillation evidence for the grid publication gate.

    Missing evidence is a hard data-quality block. A valid but weak edge is a
    strategy decision (``no_trade``), not a Bybit/risk/preflight failure.
    """
    meta = dict(range_meta or {})
    valid = bool(meta.get("mean_reversion_evidence_valid") is True)
    score = _clamp(_finite_float(meta.get("mean_reversion_score"), 0.0), 0.0, 1.0)
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
    if score < 0.55:
        return [{
            "code": "MEAN_REVERSION_EDGE_UNCONFIRMED",
            "decision": "no_trade",
            "msg": (
                f"mean_reversion_score={score:.2f} < 0.55; рынок может быть driftless/random-walk, "
                "а не повторяемым диапазоном, поэтому комиссии дают отрицательное ожидание"
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
    if bot_type == "futures_grid":
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
    else:
        raw = 0.0

    score = float(_clamp(raw / 2.2, -1.0, 1.0))
    conf0 = float(_clamp(_sigmoid(raw * 2.1), 0.0, 1.0))
    reasons = {
        "summary": "Рекомендация оценивает пригодность символа для grid-стратегии: ищется диапазонный рынок с контролируемой волатильностью, приемлемыми издержками и исполнимым bias по направлению.",
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

    gross_capture = max(0.0, (0.55 * range_score + 0.15 * coherence - 0.20 * trend_strength) * max(atr_pct, 0.0025))
    net_capture = gross_capture - net_cost_pct
    risk_proxy = max(max(atr_pct, 0.0025) * 1.5, execution_cost_pct * 2.0, 1e-6)
    return float(_clamp(net_capture / risk_proxy, 0.0, 3.0))


def _mode(venue: str, direction: str) -> tuple[str, str]:
    if venue == "linear":
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
        funding_cost_bps_for_spacing = max(0.0, _finite_float(cost_model.get("expected_funding_bps"), 0.0))
        execution_cost_bps = max(
            0.0,
            _finite_float(
                cost_model.get("total_cost_bps")
                or cost_model.get("execution_cost_bps")
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

    execution_cost_bps = max(
        0.0,
        float(cost_model.get("total_cost_bps") or cost_model.get("execution_cost_bps") or max(0.0, float(taker_fee_bps) * 2.0)),
    )
    # Grid spacing must cover not only fees/spread/slippage but also the adverse
    # expected funding carry for the planned horizon. Received funding is deliberately
    # excluded from the approval edge because it can flip by the time inventory is
    # accumulated. Without this, high-funding symbols could still render a visually
    # dense grid whose gross step cannot plausibly pay the carry, only to be blocked
    # later as GRID_NET_PROFIT_NON_POSITIVE. Build the geometry fail-closed instead.
    funding_cost_bps_for_spacing = max(0.0, _finite_float(cost_model.get("expected_funding_bps"), 0.0))
    cost_floor_bps_for_spacing = execution_cost_bps + funding_cost_bps_for_spacing
    economic_cost_bps_for_density = max(
        cost_floor_bps_for_spacing,
        _finite_float(cost_model.get("net_cost_bps"), 0.0),
    )
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
        execution_cost_bps=cost_model.get("execution_cost_bps") or execution_cost_bps,
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
PERSISTENCE_BOTS: set[str] = {"futures_grid"}
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


def _current_range_edge_calibration_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only outcomes produced by the current independently validated range model.

    Old scores and feature snapshots encoded ``range = 1 - trend``.  Mixing them
    with the new anti-persistence feature would create train/inference skew and
    could resurrect the exact false edge this release blocks.
    """
    accepted: list[dict[str, Any]] = []
    for row in rows or []:
        model_version = str(row.get("model_version") or "").strip()
        if not (model_version == RECOMMENDER_MODEL_VERSION or model_version.startswith(RECOMMENDER_MODEL_VERSION + "+")):
            continue
        reasons = row.get("reasons") or {}
        if not isinstance(reasons, dict):
            continue
        snapshot = reasons.get("feature_snapshot") or {}
        if not isinstance(snapshot, dict):
            continue
        evidence_flag = _safe_int_or_none(snapshot.get("mean_reversion_evidence_valid"))
        score = _finite_or_none(snapshot.get("mean_reversion_score"))
        if evidence_flag != 1 or score is None or not (0.0 <= score <= 1.0):
            continue
        accepted.append(row)
    return accepted


def _fit_global_logreg(conn, min_samples: int) -> LogRegScaler:
    """Fit global LogReg+Platt only on the current range-edge feature schema."""
    rows = db.get_outcomes_with_recs(conn, limit=6000, require_llm_verdict=bool(getattr(settings, "llm_reviewer_enabled", False)))
    return fit_logreg(_current_range_edge_calibration_rows(rows), min_samples=min_samples)


def _fit_bot_logregs(conn, min_samples: int) -> dict[str, LogRegScaler]:
    """Fit one LogReg+Platt per bot_type."""
    from collections import defaultdict
    rows = db.get_outcomes_with_recs(conn, limit=8000, require_llm_verdict=bool(getattr(settings, "llm_reviewer_enabled", False)))
    rows = _current_range_edge_calibration_rows(rows)
    data: dict[str, list] = defaultdict(list)
    for row in rows:
        data[row["bot_type"]].append(row)

    result: dict[str, LogRegScaler] = {}
    for bt, bt_rows in data.items():
        if bt in UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS:
            result[bt] = LogRegScaler(fitted=False)
            continue
        model = fit_logreg(bt_rows, min_samples=min_samples)
        if model.fitted:
            save_logreg_to_db(conn, BOT_CALIB_KEYS.get(bt, f"logreg_{bt}_v1"), model)
        result[bt] = model
    return result


def _load_or_fit_global_logreg(conn, min_samples: int) -> LogRegScaler:
    """Load global calibrator; re-fit if missing or older than CALIB_REFIT_INTERVAL_SEC."""
    import time as _time
    saved = load_logreg_from_db(conn, GLOBAL_LOGREG_KEY)
    if saved and saved.fitted:
        if int(_time.time()) - saved.saved_ts < CALIB_REFIT_INTERVAL_SEC:
            return saved
    model = _fit_global_logreg(conn, min_samples=min_samples)
    if model.fitted:
        save_logreg_to_db(conn, GLOBAL_LOGREG_KEY, model)
    elif saved and saved.fitted:
        return saved  # keep stale if not enough data yet
    return model


def _load_or_fit_bot_logregs(conn, min_samples: int) -> dict[str, LogRegScaler]:
    """Load per-bot calibrators; re-fit stale or missing ones."""
    import time as _time
    now = int(_time.time())
    calibrators: dict[str, LogRegScaler] = {}
    needs_refit: list[str] = []

    for bt, key in BOT_CALIB_KEYS.items():
        if bt in UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS:
            calibrators[bt] = LogRegScaler(fitted=False)
            continue
        saved = load_logreg_from_db(conn, key)
        if saved and saved.fitted:
            if now - saved.saved_ts < CALIB_REFIT_INTERVAL_SEC:
                calibrators[bt] = saved
                continue
        calibrators[bt] = saved if (saved and saved.fitted) else LogRegScaler(fitted=False)
        needs_refit.append(bt)

    if needs_refit:
        fitted = _fit_bot_logregs(conn, min_samples)
        for bt in needs_refit:
            if bt in fitted and fitted[bt].fitted:
                calibrators[bt] = fitted[bt]

    return calibrators


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


def _fit_direction_calibrator(conn, min_samples: int) -> PlattScaler:
    """Fit direction calibrator on supported directional outcomes.

    We calibrate the *raw direction confidence* (or unsigned strength fallback),
    not the signed aggregate score. This preserves symmetry between strong longs
    and strong shorts and makes the resulting value a true probability-like metric.
    """
    rows = db.get_outcomes_with_recs(conn, limit=5000, require_llm_verdict=bool(getattr(settings, "llm_reviewer_enabled", False)))
    rows = _current_range_edge_calibration_rows(rows)
    xs, ys = [], []
    for row in rows:
        if row["bot_type"] != "futures_grid":
            continue
        d = (row.get("reasons") or {}).get("direction_agg") or {}
        if str(d.get("direction") or "neutral") == "neutral":
            continue
        xs.append(_raw_direction_confidence(d))
        ys.append(int(row["success"]))
    return fit_platt(xs, ys, min_samples=min_samples) if len(xs) >= min_samples else PlattScaler(fitted=False)


def _load_or_fit_direction_calibrator(conn, min_samples: int) -> PlattScaler:
    """Load direction calibrator; re-fit if missing or older than CALIB_REFIT_INTERVAL_SEC."""
    import time as _time
    key = DIRECTION_CALIBRATION_KEY
    saved = load_platt_from_db(conn, key)
    if saved and saved.fitted:
        if int(_time.time()) - saved.saved_ts < CALIB_REFIT_INTERVAL_SEC:
            return saved
    scaler = _fit_direction_calibrator(conn, min_samples=min_samples)
    if scaler.fitted:
        save_platt_to_db(conn, key, scaler)
    elif saved and saved.fitted:
        return saved
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
        publication_root_rec_id = str(row["publication_root_rec_id"] or row["rec_id"]).strip() or str(row["rec_id"])
        expiry = db.recommendation_chain_expiry_context(
            conn,
            rec_id=str(row["rec_id"]),
            publication_root_rec_id=publication_root_rec_id,
            row_ts=row["ts"],
            ttl_sec=row["ttl_sec"],
            ts_now=ts_now,
        )
        if expiry.get("is_publication_chain_expired"):
            continue
        return {
            "rec_id": row["rec_id"],
            "ts": row["ts"],
            "score": row["score"],
            "confidence": row["confidence"],
            "expected_rr": row["expected_rr"],
            "status": row["status"],
            "params": db._json_loads_or_default(row["params_json"], {}),
            "publication_root_rec_id": publication_root_rec_id,
            "is_outcome_label_root": bool(int(row["is_outcome_label_root"] or 0)),
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
                  r.params_json, r.reasons_json, r.publication_root_rec_id, r.is_outcome_label_root
           FROM recommendations r
           LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
           WHERE r.venue=? AND r.symbol=? AND r.bot_type=? AND r.direction=?
             AND COALESCE(r.is_outcome_label_root, 1) = 1
             AND o.rec_id IS NULL
             AND r.status NOT IN ('blocked', 'no_trade', 'suppressed', 'expired', 'ignored')
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
        if not _recommendation_row_is_publication_actionable(row):
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
        publication_root_rec_id = str(row["publication_root_rec_id"] or row["rec_id"]).strip() or str(row["rec_id"])
        expiry = db.recommendation_chain_expiry_context(
            conn,
            rec_id=str(row["rec_id"]),
            publication_root_rec_id=publication_root_rec_id,
            row_ts=row["ts"],
            ttl_sec=row["ttl_sec"],
            ts_now=ts_now,
        )
        if expiry.get("is_publication_chain_expired"):
            continue
        return {
            "rec_id": row["rec_id"],
            "ts": row["ts"],
            "score": row["score"],
            "confidence": row["confidence"],
            "expected_rr": row["expected_rr"],
            "status": row["status"],
            "params": params if isinstance(params, dict) else {},
            "publication_root_rec_id": publication_root_rec_id,
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
        if not _is_llm_review_eligible_status(rec.get("status")):
            continue

        prev_open_root = _find_open_publication_position(conn, rec, ts_now, fallback_horizon_sec)
        if prev_open_root is not None:
            _material_upgrade_ignored, diagnostics = _recent_publication_dedupe_material_upgrade(prev_open_root, rec)
            reasons = rec.setdefault("reasons", {})
            previous_root_rec_id = str(prev_open_root.get("publication_root_rec_id") or prev_open_root.get("rec_id") or "").strip() or str(prev_open_root.get("rec_id") or "")
            reasons["publication_dedupe"] = {
                "cooldown_sec": int(cooldown_sec),
                "previous_rec_id": prev_open_root.get("rec_id"),
                "previous_root_rec_id": previous_root_rec_id,
                "previous_ts": prev_open_root.get("ts"),
                "previous_status": prev_open_root.get("status"),
                "decision": "reuse_active",
                "active_reuse": True,
                "suppressed": False,
                "material_upgrade": False,
                "open_position_lock": True,
                "lock_reason": "existing_same_direction_pseudo_position",
                "effective_horizon_sec": int(prev_open_root.get("effective_horizon_sec") or 0),
                "lock_until_ts": int(prev_open_root.get("lock_until_ts") or 0),
                **diagnostics,
            }
            rec["status"] = "active"
            rec["publication_root_rec_id"] = previous_root_rec_id
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
            rec["is_outcome_label_root"] = True
        else:
            rec["status"] = "active"
            rec["publication_root_rec_id"] = previous_root_rec_id
            rec["is_outcome_label_root"] = False


def run_recommender_once(conn, settings, *, heartbeat=None) -> dict[str, Any]:
    global _prev_recommended, _direction_state_cache

    def _check_heartbeat() -> None:
        if heartbeat is not None and not heartbeat():
            raise RuntimeLockLostError("reco runtime lock lost")
    _fresh_gap = _persistence_fresh_gap(settings)
    _prev_recommended = _load_prev_recommended(conn)
    _direction_state_cache = _load_direction_state(conn)
    sent_agg = compute_sentiment_agg(conn, scope="global", key="crypto")
    # Primary sentiment for scoring: adaptive blend from compute_sentiment_agg.
    # Falls back to 6h EWMA for backward compatibility with older snapshots.
    global_sent = _finite_float(sent_agg.get("effective_score", sent_agg.get("ewma", {}).get("6h", 0.0)), 0.0)
    # Per-symbol sentiment map: {SYMBOL: float} blended from RSS/Reddit/CoinGecko
    symbol_sent_map: dict[str, tuple[float, int]] = compute_symbol_sentiment_map(conn)

    # LogReg+Platt calibrators (new) — replace legacy Platt-on-score
    global_calibrator  = _load_or_fit_global_logreg(conn, min_samples=settings.calib_min_samples)
    bot_calibrators    = _load_or_fit_bot_logregs(conn, min_samples=settings.calib_min_samples)
    dir_calibrator     = _load_or_fit_direction_calibrator(conn, min_samples=settings.calib_min_samples)
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

    limits = normalize_risk_limits(db.get_active_risk_limits(conn), settings.risk_limits)
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
            if bot_type == "futures_grid" and venue != "linear":
                continue

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
            _xdir_pre = _raw_direction_confidence(_dir_agg_raw)
            _dir_conf_pre = dir_calibrator.predict(_xdir_pre) if dir_calibrator.fitted else _xdir_pre
            _dir_agg_cal = dict(_dir_agg_raw)
            _dir_agg_cal["direction_confidence_calibrated"] = _dir_conf_pre

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
                for _mr_decision in _mean_reversion_grid_blocks(_range_meta_now):
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
            # If symbol is highly correlated to BTC, direction is less independent
            beta_info = f.get("_btc_beta", {})
            if beta_info.get("is_btc_driven") and sym != "BTCUSDT":
                _dir_conf_pre = float(_clamp(_dir_conf_pre * 0.88, 0.0, 0.99))
                _dir_agg_cal["direction_confidence_calibrated"] = _dir_conf_pre
            # Block threshold 0.05 = 5% 1h ATR. Old value 0.018 was calibrated for 1m ATR
            # and blocked ALL symbols since typical 1h ATR for small caps is 3–8%.
            # ── Risk gate — uses cached risk_status (computed once per cycle) ──
            risk_blocks = gate_candidate(conn, venue, sym, limits, cached_status=_cached_risk_status)
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
            )

            _reasons_for_cal = {
                "effective_sentiment": effective_sent,
                "cost_model": cost_model,
                "direction_agg": _dir_agg_for_cal,  # includes calibrated dir_conf
                "feature_snapshot": dict(feature_snapshot),
                "top_positive_factors": (reasons.get("top_positive_factors") or []),
                "top_negative_factors": (reasons.get("top_negative_factors") or []),
            }
            _row_for_cal = {"score": score, "reasons": _reasons_for_cal, "success": 0}
            _fv = extract_features(_row_for_cal)

            bot_cal = bot_calibrators.get(bot_type)
            if bot_cal and bot_cal.fitted and len(bot_cal.coef) > 0 and _fv is not None:
                conf_cal = float(bot_cal.predict(_fv))
                _cal_source = "bot_logreg"
                _active_cal = bot_cal
            elif bot_cal and bot_cal.fitted:
                conf_cal = float(bot_cal.predict_score_only(score))
                _cal_source = "bot_platt"
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

            # OI unwinding → reduce confidence
            if venue == "linear" and oi_sig["signal"] == "caution":
                conf = float(_clamp(conf * 0.88, 0.0, 1.0))
            if str((market_shock or {}).get("severity") or "normal") == "guarded":
                conf = float(_clamp(conf * 0.93, 0.0, 1.0))

            expected_rr = _expected_rr(bot_type, f, cost_model=cost_model)
            account_mode, margin_mode = _mode(venue, direction)

            blocks = list(feasibility_blocks)  # risk_blocks already included via feasibility_blocks.extend()

            confidence_gate_applied = bool(settings.require_conf_gate and _active_cal is not None)

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
                    "msg": "market reference price is missing/non-positive/non-finite; futures-grid recommendation is blocked fail-closed",
                })
            # Add execution guide for UI "Details" panel.
            params["trade_plan"] = _build_trade_plan(bot_type, venue, f, direction, params, cost_model=cost_model)
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
            execution_cost_bps = _finite_or_none(cost_model.get("execution_cost_bps")) or _finite_or_none(cost_model.get("total_cost_bps")) or 0.0
            if bot_type == "futures_grid":
                if net_profit_bps is None:
                    blocks.append({"code": "GRID_ECONOMICS_MISSING", "msg": "нет net profit per grid — рекомендация не исполнима без экономики сетки"})
                elif net_profit_bps <= 0.0:
                    blocks.append({"code": "GRID_NET_PROFIT_NON_POSITIVE", "msg": f"net_profit_per_grid={net_profit_bps:.2f} bps после fees/spread/slippage/funding <= 0"})
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

            rec_id = f"R-{ts_now}-{venue}-{sym}-{bot_type}-{secrets.token_hex(4)}"
            reasons2 = dict(reasons)
            reasons2["regime"] = regime
            reasons2["risk_checks"] = {"passed": len(blocks)==0, "blocks": blocks}
            reasons2["grid_economics"] = params.get("economics")
            reasons2["sizing"] = params.get("sizing")
            thesis_ok = bool(score >= settings.min_score_to_recommend and (not confidence_gate_applied or conf >= settings.min_conf_to_recommend))
            reasons2["decision_layers"] = {
                "thesis_status": "favored" if thesis_ok else "unfavorable",
                "execution_status": "blocked" if blocks else ("not_actionable" if no_trade_reasons else "allowed"),
                "final_status": status,
                "no_trade_reasons": no_trade_reasons,
                "score_threshold": float(settings.min_score_to_recommend),
                "confidence_threshold": float(settings.min_conf_to_recommend),
                "confidence_gate_applied": bool(confidence_gate_applied),
            }
            _trade_plan_complete = isinstance(params.get("trade_plan"), dict) and bool(params.get("trade_plan"))
            _shadow_no_trade_eligible = bool(
                status == "no_trade"
                and not blocks
                and _trade_plan_complete
                and params.get("price_input_valid") is not False
            )
            reasons2["outcome_policy"] = {
                "eligible": bool(status in {"recommended", "active", "executed"} or _shadow_no_trade_eligible),
                "sample_role": (
                    "shadow_no_trade" if _shadow_no_trade_eligible else (
                        "actionable_root" if status in {"recommended", "active", "executed"} else "excluded"
                    )
                ),
                "reason": (
                    "model_thesis_or_launch_gate" if _shadow_no_trade_eligible else (
                        "actionable_publication" if status in {"recommended", "active", "executed"} else "hard_or_incomplete_candidate"
                    )
                ),
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
            dtmp["direction_confidence_model"] = {"type":"platt_scaling","fitted": dir_calibrator.fitted, "a": getattr(dir_calibrator,"a",None), "b": getattr(dir_calibrator,"b",None)}
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
                "type": "logreg_platt_v1" if _cal_source in ("bot_logreg", "global_logreg") else (
                    "platt_only" if _cal_source in ("bot_platt", "global_platt") else ("raw_proxy" if _cal_source == "raw_proxy" else "raw")
                ),
                "fitted": _active_cal.fitted if _active_cal is not None else False,
                "n_samples": _active_cal.n_samples if _active_cal is not None and _active_cal.fitted else 0,
                "logreg_active": _cal_source in ("bot_logreg", "global_logreg"),
                "a": getattr(getattr(_active_cal, "platt", None), "a", None) if _active_cal else None,
                "b": getattr(getattr(_active_cal, "platt", None), "b", None) if _active_cal else None,
                "heuristic_cap": float(_heur_cap) if _heur_cap is not None else None,
                "calibration_weight": float(_cal_weight),
                "confidence_gate_applied": bool(confidence_gate_applied),
                "note": (
                    "Raw heuristic confidence; treat it as an operator signal, not as calibrated probability."
                    if _active_cal is None else (
                        "Bot-specific LogReg + Platt calibration is active."
                        if _cal_source == "bot_logreg" else "Bot-specific Platt-only calibration is active."
                    )
                ),
            }

            recs.append({
                "rec_id": rec_id,
                "publication_root_rec_id": rec_id,
                "is_outcome_label_root": True,
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
                "model_version": model_version,
                "features_ref_ts": int(f["ts_last"]),
            })

    status_counts = {"recommended": 0, "active": 0, "pending": 0, "blocked": 0, "no_trade": 0, "suppressed": 0}
    llm_review_stats = {"reviewed": 0, "vetoed": 0, "errors": 0, "skipped": 0}

    if recs:
        # Publish only one best recommendation per (venue, symbol).
        # Non-winning actionable ideas are preserved as suppressed alternatives with an explicit reason.
        STATUS_PRIORITY = {"recommended": 0, "active": 1, "pending": 2, "blocked": 3, "no_trade": 4, "suppressed": 5}
        best_map: dict[tuple[str, str], dict[str, Any]] = {}
        for r in recs:
            key = (r["venue"], r["symbol"])
            cur = best_map.get(key)
            if cur is None:
                best_map[key] = r
                continue
            r_pri  = STATUS_PRIORITY.get(r["status"], 9)
            c_pri  = STATUS_PRIORITY.get(cur["status"], 9)
            if r_pri < c_pri:
                best_map[key] = r  # better status wins unconditionally
            elif r_pri == c_pri:
                if r["confidence"] > cur["confidence"] or (
                    r["confidence"] == cur["confidence"] and r["score"] > cur["score"]
                ):
                    best_map[key] = r

        for r in recs:
            key = (r["venue"], r["symbol"])
            if best_map.get(key, {}).get("rec_id") != r["rec_id"]:
                # Only suppress live candidates — preserve 'blocked'/'no_trade' for audit
                if r["status"] in {"recommended", "active", "pending"}:
                    winner = best_map.get(key, {})
                    reasons = r.setdefault("reasons", {})
                    reasons["suppression"] = {
                        "reason": "cross_bot_competition",
                        "winner_rec_id": winner.get("rec_id"),
                        "winner_bot_type": winner.get("bot_type"),
                        "winner_status": winner.get("status"),
                        "winner_confidence": float(winner.get("confidence") or 0.0),
                        "winner_score": float(winner.get("score") or 0.0),
                    }
                    r["status"] = "suppressed"

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
                "count_best": len(best_map),
                "count_recommended": status_counts["recommended"],
                "count_active": status_counts["active"],
                "count_pending": status_counts["pending"],
                "count_actionable": status_counts["recommended"] + status_counts["active"],
                "count_blocked": status_counts["blocked"],
                "count_no_trade": status_counts["no_trade"],
                "count_suppressed": status_counts["suppressed"],
                "model_version": model_version,
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
