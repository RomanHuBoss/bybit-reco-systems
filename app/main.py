from __future__ import annotations

import copy
import json
import math
import os
import secrets
import threading
import socket
import time
from collections import Counter
from functools import partial
from contextlib import closing, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt

from .settings import load_settings
from .shock_guard import (
    APP_CONFIG_KEY as MARKET_SHOCK_APP_KEY,
    apply_market_shock_gate,
    compute_symbol_fast_veto,
)
from .bybit_client import BybitPublicClient
from .collector import collect_once, collect_backfill_once, collect_futures_once, RuntimeLockLostError
from .alerts import check_and_alert
from .sentiment import collect_sentiment_once
from .outcomes import BOT_HORIZONS, compute_outcomes_cycle
from .recommender import (
    CALIBRATION_EVIDENCE_REASON_CODES,
    DIRECTION_CALIBRATION_KEY,
    LLM_REVIEW_ASYNC_STATUS_APP_KEY,
    RECOMMENDER_MODEL_VERSION,
    calibration_policy_contract,
    calibration_policy_fingerprint,
    calibration_lineage_diagnostics,
    policy_calibration_storage_key,
    run_llm_review_sweep_once,
    run_recommender_once,
)
from .risk import get_risk_limits, compute_risk_status, gate_candidate, normalize_risk_limits
from .security import is_authorized
from . import db
from .db_backend import describe_target
from .bot_types import sql_in_clause
from .grid_math import (
    arithmetic_grid_commitment,
    arithmetic_grid_cross_margin_stress,
    quantize_step,
    resolve_integer_aliases,
    strict_integer,
)
from .trading_semantics import (
    bybit_linear_protective_order_plan,
    directional_exit_levels,
    directional_trade_math,
    normalize_execution_direction,
    validate_directional_exit_geometry,
)
import logging

logger = logging.getLogger(__name__)
settings = load_settings()
RUNTIME_OWNER = f"{socket.gethostname()}:{os.getpid()}"
PROCESS_STARTED_TS = int(time.time())
OUTCOME_LABEL_VERSION = "grid_label_v26"
INSTRUMENT_META_CACHE_TTL_SEC = 15 * 60
INSTRUMENT_META_NEGATIVE_CACHE_TTL_SEC = 30
SUPPORTED_RECOMMENDER_GRID_TYPE = "arithmetic"
BYBIT_FUTURES_GRID_MIN_COUNT = 2
BYBIT_FUTURES_GRID_MAX_COUNT = 400
EXECUTION_FUNDING_MAX_STALENESS_SEC = 60 * 60
EXECUTION_FUNDING_WORSE_DELTA_BLOCK_BPS = 3.0
EXECUTION_FUNDING_EXTREME_BPS = 6.0
EXECUTION_MIN_NET_PROFIT_BPS = 2.0
EXECUTION_MAX_LIVE_SPREAD_BPS = 14.0
EXECUTION_GRID_SLIPPAGE_SPREAD_MULTIPLIER = 0.35
EXECUTION_MIN_LINEAR_SLIPPAGE_BPS = 1.0
OPERATOR_MIN_LIQUIDATION_BUFFER_PCT = 12.0
RECOMMENDATION_MAX_FUTURE_SKEW_SEC = 300
EXECUTION_GROSS_COST_COVERAGE_MULTIPLIER = 1.10
BACKGROUND_THREAD_STATE_APP_KEY_PREFIX = "runtime_thread_state:"
BACKGROUND_THREAD_RESTART_DELAY_SEC = 5.0
OUTCOME_BACKLOG_CATCHUP_SEC = 5
BACKGROUND_THREAD_ERROR_ACTIONS = {
    "collector": "COLLECT_ERROR",
    "reco": "RECO_ERROR",
    "outcomes": "OUTCOME_WORKER_ERROR",
    "sentiment": "SENTIMENT_ERROR",
    "llm_reviewer": "LLM_REVIEW_SWEEP_ERROR",
    "backfill": "COLLECT_ERROR",
}
_instrument_meta_cache: dict[tuple[str, str], tuple[float, dict[str, Any], bool]] = {}
_instrument_meta_lock = threading.Lock()
_BACKGROUND_STOP_EVENT = threading.Event()
_BACKGROUND_THREADS: list[threading.Thread] = []
_BACKGROUND_THREADS_LOCK = threading.Lock()


def _get_conn():
    return db.connect(settings.db_path)


def _get_lock_conn():
    return db.connect(settings.runtime_lock_db_path)


def _interval_loop_start(interval_sec: int) -> float:
    return time.monotonic() + max(1, int(interval_sec))


def _interval_loop_wait(next_run: float, interval_sec: int) -> float:
    interval = max(1, int(interval_sec))
    now = time.monotonic()
    if now < next_run:
        # Ожидание делаем прерываемым через общий stop-event. Иначе при shutdown
        # поток может висеть в `sleep()` ещё весь интервал и переживать lifespan.
        _BACKGROUND_STOP_EVENT.wait(next_run - now)
    # If the previous iteration overran, do not add another full sleep on top.
    return max(next_run + interval, time.monotonic())


def _start_background_thread(name: str, target) -> None:
    thread = threading.Thread(target=target, name=name, daemon=True)
    with _BACKGROUND_THREADS_LOCK:
        _BACKGROUND_THREADS.append(thread)
    thread.start()


def _join_background_threads(timeout_sec: float = 1.0) -> None:
    with _BACKGROUND_THREADS_LOCK:
        threads = list(_BACKGROUND_THREADS)
        _BACKGROUND_THREADS.clear()
    for thread in threads:
        join = getattr(thread, "join", None)
        if not callable(join):
            continue
        try:
            join(timeout=max(0.0, float(timeout_sec)))
        except Exception:
            logger.debug("background thread join failed: %s", getattr(thread, "name", "unknown"), exc_info=True)


def _process_memory_snapshot() -> dict[str, Any]:
    """Return Linux process-memory diagnostics without adding a psutil dependency."""
    snapshot: dict[str, Any] = {
        "pid": int(os.getpid()),
        "runtime_owner": RUNTIME_OWNER,
        "thread_count": int(threading.active_count()),
        "rss_mb": None,
        "peak_rss_mb": None,
    }
    try:
        status_text = Path("/proc/self/status").read_text(encoding="utf-8", errors="replace")
        for line in status_text.splitlines():
            if line.startswith("VmRSS:"):
                snapshot["rss_mb"] = round(int(line.split()[1]) / 1024.0, 2)
            elif line.startswith("VmHWM:"):
                snapshot["peak_rss_mb"] = round(int(line.split()[1]) / 1024.0, 2)
            elif line.startswith("Threads:"):
                snapshot["thread_count"] = int(line.split()[1])
    except Exception:
        logger.debug("process memory snapshot unavailable", exc_info=True)
    return snapshot


def _bootstrap_db() -> None:
    with closing(_get_conn()) as conn:
        db.init_db(conn)
        active = db.get_active_risk_limits(conn)
        if not active:
            bootstrap_limits = normalize_risk_limits(settings.risk_limits, settings.risk_limits)
            db.upsert_risk_limits(conn, version="bootstrap", limits=bootstrap_limits, is_active=True)

        current_label_version = db.get_app_config_json(conn, "outcome_label_version")
        if current_label_version != OUTCOME_LABEL_VERSION:
            deleted_outcomes = conn.execute("DELETE FROM reco_outcomes").rowcount
            # Outcome labels define the target used by every persisted calibrator.
            # Remove every historical key family, not just the currently imported
            # identity, so an older coefficient set cannot survive a later label
            # contract reset and be revived by rollback or compatibility code.
            deleted_calibrators = conn.execute(
                "DELETE FROM app_config WHERE key LIKE ? OR key LIKE ?",
                ("logreg_%", "platt_direction_%"),
            ).rowcount
            db.set_app_config_json(conn, "outcome_label_version", OUTCOME_LABEL_VERSION)
            db.log_decision(
                conn,
                "OUTCOME_LABEL_VERSION_RESET",
                None,
                None,
                {
                    "version": OUTCOME_LABEL_VERSION,
                    "previous_version": current_label_version,
                    "deleted_outcomes": int(deleted_outcomes or 0),
                    "deleted_calibrators": int(deleted_calibrators or 0),
                },
            )


_bootstrap_db()
with closing(_get_lock_conn()) as lock_conn:
    db.init_runtime_lock_db(lock_conn)
logger.info("db_target=%s", describe_target(settings.db_path))
logger.info("runtime_lock_target=%s", describe_target(settings.runtime_lock_db_path))


def _fetch_bybit_instrument_meta(venue: str, symbol: str) -> dict[str, Any]:
    # Product boundary: this service must only validate Bybit Linear USDT Futures.
    # Do not silently fetch linear metadata for a non-linear venue because that can
    # make legacy/unsupported payloads look execution-ready.
    venue_norm = str(venue or "").strip().lower()
    symbol_norm = str(symbol or "").strip().upper()
    if venue_norm != "linear" or not symbol_norm:
        return {}
    category = "linear"
    cache_key = (venue_norm, symbol_norm)
    now = time.time()
    with _instrument_meta_lock:
        cached = _instrument_meta_cache.get(cache_key)
        if cached is not None:
            try:
                cached_ts, cached_value, cache_ok = cached
            except Exception:
                cached_ts, cached_value, cache_ok = 0.0, {}, False
            ttl = INSTRUMENT_META_CACHE_TTL_SEC if bool(cache_ok) else INSTRUMENT_META_NEGATIVE_CACHE_TTL_SEC
            if now - float(cached_ts) <= ttl:
                return dict(cached_value)
            _instrument_meta_cache.pop(cache_key, None)
    client = BybitPublicClient(settings.bybit_base_url)
    cache_ok = False
    meta: dict[str, Any] = {}
    try:
        info = client.get_instrument_info(category, symbol_norm)
    except Exception as exc:
        logger.warning("instrument meta fetch failed for %s/%s: %s", venue, symbol, exc)
        info = None
    finally:
        try:
            client.close()
        except Exception:
            pass

    if info:
        price_filter = info.get("priceFilter") or {}
        lot_filter = info.get("lotSizeFilter") or {}
        # В кэш и в preflight кладём именно то, что реально пришло от upstream.
        # Иначе symbol/category mismatch валидация вырождается: если здесь всегда
        # сохранить запрошенные значения, то downstream никогда не заметит, что
        # прокси/stub вернул metadata другого инструмента.
        meta = {
            # Не подставляем запрошенные category/symbol вместо отсутствующих:
            # strict preflight должен видеть неполную/malformed metadata и блокировать запуск.
            "category": str(info.get("category") or "").strip().lower(),
            "symbol": str(info.get("symbol") or "").strip().upper(),
            "status": str(info.get("status") or "").strip(),
            "base_coin": str(info.get("baseCoin") or "").strip().upper(),
            "quote_coin": str(info.get("quoteCoin") or "").strip().upper(),
            "settle_coin": str(info.get("settleCoin") or "").strip().upper(),
            "contract_type": str(info.get("contractType") or "").strip(),
            "delivery_time": info.get("deliveryTime"),
            "funding_interval_min": info.get("fundingInterval"),
            "upper_funding_rate": info.get("upperFundingRate"),
            "lower_funding_rate": info.get("lowerFundingRate"),
            "is_pre_listing": info.get("isPreListing"),
            "unified_margin_trade": info.get("unifiedMarginTrade"),
            "tick_size": price_filter.get("tickSize"),
            "min_price": price_filter.get("minPrice"),
            "max_price": price_filter.get("maxPrice"),
            "qty_step": lot_filter.get("qtyStep"),
            "min_order_qty": lot_filter.get("minOrderQty"),
            "max_order_qty": lot_filter.get("maxOrderQty"),
            "max_market_order_qty": lot_filter.get("maxMktOrderQty"),
            "min_notional": lot_filter.get("minNotionalValue"),
            "price_scale": info.get("priceScale"),
            "min_leverage": (info.get("leverageFilter") or {}).get("minLeverage"),
            "max_leverage": (info.get("leverageFilter") or {}).get("maxLeverage"),
            "leverage_step": (info.get("leverageFilter") or {}).get("leverageStep"),
        }
        cache_ok = True

    with _instrument_meta_lock:
        _instrument_meta_cache[cache_key] = (time.time(), dict(meta), cache_ok)
    return meta


def _json_loads_or_default(raw: str | None, default: Any) -> Any:
    try:
        if not raw:
            return default
        # UI/status helpers не должны принимать non-finite JSON как норму.
        # Legacy/manual payload с NaN/Infinity лучше деградировать к None внутри
        # структуры, чем позволять ему тихо влиять на API-ответы и ветвление.
        return json.loads(raw, parse_constant=lambda _token: None)
    except Exception:
        return default


def _json_loads_mapping_or_default(raw: str | None, default: dict[str, Any] | None = None) -> dict[str, Any]:
    loaded = _json_loads_or_default(raw, None)
    if isinstance(loaded, dict):
        return dict(loaded)
    return dict(default or {})


def _normalized_optional_text(value: str | None, *, field_name: str) -> str | None:
    """Нормализует optional operator-input без создания мусорных audit-значений."""
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if "\x00" in normalized:
        raise HTTPException(status_code=422, detail=f"{field_name} must not contain NUL byte")
    return normalized


def _bounded_limit(value: int, *, default: int, max_value: int) -> int:
    try:
        num = int(value)
    except Exception:
        num = int(default)
    return max(1, min(num, int(max_value)))


def _bounded_probability(value: float | None, *, default: float) -> float:
    try:
        num = float(value if value is not None else default)
    except Exception:
        num = float(default)
    if not math.isfinite(num):
        num = float(default)
    return max(0.0, min(num, 1.0))


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_int_or_none(value: Any) -> int | None:
    return strict_integer(value)


def _ensure_json_payload_has_only_finite_numbers(value: Any, *, field_name: str, path: str = "") -> None:
    current_path = path or field_name
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HTTPException(status_code=422, detail=f"{field_name} contains non-finite number at {current_path}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{current_path}.{key}" if current_path else str(key)
            _ensure_json_payload_has_only_finite_numbers(item, field_name=field_name, path=child)
        return
    if isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            child = f"{current_path}[{idx}]"
            _ensure_json_payload_has_only_finite_numbers(item, field_name=field_name, path=child)
        return
    # Pydantic may already coerce most JSON scalars, но на всякий случай не храним
    # неожиданные типы в operator-facing JSON payload'ах.
    raise HTTPException(status_code=422, detail=f"{field_name} contains unsupported value at {current_path}")


def _normalized_non_empty_text(value: str, *, field_name: str) -> str:
    """Строгая нормализация операторских строковых ключей.

    Для audit-facing сущностей нельзя молча принимать пустые/пробельные значения:
    они создают труднообъяснимые записи в БД (например, sentiment c пустым key),
    после чего ломается смысл GET-фильтров и ручного анализа историки.

    Дополнительно запрещаем NUL-байт. SQLite/JSON обычно переживают такой ввод
    непредсказуемо: визуально строка может выглядеть нормальной, а фильтрация,
    экспорт и ручной разбор историки начинают вести себя несогласованно.
    """
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a non-empty string")
    if "\x00" in normalized:
        raise HTTPException(status_code=422, detail=f"{field_name} must not contain NUL byte")
    return normalized


def _normalize_tag_list(tags: list[str] | None) -> list[str]:
    """Убирает мусорные/дублирующиеся теги, сохраняя порядок живых значений.

    Для audit-facing series теги должны быть безопасными строками. NUL-байт здесь
    особенно неприятен: визуально тег может выглядеть нормально, но фильтрация,
    экспорт и ручной анализ начинают вести себя несогласованно.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = str(raw or "").strip()
        if not tag:
            continue
        if "\x00" in tag:
            raise HTTPException(status_code=422, detail="tags must not contain NUL byte")
        if tag in seen:
            continue
        out.append(tag)
        seen.add(tag)
    return out



def _normalized_filter_text(value: str | None, *, default: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return str(default)
    if "\x00" in normalized:
        raise HTTPException(status_code=422, detail=f"{field_name} must not contain NUL byte")
    return normalized


def _existing_trade_matches_request(
    existing: dict[str, Any] | None,
    *,
    bot_id: str,
    symbol: str,
    ts: int | None,
    pnl: float,
    fee: float,
    meta: dict[str, Any],
    funding: float = 0.0,
    slippage: float = 0.0,
) -> bool:
    if not existing:
        return False
    if str(existing.get("bot_id") or "") != str(bot_id):
        return False
    if str(existing.get("symbol") or "") != str(symbol):
        return False
    if ts is not None and _safe_int(existing.get("ts"), 0) != int(ts):
        return False
    try:
        values_match = all(
            math.isclose(float(existing.get(name) or 0.0), float(value), rel_tol=1e-12, abs_tol=1e-12)
            for name, value in (("pnl", pnl), ("fee", fee), ("funding", funding), ("slippage", slippage))
        )
    except Exception:
        return False
    if not values_match:
        return False
    return (existing.get("meta") or {}) == (meta or {})


def _llm_status_from_reasons_dict(reasons: Any) -> str:
    if not isinstance(reasons, dict):
        return "none"
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else None
    if not isinstance(llm_review, dict):
        return "none"
    return str(llm_review.get("status") or "none").strip().lower() or "none"


def _apply_publication_chain_effective_expiry_guard(out: dict[str, Any]) -> None:
    """Expose expired recommendation chains as non-actionable without relying on a background sweep."""
    ctx = out.get("operator_decision_context") if isinstance(out.get("operator_decision_context"), dict) else {}
    if not bool(ctx.get("is_publication_chain_expired")):
        return
    current_status = str(out.get("status") or "").strip().lower()
    if current_status not in {"recommended", "active", "pending"}:
        return
    out["stored_status"] = out.get("stored_status") or current_status
    out["status"] = "expired"
    out["effective_status"] = "expired"
    block = {
        "code": "PUBLICATION_CHAIN_EXPIRED",
        "msg": "publication chain TTL expired; this recommendation is historical and must not be traded",
        "publication_root_rec_id": ctx.get("publication_root_rec_id"),
        "publication_chain_age_sec": ctx.get("publication_chain_age_sec"),
        "ttl_sec": ctx.get("ttl_sec"),
    }
    blocks = out.get("blocks") if isinstance(out.get("blocks"), list) else []
    seen = {str(item.get("code") or "") for item in blocks if isinstance(item, dict)}
    if block["code"] not in seen:
        blocks.append(block)
    out["blocks"] = blocks

    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    risk_report = params.get("risk_report") if isinstance(params.get("risk_report"), dict) else {}
    risk_report["decision"] = "not_recommended"
    rejections = risk_report.get("rejection_reasons") if isinstance(risk_report.get("rejection_reasons"), list) else []
    msg = "Recommendation chain TTL expired; do not execute this stale idea."
    if msg not in [str(x) for x in rejections]:
        rejections.append(msg)
    risk_report["rejection_reasons"] = rejections
    params["risk_report"] = risk_report
    out["params"] = params

    reasons = out.get("reasons") if isinstance(out.get("reasons"), dict) else {}
    decision_layers = reasons.get("decision_layers") if isinstance(reasons.get("decision_layers"), dict) else {}
    decision_layers["publication_chain_ttl"] = "expired"
    decision_layers["final_status"] = "expired"
    reasons["decision_layers"] = decision_layers
    out["reasons"] = reasons


def _apply_llm_effective_pending_guard(out: dict[str, Any]) -> None:
    """Operator-facing guard: actionable rows require an OK LLM verdict.

    Legacy rows can already be persisted as recommended/active with no LLM verdict
    because older advisory mode did not hold publication. Do not mutate the audit row
    here; expose the safe effective status to the UI/API instead.
    """
    if not bool(getattr(settings, "llm_reviewer_enabled", False)):
        return
    status = str(out.get("status") or "").strip().lower()
    if status not in {"recommended", "active"}:
        return
    reasons = out.get("reasons") if isinstance(out.get("reasons"), dict) else {}
    llm_status = _llm_status_from_reasons_dict(reasons)
    if llm_status == "ok":
        return
    original_status = status
    out["status"] = "pending"
    out["effective_status"] = "pending"
    out["stored_status"] = original_status
    reasons = out.setdefault("reasons", {})
    if not isinstance(reasons, dict):
        reasons = {}
        out["reasons"] = reasons
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else {}
    if not isinstance(llm_review, dict):
        llm_review = {}
    if llm_status in {"none", "", "unknown"}:
        llm_review["status"] = "pending"
        llm_review.setdefault("reason", "legacy_actionable_without_llm_verdict")
    llm_review.setdefault("publish_target_status", original_status)
    llm_review["gate_decision"] = "pending"
    llm_review["hold_policy"] = "llm_verdict_required"
    llm_review["requires_ok_verdict"] = True
    reasons["llm_review"] = llm_review
    decision_layers = reasons.get("decision_layers") if isinstance(reasons.get("decision_layers"), dict) else {}
    decision_layers["llm_verdict_guard"] = {
        "stored_status": original_status,
        "effective_status": "pending",
        "llm_status": llm_status,
        "requires_ok_verdict": True,
    }
    decision_layers["final_status"] = "pending"
    reasons["decision_layers"] = decision_layers
    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    risk_report = params.get("risk_report") if isinstance(params.get("risk_report"), dict) else {}
    risk_report["decision"] = "not_recommended"
    warnings = risk_report.get("warnings") if isinstance(risk_report.get("warnings"), list) else []
    warning_text = "LLM-review ещё не завершён: запуск grid удержан в pending до OK-вердикта."
    if warning_text not in [str(x) for x in warnings]:
        warnings.append(warning_text)
    risk_report["warnings"] = warnings
    params["risk_report"] = risk_report
    out["params"] = params


def _merge_bybit_operator_guard_into_ui_payload(out: dict[str, Any], guard: dict[str, Any]) -> None:
    """Make stale/invalid Bybit exchange constraints visible before execution.

    The DB row may have been produced from market candles before fresh instrument
    metadata was available. Operator-facing API responses must therefore fail
    closed when current Bybit metadata cannot confirm LinearPerpetual/USDT/tick,
    lot, min-notional and leverage constraints. This keeps the UI from showing an
    apparently actionable futures-grid recommendation that execution preflight
    would later reject.
    """
    if not isinstance(guard, dict):
        return
    errors = [item for item in (guard.get("errors") or []) if isinstance(item, dict)]
    if not errors:
        return

    # Legacy rows with malformed JSON payloads may expose empty params. They still
    # must not remain launchable if the fresh operator guard found execution-risk
    # errors, but we avoid rebuilding params for those rows to preserve API shape
    # compatibility with JSON-hardening tests.
    params_payload = out.get("params")
    has_params_payload = isinstance(params_payload, dict) and bool(params_payload)
    if not has_params_payload:
        return

    blocks = out.get("blocks")
    if not isinstance(blocks, list):
        blocks = []
    seen_codes = {str(item.get("code") or "") for item in blocks if isinstance(item, dict)}
    rejection_messages: list[str] = []
    for err in errors:
        code = str(err.get("code") or "BYBIT_OPERATOR_GUARD_FAILED")
        msg = str(err.get("msg") or "Bybit exchange-constraint guard blocked this futures-grid recommendation.")
        rejection_messages.append(msg)
        if code in seen_codes:
            continue
        blocks.append({"code": code, "msg": msg, "source": "bybit_operator_guard"})
        seen_codes.add(code)
    out["blocks"] = blocks

    current_status = str(out.get("status") or "").strip().lower()
    if current_status in {"recommended", "pending", "active"}:
        out["stored_status"] = out.get("stored_status") or current_status
        out["status"] = "blocked"
        out["effective_status"] = "blocked"

    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    risk_report = params.get("risk_report") if isinstance(params.get("risk_report"), dict) else {}
    risk_report["decision"] = "not_recommended"
    existing_rejections = risk_report.get("rejection_reasons")
    if not isinstance(existing_rejections, list):
        existing_rejections = []
    seen_reasons = {str(item) for item in existing_rejections}
    for msg in rejection_messages:
        if msg not in seen_reasons:
            existing_rejections.append(msg)
            seen_reasons.add(msg)
    risk_report["rejection_reasons"] = existing_rejections
    params["risk_report"] = risk_report
    out["params"] = params

    reasons = out.get("reasons") if isinstance(out.get("reasons"), dict) else {}
    risk_checks = reasons.get("risk_checks") if isinstance(reasons.get("risk_checks"), dict) else {}
    risk_checks["passed"] = False
    risk_blocks = risk_checks.get("blocks")
    if not isinstance(risk_blocks, list):
        risk_blocks = []
    risk_seen_codes = {str(item.get("code") or "") for item in risk_blocks if isinstance(item, dict)}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        code = str(block.get("code") or "")
        if code and code not in risk_seen_codes:
            risk_blocks.append(block)
            risk_seen_codes.add(code)
    risk_checks["blocks"] = risk_blocks
    reasons["risk_checks"] = risk_checks

    decision_layers = reasons.get("decision_layers") if isinstance(reasons.get("decision_layers"), dict) else {}
    decision_layers["bybit_operator_guard"] = "blocked"
    decision_layers["final_status"] = "blocked"
    reasons["decision_layers"] = decision_layers
    out["reasons"] = reasons


def _ensure_effective_status(out: dict[str, Any]) -> None:
    """Always expose a concrete operator-facing status for UI filters/badges."""
    effective = str(out.get("effective_status") or "").strip().lower()
    if effective:
        return
    status = str(out.get("status") or "").strip().lower()
    out["effective_status"] = status or "unknown"


def _operator_payload_has_runtime_risk_context(out: dict[str, Any]) -> bool:
    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    # Only real operator/recommender payloads carry these publication-time guard
    # artifacts.  Unit/API-shape fixtures may inject minimal params/trade_plan values
    # solely to satisfy Bybit metadata validation; do not retroactively reinterpret
    # those synthetic rows as live launch sheets.  Actual execution still calls
    # _execution_runtime_size_risk_blocks directly.
    return any(
        isinstance(params.get(key), dict)
        for key in ("leverage_policy", "operator_sheet", "risk_report")
    )


def _mark_operator_payload_blocked(
    out: dict[str, Any],
    blocks_to_add: list[dict[str, Any]],
    *,
    source: str,
    decision_layer_key: str,
) -> None:
    if not blocks_to_add:
        return

    blocks = out.get("blocks")
    if not isinstance(blocks, list):
        blocks = []
    seen_codes = {str(item.get("code") or "") for item in blocks if isinstance(item, dict)}
    rejection_messages: list[str] = []
    for block in blocks_to_add:
        if not isinstance(block, dict):
            continue
        code = str(block.get("code") or "OPERATOR_RUNTIME_GUARD_FAILED")
        msg = str(block.get("msg") or "Operator runtime guard blocked this futures-grid recommendation.")
        rejection_messages.append(msg)
        if code not in seen_codes:
            merged = dict(block)
            merged.setdefault("code", code)
            merged.setdefault("msg", msg)
            merged.setdefault("source", source)
            blocks.append(merged)
            seen_codes.add(code)
    out["blocks"] = blocks

    current_status = str(out.get("status") or "").strip().lower()
    if current_status in {"recommended", "active", "pending"}:
        out["stored_status"] = out.get("stored_status") or current_status
    out["status"] = "blocked"
    out["effective_status"] = "blocked"

    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    risk_report = params.get("risk_report") if isinstance(params.get("risk_report"), dict) else {}
    risk_report["decision"] = "not_recommended"
    existing_rejections = risk_report.get("rejection_reasons")
    if not isinstance(existing_rejections, list):
        existing_rejections = []
    seen_reasons = {str(item) for item in existing_rejections}
    for msg in rejection_messages:
        if msg and msg not in seen_reasons:
            existing_rejections.append(msg)
            seen_reasons.add(msg)
    risk_report["rejection_reasons"] = existing_rejections
    params["risk_report"] = risk_report
    out["params"] = params

    reasons = out.get("reasons") if isinstance(out.get("reasons"), dict) else {}
    risk_checks = reasons.get("risk_checks") if isinstance(reasons.get("risk_checks"), dict) else {}
    risk_checks["passed"] = False
    risk_blocks = risk_checks.get("blocks")
    if not isinstance(risk_blocks, list):
        risk_blocks = []
    risk_seen = {str(item.get("code") or "") for item in risk_blocks if isinstance(item, dict)}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        code = str(block.get("code") or "")
        if code and code not in risk_seen:
            risk_blocks.append(block)
            risk_seen.add(code)
    risk_checks["blocks"] = risk_blocks
    reasons["risk_checks"] = risk_checks
    decision_layers = reasons.get("decision_layers") if isinstance(reasons.get("decision_layers"), dict) else {}
    decision_layers[decision_layer_key] = "blocked"
    decision_layers["final_status"] = "blocked"
    reasons["decision_layers"] = decision_layers
    out["reasons"] = reasons


def _apply_runtime_risk_limits_guard(out: dict[str, Any], *, conn: Any | None = None) -> None:
    """Revalidate persisted recommendations against the current runtime risk profile.

    Operator profiles can be changed after a recommendation is published.  A DB row
    that was generated with min/max leverage 3..5 must not remain actionable when
    /risk/status now says the 3-5x operator interval, and a 1x fallback payload must not bypass the
    current min_leverage guard merely because it is an old snapshot.
    """
    if conn is None or not _operator_payload_has_runtime_risk_context(out):
        return
    try:
        limits = get_risk_limits(conn, settings.risk_limits)
    except Exception:
        limits = normalize_risk_limits(getattr(settings, "risk_limits", {}) or {}, getattr(settings, "risk_limits", {}) or {})

    blocks = _execution_runtime_size_risk_blocks(out, limits)

    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    policy = params.get("leverage_policy") if isinstance(params.get("leverage_policy"), dict) else {}
    if policy:
        current_min = _finite_float_or_none(limits.get("min_leverage"))
        current_max = _finite_float_or_none(limits.get("max_leverage"))
        policy_min = _finite_float_or_none(policy.get("min_operator_leverage"))
        policy_max = _finite_float_or_none(policy.get("max_operator_leverage"))
        if (
            current_min is not None
            and policy_min is not None
            and not math.isclose(float(current_min), float(policy_min), rel_tol=0.0, abs_tol=1e-12)
        ) or (
            current_max is not None
            and policy_max is not None
            and not math.isclose(float(current_max), float(policy_max), rel_tol=0.0, abs_tol=1e-12)
        ):
            blocks.append({
                "code": "RUNTIME_RISK_PROFILE_CHANGED",
                "msg": (
                    f"recommendation leverage policy was generated for min/max "
                    f"{policy_min if policy_min is not None else 'unknown'}x/"
                    f"{policy_max if policy_max is not None else 'unknown'}x, but current runtime profile is "
                    f"{current_min if current_min is not None else 'unknown'}x/"
                    f"{current_max if current_max is not None else 'unknown'}x; refresh recommendation before launch."
                ),
            })

    if blocks:
        _mark_operator_payload_blocked(
            out,
            blocks,
            source="runtime_risk_limits_guard",
            decision_layer_key="runtime_risk_limits_guard",
        )


def _apply_snapshot_stale_guard(out: dict[str, Any], *, snapshot_age_sec: int | None, stale_after_sec: int) -> None:
    status = str(out.get("status") or "").strip().lower()
    if status not in {"recommended", "active", "pending"}:
        return
    if snapshot_age_sec is None or int(snapshot_age_sec) <= int(stale_after_sec):
        return
    _mark_operator_payload_blocked(
        out,
        [{
            "code": "SNAPSHOT_STALE_FOR_OPERATOR_LAUNCH",
            "msg": (
                f"recommendation snapshot age_sec={int(snapshot_age_sec)} exceeds operator freshness limit "
                f"{int(stale_after_sec)} sec; do not launch stale grid parameters."
            ),
            "snapshot_age_sec": int(snapshot_age_sec),
            "stale_after_sec": int(stale_after_sec),
        }],
        source="snapshot_freshness_guard",
        decision_layer_key="snapshot_freshness_guard",
    )


def _directional_exit_qty_for_reco(rec: dict[str, Any], reference_price: Any) -> dict[str, Any]:
    """Conservative quantity context for directional TP/SL math.

    The UI should not imply that gross TP/SL PnL is for one coin when the
    recommendation already carries total grid exposure. Prefer explicit total
    position qty, then explicit per-grid qty multiplied by grid_count. If only
    notional is available, derive qty with the same price convention that
    produced the notional: worst-case notional uses max executable grid price,
    while legacy/reference notional uses reference_price.
    """
    if not isinstance(rec, dict):
        return {"qty": None, "qty_source": None}
    params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
    plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    operator_sheet = params.get("operator_sheet") if isinstance(params.get("operator_sheet"), dict) else {}

    def finite(value: Any) -> float | None:
        num = _finite_float_or_none(value)
        if num is None or num <= 0:
            return None
        return float(num)

    def find_first(mappings: list[Any], keys: tuple[str, ...]) -> tuple[str | None, float | None]:
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            for key in keys:
                if key not in mapping:
                    continue
                value = finite(mapping.get(key))
                if value is not None:
                    return key, value
        return None, None

    def find_first_positive_int(mappings: list[Any], keys: tuple[str, ...]) -> tuple[str | None, int | None]:
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            for key in keys:
                if key not in mapping:
                    continue
                value = _safe_int_or_none(mapping.get(key))
                if value is not None and value > 0:
                    return key, int(value)
        return None, None

    sizing_maps: list[Any] = [
        plan.get("sizing"),
        params.get("sizing"),
        operator_sheet.get("sizing") if isinstance(operator_sheet, dict) else None,
        plan.get("economics"),
        params.get("economics"),
        operator_sheet.get("economics") if isinstance(operator_sheet, dict) else None,
        params.get("risk_report"),
        plan.get("risk_report"),
        params,
        plan,
        # Keep backend TP/SL math in parity with operator UI lookups: generated
        # operator sheets may expose total qty/notional on the sheet itself, not
        # only inside nested sizing/economics blocks.
        operator_sheet,
    ]
    total_qty_keys = (
        "estimated_position_qty",
        "position_qty",
        "total_qty",
        "estimated_total_qty",
        "max_position_qty",
        "estimated_max_position_qty",
    )
    key, qty = find_first(sizing_maps, total_qty_keys)
    if qty is not None:
        return {"qty": qty, "qty_source": key}

    ref = finite(reference_price)
    per_order_qty_keys = (
        "qty_per_order",
        "order_qty",
        "qty",
        "qty_per_leg",
        "base_qty_per_order",
        "base_qty",
        "order_size_qty",
        "leg_qty",
    )
    key, per_order_qty = find_first(sizing_maps, per_order_qty_keys)
    if per_order_qty is not None:
        # Generated payloads may carry the executable grid/order count inside
        # trade_plan.sizing/economics, params.sizing/economics or only on the
        # legacy top-level params/trade_plan object.  Missing that nested count
        # understates the TP/SL PnL context by showing one grid order instead of
        # total active grid exposure.
        grid_count_key, grid_count = find_first_positive_int(
            sizing_maps,
            (
                "grid_count",
                "estimated_active_orders",
                "active_grid_intervals",
                "grid_levels",
                "levels_count",
                "orders_count",
            ),
        )
        if grid_count is not None and grid_count > 1:
            return {"qty": float(per_order_qty) * float(grid_count), "qty_source": f"{key}*{grid_count_key}"}
        return {"qty": per_order_qty, "qty_source": key}

    if ref is not None:
        levels = plan.get("levels") if isinstance(plan.get("levels"), dict) else {}
        range_levels = levels.get("range") if isinstance(levels.get("range"), dict) else {}
        range_lower = finite(range_levels.get("lower")) or finite(params.get("price_range_lower"))
        range_upper = finite(range_levels.get("upper")) or finite(params.get("price_range_upper"))
        worst_notional_price = _grid_max_notional_price(ref, range_lower, range_upper)
        worst_total_notional_keys = (
            "estimated_worst_case_total_order_notional_usdt",
            "worst_case_total_order_notional_usdt",
            "estimated_max_position_notional_usdt",
            "max_position_notional_usdt",
        )
        key, notional = find_first(sizing_maps, worst_total_notional_keys)
        if notional is not None and worst_notional_price is not None and worst_notional_price > 0:
            return {"qty": float(notional) / float(worst_notional_price), "qty_source": f"{key}/max_grid_price"}

        total_notional_keys = (
            "estimated_total_order_notional_usdt",
            "total_order_notional_usdt",
            "position_notional_usdt",
            "notional_usdt",
        )
        key, notional = find_first(sizing_maps, total_notional_keys)
        if notional is not None:
            return {"qty": float(notional) / float(ref), "qty_source": f"{key}/reference_price"}

    if ref is not None:
        key, notional = find_first(
            sizing_maps,
            (
                "order_notional_usdt",
                "order_notional",
                "notional_per_order",
                "quote_qty",
                "quote_amount",
                "capital_per_leg_usdt",
                "investment_per_grid",
                "usdt_per_order",
            ),
        )
        if notional is not None:
            return {"qty": float(notional) / float(ref), "qty_source": f"{key}/reference_price"}

    return {"qty": None, "qty_source": None}


def _directional_exit_payload_for_reco(rec: dict[str, Any]) -> dict[str, Any]:
    ctx = _trade_plan_price_context(rec)
    levels = directional_exit_levels(
        rec.get("direction"),
        ctx.get("kill_switch_lower"),
        ctx.get("kill_switch_upper"),
    ).as_dict()
    direction = str(levels.get("direction") or "neutral").strip().lower()
    reference_price = ctx.get("reference_price")
    levels["reference_price"] = reference_price
    qty_context = _directional_exit_qty_for_reco(rec, reference_price)
    position_qty = qty_context.get("qty")
    qty_source = qty_context.get("qty_source")
    unit_qty_ratio_only = position_qty is None
    levels["qty"] = position_qty
    levels["qty_source"] = qty_source or ("unit_qty_ratio_only" if unit_qty_ratio_only else None)
    levels["trade_math"] = None
    levels["bybit_protective_orders"] = {}
    if direction in {"long", "short"}:
        errors = validate_directional_exit_geometry(
            direction,
            reference_price,
            levels.get("take_profit"),
            levels.get("stop_loss"),
        )
        levels["geometry_valid"] = len(errors) == 0
        levels["geometry_errors"] = errors
        math_payload = directional_trade_math(
            direction,
            reference_price,
            levels.get("take_profit"),
            levels.get("stop_loss"),
            position_qty if position_qty is not None else 1.0,
        )
        if math_payload is not None:
            math_dict = math_payload.as_dict()
            math_dict["gross_pnl_is_position_estimate"] = not unit_qty_ratio_only
            math_dict["qty_basis"] = (
                "position_qty" if not unit_qty_ratio_only else "one_base_asset_for_ratio_only"
            )
            levels["trade_math"] = math_dict
            levels["take_profit_distance_pct"] = math_dict.get("take_profit_distance_pct")
            levels["stop_loss_distance_pct"] = math_dict.get("stop_loss_distance_pct")
            levels["risk_reward"] = math_dict.get("risk_reward")
        if len(errors) == 0 and math_payload is not None:
            levels["bybit_protective_orders"] = {
                "take_profit": bybit_linear_protective_order_plan(
                    direction,
                    "take_profit",
                    levels.get("take_profit"),
                    reference_price,
                ),
                "stop_loss": bybit_linear_protective_order_plan(
                    direction,
                    "stop_loss",
                    levels.get("stop_loss"),
                    reference_price,
                ),
            }
    else:
        levels["geometry_valid"] = True
        levels["geometry_errors"] = []
    return levels


def _pct_delta(value: float | None, reference: float | None) -> float | None:
    if value is None or reference is None or reference <= 0:
        return None
    return (float(value) - float(reference)) / float(reference) * 100.0


def _distance_from_current_to_bound_pct(current_price: float | None, bound: float | None, *, side: str) -> float | None:
    """Signed distance from current price to a range/kill-switch bound.

    Both lower and upper distances use the same denominator: current price.
    This keeps the operator UI symmetric and avoids overstating downside room
    when the lower bound is far below the current price. Positive means the
    current price is still inside the relevant side of the bound; negative means
    that side has already been breached.
    """
    current = _finite_float_or_none(current_price)
    level = _finite_float_or_none(bound)
    if current is None or level is None or current <= 0:
        return None
    side_norm = str(side or "").strip().lower()
    if side_norm == "lower":
        return (current - level) / current * 100.0
    if side_norm == "upper":
        return (level - current) / current * 100.0
    return None


def _recommendation_timestamp_state(value: Any, *, now_ts: int) -> dict[str, Any]:
    """Validate recommendation timestamps before deriving age/TTL.

    A timestamp far in the future must never be converted to age=0 because that
    makes a poisoned or clock-skewed recommendation look perpetually fresh.
    Small positive skew is tolerated to accommodate normal host/exchange drift.
    """
    ts_value = _safe_int_or_none(value)
    if ts_value is None or ts_value <= 0:
        return {
            "ts": ts_value,
            "valid": False,
            "invalid_reason": "missing_or_nonpositive",
            "age_sec": None,
            "future_skew_sec": None,
        }
    future_skew_sec = max(0, int(ts_value) - int(now_ts))
    if future_skew_sec > RECOMMENDATION_MAX_FUTURE_SKEW_SEC:
        return {
            "ts": int(ts_value),
            "valid": False,
            "invalid_reason": "future_clock_skew",
            "age_sec": None,
            "future_skew_sec": future_skew_sec,
        }
    return {
        "ts": int(ts_value),
        "valid": True,
        "invalid_reason": None,
        "age_sec": max(0, int(now_ts) - int(ts_value)),
        "future_skew_sec": future_skew_sec,
    }


def _publication_chain_context_for_reco(
    conn: Any | None,
    rec: dict[str, Any],
    *,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Return first-root lineage timing for a recommendation publication chain.

    A row can be superseded by a sharper recommendation while reusing the same
    publication_root_rec_id. Row age alone then understates how long the original
    idea has been alive. Execution/UI must expose both values.
    """
    now = int(time.time() if now_ts is None else now_ts)
    rec_id = str(rec.get("rec_id") or "").strip()
    root_id = str(rec.get("publication_root_rec_id") or rec_id).strip() or rec_id or None
    row_state = _recommendation_timestamp_state(rec.get("ts"), now_ts=now)
    row_ts = row_state.get("ts")
    row_age_sec = row_state.get("age_sec")

    first_ts = row_ts
    last_ts = row_ts
    update_count = 1 if rec_id or row_ts is not None else 0
    if conn is not None and root_id:
        try:
            cur = conn.execute(
                """SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS update_count
                     FROM recommendations
                    WHERE publication_root_rec_id = ? OR rec_id = ?""",
                (root_id, root_id),
            )
            row = cur.fetchone()
            if row and int(row["update_count"] or 0) > 0:
                first_ts = _safe_int_or_none(row["first_ts"])
                last_ts = _safe_int_or_none(row["last_ts"])
                update_count = int(row["update_count"] or 0)
        except Exception:
            first_ts = row_ts
            last_ts = row_ts
            update_count = 1 if rec_id or row_ts is not None else 0

    chain_state = _recommendation_timestamp_state(first_ts, now_ts=now)
    return {
        "publication_root_rec_id": root_id,
        "recommendation_timestamp_valid": bool(row_state.get("valid")),
        "recommendation_timestamp_invalid_reason": row_state.get("invalid_reason"),
        "recommendation_timestamp_future_skew_sec": row_state.get("future_skew_sec"),
        "recommendation_row_age_sec": row_age_sec,
        "publication_chain_started_ts": first_ts,
        "publication_chain_updated_ts": last_ts,
        "publication_chain_timestamp_valid": bool(chain_state.get("valid")),
        "publication_chain_timestamp_invalid_reason": chain_state.get("invalid_reason"),
        "publication_chain_timestamp_future_skew_sec": chain_state.get("future_skew_sec"),
        "publication_chain_age_sec": chain_state.get("age_sec"),
        "publication_chain_update_count": int(update_count or 0),
    }


def _execution_recommendation_freshness_blocks(
    conn: Any | None,
    rec: dict[str, Any],
    *,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    now = int(time.time() if now_ts is None else now_ts)
    chain = _publication_chain_context_for_reco(conn, rec, now_ts=now)
    if chain.get("recommendation_timestamp_valid") is not True:
        blocks.append({
            "code": "RECOMMENDATION_TIMESTAMP_INVALID",
            "msg": "recommendation timestamp is missing, non-positive, or too far in the future",
            "reason": chain.get("recommendation_timestamp_invalid_reason"),
            "future_skew_sec": chain.get("recommendation_timestamp_future_skew_sec"),
        })
    if chain.get("publication_chain_timestamp_valid") is not True:
        blocks.append({
            "code": "PUBLICATION_CHAIN_TIMESTAMP_INVALID",
            "msg": "publication-chain root timestamp is missing, non-positive, or too far in the future",
            "reason": chain.get("publication_chain_timestamp_invalid_reason"),
            "future_skew_sec": chain.get("publication_chain_timestamp_future_skew_sec"),
            "publication_root_rec_id": chain.get("publication_root_rec_id"),
        })

    ttl_sec = _safe_int_or_none(rec.get("ttl_sec"))
    if ttl_sec is None or ttl_sec <= 0:
        return blocks

    row_age = _safe_int_or_none(chain.get("recommendation_row_age_sec"))
    chain_age = _safe_int_or_none(chain.get("publication_chain_age_sec"))
    if row_age is not None and row_age > ttl_sec:
        blocks.append({
            "code": "RECOMMENDATION_ROW_EXPIRED",
            "msg": f"current recommendation row age={row_age}s exceeds ttl={ttl_sec}s",
        })
    if chain_age is not None and chain_age > ttl_sec:
        blocks.append({
            "code": "PUBLICATION_CHAIN_TOO_OLD",
            "msg": f"publication chain age from first root={chain_age}s exceeds ttl={ttl_sec}s",
            "publication_root_rec_id": chain.get("publication_root_rec_id"),
            "publication_chain_update_count": chain.get("publication_chain_update_count"),
        })
    return blocks


def _append_recommendation_timestamp_errors_to_operator_guard(
    guard: dict[str, Any],
    conn: Any | None,
    rec: dict[str, Any],
) -> dict[str, Any]:
    """Make invalid recommendation clocks visible in list/detail effective status.

    Full freshness/TTL remains part of execution preflight.  Here we only merge
    timestamp-integrity failures so a future/poisoned row cannot look actionable
    in the operator UI while execution would reject it later.
    """
    if not isinstance(guard, dict):
        guard = {}
    # Pure helper/unit payloads may intentionally omit DB identity/timestamps.
    # Only persisted recommendations can be operator-actionable and therefore
    # require the timestamp-integrity guard here.
    if not str(rec.get("rec_id") or "").strip():
        return guard
    invalid_codes = {
        "RECOMMENDATION_TIMESTAMP_INVALID",
        "PUBLICATION_CHAIN_TIMESTAMP_INVALID",
    }
    timestamp_errors = [
        dict(item)
        for item in _execution_recommendation_freshness_blocks(conn, rec)
        if isinstance(item, dict) and str(item.get("code") or "") in invalid_codes
    ]
    if not timestamp_errors:
        return guard
    errors = [dict(item) for item in (guard.get("errors") or []) if isinstance(item, dict)]
    seen = {str(item.get("code") or "") for item in errors}
    for item in timestamp_errors:
        code = str(item.get("code") or "")
        if code and code not in seen:
            errors.append(item)
            seen.add(code)
    guard["errors"] = errors
    guard["ok"] = False
    guard["critical"] = True
    return guard


def _guard_item_code_set(items: Any) -> set[str]:
    return {
        str(item.get("code") or "").strip().upper()
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    }


def _operator_next_actions_for_reco(
    rec: dict[str, Any],
    *,
    ctx: dict[str, Any],
    guard_errors: list[Any],
    guard_warnings: list[Any],
) -> list[dict[str, Any]]:
    """Operator-facing remediation hints; never grant launch permission.

    The UI previously showed the fail-closed reason but not the next safe action,
    so a portfolio dominated by `blocked` rows looked like the recommender was
    simply broken.  These hints explain what to change or wait for while keeping
    the hard guard semantics intact.
    """
    actions: list[dict[str, Any]] = []
    error_codes = _guard_item_code_set(guard_errors)
    warning_codes = _guard_item_code_set(guard_warnings)
    params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
    reasons = rec.get("reasons") if isinstance(rec.get("reasons"), dict) else {}
    risk_report = params.get("risk_report") if isinstance(params.get("risk_report"), dict) else {}
    decision_layers = reasons.get("decision_layers") if isinstance(reasons.get("decision_layers"), dict) else {}
    economics = _first_mapping(params.get("economics"), (params.get("trade_plan") or {}).get("economics") if isinstance(params.get("trade_plan"), dict) else {})
    direction = str(rec.get("direction") or "neutral").strip().lower()
    leverage = _finite_float_or_none(params.get("leverage"))
    liq_buffer = _finite_float_or_none(ctx.get("liquidation_buffer_pct"))
    liq_floor = OPERATOR_MIN_LIQUIDATION_BUFFER_PCT

    def _item_text(item: Any) -> str:
        if isinstance(item, dict):
            return " ".join(str(item.get(key) or "") for key in ("code", "msg", "message", "reason", "note", "text", "feature"))
        return str(item or "")

    no_trade_reason_texts = [
        _item_text(item)
        for item in (risk_report.get("no_trade_reasons") if isinstance(risk_report.get("no_trade_reasons"), list) else [])
    ]
    no_trade_reason_texts.extend(
        _item_text(item)
        for item in (decision_layers.get("no_trade_reasons") if isinstance(decision_layers.get("no_trade_reasons"), list) else [])
    )
    risk_warning_texts = [
        _item_text(item)
        for item in (risk_report.get("warnings") if isinstance(risk_report.get("warnings"), list) else [])
    ]
    risk_warning_texts.extend(
        _item_text(item)
        for item in (reasons.get("top_negative_factors") if isinstance(reasons.get("top_negative_factors"), list) else [])
    )
    no_trade_blob = " ".join(no_trade_reason_texts).lower()
    warning_blob = " ".join(risk_warning_texts).lower()
    status_norm = str(rec.get("effective_status") or rec.get("status") or "").strip().lower()

    def add(code: str, title: str, detail: str, severity: str = "info") -> None:
        if any(item.get("code") == code for item in actions):
            return
        actions.append({
            "code": code,
            "title": title,
            "detail": detail,
            "severity": severity,
        })

    if "LIQUIDATION_BUFFER_TOO_LOW" in error_codes:
        buffer_txt = f"{liq_buffer:.2f}%" if liq_buffer is not None else "не оценён"
        lev_txt = f"{leverage:.8g}×" if leverage is not None else "текущее значение"
        policy = params.get("leverage_policy") if isinstance(params.get("leverage_policy"), dict) else {}
        diagnostics = policy.get("diagnostics") if isinstance(policy.get("diagnostics"), dict) else {}
        safe_lev = _safe_int_or_none(diagnostics.get("liquidation_safe_max_leverage"))
        safe_lev_txt = (
            f"Максимальное плечо, прошедшее текущую проверку общей маржи: ≤{safe_lev}×."
            if safe_lev is not None and leverage is not None and safe_lev < leverage
            else "Нужен новый расчёт диапазона, аварийной границы выхода, капитала или плеча; цена ликвидации изолированной позиции здесь не является надёжным ориентиром."
        )
        add(
            "DO_NOT_LAUNCH_LOW_LIQUIDATION_BUFFER",
            "Не запускать текущую сетку",
            f"Запас капитала при общей марже {buffer_txt} ниже обязательного минимума {liq_floor:.0f}% при плече {lev_txt}. Это безопасная блокировка при недостатке подтверждённых данных. {safe_lev_txt}",
            "danger",
        )
        add(
            "RECALCULATE_WITH_LOWER_LEVERAGE_OR_NARROWER_RANGE",
            "Пересчитать профиль риска",
            "Снизьте плечо либо измените диапазон и аварийную границу выхода, затем дождитесь новой публикации. Не подменяйте проверку общей маржи расчётом ликвидации одной изолированной позиции.",
            "warning",
        )

    if "GRID_NET_PROFIT_TOO_THIN" in error_codes or "GRID_NET_PROFIT_NON_POSITIVE" in error_codes or "GRID_GROSS_EDGE_BELOW_COSTS" in error_codes:
        add(
            "WAIT_FOR_WIDER_NET_EDGE",
            "Ждать более широкой сеточной прибыли",
            "Комиссии двух исполнений сетки поглощают прибыль интервала. Разница цен покупки и продажи, проскальзывание и платёж финансирования проверяются отдельно; до нового расчёта торговлю не начинать.",
            "warning",
        )

    if "FUNDING_RATE_UNKNOWN" in error_codes:
        add(
            "REFRESH_FUNDING_RATE_SNAPSHOT",
            "Обновить ставку платежа финансирования",
            "Для бессрочных фьючерсов с расчётом в USDT ставка платежа финансирования является обязательной частью чистого результата. Перезапустите сбор данных Bybit, проверьте свежесть ставки и дождитесь новой публикации; без этих данных торговля остаётся заблокированной.",
            "warning",
        )

    if "FUNDING_INTERVAL_UNCONFIRMED" in error_codes or "FUNDING_SNAPSHOT_STALE" in error_codes or "EXECUTION_FUNDING_WORSE_THAN_PUBLICATION" in error_codes:
        add(
            "REFRESH_FUNDING_AND_RECOMMENDER",
            "Обновить данные платежа финансирования",
            "Перезапустите сбор рыночных данных и сведений об инструменте, затем дождитесь новой рекомендации; торговля по устаревшей ставке платежа финансирования остаётся заблокированной.",
            "warning",
        )

    if "CURRENT_PRICE_OUTSIDE_GRID_RANGE" in error_codes or "CURRENT_PRICE_BEYOND_KILL_SWITCH" in error_codes:
        add(
            "REFRESH_STALE_PRICE_PLAN",
            "Дождаться нового расчёта уровней",
            "Цена уже вышла из рекомендованного диапазона или за аварийную границу. Не переносите уровни вручную; нужна новая рекомендация по свежим рыночным данным.",
            "danger",
        )

    if status_norm == "no_trade" and ("operator_leverage_profile_not_actionable" in no_trade_blob or "operator_minimum" in no_trade_blob):
        lev_txt = f"{leverage:.8g}×" if leverage is not None else "текущем профиле плеча 3–5×"
        add(
            "DO_NOT_LAUNCH_PROFILE_NOT_ACTIONABLE",
            "Не запускать при текущем профиле плеча 3–5×",
            f"Идея оценена при {lev_txt}, но не прошла операторский профиль без ослабления правил риска. Оставьте решение «Не торговать»: ручной запуск такой сетки обойдёт обязательное условие безопасности.",
            "warning",
        )
    if "signal_quality_too_low_for_operator_minimum" in no_trade_blob:
        add(
            "WAIT_FOR_STRONGER_SIGNAL_OR_RANGE",
            "Ждать более сильного сигнала или устойчивого бокового режима",
            "Для профиля плеча 3–5× качество направления или бокового режима недостаточно. Нужна новая публикация с более выраженным направлением либо устойчивым боковым движением; не повышайте статус вручную из-за высокого относительного ранга.",
            "warning",
        )
    if "atr_too_high_for_operator_minimum" in no_trade_blob or "unsafe_volatility_or_execution_cost" in no_trade_blob or "высокая волатильность" in warning_blob:
        add(
            "WAIT_FOR_LOWER_VOLATILITY",
            "Ждать снижения волатильности",
            "Высокая волатильность повышает риск выхода из диапазона и ликвидационного сценария для сетки. Без новой публикации при более спокойном рынке торговлю не начинать.",
            "warning",
        )
    if "insufficient_net_edge_for_operator_minimum" in no_trade_blob:
        add(
            "WAIT_FOR_WIDER_NET_EDGE",
            "Ждать более широкой сеточной прибыли",
            "Совокупный результат на расчётном горизонте недостаточен для профиля плеча 3–5× после повторяющихся комиссий, разовых издержек исполнения и неблагоприятного платежа финансирования. Нужен новый расчёт, а не ручной запуск.",
            "warning",
        )
    if "funding" in warning_blob or "издержки" in warning_blob:
        add(
            "CHECK_COSTS_AND_FUNDING_BEFORE_NEXT_PUBLICATION",
            "Проверить издержки и платёж финансирования",
            "Издержки исполнения и неблагоприятный платёж финансирования ухудшают чистый результат. Дождитесь свежих данных об издержках и ставке; возможное получение платежа не считать гарантированным преимуществом.",
            "info",
        )
    if "тренд" in warning_blob or "trend" in warning_blob:
        add(
            "AVOID_GRID_IN_STRONG_TREND",
            "Не запускать сетку против сильного тренда",
            "Сильный тренд нарушает сеточную гипотезу и может накапливать позицию против движения. Ждите ослабления тренда или перехода к устойчивому боковому движению.",
            "warning",
        )
    if "спред" in warning_blob or "spread" in warning_blob:
        add(
            "WAIT_FOR_TIGHTER_SPREAD",
            "Ждать улучшения ликвидности",
            "Большая разница цен покупки и продажи ухудшает исполнение и быстро поглощает преимущество сетки. Торговать можно только после свежей публикации с приемлемыми издержками исполнения.",
            "info",
        )

    if "INSUFFICIENT_MTF_HISTORY_FOR_GRID" in error_codes:
        add(
            "WAIT_FOR_MTF_HISTORY",
            "Дождаться закрытых свечей на нескольких интервалах",
            "Фьючерсная сетка не публикуется без как минимум трёх закрытых временных интервалов. Продолжайте сбор свечей и дождитесь новой публикации; не добавляйте вручную будущие или незакрытые свечи.",
            "info",
        )
    if "RANGE_EDGE_TOO_WEAK_FOR_GRID" in error_codes or "MARKET_TOO_TRENDY_FOR_GRID" in error_codes:
        add(
            "WAIT_FOR_RANGE_REGIME",
            "Ждать устойчивого бокового режима",
            "Текущий рынок слишком трендовый либо преимущество бокового режима недостаточно для сетки. Без свежих рыночных данных с устойчивым диапазоном торговлю не начинать.",
            "warning",
        )
    if "LIQUIDITY_UNKNOWN" in error_codes or "LIQUIDITY_TOO_LOW" in error_codes or "LIQUIDITY_LOW_FUTURES" in error_codes:
        add(
            "WAIT_FOR_CONFIRMED_LIQUIDITY",
            "Проверить ликвидность",
            "Оборот, разница цен покупки и продажи и ликвидность не подтверждают безопасность фьючерсной сетки. Дождитесь свежих рыночных данных либо исключите инструмент из списка доступных для торговли.",
            "warning",
        )

    if not actions and (guard_errors or guard_warnings):
        add(
            "READ_GUARD_AND_REFRESH",
            "Разобрать причину блокировки и пересчитать",
            "Сохранена безопасная блокировка при неопределённости. Откройте технические подробности, устраните указанную причину и дождитесь свежей публикации; не меняйте решение на «Можно торговать» вручную.",
            "info",
        )
    if not actions and status_norm == "no_trade" and (no_trade_reason_texts or risk_warning_texts):
        add(
            "KEEP_NO_TRADE_AND_REFRESH",
            "Оставить «Не торговать» и ждать свежей публикации",
            "Причина не является ошибкой UI: идея не прошла проверку допуска, уверенности или экономической целесообразности. Безопасное действие — обновить рыночные данные и дождаться новой рекомендации, а не запускать торговлю вручную.",
            "info",
        )

    return actions[:5]


_OPERATOR_REASON_HINTS: dict[str, str] = {
    "MEAN_REVERSION_EDGE_UNCONFIRMED": "Возвратность цены не подтверждена",
    "MEAN_REVERSION_EVIDENCE_INSUFFICIENT": "Недостаточно рыночных данных",
    "PROXY_MONETARY_EXPECTANCY_UNPROVEN": "Недостаточно данных об эффективности",
    "PROXY_MONETARY_EXPECTANCY_NON_POSITIVE": "Ожидание стратегии неположительное",
    "CALIBRATED_CONFIDENCE_UNAVAILABLE": "Недостаточно данных для калибровки",
    "FUNDING_EXTREME": "Платёж финансирования слишком неблагоприятен",
    "FUNDING_RATE_UNKNOWN": "Нет надёжных данных о платеже финансирования",
    "FUNDING_INTERVAL_UNKNOWN": "Неизвестен интервал платежа финансирования",
    "WAIT_FOR_TIGHTER_SPREAD": "Спред слишком широк",
    "SPREAD_TOO_WIDE": "Спред слишком широк",
    "AVOID_GRID_IN_STRONG_TREND": "Слишком сильный тренд",
    "STRONG_TREND": "Слишком сильный тренд",
    "INVALID_MARKET_REFERENCE_PRICE": "Нет корректной рыночной цены",
    "CURRENT_PRICE_OUTSIDE_RANGE": "Цена вышла из диапазона",
    "CURRENT_PRICE_OUTSIDE_KILL_SWITCH": "Цена вышла за защитную границу",
    "RECOMMENDATION_EXPIRED": "Рекомендация устарела",
    "PUBLICATION_CHAIN_EXPIRED": "Рекомендация устарела",
    "STALE_RECOMMENDATION": "Рекомендация устарела",
    "STALE_MARKET_DATA": "Рыночные данные устарели",
    "TICKER_STALE": "Рыночные данные устарели",
    "DAILY_LOSS_BUDGET_EXCEEDED": "Превышен дневной лимит риска",
    "INSUFFICIENT_RISK_BUFFER": "Недостаточный запас капитала",
    "MIN_NOTIONAL_NOT_MET": "Объём ниже минимума биржи",
    "MIN_QTY_NOT_MET": "Количество ниже минимума биржи",
    "BYBIT_METADATA_MISSING": "Не подтверждены параметры инструмента",
    "INSTRUMENT_METADATA_ABSENT": "Инструмент недоступен на бирже",
    "AWAITING_REVIEW": "Ожидается обязательная проверка",
    "ALL_REQUIRED_GATES_PASSED": "Все проверки пройдены",
    "OPERATOR_CONFIRMED_EXECUTION": "Запуск подтверждён оператором",
}


def _operator_reason_hint(code: str, status: str) -> str:
    normalized_code = str(code or "").strip().upper()
    if normalized_code in _OPERATOR_REASON_HINTS:
        return _OPERATOR_REASON_HINTS[normalized_code]
    if normalized_code.startswith("LIVE_VALIDATION_"):
        return "Негативная статистика реальных запусков"
    if "SPREAD" in normalized_code:
        return "Спред слишком широк"
    if "FUNDING" in normalized_code:
        return "Проверка funding не пройдена"
    if "STALE" in normalized_code or "EXPIRED" in normalized_code:
        return "Рекомендация или данные устарели"
    if "PRICE" in normalized_code and ("RANGE" in normalized_code or "BOUND" in normalized_code):
        return "Цена вне допустимого диапазона"
    if "BYBIT" in normalized_code or "INSTRUMENT" in normalized_code:
        return "Параметры инструмента не подтверждены"

    normalized_status = str(status or "unknown").strip().lower()
    return {
        "recommended": "Все проверки пройдены",
        "active": "Все проверки пройдены",
        "executed": "Запуск подтверждён оператором",
        "pending": "Ожидается обязательная проверка",
        "blocked": "Есть блокирующая проверка",
        "no_trade": "Не пройдены условия запуска",
        "suppressed": "Рекомендация временно скрыта",
        "expired": "Рекомендация устарела",
        "ignored": "Отклонено оператором",
    }.get(normalized_status, "Решение требует проверки")


def _operator_summary_for_reco(
    rec: dict[str, Any],
    *,
    conn: Any | None = None,
    guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stable six-field list contract; full diagnostics remain in Details."""
    del conn
    status = str(rec.get("effective_status") or rec.get("status") or "unknown").strip().lower()
    reasons = rec.get("reasons") if isinstance(rec.get("reasons"), dict) else {}
    metrics = reasons.get("operator_metrics") if isinstance(reasons.get("operator_metrics"), dict) else {}
    plan = metrics.get("plan_rr") if isinstance(metrics.get("plan_rr"), dict) else {}
    empirical = metrics.get("empirical_expectancy") if isinstance(metrics.get("empirical_expectancy"), dict) else {}

    if status in {"recommended", "active"}:
        decision = "enter_allowed"
    elif status == "pending":
        decision = "wait"
    elif status == "executed":
        decision = "executed"
    else:
        decision = "do_not_enter"

    candidates: list[Any] = []
    if status == "blocked" and isinstance(guard, dict):
        candidates.extend(guard.get("errors") if isinstance(guard.get("errors"), list) else [])
    blocks = rec.get("blocks") if isinstance(rec.get("blocks"), list) else []
    if status == "blocked":
        candidates.extend(blocks)
    layers = reasons.get("decision_layers") if isinstance(reasons.get("decision_layers"), dict) else {}
    if status == "no_trade":
        candidates.extend(layers.get("no_trade_reasons") if isinstance(layers.get("no_trade_reasons"), list) else [])
    if status == "pending":
        candidates.append({"code": "AWAITING_REVIEW", "msg": "Ожидается обязательная проверка"})
    if status in {"recommended", "active"}:
        candidates.append({"code": "ALL_REQUIRED_GATES_PASSED", "msg": "Все обязательные проверки пройдены"})
    if status == "executed":
        candidates.append({"code": "OPERATOR_CONFIRMED_EXECUTION", "msg": "Запуск подтверждён оператором"})

    primary_code = "STATUS_ONLY"
    primary_text = status or "unknown"
    for item in candidates:
        if isinstance(item, dict):
            code = str(item.get("code") or item.get("reason_code") or "").strip()
            text = str(item.get("msg") or item.get("message") or item.get("reason") or code).strip()
        else:
            code = "REASON"
            text = str(item or "").strip()
        if text:
            primary_code = code or "REASON"
            primary_text = text
            break

    return {
        "decision": decision,
        "effective_status": status,
        "plan_rr": _finite_float_or_none(plan.get("rr")),
        "plan_rr_status": str(plan.get("status") or "unavailable"),
        "empirical_expectancy_status": str(empirical.get("status") or "insufficient"),
        "empirical_mean_return": _finite_float_or_none(empirical.get("mean_return")),
        "primary_reason_code": primary_code,
        "primary_reason": _operator_reason_hint(primary_code, status),
        "primary_reason_detail": primary_text,
    }


def _operator_decision_context_for_reco(
    rec: dict[str, Any],
    *,
    conn: Any | None = None,
    guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compact, operator-facing decision context for the Details panel.

    This is intentionally not another source of trading semantics. It summarizes
    already canonical trade_plan/economics/preflight fields so the UI can answer
    the operator's pre-launch questions without parsing raw diagnostics.
    """
    now = int(time.time())
    ctx = _trade_plan_price_context(rec)
    params, plan = _rec_params_and_plan(rec)
    economics = _first_mapping(params.get("economics"), plan.get("economics"))
    cost_model = _cost_model_from_rec(rec)
    reasons = rec.get("reasons") if isinstance(rec.get("reasons"), dict) else {}
    operator_metrics = _first_mapping(
        reasons.get("operator_metrics"),
        params.get("operator_metrics"),
    )
    plan_rr_metrics = operator_metrics.get("plan_rr") if isinstance(operator_metrics.get("plan_rr"), dict) else {}
    empirical_metrics = operator_metrics.get("empirical_expectancy") if isinstance(operator_metrics.get("empirical_expectancy"), dict) else {}

    reference_price = ctx.get("reference_price")
    range_lower = ctx.get("range_lower")
    range_upper = ctx.get("range_upper")
    kill_lower = ctx.get("kill_switch_lower")
    kill_upper = ctx.get("kill_switch_upper")

    rec_ts = _safe_int_or_none(rec.get("ts"))
    ttl_sec = _safe_int_or_none(rec.get("ttl_sec"))
    chain_ctx = _publication_chain_context_for_reco(conn, rec, now_ts=now)
    age_sec = chain_ctx.get("recommendation_row_age_sec")
    expires_in_sec = None
    if chain_ctx.get("recommendation_timestamp_valid") is True and rec_ts is not None and ttl_sec is not None and ttl_sec > 0:
        expires_in_sec = int(rec_ts) + int(ttl_sec) - now
    chain_expires_in_sec = None
    chain_started_ts = _safe_int_or_none(chain_ctx.get("publication_chain_started_ts"))
    if chain_ctx.get("publication_chain_timestamp_valid") is True and chain_started_ts is not None and ttl_sec is not None and ttl_sec > 0:
        chain_expires_in_sec = int(chain_started_ts) + int(ttl_sec) - now

    ticker: dict[str, Any] | None = None
    if conn is not None:
        try:
            ticker = db.get_latest_ticker(conn, str(rec.get("venue") or ""), str(rec.get("symbol") or ""))
        except Exception:
            ticker = None
    current_price = _current_price_from_ticker(ticker)
    ticker_ts = _safe_int_or_none((ticker or {}).get("ts"))
    ticker_age_sec = None if ticker_ts is None else max(0, now - int(ticker_ts))

    price_status = "missing"
    if current_price is not None:
        if range_lower is not None and range_upper is not None:
            price_status = "inside_range" if range_lower <= current_price <= range_upper else "outside_range"
        else:
            price_status = "available"

    direction = str(rec.get("direction") or "").strip().lower()
    leverage = _finite_float_or_none(params.get("leverage"))
    # Bybit Futures Grid uses cross margin.  Do not fabricate a standalone
    # isolated liquidation price; expose the deterministic bot-equity stress
    # generated from grid commitment and kill-switch geometry instead.
    liq_price = None
    liq_buffer = _finite_float_or_none(economics.get("cross_margin_stress_buffer_pct"))
    if liq_buffer is None:
        liq_buffer = _finite_float_or_none(economics.get("liquidation_buffer_pct"))

    guard_errors = guard.get("errors") if isinstance(guard, dict) and isinstance(guard.get("errors"), list) else []
    guard_warnings = guard.get("warnings") if isinstance(guard, dict) and isinstance(guard.get("warnings"), list) else []
    if isinstance(guard, dict) and guard.get("ok") is True:
        preflight_status = "ok"
    elif guard_errors:
        preflight_status = "blocked"
    elif guard_warnings:
        preflight_status = "warning"
    elif isinstance(guard, dict):
        preflight_status = "unknown"
    else:
        preflight_status = "not_checked"

    net_profit_bps = _finite_float_or_none(economics.get("net_profit_bps"))
    gross_profit_bps = _finite_float_or_none(economics.get("gross_profit_bps"))
    execution_cost_bps = _finite_float_or_none(economics.get("execution_cost_bps"))
    if execution_cost_bps is None:
        execution_cost_bps = _finite_float_or_none(cost_model.get("execution_cost_bps"))
    funding_cost_bps = _finite_float_or_none(economics.get("funding_cost_bps"))
    if funding_cost_bps is None:
        expected_funding = _finite_float_or_none(cost_model.get("expected_funding_bps"))
        funding_cost_bps = None if expected_funding is None else max(0.0, expected_funding)

    if liq_buffer is None:
        risk_profile = "unknown"
    elif liq_buffer < OPERATOR_MIN_LIQUIDATION_BUFFER_PCT:
        risk_profile = "critical"
    elif liq_buffer < 20.0:
        risk_profile = "high"
    elif liq_buffer < 35.0:
        risk_profile = "moderate"
    else:
        risk_profile = "low"

    context = {
        "recommendation_ts": rec_ts,
        "recommendation_timestamp_valid": chain_ctx.get("recommendation_timestamp_valid"),
        "recommendation_timestamp_invalid_reason": chain_ctx.get("recommendation_timestamp_invalid_reason"),
        "recommendation_timestamp_future_skew_sec": chain_ctx.get("recommendation_timestamp_future_skew_sec"),
        "recommendation_age_sec": age_sec,
        "recommendation_row_age_sec": age_sec,
        "publication_root_rec_id": chain_ctx.get("publication_root_rec_id"),
        "publication_chain_started_ts": chain_ctx.get("publication_chain_started_ts"),
        "publication_chain_updated_ts": chain_ctx.get("publication_chain_updated_ts"),
        "publication_chain_timestamp_valid": chain_ctx.get("publication_chain_timestamp_valid"),
        "publication_chain_timestamp_invalid_reason": chain_ctx.get("publication_chain_timestamp_invalid_reason"),
        "publication_chain_timestamp_future_skew_sec": chain_ctx.get("publication_chain_timestamp_future_skew_sec"),
        "publication_chain_age_sec": chain_ctx.get("publication_chain_age_sec"),
        "publication_chain_update_count": chain_ctx.get("publication_chain_update_count"),
        "publication_chain_expires_in_sec": chain_expires_in_sec,
        "ttl_sec": ttl_sec,
        "expires_in_sec": expires_in_sec,
        "is_expired": bool(expires_in_sec is not None and expires_in_sec <= 0),
        "is_publication_chain_expired": bool(chain_expires_in_sec is not None and chain_expires_in_sec <= 0),
        "current_price": current_price,
        "ticker_ts": ticker_ts,
        "ticker_age_sec": ticker_age_sec,
        "price_status": price_status,
        "entry_price": reference_price,
        "price_drift_from_entry_pct": _pct_delta(current_price, reference_price),
        "range_lower": range_lower,
        "range_upper": range_upper,
        "distance_to_lower_pct": _distance_from_current_to_bound_pct(current_price, range_lower, side="lower"),
        "distance_to_upper_pct": _distance_from_current_to_bound_pct(current_price, range_upper, side="upper"),
        "kill_switch_lower": kill_lower,
        "kill_switch_upper": kill_upper,
        "distance_to_kill_lower_pct": _distance_from_current_to_bound_pct(current_price, kill_lower, side="lower"),
        "distance_to_kill_upper_pct": _distance_from_current_to_bound_pct(current_price, kill_upper, side="upper"),
        "net_profit_bps": net_profit_bps,
        "gross_profit_bps": gross_profit_bps,
        "execution_cost_bps": execution_cost_bps,
        "funding_cost_bps": funding_cost_bps,
        "plan_rr": _finite_float_or_none(plan_rr_metrics.get("rr")),
        "plan_rr_status": str(plan_rr_metrics.get("status") or "unavailable"),
        "plan_projected_net_reward_usdt": _finite_float_or_none(plan_rr_metrics.get("projected_net_reward_usdt")),
        "plan_kill_switch_loss_usdt": _finite_float_or_none(plan_rr_metrics.get("kill_switch_loss_usdt")),
        "plan_projected_completed_pairs": _finite_float_or_none(plan_rr_metrics.get("projected_completed_pairs")),
        "empirical_expectancy_status": str(empirical_metrics.get("status") or "insufficient"),
        "empirical_expectancy_available": bool(empirical_metrics.get("available")),
        "empirical_expectancy_decision_ready": bool(empirical_metrics.get("decision_ready")),
        "empirical_mean_return": _finite_float_or_none(empirical_metrics.get("mean_return")),
        "empirical_expected_shortfall": _finite_float_or_none(empirical_metrics.get("expected_shortfall")),
        "empirical_rr": _finite_float_or_none(empirical_metrics.get("empirical_rr")),
        "empirical_return_samples": _safe_int_or_none(empirical_metrics.get("return_samples")),
        "empirical_temporal_cluster_count": _safe_int_or_none(empirical_metrics.get("temporal_cluster_count")),
        "empirical_minimum_temporal_clusters": _safe_int_or_none(empirical_metrics.get("minimum_temporal_clusters")),
        "empirical_confidence_level": _finite_float_or_none(
            (empirical_metrics.get("confidence_interval") or {}).get("level")
            if isinstance(empirical_metrics.get("confidence_interval"), dict)
            else None
        ),
        "empirical_confidence_interval_lower": _finite_float_or_none(
            (empirical_metrics.get("confidence_interval") or {}).get("lower")
            if isinstance(empirical_metrics.get("confidence_interval"), dict)
            else None
        ),
        "empirical_confidence_interval_upper": _finite_float_or_none(
            (empirical_metrics.get("confidence_interval") or {}).get("upper")
            if isinstance(empirical_metrics.get("confidence_interval"), dict)
            else None
        ),
        "estimated_liquidation_price": liq_price,
        "liquidation_buffer_pct": liq_buffer,
        "risk_profile": risk_profile,
        "preflight_status": preflight_status,
        "preflight_error_count": len(guard_errors),
        "preflight_warning_count": len(guard_warnings),
    }
    context["operator_next_actions"] = _operator_next_actions_for_reco(
        rec,
        ctx=context,
        guard_errors=guard_errors,
        guard_warnings=guard_warnings,
    )
    return context


def _augment_reco_for_ui(rec: dict[str, Any], *, conn: Any | None = None) -> dict[str, Any]:
    out = dict(rec)
    out["directional_exit_levels"] = _directional_exit_payload_for_reco(out)
    venue = str(out.get("venue") or "")
    symbol = str(out.get("symbol") or "")
    try:
        bybit_meta = _fetch_bybit_instrument_meta(venue, symbol) if venue and symbol else {}
    except Exception:
        bybit_meta = {}
    if not isinstance(out.get("params"), dict) or not out.get("params"):
        out["bybit_meta"] = bybit_meta
        out["bybit_plan_validation"] = _validate_trade_plan_against_bybit_meta(out, bybit_meta)
        guard = _validate_trade_plan_against_bybit_meta(out, bybit_meta, require_meta=True, require_execution_plan=True)
        guard = _append_recommendation_timestamp_errors_to_operator_guard(guard, conn, out)
        guard_errors = guard.get("errors") if isinstance(guard.get("errors"), list) else []
        payload_error = {
            "code": "PAYLOAD_UNAVAILABLE_FOR_OPERATOR_GUARD",
            "msg": "params_json пустой или повреждён; operator guard блокирует запуск без полного исполнимого trade_plan.",
        }
        if not any(isinstance(err, dict) and err.get("code") == payload_error["code"] for err in guard_errors):
            guard_errors.append(payload_error)
        guard["errors"] = guard_errors
        guard["ok"] = False
        guard["critical"] = True
        out["bybit_operator_guard"] = guard
        # Empty/corrupted legacy payloads intentionally keep params/reasons/blocks
        # untouched for API-shape compatibility, but they still must not look
        # actionable in list banners, status badges or effective-status filters.
        if str(out.get("status") or "").strip().lower() in {"recommended", "active", "pending"}:
            out["stored_status"] = out.get("stored_status") or out.get("status")
            out["status"] = "blocked"
            out["effective_status"] = "blocked"
        _merge_bybit_operator_guard_into_ui_payload(out, out["bybit_operator_guard"])
        _apply_runtime_risk_limits_guard(out, conn=conn)
        out["operator_decision_context"] = _operator_decision_context_for_reco(out, conn=conn, guard=out.get("bybit_operator_guard"))
        _apply_llm_effective_pending_guard(out)
        _apply_publication_chain_effective_expiry_guard(out)
        _ensure_effective_status(out)
        out["operator_summary"] = _operator_summary_for_reco(out, conn=conn, guard=out.get("bybit_operator_guard"))
        return out
    out = _snap_reco_payload_to_bybit_meta(out, bybit_meta)
    out["directional_exit_levels"] = _directional_exit_payload_for_reco(out)
    out["bybit_meta"] = bybit_meta
    out["bybit_plan_validation"] = _validate_trade_plan_against_bybit_meta(out, bybit_meta)
    out["bybit_operator_guard"] = _validate_trade_plan_against_bybit_meta(out, bybit_meta, require_meta=True, require_execution_plan=True)
    out["bybit_operator_guard"] = _append_recommendation_timestamp_errors_to_operator_guard(
        out["bybit_operator_guard"], conn, out
    )
    _merge_bybit_operator_guard_into_ui_payload(out, out["bybit_operator_guard"])
    _apply_runtime_risk_limits_guard(out, conn=conn)
    out["operator_decision_context"] = _operator_decision_context_for_reco(out, conn=conn, guard=out.get("bybit_operator_guard"))
    _apply_llm_effective_pending_guard(out)
    _apply_publication_chain_effective_expiry_guard(out)
    _ensure_effective_status(out)
    out["operator_summary"] = _operator_summary_for_reco(out, conn=conn, guard=out.get("bybit_operator_guard"))
    return out


def _collapse_recommendation_items_by_publication_chain(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Скрывает дубли одной publication-chain в operator-facing списке.

    Audit-след в БД сохраняется полностью: каждая рекомендация остаётся отдельной
    строкой со своим ts/reasons/status. Но операторскому списку почти никогда не
    нужна вся цепочка near-identical `active` updates поверх одного root — это
    визуально выглядит как поток повторов и затрудняет разбор живых идей.
    Поэтому по умолчанию показываем один лучший элемент на publication_root.
    """
    if not items:
        return [], 0

    status_priority = {
        "recommended": 0,
        "active": 1,
        "executed": 2,
        "ignored": 3,
        "pending": 4,
        "blocked": 5,
        "no_trade": 6,
        "suppressed": 7,
        "expired": 8,
    }

    def _sort_key(rec: dict[str, Any]) -> tuple[int, int, int, float, float]:
        return (
            int(rec.get("ts") or 0),
            -int(status_priority.get(str(rec.get("status") or "").strip().lower(), 99)),
            1 if bool(rec.get("is_outcome_label_root")) else 0,
            float(rec.get("confidence") or 0.0),
            float(rec.get("score") or 0.0),
        )

    best_by_root: dict[str, dict[str, Any]] = {}
    for item in items:
        rec = dict(item)
        root = str(rec.get("publication_root_rec_id") or rec.get("rec_id") or "").strip() or str(rec.get("rec_id") or "")
        prev = best_by_root.get(root)
        if prev is None or _sort_key(rec) > _sort_key(prev):
            best_by_root[root] = rec

    deduped = list(best_by_root.values())
    deduped.sort(
        key=lambda rec: (
            int(status_priority.get(str(rec.get("status") or "").strip().lower(), 99)),
            -float(rec.get("confidence") or 0.0),
            -float(rec.get("score") or 0.0),
            -int(rec.get("ts") or 0),
        )
    )
    hidden = max(0, len(items) - len(deduped))
    return deduped, hidden


def _operator_fetch_statuses_for_effective_filters(statuses: list[str]) -> list[str]:
    """Return DB statuses that can produce the requested operator-facing statuses.

    The UI displays an *effective* status: live Bybit metadata/operator guard can
    turn a persisted recommended/active/pending row into blocked without mutating
    the audit row. Therefore a blocked-only view must also scan actionable DB rows
    that may become blocked after augmentation.
    """
    out = list(dict.fromkeys(str(s or "").strip().lower() for s in statuses if str(s or "").strip()))
    if "blocked" in out:
        for convertible in ("recommended", "active", "pending"):
            if convertible not in out:
                out.append(convertible)
    if bool(getattr(settings, "llm_reviewer_enabled", False)) and "pending" in out:
        for convertible in ("recommended", "active"):
            if convertible not in out:
                out.append(convertible)
    return out


def _operator_candidate_limit(top_n: int, *, collapse_chains: bool, statuses: list[str]) -> int:
    """Fetch enough candidates before effective-status filtering.

    If the Bybit operator guard blocks several actionable rows, filtering after
    augmentation can otherwise leave the table under-filled and make the list and
    detail card appear to disagree.
    """
    if top_n <= 0:
        return 0
    if not collapse_chains:
        return top_n
    if "blocked" in set(statuses):
        return min(4000, max(top_n * 8, 80))
    return min(4000, max(top_n * 4, 40))


def _filter_operator_items_by_effective_status(items: list[dict[str, Any]], statuses: list[str], top_n: int) -> list[dict[str, Any]]:
    allowed = {str(status or "").strip().lower() for status in statuses if str(status or "").strip()}
    if not allowed or top_n <= 0:
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        effective_status = str(item.get("effective_status") or item.get("status") or "").strip().lower()
        if effective_status not in allowed:
            continue
        out.append(item)
        if len(out) >= top_n:
            break
    return out


def _load_recommendations_for_operator_view(
    conn,
    *,
    venue: str | None,
    top_n: int,
    min_conf: float,
    statuses: list[str],
    snapshot_ts: int | None,
    strict_min_conf: bool,
    collapse_chains: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Возвращает рекомендации для operator-facing списка без скрытой недовыборки chain'ов.

    Простое чтение `top_n * 4` raw-строк выглядело достаточным, пока одна и та же
    publication-chain не начинала доминировать десятками `active` updates. Тогда
    дедуп-проход скрывал почти всё, а API возвращал меньше уникальных идей, чем
    реально было доступно в том же snapshot. Для оператора это выглядело как
    "пропавшие" рекомендации и визуально усиливало ощущение repeated-потока.

    Поэтому при включённом collapse_chains адаптивно увеличиваем budget выборки до
    тех пор, пока не наберём нужное число уникальных roots или пока не исчерпаем
    разумный scan-cap для одного snapshot.
    """
    if top_n <= 0:
        return [], 0

    if not collapse_chains:
        raw_items = db.get_recommendations(
            conn,
            venue=venue,
            top_n=top_n,
            min_conf=min_conf,
            statuses=statuses,
            snapshot_ts=snapshot_ts,
            strict_min_conf=strict_min_conf,
        )
        return raw_items, 0

    budget = max(top_n, top_n * 4, 20)
    scan_cap = min(4000, max(200, top_n * 20))
    previous_raw_count = -1
    raw_items: list[dict[str, Any]] = []
    deduped: list[dict[str, Any]] = []
    hidden_duplicates = 0

    while True:
        raw_items = db.get_recommendations(
            conn,
            venue=venue,
            top_n=budget,
            min_conf=min_conf,
            statuses=statuses,
            snapshot_ts=snapshot_ts,
            strict_min_conf=strict_min_conf,
        )
        deduped, hidden_duplicates = _collapse_recommendation_items_by_publication_chain(raw_items)
        raw_count = len(raw_items)
        if len(deduped) >= top_n:
            break
        if raw_count < budget:
            break
        if budget >= scan_cap:
            break
        if raw_count == previous_raw_count:
            break
        previous_raw_count = raw_count
        budget = min(scan_cap, budget * 2)

    return deduped[:top_n], hidden_duplicates


def _finite_float_or_none(value: Any) -> float | None:
    # bool is an int subclass; reject it before float() so malformed JSON cannot
    # become price/qty/leverage=1 or 0 on the execution path.
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _count_step_decimals(step: float | str | None) -> int | None:
    if step in (None, ""):
        return None
    raw = str(step).strip().lower()
    if not raw:
        return None
    if "e-" in raw:
        try:
            return max(0, int(raw.split("e-")[-1]))
        except Exception:
            return None
    if "." not in raw:
        return 0
    normalized = raw.rstrip("0")
    if normalized.endswith("."):
        return 0
    return max(0, len(normalized.split(".", 1)[1]))


def _quantize_to_step(value: float | None, step: float | None, *, mode: str = "nearest") -> float | None:
    num = _finite_float_or_none(value)
    tick = _finite_float_or_none(step)
    if num is None or tick is None or tick <= 0:
        return None
    rounded = quantize_step(str(num), str(tick), mode=mode)
    return float(rounded) if rounded is not None else None


def _format_step_aligned(value: float | None, step: float | None) -> str | None:
    num = _finite_float_or_none(value)
    tick = _finite_float_or_none(step)
    if num is None:
        return None
    decimals = _count_step_decimals(step if tick is not None and tick > 0 else num) or 0
    return f"{num:.{max(0, int(decimals))}f}"




def _as_aligned_float(value: float | None, step: float | None) -> float | None:
    """Return a float whose decimal string is explicitly aligned to the step."""
    formatted = _format_step_aligned(value, step)
    if formatted is None:
        return _finite_float_or_none(value)
    try:
        return float(formatted)
    except Exception:
        return _finite_float_or_none(value)


def _update_float_key(mapping: Any, key: str, value: float | None) -> None:
    if isinstance(mapping, dict) and value is not None:
        mapping[key] = float(value)


def _snap_reco_payload_to_bybit_meta(rec: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Return a recommendation copy with operator-facing levels snapped to Bybit filters.

    The recommender builds grid levels from ATR/percent formulas before live exchange
    filters are fetched. The strict operator guard must stay fail-closed for manual or
    malformed payloads, but the operator UI/execution path should validate the exchange-
    executable values that the payload itself already exposes as ``snapped_levels``.
    """
    if not isinstance(rec, dict):
        return {}
    out = copy.deepcopy(rec)
    if not isinstance(meta, dict) or not meta:
        return out

    tick_size = _finite_float_or_none(meta.get("tick_size"))
    qty_step = _finite_float_or_none(meta.get("qty_step"))
    min_order_qty = _finite_float_or_none(meta.get("min_order_qty"))
    min_notional = _finite_float_or_none(meta.get("min_notional"))
    leverage_step = _finite_float_or_none(meta.get("leverage_step"))

    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    if not isinstance(out.get("params"), dict):
        out["params"] = params
    plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    if not isinstance(params.get("trade_plan"), dict):
        params["trade_plan"] = plan
    levels = plan.get("levels") if isinstance(plan.get("levels"), dict) else {}
    if not isinstance(plan.get("levels"), dict):
        plan["levels"] = levels
    operator_sheet = params.get("operator_sheet") if isinstance(params.get("operator_sheet"), dict) else {}
    if not isinstance(params.get("operator_sheet"), dict) and operator_sheet:
        params["operator_sheet"] = operator_sheet
    grid_count_resolution = _grid_count_resolution_for_reco(out)
    strict_grid_count = grid_count_resolution.get("value") if grid_count_resolution.get("ok") else None
    conservative_grid_count = grid_count_resolution.get("conservative_max")

    sizing_candidates = [
        params.get("sizing") if isinstance(params.get("sizing"), dict) else {},
        plan.get("sizing") if isinstance(plan.get("sizing"), dict) else {},
        operator_sheet.get("sizing") if isinstance(operator_sheet, dict) and isinstance(operator_sheet.get("sizing"), dict) else {},
    ]
    auto_snap_allowed = any(
        str(candidate.get("basis") or "").strip() == "minimum_viable_operator_default"
        or (
            isinstance(candidate.get("exchange_filter_assumption"), dict)
            and str(candidate["exchange_filter_assumption"].get("mode") or "").strip() in {
                "fallback_qty_step_until_bybit_preflight",
                "provisional_target_notional_until_bybit_preflight",
            }
        )
        for candidate in sizing_candidates
    )
    if not auto_snap_allowed:
        return out

    def snap(value: Any, step: float | None, *, mode: str = "nearest") -> float | None:
        snapped = _quantize_to_step(_finite_float_or_none(value), step, mode=mode)
        return _as_aligned_float(snapped, step) if snapped is not None else None

    snapped_price: dict[str, float] = {}
    if tick_size is not None and tick_size > 0:
        price_paths: dict[str, tuple[tuple[dict[str, Any], str], ...]] = {
            "reference_price": ((plan, "reference_price"), (params, "price_ref"), (operator_sheet, "price_ref")),
            "range_lower": ((levels.setdefault("range", {}), "lower"), (params, "price_range_lower"), (operator_sheet, "range_lower")),
            "range_upper": ((levels.setdefault("range", {}), "upper"), (params, "price_range_upper"), (operator_sheet, "range_upper")),
            "kill_switch_lower": ((levels.setdefault("kill_switch", {}), "lower"),),
            "kill_switch_upper": ((levels.setdefault("kill_switch", {}), "upper"),),
        }
        if operator_sheet:
            ks = operator_sheet.get("kill_switch") if isinstance(operator_sheet.get("kill_switch"), dict) else {}
            if isinstance(ks, dict):
                price_paths["kill_switch_lower"] = (*price_paths["kill_switch_lower"], (ks, "lower"))
                price_paths["kill_switch_upper"] = (*price_paths["kill_switch_upper"], (ks, "upper"))

        source_values = {
            "reference_price": plan.get("reference_price", params.get("price_ref")),
            "range_lower": (levels.get("range") or {}).get("lower", params.get("price_range_lower")) if isinstance(levels.get("range"), dict) else params.get("price_range_lower"),
            "range_upper": (levels.get("range") or {}).get("upper", params.get("price_range_upper")) if isinstance(levels.get("range"), dict) else params.get("price_range_upper"),
            "kill_switch_lower": (levels.get("kill_switch") or {}).get("lower") if isinstance(levels.get("kill_switch"), dict) else None,
            "kill_switch_upper": (levels.get("kill_switch") or {}).get("upper") if isinstance(levels.get("kill_switch"), dict) else None,
        }
        # Preserve the containment guarantees of the generated grid after tick rounding.
        # Rounding every boundary to the nearest tick can shrink the tradable range or
        # move kill-switch levels inside the range, which makes the UI display a safer
        # plan than the exchange can actually place. Lower boundaries snap down, upper
        # boundaries snap up; only the reference price is allowed to use nearest-tick.
        snap_modes = {
            "reference_price": "nearest",
            "range_lower": "down",
            "range_upper": "up",
            "kill_switch_lower": "down",
            "kill_switch_upper": "up",
        }
        def apply_snapped_price(name: str, snapped: float | None) -> None:
            if snapped is None:
                return
            snapped_price[name] = snapped
            for mapping, key in price_paths.get(name, ()):  # update all aliases
                _update_float_key(mapping, key, snapped)

        for name, raw in source_values.items():
            snapped = snap(raw, tick_size, mode=snap_modes.get(name, "nearest"))
            apply_snapped_price(name, snapped)

        grid_step = levels.get("grid_step") if isinstance(levels.get("grid_step"), dict) else {}
        if not isinstance(levels.get("grid_step"), dict):
            levels["grid_step"] = grid_step

        def apply_snapped_step(snapped_step_value: float | None) -> None:
            if snapped_step_value is None or snapped_step_value <= 0:
                return
            grid_step["step_abs"] = float(snapped_step_value)
            ref = snapped_price.get("reference_price") or _finite_float_or_none(plan.get("reference_price"))
            if ref and ref > 0:
                step_pct = float(snapped_step_value) / float(ref) * 100.0
                grid_step["step_pct"] = float(step_pct)
                params["grid_spacing_pct"] = float(step_pct)
                if strict_geometry_payload:
                    params["actual_grid_spacing_pct"] = float(step_pct)
                    params["actual_grid_step_abs"] = float(snapped_step_value)
                if operator_sheet:
                    operator_sheet["grid_spacing_pct"] = float(step_pct)

        raw_step = grid_step.get("step_abs")
        grid_count = int(strict_grid_count or 0)
        strict_geometry_payload = (
            str(params.get("grid_geometry_model") or "").strip() == "bybit_arithmetic_range_width_div_grid_count"
            or params.get("actual_grid_step_abs") is not None
            or params.get("actual_grid_spacing_pct") is not None
        )
        if strict_geometry_payload and grid_count > 0:
            # Generated Bybit arithmetic futures grids are configured by
            # lower/upper + Number of Grids. The displayed step is only a derived
            # operator hint. If we snap lower/upper and step independently, a
            # harmless sub-tick/economic padding delta can turn into
            # floor((upper-lower)/step_abs)=grid_count-1 and show a false launch
            # blocker. Rebuild the snapped range from the snapped lower bound, an
            # exchange-aligned step, and the exact grid_count instead.
            lower_bound = snapped_price.get("range_lower")
            upper_bound = snapped_price.get("range_upper")
            if lower_bound is not None and upper_bound is not None and upper_bound > lower_bound:
                span = float(upper_bound) - float(lower_bound)
                step_floor = span / float(grid_count)
                model_step = _finite_float_or_none(params.get("actual_grid_step_abs")) or _finite_float_or_none(raw_step)
                required_step = max(step_floor, float(model_step or 0.0))
                aligned_step = snap(required_step, tick_size, mode="up")
                if aligned_step is not None and aligned_step > 0:
                    aligned_span = float(aligned_step) * float(grid_count)
                    # Float roundoff can otherwise leave the span infinitesimally
                    # below grid_count * step and reintroduce the mismatch.
                    if aligned_span + max(1e-12, abs(tick_size) * 1e-9) < span:
                        aligned_step = snap(float(aligned_step) + float(tick_size), tick_size, mode="up")
                        aligned_span = float(aligned_step) * float(grid_count) if aligned_step is not None else aligned_span
                    if aligned_step is not None and aligned_step > 0:
                        new_upper = snap(float(lower_bound) + float(aligned_span), tick_size, mode="up")
                        if new_upper is not None and new_upper > upper_bound:
                            old_upper = upper_bound
                            apply_snapped_price("range_upper", new_upper)
                            upper_bound = new_upper
                            if operator_sheet:
                                operator_sheet["range_upper"] = float(new_upper)
                            ref = snapped_price.get("reference_price") or _finite_float_or_none(plan.get("reference_price"))
                            if ref and ref > 0 and lower_bound is not None:
                                params["range_span_pct_total"] = float((float(new_upper) - float(lower_bound)) / float(ref) * 100.0)
                            old_ks_upper = snapped_price.get("kill_switch_upper")
                            if old_ks_upper is not None and old_ks_upper <= new_upper:
                                old_pad = max(float(tick_size), float(old_ks_upper) - float(old_upper)) if old_ks_upper > old_upper else float(tick_size)
                                new_ks_upper = snap(float(new_upper) + old_pad, tick_size, mode="up")
                                apply_snapped_price("kill_switch_upper", new_ks_upper)
                        apply_snapped_step(aligned_step)

        # A grid step rounded down can invalidate the net-edge floor used by the
        # recommender. Snap up so the exchange-aligned step is never thinner than
        # the economics/risk model assumed. For strict Bybit arithmetic payloads,
        # the branch above already rebuilt range+step as an exact grid_count model.
        snapped_step = snap(grid_step.get("step_abs", raw_step), tick_size, mode="up")
        apply_snapped_step(snapped_step)

        tp_per_leg = levels.get("tp_per_leg") if isinstance(levels.get("tp_per_leg"), dict) else {}
        if not isinstance(levels.get("tp_per_leg"), dict):
            levels["tp_per_leg"] = tp_per_leg
        # Same principle as grid step: do not round a TP hint below the modelled
        # adjacent interval. Recurring fees are checked against that interval;
        # market friction and funding remain separate Total-P&L layers.
        snapped_tp = snap(tp_per_leg.get("abs"), tick_size, mode="up")
        if snapped_tp is not None and snapped_tp > 0:
            tp_per_leg["abs"] = snapped_tp
            ref = snapped_price.get("reference_price") or _finite_float_or_none(plan.get("reference_price"))
            if ref and ref > 0:
                tp_per_leg["pct"] = float(snapped_tp / ref * 100.0)
            if operator_sheet and isinstance(operator_sheet.get("tp_per_leg"), dict):
                operator_sheet["tp_per_leg"]["abs"] = snapped_tp
                if ref and ref > 0:
                    operator_sheet["tp_per_leg"]["pct"] = float(snapped_tp / ref * 100.0)

    leverage = _finite_float_or_none(params.get("leverage"))
    if leverage is not None and leverage_step is not None and leverage_step > 0:
        snapped_lev = snap(leverage, leverage_step, mode="nearest")
        if snapped_lev is not None and snapped_lev > 0:
            params["leverage"] = float(snapped_lev)
            if operator_sheet:
                operator_sheet["leverage"] = float(snapped_lev)

    sizing_maps: list[dict[str, Any]] = []
    for candidate in (
        params.get("sizing"),
        plan.get("sizing"),
        params.get("economics"),
        plan.get("economics"),
        operator_sheet.get("sizing") if isinstance(operator_sheet, dict) else None,
        operator_sheet.get("economics") if isinstance(operator_sheet, dict) else None,
    ):
        if isinstance(candidate, dict):
            sizing_maps.append(candidate)

    qty_keys = (
        "order_qty",
        "qty",
        "qty_per_order",
        "qty_per_leg",
        "base_qty",
        "base_qty_per_order",
        "order_size_qty",
        "leg_qty",
    )
    _, order_qty = _first_finite_from_mapping(plan.get("sizing") if isinstance(plan.get("sizing"), dict) else {}, qty_keys)
    if order_qty is None:
        _, order_qty = _first_finite_from_mapping(params.get("sizing") if isinstance(params.get("sizing"), dict) else {}, qty_keys)
    if order_qty is None:
        _, order_qty = _first_finite_from_mapping(params, qty_keys)
    if order_qty is None:
        _, order_qty = _first_finite_from_mapping(params.get("economics") if isinstance(params.get("economics"), dict) else {}, qty_keys)

    reference_price = _finite_float_or_none(plan.get("reference_price")) or _finite_float_or_none(params.get("price_ref"))
    lower_price = _finite_float_or_none((levels.get("range") or {}).get("lower")) if isinstance(levels.get("range"), dict) else None
    notional_price = _grid_min_notional_price(reference_price, lower_price, _finite_float_or_none((levels.get("range") or {}).get("upper")) if isinstance(levels.get("range"), dict) else None)

    if qty_step is not None and qty_step > 0:
        # Quantity alignment is a risk boundary: never increase a generated or
        # operator-supplied size merely to satisfy minQty/minNotional. Round the
        # requested quantity down to the live exchange step; downstream strict
        # validation then blocks values that remain below exchange minimums.
        raw_qty = max(0.0, float(order_qty or 0.0))
        snapped_qty = snap(raw_qty, qty_step, mode="down")
        if snapped_qty is not None and snapped_qty > 0:
            for mapping in sizing_maps:
                for key in ("qty_per_order", "order_qty"):
                    if key in mapping or key == "qty_per_order":
                        mapping[key] = float(snapped_qty)
            if reference_price is not None and reference_price > 0:
                order_notional = float(snapped_qty) * float(reference_price)
                upper_price = _finite_float_or_none((levels.get("range") or {}).get("upper")) if isinstance(levels.get("range"), dict) else None
                worst_notional_price = _grid_max_notional_price(reference_price, lower_price, upper_price)
                worst_order_notional = float(snapped_qty) * float(worst_notional_price or reference_price)
                grid_count = int(conservative_grid_count) if conservative_grid_count is not None else 1
                commitment = arithmetic_grid_commitment(
                    lower=lower_price,
                    upper=upper_price,
                    grid_count=grid_count,
                    reference_price=reference_price,
                    direction=str(out.get("direction") or params.get("direction") or ""),
                )
                active_orders = (
                    int(commitment["active_order_count"])
                    if commitment is not None
                    else max(1, grid_count)
                )
                committed_slots = (
                    int(commitment["committed_slot_count"])
                    if commitment is not None
                    else active_orders
                )
                max_position_slots = (
                    int(commitment["max_abs_position_slots"])
                    if commitment is not None
                    else committed_slots
                )
                total_notional = (
                    float(snapped_qty) * float(commitment["committed_notional_per_qty"])
                    if commitment is not None
                    else order_notional * active_orders
                )
                worst_total_notional = worst_order_notional * max_position_slots
                leverage_used = float(params.get("leverage") or 1.0) or 1.0
                margin_required = total_notional / max(1.0, leverage_used)
                worst_margin_required = worst_total_notional / max(1.0, leverage_used)
                for mapping in sizing_maps:
                    for key in ("order_notional_usdt", "order_notional"):
                        if key in mapping or key == "order_notional_usdt":
                            mapping[key] = float(order_notional)
                    mapping["estimated_active_orders"] = int(active_orders)
                    mapping["estimated_committed_slots"] = int(committed_slots)
                    mapping["estimated_max_position_slots"] = int(max_position_slots)
                    mapping["grid_commitment_model"] = (
                        "neutral_all_initial_opening_orders"
                        if normalize_execution_direction(out.get("direction") or params.get("direction")) == "neutral"
                        else "arithmetic_levels_plus_directional_inventory"
                    )
                    mapping["estimated_worst_case_order_notional_usdt"] = float(worst_order_notional)
                    mapping["estimated_worst_case_total_order_notional_usdt"] = float(worst_total_notional)
                    mapping["estimated_worst_case_margin_required_usdt"] = float(worst_margin_required)
                    if "estimated_total_order_notional_usdt" in mapping:
                        mapping["estimated_total_order_notional_usdt"] = float(total_notional)
                    if "estimated_margin_required_usdt" in mapping:
                        mapping["estimated_margin_required_usdt"] = float(margin_required)
                    if "estimated_max_position_notional_usdt" in mapping:
                        mapping["estimated_max_position_notional_usdt"] = max(float(mapping.get("estimated_max_position_notional_usdt") or 0.0), float(worst_total_notional))
                risk_report = params.get("risk_report") if isinstance(params.get("risk_report"), dict) else None
                if risk_report is not None:
                    risk_report["capital_required_usdt"] = float(max(float(risk_report.get("capital_required_usdt") or 0.0), worst_margin_required))
                    risk_report["estimated_worst_case_margin_required_usdt"] = float(worst_margin_required)
                if isinstance(operator_sheet, dict) and isinstance(operator_sheet.get("economics"), dict):
                    operator_sheet["economics"]["capital_required_usdt"] = float(max(float(operator_sheet["economics"].get("capital_required_usdt") or 0.0), worst_margin_required))
                    operator_sheet["economics"]["estimated_worst_case_margin_required_usdt"] = float(worst_margin_required)

    out["params"] = params
    return out


def _first_finite_from_mapping(mapping: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, float | None]:
    """Возвращает первое finite-число из набора синонимичных полей sizing.

    В проекте размер заявки не рассчитывается автоматически, но ручной/audit payload
    может уже содержать операторский qty/notional. В таком случае preflight обязан
    проверить биржевые фильтры Bybit, а не продолжать писать только предупреждение
    «размер неизвестен». Helper изолирует список legacy/операторских алиасов от
    основной валидации, чтобы одинаково обрабатывать params и trade_plan.sizing.
    """
    if not isinstance(mapping, dict):
        return None, None
    for key in keys:
        if key not in mapping:
            continue
        value = _finite_float_or_none(mapping.get(key))
        if value is not None:
            return key, value
    return None, None


def _step_aligned(value: float, step: float, *, tolerance: float = 1e-9) -> bool:
    """Проверяет кратность шагу без ложных ошибок на двоичной арифметике float."""
    num = _finite_float_or_none(value)
    tick = _finite_float_or_none(step)
    if num is None or tick is None or tick <= 0:
        return False
    units = num / tick
    nearest = round(units)
    return abs(units - nearest) <= max(float(tolerance), abs(units) * float(tolerance))


def _grid_min_notional_price(reference_price: Any, lower: Any, upper: Any) -> float | None:
    """Conservative price for Bybit minNotional checks across a grid range.

    Bybit validates notional at the actual order price. A fixed base qty that
    passes ``qty * reference_price`` can still fail for buy levels near the lower
    grid boundary. Use the smallest positive executable range/reference price so
    a recommendation is not approved with lower-grid orders below minNotional.
    """
    candidates: list[float] = []
    for raw in (lower, reference_price, upper):
        num = _finite_float_or_none(raw)
        if num is not None and num > 0:
            candidates.append(float(num))
    if not candidates:
        return None
    return min(candidates)


def _grid_max_notional_price(reference_price: Any, lower: Any, upper: Any) -> float | None:
    """Worst executable price for exposure/margin caps across a fixed-qty grid.

    Minimum-notional checks must use the lowest positive grid price, but risk caps
    need the opposite side: Bybit linear order value is qty * order price.  A
    range whose upper boundary is above reference can breach max notional/margin
    even when ``qty * reference_price * grid_count`` looks safe.
    """
    candidates: list[float] = []
    for raw in (lower, reference_price, upper):
        num = _finite_float_or_none(raw)
        if num is not None and num > 0:
            candidates.append(float(num))
    if not candidates:
        return None
    return max(candidates)


def _is_exact_linear_usdt_symbol(symbol: str | None) -> bool:
    normalized = str(symbol or "").strip().upper()
    if not normalized.endswith("USDT"):
        return False
    base = normalized[:-4]
    return bool(base) and normalized.isalnum()


def _boolish_true(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _explicit_bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    return None


def _rec_params_and_plan(rec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    params = rec.get("params") if isinstance(rec, dict) else {}
    if not isinstance(params, dict):
        params = {}
    plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    if not isinstance(plan, dict):
        plan = {}
    return params, plan


def _first_mapping(*items: Any) -> dict[str, Any]:
    for item in items:
        if isinstance(item, dict):
            return item
    return {}


def _cost_model_from_rec(rec: dict[str, Any]) -> dict[str, Any]:
    params, plan = _rec_params_and_plan(rec)
    return _first_mapping(params.get("cost_model"), plan.get("cost_model"))


def _economics_from_rec(rec: dict[str, Any]) -> dict[str, Any]:
    params, plan = _rec_params_and_plan(rec)
    return _first_mapping(params.get("economics"), plan.get("economics"))


def _execution_label_horizon_sec(rec: dict[str, Any]) -> int:
    params, plan = _rec_params_and_plan(rec)
    cost_model = _cost_model_from_rec(rec)
    candidates = (
        cost_model.get("horizon_sec"),
        params.get("label_horizon_sec"),
        plan.get("label_horizon_sec"),
        plan.get("expected_horizon_sec"),
    )
    for raw in candidates:
        value = strict_integer(raw)
        if value is not None and value > 0:
            return int(min(max(value, 6 * 3600), 48 * 3600))
    for raw in (
        params.get("label_horizon_hours"),
        plan.get("label_horizon_hours"),
        _first_mapping(plan.get("expected_horizon")).get("label_horizon_hours"),
    ):
        value = strict_integer(raw)
        if value is not None and value > 0:
            return int(min(max(value * 3600, 6 * 3600), 48 * 3600))
    return 12 * 3600


def _signed_funding_bps_for_direction(direction: str, funding_rate: float) -> float | None:
    rate = _finite_float_or_none(funding_rate)
    if rate is None:
        return None
    direction_norm = str(direction or "neutral").strip().lower()
    bps = float(rate) * 10_000.0
    if direction_norm == "long":
        return bps
    if direction_norm == "short":
        return -bps
    if direction_norm == "neutral":
        # Neutral futures-grid can accumulate either leg. Treat non-zero funding as
        # adverse execution carry unless a direction-specific hedge model is added.
        return abs(bps)
    return None


def _funding_events_until_horizon(now_ts: int, next_funding_ts: Any, interval_sec: int, horizon_sec: int) -> int:
    now_int = strict_integer(now_ts)
    interval_int = strict_integer(interval_sec)
    horizon_int = strict_integer(horizon_sec)
    if (
        now_int is None
        or interval_int is None
        or horizon_int is None
        or now_int <= 0
        or interval_int <= 0
        or horizon_int <= 0
    ):
        return 0
    next_int = strict_integer(next_funding_ts)
    if next_int is None or next_int <= 0:
        # If Bybit/DB has the funding interval but not the next event timestamp,
        # execution preflight must not assume that only one event can occur. The
        # first event may be minutes away and a futures grid can carry inventory
        # across every boundary in the label horizon. Match recommendation-time
        # economics: use a conservative ceil(horizon / interval), capped only to
        # avoid absurd legacy horizons.
        return min(32, max(1, int(math.ceil(float(horizon_int) / float(interval_int)))))
    # Bybit and some fixtures can provide ms timestamps; normalize defensively.
    if next_int > 10_000_000_000:
        next_int //= 1000
    while next_int <= now_int:
        next_int += interval_int
    horizon_end = now_int + horizon_int
    events = 0
    ts = next_int
    while ts <= horizon_end and events < 32:
        events += 1
        ts += interval_int
    return events


def _execution_funding_blocks(conn, rec: dict[str, Any], *, now_ts: int | None = None) -> list[dict[str, Any]]:
    """Fail-closed funding guard for the operator execute path.

    Recommendation-time economics already estimate funding, but the operator can
    execute minutes later. If the latest funding row is missing/stale or current
    funding turns the net grid edge negative, execution must be blocked rather
    than materialising a bot from stale carry assumptions.
    """
    if str(rec.get("bot_type") or "").strip().lower() != "futures_grid":
        return []
    if str(rec.get("venue") or "").strip().lower() != "linear":
        return []
    symbol = str(rec.get("symbol") or "").strip().upper()
    if not symbol:
        return [{"code": "FUNDING_SYMBOL_MISSING", "msg": "Нельзя проверить funding без symbol; execution заблокирован fail-closed."}]

    params, plan = _rec_params_and_plan(rec)
    has_cost_model = any(isinstance(container.get("cost_model"), dict) for container in (params, plan))
    if not has_cost_model:
        # Legacy/API fixture records that predate the full cost model do not contain
        # recommendation-time funding assumptions to compare against. Full Bybit
        # grid recommendations produced by the recommender carry cost_model and are
        # checked fail-closed below.
        return []

    now = int(now_ts or time.time())
    funding = db.get_latest_funding_rate(conn, symbol)
    if not funding:
        return [{"code": "FUNDING_RATE_UNAVAILABLE_AT_EXECUTION", "msg": f"{symbol}: нет funding_rate для execution-time проверки carry; запуск grid заблокирован."}]

    ts = strict_integer(funding.get("ts"))
    ts = ts if ts is not None else 0
    age_sec = max(0, now - ts) if ts > 0 else None
    if age_sec is None or age_sec > EXECUTION_FUNDING_MAX_STALENESS_SEC:
        return [{
            "code": "STALE_FUNDING_RATE",
            "msg": f"{symbol}: funding_rate stale at execution: age_sec={age_sec if age_sec is not None else 'unknown'} > limit={EXECUTION_FUNDING_MAX_STALENESS_SEC}.",
        }]

    rate = _finite_float_or_none(funding.get("funding_rate"))
    signed_bps = _signed_funding_bps_for_direction(str(rec.get("direction") or "neutral"), rate if rate is not None else float("nan"))
    if signed_bps is None:
        return [{"code": "FUNDING_RATE_INVALID_AT_EXECUTION", "msg": f"{symbol}: funding_rate не является finite-числом; запуск grid заблокирован."}]

    interval_min = strict_integer(funding.get("funding_interval_min"))
    if interval_min is None or interval_min <= 0:
        return [{"code": "FUNDING_INTERVAL_UNAVAILABLE_AT_EXECUTION", "msg": f"{symbol}: funding_interval_min отсутствует; нельзя оценить carry до горизонта сделки."}]
    interval_sec = int(interval_min * 60)
    horizon_sec = _execution_label_horizon_sec(rec)
    events = _funding_events_until_horizon(now, funding.get("next_funding_ts"), interval_sec, horizon_sec)
    current_expected_bps = signed_bps * max(0, events)

    cost_model = _cost_model_from_rec(rec)
    economics = _economics_from_rec(rec)
    stored_expected_bps = _finite_float_or_none(cost_model.get("expected_funding_bps"))
    if stored_expected_bps is None:
        stored_expected_bps = 0.0
    net_profit_bps = _finite_float_or_none(economics.get("net_profit_bps"))

    current_adverse_cost_bps = max(0.0, float(current_expected_bps))
    stored_adverse_cost_bps = max(0.0, float(stored_expected_bps))
    worsened_bps = current_adverse_cost_bps - stored_adverse_cost_bps

    blocks: list[dict[str, Any]] = []
    if current_adverse_cost_bps >= EXECUTION_FUNDING_EXTREME_BPS:
        blocks.append({
            "code": "FUNDING_EXTREME_AT_EXECUTION",
            "msg": f"{symbol}: текущий funding carry {current_adverse_cost_bps:.2f} bps до горизонта {horizon_sec}s превышает лимит {EXECUTION_FUNDING_EXTREME_BPS:.2f} bps.",
        })
    if (
        net_profit_bps is not None
        and worsened_bps > EXECUTION_FUNDING_WORSE_DELTA_BLOCK_BPS
        and (float(net_profit_bps) - worsened_bps) <= 0.0
    ):
        blocks.append({
            "code": "FUNDING_EDGE_TURNED_NEGATIVE",
            "msg": f"{symbol}: funding ухудшился на {worsened_bps:.2f} bps; net edge {net_profit_bps:.2f} bps стал неположительным после актуального carry.",
        })
    return blocks


def _first_finite_from_mappings(mappings: list[Any], keys: tuple[str, ...]) -> tuple[str | None, float | None]:
    for mapping in mappings:
        key, value = _first_finite_from_mapping(mapping if isinstance(mapping, dict) else {}, keys)
        if value is not None:
            return key, value
    return None, None


def _execution_runtime_size_risk_blocks(rec: dict[str, Any], limits: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-check per-bot leverage/notional/margin caps at operator execution time.

    Recommendation-time risk gates are only a snapshot. Runtime limits may be
    tightened after publication, and auto-snap can increase qty/notional to satisfy
    Bybit minNotional/qtyStep. Execute-path must therefore validate the exact
    snapped payload that will be stored in the bot instance.
    """
    if str(rec.get("bot_type") or "").strip().lower() != "futures_grid":
        return []
    if str(rec.get("venue") or "").strip().lower() != "linear":
        return []

    effective_limits = normalize_risk_limits(limits, limits)
    params, plan = _rec_params_and_plan(rec)
    operator_sheet = params.get("operator_sheet") if isinstance(params.get("operator_sheet"), dict) else {}
    sizing_maps: list[Any] = [
        params.get("sizing"),
        plan.get("sizing"),
        operator_sheet.get("sizing"),
        params.get("economics"),
        plan.get("economics"),
        operator_sheet.get("economics"),
        params.get("risk_report"),
        plan.get("risk_report"),
        operator_sheet.get("risk_report"),
        params,
        plan,
        operator_sheet,
    ]

    blocks: list[dict[str, Any]] = []
    leverage = _finite_float_or_none(params.get("leverage"))
    if leverage is None:
        leverage = _finite_float_or_none(plan.get("leverage"))
    if leverage is None:
        leverage = _finite_float_or_none(operator_sheet.get("leverage"))

    if leverage is None:
        blocks.append({
            "code": "LEVERAGE_MISSING_AT_EXECUTION",
            "msg": "execution payload не содержит явное leverage; нельзя проверить runtime leverage caps, margin и cross-margin stress semantics.",
        })

    min_leverage = _finite_float_or_none(effective_limits.get("min_leverage"))
    enforce_operator_min_leverage = _operator_payload_has_runtime_risk_context(rec)
    if (
        enforce_operator_min_leverage
        and leverage is not None
        and min_leverage is not None
        and min_leverage > 0
        and leverage < min_leverage
    ):
        blocks.append({
            "code": "MIN_LEVERAGE_PER_BOT_AT_EXECUTION",
            "msg": f"execution payload leverage={leverage:.8g}x ниже текущего runtime min_leverage={min_leverage:.8g}x.",
        })

    max_leverage = _finite_float_or_none(effective_limits.get("max_leverage"))
    if leverage is not None and max_leverage is not None and max_leverage > 0 and leverage > max_leverage:
        blocks.append({
            "code": "MAX_LEVERAGE_PER_BOT_AT_EXECUTION",
            "msg": f"execution payload leverage={leverage:.8g}x выше текущего runtime max_leverage={max_leverage:.8g}x.",
        })

    notional_key, estimated_notional = _first_finite_from_mappings(
        sizing_maps,
        (
            "estimated_worst_case_total_order_notional_usdt",
            "estimated_max_position_notional_usdt",
            "max_position_notional_usdt",
            "estimated_total_order_notional_usdt",
            "total_order_notional_usdt",
            "position_notional_usdt",
            "notional_usdt",
        ),
    )
    margin_key, estimated_margin = _first_finite_from_mappings(
        sizing_maps,
        (
            "estimated_worst_case_margin_required_usdt",
            "estimated_margin_required_usdt",
            "margin_required_usdt",
            "capital_required_usdt",
            "margin_usdt",
            "investment_usdt",
        ),
    )

    ctx = _trade_plan_price_context(rec)
    worst_price = _grid_max_notional_price(ctx.get("reference_price"), ctx.get("range_lower"), ctx.get("range_upper"))
    _, order_qty_for_worst = _first_finite_from_mappings(
        sizing_maps,
        (
            "order_qty",
            "qty_per_order",
            "qty_per_leg",
            "base_qty_per_order",
            "order_size_qty",
            "leg_qty",
        ),
    )
    grid_count_resolution = ctx.get("grid_count_resolution") if isinstance(ctx.get("grid_count_resolution"), dict) else {}
    grid_count_for_worst = grid_count_resolution.get("conservative_max")
    if grid_count_for_worst is None:
        grid_count_for_worst = 1
    commitment_for_worst = arithmetic_grid_commitment(
        lower=ctx.get("range_lower"),
        upper=ctx.get("range_upper"),
        grid_count=grid_count_for_worst,
        reference_price=ctx.get("reference_price"),
        direction=str(rec.get("direction") or ""),
    )
    max_position_slots_for_worst = (
        int(commitment_for_worst["max_abs_position_slots"])
        if commitment_for_worst is not None
        else max(1, int(grid_count_for_worst) + 1)
    )
    derived_worst_notional = None
    notional_understatement_block: dict[str, Any] | None = None
    if order_qty_for_worst is not None and worst_price is not None and worst_price > 0:
        derived_worst_notional = float(order_qty_for_worst) * float(worst_price) * max_position_slots_for_worst
        if estimated_notional is None:
            estimated_notional = float(derived_worst_notional)
            notional_key = "order_qty*max_grid_price*max_position_slots"
        elif derived_worst_notional > float(estimated_notional) * 1.005:
            notional_understatement_block = {
                "code": "POSITION_NOTIONAL_UNDERSTATED_BY_GRID_PRICE",
                "msg": (
                    f"execution {notional_key or 'position_notional'}={estimated_notional:.8g} USDT is below "
                    f"worst-case grid notional qty*max(range/reference price)*committed_slots={derived_worst_notional:.8g} USDT; "
                    "runtime risk caps must use the upper executable price for fixed-qty linear futures grids."
                ),
            }
            estimated_notional = float(derived_worst_notional)
            notional_key = "order_qty*max_grid_price*max_position_slots"

    if estimated_margin is None and estimated_notional is not None and leverage is not None and leverage > 0:
        estimated_margin = float(estimated_notional) / max(1.0, float(leverage))
        margin_key = f"{notional_key or 'estimated_notional'}/leverage"
    if estimated_notional is None and estimated_margin is not None and leverage is not None and leverage > 0:
        estimated_notional = float(estimated_margin) * max(1.0, float(leverage))
        notional_key = "estimated_margin_required_usdt*leverage"

    max_notional = _finite_float_or_none(effective_limits.get("max_position_notional_usdt"))
    max_margin = _finite_float_or_none(effective_limits.get("max_margin_per_bot_usdt"))
    size_context_keys = {
        "estimated_worst_case_total_order_notional_usdt",
        "estimated_worst_case_order_notional_usdt",
        "estimated_max_position_notional_usdt",
        "max_position_notional_usdt",
        "estimated_total_order_notional_usdt",
        "total_order_notional_usdt",
        "position_notional_usdt",
        "notional_usdt",
        "estimated_worst_case_margin_required_usdt",
        "estimated_margin_required_usdt",
        "margin_required_usdt",
        "capital_required_usdt",
        "margin_usdt",
        "investment_usdt",
        "order_qty",
        "qty",
        "qty_per_order",
        "qty_per_leg",
        "order_notional_usdt",
        "order_notional",
        "notional_per_order",
    }
    size_context_present = any(
        isinstance(mapping, dict) and any(key in mapping for key in size_context_keys)
        for mapping in sizing_maps
    )
    size_caps_active = (max_notional is not None and max_notional > 0) or (max_margin is not None and max_margin > 0)
    if estimated_notional is None and estimated_margin is None and size_context_present and size_caps_active:
        blocks.append({
            "code": "POSITION_SIZE_MISSING_AT_EXECUTION",
            "msg": "execution payload contains sizing/economics context but no estimated notional/margin; runtime max_position_notional_usdt и max_margin_per_bot_usdt cannot be verified fail-closed.",
        })

    if max_notional is not None and max_notional > 0 and estimated_notional is not None and estimated_notional > max_notional:
        if notional_understatement_block is not None:
            blocks.append(notional_understatement_block)
        blocks.append({
            "code": "MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION",
            "msg": f"execution {notional_key or 'position_notional'}={estimated_notional:.8g} USDT выше текущего runtime cap={max_notional:.8g} USDT.",
        })

    if max_margin is not None and max_margin > 0 and estimated_margin is not None and estimated_margin > max_margin:
        blocks.append({
            "code": "MAX_MARGIN_PER_BOT_AT_EXECUTION",
            "msg": f"execution {margin_key or 'margin_required'}={estimated_margin:.8g} USDT выше текущего runtime cap={max_margin:.8g} USDT.",
        })

    return blocks


def _execution_daily_loss_budget_guard(
    rec: dict[str, Any],
    limits: dict[str, Any],
    risk_status: Any,
) -> dict[str, Any]:
    """Estimate kill-switch loss against the remaining realised daily-DD budget.

    Existing risk gates stopped new bots only *after* realised daily drawdown had
    reached the limit.  A single grid whose conservative loss to kill-switch was
    larger than the remaining budget could therefore be launched legally and make
    the configured daily cap impossible to respect.

    This is deliberately a conservative preflight estimate, not an exchange fill
    simulation.  It uses the greatest persisted/derived position notional, the
    adverse reference-to-kill distance and explicit execution friction.
    """
    result: dict[str, Any] = {
        "blocks": [],
        "max_daily_dd_usdt": None,
        "daily_dd_usdt": None,
        "remaining_daily_loss_budget_usdt": None,
        "estimated_kill_switch_loss_usdt": None,
        "estimated_position_notional_usdt": None,
        "adverse_distance_pct": None,
        "execution_cost_bps": None,
    }
    if str(rec.get("bot_type") or "").strip().lower() != "futures_grid":
        return result
    if str(rec.get("venue") or "").strip().lower() != "linear":
        return result

    effective_limits = normalize_risk_limits(limits, limits)
    max_daily_dd = _finite_float_or_none(effective_limits.get("max_daily_dd_usdt"))
    daily_dd = _finite_float_or_none(getattr(risk_status, "daily_dd", None))
    result["max_daily_dd_usdt"] = max_daily_dd
    result["daily_dd_usdt"] = daily_dd
    if max_daily_dd is None or max_daily_dd < 0.0 or daily_dd is None or daily_dd < 0.0:
        result["blocks"].append({
            "code": "DAILY_LOSS_BUDGET_UNAVAILABLE",
            "msg": "Текущий daily drawdown или max_daily_dd_usdt невалиден; остаток дневного loss budget нельзя проверить.",
        })
        return result

    remaining_budget = max(0.0, float(max_daily_dd) - float(daily_dd))
    result["remaining_daily_loss_budget_usdt"] = remaining_budget

    params, plan = _rec_params_and_plan(rec)
    operator_sheet = params.get("operator_sheet") if isinstance(params.get("operator_sheet"), dict) else {}
    sizing_maps: list[Any] = [
        params.get("sizing"),
        plan.get("sizing"),
        operator_sheet.get("sizing"),
        params.get("economics"),
        plan.get("economics"),
        operator_sheet.get("economics"),
        params.get("risk_report"),
        plan.get("risk_report"),
        operator_sheet.get("risk_report"),
        params,
        plan,
        operator_sheet,
    ]
    notional_key, estimated_notional = _first_finite_from_mappings(
        sizing_maps,
        (
            "estimated_worst_case_total_order_notional_usdt",
            "estimated_max_position_notional_usdt",
            "max_position_notional_usdt",
            "estimated_total_order_notional_usdt",
            "total_order_notional_usdt",
            "position_notional_usdt",
            "notional_usdt",
        ),
    )

    ctx = _trade_plan_price_context(rec)
    worst_price = _grid_max_notional_price(
        ctx.get("reference_price"),
        ctx.get("range_lower"),
        ctx.get("range_upper"),
    )
    _, order_qty = _first_finite_from_mappings(
        sizing_maps,
        (
            "order_qty",
            "qty_per_order",
            "qty_per_leg",
            "base_qty_per_order",
            "order_size_qty",
            "leg_qty",
        ),
    )
    count_resolution = ctx.get("grid_count_resolution") if isinstance(ctx.get("grid_count_resolution"), dict) else {}
    grid_count = count_resolution.get("conservative_max") or count_resolution.get("value") or 1
    reference = _finite_float_or_none(ctx.get("reference_price"))
    range_lower = _finite_float_or_none(ctx.get("range_lower"))
    range_upper = _finite_float_or_none(ctx.get("range_upper"))
    direction = normalize_execution_direction(rec.get("direction"))
    committed_slots = max(1, int(grid_count))
    if reference is not None and range_lower is not None and range_upper is not None and direction is not None:
        commitment = arithmetic_grid_commitment(
            lower=range_lower,
            upper=range_upper,
            grid_count=grid_count,
            reference_price=reference,
            direction=direction,
        )
        if commitment is not None:
            committed_slots = max(1, int(commitment["max_abs_position_slots"]))
    if order_qty is not None and worst_price is not None and worst_price > 0.0:
        derived_notional = float(order_qty) * float(worst_price) * float(committed_slots)
        if estimated_notional is None or derived_notional > float(estimated_notional):
            estimated_notional = derived_notional
            notional_key = "qty*max_grid_price*committed_slots"

    kill_lower = _finite_float_or_none(ctx.get("kill_switch_lower"))
    kill_upper = _finite_float_or_none(ctx.get("kill_switch_upper"))

    missing: list[str] = []
    if estimated_notional is None or estimated_notional <= 0.0:
        missing.append("position_notional")
    if reference is None or reference <= 0.0:
        missing.append("reference_price")
    if direction in {"long", "neutral"} and (kill_lower is None or kill_lower <= 0.0):
        missing.append("kill_switch_lower")
    if direction in {"short", "neutral"} and (kill_upper is None or kill_upper <= 0.0):
        missing.append("kill_switch_upper")
    if direction not in {"long", "short", "neutral"}:
        missing.append("direction")

    if missing:
        if _operator_payload_has_runtime_risk_context(rec):
            result["blocks"].append({
                "code": "KILL_SWITCH_LOSS_UNVERIFIABLE",
                "msg": "Нельзя оценить потенциальный loss до kill-switch: отсутствуют/невалидны " + ", ".join(sorted(set(missing))) + ".",
                "missing_fields": sorted(set(missing)),
            })
        return result

    assert estimated_notional is not None and reference is not None
    downside = max(0.0, (float(reference) - float(kill_lower or reference)) / float(reference))
    upside = max(0.0, (float(kill_upper or reference) - float(reference)) / float(reference))
    if direction == "long":
        adverse_fraction = downside
    elif direction == "short":
        adverse_fraction = upside
    else:
        adverse_fraction = max(downside, upside)

    cost_model = _cost_model_from_rec(rec)
    economics = _economics_from_rec(rec)
    _, execution_cost_bps = _first_finite_from_mappings(
        [cost_model, economics],
        ("execution_cost_bps", "total_cost_bps", "net_cost_bps", "fee_bps_round_trip"),
    )
    execution_cost_bps = max(0.0, float(execution_cost_bps or 0.0))
    estimated_loss = float(estimated_notional) * (float(adverse_fraction) + execution_cost_bps / 10_000.0)

    result.update({
        "estimated_position_notional_usdt": float(estimated_notional),
        "estimated_position_notional_source": notional_key,
        "adverse_distance_pct": float(adverse_fraction) * 100.0,
        "execution_cost_bps": execution_cost_bps,
        "estimated_kill_switch_loss_usdt": estimated_loss,
    })
    tolerance = max(1e-9, remaining_budget * 1e-9)
    if estimated_loss > remaining_budget + tolerance:
        result["blocks"].append({
            "code": "DAILY_LOSS_BUDGET_EXCEEDED",
            "msg": (
                f"Консервативный loss до kill-switch={estimated_loss:.2f} USDT превышает остаток "
                f"дневного max-DD budget={remaining_budget:.2f} USDT "
                f"(daily_dd={daily_dd:.2f}, limit={max_daily_dd:.2f}); запуск запрещён."
            ),
            "estimated_kill_switch_loss_usdt": estimated_loss,
            "remaining_daily_loss_budget_usdt": remaining_budget,
            "daily_dd_usdt": float(daily_dd),
            "max_daily_dd_usdt": float(max_daily_dd),
            "estimated_position_notional_usdt": float(estimated_notional),
            "adverse_distance_pct": float(adverse_fraction) * 100.0,
            "execution_cost_bps": execution_cost_bps,
        })
    return result


def _active_symbol_disable_state(conn, venue: str, symbol: str, *, now_ts: int | None = None) -> dict[str, Any] | None:
    now = int(now_ts or time.time())
    cur = conn.execute(
        """SELECT ts, details_json FROM decision_log
           WHERE action='SYMBOL_DISABLED' AND ts >= ?
           ORDER BY ts DESC""",
        (max(0, now - 86400),),
    )
    for row in cur.fetchall():
        details = _json_loads_mapping_or_default(row["details_json"], {})
        if str(details.get("venue") or "") != str(venue):
            continue
        if str(details.get("symbol") or "") != str(symbol):
            continue
        retry_at = None
        try:
            if details.get("retry_at") not in (None, ""):
                retry_at = int(details.get("retry_at"))
        except Exception:
            retry_at = None
        if retry_at is None:
            try:
                retry_after_sec = int(details.get("retry_after_sec") or 0)
            except Exception:
                retry_after_sec = 0
            retry_at = int(row["ts"] or 0) + max(retry_after_sec, 86400 if retry_after_sec <= 0 else retry_after_sec)
        if int(retry_at or 0) > now:
            return {
                "retry_at": int(retry_at),
                "details": details,
                "logged_ts": int(row["ts"] or 0),
            }
    return None


def _execution_market_data_blocks(conn, rec: dict[str, Any], *, now_ts: int | None = None) -> list[dict[str, Any]]:
    now = int(now_ts or time.time())
    venue = str(rec.get("venue") or "")
    symbol = str(rec.get("symbol") or "")
    stale_sec = max(60, int(getattr(settings, "stale_data_max_sec", 0) or 0))

    blocks: list[dict[str, Any]] = []
    last_candle_ts = db.get_latest_ohlcv_ts(conn, venue, symbol, 60)
    last_ticker_ts = db.get_latest_ticker_ts(conn, venue, symbol)
    candle_age_sec = None if last_candle_ts is None else max(0, now - int(last_candle_ts))
    ticker_age_sec = None if last_ticker_ts is None else max(0, now - int(last_ticker_ts))

    if last_candle_ts is None:
        blocks.append({"code": "MISSING_CANDLE_DATA", "msg": f"{venue}:{symbol} — отсутствуют 1m candles для execution-time preflight"})
    elif candle_age_sec is not None and candle_age_sec > stale_sec:
        blocks.append({"code": "STALE_CANDLE_DATA", "msg": f"{venue}:{symbol} — 1m candles stale: age_sec={candle_age_sec} > limit={stale_sec}"})

    if last_ticker_ts is None:
        blocks.append({"code": "MISSING_TICKER_DATA", "msg": f"{venue}:{symbol} — отсутствует свежий ticker для execution-time preflight"})
    elif ticker_age_sec is not None and ticker_age_sec > stale_sec:
        blocks.append({"code": "STALE_TICKER_DATA", "msg": f"{venue}:{symbol} — ticker stale: age_sec={ticker_age_sec} > limit={stale_sec}"})

    disabled = _active_symbol_disable_state(conn, venue, symbol, now_ts=now)
    if disabled is not None:
        blocks.append({
            "code": "SYMBOL_DISABLED",
            "msg": f"{venue}:{symbol} временно отключён после upstream-ошибок до ts={int(disabled['retry_at'])}",
        })

    return blocks



def _grid_count_resolution_for_reco(rec: dict[str, Any]) -> dict[str, Any]:
    """Resolve every persisted grid-count alias with strict integer semantics."""
    params = rec.get("params") if isinstance(rec, dict) else {}
    if not isinstance(params, dict):
        params = {}
    plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    operator_sheet = params.get("operator_sheet") if isinstance(params.get("operator_sheet"), dict) else {}
    params_sizing = params.get("sizing") if isinstance(params.get("sizing"), dict) else {}
    params_economics = params.get("economics") if isinstance(params.get("economics"), dict) else {}
    plan_sizing = plan.get("sizing") if isinstance(plan.get("sizing"), dict) else {}
    plan_economics = plan.get("economics") if isinstance(plan.get("economics"), dict) else {}
    operator_sizing = operator_sheet.get("sizing") if isinstance(operator_sheet.get("sizing"), dict) else {}
    operator_economics = operator_sheet.get("economics") if isinstance(operator_sheet.get("economics"), dict) else {}
    return resolve_integer_aliases([
        ("params.grid_count", params.get("grid_count")),
        ("params.trade_plan.grid_count", plan.get("grid_count")),
        ("params.grid_levels", params.get("grid_levels")),
        ("params.operator_sheet.grid_count", operator_sheet.get("grid_count")),
        ("params.operator_sheet.grid_levels", operator_sheet.get("grid_levels")),
        ("params.sizing.grid_count", params_sizing.get("grid_count")),
        ("params.economics.grid_count", params_economics.get("grid_count")),
        ("params.trade_plan.sizing.grid_count", plan_sizing.get("grid_count")),
        ("params.trade_plan.economics.grid_count", plan_economics.get("grid_count")),
        ("params.operator_sheet.sizing.grid_count", operator_sizing.get("grid_count")),
        ("params.operator_sheet.economics.grid_count", operator_economics.get("grid_count")),
    ])


def _trade_plan_price_context(rec: dict[str, Any]) -> dict[str, Any]:
    """Достаёт ценовой контекст trade_plan в едином виде для preflight/UI.

    В execution-time проверках нельзя повторять парсинг JSON руками в нескольких
    местах: любое расхождение между Bybit-валидацией и live-price guard создаёт
    окно, где один слой считает сетку допустимой, а другой уже не видит её
    границы. Helper намеренно возвращает только finite-числа или None.

    ``params.operator_sheet`` остаётся fallback-источником только для чтения
    legacy/operator display context. Отсутствующий полный ``trade_plan`` всё равно
    блокируется strict execution-preflight через ``TRADE_PLAN_MISSING``.
    """
    params = rec.get("params") if isinstance(rec, dict) else {}
    if not isinstance(params, dict):
        params = {}
    plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    levels = plan.get("levels") if isinstance(plan.get("levels"), dict) else {}
    range_levels = levels.get("range") if isinstance(levels.get("range"), dict) else {}
    kill_switch = levels.get("kill_switch") if isinstance(levels.get("kill_switch"), dict) else {}
    grid_step = levels.get("grid_step") if isinstance(levels.get("grid_step"), dict) else {}
    tp_per_leg = levels.get("tp_per_leg") if isinstance(levels.get("tp_per_leg"), dict) else {}
    operator_sheet = params.get("operator_sheet") if isinstance(params.get("operator_sheet"), dict) else {}
    operator_kill_switch = operator_sheet.get("kill_switch") if isinstance(operator_sheet.get("kill_switch"), dict) else {}
    operator_tp_per_leg = operator_sheet.get("tp_per_leg") if isinstance(operator_sheet.get("tp_per_leg"), dict) else {}
    grid_count_resolution = _grid_count_resolution_for_reco(rec)
    resolved_grid_count = (
        grid_count_resolution.get("value")
        if grid_count_resolution.get("ok")
        else grid_count_resolution.get("conservative_max")
    )

    return {
        "params": params,
        "plan": plan,
        "levels": levels,
        "range": range_levels,
        "kill_switch": kill_switch,
        "grid_step": grid_step,
        "tp_per_leg": tp_per_leg,
        "operator_sheet": operator_sheet,
        "reference_price": _first_finite_from_mappings([plan, params, operator_sheet], ("reference_price", "price_ref"))[1],
        "range_lower": _first_finite_from_mappings([range_levels, params, operator_sheet], ("lower", "price_range_lower", "range_lower"))[1],
        "range_upper": _first_finite_from_mappings([range_levels, params, operator_sheet], ("upper", "price_range_upper", "range_upper"))[1],
        "kill_switch_lower": _first_finite_from_mappings([kill_switch, operator_kill_switch], ("lower", "kill_switch_lower"))[1],
        "kill_switch_upper": _first_finite_from_mappings([kill_switch, operator_kill_switch], ("upper", "kill_switch_upper"))[1],
        "grid_step_abs": _first_finite_from_mappings([grid_step, operator_sheet], ("step_abs", "grid_step_abs", "actual_grid_step_abs"))[1],
        "grid_type": str(params.get("grid_type") or plan.get("grid_type") or operator_sheet.get("grid_type") or "").strip().lower(),
        "grid_levels": resolved_grid_count,
        "grid_count_resolution": grid_count_resolution,
        "tp_per_leg_abs": _first_finite_from_mappings([tp_per_leg, operator_tp_per_leg, operator_sheet], ("abs", "tp_per_leg_abs"))[1],
        "tp_per_leg_pct": _first_finite_from_mappings([tp_per_leg, operator_tp_per_leg, operator_sheet], ("pct", "tp_per_leg_pct"))[1],
    }


def _current_price_from_ticker(ticker: dict[str, Any] | None) -> float | None:
    """Возвращает консервативный live-price для проверки актуальности сетки."""
    if not isinstance(ticker, dict):
        return None
    last = _finite_float_or_none(ticker.get("last"))
    bid = _finite_float_or_none(ticker.get("bid"))
    ask = _finite_float_or_none(ticker.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    if last is not None and last > 0:
        return last
    return None


def _live_spread_bps_from_ticker(ticker: dict[str, Any] | None) -> float | None:
    """Return executable best-bid/ask spread; lastPrice is not a spread proxy."""
    if not isinstance(ticker, dict):
        return None
    bid = _finite_float_or_none(ticker.get("bid"))
    ask = _finite_float_or_none(ticker.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0:
        return None
    return (ask - bid) / midpoint * 10_000.0


def _execution_live_cost_blocks(ticker: dict[str, Any] | None, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Reprice generated futures-grid execution friction from the live bid/ask.

    Recommendation economics are a publication-time snapshot. Preserve their
    gross edge and adverse funding assumption, but replace spread/slippage with
    the executable live quote and keep the greater of stored/configured fee
    floors. This prevents a still-fresh recommendation from being materialised
    after transaction costs have consumed its edge.
    """
    if str(rec.get("bot_type") or "").strip().lower() != "futures_grid":
        return []
    if str(rec.get("venue") or "").strip().lower() != "linear":
        return []

    cost_model = _cost_model_from_rec(rec)
    if not cost_model:
        # Legacy/manual payloads predate publication-time cost modelling. Their
        # existing strict plan checks remain unchanged; generated recommendations
        # always carry cost_model and are revalidated below.
        return []

    live_spread_bps = _live_spread_bps_from_ticker(ticker)
    if live_spread_bps is None:
        return [{
            "code": "LIVE_SPREAD_UNAVAILABLE",
            "msg": "Текущий ticker не содержит валидную пару bid/ask; spread и live execution edge нельзя проверить, запуск grid запрещён.",
        }]

    blocks: list[dict[str, Any]] = []
    if live_spread_bps > EXECUTION_MAX_LIVE_SPREAD_BPS:
        blocks.append({
            "code": "LIVE_SPREAD_TOO_WIDE",
            "msg": (
                f"spread_bps={live_spread_bps:.2f} > {EXECUTION_MAX_LIVE_SPREAD_BPS:.2f} по текущему bid/ask; "
                "transaction costs слишком велики для запуска grid."
            ),
        })

    economics = _economics_from_rec(rec)
    gross_profit_bps = _finite_float_or_none(economics.get("gross_profit_bps"))
    if gross_profit_bps is None:
        # The strict trade-plan validator owns malformed/legacy economics. The
        # spread cap above is still enforceable without inventing a gross edge.
        return blocks

    stored_fee_bps = _finite_float_or_none(cost_model.get("fee_bps_round_trip"))
    configured_taker_fee_bps = _finite_float_or_none(getattr(settings, "taker_fee_bps_linear", None))
    configured_fee_bps = None
    if configured_taker_fee_bps is not None and configured_taker_fee_bps >= 0:
        configured_fee_bps = configured_taker_fee_bps * 2.0
    fee_candidates = [
        value
        for value in (stored_fee_bps, configured_fee_bps)
        if value is not None and value >= 0
    ]
    fee_floor_bps = max(fee_candidates, default=0.0)

    live_slippage_bps = max(
        EXECUTION_MIN_LINEAR_SLIPPAGE_BPS,
        live_spread_bps * EXECUTION_GRID_SLIPPAGE_SPREAD_MULTIPLIER,
    )
    live_market_round_trip_cost_bps = fee_floor_bps + live_spread_bps + live_slippage_bps
    stored_grid_fee_bps = _finite_float_or_none(economics.get("grid_round_trip_fee_bps"))
    if stored_grid_fee_bps is None:
        stored_grid_fee_bps = _finite_float_or_none(cost_model.get("grid_round_trip_fee_bps"))
    if stored_grid_fee_bps is None:
        stored_grid_fee_bps = _finite_float_or_none(cost_model.get("fee_bps_round_trip"))
    live_grid_round_trip_fee_bps = max(fee_floor_bps, stored_grid_fee_bps or 0.0)

    # The live bid/ask spread remains a liquidity/launch gate and market friction
    # diagnostic. It is not subtracted from every completed resting grid pair.
    # Funding is validated by the dedicated inventory/schedule guard and belongs
    # to total P&L, not the per-pair Bybit Grid Profit formula.
    live_net_profit_bps = gross_profit_bps - live_grid_round_trip_fee_bps
    edge_msg = (
        f"gross_profit_bps={gross_profit_bps:.2f}, grid_round_trip_fee_bps="
        f"{live_grid_round_trip_fee_bps:.2f}, net_grid_profit_bps={live_net_profit_bps:.2f}; "
        f"live_market_round_trip_cost_bps={live_market_round_trip_cost_bps:.2f} "
        "учитывается отдельно как launch/terminal friction."
    )
    if live_net_profit_bps <= 0.0:
        blocks.append({
            "code": "LIVE_EXECUTION_EDGE_NON_POSITIVE",
            "msg": edge_msg + " Запуск grid запрещён: live edge неположительный.",
        })
    elif live_net_profit_bps < EXECUTION_MIN_NET_PROFIT_BPS:
        blocks.append({
            "code": "LIVE_EXECUTION_EDGE_TOO_THIN",
            "msg": (
                edge_msg
                + f" Минимум для запуска — {EXECUTION_MIN_NET_PROFIT_BPS:.2f} bps; нужен новый расчёт."
            ),
        })

    if gross_profit_bps <= live_grid_round_trip_fee_bps * EXECUTION_GROSS_COST_COVERAGE_MULTIPLIER:
        blocks.append({
            "code": "LIVE_GROSS_EDGE_BELOW_COSTS",
            "msg": (
                f"gross_profit_bps={gross_profit_bps:.2f} не покрывает live grid_round_trip_fee_bps="
                f"{live_grid_round_trip_fee_bps:.2f} с запасом {EXECUTION_GROSS_COST_COVERAGE_MULTIPLIER:.2f}x; "
                "запуск fail-closed."
            ),
        })
    return blocks


def _execution_live_price_blocks(conn, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Fail-closed защита от исполнения сетки по уехавшей цене/стоимости.

    Fresh ticker сам по себе не гарантирует исполнимость рекомендации: цена могла
    выйти за рекомендованный диапазон между публикацией и действием оператора.
    Для grid-бота старт вне диапазона — это уже другая сделка с иным профилем
    риска, поэтому execute-path блокируется до повторного пересчёта рекомендации.
    """
    ctx = _trade_plan_price_context(rec)
    lower = ctx["range_lower"]
    upper = ctx["range_upper"]
    lower_ks = ctx["kill_switch_lower"]
    upper_ks = ctx["kill_switch_upper"]
    reference = ctx["reference_price"]
    plan = ctx["plan"]
    if not isinstance(plan, dict) or not plan:
        return []

    ticker = db.get_latest_ticker(conn, str(rec.get("venue") or ""), str(rec.get("symbol") or ""))
    current_price = _current_price_from_ticker(ticker)

    blocks: list[dict[str, Any]] = []
    if current_price is None:
        # Freshness alone is not enough: a ticker row can be fresh but unusable
        # (for example all price fields are NULL after sanitisation of a broken
        # upstream payload). In that case execution must fail closed because the
        # grid range, kill-switch and reference-price drift cannot be checked.
        blocks.append({
            "code": "LIVE_PRICE_UNAVAILABLE",
            "msg": "Текущий ticker свежий, но не содержит пригодной last/bid/ask цены; запуск grid запрещён до получения валидной live price.",
        })
        return blocks

    blocks.extend(_execution_live_cost_blocks(ticker, rec))

    if lower_ks is not None and upper_ks is not None and not (lower_ks <= current_price <= upper_ks):
        blocks.append({
            "code": "CURRENT_PRICE_OUTSIDE_KILL_SWITCH",
            "msg": f"Текущая цена {current_price:.12g} находится вне kill_switch [{lower_ks}, {upper_ks}]; запуск сетки запрещён до пересчёта рекомендации.",
        })
    if lower is not None and upper is not None and not (lower <= current_price <= upper):
        blocks.append({
            "code": "CURRENT_PRICE_OUTSIDE_GRID_RANGE",
            "msg": f"Текущая цена {current_price:.12g} находится вне рекомендованного диапазона [{lower}, {upper}]; нужен новый расчёт уровней.",
        })
    if reference is not None and reference > 0 and lower is not None and upper is not None:
        span_pct = abs(upper - lower) / reference * 100.0 if reference else 0.0
        drift_pct = abs(current_price - reference) / reference * 100.0
        # Даже внутри диапазона слишком большой drift от цены расчёта означает,
        # что spacing/funding/TP уже относятся к другому рыночному состоянию.
        if span_pct > 0 and drift_pct > max(0.75 * span_pct, 3.0):
            blocks.append({
                "code": "REFERENCE_PRICE_DRIFT_TOO_LARGE",
                "msg": f"Текущая цена отклонилась от reference_price на {drift_pct:.2f}% при ширине диапазона {span_pct:.2f}%; нужен новый снимок рекомендации.",
            })
    return blocks

def _validate_trade_plan_against_bybit_meta(rec: dict[str, Any], meta: dict[str, Any], *, require_meta: bool = False, require_execution_plan: bool = False) -> dict[str, Any]:
    ctx = _trade_plan_price_context(rec)
    params = ctx["params"]
    plan = ctx["plan"]

    tick_size = _finite_float_or_none((meta or {}).get("tick_size"))
    min_price = _finite_float_or_none((meta or {}).get("min_price"))
    max_price = _finite_float_or_none((meta or {}).get("max_price"))
    qty_step = _finite_float_or_none((meta or {}).get("qty_step"))
    min_order_qty = _finite_float_or_none((meta or {}).get("min_order_qty"))
    max_order_qty = _finite_float_or_none((meta or {}).get("max_order_qty"))
    min_notional = _finite_float_or_none((meta or {}).get("min_notional"))
    min_leverage = _finite_float_or_none((meta or {}).get("min_leverage"))
    max_leverage = _finite_float_or_none((meta or {}).get("max_leverage"))
    leverage_step = _finite_float_or_none((meta or {}).get("leverage_step"))
    operator_sheet = params.get("operator_sheet") if isinstance(params.get("operator_sheet"), dict) else {}
    leverage = _first_finite_from_mapping(
        params,
        ("leverage",),
    )[1]
    if leverage is None:
        leverage = _first_finite_from_mapping(plan, ("leverage",))[1]
    if leverage is None:
        leverage = _first_finite_from_mapping(operator_sheet, ("leverage",))[1]

    bot_type = str(rec.get("bot_type") or "").strip()
    venue = str(rec.get("venue") or "").strip().lower()
    direction = str(rec.get("direction") or "").strip().lower()
    account_mode = str(rec.get("account_mode") or params.get("account_mode") or operator_sheet.get("account_mode") or "").strip().lower()
    margin_mode = str(rec.get("margin_mode") or params.get("margin_mode") or plan.get("margin_mode") or operator_sheet.get("margin_mode") or "").strip().lower()
    meta_category = str((meta or {}).get("category") or "").strip().lower()
    meta_symbol = str((meta or {}).get("symbol") or "").strip().upper()
    meta_status = str((meta or {}).get("status") or "").strip()
    meta_contract_type = str((meta or {}).get("contract_type") or "").strip()
    meta_quote_coin = str((meta or {}).get("quote_coin") or "").strip().upper()
    meta_settle_coin = str((meta or {}).get("settle_coin") or "").strip().upper()
    meta_delivery_time = _finite_float_or_none((meta or {}).get("delivery_time"))
    meta_is_pre_listing = (meta or {}).get("is_pre_listing")
    meta_unified_margin_trade = _explicit_bool_or_none((meta or {}).get("unified_margin_trade"))
    rec_symbol = str(rec.get("symbol") or "").strip().upper()

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    snapped: dict[str, str] = {}

    def _meta_target():
        return errors if require_meta else warnings

    if not meta:
        missing_meta_item = {
            "code": "BYBIT_META_UNAVAILABLE",
            "msg": "Не удалось получить metadata инструмента Bybit; точная проверка contractType/USDT settlement/tick/lot/min-notional недоступна.",
        }
        if require_meta:
            errors.append({
                "code": "BYBIT_META_UNAVAILABLE",
                "msg": "Не удалось получить metadata инструмента Bybit; запуск запрещён fail-closed, потому что нельзя подтвердить LinearPerpetual/USDT/tick/lot/min-notional constraints.",
            })
        else:
            warnings.append(missing_meta_item)
    raw_trade_plan = params.get("trade_plan") if isinstance(params, dict) else None
    raw_plan = raw_trade_plan if isinstance(raw_trade_plan, dict) else {}
    raw_levels = raw_plan.get("levels") if isinstance(raw_plan.get("levels"), dict) else {}
    raw_range = raw_levels.get("range") if isinstance(raw_levels.get("range"), dict) else {}
    raw_kill_switch = raw_levels.get("kill_switch") if isinstance(raw_levels.get("kill_switch"), dict) else {}
    raw_grid_step = raw_levels.get("grid_step") if isinstance(raw_levels.get("grid_step"), dict) else {}
    canonical_plan_values = (
        _finite_float_or_none(raw_plan.get("reference_price")),
        _finite_float_or_none(raw_range.get("lower")),
        _finite_float_or_none(raw_range.get("upper")),
        _finite_float_or_none(raw_kill_switch.get("lower")),
        _finite_float_or_none(raw_kill_switch.get("upper")),
        _finite_float_or_none(raw_grid_step.get("step_abs")),
    )
    # Legacy/operator aliases remain useful for read-only UI diagnostics, but they
    # must not turn an arbitrary or partial object into a complete execution plan.
    # A strict execution guard requires the canonical nested plan itself to carry
    # every price needed to prove range, kill-switch and grid-step geometry.
    plan_effectively_missing = (
        not isinstance(raw_trade_plan, dict)
        or any(value is None for value in canonical_plan_values)
    )
    if plan_effectively_missing:
        target = errors if require_execution_plan else warnings
        target.append({
            "code": "TRADE_PLAN_MISSING",
            "msg": "У рекомендации нет полного trade_plan; execution-time preflight не может проверить reference/range/kill-switch/grid-step и должен блокировать запуск fail-closed.",
        })

    # Эта система рекомендует только один продуктовый режим. Если рекомендация
    # в БД/legacy payload уже противоречит собственной доменной модели, её нельзя
    # считать исполнимой даже до похода в Bybit API.
    if bot_type != "futures_grid":
        errors.append({
            "code": "BOT_TYPE_UNSUPPORTED",
            "msg": f"Поддерживается только bot_type=futures_grid для Bybit Linear USDT Futures, получено bot_type={bot_type or 'unknown'}.",
        })
    if venue != "linear":
        errors.append({
            "code": "VENUE_UNSUPPORTED",
            "msg": f"Поддерживается только venue=linear / USDT perpetual, получено venue={venue or 'unknown'}.",
        })

    if bot_type == "futures_grid":
        if direction not in {"neutral", "long", "short"}:
            errors.append({"code": "FUTURES_DIRECTION_INVALID", "msg": f"futures_grid не поддерживает direction={direction or 'unknown'}."})
        if not account_mode:
            target = errors if require_execution_plan else warnings
            target.append({"code": "ACCOUNT_MODE_MISSING", "msg": "futures_grid требует явный account_mode=unified; execution-preflight не должен угадывать режим счёта."})
        elif account_mode == "one_way":
            warnings.append({"code": "ACCOUNT_MODE_LEGACY_ALIAS", "msg": "account_mode=one_way трактуется как legacy-алиас позиции/position-mode; штатное значение этой ревизии — account_mode=unified."})
        elif account_mode != "unified":
            errors.append({"code": "ACCOUNT_MODE_UNSUPPORTED", "msg": f"futures_grid поддерживает только account_mode=unified; получено {account_mode}. Неподдержанный режим блокируется fail-closed."})
        # Bybit Futures Grid Bot uses cross margin and one-way position mode.
        # An isolated payload would apply the wrong liquidation/risk semantics.
        if not margin_mode:
            errors.append({"code": "MARGIN_MODE_MISSING", "msg": "futures_grid требует явный margin_mode=cross; legacy/manual recommendation без режима исполнения блокируется fail-closed."})
        elif margin_mode != "cross":
            errors.append({"code": "MARGIN_MODE_UNSUPPORTED", "msg": f"Bybit futures_grid поддерживается только в margin_mode=cross, получено {margin_mode}."})
        if rec_symbol and not _is_exact_linear_usdt_symbol(rec_symbol):
            errors.append({"code": "USDT_PERPETUAL_SYMBOL_REQUIRED", "msg": f"futures_grid поддерживается только для точных alphanumeric USDT perpetual symbols без разделителей, получено symbol={rec_symbol}."})

    if bot_type == "futures_grid" and meta:
        if account_mode == "unified" and meta_unified_margin_trade is False:
            errors.append({
                "code": "BYBIT_UNIFIED_MARGIN_UNSUPPORTED",
                "msg": "Bybit metadata явно сообщает unifiedMarginTrade=false; инструмент несовместим с требуемым account_mode=unified.",
            })
        if meta_contract_type and meta_contract_type != "LinearPerpetual":
            errors.append({
                "code": "BYBIT_CONTRACT_TYPE_UNSUPPORTED",
                "msg": f"Bybit contractType={meta_contract_type}; проект поддерживает только LinearPerpetual USDT futures grid.",
            })
        elif not meta_contract_type:
            _meta_target().append({
                "code": "BYBIT_CONTRACT_TYPE_MISSING",
                "msg": "Bybit metadata не содержит contractType; невозможно подтвердить LinearPerpetual. Execution-preflight блокирует запуск fail-closed.",
            })
        if meta_delivery_time is not None and meta_delivery_time > 0:
            errors.append({
                "code": "BYBIT_DELIVERY_TIME_NOT_PERPETUAL",
                "msg": f"Bybit deliveryTime={meta_delivery_time:.0f}; проект поддерживает только perpetual-контракты без даты поставки.",
            })
        if meta_quote_coin and meta_quote_coin != "USDT":
            errors.append({
                "code": "BYBIT_QUOTE_COIN_UNSUPPORTED",
                "msg": f"Bybit quoteCoin={meta_quote_coin}; проект поддерживает только USDT-quoted linear perpetual.",
            })
        elif not meta_quote_coin:
            _meta_target().append({"code": "BYBIT_QUOTE_COIN_MISSING", "msg": "Bybit metadata не содержит quoteCoin; невозможно подтвердить USDT quote. Execution-preflight блокирует запуск fail-closed."})
        if meta_settle_coin and meta_settle_coin != "USDT":
            errors.append({
                "code": "BYBIT_SETTLE_COIN_UNSUPPORTED",
                "msg": f"Bybit settleCoin={meta_settle_coin}; проект поддерживает только USDT-settled linear perpetual.",
            })
        elif not meta_settle_coin:
            _meta_target().append({"code": "BYBIT_SETTLE_COIN_MISSING", "msg": "Bybit metadata не содержит settleCoin; невозможно подтвердить USDT settlement. Execution-preflight блокирует запуск fail-closed."})

        required_filter_fields = (
            ("BYBIT_TICK_SIZE_MISSING", tick_size, "priceFilter.tickSize", "цены и шаг сетки нельзя безопасно округлить по биржевому tick size"),
            ("BYBIT_QTY_STEP_MISSING", qty_step, "lotSizeFilter.qtyStep", "размер ордера нельзя безопасно округлить по qty step"),
            ("BYBIT_MIN_ORDER_QTY_MISSING", min_order_qty, "lotSizeFilter.minOrderQty", "невозможно проверить минимальный размер заявки"),
            ("BYBIT_MAX_ORDER_QTY_MISSING", max_order_qty, "lotSizeFilter.maxOrderQty", "невозможно проверить максимальный размер заявки"),
            ("BYBIT_MIN_NOTIONAL_MISSING", min_notional, "lotSizeFilter.minNotionalValue", "невозможно проверить минимальный notional в USDT"),
            ("BYBIT_MIN_LEVERAGE_MISSING", min_leverage, "leverageFilter.minLeverage", "невозможно проверить нижнюю границу leverage"),
            ("BYBIT_MAX_LEVERAGE_MISSING", max_leverage, "leverageFilter.maxLeverage", "невозможно проверить верхнюю границу leverage"),
            ("BYBIT_LEVERAGE_STEP_MISSING", leverage_step, "leverageFilter.leverageStep", "невозможно проверить шаг leverage"),
        )
        for code, value, source_field, consequence in required_filter_fields:
            if value is None or value <= 0:
                _meta_target().append({
                    "code": code,
                    "msg": f"Bybit metadata не содержит корректный {source_field}; {consequence}. Execution-preflight блокирует запуск fail-closed.",
                })

        pre_listing = _boolish_true(meta_is_pre_listing) or str(meta_status).strip().lower() in {"prelaunch", "pre-listing", "prelisting"}
        if pre_listing:
            errors.append({
                "code": "BYBIT_PRELISTING_UNSUPPORTED",
                "msg": "Pre-market/pre-listing контракты не поддерживаются для production futures grid recommendation.",
            })

    if meta and not meta_symbol:
        _meta_target().append({
            "code": "BYBIT_META_SYMBOL_MISSING",
            "msg": "Bybit metadata не содержит symbol; strict preflight не может доказать, что exchange filters относятся к той же торговой паре.",
        })
    elif meta_symbol and rec_symbol and meta_symbol != rec_symbol:
        errors.append({
            "code": "BYBIT_META_SYMBOL_MISMATCH",
            "msg": f"Metadata Bybit получена для symbol={meta_symbol}, тогда как recommendation ожидает symbol={rec_symbol}; применять такие ограничения опасно.",
        })

    if meta and not meta_category:
        _meta_target().append({
            "code": "BYBIT_META_CATEGORY_MISSING",
            "msg": "Bybit metadata не содержит category; нельзя подтвердить, что инструмент относится к linear USDT futures.",
        })
    elif meta_category and venue and meta_category != venue:
        errors.append({
            "code": "BYBIT_META_CATEGORY_MISMATCH",
            "msg": f"Metadata Bybit получена для category={meta_category}, тогда как recommendation ожидает venue={venue}; применять такие ограничения небезопасно.",
        })

    if meta and not meta_status:
        _meta_target().append({
            "code": "BYBIT_STATUS_MISSING",
            "msg": "Bybit metadata не содержит status; нельзя подтвердить, что контракт сейчас торгуется.",
        })
    elif meta and meta_status and meta_status.lower() != "trading":
        errors.append({
            "code": "BYBIT_INSTRUMENT_NOT_TRADING",
            "msg": f"Bybit instrument status={meta_status}; запускать новую grid-рекомендацию можно только для status=Trading.",
        })

    reference_price = ctx["reference_price"]
    lower = ctx["range_lower"]
    upper = ctx["range_upper"]
    lower_ks = ctx["kill_switch_lower"]
    upper_ks = ctx["kill_switch_upper"]
    step_abs = ctx["grid_step_abs"]
    grid_type = ctx["grid_type"]
    grid_levels = ctx["grid_levels"]
    grid_count_resolution = ctx.get("grid_count_resolution") if isinstance(ctx.get("grid_count_resolution"), dict) else {}
    tp_abs = ctx["tp_per_leg_abs"]
    tp_pct = ctx["tp_per_leg_pct"]
    grid_commitment = None
    if (
        bot_type == "futures_grid"
        and reference_price is not None
        and lower is not None
        and upper is not None
        and grid_levels is not None
    ):
        grid_commitment = arithmetic_grid_commitment(
            lower=lower,
            upper=upper,
            grid_count=grid_levels,
            reference_price=reference_price,
            direction=direction,
        )

    if require_execution_plan and bot_type == "futures_grid":
        required_plan_fields = (
            ("TRADE_PLAN_REFERENCE_PRICE_MISSING", reference_price, "trade_plan.reference_price"),
            ("TRADE_PLAN_RANGE_LOWER_MISSING", lower, "trade_plan.levels.range.lower"),
            ("TRADE_PLAN_RANGE_UPPER_MISSING", upper, "trade_plan.levels.range.upper"),
            ("TRADE_PLAN_KILL_SWITCH_LOWER_MISSING", lower_ks, "trade_plan.levels.kill_switch.lower"),
            ("TRADE_PLAN_KILL_SWITCH_UPPER_MISSING", upper_ks, "trade_plan.levels.kill_switch.upper"),
            ("TRADE_PLAN_GRID_STEP_MISSING", step_abs, "trade_plan.levels.grid_step.step_abs"),
        )
        for code, value, field in required_plan_fields:
            if value is None:
                errors.append({
                    "code": code,
                    "msg": f"{field} отсутствует или не является finite-числом; execution-time preflight не может доказать исполнимость Bybit Linear USDT futures grid.",
                })

    named_prices = {
        "reference_price": reference_price,
        "range_lower": lower,
        "range_upper": upper,
        "kill_switch_lower": lower_ks,
        "kill_switch_upper": upper_ks,
    }
    for field_name, value in named_prices.items():
        if value is None:
            continue
        if min_price is not None and value < min_price:
            errors.append({"code": "PRICE_BELOW_MIN_PRICE", "msg": f"{field_name}={value} ниже min_price={min_price}."})
        if max_price is not None and value > max_price:
            errors.append({"code": "PRICE_ABOVE_MAX_PRICE", "msg": f"{field_name}={value} выше max_price={max_price}."})
        snapped_value = _quantize_to_step(value, tick_size, mode="nearest")
        if snapped_value is not None:
            snapped[field_name] = _format_step_aligned(snapped_value, tick_size) or str(snapped_value)
            if abs(float(snapped_value) - float(value)) > max(1e-12, abs(tick_size or 0.0) * 1e-6):
                target = errors if require_meta else warnings
                target.append({
                    "code": "PRICE_OFF_TICK",
                    "msg": f"{field_name}={value} не выровнен по tick_size={tick_size}; ближайшее допустимое значение={snapped[field_name]}",
                })

    if lower is not None and upper is not None:
        if upper <= lower:
            errors.append({"code": "INVALID_RANGE", "msg": "price_range_upper должен быть строго больше price_range_lower."})
        elif tick_size is not None and (upper - lower) < tick_size:
            errors.append({"code": "RANGE_TOO_NARROW_FOR_TICK", "msg": f"Ширина диапазона {upper - lower:.12f} меньше tick_size={tick_size}."})

    if reference_price is not None and lower is not None and upper is not None and not (lower <= reference_price <= upper):
        errors.append({
            "code": "REFERENCE_OUTSIDE_RANGE",
            "msg": f"reference_price={reference_price} находится вне основного диапазона [{lower}, {upper}].",
        })

    if lower_ks is not None and upper_ks is not None and upper_ks <= lower_ks:
        errors.append({"code": "INVALID_KILL_SWITCH_RANGE", "msg": "kill_switch.upper должен быть строго больше kill_switch.lower."})
    if lower is not None and lower_ks is not None and lower_ks > lower:
        errors.append({
            "code": "KILL_SWITCH_INSIDE_MAIN_RANGE",
            "msg": f"kill_switch.lower={lower_ks} находится внутри основного диапазона и не защищает нижнюю границу {lower}.",
        })
    if upper is not None and upper_ks is not None and upper_ks < upper:
        errors.append({
            "code": "KILL_SWITCH_INSIDE_MAIN_RANGE",
            "msg": f"kill_switch.upper={upper_ks} находится внутри основного диапазона и не защищает верхнюю границу {upper}.",
        })

    if direction in {"long", "short"}:
        exits = directional_exit_levels(direction, lower_ks, upper_ks)
        for err in validate_directional_exit_geometry(direction, reference_price, exits.take_profit, exits.stop_loss):
            errors.append({
                "code": err.get("code", "DIRECTIONAL_EXIT_GEOMETRY_INVALID"),
                "msg": err.get("msg", "Directional TP/SL geometry is invalid."),
            })

    snapped_step = None
    if step_abs is not None and tick_size is not None and step_abs < tick_size:
        errors.append({"code": "GRID_STEP_BELOW_TICK", "msg": f"Шаг сетки {step_abs} меньше tick_size={tick_size}; уровни схлопнутся после округления."})
    if step_abs is not None:
        snapped_step = _quantize_to_step(step_abs, tick_size, mode="nearest") if tick_size is not None else step_abs
        if snapped_step is not None:
            snapped["grid_step_abs"] = _format_step_aligned(snapped_step, tick_size if tick_size is not None else snapped_step) or str(snapped_step)
            if tick_size is not None and tick_size > 0 and not _step_aligned(step_abs, tick_size):
                target = errors if require_meta else warnings
                target.append({
                    "code": "GRID_STEP_OFF_TICK",
                    "msg": f"grid_step_abs={step_abs} не выровнен по tick_size={tick_size}; ближайшее допустимое значение={snapped['grid_step_abs']}",
                })

    snapped_lower = _quantize_to_step(lower, tick_size, mode="nearest") if lower is not None and tick_size is not None else lower
    snapped_upper = _quantize_to_step(upper, tick_size, mode="nearest") if upper is not None and tick_size is not None else upper
    if snapped_lower is not None and snapped_upper is not None and snapped_upper <= snapped_lower:
        errors.append({
            "code": "RANGE_COLLAPSES_AFTER_TICK_ROUNDING",
            "msg": f"После выравнивания по tick_size={tick_size} диапазон схлопывается: lower={snapped_lower}, upper={snapped_upper}.",
        })
    # Validate Bybit "Number of Grids" independently from price metadata.
    # The old check lived inside the range/step branch, so a malformed/manual
    # payload could carry grid_count=401 and avoid the product-cap gate when
    # trade_plan.levels was incomplete.
    if bot_type == "futures_grid":
        for invalid_source in grid_count_resolution.get("invalid") or []:
            errors.append({
                "code": "GRID_COUNT_NOT_INTEGER",
                "msg": (
                    f"{invalid_source.get('field')}={invalid_source.get('value')!r} не является точным целым числом; "
                    "grid_count нельзя усекать или округлять при execution-preflight."
                ),
            })
        if grid_count_resolution.get("conflict"):
            rendered_sources = ", ".join(
                f"{item.get('field')}={item.get('value')}" for item in (grid_count_resolution.get("sources") or [])
            )
            errors.append({
                "code": "GRID_COUNT_CONFLICT",
                "msg": f"Конфликт grid-count aliases: {rendered_sources}. Execution-preflight блокирует неоднозначную геометрию.",
            })
        if grid_levels is None:
            target = errors if require_meta else warnings
            target.append({
                "code": "GRID_COUNT_MISSING",
                "msg": "grid_count/grid_levels отсутствует; нельзя подтвердить число price intervals для Bybit Futures Grid.",
            })
        elif grid_levels < BYBIT_FUTURES_GRID_MIN_COUNT:
            errors.append({
                "code": "GRID_LEVELS_INVALID",
                "msg": f"grid_count/grid_levels должен быть >= {BYBIT_FUTURES_GRID_MIN_COUNT}, получено {grid_levels}.",
            })
        elif grid_levels > BYBIT_FUTURES_GRID_MAX_COUNT:
            errors.append({
                "code": "GRID_COUNT_ABOVE_BYBIT_MAX",
                "msg": f"Bybit Futures Grid Bot допускает максимум {BYBIT_FUTURES_GRID_MAX_COUNT} grids, получено {grid_levels}.",
            })

    if snapped_step is not None and snapped_lower is not None and snapped_upper is not None and snapped_upper > snapped_lower:
        span = float(snapped_upper) - float(snapped_lower)
        if snapped_step > span:
            errors.append({
                "code": "GRID_STEP_EXCEEDS_RANGE",
                "msg": f"Шаг сетки {snapped_step} больше ширины диапазона {span}; сетка вырождается.",
            })
        else:
            intervals = int(math.floor((span / float(snapped_step)) + 1e-12)) if snapped_step > 0 else 0
            if intervals < BYBIT_FUTURES_GRID_MIN_COUNT:
                errors.append({
                    "code": "GRID_TOO_FEW_TICK_LEVELS",
                    "msg": f"После выравнивания по tick_size сетка содержит только {intervals} интервал(ов); для grid требуется минимум {BYBIT_FUTURES_GRID_MIN_COUNT}.",
                })
            if grid_levels is not None and BYBIT_FUTURES_GRID_MIN_COUNT <= grid_levels <= BYBIT_FUTURES_GRID_MAX_COUNT:
                # Bybit's "Number of Grids" is the count of price intervals. If
                # range/step implies a different number of intervals after tick
                # rounding, the displayed TP/economics no longer describe the grid
                # an operator will create from lower/upper/grid_count. Treat this as
                # a hard execution error in strict preflight, not as a harmless
                # legacy price-point ambiguity.
                if intervals != int(grid_levels):
                    strict_geometry_payload = (
                        str(params.get("grid_geometry_model") or "").strip() == "bybit_arithmetic_range_width_div_grid_count"
                        or params.get("actual_grid_step_abs") is not None
                        or params.get("actual_grid_spacing_pct") is not None
                    )
                    target = errors if (require_meta and strict_geometry_payload) else warnings
                    target.append({
                        "code": "GRID_STEP_LEVELS_MISMATCH",
                        "msg": f"Диапазон и step_abs дают примерно {intervals} интервал(ов), а params.grid_count/grid_levels={grid_levels}; Bybit Number of Grids должен совпадать с числом интервалов, иначе TP/economics не соответствуют исполнимой сетке.",
                    })

    if grid_type and grid_type != SUPPORTED_RECOMMENDER_GRID_TYPE:
        errors.append({
            "code": "GRID_TYPE_UNSUPPORTED",
            "msg": f"Эта ревизия рассчитывает и проверяет только {SUPPORTED_RECOMMENDER_GRID_TYPE} grid; получено grid_type={grid_type}. Geometric grid нельзя запускать без отдельной геометрической математики диапазона, net-profit и tick rounding.",
        })

    if tp_abs is not None:
        if tp_abs <= 0:
            errors.append({"code": "TP_PER_LEG_NON_POSITIVE", "msg": f"tp_per_leg.abs должен быть > 0, получено {tp_abs}."})
        elif tick_size is not None:
            snapped_tp = _quantize_to_step(tp_abs, tick_size, mode="nearest")
            if snapped_tp is not None:
                snapped["tp_per_leg_abs"] = _format_step_aligned(snapped_tp, tick_size) or str(snapped_tp)
                if snapped_tp <= 0:
                    errors.append({"code": "TP_PER_LEG_COLLAPSES_AFTER_TICK_ROUNDING", "msg": f"tp_per_leg.abs={tp_abs} схлопывается после округления по tick_size={tick_size}."})
                elif abs(float(snapped_tp) - float(tp_abs)) > max(1e-12, abs(tick_size) * 1e-6):
                    target = errors if require_meta else warnings
                    target.append({
                        "code": "TP_PER_LEG_OFF_TICK",
                        "msg": f"tp_per_leg.abs={tp_abs} не выровнен по tick_size={tick_size}; ближайшее допустимое значение={snapped['tp_per_leg_abs']}",
                    })
    if tp_pct is not None and tp_pct <= 0:
        errors.append({"code": "TP_PER_LEG_PCT_NON_POSITIVE", "msg": f"tp_per_leg.pct должен быть > 0, получено {tp_pct}."})

    if bot_type == "futures_grid" and venue == "linear" and leverage is None:
        leverage_missing_msg = "leverage не указан; execution-time preflight не может подтвердить плечо, маржу и cross-margin equity buffer."
        if require_execution_plan:
            errors.append({"code": "LEVERAGE_MISSING_FOR_EXECUTION", "msg": leverage_missing_msg})
        else:
            warnings.append({"code": "LEVERAGE_DEFAULTED_TO_ONE", "msg": "leverage не указан в legacy/manual payload; для preflight принимается только безопасный default 1x, новые рекомендации должны хранить явный leverage."})
    if leverage is not None and leverage <= 0:
        errors.append({"code": "LEVERAGE_NON_POSITIVE", "msg": f"Leverage должен быть > 0, получено {leverage}."})
    if min_leverage is not None and leverage is not None and leverage < min_leverage:
        errors.append({"code": "LEVERAGE_BELOW_MIN", "msg": f"Рекомендованное leverage={leverage} ниже Bybit min_leverage={min_leverage}."})
    if max_leverage is not None and leverage is not None and leverage > max_leverage:
        errors.append({"code": "LEVERAGE_ABOVE_MAX", "msg": f"Рекомендованное leverage={leverage} выше Bybit max_leverage={max_leverage}."})
    if leverage is not None and leverage_step is not None and leverage_step > 0:
        snapped_leverage = _quantize_to_step(leverage, leverage_step, mode="nearest")
        if snapped_leverage is not None:
            snapped["leverage"] = _format_step_aligned(snapped_leverage, leverage_step) or str(snapped_leverage)
            if abs(float(snapped_leverage) - float(leverage)) > max(1e-12, abs(leverage_step) * 1e-6):
                errors.append({
                    "code": "LEVERAGE_OFF_STEP",
                    "msg": f"Leverage {leverage} не выровнен по leverage_step={leverage_step}; ближайшее допустимое значение={snapped['leverage']}",
                })

    # Bybit Futures Grid uses cross margin.  Recompute a deterministic
    # bot-equity stress from canonical grid commitment and kill-switch geometry;
    # never use a single-position isolated liquidation-price approximation.
    if leverage is not None and venue == "linear" and leverage > 1:
        economics = params.get("economics") if isinstance(params.get("economics"), dict) else {}
        cost_model = params.get("cost_model") if isinstance(params.get("cost_model"), dict) else {}
        execution_cost_bps = _finite_float_or_none(economics.get("execution_cost_bps"))
        if execution_cost_bps is None:
            execution_cost_bps = _finite_float_or_none(cost_model.get("execution_cost_bps"))
        if execution_cost_bps is None:
            execution_cost_bps = _finite_float_or_none(cost_model.get("total_cost_bps"))
        count_resolution = resolve_integer_aliases([
            ("params.grid_count", params.get("grid_count")),
            ("params.grid_levels", params.get("grid_levels")),
            ("plan.grid_count", plan.get("grid_count")),
        ])
        resolved_count = count_resolution.get("value") if count_resolution.get("ok") else None
        stress = None
        if all(value is not None for value in (reference_price, lower, upper, lower_ks, upper_ks, resolved_count)):
            stress = arithmetic_grid_cross_margin_stress(
                lower=lower,
                upper=upper,
                grid_count=resolved_count,
                reference_price=reference_price,
                direction=direction,
                leverage=leverage,
                kill_switch_lower=lower_ks,
                kill_switch_upper=upper_ks,
                execution_cost_bps=execution_cost_bps or 0.0,
            )
        stress_buffer_pct = (
            _finite_float_or_none(stress.get("equity_buffer_pct"))
            if isinstance(stress, dict)
            else None
        )
        if stress_buffer_pct is None:
            errors.append({
                "code": "CROSS_MARGIN_STRESS_UNAVAILABLE",
                "msg": "Leverage > 1 требует проверяемого cross-margin equity stress по grid geometry и kill-switch; isolated liquidation price не используется.",
            })
        elif stress_buffer_pct < 12.0:
            errors.append({
                "code": "LIQUIDATION_BUFFER_TOO_LOW",
                "msg": f"Cross-margin equity buffer={stress_buffer_pct:.2f}% слишком мал для запуска futures grid с leverage={leverage}.",
            })

    sizing_candidates = [
        plan.get("sizing") if isinstance(plan.get("sizing"), dict) else {},
        params.get("sizing") if isinstance(params.get("sizing"), dict) else {},
        operator_sheet.get("sizing") if isinstance(operator_sheet.get("sizing"), dict) else {},
        params.get("economics") if isinstance(params.get("economics"), dict) else {},
        plan.get("economics") if isinstance(plan.get("economics"), dict) else {},
        operator_sheet.get("economics") if isinstance(operator_sheet.get("economics"), dict) else {},
        operator_sheet,
    ]
    qty_keys = (
        "order_qty",
        "qty",
        "qty_per_order",
        "qty_per_leg",
        "base_qty",
        "base_qty_per_order",
        "order_size_qty",
        "leg_qty",
    )
    notional_keys = (
        "order_notional",
        "order_notional_usdt",
        "notional_per_order",
        "quote_qty",
        "quote_amount",
        "capital_per_leg_usdt",
        "investment_per_grid",
        "usdt_per_order",
    )
    qty_source, order_qty = (None, None)
    notional_source, order_notional = (None, None)
    for sizing in sizing_candidates:
        if order_qty is None:
            qty_source, order_qty = _first_finite_from_mapping(sizing, qty_keys)
        if order_notional is None:
            notional_source, order_notional = _first_finite_from_mapping(sizing, notional_keys)
    if order_qty is None:
        qty_source, order_qty = _first_finite_from_mapping(params, qty_keys)
    if order_notional is None:
        notional_source, order_notional = _first_finite_from_mapping(params, notional_keys)

    size_known = order_qty is not None or order_notional is not None
    notional_checked = False
    if order_qty is not None:
        if order_qty <= 0:
            errors.append({"code": "ORDER_QTY_NON_POSITIVE", "msg": f"{qty_source or 'order_qty'} должен быть > 0, получено {order_qty}."})
        if min_order_qty is not None and order_qty < min_order_qty:
            errors.append({"code": "ORDER_QTY_BELOW_MIN", "msg": f"{qty_source or 'order_qty'}={order_qty} ниже Bybit min_order_qty={min_order_qty}."})
        if max_order_qty is not None and order_qty > max_order_qty:
            errors.append({"code": "ORDER_QTY_ABOVE_MAX", "msg": f"{qty_source or 'order_qty'}={order_qty} выше Bybit max_order_qty={max_order_qty}."})
        if qty_step is not None and qty_step > 0:
            snapped_qty = _quantize_to_step(order_qty, qty_step, mode="nearest")
            if snapped_qty is not None:
                snapped["order_qty"] = _format_step_aligned(snapped_qty, qty_step) or str(snapped_qty)
            if not _step_aligned(order_qty, qty_step):
                errors.append({
                    "code": "ORDER_QTY_OFF_STEP",
                    "msg": f"{qty_source or 'order_qty'}={order_qty} не выровнен по Bybit qty_step={qty_step}; ближайшее значение={snapped.get('order_qty') or snapped_qty}.",
                })
        if min_notional is not None:
            notional_price = _grid_min_notional_price(reference_price, lower, upper)
            if notional_price is not None:
                qty_notional = order_qty * notional_price
                notional_checked = True
                if qty_notional < min_notional:
                    errors.append({
                        "code": "ORDER_NOTIONAL_BELOW_MIN",
                        "msg": f"Минимальный расчётный notional={qty_notional:.12g} по {qty_source or 'order_qty'} и grid_min_price={notional_price:.12g} ниже Bybit min_notional={min_notional}; lower-grid заявки могут быть отклонены биржей.",
                    })
            else:
                warnings.append({
                    "code": "MIN_NOTIONAL_NOT_CHECKED",
                    "msg": f"Bybit min_notional={min_notional}, но reference/range price отсутствуют; notional по order_qty проверить нельзя.",
                })

    if order_notional is not None:
        notional_checked = True
        if order_notional <= 0:
            errors.append({"code": "ORDER_NOTIONAL_NON_POSITIVE", "msg": f"{notional_source or 'order_notional'} должен быть > 0, получено {order_notional}."})
        if min_notional is not None and order_notional < min_notional:
            errors.append({
                "code": "ORDER_NOTIONAL_BELOW_MIN",
                "msg": f"{notional_source or 'order_notional'}={order_notional} ниже Bybit min_notional={min_notional}.",
            })
        if min_notional is not None and order_notional >= min_notional:
            notional_price = _grid_min_notional_price(reference_price, lower, upper)
            if reference_price is not None and reference_price > 0 and notional_price is not None and notional_price > 0:
                # A quote-notional-only payload is normally estimated at reference_price.
                # For fixed-base Bybit grid orders, lower levels have notional
                # order_notional * grid_min_price / reference_price, so a value barely
                # above the Bybit floor at reference can still be rejected on lower grid levels.
                grid_min_notional = float(order_notional) * float(notional_price) / float(reference_price)
                if grid_min_notional < min_notional:
                    errors.append({
                        "code": "ORDER_NOTIONAL_BELOW_MIN",
                        "msg": f"Минимальный расчётный notional={grid_min_notional:.12g} по {notional_source or 'order_notional'}={order_notional:.12g}, reference_price={reference_price:.12g} и grid_min_price={notional_price:.12g} ниже Bybit min_notional={min_notional}; lower-grid заявки могут быть отклонены биржей.",
                    })
            elif order_qty is None:
                warnings.append({
                    "code": "MIN_NOTIONAL_NOT_CHECKED",
                    "msg": f"Bybit min_notional={min_notional}, но reference/range price отсутствуют; notional-only sizing нельзя консервативно пересчитать для lower-grid уровней.",
                })

    if order_qty is not None and order_notional is not None and reference_price is not None and reference_price > 0:
        implied_notional = order_qty * reference_price
        tolerance = max(0.01, abs(implied_notional) * 0.005)
        if abs(implied_notional - order_notional) > tolerance:
            errors.append({
                "code": "ORDER_QTY_NOTIONAL_MISMATCH",
                "msg": f"{qty_source or 'order_qty'} * reference_price = {implied_notional:.12g} USDT, но {notional_source or 'order_notional'}={order_notional:.12g}; sizing payload внутренне несогласован.",
            })


    if bot_type == "futures_grid" and require_execution_plan:
        economics_candidates = [
            params.get("economics") if isinstance(params.get("economics"), dict) else {},
            plan.get("economics") if isinstance(plan.get("economics"), dict) else {},
            operator_sheet.get("economics") if isinstance(operator_sheet.get("economics"), dict) else {},
        ]
        economics: dict[str, Any] = {}
        for candidate in economics_candidates:
            if candidate:
                economics = candidate
                break
        cost_model = params.get("cost_model") if isinstance(params.get("cost_model"), dict) else {}
        if not economics:
            warnings.append({
                "code": "GRID_ECONOMICS_MISSING",
                "msg": "Execution-plan не содержит economics; legacy payload допускается только после остальных preflight-проверок, новые рекомендации должны хранить net economics.",
            })
        else:
            net_profit_bps = _finite_float_or_none(economics.get("net_profit_bps"))
            gross_profit_bps = _finite_float_or_none(economics.get("gross_profit_bps"))
            execution_cost_bps = _finite_float_or_none(economics.get("execution_cost_bps"))
            if execution_cost_bps is None:
                execution_cost_bps = _finite_float_or_none(cost_model.get("execution_cost_bps"))
            funding_cost_bps = _finite_float_or_none(economics.get("funding_cost_bps"))
            if funding_cost_bps is None:
                funding_cost_bps = max(0.0, _finite_float_or_none(cost_model.get("expected_funding_bps")) or 0.0)
            if gross_profit_bps is not None and gross_profit_bps < 0.0:
                errors.append({"code": "GRID_GROSS_PROFIT_NEGATIVE", "msg": f"gross_profit_bps={gross_profit_bps:.2f} не может быть отрицательным."})
            if execution_cost_bps is not None and execution_cost_bps < 0.0:
                errors.append({"code": "GRID_EXECUTION_COST_NEGATIVE", "msg": f"execution_cost_bps={execution_cost_bps:.2f} не может быть отрицательным."})
            if funding_cost_bps is not None and funding_cost_bps < 0.0:
                errors.append({"code": "GRID_FUNDING_COST_NEGATIVE", "msg": f"funding_cost_bps={funding_cost_bps:.2f} не может быть отрицательным; funding benefit должен храниться отдельно как signed diagnostic."})
            if net_profit_bps is not None and net_profit_bps <= 0.0:
                errors.append({"code": "GRID_NET_PROFIT_NON_POSITIVE", "msg": f"net_profit_bps={net_profit_bps:.2f} после execution/funding costs <= 0."})
            elif net_profit_bps is not None and net_profit_bps < EXECUTION_MIN_NET_PROFIT_BPS:
                errors.append({"code": "GRID_NET_PROFIT_TOO_THIN", "msg": f"net_profit_bps={net_profit_bps:.2f} < {EXECUTION_MIN_NET_PROFIT_BPS:.2f} bps; edge слишком тонкий для live execution."})
            if execution_cost_bps is not None and gross_profit_bps is not None and gross_profit_bps <= execution_cost_bps * EXECUTION_GROSS_COST_COVERAGE_MULTIPLIER:
                errors.append({
                    "code": "GRID_GROSS_EDGE_BELOW_COSTS",
                    "msg": f"gross_profit_bps={gross_profit_bps:.2f} почти не покрывает execution_cost_bps={execution_cost_bps:.2f}; запуск fail-closed.",
                })
            if funding_cost_bps is not None and funding_cost_bps >= EXECUTION_FUNDING_EXTREME_BPS:
                errors.append({
                    "code": "GRID_FUNDING_COST_EXTREME",
                    "msg": f"funding_cost_bps={funding_cost_bps:.2f} за горизонт >= {EXECUTION_FUNDING_EXTREME_BPS:.2f}; carry риск слишком высок для grid.",
                })
            raw_active_orders = economics.get("estimated_active_orders")
            if raw_active_orders is not None and not (isinstance(raw_active_orders, str) and not raw_active_orders.strip()):
                est_active_orders = strict_integer(raw_active_orders)
                if est_active_orders is None:
                    errors.append({
                        "code": "ACTIVE_ORDERS_NOT_INTEGER",
                        "msg": f"estimated_active_orders={raw_active_orders!r} не является точным целым числом; оценка маржи неоднозначна.",
                    })
                elif grid_commitment is not None and est_active_orders != int(grid_commitment["active_order_count"]):
                    errors.append({
                        "code": "ACTIVE_ORDERS_GRID_COUNT_MISMATCH",
                        "msg": (
                            f"estimated_active_orders={est_active_orders}, но исполнимая arithmetic topology требует "
                            f"{int(grid_commitment['active_order_count'])} ордер(ов) при grid_count={grid_levels}; "
                            "Number of Grids считает интервалы: существует grid_count+1 ценовых уровней, но динамическая topology оставляет один pivot/bridge уровень без начальной заявки, поэтому initial orders = grid_count."
                        ),
                    })
            raw_committed_slots = economics.get("estimated_committed_slots")
            if raw_committed_slots is not None and not (isinstance(raw_committed_slots, str) and not raw_committed_slots.strip()):
                est_committed_slots = strict_integer(raw_committed_slots)
                if est_committed_slots is None:
                    errors.append({
                        "code": "COMMITTED_SLOTS_NOT_INTEGER",
                        "msg": f"estimated_committed_slots={raw_committed_slots!r} не является точным целым числом; оценка initial-order commitment неоднозначна.",
                    })
                elif grid_commitment is not None and est_committed_slots != int(grid_commitment["committed_slot_count"]):
                    errors.append({
                        "code": "COMMITTED_SLOTS_TOPOLOGY_MISMATCH",
                        "msg": (
                            f"estimated_committed_slots={est_committed_slots}, но arithmetic topology требует "
                            f"{int(grid_commitment['committed_slot_count'])}; active resting orders и одновременно "
                            "резервируемые directional slots не являются одним показателем."
                        ),
                    })
            raw_max_position_slots = economics.get("estimated_max_position_slots")
            if raw_max_position_slots is not None and not (isinstance(raw_max_position_slots, str) and not raw_max_position_slots.strip()):
                est_max_position_slots = strict_integer(raw_max_position_slots)
                if est_max_position_slots is None:
                    errors.append({
                        "code": "MAX_POSITION_SLOTS_NOT_INTEGER",
                        "msg": f"estimated_max_position_slots={raw_max_position_slots!r} не является точным целым числом.",
                    })
                elif grid_commitment is not None and est_max_position_slots != int(grid_commitment["max_abs_position_slots"]):
                    errors.append({
                        "code": "MAX_POSITION_SLOTS_TOPOLOGY_MISMATCH",
                        "msg": (
                            f"estimated_max_position_slots={est_max_position_slots}, но arithmetic topology допускает "
                            f"максимум {int(grid_commitment['max_abs_position_slots'])} однонаправленных slot(ов)."
                        ),
                    })
            total_notional_est = _finite_float_or_none(economics.get("estimated_total_order_notional_usdt"))
            margin_est = _finite_float_or_none(economics.get("estimated_margin_required_usdt"))
            leverage_for_margin = leverage if leverage is not None and leverage > 0 else 1.0
            if total_notional_est is not None and margin_est is not None and leverage_for_margin > 0:
                expected_margin = total_notional_est / leverage_for_margin
                tolerance = max(0.02, abs(expected_margin) * 0.02)
                if abs(margin_est - expected_margin) > tolerance:
                    errors.append({
                        "code": "MARGIN_NOTIONAL_LEVERAGE_MISMATCH",
                        "msg": f"estimated_margin_required={margin_est:.6g} не соответствует total_notional/leverage={expected_margin:.6g}; риск маржи рассчитан неверно.",
                    })
            if total_notional_est is not None and grid_commitment is not None:
                expected_total_notional = None
                if order_qty is not None:
                    expected_total_notional = (
                        float(order_qty) * float(grid_commitment["committed_notional_per_qty"])
                    )
                elif order_notional is not None and reference_price is not None and reference_price > 0:
                    expected_total_notional = (
                        float(order_notional) / float(reference_price)
                        * float(grid_commitment["committed_notional_per_qty"])
                    )
                if expected_total_notional is not None:
                    tolerance = max(0.05, abs(expected_total_notional) * 0.02)
                    if abs(total_notional_est - expected_total_notional) > tolerance:
                        errors.append({
                            "code": "TOTAL_NOTIONAL_GRID_COUNT_MISMATCH",
                            "msg": (
                                f"estimated_total_order_notional={total_notional_est:.6g} не соответствует "
                                f"initial grid commitment={expected_total_notional:.6g}; interval count нельзя "
                                "механически умножать на reference notional, когда reference находится между уровнями."
                            ),
                        })

    if not size_known:
        warnings.append({
            "code": "SIZE_INPUT_REQUIRED",
            "msg": "Проект не рассчитывает order qty/капитал на leg, поэтому qty_step/min_order_qty нельзя проверить без операторского размера позиции.",
        })
    if min_notional is not None and not notional_checked:
        warnings.append({
            "code": "MIN_NOTIONAL_NOT_CHECKED",
            "msg": f"Bybit min_notional={min_notional}, но система не знает фактический размер заявки и не может проверить этот лимит автоматически.",
        })

    return {
        "ok": len(errors) == 0,
        "critical": len(errors) > 0,
        "errors": errors,
        "warnings": warnings,
        "meta_checked": bool(meta),
        "snapped_levels": snapped,
    }


LIVE_VALIDATION_DIRECTION_MIN_BOTS = 8
LIVE_VALIDATION_SYMBOL_MIN_BOTS = 12
LIVE_VALIDATION_PORTFOLIO_MIN_BOTS = 20
LIVE_VALIDATION_DIRECTION_LOSS_STREAK = 5
LIVE_VALIDATION_WINDOW_BOTS = 50
LIVE_VALIDATION_SOURCE_SCAN_LIMIT = 1000


def _live_validation_scope_summary(
    records: list[dict[str, Any]],
    *,
    max_observations: int = LIVE_VALIDATION_WINDOW_BOTS,
) -> dict[str, Any]:
    """Summarise independent, stopped bots backed by exact execution evidence.

    One publication root contributes at most one observation. This prevents repeated
    publications of the same signal lineage from pretending to be independent
    evidence. Input is newest-first, matching ``list_live_validation_records``.
    """
    independent: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for row in records:
        if len(independent) >= max(1, int(max_observations)):
            break
        if (
            not isinstance(row, dict)
            or not bool(row.get("validation_eligible"))
            or row.get("total_pnl_finalized") is not True
        ):
            continue
        root = str(row.get("publication_root_rec_id") or row.get("rec_id") or row.get("bot_id") or "").strip()
        if not root or root in seen_roots:
            continue
        raw_net = row.get("realized_pnl_net")
        if isinstance(raw_net, bool):
            continue
        try:
            net = float(raw_net)
        except Exception:
            continue
        if not math.isfinite(net):
            continue
        seen_roots.add(root)
        independent.append({**row, "realized_pnl_net": net})

    values = [float(row["realized_pnl_net"]) for row in independent]
    total = float(sum(values))
    mean = (total / len(values)) if values else None
    ordered = sorted(values)
    median = None
    if ordered:
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    wins = sum(1 for value in values if value > 0.0)
    losses = sum(1 for value in values if value < 0.0)
    consecutive_losses = 0
    for value in values:  # newest first
        if value < 0.0:
            consecutive_losses += 1
        else:
            break
    return {
        "eligible_stopped_bots": len(values),
        "independent_publication_roots": len(values),
        "total_realized_pnl_net": total,
        "mean_realized_pnl_net": mean,
        "median_realized_pnl_net": median,
        "positive_bot_rate": (wins / len(values)) if values else None,
        "negative_bot_rate": (losses / len(values)) if values else None,
        "consecutive_losses": consecutive_losses,
        "newest_bot_id": independent[0].get("bot_id") if independent else None,
        "oldest_bot_id": independent[-1].get("bot_id") if independent else None,
    }


def _negative_expectancy_condition(summary: dict[str, Any], *, min_bots: int) -> bool:
    """Stop a sufficiently large cohort once its realised cumulative net PnL is negative.

    Grid systems commonly realise many small profitable cycles and an occasional
    large range-break loss. Requiring a negative median and a sub-50% win rate
    therefore lets the defining short-gamma/tail-loss failure mode pass open even
    when the exact-evidence cohort has already lost money in aggregate. The sample
    floor remains the noise guard; after that floor, negative cumulative net PnL is
    sufficient for this operational stop criterion.
    """
    count = int(summary.get("eligible_stopped_bots") or 0)
    total = summary.get("total_realized_pnl_net")
    mean = summary.get("mean_realized_pnl_net")
    if count < int(min_bots) or total is None or mean is None:
        return False
    return bool(float(total) < 0.0 and float(mean) < 0.0)


def _compute_live_validation_strategy_health(
    conn,
    *,
    venue: str,
    symbol: str | None,
    direction: str | None,
    bot_type: str,
    model_version: str | None = None,
) -> dict[str, Any]:
    """Return a conservative operational stop gate from exact realised evidence.

    This is deliberately not an alpha/significance claim. It only prevents the
    operator lifecycle from continuing unchanged after persistent realised losses.
    Direction is evaluated separately so a losing long cohort cannot silently
    contaminate or suppress an opposite-direction cohort before the broader symbol
    stop threshold is reached.
    """
    venue_key = str(venue or "").strip().lower()
    symbol_key = str(symbol or "").strip().upper()
    direction_key = str(direction or "").strip().lower()
    bot_type_key = str(bot_type or "").strip().lower()
    model_version_key = str(model_version or "").strip()
    records = db.list_live_validation_records(conn, limit=LIVE_VALIDATION_SOURCE_SCAN_LIMIT)
    portfolio_records = [
        row
        for row in records
        if str(row.get("venue") or "").strip().lower() == venue_key
        and str(row.get("bot_type") or "").strip().lower() == bot_type_key
        and (not model_version_key or str(row.get("model_version") or "").strip() == model_version_key)
    ]
    symbol_records = [
        row
        for row in portfolio_records
        if symbol_key and str(row.get("symbol") or "").strip().upper() == symbol_key
    ]
    direction_records = [
        row
        for row in symbol_records
        if direction_key and str(row.get("direction") or "").strip().lower() == direction_key
    ]
    portfolio = _live_validation_scope_summary(portfolio_records)
    symbol_summary = _live_validation_scope_summary(symbol_records)
    direction_summary = _live_validation_scope_summary(direction_records)
    blocks: list[dict[str, Any]] = []

    if direction_key and int(direction_summary["consecutive_losses"]) >= LIVE_VALIDATION_DIRECTION_LOSS_STREAK:
        blocks.append({
            "code": "LIVE_VALIDATION_DIRECTION_LOSS_STREAK",
            "msg": (
                f"{symbol_key} {direction_key}: последние {direction_summary['consecutive_losses']} независимых "
                "остановленных ботов с exact execution evidence убыточны; новые запуски этого направления остановлены."
            ),
            "scope": "symbol_direction",
            "metrics": direction_summary,
        })

    if direction_key and _negative_expectancy_condition(direction_summary, min_bots=LIVE_VALIDATION_DIRECTION_MIN_BOTS):
        rate = float(direction_summary["positive_bot_rate"])
        blocks.append({
            "code": "LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY",
            "msg": (
                f"{symbol_key} {direction_key}: exact execution evidence имеет отрицательные cumulative total и mean "
                f"net PnL после минимальной выборки; median={direction_summary.get('median_realized_pnl_net')}, "
                f"positive_rate={rate:.1%}. Tail-loss grid cohort не может оставаться открытым только из-за высокого win rate."
            ),
            "scope": "symbol_direction",
            "metrics": direction_summary,
        })

    if symbol_key and _negative_expectancy_condition(symbol_summary, min_bots=LIVE_VALIDATION_SYMBOL_MIN_BOTS):
        rate = float(symbol_summary["positive_bot_rate"])
        blocks.append({
            "code": "LIVE_VALIDATION_SYMBOL_NEGATIVE_EXPECTANCY",
            "msg": (
                f"{symbol_key}: exact execution evidence показывает отрицательные cumulative total и mean net PnL "
                f"после минимальной выборки; median={symbol_summary.get('median_realized_pnl_net')}, "
                f"positive_rate={rate:.1%}. Новые запуски символа остановлены."
            ),
            "scope": "symbol",
            "metrics": symbol_summary,
        })

    if _negative_expectancy_condition(portfolio, min_bots=LIVE_VALIDATION_PORTFOLIO_MIN_BOTS):
        blocks.append({
            "code": "LIVE_VALIDATION_PORTFOLIO_NEGATIVE_EXPECTANCY",
            "msg": (
                "Весь futures_grid-контур имеет отрицательные cumulative total и mean net PnL по exact execution evidence; "
                "операторские запуски остановлены независимо от median/win rate до ревизии модели."
            ),
            "scope": "portfolio",
            "metrics": portfolio,
        })

    return {
        "blocked": bool(blocks),
        "blocks": blocks,
        "policy": {
            "window_bots": LIVE_VALIDATION_WINDOW_BOTS,
            "source_scan_limit": LIVE_VALIDATION_SOURCE_SCAN_LIMIT,
            "direction_min_bots": LIVE_VALIDATION_DIRECTION_MIN_BOTS,
            "symbol_min_bots": LIVE_VALIDATION_SYMBOL_MIN_BOTS,
            "portfolio_min_bots": LIVE_VALIDATION_PORTFOLIO_MIN_BOTS,
            "direction_loss_streak": LIVE_VALIDATION_DIRECTION_LOSS_STREAK,
            "negative_expectancy_basis": "negative_cumulative_net_pnl_after_min_sample",
            "tail_loss_guard": True,
            "requires_exact_execution_evidence": True,
            "deduplicates_publication_roots": True,
            "statistical_claim": False,
            "model_version_scoped": bool(model_version_key),
        },
        "model_version": model_version_key or None,
        "direction": direction_summary,
        "symbol": symbol_summary,
        "portfolio": portfolio,
    }


def _execution_preflight(
    conn,
    rec: dict[str, Any],
    *,
    now_ts: int | None = None,
    bybit_meta: dict[str, Any] | None = None,
    risk_limits: dict[str, Any] | None = None,
    risk_status: Any | None = None,
) -> dict[str, Any]:
    now = int(now_ts or time.time())
    blocks: list[dict[str, Any]] = []

    strategy_health = _compute_live_validation_strategy_health(
        conn,
        venue=str(rec.get("venue") or ""),
        symbol=str(rec.get("symbol") or ""),
        direction=str(rec.get("direction") or ""),
        bot_type=str(rec.get("bot_type") or ""),
        model_version=str(rec.get("model_version") or ""),
    )
    for item in strategy_health.get("blocks") or []:
        if isinstance(item, dict):
            blocks.append(dict(item))

    effective_risk_limits = risk_limits if isinstance(risk_limits, dict) else get_risk_limits(conn, settings.risk_limits)
    effective_risk_status = risk_status if risk_status is not None else compute_risk_status(conn, effective_risk_limits)
    daily_loss_budget = _execution_daily_loss_budget_guard(rec, effective_risk_limits, effective_risk_status)
    for item in daily_loss_budget.get("blocks") or []:
        if isinstance(item, dict):
            blocks.append(dict(item))

    if bybit_meta is None:
        try:
            bybit_meta = _fetch_bybit_instrument_meta(str(rec.get("venue") or ""), str(rec.get("symbol") or ""))
        except Exception:
            bybit_meta = {}
    elif not isinstance(bybit_meta, dict):
        bybit_meta = {}
    else:
        bybit_meta = dict(bybit_meta)

    rec_for_validation = _snap_reco_payload_to_bybit_meta(rec, bybit_meta)
    blocks.extend(_execution_recommendation_freshness_blocks(conn, rec_for_validation, now_ts=now))
    blocks.extend(_execution_market_data_blocks(conn, rec_for_validation, now_ts=now))
    blocks.extend(_execution_live_price_blocks(conn, rec_for_validation))
    blocks.extend(_execution_funding_blocks(conn, rec_for_validation, now_ts=now))

    market_shock = _get_app_config_mapping(
        conn,
        MARKET_SHOCK_APP_KEY,
        default={
            "state": "normal",
            "title": "Нормальный режим",
            "severity": "normal",
            "entry_mode": "normal",
            "operator_note": "Новые входы разрешены в обычном режиме.",
            "reasons": [],
            "metrics": {},
        },
    )
    blocks.extend(
        apply_market_shock_gate(
            market_shock,
            str(rec.get("venue") or ""),
            str(rec.get("bot_type") or ""),
            str(rec.get("direction") or "neutral"),
        )
    )

    feature_row = db.get_latest_features(conn, str(rec.get("venue") or ""), str(rec.get("symbol") or "")) or {}
    fast_veto = compute_symbol_fast_veto(
        conn,
        str(rec.get("venue") or ""),
        str(rec.get("symbol") or ""),
        now,
        str(rec.get("direction") or "neutral"),
        feature_row=feature_row,
    )
    for block in fast_veto.get("blocks") or []:
        if isinstance(block, dict):
            blocks.append(dict(block))

    bybit_validation = _validate_trade_plan_against_bybit_meta(rec_for_validation, bybit_meta, require_meta=True, require_execution_plan=True)
    for item in bybit_validation.get("errors") or []:
        if isinstance(item, dict):
            blocks.append({"code": str(item.get("code") or "BYBIT_PLAN_INVALID"), "msg": str(item.get("msg") or "Bybit plan validation failed")})

    return {
        "blocks": blocks,
        "strategy_health": strategy_health,
        "market_shock": market_shock,
        "fast_veto": fast_veto,
        "bybit_meta": bybit_meta,
        "bybit_validation": bybit_validation,
        "daily_loss_budget": daily_loss_budget,
    }


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        logger.debug("rollback error", exc_info=True)


def _is_runtime_lock_lost_error(exc: Exception) -> bool:
    if isinstance(exc, RuntimeLockLostError):
        return True
    msg = str(exc).lower()
    return "runtime lock lost" in msg or "lost reco leadership" in msg or "lost llm leadership" in msg or "lost collector leadership" in msg


def _log_decision_fresh(action: str, rec_id: str | None, operator: str | None, details: dict[str, Any]) -> None:
    with closing(_get_conn()) as log_conn:
        db.log_decision(log_conn, action, rec_id, operator, details)


def _background_thread_state_key(name: str) -> str:
    return f"{BACKGROUND_THREAD_STATE_APP_KEY_PREFIX}{str(name or '').strip().lower()}"


def _set_background_thread_state(name: str, state: str, **fields: Any) -> None:
    payload = {
        "name": str(name or "").strip().lower(),
        "state": str(state or "unknown"),
        "updated_ts": int(time.time()),
        **fields,
    }
    try:
        with closing(_get_conn()) as conn:
            db.set_app_config_json(conn, _background_thread_state_key(name), payload)
    except Exception:
        logger.warning("background thread state persist failed for %s", name, exc_info=True)


def _get_app_config_mapping(conn, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = db.get_app_config_json(conn, key, default=None)
    if isinstance(payload, dict):
        return dict(payload)
    return dict(default or {})


def _get_background_thread_state(conn, name: str) -> dict[str, Any]:
    out = _get_app_config_mapping(conn, _background_thread_state_key(name), default={})
    updated_ts = int(out.get("updated_ts") or 0)
    out["age_sec"] = None if updated_ts <= 0 else max(0, int(time.time()) - updated_ts)
    return out


def _log_background_thread_error(name: str, exc: Exception) -> None:
    action = BACKGROUND_THREAD_ERROR_ACTIONS.get(str(name or "").strip().lower())
    if not action:
        return
    details: dict[str, Any] = {
        "component": str(name or "").strip().lower(),
        "stage": "background_supervisor",
        "err": str(exc),
        "err_type": exc.__class__.__name__,
    }
    if action == "COLLECT_ERROR":
        details = {
            "venue": "*",
            "symbol": "UNKNOWN",
            "field": "background_thread",
            **details,
        }
    try:
        _log_decision_fresh(action, None, None, details)
    except Exception:
        logger.warning("background thread error log failed for %s", name, exc_info=True)


BACKGROUND_RUNTIME_LOCK_KEYS = {
    "collector": "runtime:collector",
    "backfill": "runtime:backfill",
    "futures_meta": "runtime:futures_meta",
    "sentiment": "runtime:sentiment",
    "reco": "runtime:reco",
    "outcomes": "runtime:outcomes",
    "llm_reviewer": "runtime:llm_reviewer",
}


def _collector_runtime_lock_ttl_sec() -> int:
    return max(120, int(settings.collect_interval_sec) * 20)


def _runtime_handover_grace_sec() -> int:
    # A clean restart must be allowed to wait until a prior collector lease can
    # expire, plus one scheduling interval, without being reported as a crash.
    return max(
        int(settings.stale_data_max_sec),
        _collector_runtime_lock_ttl_sec() + int(settings.collect_interval_sec),
    )


def _release_component_runtime_lock(name: str) -> None:
    lock_key = BACKGROUND_RUNTIME_LOCK_KEYS.get(str(name or "").strip().lower())
    if not lock_key:
        return
    try:
        with closing(_get_lock_conn()) as lock_conn:
            db.release_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER)
    except Exception:
        logger.warning("runtime lock release failed for %s", name, exc_info=True)


def _run_supervised_background_target(
    name: str,
    target,
    *,
    restart_delay_sec: float = BACKGROUND_THREAD_RESTART_DELAY_SEC,
    max_restarts: int | None = None,
    sleep_fn=time.sleep,
    treat_return_as_error: bool = True,
) -> None:
    restart_count = 0
    consecutive_failures = 0
    while not _BACKGROUND_STOP_EVENT.is_set():
        start_ts = int(time.time())
        _set_background_thread_state(
            name,
            "running",
            last_start_ts=start_ts,
            restart_count=int(restart_count),
            consecutive_failures=int(consecutive_failures),
            owner=RUNTIME_OWNER,
        )
        try:
            target()
            if treat_return_as_error and not _BACKGROUND_STOP_EVENT.is_set():
                raise RuntimeError(f"{name} background loop returned unexpectedly")
            _set_background_thread_state(
                name,
                "stopped",
                last_stop_ts=int(time.time()),
                restart_count=int(restart_count),
                consecutive_failures=0,
                owner=RUNTIME_OWNER,
            )
            return
        except Exception as exc:
            restart_count += 1
            consecutive_failures += 1
            logger.exception("background thread crashed: %s", name)
            _set_background_thread_state(
                name,
                "error",
                last_error_ts=int(time.time()),
                restart_count=int(restart_count),
                consecutive_failures=int(consecutive_failures),
                error=str(exc),
                error_type=exc.__class__.__name__,
                owner=RUNTIME_OWNER,
            )
            _log_background_thread_error(name, exc)
            if max_restarts is not None and restart_count > int(max_restarts):
                return
            try:
                sleep_fn(max(0.0, float(restart_delay_sec)))
            except Exception:
                return
        finally:
            _release_component_runtime_lock(name)


def _make_runtime_lock_heartbeat(lock_key: str, lock_conn_factory=None):
    def _heartbeat() -> bool:
        factory = lock_conn_factory or _get_lock_conn
        try:
            with closing(factory()) as lock_conn:
                return bool(db.heartbeat_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER))
        except Exception:
            logger.warning("runtime lock heartbeat failed", exc_info=True)
            return False
    return _heartbeat


def _collect_hot_once(conn, client: BybitPublicClient, venue: str, symbols: list[str], heartbeat, max_workers: int) -> dict[str, Any]:
    try:
        return collect_once(
            conn,
            client,
            venue,
            symbols,
            heartbeat=heartbeat,
            max_workers=max_workers,
            api_fetch_tfs=(60,),
            allow_derived_bootstrap=False,
        )
    except TypeError as exc:
        msg = str(exc)
        if "api_fetch_tfs" not in msg and "allow_derived_bootstrap" not in msg:
            raise
        return collect_once(
            conn,
            client,
            venue,
            symbols,
            heartbeat=heartbeat,
            max_workers=max_workers,
        )


def _warmup_status_payload(conn) -> dict[str, Any]:
    return _collector_warmup_status(conn)


def _collect_backfill_cycle(conn, client: BybitPublicClient, venue: str, symbols: list[str], heartbeat, max_workers: int) -> dict[str, Any]:
    per_tf_budget = int(getattr(settings, "backfill_per_tf_budget", 0) or 0)
    if per_tf_budget > 0:
        per_tf_budget = min(per_tf_budget, max(1, len(symbols)))
    if per_tf_budget <= 0 and bool(getattr(settings, "backfill_full_sweep_on_warmup", False)):
        try:
            warmup = _warmup_status_payload(conn)
        except Exception:
            warmup = {"ready": False}
        if not bool(warmup.get("ready")):
            per_tf_budget = max(1, len(symbols))
    try:
        kwargs = {
            "heartbeat": heartbeat,
            "max_workers": max_workers,
        }
        if per_tf_budget > 0:
            kwargs["per_tf_budget"] = per_tf_budget
        return collect_backfill_once(
            conn,
            client,
            venue,
            symbols,
            **kwargs,
        )
    except TypeError as exc:
        msg = str(exc)
        legacy_arg_markers = (
            "unexpected keyword argument 'heartbeat'",
            'unexpected keyword argument "heartbeat"',
            "unexpected keyword argument 'max_workers'",
            'unexpected keyword argument "max_workers"',
            "unexpected keyword argument 'per_tf_budget'",
            'unexpected keyword argument "per_tf_budget"',
            "positional arguments but",
        )
        if not any(marker in msg for marker in legacy_arg_markers):
            raise
        # Test doubles may still expose the legacy collector signature.
        return {"venue": venue, "symbols_total": len(symbols), "legacy_stub": True}


def _collector_warmup_status(conn) -> dict[str, Any]:
    status = db.get_recommender_warmup_status(
        conn,
        settings.symbols_linear,
        stale_sec=int(settings.stale_data_max_sec),
        min_rows_per_tf=80,
        required_tfs=(60, 900, 1800, 3600, 14400, 86400),
        active_venues=list(settings.venues),
    )
    min_ratio = max(0.0, min(1.0, float(getattr(settings, "reco_warmup_min_ready_ratio", 0.85) or 0.85)))
    min_symbols = max(1, int(getattr(settings, "reco_warmup_min_ready_symbols", 1) or 1))
    venue_summaries = status.get("venues") if isinstance(status.get("venues"), list) else []
    ready = True
    for item in venue_summaries:
        total = int(item.get("symbols_total") or 0)
        ready_symbols = int(item.get("ready_symbols") or 0)
        if total <= 0:
            continue
        required_ready = max(min_symbols, int(math.ceil(total * min_ratio)))
        if ready_symbols < min(required_ready, total):
            ready = False
            break
    status["ready"] = bool(ready)
    status["min_ready_ratio"] = min_ratio
    status["min_ready_symbols"] = min_symbols
    return status


def _load_collector_warmup_status(conn, *, recompute_if_missing: bool = False) -> dict[str, Any]:
    status = _get_app_config_mapping(conn, "collector_warmup", default={})
    if status:
        return dict(status)
    if not recompute_if_missing:
        return {}
    try:
        computed = _collector_warmup_status(conn)
    except Exception:
        logger.warning("collector warmup fallback compute failed", exc_info=True)
        return {
            "ready": False,
            "reason": "collector_warmup_unavailable",
            "derived_on_read": True,
        }
    if isinstance(computed, dict):
        computed = dict(computed)
        computed["derived_on_read"] = True
        return computed
    return {
        "ready": False,
        "reason": "collector_warmup_unavailable",
        "derived_on_read": True,
    }


def _symbol_health_boot_grace_sec() -> int:
    return max(int(settings.stale_data_max_sec), int(settings.collect_interval_sec) * 3)


def _collector_completed_cycle_this_process(collector_last_cycle: dict[str, Any] | None) -> bool:
    if not isinstance(collector_last_cycle, dict):
        return False
    try:
        started_ts = int(collector_last_cycle.get("started_ts") or 0)
    except Exception:
        started_ts = 0
    return started_ts >= PROCESS_STARTED_TS


def _load_symbol_health(conn) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    active_venues = list(getattr(settings, "venues", []) or [])
    warmup = _load_collector_warmup_status(conn, recompute_if_missing=True)
    collector_last_cycle = _get_app_config_mapping(conn, "collector_last_cycle", default={})
    items = db.get_symbol_health(
        conn,
        settings.symbols_linear,
        stale_sec=settings.stale_data_max_sec,
        active_venues=active_venues,
    )
    boot_grace_sec = _symbol_health_boot_grace_sec()
    boot_grace_active = (
        int(time.time()) < PROCESS_STARTED_TS + boot_grace_sec
        and not _collector_completed_cycle_this_process(collector_last_cycle)
    )
    if boot_grace_active:
        normalized: list[dict[str, Any]] = []
        for item in items:
            row = dict(item)
            if row.get("status") == "stale" and row.get("last_candle_ts") and row.get("last_ticker_ts"):
                data_age_sec = row.get("data_age_sec")
                if data_age_sec is not None and int(data_age_sec) <= int(boot_grace_sec):
                    row["raw_status"] = "stale"
                    row["status"] = "ok"
                    row["status_reason"] = "boot_grace"
            normalized.append(row)
        items = normalized
    return items, {
        "venues": active_venues,
        "warmup": warmup,
        "collector_last_cycle": collector_last_cycle,
        "boot_grace_active": bool(boot_grace_active),
        "boot_grace_sec": int(boot_grace_sec),
        "process_started_ts": int(PROCESS_STARTED_TS),
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    _BACKGROUND_STOP_EVENT.clear()
    _start_background_thread("collector", partial(_run_supervised_background_target, "collector", _collector_thread))
    _start_background_thread("backfill", partial(_run_supervised_background_target, "backfill", _backfill_thread))
    _start_background_thread("futures_meta", partial(_run_supervised_background_target, "futures_meta", _futures_meta_thread))
    _start_background_thread("sentiment", partial(_run_supervised_background_target, "sentiment", _sentiment_thread))
    _start_background_thread("reco", partial(_run_supervised_background_target, "reco", _reco_thread))
    _start_background_thread("outcomes", partial(_run_supervised_background_target, "outcomes", _outcome_thread))
    if bool(getattr(settings, "llm_reviewer_enabled", False)):
        _start_background_thread("llm_reviewer", partial(_run_supervised_background_target, "llm_reviewer", _llm_reviewer_thread))
    else:
        # Когда reviewer выключен конфигом, его воркер не должен стартовать вообще.
        # Иначе supervised-wrapper интерпретирует штатный return как падение потока,
        # бесконечно пишет "background thread crashed" и засоряет статус/логи.
        _set_background_thread_state("llm_reviewer", "disabled", owner=RUNTIME_OWNER)
        try:
            with closing(_get_conn()) as conn:
                db.set_app_config_json(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, {
                    "enabled": False,
                    "state": "disabled",
                    "updated_ts": int(time.time()),
                })
        except Exception:
            logger.warning("llm reviewer disabled state persist failed", exc_info=True)
    try:
        yield
    finally:
        _BACKGROUND_STOP_EVENT.set()
        _join_background_threads()


app = FastAPI(title="Bybit Recommender (Scenario B)", version="1.0.76", lifespan=lifespan)

static_dir = Path(__file__).resolve().parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response



class UpdateRiskLimitsRequest(BaseModel):
    version: str
    limits: dict[str, Any]


class SentimentPointRequest(BaseModel):
    scope: str = Field(..., pattern="^(global|symbol|topic)$")
    key: str
    ts: StrictInt | None = Field(None, gt=0)
    sentiment: StrictFloat = Field(..., ge=-1.0, le=1.0, allow_inf_nan=False)
    velocity: StrictFloat = Field(0.0, allow_inf_nan=False)
    volume: StrictInt = Field(1, ge=0)
    sources: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class RecoActionRequest(BaseModel):
    action: str
    operator: str | None = None


class BotStopRequest(BaseModel):
    operator: str | None = None
    reason: str | None = None


class BotTradeRequest(BaseModel):
    trade_id: str | None = None
    ts: StrictInt | None = Field(None, gt=0)
    pnl: StrictFloat = Field(..., allow_inf_nan=False, description="Gross realized PnL before costs")
    fee: StrictFloat = Field(0.0, ge=0.0, allow_inf_nan=False, description="Trading fee expense")
    funding: StrictFloat = Field(0.0, allow_inf_nan=False, description="Signed funding: positive receipt, negative payment")
    slippage: StrictFloat = Field(0.0, ge=0.0, allow_inf_nan=False, description="Non-negative fill-quality diagnostic; not subtracted again from fill-based gross PnL")
    operator: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    stop_bot: bool = False


class ExecutionEvidenceRequest(BaseModel):
    event_id: str | None = None
    event_type: str = Field(..., pattern="^(execution|funding)$")
    source: str = Field(..., pattern="^(bybit_execution|bybit_transaction_log)$")
    external_event_id: str
    external_order_id: str | None = None
    ts: StrictInt | None = Field(None, gt=0)
    side: str | None = Field(None, pattern="^(Buy|Sell|buy|sell)$")
    qty: StrictFloat | None = Field(None, gt=0.0, allow_inf_nan=False)
    price: StrictFloat | None = Field(None, gt=0.0, allow_inf_nan=False)
    order_price: StrictFloat | None = Field(None, gt=0.0, allow_inf_nan=False)
    benchmark_price: StrictFloat | None = Field(None, gt=0.0, allow_inf_nan=False)
    benchmark_ts: StrictInt | None = Field(None, gt=0)
    benchmark_source: str | None = Field(None, pattern="^(pre_submit_mid|pre_submit_opposite|decision_reference)$")
    gross_pnl: StrictFloat = Field(0.0, allow_inf_nan=False)
    fee: StrictFloat = Field(0.0, allow_inf_nan=False, description="Signed trading fee: positive expense, negative rebate")
    funding: StrictFloat = Field(0.0, allow_inf_nan=False, description="Signed funding cashflow: positive receipt, negative payment")
    slippage: StrictFloat | None = Field(None, ge=0.0, allow_inf_nan=False, description="Adverse benchmark-to-fill deviation derived from side/qty/benchmark_price/price; diagnostic only")
    currency: str = "USDT"
    operator: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ExecutionReconciliationRequest(BaseModel):
    reconciliation_id: str | None = None
    source: str = Field(..., pattern="^bybit_private_reconciliation$")
    external_snapshot_id: str
    ts: StrictInt | None = Field(None, gt=0)
    position_qty: StrictFloat = Field(..., allow_inf_nan=False)
    open_order_count: StrictInt = Field(..., ge=0)
    execution_event_count: StrictInt = Field(..., ge=0)
    funding_event_count: StrictInt = Field(..., ge=0)
    realized_pnl_gross: StrictFloat = Field(..., allow_inf_nan=False)
    fee: StrictFloat = Field(..., allow_inf_nan=False)
    funding: StrictFloat = Field(..., allow_inf_nan=False)
    currency: str = "USDT"
    complete: StrictBool
    operator: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (static_dir / "index.html").read_text(encoding="utf-8")


def _require_admin_key(x_api_key: str | None, request: Request | None = None) -> None:
    client_host = None
    if request is not None:
        try:
            client = getattr(request, "client", None)
            client_host = getattr(client, "host", None)
        except Exception:
            client_host = None
    if not is_authorized(settings.admin_api_key, x_api_key, client_host=client_host):
        if settings.admin_api_key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
        raise HTTPException(status_code=401, detail="mutating API requires ADMIN_API_KEY or loopback access")


def _is_supported_execution_direction(bot_type: str, venue: str, direction: str) -> bool:
    return bot_type == "futures_grid" and venue == "linear" and direction in ("neutral", "long", "short")


def _execution_symbol_direction_conflict_blocks(conn, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Fail closed when a one-way Linear USDT symbol already has another direction running.

    The execution layer models Bybit Linear USDT futures grids as one-way/isolated
    bots unless hedge-mode support is added explicitly. ``gate_candidate`` enforces
    numeric symbol caps, but operators can intentionally raise ``max_symbol_bots``.
    That must not allow a second local bot with an incompatible direction on the
    same venue/symbol, because protective TP/SL, exposure and reconciliation would
    no longer have a single directional source of truth.
    """
    venue = str(rec.get("venue") or "").strip().lower()
    symbol = str(rec.get("symbol") or "").strip().upper()
    direction = str(rec.get("direction") or "").strip().lower()
    bot_type = str(rec.get("bot_type") or "").strip()
    if bot_type != "futures_grid" or venue != "linear" or direction not in {"neutral", "long", "short"}:
        return []

    publication_root = str(rec.get("publication_root_rec_id") or rec.get("rec_id") or "").strip()
    blocks: list[dict[str, Any]] = []
    for bot in db.list_bot_instances(conn, status="running", limit=10000):
        if str(bot.get("bot_type") or "").strip() != "futures_grid":
            continue
        if str(bot.get("venue") or "").strip().lower() != venue:
            continue
        if str(bot.get("symbol") or "").strip().upper() != symbol:
            continue

        mode = bot.get("mode") if isinstance(bot.get("mode"), dict) else {}
        existing_direction = str(mode.get("direction") or "").strip().lower()
        if existing_direction not in {"neutral", "long", "short"}:
            blocks.append(
                {
                    "code": "EXISTING_SYMBOL_DIRECTION_UNKNOWN",
                    "msg": (
                        f"{venue}:{symbol} already has running bot {bot.get('bot_id')} with unknown direction; "
                        "one-way Linear USDT execution cannot prove TP/SL and exposure semantics."
                    ),
                    "bot_id": bot.get("bot_id"),
                    "existing_direction": existing_direction or None,
                    "candidate_direction": direction,
                }
            )
            continue

        existing_root = str(bot.get("publication_root_rec_id") or bot.get("origin_rec_id") or "").strip()
        same_publication_root = bool(publication_root and existing_root == publication_root)
        if same_publication_root and existing_direction == direction:
            # Safe idempotent re-attachment: same chain, same one-way direction.
            continue

        if existing_direction != direction:
            blocks.append(
                {
                    "code": "OPPOSITE_SYMBOL_DIRECTION_RUNNING",
                    "msg": (
                        f"{venue}:{symbol} already has running {existing_direction} bot {bot.get('bot_id')}; "
                        f"cannot start or reattach {direction} bot without explicit hedge-mode support."
                    ),
                    "bot_id": bot.get("bot_id"),
                    "existing_direction": existing_direction,
                    "candidate_direction": direction,
                    "same_publication_root": same_publication_root,
                }
            )
    return blocks


def _running_publication_root_bot_direction_blocks(existing_bot: dict[str, Any], rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Block idempotent publication-chain reuse if the live bot is another side.

    Reusing a live bot for a later recommendation in the same chain is only safe
    while the executable one-way direction is unchanged. If the chain flips from
    long to short (or directional to neutral), returning the existing bot would
    falsely mark a different side as executed and hide exposure/TP/SL conflict.
    """
    if not existing_bot:
        return []
    if str(rec.get("bot_type") or "").strip() != "futures_grid":
        return []
    if str(rec.get("venue") or "").strip().lower() != "linear":
        return []
    candidate_direction = str(rec.get("direction") or "").strip().lower()
    if candidate_direction not in {"neutral", "long", "short"}:
        return []

    mode = existing_bot.get("mode") if isinstance(existing_bot.get("mode"), dict) else {}
    existing_direction = str(mode.get("direction") or "").strip().lower()
    if existing_direction not in {"neutral", "long", "short"}:
        return [
            {
                "code": "EXISTING_CHAIN_DIRECTION_UNKNOWN",
                "msg": (
                    f"publication-chain bot {existing_bot.get('bot_id')} has unknown direction; "
                    "cannot prove one-way TP/SL/exposure semantics for idempotent reattach."
                ),
                "bot_id": existing_bot.get("bot_id"),
                "existing_direction": existing_direction or None,
                "candidate_direction": candidate_direction,
            }
        ]
    if existing_direction != candidate_direction:
        return [
            {
                "code": "PUBLICATION_CHAIN_DIRECTION_CHANGED",
                "msg": (
                    f"publication-chain bot {existing_bot.get('bot_id')} is {existing_direction}, "
                    f"but candidate recommendation is {candidate_direction}; reattach would mark the wrong side executed."
                ),
                "bot_id": existing_bot.get("bot_id"),
                "existing_direction": existing_direction,
                "candidate_direction": candidate_direction,
            }
        ]
    return []


def _prefetch_execution_bybit_meta(conn, rec_id: str) -> dict[str, Any]:
    """Заранее подтягивает metadata Bybit без удержания write-lock SQLite.

    Execute-path сериализуется через ``BEGIN IMMEDIATE``. Если внутри этой
    транзакции пойти в сеть за instrument metadata, то медленный Bybit/прокси
    блокирует все локальные writer-потоки: collector, recommender, sentiment,
    operator actions. Поэтому внешний fetch делаем до захвата write-lock, а
    внутри критической секции используем уже готовый snapshot metadata.
    """
    rec = db.get_recommendation_by_id(conn, rec_id)
    if not rec:
        return {}
    venue = str(rec.get("venue") or "")
    symbol = str(rec.get("symbol") or "")
    if not venue or not symbol:
        return {}
    try:
        meta = _fetch_bybit_instrument_meta(venue, symbol)
    except Exception:
        return {}
    return dict(meta) if isinstance(meta, dict) else {}


def _materialize_bot_from_rec(conn, rec_id: str, operator: str | None = None) -> tuple[dict[str, Any], bool]:
    bybit_meta = _prefetch_execution_bybit_meta(conn, rec_id)
    db.begin_immediate(conn)
    rec = db.get_recommendation_by_id(conn, rec_id, for_update=True)
    if not rec:
        raise HTTPException(status_code=404, detail="rec_id not found")

    current_status = str(rec.get("status") or "")
    ttl_sec = max(0, _safe_int(rec.get("ttl_sec"), 0))
    rec_ts = max(0, _safe_int(rec.get("ts"), 0))
    is_expired = bool(ttl_sec > 0 and rec_ts > 0 and int(time.time()) > rec_ts + ttl_sec)
    existing = db.get_bot_by_origin_rec(conn, rec_id)
    if existing:
        if current_status != "executed":
            db.update_recommendation_status(conn, rec_id, "executed", operator, commit=False)
            conn.commit()
        else:
            _rollback_quietly(conn)
        return existing, True

    # Идемпотентный reuse живого бота допустим только для уже исполненной записи
    # или для по-прежнему исполнимого actionable recommendation. Иначе можно было
    # бы тихо перевести `pending`/`ignored`/`expired` запись в `executed` только
    # потому, что в той же publication-chain уже существует running bot.
    if current_status != "executed":
        if is_expired:
            db.update_recommendation_status(conn, rec_id, "expired", operator)
            raise HTTPException(status_code=409, detail="recommendation already expired")
        if current_status in {"blocked", "no_trade", "suppressed", "pending", "expired", "ignored"}:
            raise HTTPException(status_code=409, detail=f"recommendation status={current_status} cannot be executed")
        if not _is_supported_execution_direction(str(rec.get("bot_type") or ""), str(rec.get("venue") or ""), str(rec.get("direction") or "")):
            raise HTTPException(status_code=409, detail="recommendation direction is not executable for this bot_type/venue")
        freshness_blocks = _execution_recommendation_freshness_blocks(conn, rec, now_ts=int(time.time()))
        if freshness_blocks:
            codes = {str(block.get("code") or "") for block in freshness_blocks if isinstance(block, dict)}
            if codes & {"RECOMMENDATION_ROW_EXPIRED", "PUBLICATION_CHAIN_TOO_OLD"}:
                db.update_recommendation_status(conn, rec_id, "expired", operator)
            timestamp_invalid = bool(codes & {
                "RECOMMENDATION_TIMESTAMP_INVALID",
                "PUBLICATION_CHAIN_TIMESTAMP_INVALID",
            })
            action = "EXECUTION_INVALID_RECOMMENDATION_TIMESTAMP_BLOCKED" if timestamp_invalid else "EXECUTION_STALE_RECOMMENDATION_BLOCKED"
            db.log_decision(conn, action, rec_id, operator, {"blocks": freshness_blocks})
            detail = "recommendation timestamp is invalid" if timestamp_invalid else "recommendation publication chain already expired"
            raise HTTPException(status_code=409, detail=detail)

    publication_root_rec_id = str(rec.get("publication_root_rec_id") or rec_id).strip() or rec_id
    if publication_root_rec_id:
        # Reuse only a live bot from the same publication chain. Re-attaching a later
        # `active` recommendation to a historical *stopped* bot makes the API claim the
        # signal was executed while leaving the operator with no running position.
        chain_existing = db.get_bot_by_publication_root(conn, publication_root_rec_id, status="running")
        if chain_existing:
            chain_direction_blocks = _running_publication_root_bot_direction_blocks(chain_existing, rec)
            if chain_direction_blocks:
                db.log_decision(
                    conn,
                    "EXECUTION_PUBLICATION_CHAIN_DIRECTION_BLOCKED",
                    rec_id,
                    operator,
                    {"blocks": chain_direction_blocks},
                    commit=False,
                )
                codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in chain_direction_blocks)
                raise HTTPException(status_code=409, detail=f"execution blocked by publication-chain direction state: {codes}")
            if current_status != "executed":
                db.update_recommendation_status(conn, rec_id, "executed", operator, commit=False)
                conn.commit()
            else:
                _rollback_quietly(conn)
            return chain_existing, True

    if current_status == "executed":
        raise HTTPException(status_code=409, detail="recommendation marked executed but no running bot exists for this publication chain")

    if not _is_supported_execution_direction(str(rec.get("bot_type") or ""), str(rec.get("venue") or ""), str(rec.get("direction") or "")):
        raise HTTPException(status_code=409, detail="recommendation direction is not executable for this bot_type/venue")

    # Повторно проверяем риск-лимиты в момент operator action.
    # Recommendation-time gate — это только снимок; к моменту подтверждения
    # могли измениться число активных ботов, symbol-cap, cooldown и дневной DD.
    limits = get_risk_limits(conn, settings.risk_limits)
    runtime_risk_status = compute_risk_status(conn, limits)
    exec_blocks = gate_candidate(conn, rec["venue"], rec["symbol"], limits, cached_status=runtime_risk_status)
    if exec_blocks:
        db.log_decision(conn, "EXECUTION_BLOCKED", rec_id, operator, {"blocks": exec_blocks})
        codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in exec_blocks)
        raise HTTPException(status_code=409, detail=f"execution blocked by current risk limits: {codes}")

    direction_conflict_blocks = _execution_symbol_direction_conflict_blocks(conn, rec)
    if direction_conflict_blocks:
        db.log_decision(
            conn,
            "EXECUTION_SYMBOL_DIRECTION_CONFLICT_BLOCKED",
            rec_id,
            operator,
            {"blocks": direction_conflict_blocks},
        )
        codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in direction_conflict_blocks)
        raise HTTPException(status_code=409, detail=f"execution blocked by current symbol direction state: {codes}")

    rec_for_execution = _snap_reco_payload_to_bybit_meta(rec, bybit_meta)

    preflight = _execution_preflight(
        conn,
        rec_for_execution,
        now_ts=int(time.time()),
        bybit_meta=bybit_meta,
        risk_limits=limits,
        risk_status=runtime_risk_status,
    )
    preflight_blocks = list(preflight.get("blocks") or [])
    if preflight_blocks:
        db.log_decision(
            conn,
            "EXECUTION_PRECHECK_BLOCKED",
            rec_id,
            operator,
            {
                "blocks": preflight_blocks,
                "strategy_health": preflight.get("strategy_health"),
                "market_shock": preflight.get("market_shock"),
                "fast_veto": preflight.get("fast_veto"),
                "bybit_validation": preflight.get("bybit_validation"),
            },
        )
        codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in preflight_blocks)
        raise HTTPException(status_code=409, detail=f"execution blocked by preflight checks: {codes}")

    size_risk_blocks = _execution_runtime_size_risk_blocks(rec_for_execution, limits)
    if size_risk_blocks:
        db.log_decision(conn, "EXECUTION_SIZE_RISK_BLOCKED", rec_id, operator, {"blocks": size_risk_blocks})
        codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in size_risk_blocks)
        raise HTTPException(status_code=409, detail=f"execution blocked by runtime size/leverage risk caps: {codes}")

    bot = {
        "bot_id": f"B-{int(time.time())}-{rec['symbol']}-{secrets.token_hex(4)}",
        "started_ts": int(time.time()),
        "stopped_ts": None,
        "venue": rec["venue"],
        "symbol": rec["symbol"],
        "bot_type": rec["bot_type"],
        "mode": {
            "account_mode": rec["account_mode"],
            "margin_mode": rec["margin_mode"],
            "direction": rec["direction"],
        },
        "params": rec_for_execution.get("params", rec["params"]),
        "state": {
            "created_from_rec_id": rec_id,
            "operator": operator,
            "trade_count": 0,
            "realized_pnl": 0.0,
            "realized_pnl_gross": 0.0,
            "realized_pnl_net": 0.0,
            "realized_fee": 0.0,
            "last_trade_ts": None,
            "execution_preflight": {
                "checked_ts": int(time.time()),
                "strategy_health": preflight.get("strategy_health"),
                "market_shock": preflight.get("market_shock"),
                "fast_veto": preflight.get("fast_veto"),
                "bybit_validation": preflight.get("bybit_validation"),
            },
        },
        "status": "running",
        "origin_rec_id": rec_id,
        "publication_root_rec_id": publication_root_rec_id,
    }
    try:
        insert_result = db.insert_bot_instance(conn, bot, commit=False)
        if insert_result == "duplicate_origin":
            existing = db.get_bot_by_origin_rec(conn, rec_id)
            if existing:
                if rec.get("status") != "executed":
                    db.update_recommendation_status(conn, rec_id, "executed", operator)
                return existing, True
            raise HTTPException(status_code=409, detail="bot creation conflicted with an existing origin_rec_id")
        if insert_result == "duplicate_publication_root_running":
            existing = db.get_bot_by_publication_root(conn, publication_root_rec_id, status="running")
            if existing:
                chain_direction_blocks = _running_publication_root_bot_direction_blocks(existing, rec)
                if chain_direction_blocks:
                    db.log_decision(
                        conn,
                        "EXECUTION_PUBLICATION_CHAIN_DIRECTION_BLOCKED",
                        rec_id,
                        operator,
                        {"blocks": chain_direction_blocks},
                        commit=False,
                    )
                    codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in chain_direction_blocks)
                    raise HTTPException(status_code=409, detail=f"execution blocked by publication-chain direction state: {codes}")
                if rec.get("status") != "executed":
                    db.update_recommendation_status(conn, rec_id, "executed", operator)
                    conn.commit()
                else:
                    _rollback_quietly(conn)
                return existing, True
            raise HTTPException(status_code=409, detail="bot creation conflicted with an existing running publication chain")

        status_updated = db.update_recommendation_status(conn, rec_id, "executed", operator, commit=False)
        if not status_updated:
            raise HTTPException(status_code=409, detail="recommendation status changed during execution")
        db.log_decision(
            conn,
            "BOT_STARTED",
            rec_id,
            operator,
            {
                "bot_id": bot["bot_id"],
                "symbol": bot["symbol"],
                "bot_type": bot["bot_type"],
                "insert_result": insert_result,
            },
            commit=False,
        )
        conn.commit()
    except HTTPException:
        _rollback_quietly(conn)
        raise
    except Exception:
        _rollback_quietly(conn)
        raise
    created = db.get_bot_instance(conn, bot["bot_id"])
    return created or bot, False


def _resolve_recommendation_snapshot_ts(
    conn,
    venue: str | None,
    snapshot: str,
    *,
    min_conf: float,
    strict_min_conf: bool,
    requested_statuses: list[str] | None = None,
) -> int | None:
    mode = str(snapshot or "latest_operator").strip().lower()
    if mode == "latest":
        return db.get_latest_reco_ts(conn, venue=venue)
    recent = db.list_recent_reco_snapshot_ts(conn, venue=venue, limit=50)
    if not recent:
        return None
    if mode == "latest_operator":
        # Always show the actual latest publication cycle. Filters are applied
        # inside that cycle; they must never make the UI search backwards and
        # silently resurrect an older recommendation as if it were current.
        return recent[0]

    requested_statuses = list(dict.fromkeys(requested_statuses or []))
    actionable_statuses = ["recommended", "active"]
    requested_has_actionable = any(status in actionable_statuses for status in requested_statuses)
    requested_non_actionable_only = bool(requested_statuses) and not requested_has_actionable

    if requested_non_actionable_only:
        for ts in recent:
            matching_count = db.count_recommendations_for_statuses(
                conn,
                venue=venue,
                min_conf=min_conf,
                statuses=requested_statuses,
                snapshot_ts=ts,
                strict_min_conf=strict_min_conf,
            )
            if matching_count > 0:
                return ts
        return recent[0]

    latest_visible_ts: int | None = None
    latest_llm_ready_ts: int | None = None
    for ts in recent:
        visible_count = db.count_visible_recommendations(
            conn,
            venue=venue,
            min_conf=min_conf,
            snapshot_ts=ts,
            strict_min_conf=strict_min_conf,
        )
        if visible_count > 0 and latest_visible_ts is None:
            latest_visible_ts = ts
        if visible_count > 0:
            llm_counts = db.get_llm_status_counts(
                conn,
                venue=venue,
                min_conf=min_conf,
                statuses=actionable_statuses,
                snapshot_ts=ts,
                strict_min_conf=strict_min_conf,
            )
            if (llm_counts.get("ok", 0) + llm_counts.get("error", 0) + llm_counts.get("skipped", 0)) > 0:
                latest_llm_ready_ts = ts
                break
    if mode == "latest_visible":
        return latest_visible_ts if latest_visible_ts is not None else recent[0]
    if mode == "latest_llm_ready":
        return latest_llm_ready_ts if latest_llm_ready_ts is not None else (latest_visible_ts if latest_visible_ts is not None else recent[0])
    raise HTTPException(status_code=400, detail="unsupported snapshot mode")


@app.get("/api/v1/recommendations")
def api_recommendations(
    venue: str | None = None,
    top_n: int = 20,
    min_conf: float | None = None,
    show_recommended: bool = True,
    show_pending: bool = False,
    show_blocked: bool = False,
    show_no_trade: bool = False,
    show_suppressed: bool = False,
    snapshot: str = "latest_operator",
    collapse_chains: bool = True,
) -> dict[str, Any]:
    top_n = _bounded_limit(top_n, default=20, max_value=200)
    with closing(_get_conn()) as conn:
        db.expire_stale_recommendations(conn)
        statuses: list[str] = []
        if show_recommended:
            statuses.extend(["recommended", "active"])
        if show_pending:
            statuses.append("pending")
        if show_blocked:
            statuses.append("blocked")
        if show_no_trade:
            statuses.append("no_trade")
        if show_suppressed:
            statuses.append("suppressed")

        effective_min_conf = _bounded_probability(min_conf, default=float(settings.min_conf_to_recommend))
        strict_min_conf = min_conf is not None
        fetch_statuses = _operator_fetch_statuses_for_effective_filters(statuses)
        candidate_limit = _operator_candidate_limit(top_n, collapse_chains=collapse_chains, statuses=statuses)
        snapshot_ts = _resolve_recommendation_snapshot_ts(
            conn,
            venue,
            snapshot,
            min_conf=effective_min_conf,
            strict_min_conf=strict_min_conf,
            requested_statuses=fetch_statuses,
        )
        raw_items, hidden_duplicates = _load_recommendations_for_operator_view(
            conn,
            venue=venue,
            top_n=candidate_limit,
            min_conf=effective_min_conf,
            statuses=fetch_statuses,
            snapshot_ts=snapshot_ts,
            strict_min_conf=strict_min_conf,
            collapse_chains=collapse_chains,
        )
        snapshot_age_sec = None if snapshot_ts is None else max(0, int(time.time()) - int(snapshot_ts))
        snapshot_stale_after_sec = max(180, int(settings.reco_interval_sec) * 3)
        snapshot_is_stale = bool(snapshot_age_sec is not None and snapshot_age_sec > snapshot_stale_after_sec)
        augmented_items = [_augment_reco_for_ui(item, conn=conn) for item in raw_items]
        effective_status_counts: dict[str, int] = {}
        for item in augmented_items:
            effective = str(item.get("effective_status") or item.get("status") or "unknown").strip().lower() or "unknown"
            effective_status_counts[effective] = effective_status_counts.get(effective, 0) + 1
        if snapshot_is_stale:
            for item in augmented_items:
                _apply_snapshot_stale_guard(item, snapshot_age_sec=snapshot_age_sec, stale_after_sec=snapshot_stale_after_sec)
                _ensure_effective_status(item)
            effective_status_counts = {}
            for item in augmented_items:
                effective = str(item.get("effective_status") or item.get("status") or "unknown").strip().lower() or "unknown"
                effective_status_counts[effective] = effective_status_counts.get(effective, 0) + 1
        items = _filter_operator_items_by_effective_status(augmented_items, statuses, top_n)

        status_counts = db.get_recommendation_status_counts(conn, venue=venue, snapshot_ts=snapshot_ts)
        no_trade = not any(str(item.get("effective_status") or item.get("status") or "").strip().lower() in {"recommended", "active"} for item in items)

        cur = conn.execute("SELECT regime_json FROM market_regime ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
        regime = _json_loads_mapping_or_default(row["regime_json"], {
            "vol_state": "unknown",
            "trend_state": "unknown",
            "risk_state": "unknown",
            "confidence": 0.0,
        }) if row else {
            "vol_state": "unknown",
            "trend_state": "unknown",
            "risk_state": "unknown",
            "confidence": 0.0,
        }

        llm_status_counts = db.get_llm_status_counts(
            conn,
            venue=venue,
            min_conf=effective_min_conf,
            statuses=statuses,
            snapshot_ts=snapshot_ts,
            strict_min_conf=strict_min_conf,
        )

        return {
            "ts": int(time.time()),
            "snapshot_mode": str(snapshot or "latest").strip().lower(),
            "snapshot_ts": snapshot_ts,
            "snapshot_age_sec": snapshot_age_sec,
            "snapshot_is_stale": snapshot_is_stale,
            "regime": regime,
            "items": items,
            "no_trade": no_trade,
            "publication_chain_dedupe": {
                "enabled": bool(collapse_chains),
                "hidden_duplicates": int(hidden_duplicates),
            },
            "min_conf": float(effective_min_conf),
            "status_counts": status_counts,
            "effective_status_counts": effective_status_counts,
            "llm_status_counts": llm_status_counts,
        }


@app.get("/api/v1/recommendations/history")
def api_recommendation_history(
    venue: str = "linear",
    symbol: str = "",
    bot_type: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Return a chronological publication timeline for one symbol.

    Historical rows expose persisted status and LLM state. Only the latest row is
    augmented with current runtime guards, because reconstructing historical Bybit
    metadata from today's snapshot would be misleading.
    """
    venue_norm = _normalized_filter_text(venue, default="linear", field_name="venue").lower()
    symbol_norm = _normalized_filter_text(symbol, default="", field_name="symbol").upper()
    if not symbol_norm:
        raise HTTPException(status_code=422, detail="symbol must be a non-empty string")
    bot_type_norm = str(bot_type or "").strip() or None
    bounded_limit = _bounded_limit(limit, default=500, max_value=2000)
    now = int(time.time())

    with closing(_get_conn()) as conn:
        rows, total = db.get_recommendation_history(
            conn,
            venue=venue_norm,
            symbol=symbol_norm,
            bot_type=bot_type_norm,
            limit=bounded_limit,
        )

        previous_direction: str | None = None
        previous_status: str | None = None
        previous_root: str | None = None
        items: list[dict[str, Any]] = []
        root_ids: set[str] = set()
        direction_changes = 0
        status_changes = 0
        for index, row in enumerate(rows):
            ts_value = _safe_int_or_none(row.get("ts"))
            direction = str(row.get("direction") or "neutral").strip().lower() or "neutral"
            stored_status = str(row.get("status") or "unknown").strip().lower() or "unknown"
            root_id = str(row.get("publication_root_rec_id") or row.get("rec_id") or "").strip()
            is_root = bool(row.get("is_outcome_label_root")) or root_id == str(row.get("rec_id") or "")
            direction_changed = previous_direction is not None and direction != previous_direction
            status_changed = previous_status is not None and stored_status != previous_status
            root_changed = previous_root is not None and root_id != previous_root
            direction_changes += int(direction_changed)
            status_changes += int(status_changed)
            if root_id:
                root_ids.add(root_id)

            timestamp_state = _recommendation_timestamp_state(ts_value, now_ts=now)
            item = dict(row)
            item.update({
                "timestamp_valid": bool(timestamp_state.get("valid")),
                "timestamp_invalid_reason": timestamp_state.get("invalid_reason"),
                "timestamp_future_skew_sec": timestamp_state.get("future_skew_sec"),
                "age_sec": timestamp_state.get("age_sec"),
                "stored_status": stored_status,
                "publication_kind": "root" if is_root else "update",
                "sequence": index + 1,
                "direction_changed": bool(direction_changed),
                "status_changed": bool(status_changed),
                "publication_root_changed": bool(root_changed),
            })
            items.append(item)
            previous_direction = direction
            previous_status = stored_status
            previous_root = root_id

        latest = items[-1] if items else None
        latest_effective_status = None
        latest_augmented: dict[str, Any] | None = None
        if latest is not None:
            latest_rec = db.get_recommendation_by_id(conn, str(latest.get("rec_id") or ""))
            if latest_rec:
                latest_augmented = _augment_reco_for_ui(latest_rec, conn=conn)
                latest_effective_status = str(
                    latest_augmented.get("effective_status") or latest_augmented.get("status") or "unknown"
                ).strip().lower()

        first_ts = _safe_int_or_none(items[0].get("ts")) if items else None
        latest_ts = _safe_int_or_none(latest.get("ts")) if latest else None
        return {
            "ts": now,
            "venue": venue_norm,
            "symbol": symbol_norm,
            "bot_type": bot_type_norm,
            "items": items,
            "items_total": int(total),
            "returned": len(items),
            "truncated": int(total) > len(items),
            "first_ts": first_ts,
            "latest_ts": latest_ts,
            "latest_rec_id": latest.get("rec_id") if latest else None,
            "latest_effective_status": latest_effective_status,
            "latest_operator_context": (latest_augmented or {}).get("operator_decision_context", {}),
            "publication_root_count": len(root_ids),
            "direction_change_count": int(direction_changes),
            "status_change_count": int(status_changes),
        }


@app.get("/api/v1/recommendations/{rec_id}")
def api_reco_details(rec_id: str) -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        db.expire_stale_recommendations(conn)
        r = db.get_recommendation_by_id(conn, str(rec_id))
        if not r:
            raise HTTPException(status_code=404, detail="rec_id not found")
        return _augment_reco_for_ui(r, conn=conn)


@app.get("/api/v1/risk/status")
def api_risk_status() -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        limits = get_risk_limits(conn, settings.risk_limits)
        rs = compute_risk_status(conn, limits)
        return {
            "limits": rs.limits,
            "active_bots": rs.active_bots,
            "daily_pnl": rs.daily_pnl,
            "daily_dd": rs.daily_dd,
            "cooldown_active": rs.cooldown_active,
            "symbol_bot_counts": rs.symbol_bot_counts,
        }


@app.post("/api/v1/risk/limits")
def api_update_risk_limits(req: UpdateRiskLimitsRequest, request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key, request)
    _ensure_json_payload_has_only_finite_numbers(req.limits, field_name="limits")
    effective_limits = normalize_risk_limits(req.limits, settings.risk_limits)
    with closing(_get_conn()) as conn:
        try:
            db.begin_immediate(conn)
            version = _normalized_non_empty_text(req.version, field_name="version")
            # Persist the effective normalized limits, otherwise DB/audit and runtime
            # diverge: operator sees the raw payload, while the engine silently applies
            # clamped defaults/guards on read.
            db.upsert_risk_limits(conn, version=version, limits=effective_limits, is_active=True, commit=False)
            db.log_decision(
                conn,
                "UPDATE_LIMITS",
                None,
                {"version": version, "limits": effective_limits, "raw_limits": req.limits},
                commit=False,
            )
            conn.commit()
        except Exception:
            _rollback_quietly(conn)
            raise
        return {"ok": True, "version": version, "limits": effective_limits}


@app.post("/api/v1/recommendations/{rec_id}/action")
def api_reco_action(rec_id: str, req: RecoActionRequest, request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key, request)
    operator = _normalized_optional_text(req.operator, field_name="operator")
    allowed = {"executed", "ignored"}
    if req.action not in allowed:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(allowed)}")
    with closing(_get_conn()) as conn:
        if req.action == "executed":
            # В execution-path write-lock захватывается внутри `_materialize_bot_from_rec`
            # уже после prefetch metadata Bybit. Иначе медленный upstream удерживает
            # SQLite writer-lock и блокирует collector/recommender/operator flows.
            bot, existed = _materialize_bot_from_rec(conn, rec_id, operator)
            return {"ok": True, "rec_id": rec_id, "new_status": "executed", "bot_id": bot["bot_id"], "bot": bot, "idempotent": existed}
        db.begin_immediate(conn)
        rec = db.get_recommendation_by_id(conn, rec_id, for_update=True)
        if rec is None:
            _rollback_quietly(conn)
            raise HTTPException(status_code=404, detail="rec_id not found")
        ok = db.update_recommendation_status(conn, rec_id, req.action, operator, commit=False)
        if not ok:
            _rollback_quietly(conn)
            rec = db.get_recommendation_by_id(conn, rec_id)
            if rec is None:
                raise HTTPException(status_code=404, detail="rec_id not found")
            raise HTTPException(status_code=409, detail=f"recommendation status={rec.get('status')} cannot be changed to {req.action}")
        conn.commit()
        return {"ok": True, "rec_id": rec_id, "new_status": req.action}


@app.get("/api/v1/bots")
def api_bots(status: str | None = None, limit: int = 200) -> dict[str, Any]:
    limit = _bounded_limit(limit, default=200, max_value=1000)
    with closing(_get_conn()) as conn:
        items = db.list_bot_instances(conn, status=status, limit=limit)
        return {"items": items, "count": len(items)}


@app.get("/api/v1/bots/{bot_id}")
def api_bot_details(bot_id: str) -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        bot = db.get_bot_instance(conn, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="bot_id not found")
        return bot


@app.post("/api/v1/bots/{bot_id}/stop")
def api_stop_bot(bot_id: str, req: BotStopRequest, request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key, request)
    with closing(_get_conn()) as conn:
        db.begin_immediate(conn)
        bot = db.get_bot_instance(conn, bot_id, for_update=True)
        if not bot:
            raise HTTPException(status_code=404, detail="bot_id not found")
        if str(bot.get("status") or "") == "stopped":
            _rollback_quietly(conn)
            return {"ok": True, "bot_id": bot_id, "status": "stopped", "idempotent": True}
        try:
            operator = _normalized_optional_text(req.operator, field_name="operator")
            reason = _normalized_optional_text(req.reason, field_name="reason")
            stopped_ts = int(time.time())
            ok = db.stop_bot(conn, bot_id, stopped_ts=stopped_ts, commit=False)
            if not ok:
                _rollback_quietly(conn)
                return {"ok": False, "bot_id": bot_id, "status": bot["status"]}
            state_updated = db.update_bot_state(conn, bot_id, {"stop_reason": reason, "stopped_by": operator, "stopped_ts": stopped_ts}, commit=False)
            if not state_updated:
                raise RuntimeError("bot state update failed after stop")
            db.log_decision(conn, "BOT_STOPPED", bot.get("origin_rec_id"), operator, {"bot_id": bot_id, "reason": reason}, commit=False)
            conn.commit()
        except Exception:
            _rollback_quietly(conn)
            raise
        return {"ok": True, "bot_id": bot_id, "status": "stopped", "idempotent": False}


@app.post("/api/v1/bots/{bot_id}/trades")
def api_record_trade(bot_id: str, req: BotTradeRequest, request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key, request)
    _ensure_json_payload_has_only_finite_numbers(req.meta, field_name="meta")
    with closing(_get_conn()) as conn:
        db.begin_immediate(conn)
        bot = db.get_bot_instance(conn, bot_id, for_update=True)
        if not bot:
            raise HTTPException(status_code=404, detail="bot_id not found")

        operator = _normalized_optional_text(req.operator, field_name="operator")
        trade_id = _normalized_non_empty_text(req.trade_id, field_name="trade_id") if req.trade_id is not None else f"T-{int(time.time())}-{secrets.token_hex(4)}"
        ts = req.ts or int(time.time())
        if str(bot.get("status") or "") != "running":
            existing_trade = db.get_trade_by_id(conn, trade_id) if req.trade_id else None
            if _existing_trade_matches_request(
                existing_trade,
                bot_id=bot_id,
                symbol=str(bot.get("symbol") or ""),
                ts=req.ts,
                pnl=req.pnl,
                fee=req.fee,
                funding=req.funding,
                slippage=req.slippage,
                meta=req.meta,
            ):
                trade_summary = db.get_bot_trade_summary(conn, bot_id)
                realized_pnl_gross = float(trade_summary["realized_pnl_gross"])
                realized_fee = float(trade_summary["realized_fee"])
                realized_funding = float(trade_summary["realized_funding"])
                realized_slippage = float(trade_summary["realized_slippage"])
                realized_pnl_net = float(trade_summary["realized_pnl_net"])
                _rollback_quietly(conn)
                return {
                    "ok": True,
                    "trade_id": trade_id,
                    "bot_id": bot_id,
                    "trade_count": int(trade_summary["trade_count"]),
                    "insert_result": "duplicate",
                    "realized_pnl": realized_pnl_net,
                    "realized_pnl_gross": realized_pnl_gross,
                    "realized_pnl_net": realized_pnl_net,
                    "realized_fee": realized_fee,
                    "realized_funding": realized_funding,
                    "realized_slippage": realized_slippage,
                    "bot_status": bot["status"],
                    "idempotent": True,
                }
            raise HTTPException(status_code=409, detail=f"cannot record trade for bot status={bot.get('status')}")

        current_ts = int(time.time())
        max_future_skew_sec = 300
        if ts > current_ts + max_future_skew_sec:
            raise HTTPException(status_code=409, detail=f"trade timestamp is too far in the future (> {max_future_skew_sec}s)")
        started_ts = int(bot.get("started_ts") or 0)
        if started_ts > 0 and ts < started_ts:
            raise HTTPException(status_code=409, detail="trade timestamp is earlier than bot start")
        trade = {
            "trade_id": trade_id,
            "bot_id": bot_id,
            "ts": ts,
            "symbol": bot["symbol"],
            "pnl": req.pnl,
            "fee": req.fee,
            "funding": req.funding,
            "slippage": req.slippage,
            "meta": req.meta,
        }
        try:
            insert_result = db.insert_trade(conn, trade, commit=False)
        except ValueError as exc:
            _rollback_quietly(conn)
            raise HTTPException(status_code=409, detail=str(exc))

        trade_summary = db.get_bot_trade_summary(conn, bot_id)
        realized_pnl_gross = float(trade_summary["realized_pnl_gross"])
        realized_fee = float(trade_summary["realized_fee"])
        realized_funding = float(trade_summary["realized_funding"])
        realized_slippage = float(trade_summary["realized_slippage"])
        realized_pnl_net = float(trade_summary["realized_pnl_net"])
        if insert_result == "duplicate":
            _rollback_quietly(conn)
            return {
                "ok": True,
                "trade_id": trade_id,
                "bot_id": bot_id,
                "trade_count": int(trade_summary["trade_count"]),
                "insert_result": insert_result,
                "realized_pnl": realized_pnl_net,
                "realized_pnl_gross": realized_pnl_gross,
                "realized_pnl_net": realized_pnl_net,
                "realized_fee": realized_fee,
                "realized_funding": realized_funding,
                "realized_slippage": realized_slippage,
                "bot_status": bot["status"],
                "idempotent": True,
            }
        try:
            state_updated = db.update_bot_state(
                conn,
                bot_id,
                {
                    "trade_count": int(trade_summary["trade_count"]),
                    "realized_pnl": realized_pnl_net,
                    "realized_pnl_gross": realized_pnl_gross,
                    "realized_pnl_net": realized_pnl_net,
                    "realized_fee": realized_fee,
                    "realized_funding": realized_funding,
                    "realized_slippage": realized_slippage,
                    "last_trade_ts": int(trade_summary.get("last_trade_ts") or ts),
                    "last_trade_id": trade_id,
                    "last_trade_meta": req.meta,
                    "last_operator": operator,
                },
                commit=False,
            )
            if not state_updated:
                raise RuntimeError("bot state update failed after trade")
            if req.stop_bot:
                stopped_ts = int(time.time())
                stop_ok = db.stop_bot(conn, bot_id, stopped_ts=stopped_ts, commit=False)
                if not stop_ok:
                    raise HTTPException(status_code=409, detail="bot status changed during trade finalization")
                stop_state_updated = db.update_bot_state(conn, bot_id, {"stop_reason": "stop_bot_on_trade", "stopped_by": operator, "stopped_ts": stopped_ts}, commit=False)
                if not stop_state_updated:
                    raise RuntimeError("bot state update failed after trade stop")
            db.log_decision(conn, "TRADE_RECORDED", bot.get("origin_rec_id"), operator, {"bot_id": bot_id, "trade_id": trade_id, "insert_result": insert_result, "pnl": req.pnl, "fee": req.fee, "funding": req.funding, "slippage": req.slippage, "stop_bot": req.stop_bot}, commit=False)
            conn.commit()
        except Exception:
            _rollback_quietly(conn)
            raise
        return {
            "ok": True,
            "trade_id": trade_id,
            "bot_id": bot_id,
            "trade_count": int(trade_summary["trade_count"]),
            "insert_result": insert_result,
            "realized_pnl": realized_pnl_net,
            "realized_pnl_gross": realized_pnl_gross,
            "realized_pnl_net": realized_pnl_net,
            "realized_fee": realized_fee,
            "realized_funding": realized_funding,
            "realized_slippage": realized_slippage,
            "bot_status": "stopped" if req.stop_bot else bot["status"],
        }


@app.post("/api/v1/bots/{bot_id}/execution-evidence")
def api_record_execution_evidence(
    bot_id: str,
    req: ExecutionEvidenceRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Record immutable Bybit execution/funding evidence without placing orders."""
    _require_admin_key(x_api_key, request)
    _ensure_json_payload_has_only_finite_numbers(req.meta, field_name="meta")
    with closing(_get_conn()) as conn:
        db.begin_immediate(conn)
        bot = db.get_bot_instance(conn, bot_id, for_update=True)
        if not bot:
            raise HTTPException(status_code=404, detail="bot_id not found")

        event_id = _normalized_non_empty_text(req.event_id, field_name="event_id") if req.event_id is not None else f"EV-{int(time.time())}-{secrets.token_hex(5)}"
        external_event_id = _normalized_non_empty_text(req.external_event_id, field_name="external_event_id")
        external_order_id = _normalized_optional_text(req.external_order_id, field_name="external_order_id")
        operator = _normalized_optional_text(req.operator, field_name="operator")
        currency = _normalized_non_empty_text(req.currency, field_name="currency").upper()
        ts = req.ts or int(time.time())
        current_ts = int(time.time())
        if ts > current_ts + 300:
            raise HTTPException(status_code=409, detail="execution evidence timestamp is too far in the future (> 300s)")
        started_ts = int(bot.get("started_ts") or 0)
        if started_ts > 0 and ts < started_ts:
            raise HTTPException(status_code=409, detail="execution evidence timestamp is earlier than bot start")
        status = str(bot.get("status") or "").strip().lower()
        if status not in {"running", "stopped"}:
            raise HTTPException(status_code=409, detail=f"cannot record execution evidence for bot status={status}")
        stopped_ts = int(bot.get("stopped_ts") or 0)
        if status == "stopped" and stopped_ts > 0 and ts > stopped_ts + 300:
            raise HTTPException(status_code=409, detail="execution evidence timestamp is later than bot stop reconciliation window")

        event = {
            "event_id": event_id,
            "bot_id": bot_id,
            "origin_rec_id": bot.get("origin_rec_id"),
            "ts": ts,
            "symbol": bot.get("symbol"),
            "event_type": req.event_type,
            "source": req.source,
            "external_event_id": external_event_id,
            "external_order_id": external_order_id,
            "side": req.side,
            "qty": req.qty,
            "price": req.price,
            "order_price": req.order_price,
            "benchmark_price": req.benchmark_price,
            "benchmark_ts": req.benchmark_ts,
            "benchmark_source": req.benchmark_source,
            "gross_pnl": req.gross_pnl,
            "fee": req.fee,
            "funding": req.funding,
            "slippage": req.slippage,
            "currency": currency,
            "meta": req.meta,
        }
        try:
            insert_result = db.insert_execution_event(conn, event, commit=False)
        except ValueError as exc:
            _rollback_quietly(conn)
            message = str(exc)
            status_code = 409 if "already exists with different payload" in message else 422
            raise HTTPException(status_code=status_code, detail=message)

        summary = db.get_bot_execution_summary(conn, bot_id)
        if insert_result == "duplicate":
            canonical = db.get_execution_event_by_external_id(conn, req.source, external_event_id)
            canonical_event_id = str((canonical or {}).get("event_id") or event_id)
            _rollback_quietly(conn)
            return {
                "ok": True,
                "event_id": canonical_event_id,
                "external_event_id": external_event_id,
                "bot_id": bot_id,
                "insert_result": "duplicate",
                "idempotent": True,
                **summary,
            }
        try:
            state_updated = db.update_bot_state(
                conn,
                bot_id,
                {
                    "execution_evidence_event_count": int(summary["event_count"]),
                    "execution_evidence_execution_count": int(summary["execution_count"]),
                    "execution_evidence_funding_event_count": int(summary["funding_event_count"]),
                    "execution_evidence_realized_pnl_gross": float(summary["realized_pnl_gross"]),
                    "execution_evidence_realized_fee": float(summary["realized_fee"]),
                    "execution_evidence_realized_funding": float(summary["realized_funding"]),
                    "execution_evidence_realized_slippage": float(summary["realized_slippage"]),
                    "execution_evidence_realized_pnl_net": float(summary["realized_pnl_net"]),
                    "execution_evidence_buy_qty": float(summary["buy_qty"]),
                    "execution_evidence_sell_qty": float(summary["sell_qty"]),
                    "execution_evidence_net_position_qty": float(summary["net_position_qty"]),
                    "execution_evidence_position_flat": bool(summary["position_flat"]),
                    "exchange_reconciled": bool(summary.get("exchange_reconciled")),
                    "exchange_reconciliation_failures": list(
                        summary.get("exchange_reconciliation_failures") or []
                    ),
                    "execution_evidence_total_pnl_finalized": bool(summary["total_pnl_finalized"]),
                    "execution_evidence_last_event_ts": int(summary.get("last_event_ts") or ts),
                    "execution_evidence_last_event_id": event_id,
                    "execution_evidence_last_operator": operator,
                },
                commit=False,
            )
            if not state_updated:
                raise RuntimeError("bot state update failed after execution evidence")
            db.log_decision(
                conn,
                "EXECUTION_EVIDENCE_RECORDED",
                bot.get("origin_rec_id"),
                operator,
                {
                    "bot_id": bot_id,
                    "event_id": event_id,
                    "external_event_id": external_event_id,
                    "event_type": req.event_type,
                    "source": req.source,
                    "insert_result": insert_result,
                },
                commit=False,
            )
            conn.commit()
        except Exception:
            _rollback_quietly(conn)
            raise
        return {
            "ok": True,
            "event_id": event_id,
            "external_event_id": external_event_id,
            "bot_id": bot_id,
            "insert_result": insert_result,
            "idempotent": False,
            **summary,
        }


@app.post("/api/v1/bots/{bot_id}/execution-reconciliation")
def api_record_execution_reconciliation(
    bot_id: str,
    req: ExecutionReconciliationRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Record an immutable terminal snapshot from a read-only Bybit adapter."""
    _require_admin_key(x_api_key, request)
    _ensure_json_payload_has_only_finite_numbers(req.meta, field_name="meta")
    with closing(_get_conn()) as conn:
        db.begin_immediate(conn)
        bot = db.get_bot_instance(conn, bot_id, for_update=True)
        if not bot:
            raise HTTPException(status_code=404, detail="bot_id not found")
        if str(bot.get("status") or "").strip().lower() != "stopped":
            raise HTTPException(
                status_code=409,
                detail="terminal exchange reconciliation requires a stopped bot",
            )

        reconciliation_id = (
            _normalized_non_empty_text(
                req.reconciliation_id,
                field_name="reconciliation_id",
            )
            if req.reconciliation_id is not None
            else f"XR-{int(time.time())}-{secrets.token_hex(5)}"
        )
        external_snapshot_id = _normalized_non_empty_text(
            req.external_snapshot_id,
            field_name="external_snapshot_id",
        )
        operator = _normalized_optional_text(req.operator, field_name="operator")
        ts = req.ts or int(time.time())
        now = int(time.time())
        if ts > now + 300:
            raise HTTPException(
                status_code=409,
                detail="reconciliation timestamp is too far in the future (> 300s)",
            )
        stopped_ts = int(bot.get("stopped_ts") or 0)
        if stopped_ts <= 0 or ts < stopped_ts:
            raise HTTPException(
                status_code=409,
                detail="terminal reconciliation timestamp must be at or after bot stop",
            )
        snapshot = {
            "reconciliation_id": reconciliation_id,
            "bot_id": bot_id,
            "origin_rec_id": bot.get("origin_rec_id"),
            "ts": ts,
            "source": req.source,
            "external_snapshot_id": external_snapshot_id,
            "position_qty": req.position_qty,
            "open_order_count": req.open_order_count,
            "execution_event_count": req.execution_event_count,
            "funding_event_count": req.funding_event_count,
            "realized_pnl_gross": req.realized_pnl_gross,
            "fee": req.fee,
            "funding": req.funding,
            "currency": req.currency,
            "complete": req.complete,
            "meta": req.meta,
        }
        try:
            insert_result = db.insert_execution_reconciliation(
                conn,
                snapshot,
                commit=False,
            )
        except ValueError as exc:
            _rollback_quietly(conn)
            message = str(exc)
            status_code = 409 if "already exists with different payload" in message else 422
            raise HTTPException(status_code=status_code, detail=message)

        summary = db.get_bot_execution_summary(conn, bot_id)
        if insert_result == "duplicate":
            canonical = db.get_execution_reconciliation_by_external_id(
                conn,
                req.source,
                external_snapshot_id,
            )
            canonical_id = str(
                (canonical or {}).get("reconciliation_id") or reconciliation_id
            )
            _rollback_quietly(conn)
            return {
                "ok": True,
                "reconciliation_id": canonical_id,
                "external_snapshot_id": external_snapshot_id,
                "bot_id": bot_id,
                "insert_result": "duplicate",
                "idempotent": True,
                **summary,
            }
        try:
            state_updated = db.update_bot_state(
                conn,
                bot_id,
                {
                    "exchange_reconciliation_id": reconciliation_id,
                    "exchange_reconciliation_ts": ts,
                    "exchange_reconciled": bool(summary.get("exchange_reconciled")),
                    "exchange_reconciliation_failures": list(
                        summary.get("exchange_reconciliation_failures") or []
                    ),
                    "execution_evidence_total_pnl_finalized": bool(
                        summary.get("total_pnl_finalized")
                    ),
                    "exchange_reconciliation_last_operator": operator,
                },
                commit=False,
            )
            if not state_updated:
                raise RuntimeError("bot state update failed after execution reconciliation")
            db.log_decision(
                conn,
                "EXECUTION_RECONCILIATION_RECORDED",
                bot.get("origin_rec_id"),
                operator,
                {
                    "bot_id": bot_id,
                    "reconciliation_id": reconciliation_id,
                    "external_snapshot_id": external_snapshot_id,
                    "exchange_reconciled": bool(summary.get("exchange_reconciled")),
                    "failures": list(summary.get("exchange_reconciliation_failures") or []),
                },
                commit=False,
            )
            conn.commit()
        except Exception:
            _rollback_quietly(conn)
            raise
        return {
            "ok": True,
            "reconciliation_id": reconciliation_id,
            "external_snapshot_id": external_snapshot_id,
            "bot_id": bot_id,
            "insert_result": insert_result,
            "idempotent": False,
            **summary,
        }


@app.get("/api/v1/execution-evidence")
def api_execution_evidence(
    request: Request,
    bot_id: str | None = None,
    limit: int = 500,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _require_admin_key(x_api_key, request)
    limit = _bounded_limit(limit, default=500, max_value=2000)
    with closing(_get_conn()) as conn:
        items = db.list_execution_events(conn, bot_id=bot_id, limit=limit)
        return {
            "items": items,
            "count": len(items),
            "evidence_grade": False,
            "source_records_only": True,
            "requires_terminal_exchange_reconciliation": True,
        }


@app.get("/api/v1/execution-reconciliations")
def api_execution_reconciliations(
    request: Request,
    bot_id: str | None = None,
    limit: int = 200,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _require_admin_key(x_api_key, request)
    limit = _bounded_limit(limit, default=200, max_value=1000)
    with closing(_get_conn()) as conn:
        items = db.list_execution_reconciliations(
            conn,
            bot_id=bot_id,
            limit=limit,
        )
        return {
            "items": items,
            "count": len(items),
            "terminal_exchange_reconciliation": True,
        }


@app.get("/api/v1/validation/live-evidence")
def api_live_evidence_validation(
    request: Request,
    limit: int = 200,
    symbol: str | None = None,
    direction: str | None = None,
    model_version: str | None = None,
    venue: str = "linear",
    bot_type: str = "futures_grid",
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _require_admin_key(x_api_key, request)
    limit = _bounded_limit(limit, default=200, max_value=1000)
    with closing(_get_conn()) as conn:
        records = db.list_live_validation_records(conn, limit=limit)
        strategy_health = _compute_live_validation_strategy_health(
            conn,
            venue=venue,
            symbol=symbol,
            direction=direction,
            bot_type=bot_type,
            model_version=model_version,
        )
    eligible = [
        row
        for row in records
        if bool(row.get("validation_eligible")) and row.get("total_pnl_finalized") is True
    ]
    net_values = [float(row.get("realized_pnl_net") or 0.0) for row in eligible]
    wins = sum(1 for value in net_values if value > 0.0)
    return {
        "records": records,
        "count": len(records),
        "strategy_health": strategy_health,
        "eligible_stopped_bots": len(eligible),
        "summary": {
            "total_realized_pnl_net": sum(net_values),
            "mean_realized_pnl_net": (sum(net_values) / len(net_values)) if net_values else None,
            "positive_bot_rate": (wins / len(net_values)) if net_values else None,
            "legacy_trade_rows_excluded": True,
            "descriptive_only": True,
            "live_edge_claim_supported": False,
            "reason": "A chronological comparator, no-trade baseline and sufficient independent sample are still required.",
        },
    }


@app.get("/api/v1/trades")
def api_trades(bot_id: str | None = None, limit: int = 200) -> dict[str, Any]:
    limit = _bounded_limit(limit, default=200, max_value=1000)
    with closing(_get_conn()) as conn:
        items = db.list_trades(conn, bot_id=bot_id, limit=limit)
        return {"items": items, "count": len(items)}


@app.get("/api/v1/outcomes/stats")
def api_outcomes_stats(
    scope: str = "current_policy",
    detail: str = "full",
) -> dict[str, Any]:
    """Return outcome proxies in an explicit evidence lineage.

    The operator UI defaults to the exact policy currently running. Historical
    outcomes remain available via ``scope=archive`` but are never blended into the
    current-policy headline.
    """
    detail_norm = str(detail or "full").strip().lower()
    if detail_norm not in {"full", "summary"}:
        raise HTTPException(status_code=400, detail="detail must be full or summary")
    with closing(_get_conn()) as conn:
        active_risk_limits = normalize_risk_limits(
            db.get_active_risk_limits(conn),
            settings.risk_limits,
        )
        policy_fingerprint = calibration_policy_fingerprint(
            settings,
            active_risk_limits,
        )
        try:
            return db.get_outcomes_stats(
                conn,
                require_llm_verdict=bool(getattr(settings, "llm_reviewer_enabled", False)),
                scope=scope,
                current_model_version=RECOMMENDER_MODEL_VERSION,
                policy_fingerprint=policy_fingerprint,
                include_breakdowns=detail_norm == "full",
                recent_limit=20 if detail_norm == "summary" else 120,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/health/symbols")
def api_symbol_health() -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        items, meta = _load_symbol_health(conn)
        collector_last_cycle = _get_app_config_mapping(conn, "collector_last_cycle", default={})
        backfill_last_cycle = _get_app_config_mapping(conn, "backfill_last_cycle", default={})
        n_ok = sum(1 for i in items if i["status"] == "ok")
        n_stale = sum(1 for i in items if i["status"] == "stale")
        n_missing = sum(1 for i in items if i["status"] == "missing")
        n_disabled = sum(1 for i in items if i["status"] == "disabled")
        n_errors = sum(i["error_count_10m"] for i in items)
        return {
            "ts": int(time.time()),
            "summary": {"ok": n_ok, "stale": n_stale, "missing": n_missing, "disabled": n_disabled, "errors_10m": n_errors},
            "venues": meta["venues"],
            "warmup": meta["warmup"],
            "boot_grace": {
                "active": meta["boot_grace_active"],
                "grace_sec": meta["boot_grace_sec"],
                "process_started_ts": meta["process_started_ts"],
            },
            "runtime": _process_memory_snapshot(),
            "collector": {
                **collector_last_cycle,
                "max_workers": int(getattr(settings, "collector_max_workers", 1) or 1),
                "recent_tail_bars": 360,
            },
            "backfill": {
                **backfill_last_cycle,
                "full_sweep_on_warmup": bool(getattr(settings, "backfill_full_sweep_on_warmup", False)),
                "budget_per_tf": int(getattr(settings, "backfill_per_tf_budget", 8) or 8),
                "chunk_bars": 360,
            },
            "llm_reviewer": {
                "enabled": bool(getattr(settings, "llm_reviewer_enabled", False)),
                "mode": getattr(settings, "llm_reviewer_mode", "advisory"),
                "provider": getattr(settings, "llm_reviewer_provider", "ollama"),
                "model": getattr(settings, "llm_reviewer_model", None),
                "tf_secs": list(getattr(settings, "llm_reviewer_tf_secs", []) or []),
                "candles_per_tf": int(getattr(settings, "llm_reviewer_candles_per_tf", 32) or 32),
                "max_candidates": int(getattr(settings, "llm_reviewer_max_candidates", 24) or 24),
                "max_workers": int(getattr(settings, "llm_reviewer_max_workers", 2) or 2),
                "async_mode": True,
                "min_confidence": float(getattr(settings, "llm_reviewer_min_confidence", 0.65) or 0.65),
                "cadence_sec": int(getattr(settings, "llm_reviewer_cadence_sec", 300) or 300),
                "pending_timeout_sec": int(getattr(settings, "llm_reviewer_pending_timeout_sec", 900) or 900),
                "keep_alive": str(getattr(settings, "llm_reviewer_keep_alive", "90s") or "90s"),
            },
            "symbols": items,
        }


@app.get("/api/v1/decisions")
def api_decisions(limit: int = 200) -> list[dict[str, Any]]:
    limit = _bounded_limit(limit, default=200, max_value=1000)
    with closing(_get_conn()) as conn:
        rows = conn.execute(
            """SELECT ts, action, rec_id, operator, details_json
               FROM decision_log ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "ts": r["ts"],
                "action": r["action"],
                "rec_id": r["rec_id"],
                "operator": r["operator"],
                "details": _json_loads_mapping_or_default(r["details_json"], {}),
            })
        return out


@app.post("/api/v1/sentiment")
def api_sentiment_put(req: SentimentPointRequest, request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key, request)
    scope = _normalized_non_empty_text(req.scope, field_name="scope")
    key = _normalized_non_empty_text(req.key, field_name="key")
    tags = _normalize_tag_list(req.tags)
    _ensure_json_payload_has_only_finite_numbers(req.sources, field_name="sources")
    with closing(_get_conn()) as conn:
        ts = req.ts or int(time.time())
        try:
            db.begin_immediate(conn)
            db.insert_sentiment_point(conn, scope, key, ts, req.sentiment, req.velocity, req.volume, req.sources, tags, commit=False)
            db.log_decision(conn, "SENTIMENT_PUT", None, None, {"scope": scope, "key": key, "ts": ts, "sentiment": req.sentiment}, commit=False)
            conn.commit()
        except Exception:
            _rollback_quietly(conn)
            raise
        return {"ok": True, "ts": ts}


@app.get("/api/v1/sentiment")
def api_sentiment_get(scope: str = "global", key: str = "crypto", limit: int = 120) -> dict[str, Any]:
    # GET-фильтры нормализуем так же, как mutating API: операторский пробельный
    # ввод не должен приводить к "пустому" ответу при существующей серии.
    scope = _normalized_filter_text(scope, default="global", field_name="scope")
    key = _normalized_filter_text(key, default="crypto", field_name="key")
    limit = _bounded_limit(limit, default=120, max_value=1000)
    with closing(_get_conn()) as conn:
        series = db.get_sentiment_series(conn, scope, key, limit=limit)
        return {"scope": scope, "key": key, "items": series}


def _collector_thread():
    client = BybitPublicClient(settings.bybit_base_url)
    lock_key = "runtime:collector"
    lock_ttl = _collector_runtime_lock_ttl_sec()
    next_run = time.monotonic()
    try:
        while not _BACKGROUND_STOP_EVENT.is_set():
            with closing(_get_lock_conn()) as lock_conn:
                has_lock = db.acquire_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
            if has_lock:
                cycle_started = time.time()
                cycle_stats: dict[str, Any] = {
                    "started_ts": int(cycle_started),
                    "owner": RUNTIME_OWNER,
                    "venues": [],
                    "futures_meta": {},
                    "duration_ms": 0,
                    "lock_lost": False,
                    "collector_max_workers": int(getattr(settings, "collector_max_workers", 1) or 1),
                    "futures_collect_max_workers": int(getattr(settings, "futures_collect_max_workers", 1) or 1),
                }
                with closing(_get_conn()) as conn:
                    heartbeat = _make_runtime_lock_heartbeat(lock_key)
                    for venue in settings.venues:
                        symbols = settings.symbols_linear
                        try:
                            cycle_stats["venues"].append(
                                _collect_hot_once(
                                    conn,
                                    client,
                                    venue,
                                    symbols,
                                    heartbeat,
                                    int(getattr(settings, "collector_max_workers", 1) or 1),
                                )
                            )
                        except RuntimeLockLostError as e:
                            cycle_stats["lock_lost"] = True
                            _rollback_quietly(conn)
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "field": "runtime_lock", "err": str(e)})
                            break
                        except Exception as e:
                            _rollback_quietly(conn)
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "err": str(e)})
                        if not heartbeat():
                            cycle_stats["lock_lost"] = True
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "field": "runtime_lock", "err": "collector runtime lock lost"})
                            break
                cycle_stats["duration_ms"] = int((time.time() - cycle_started) * 1000)
                with closing(_get_conn()) as conn:
                    db.set_app_config_json(conn, "collector_last_cycle", cycle_stats)
                    try:
                        db.set_app_config_json(conn, "collector_warmup", _collector_warmup_status(conn))
                    except Exception:
                        logger.warning("collector warmup status update failed", exc_info=True)
            next_run = _interval_loop_wait(next_run, settings.collect_interval_sec)
    finally:
        client.close()


def _backfill_thread():
    client = BybitPublicClient(settings.bybit_base_url)
    lock_key = "runtime:backfill"
    lock_ttl = _collector_runtime_lock_ttl_sec()
    next_run = time.monotonic()
    try:
        while not _BACKGROUND_STOP_EVENT.is_set():
            with closing(_get_lock_conn()) as lock_conn:
                has_lock = db.acquire_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
            if has_lock:
                cycle_started = time.time()
                cycle_stats: dict[str, Any] = {
                    "started_ts": int(cycle_started),
                    "owner": RUNTIME_OWNER,
                    "venues": [],
                    "futures_meta": {},
                    "duration_ms": 0,
                    "lock_lost": False,
                    "collector_max_workers": int(getattr(settings, "collector_max_workers", 1) or 1),
                    "futures_collect_max_workers": int(getattr(settings, "futures_collect_max_workers", 1) or 1),
                }
                with closing(_get_conn()) as conn:
                    heartbeat = _make_runtime_lock_heartbeat(lock_key)
                    for venue in settings.venues:
                        symbols = settings.symbols_linear
                        try:
                            cycle_stats["venues"].append(
                                _collect_backfill_cycle(
                                    conn,
                                    client,
                                    venue,
                                    symbols,
                                    heartbeat,
                                    int(getattr(settings, "collector_max_workers", 1) or 1),
                                )
                            )
                        except RuntimeLockLostError as e:
                            cycle_stats["lock_lost"] = True
                            _rollback_quietly(conn)
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "field": "runtime_lock", "err": str(e)})
                            break
                        except Exception as e:
                            _rollback_quietly(conn)
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "field": "backfill", "err": str(e)})
                        if not heartbeat():
                            cycle_stats["lock_lost"] = True
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "field": "runtime_lock", "err": "backfill runtime lock lost"})
                            break
                cycle_stats["duration_ms"] = int((time.time() - cycle_started) * 1000)
                with closing(_get_conn()) as conn:
                    db.set_app_config_json(conn, "backfill_last_cycle", cycle_stats)
                    try:
                        db.set_app_config_json(conn, "collector_warmup", _collector_warmup_status(conn))
                    except Exception:
                        logger.warning("collector warmup status update failed", exc_info=True)
            next_run = _interval_loop_wait(next_run, settings.collect_interval_sec)
    finally:
        client.close()


def _futures_meta_thread():
    client = BybitPublicClient(settings.bybit_base_url)
    lock_key = "runtime:futures_meta"
    lock_ttl = max(120, settings.futures_collect_interval_sec * 2)
    next_run = time.monotonic()
    _last_run = 0.0
    try:
        while not _BACKGROUND_STOP_EVENT.is_set():
            if settings.symbols_linear:
                run_allowed = True
                if not bool(getattr(settings, "futures_meta_during_warmup", False)):
                    with closing(_get_conn()) as conn:
                        try:
                            run_allowed = bool(_warmup_status_payload(conn).get("ready"))
                        except Exception:
                            run_allowed = False
                if run_allowed and time.time() - _last_run >= settings.futures_collect_interval_sec:
                    with closing(_get_lock_conn()) as lock_conn:
                        has_lock = db.acquire_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
                    if has_lock:
                        cycle_started = time.time()
                        cycle_stats: dict[str, Any] = {
                            "started_ts": int(cycle_started),
                            "owner": RUNTIME_OWNER,
                            "duration_ms": 0,
                            "lock_lost": False,
                        }
                        with closing(_get_conn()) as conn:
                            heartbeat = _make_runtime_lock_heartbeat(lock_key)
                            try:
                                cycle_stats.update(
                                    collect_futures_once(
                                        conn,
                                        client,
                                        settings.symbols_linear,
                                        heartbeat=heartbeat,
                                        max_workers=int(getattr(settings, "futures_collect_max_workers", getattr(settings, "collector_max_workers", 1)) or 1),
                                    )
                                )
                                if not heartbeat():
                                    raise RuntimeLockLostError("futures_meta runtime lock lost")
                                _last_run = time.time()
                            except RuntimeLockLostError as e:
                                cycle_stats["lock_lost"] = True
                                _rollback_quietly(conn)
                                _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": "linear", "symbol": "UNKNOWN", "field": "runtime_lock", "err": str(e)})
                            except Exception as e:
                                _rollback_quietly(conn)
                                _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": "linear", "symbol": "UNKNOWN", "field": "futures_meta", "err": str(e)})
                        cycle_stats["duration_ms"] = int((time.time() - cycle_started) * 1000)
                        with closing(_get_conn()) as conn:
                            db.set_app_config_json(conn, "futures_meta_last_cycle", cycle_stats)
            next_run = _interval_loop_wait(next_run, min(settings.futures_collect_interval_sec, max(10, settings.collect_interval_sec)))
    finally:
        client.close()


def _sentiment_thread():
    lock_key = "runtime:sentiment"
    lock_ttl = max(60, settings.sentiment_interval_sec * 4)
    next_run = _interval_loop_start(settings.sentiment_interval_sec)
    while not _BACKGROUND_STOP_EVENT.is_set():
        with closing(_get_lock_conn()) as lock_conn:
            has_lock = db.acquire_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
        if has_lock:
            heartbeat = _make_runtime_lock_heartbeat(lock_key)
            with closing(_get_conn()) as conn:
                try:
                    if not heartbeat():
                        raise RuntimeLockLostError("sentiment runtime lock lost")
                    pts = collect_sentiment_once()
                    if not heartbeat():
                        raise RuntimeLockLostError("sentiment runtime lock lost")
                    db.insert_sentiment_points(conn, pts, commit=False)
                    db.log_decision(conn, "SENTIMENT_COLLECT", None, None, {"count": len(pts)}, commit=False)
                    if not heartbeat():
                        raise RuntimeLockLostError("sentiment runtime lock lost")
                    conn.commit()
                except Exception as e:
                    _rollback_quietly(conn)
                    details = {"err": str(e)}
                    if _is_runtime_lock_lost_error(e):
                        details = {"field": "runtime_lock", "err": str(e)}
                    _log_decision_fresh("SENTIMENT_ERROR", None, None, details)
        next_run = _interval_loop_wait(next_run, settings.sentiment_interval_sec)


def _reco_thread():
    _last_prune = 0.0
    _last_warmup_log = 0.0
    PRUNE_INTERVAL = 3600
    lock_key = "runtime:reco"
    lock_ttl = max(60, settings.reco_interval_sec * 4)
    next_run = time.monotonic()
    while not _BACKGROUND_STOP_EVENT.is_set():
        result = {}
        with closing(_get_lock_conn()) as lock_conn:
            has_lock = db.acquire_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
        if has_lock:
            warmup_ready = True
            warmup_status: dict[str, Any] = {}
            with closing(_get_conn()) as conn:
                try:
                    warmup_status = _load_collector_warmup_status(conn, recompute_if_missing=True)
                except Exception:
                    warmup_status = {"ready": False, "reason": "collector_warmup_unavailable"}
                warmup_ready = bool(warmup_status.get("ready", False)) if isinstance(warmup_status, dict) else False
                if not warmup_ready:
                    now_ts = time.time()
                    cooldown = max(30, int(getattr(settings, "reco_warmup_log_cooldown_sec", 120) or 120))
                    if now_ts - _last_warmup_log >= cooldown:
                        _last_warmup_log = now_ts
                        _log_decision_fresh("RECO_WARMUP_SKIP", None, None, warmup_status if isinstance(warmup_status, dict) else {"ready": False})
            if warmup_ready:
                heartbeat = _make_runtime_lock_heartbeat(lock_key)
                leadership_ok = True
                with closing(_get_conn()) as conn:
                    try:
                        if not heartbeat():
                            raise RuntimeLockLostError("reco runtime lock lost")
                        result = run_recommender_once(
                            conn,
                            settings,
                            heartbeat=heartbeat,
                        )
                        if not heartbeat():
                            raise RuntimeLockLostError("reco runtime lock lost")
                    except Exception as e:
                        leadership_ok = False if _is_runtime_lock_lost_error(e) else leadership_ok
                        _rollback_quietly(conn)
                        details = {"err": str(e)}
                        if leadership_ok is False:
                            details = {"field": "runtime_lock", "err": str(e)}
                        _log_decision_fresh("RECO_ERROR", None, None, details)

                if leadership_ok and heartbeat():
                    with closing(_get_conn()) as conn:
                        try:
                            db.expire_stale_recommendations(conn)
                        except Exception:
                            logger.debug("expire_stale_recommendations error", exc_info=True)

                if leadership_ok and heartbeat() and time.time() - _last_prune >= PRUNE_INTERVAL:
                    with closing(_get_conn()) as conn:
                        try:
                            deleted = db.prune_old_data(conn, retain_days=7)
                            db.log_decision(conn, "DB_PRUNE", None, None, deleted)
                            _last_prune = time.time()
                        except Exception:
                            _rollback_quietly(conn)
                            logger.debug("prune_old_data error", exc_info=True)

                if leadership_ok and heartbeat() and settings.telegram_token:
                    try:
                        with closing(_get_conn()) as conn:
                            health, _ = _load_symbol_health(conn)
                            err_cur = conn.execute(
                                """SELECT COUNT(*) as c FROM decision_log
                                   WHERE action='COLLECT_ERROR' AND ts >= ?""",
                                (int(time.time()) - 600,),
                            )
                            err_count = int(err_cur.fetchone()["c"])
                        check_and_alert(token=settings.telegram_token, chat_id=settings.telegram_chat_id, symbol_health=health, collect_errors_10m=err_count, reco_count=int(result.get("count_actionable", result.get("count_recommended", 0))))
                    except Exception:
                        logger.debug("telegram alert error", exc_info=True)

        next_run = _interval_loop_wait(next_run, settings.reco_interval_sec)


def _outcome_worker_stale_after_sec() -> int:
    return max(180, int(getattr(settings, "outcomes_interval_sec", 60) or 60) * 3)


def _run_outcome_cycle_once(conn, *, heartbeat=None) -> dict[str, Any]:
    """Execute and persist one independently observable outcome-maintenance cycle."""
    started_ts = int(time.time())
    stale_after_sec = _outcome_worker_stale_after_sec()
    require_llm_verdict = bool(getattr(settings, "llm_reviewer_enabled", False))
    before = db.get_outcome_worker_liveness(
        conn,
        now_ts_value=started_ts,
        require_llm_verdict=require_llm_verdict,
        worker_stale_after_sec=stale_after_sec,
    )
    running = {
        "state": "running",
        "cycle_started_ts": started_ts,
        "cycle_finished_ts": None,
        "updated_ts": started_ts,
        "rows_selected": 0,
        "rows_examined": 0,
        "rows_labeled": 0,
        "rows_waiting": 0,
        "rows_censored": 0,
        "rows_failed": 0,
        "matured_pending_before": int(before.get("matured_pending_total") or 0),
        "matured_pending_after": None,
        "oldest_pending_before": before.get("oldest_due_ts"),
        "oldest_pending_after": None,
        "last_processed_rec_id": None,
        "duration_ms": 0,
    }
    db.set_app_config_json(conn, db.OUTCOME_WORKER_CYCLE_APP_KEY, running)
    try:
        if heartbeat is not None and not heartbeat():
            raise RuntimeLockLostError("outcome runtime lock lost")
        last_progress_persisted = [time.monotonic()]

        def persist_running_progress(snapshot: dict[str, object]) -> None:
            now_monotonic = time.monotonic()
            is_terminal_snapshot = int(snapshot.get("rows_examined") or 0) >= int(snapshot.get("rows_selected") or 0)
            if not is_terminal_snapshot and now_monotonic - last_progress_persisted[0] < 5.0:
                return
            last_progress_persisted[0] = now_monotonic
            db.set_app_config_json(
                conn,
                db.OUTCOME_WORKER_CYCLE_APP_KEY,
                {
                    **running,
                    **dict(snapshot),
                    "state": "running",
                    "updated_ts": int(time.time()),
                },
            )

        stats = compute_outcomes_cycle(
            conn,
            horizon_sec=settings.outcome_horizon_fallback_sec,
            max_to_process=int(getattr(settings, "outcomes_max_to_process", 200) or 200),
            heartbeat=heartbeat,
            progress_callback=persist_running_progress,
        )
        if heartbeat is not None and not heartbeat():
            raise RuntimeLockLostError("outcome runtime lock lost")
        finished_ts = int(time.time())
        after = db.get_outcome_worker_liveness(
            conn,
            now_ts_value=finished_ts,
            require_llm_verdict=require_llm_verdict,
            worker_stale_after_sec=stale_after_sec,
        )
        completed = {
            **running,
            **dict(stats),
            "state": "completed",
            "cycle_finished_ts": finished_ts,
            "updated_ts": finished_ts,
            "matured_pending_after": int(after.get("matured_pending_total") or 0),
            "oldest_pending_after": after.get("oldest_due_ts"),
        }
        db.set_app_config_json(conn, db.OUTCOME_WORKER_CYCLE_APP_KEY, completed)
        completed["liveness"] = db.get_outcome_worker_liveness(
            conn,
            now_ts_value=finished_ts,
            require_llm_verdict=require_llm_verdict,
            worker_stale_after_sec=stale_after_sec,
        )
        db.set_app_config_json(conn, db.OUTCOME_WORKER_CYCLE_APP_KEY, completed)
        return completed
    except Exception as exc:
        failed_ts = int(time.time())
        failed = {
            **running,
            "state": "error",
            "cycle_finished_ts": failed_ts,
            "updated_ts": failed_ts,
            "rows_failed": 1,
            "duration_ms": max(0, int((failed_ts - started_ts) * 1000)),
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }
        try:
            db.set_app_config_json(conn, db.OUTCOME_WORKER_CYCLE_APP_KEY, failed)
        except Exception:
            logger.warning("outcome worker error state persist failed", exc_info=True)
        raise


def _outcome_thread():
    lock_key = "runtime:outcomes"
    base_interval = max(1, int(getattr(settings, "outcomes_interval_sec", 60) or 60))
    lock_ttl = max(120, base_interval * 4)
    next_run = time.monotonic()
    interval_sec = base_interval
    while not _BACKGROUND_STOP_EVENT.is_set():
        with closing(_get_lock_conn()) as lock_conn:
            has_lock = db.acquire_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
        interval_sec = base_interval
        if has_lock:
            heartbeat = _make_runtime_lock_heartbeat(lock_key)
            with closing(_get_conn()) as conn:
                cycle = _run_outcome_cycle_once(conn, heartbeat=heartbeat)
                liveness = cycle.get("liveness") if isinstance(cycle.get("liveness"), dict) else {}
                state = str(liveness.get("state") or "")
                if state == "stalled":
                    db.log_decision(conn, "OUTCOME_WORKER_STALLED", None, None, liveness)
                pending_before = int(cycle.get("matured_pending_before") or 0)
                pending_after = int(cycle.get("matured_pending_after") or 0)
                terminal_progress = (
                    int(cycle.get("rows_labeled") or 0) > 0
                    or int(cycle.get("rows_censored") or 0) > 0
                    or pending_after < pending_before
                )
                if pending_after > 0 and terminal_progress:
                    interval_sec = min(base_interval, OUTCOME_BACKLOG_CATCHUP_SEC)
        next_run = _interval_loop_wait(next_run, interval_sec)


def _llm_reviewer_thread():
    if not bool(getattr(settings, "llm_reviewer_enabled", False)):
        return
    lock_key = "runtime:llm_reviewer"
    base_interval = int(getattr(settings, "llm_reviewer_cadence_sec", 300) or 300)
    reco_interval = max(5, int(getattr(settings, "reco_interval_sec", 20) or 20))
    eager_interval = min(base_interval, max(60, reco_interval * 3))
    lock_ttl = max(60, base_interval * 4)
    next_run = time.monotonic()
    interval_sec = eager_interval
    while not _BACKGROUND_STOP_EVENT.is_set():
        with closing(_get_lock_conn()) as lock_conn:
            has_lock = db.acquire_runtime_lock(lock_conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
        if has_lock:
            heartbeat = _make_runtime_lock_heartbeat(lock_key)
            with closing(_get_conn()) as conn:
                try:
                    if not heartbeat():
                        raise RuntimeLockLostError("llm reviewer runtime lock lost")
                    stats = run_llm_review_sweep_once(conn, settings, heartbeat=heartbeat)
                    if not heartbeat():
                        raise RuntimeLockLostError("llm reviewer runtime lock lost")
                    db.set_app_config_json(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, {**stats, "updated_ts": int(time.time())})
                    interval_sec = eager_interval if int(stats.get("pending_after", 0) or 0) > 0 else base_interval
                except Exception as e:
                    interval_sec = base_interval
                    _rollback_quietly(conn)
                    if _is_runtime_lock_lost_error(e):
                        db.set_app_config_json(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, {"enabled": True, "updated_ts": int(time.time())})
                        details = {"field": "runtime_lock", "err": str(e)}
                    else:
                        db.set_app_config_json(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, {"enabled": True, "error": str(e), "updated_ts": int(time.time())})
                        details = {"err": str(e)}
                    _log_decision_fresh("LLM_REVIEW_SWEEP_ERROR", None, None, details)
        next_run = _interval_loop_wait(next_run, interval_sec)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    with closing(_get_conn()) as conn:
        health, _ = _load_symbol_health(conn)
        collector_last_cycle = _get_app_config_mapping(conn, "collector_last_cycle", default={})
        collector_warmup = _load_collector_warmup_status(conn, recompute_if_missing=True)
        status_counts = {"ok": 0, "stale": 0, "missing": 0, "disabled": 0}
        for item in health:
            status = str(item.get("status") or "missing")
            status_counts[status] = status_counts.get(status, 0) + 1
        active_recommendations = int(conn.execute(
            "SELECT COUNT(*) AS c FROM recommendations WHERE status IN ('recommended','active')"
        ).fetchone()["c"])
        collect_errors_10m = int(conn.execute(
            "SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR' AND ts >= ?",
            (db.now_ts() - 600,),
        ).fetchone()["c"])
        outcome_liveness = db.get_outcome_worker_liveness(
            conn,
            require_llm_verdict=bool(getattr(settings, "llm_reviewer_enabled", False)),
            worker_stale_after_sec=_outcome_worker_stale_after_sec(),
        )
        lines = [
            "# TYPE bybit_reco_symbols_total gauge",
            f"bybit_reco_symbols_total {len(health)}",
            "# TYPE bybit_reco_symbols_ok gauge",
            f"bybit_reco_symbols_ok {status_counts.get('ok', 0)}",
            "# TYPE bybit_reco_symbols_stale gauge",
            f"bybit_reco_symbols_stale {status_counts.get('stale', 0)}",
            "# TYPE bybit_reco_symbols_missing gauge",
            f"bybit_reco_symbols_missing {status_counts.get('missing', 0)}",
            "# TYPE bybit_reco_symbols_disabled gauge",
            f"bybit_reco_symbols_disabled {status_counts.get('disabled', 0)}",
            "# TYPE bybit_reco_collect_errors_10m gauge",
            f"bybit_reco_collect_errors_10m {collect_errors_10m}",
            "# TYPE bybit_reco_recommendations_active gauge",
            f"bybit_reco_recommendations_active {active_recommendations}",
            "# TYPE bybit_reco_outcome_matured_pending gauge",
            f"bybit_reco_outcome_matured_pending {int(outcome_liveness.get('matured_pending_total') or 0)}",
            "# TYPE bybit_reco_outcome_unattempted gauge",
            f"bybit_reco_outcome_unattempted {int(outcome_liveness.get('unattempted_total') or 0)}",
            "# TYPE bybit_reco_outcome_worker_stalled gauge",
            f"bybit_reco_outcome_worker_stalled {1 if outcome_liveness.get('state') == 'stalled' else 0}",
            "# TYPE bybit_reco_outcome_worker_processing gauge",
            f"bybit_reco_outcome_worker_processing {1 if outcome_liveness.get('state') == 'processing' else 0}",
            "# TYPE bybit_reco_outcome_worker_backlog gauge",
            f"bybit_reco_outcome_worker_backlog {1 if outcome_liveness.get('state') == 'backlog' else 0}",
            "# TYPE bybit_reco_outcome_worker_error gauge",
            f"bybit_reco_outcome_worker_error {1 if outcome_liveness.get('state') == 'error' else 0}",
            "# TYPE bybit_reco_outcome_cycle_duration_ms gauge",
            f"bybit_reco_outcome_cycle_duration_ms {int((outcome_liveness.get('worker_cycle') or {}).get('duration_ms') or 0)}",
            "# TYPE bybit_reco_outcome_cycle_rows_examined gauge",
            f"bybit_reco_outcome_cycle_rows_examined {int((outcome_liveness.get('worker_cycle') or {}).get('rows_examined') or 0)}",
            "# TYPE bybit_reco_outcome_cycle_rows_labeled gauge",
            f"bybit_reco_outcome_cycle_rows_labeled {int((outcome_liveness.get('worker_cycle') or {}).get('rows_labeled') or 0)}",
            "# TYPE bybit_reco_outcome_cycle_rows_waiting gauge",
            f"bybit_reco_outcome_cycle_rows_waiting {int((outcome_liveness.get('worker_cycle') or {}).get('rows_waiting') or 0)}",
            "# TYPE bybit_reco_outcome_cycle_rows_censored gauge",
            f"bybit_reco_outcome_cycle_rows_censored {int((outcome_liveness.get('worker_cycle') or {}).get('rows_censored') or 0)}",
            "# TYPE bybit_reco_outcome_cycle_rows_failed gauge",
            f"bybit_reco_outcome_cycle_rows_failed {int((outcome_liveness.get('worker_cycle') or {}).get('rows_failed') or 0)}",
            "# TYPE bybit_reco_collector_cycle_duration_ms gauge",
            f"bybit_reco_collector_cycle_duration_ms {int(collector_last_cycle.get('duration_ms') or 0)}",
            "# TYPE bybit_reco_collector_max_workers gauge",
            f"bybit_reco_collector_max_workers {int(getattr(settings, 'collector_max_workers', 1) or 1)}",
            "# TYPE bybit_reco_futures_collect_max_workers gauge",
            f"bybit_reco_futures_collect_max_workers {int(getattr(settings, 'futures_collect_max_workers', 1) or 1)}",
            "# TYPE bybit_reco_warmup_ready gauge",
            f"bybit_reco_warmup_ready {1 if collector_warmup.get('ready') else 0}",
            "# TYPE bybit_reco_warmup_ready_symbols gauge",
            f"bybit_reco_warmup_ready_symbols {int(collector_warmup.get('ready_symbols') or 0)}",
            "# TYPE bybit_reco_warmup_symbols_total gauge",
            f"bybit_reco_warmup_symbols_total {int(collector_warmup.get('symbols_total') or 0)}",
        ]
        return "\n".join(lines) + "\n"


def _latest_recommendation_readiness(conn) -> dict[str, Any]:
    """Summarise the latest publication without conflating safety with actionability.

    The latest snapshot is bounded by the configured universe, so parsing its
    reasons is safe and gives the operator a concrete explanation for a uniform
    ``no_trade`` screen. Historical queues are intentionally excluded here.
    """
    latest_ts = db.get_latest_reco_ts(conn)
    status_counts = {
        "recommended": 0,
        "active": 0,
        "pending": 0,
        "blocked": 0,
        "no_trade": 0,
        "suppressed": 0,
        "expired": 0,
        "executed": 0,
        "ignored": 0,
        "unknown": 0,
    }
    if latest_ts is None:
        return {
            "latest_snapshot_ts": None,
            "latest_snapshot_age_sec": None,
            "latest_snapshot_total": 0,
            "status_counts": status_counts,
            "actionable_count": 0,
            "calibration_only_no_trade_count": 0,
            "non_calibration_no_trade_count": 0,
            "no_trade_reason_counts": [],
            "blocked_reason_counts": [],
            "dominant_state": "no_snapshot",
        }

    rows = conn.execute(
        """SELECT status, reasons_json
               FROM recommendations
              WHERE ts=?
              ORDER BY rec_id ASC
              LIMIT 1000""",
        (int(latest_ts),),
    ).fetchall()
    no_trade_counts: Counter[str] = Counter()
    blocked_counts: Counter[str] = Counter()
    no_trade_messages: dict[str, str] = {}
    blocked_messages: dict[str, str] = {}
    no_trade_order: dict[str, int] = {}
    blocked_order: dict[str, int] = {}
    calibration_only_no_trade_count = 0
    non_calibration_no_trade_count = 0

    def _add_reason(
        counter: Counter[str],
        messages: dict[str, str],
        ordering: dict[str, int],
        item: Any,
        *,
        fallback_code: str,
    ) -> str:
        if isinstance(item, dict):
            code = str(item.get("code") or fallback_code).strip().upper() or fallback_code
            message = str(item.get("msg") or item.get("message") or code).strip() or code
        else:
            code = fallback_code
            message = str(item or fallback_code).strip() or fallback_code
        if code not in ordering:
            ordering[code] = len(ordering)
        counter[code] += 1
        messages.setdefault(code, message)
        return code

    for row in rows:
        status = str(row["status"] or "unknown").strip().lower() or "unknown"
        if status not in status_counts:
            status = "unknown"
        status_counts[status] += 1
        reasons = _json_loads_mapping_or_default(row["reasons_json"], {})
        if status == "no_trade":
            decision_layers = reasons.get("decision_layers") if isinstance(reasons.get("decision_layers"), dict) else {}
            reason_items = decision_layers.get("no_trade_reasons") if isinstance(decision_layers.get("no_trade_reasons"), list) else []
            codes: list[str] = []
            for item in reason_items:
                codes.append(_add_reason(
                    no_trade_counts, no_trade_messages, no_trade_order, item,
                    fallback_code="UNSPECIFIED_NO_TRADE_REASON",
                ))
            if not codes:
                codes.append(_add_reason(
                    no_trade_counts, no_trade_messages, no_trade_order,
                    {
                        "code": "SCORE_OR_CONFIDENCE_BELOW_THRESHOLD",
                        "msg": "оценка или подтверждённая уверенность ниже порога допуска",
                    },
                    fallback_code="SCORE_OR_CONFIDENCE_BELOW_THRESHOLD",
                ))
            if codes and all(code in CALIBRATION_EVIDENCE_REASON_CODES for code in codes):
                calibration_only_no_trade_count += 1
            else:
                non_calibration_no_trade_count += 1
        elif status == "blocked":
            risk_checks = reasons.get("risk_checks") if isinstance(reasons.get("risk_checks"), dict) else {}
            block_items = risk_checks.get("blocks") if isinstance(risk_checks.get("blocks"), list) else []
            if not block_items:
                block_items = [{
                    "code": "UNSPECIFIED_HARD_BLOCK",
                    "msg": "жёсткая причина блокировки не детализирована",
                }]
            for item in block_items:
                _add_reason(
                    blocked_counts, blocked_messages, blocked_order, item,
                    fallback_code="UNSPECIFIED_HARD_BLOCK",
                )

    def _ranked(counter: Counter[str], messages: dict[str, str], ordering: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"code": code, "count": int(count), "message": messages.get(code, code)}
            for code, count in sorted(counter.items(), key=lambda item: (-item[1], ordering.get(item[0], 10**9)))
        ]

    actionable_count = status_counts["recommended"] + status_counts["active"]
    if actionable_count > 0:
        dominant_state = "actionable"
    elif status_counts["no_trade"] > 0 and calibration_only_no_trade_count == status_counts["no_trade"]:
        dominant_state = "calibration_evidence_pending"
    elif status_counts["blocked"] == len(rows) and rows:
        dominant_state = "all_blocked"
    else:
        dominant_state = "not_actionable"
    return {
        "latest_snapshot_ts": int(latest_ts),
        "latest_snapshot_age_sec": max(0, db.now_ts() - int(latest_ts)),
        "latest_snapshot_total": len(rows),
        "status_counts": status_counts,
        "actionable_count": actionable_count,
        "calibration_only_no_trade_count": calibration_only_no_trade_count,
        "non_calibration_no_trade_count": non_calibration_no_trade_count,
        "no_trade_reason_counts": _ranked(no_trade_counts, no_trade_messages, no_trade_order),
        "blocked_reason_counts": _ranked(blocked_counts, blocked_messages, blocked_order),
        "dominant_state": dominant_state,
    }


def _collector_runtime_state(
    *,
    collector_last_cycle: dict[str, Any],
    collector_thread_state: dict[str, Any],
    collector_cycle_age_sec: int | None,
    runtime_provenance: dict[str, Any],
) -> str:
    thread_state = str(collector_thread_state.get("state") or "").strip().lower()
    if thread_state == "error":
        return "error"
    current_cycle = bool(runtime_provenance.get("collector_cycle_current_process"))
    handover_active = bool(runtime_provenance.get("boot_grace_active")) and not current_cycle
    owner_matches = bool(runtime_provenance.get("collector_owner_matches_runtime"))
    if handover_active and thread_state == "running":
        return "starting" if owner_matches else "handover"
    stale_after = max(int(settings.collect_interval_sec) * 6, int(settings.stale_data_max_sec))
    if collector_cycle_age_sec is not None and collector_cycle_age_sec > stale_after:
        return "stalled"
    if collector_last_cycle:
        return "ok"
    if thread_state == "running":
        return "starting"
    return "unknown"


def _runtime_provenance_status(
    *,
    now_ts: int,
    collector_last_cycle: dict[str, Any],
    recommendation_readiness: dict[str, Any],
    collector_lock_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    collector_started_ts = int(collector_last_cycle.get("started_ts") or 0)
    collector_owner = str(collector_last_cycle.get("owner") or "").strip()
    latest_snapshot_ts = int(recommendation_readiness.get("latest_snapshot_ts") or 0)
    collector_cycle_current_process = collector_started_ts >= int(PROCESS_STARTED_TS)
    publication_current_process = latest_snapshot_ts >= int(PROCESS_STARTED_TS)
    collector_owner_matches_runtime = bool(collector_owner) and collector_owner == RUNTIME_OWNER
    boot_grace_sec = _runtime_handover_grace_sec()
    boot_grace_active = int(now_ts) < int(PROCESS_STARTED_TS) + int(boot_grace_sec)
    lock_snapshot = collector_lock_snapshot if isinstance(collector_lock_snapshot, dict) else {}
    lock_owner = str(lock_snapshot.get("owner") or "").strip() or None
    lock_heartbeat_ts = strict_integer(lock_snapshot.get("heartbeat_ts"))
    lock_ttl_sec = _collector_runtime_lock_ttl_sec()
    lock_takeover_in_sec = None
    if lock_heartbeat_ts is not None and lock_heartbeat_ts > 0 and lock_owner != RUNTIME_OWNER:
        lock_takeover_in_sec = max(0, int(lock_heartbeat_ts) + int(lock_ttl_sec) - int(now_ts))
    return {
        "runtime_owner": RUNTIME_OWNER,
        "process_started_ts": int(PROCESS_STARTED_TS),
        "process_age_sec": max(0, int(now_ts) - int(PROCESS_STARTED_TS)),
        "boot_grace_sec": int(boot_grace_sec),
        "boot_grace_active": bool(boot_grace_active),
        "collector_cycle_started_ts": collector_started_ts or None,
        "collector_cycle_owner": collector_owner or None,
        "collector_cycle_current_process": bool(collector_cycle_current_process),
        "collector_owner_matches_runtime": bool(collector_owner_matches_runtime),
        "collector_lock_owner": lock_owner,
        "collector_lock_heartbeat_ts": lock_heartbeat_ts,
        "collector_lock_ttl_sec": int(lock_ttl_sec),
        "collector_lock_takeover_in_sec": lock_takeover_in_sec,
        "collector_lock_owned_by_current_process": bool(lock_owner and lock_owner == RUNTIME_OWNER),
        "publication_ts": latest_snapshot_ts or None,
        "publication_current_process": bool(publication_current_process),
        "current_process_ready": bool(collector_cycle_current_process and publication_current_process),
    }


def _operator_runtime_readiness(
    *,
    schema_status: dict[str, Any],
    recommendation_readiness: dict[str, Any],
    outcome_worker: dict[str, Any],
    collector_state: str,
    background_threads: dict[str, Any],
    runtime_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    if not bool(schema_status.get("migration_applied")):
        issues.append({"code": "DATABASE_MIGRATION_MISSING", "message": "не все outcome-поля присутствуют в БД"})
    elif int(schema_status.get("materialization_pending") or 0) > 0:
        issues.append({"code": "DATABASE_MATERIALIZATION_PENDING", "message": "миграция применена, но часть старых рекомендаций ещё не материализована"})
    outcome_state = str(outcome_worker.get("state") or "unknown").lower()
    if outcome_state in {"stalled", "error"}:
        issues.append({"code": f"OUTCOME_WORKER_{outcome_state.upper()}", "message": "контур исходов не продвигает очередь"})
    if collector_state in {"stalled", "error"}:
        issues.append({"code": f"COLLECTOR_{collector_state.upper()}", "message": "контур рыночных данных не работает штатно"})
    required_threads = {"collector", "backfill", "futures_meta", "sentiment", "reco", "outcomes"}
    for name, info in background_threads.items():
        thread_state = str((info or {}).get("state") or "").lower()
        if thread_state == "error":
            issues.append({"code": f"THREAD_{str(name).upper()}_ERROR", "message": f"фоновый контур {name} завершился с ошибкой"})
        elif name in required_threads and thread_state == "stopped":
            issues.append({"code": f"THREAD_{str(name).upper()}_STOPPED", "message": f"обязательный фоновый контур {name} остановлен"})
        elif name in required_threads and not thread_state:
            issues.append({"code": f"THREAD_{str(name).upper()}_NOT_STARTED", "message": f"нет подтверждения запуска обязательного фонового контура {name}"})

    actionable_count = int(recommendation_readiness.get("actionable_count") or 0)
    snapshot_total = int(recommendation_readiness.get("latest_snapshot_total") or 0)
    provenance = runtime_provenance if isinstance(runtime_provenance, dict) else {}
    current_process_ready = bool(provenance.get("current_process_ready")) if provenance else True
    boot_grace_active = bool(provenance.get("boot_grace_active")) if provenance else False
    if provenance and not current_process_ready and not boot_grace_active:
        issues.append({
            "code": "CURRENT_PROCESS_CYCLE_STALLED",
            "message": "текущий процесс не завершил собственный цикл сбора и публикации после периода запуска",
        })
    if issues:
        state = "degraded"
    elif provenance and not current_process_ready:
        state = "starting"
    elif snapshot_total <= 0:
        state = "starting"
    elif actionable_count > 0:
        state = "ready"
    else:
        state = "healthy_not_actionable"

    explanations = list(issues)
    if not issues and provenance and not current_process_ready:
        if collector_state == "handover":
            explanations.append({
                "code": "RUNTIME_LOCK_HANDOVER",
                "message": "новый процесс запущен и ожидает безопасной передачи блокировки сборщика от предыдущего процесса",
            })
        explanations.append({
            "code": "CURRENT_PROCESS_CYCLE_PENDING",
            "message": "после перезапуска ожидается первый собственный цикл сбора данных и публикации текущего процесса",
        })
    dominant = str(recommendation_readiness.get("dominant_state") or "")
    if state != "starting" and not issues and dominant == "calibration_evidence_pending":
        explanations.append({
            "code": "CALIBRATION_EVIDENCE_PENDING",
            "message": "система работает, но текущий набор правил ещё не доказал положительную ожидаемость и/или вероятность вне обучения",
        })
    elif state != "starting" and not issues and actionable_count == 0 and snapshot_total > 0:
        explanations.append({
            "code": "NO_ACTIONABLE_RECOMMENDATIONS",
            "message": "инфраструктура работает, но текущие сигналы не прошли модельные, экономические или риск-условия",
        })
    return {
        "state": state,
        "runtime_healthy": not issues,
        "trading_actionable": actionable_count > 0,
        "issues": issues,
        "explanations": explanations,
    }


@app.get("/api/v1/status")
def api_status() -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        from .calibration import load_logreg_from_db, GLOBAL_LOGREG_KEY, BOT_CALIB_KEYS, label_balance_stats
        from .sentiment_features import compute_sentiment_agg

        llm_async_status = _get_app_config_mapping(conn, LLM_REVIEW_ASYNC_STATUS_APP_KEY, default={})
        collector_last_cycle = _get_app_config_mapping(conn, "collector_last_cycle", default={})
        backfill_last_cycle = _get_app_config_mapping(conn, "backfill_last_cycle", default={})
        futures_meta_last_cycle = _get_app_config_mapping(conn, "futures_meta_last_cycle", default={})
        collector_warmup = _load_collector_warmup_status(conn, recompute_if_missing=True)
        background_threads = {
            name: _get_background_thread_state(conn, name)
            for name in ("collector", "backfill", "futures_meta", "sentiment", "reco", "outcomes", "llm_reviewer")
        }
        now_ts_int = db.now_ts()
        collector_cycle_started_ts = int(collector_last_cycle.get("started_ts") or 0)
        collector_cycle_age_sec = None if collector_cycle_started_ts <= 0 else max(0, now_ts_int - collector_cycle_started_ts)
        collector_thread_state = background_threads.get("collector") or {}
        backfill_thread_state = background_threads.get("backfill") or {}
        futures_meta_thread_state = background_threads.get("futures_meta") or {}
        collector_runtime_state = "unknown"

        active_risk_limits = normalize_risk_limits(
            db.get_active_risk_limits(conn),
            settings.risk_limits,
        )
        policy_contract = calibration_policy_contract(settings, active_risk_limits)
        policy_fingerprint = calibration_policy_fingerprint(settings, active_risk_limits)
        global_calibrator_key = policy_calibration_storage_key(
            GLOBAL_LOGREG_KEY,
            policy_fingerprint,
        )
        direction_calibrator_key = policy_calibration_storage_key(
            DIRECTION_CALIBRATION_KEY,
            policy_fingerprint,
        )
        global_model = load_logreg_from_db(conn, global_calibrator_key)
        calib_fitted = bool(global_model and global_model.fitted)
        calib_n = int(global_model.n_samples) if global_model and global_model.fitted else 0
        calib_logreg = bool(global_model and global_model.fitted and len(global_model.coef) > 0)

        min_samples = int(settings.calib_min_samples)
        logreg_min_samples = 300
        label_horizon_sec = int(BOT_HORIZONS.get("futures_grid", 12 * 3600))
        minimum_temporal_clusters = max(1, min(20, int(math.ceil(float(min_samples) / 4.0))))
        minimum_temporal_span_sec = int(label_horizon_sec * minimum_temporal_clusters)

        require_llm_outcome_verdict = bool(
            getattr(settings, "llm_reviewer_enabled", False)
        )
        history_summary = db.get_outcome_history_summary(
            conn,
            require_llm_verdict=require_llm_outcome_verdict,
        )
        lineage = calibration_lineage_diagnostics(
            db.iter_calibration_lineage_rows(
                conn,
                require_llm_verdict=require_llm_outcome_verdict,
                current_model_version=RECOMMENDER_MODEL_VERSION,
            ),
            policy_fingerprint=policy_fingerprint,
            mean_reversion_min_score=settings.mean_reversion_min_score,
            retain_rows=False,
            recent_cutoff_ts=now_ts_int - 7 * 86400,
        )
        lineage["historical_total"] = int(history_summary["historical_total"])
        lineage["dropped_old_model"] = max(
            0,
            int(history_summary["historical_total"])
            - int(lineage.get("current_model_total") or 0),
        )
        lineage.setdefault("stats_by_bot", {})["historical"] = dict(
            history_summary.get("stats_by_bot") or {}
        )
        lineage_stats = lineage.get("stats_by_bot") or {}
        historical_stats_by_bot = lineage_stats.get("historical") or {}
        current_model_stats_by_bot = lineage_stats.get("current_model") or {}
        feature_stats_by_bot = lineage_stats.get("feature_eligible") or {}
        outcome_stats_by_bot = lineage_stats.get("policy_eligible") or {}
        outcome_stats_7d_by_bot = lineage_stats.get("policy_recent") or {}
        outcome_count = int(lineage["historical_total"])


        def _bot_gate(total: int, wins: int, losses: int, fitted: bool) -> tuple[bool, str | None]:
            if fitted:
                return True, None
            balance = label_balance_stats(([1] * int(wins)) + ([0] * int(losses)))
            effective = int(balance["effective_samples"])
            win_rate = balance["win_rate"]
            if effective < min_samples:
                return False, "not_enough_effective_samples"
            if win_rate is None:
                return False, "not_enough_effective_samples"
            if win_rate < 0.15 or win_rate > 0.85:
                return False, "degenerate_win_rate"
            return True, "pending_refit"

        bot_status = {}
        for bt, base_key in BOT_CALIB_KEYS.items():
            key = policy_calibration_storage_key(base_key, policy_fingerprint)
            m = load_logreg_from_db(conn, key)
            fitted = bool(m and m.fitted)
            logreg_active = bool(m and m.fitted and len(m.coef) > 0)
            stats = outcome_stats_by_bot.get(bt, {
                "total": 0, "wins": 0, "losses": 0, "win_rate": None,
                "minority_class_count": 0, "effective_samples": 0, "class_entropy_bits": 0.0,
            })
            historical_stats = historical_stats_by_bot.get(bt, {"total": 0})
            current_stats = current_model_stats_by_bot.get(bt, {"total": 0})
            feature_stats = feature_stats_by_bot.get(bt, {"total": 0})
            observability = db.get_policy_outcome_observability(
                conn,
                model_version=RECOMMENDER_MODEL_VERSION,
                policy_fingerprint=policy_fingerprint,
                bot_type=bt,
                require_llm_verdict=require_llm_outcome_verdict,
            )
            eligible, unfitted_reason = _bot_gate(
                int(stats["total"]),
                int(stats["wins"]),
                int(stats.get("losses", max(0, int(stats["total"]) - int(stats["wins"])))),
                fitted,
            )
            recent7d = outcome_stats_7d_by_bot.get(bt, {"total": 0, "wins": 0, "losses": 0})
            confidence_mode = "raw_only"
            if fitted and logreg_active:
                confidence_mode = "bot_logreg"
            elif fitted:
                confidence_mode = "invalid_legacy_state"
            fit_rows = int(m.n_samples) if m is not None else 0
            bot_status[bt] = {
                "fitted": fitted,
                "logreg_active": logreg_active,
                "n_samples": fit_rows,
                "return_samples": int(getattr(m, "return_samples", 0) or 0) if m is not None else 0,
                "rows_dropped_for_fit": max(0, int(stats["total"]) - fit_rows),
                "last_fit_ts": int(m.saved_ts) if m is not None else 0,
                "confidence_mode": confidence_mode,
                "calibrator_key": key,
                "calibrator_base_key": base_key,
                "calibration_model_version": RECOMMENDER_MODEL_VERSION,
                "policy_fingerprint": policy_fingerprint,
                "expectancy_status": str(getattr(m, "expectancy_status", "insufficient")) if m is not None else "insufficient",
                "temporal_cluster_count": int(getattr(m, "temporal_cluster_count", 0) or 0) if m is not None else 0,
                "minimum_temporal_clusters": int(getattr(m, "minimum_temporal_clusters", 0) or 0) if m is not None else 0,
                "historical_outcomes_total": int(historical_stats.get("total", 0)),
                "current_model_outcomes_total": int(current_stats.get("total", 0)),
                "feature_eligible_outcomes_total": int(feature_stats.get("total", 0)),
                "policy_eligible_outcomes_total": int(stats["total"]),
                "outcomes_total": int(stats["total"]),
                "wins": int(stats["wins"]),
                "losses": int(stats.get("losses", max(0, int(stats["total"]) - int(stats["wins"])))),
                "minority_class_count": int(stats.get("minority_class_count", 0)),
                "effective_samples": int(stats.get("effective_samples", 0)),
                "win_rate": stats["win_rate"],
                "class_entropy_bits": float(stats.get("class_entropy_bits", 0.0)),
                "wins_7d": int(recent7d.get("wins", 0)),
                "losses_7d": int(recent7d.get("losses", 0)),
                "outcomes_7d": int(recent7d.get("total", 0)),
                "eligible_for_fit": bool(eligible),
                "unfitted_reason": unfitted_reason,
                "min_samples": min_samples,
                "logreg_min_samples": logreg_min_samples,
                "monetary_min_samples": min_samples,
                "probability_min_samples": logreg_min_samples,
                "full_actionability_sample_floor": (
                    logreg_min_samples if bool(settings.require_conf_gate) else min_samples
                ),
                "monetary_sample_gap": max(0, min_samples - int(stats["total"])),
                "probability_sample_gap": max(0, logreg_min_samples - int(stats["total"])),
                "observability_hard_block": bool(
                    int(observability.get("censored_total") or 0) > 0
                    or int(observability.get("unresolved_total") or 0) > 0
                    or int(observability.get("invalid_labeled_total") or 0) > 0
                ),
                "purged_oof_status": str(getattr(m, "oof_status", "not_evaluated")) if m is not None else "not_evaluated",
                "purged_oof_skill_status": str(getattr(m, "oof_skill_status", "not_evaluated")) if m is not None else "not_evaluated",
                "purged_oof_feature_log_loss": getattr(m, "oof_feature_log_loss", None) if m is not None else None,
                "purged_oof_score_log_loss": getattr(m, "oof_score_log_loss", None) if m is not None else None,
                "purged_oof_null_log_loss": getattr(m, "oof_null_log_loss", None) if m is not None else None,
                "purged_oof_final_samples": int(getattr(m, "oof_final_samples", 0) or 0) if m is not None else 0,
                "purged_oof_required_final_samples": int(getattr(m, "oof_required_final_samples", 0) or 0) if m is not None else 0,
                "purged_oof_final_decision_cohorts": int(getattr(m, "oof_final_decision_cohorts", 0) or 0) if m is not None else 0,
                "purged_oof_required_final_decision_cohorts": int(getattr(m, "oof_required_final_decision_cohorts", 0) or 0) if m is not None else 0,
                "selected_policy_expectancy_status": str(getattr(m, "selected_policy_expectancy_status", "not_evaluated")) if m is not None else "not_evaluated",
                "selected_policy_confidence_threshold": getattr(m, "selected_policy_confidence_threshold", None) if m is not None else None,
                "selected_policy_samples": int(getattr(m, "selected_policy_samples", 0) or 0) if m is not None else 0,
                "selected_policy_weighted_mean_return": getattr(m, "selected_policy_weighted_mean_return", None) if m is not None else None,
                "selected_policy_weighted_mean_return_lower_bound": getattr(m, "selected_policy_weighted_mean_return_lower_bound", None) if m is not None else None,
                "selected_policy_temporal_cluster_count": int(getattr(m, "selected_policy_temporal_cluster_count", 0) or 0) if m is not None else 0,
                "selected_policy_minimum_temporal_clusters": int(getattr(m, "selected_policy_minimum_temporal_clusters", 0) or 0) if m is not None else 0,
                "selected_policy_weighted_temporal_mean_return_lower_bound": getattr(m, "selected_policy_weighted_temporal_mean_return_lower_bound", None) if m is not None else None,
                "terminal_selected_policy_expectancy_status": str(getattr(m, "terminal_selected_policy_expectancy_status", "not_evaluated")) if m is not None else "not_evaluated",
                "terminal_selected_policy_samples": int(getattr(m, "terminal_selected_policy_samples", 0) or 0) if m is not None else 0,
                "terminal_selected_policy_required_samples": int(getattr(m, "terminal_selected_policy_required_samples", 0) or 0) if m is not None else 0,
                "terminal_selected_policy_weighted_mean_return": getattr(m, "terminal_selected_policy_weighted_mean_return", None) if m is not None else None,
                "terminal_selected_policy_weighted_mean_return_lower_bound": getattr(m, "terminal_selected_policy_weighted_mean_return_lower_bound", None) if m is not None else None,
                "terminal_selected_policy_temporal_cluster_count": int(getattr(m, "terminal_selected_policy_temporal_cluster_count", 0) or 0) if m is not None else 0,
                "terminal_selected_policy_required_temporal_clusters": int(getattr(m, "terminal_selected_policy_required_temporal_clusters", 0) or 0) if m is not None else 0,
                "terminal_selected_policy_weighted_temporal_mean_return_lower_bound": getattr(m, "terminal_selected_policy_weighted_temporal_mean_return_lower_bound", None) if m is not None else None,
                "policy_matured_total": int(observability.get("matured_total") or 0),
                "policy_labeled_total": int(observability.get("labeled_total") or 0),
                "policy_censored_total": int(observability.get("censored_total") or 0),
                "policy_unresolved_total": int(observability.get("unresolved_total") or 0),
                "policy_invalid_contract_total": int(observability.get("invalid_contract_total") or 0),
                "policy_invalid_labeled_total": max(
                    int(observability.get("invalid_labeled_total") or 0),
                    int(getattr(m, "policy_invalid_labeled_total", 0) or 0)
                    if m is not None
                    else 0,
                ),
                "policy_censor_reasons": dict(observability.get("censor_reasons") or {}),
            }

        last_reco_ts = db.get_latest_reco_ts(conn)
        cur = conn.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR' AND ts >= ?", (db.now_ts() - 600,))
        collect_errors_10m = int(cur.fetchone()["c"])
        sent = compute_sentiment_agg(conn, scope="global", key="crypto")
        market_shock = _get_app_config_mapping(conn, MARKET_SHOCK_APP_KEY, default={"state": "normal", "title": "Нормальный режим", "severity": "normal", "entry_mode": "normal", "operator_note": "Новые входы разрешены в обычном режиме.", "reasons": [], "metrics": {}})

        outcome_worker_liveness = db.get_outcome_worker_liveness(
            conn,
            require_llm_verdict=require_llm_outcome_verdict,
            worker_stale_after_sec=_outcome_worker_stale_after_sec(),
        )
        database_schema = db.get_outcome_policy_schema_status(conn)
        database_continuity = db.get_database_continuity_status(conn)
        recommendation_readiness = _latest_recommendation_readiness(conn)
        collector_lock_snapshot: dict[str, Any] = {}
        try:
            with closing(_get_lock_conn()) as lock_conn:
                collector_lock_snapshot = db.get_runtime_lock_snapshot(lock_conn, "runtime:collector")
        except Exception:
            logger.warning("collector runtime lock diagnostics unavailable", exc_info=True)
        runtime_provenance = _runtime_provenance_status(
            now_ts=now_ts_int,
            collector_last_cycle=collector_last_cycle,
            recommendation_readiness=recommendation_readiness,
            collector_lock_snapshot=collector_lock_snapshot,
        )
        collector_runtime_state = _collector_runtime_state(
            collector_last_cycle=collector_last_cycle,
            collector_thread_state=collector_thread_state,
            collector_cycle_age_sec=collector_cycle_age_sec,
            runtime_provenance=runtime_provenance,
        )
        operator_readiness = _operator_runtime_readiness(
            schema_status=database_schema,
            recommendation_readiness=recommendation_readiness,
            outcome_worker=outcome_worker_liveness,
            collector_state=collector_runtime_state,
            background_threads=background_threads,
            runtime_provenance=runtime_provenance,
        )

        inference_ready_bot_count = sum(1 for info in bot_status.values() if bool(info.get("fitted")))
        inference_supported_bot_count = len(bot_status)
        if inference_ready_bot_count == 0:
            confidence_mode_in_use = "raw_only"
        elif inference_ready_bot_count == inference_supported_bot_count:
            confidence_mode_in_use = "bot_specific_only"
        else:
            confidence_mode_in_use = "mixed_bot_and_raw"

        return {
            "app_version": app.version,
            "operator_readiness": operator_readiness,
            "recommendation_readiness": recommendation_readiness,
            "runtime_provenance": runtime_provenance,
            "database_schema": database_schema,
            "database_continuity": database_continuity,
            "calibrator_fitted": calib_fitted,
            "calibrator_logreg": calib_logreg,
            "calibrator_n": calib_n,
            "global_calibrator_diagnostic_only": True,
            "inference_calibration_mode": confidence_mode_in_use,
            "confidence_mode_in_use": confidence_mode_in_use,
            "outcome_label_version": OUTCOME_LABEL_VERSION,
            "calibration_model_version": RECOMMENDER_MODEL_VERSION,
            "calibration_policy_fingerprint": policy_fingerprint,
            "calibration_policy_contract": policy_contract,
            "global_calibrator_key": global_calibrator_key,
            "global_calibrator_base_key": GLOBAL_LOGREG_KEY,
            "direction_calibrator_key": direction_calibrator_key,
            "historical_outcome_count": int(lineage["historical_total"]),
            "current_model_outcome_count": int(lineage["current_model_total"]),
            "feature_eligible_outcome_count": int(lineage["feature_eligible_total"]),
            "calibration_eligible_outcome_count": int(lineage["policy_eligible_total"]),
            "calibration_lineage_drops": {
                "old_model": int(lineage["dropped_old_model"]),
                "invalid_feature_evidence": int(lineage["dropped_invalid_feature_evidence"]),
                "candidate_policy": int(lineage["dropped_candidate_policy"]),
                "invalid_policy_maturity": int(lineage["dropped_invalid_policy_maturity"]),
                "invalid_policy_contract": int(lineage["dropped_invalid_policy_contract"]),
                "not_matured": int(lineage["dropped_not_matured"]),
            },
            "inference_ready_bot_count": inference_ready_bot_count,
            "inference_supported_bot_count": inference_supported_bot_count,
            "calibrator_params": {
                "a": float(global_model.platt.a) if global_model and global_model.fitted else None,
                "b": float(global_model.platt.b) if global_model and global_model.fitted else None,
            },
            "bot_calibrators": bot_status,
            "outcome_count": outcome_count,
            "outcome_worker": outcome_worker_liveness,
            "calib_min_samples": min_samples,
            "calib_logreg_min_samples": logreg_min_samples,
            "calibration_gate_contract": {
                "require_conf_gate": bool(settings.require_conf_gate),
                "monetary_min_samples": min_samples,
                "probability_min_samples": logreg_min_samples,
                "full_actionability_sample_floor": (
                    logreg_min_samples if bool(settings.require_conf_gate) else min_samples
                ),
                "bounded_censoring_sensitivity_enabled": True,
                "maximum_censoring_rate": 0.05,
                "unresolved_or_invalid_root_blocks_positive_expectancy": True,
                "any_unresolved_root_blocks_positive_expectancy": True,
                "label_horizon_sec": label_horizon_sec,
                "minimum_temporal_clusters": minimum_temporal_clusters,
                "minimum_temporal_span_sec": minimum_temporal_span_sec,
                "minimum_temporal_span_days": round(minimum_temporal_span_sec / 86400.0, 2),
                "policy_fingerprint_change_starts_new_cohort": True,
                "probability_requires_purged_oof_skill": True,
                "probability_requires_positive_selected_policy_expectancy": True,
                "probability_requires_positive_terminal_selected_policy_expectancy": True,
                "terminal_holdout_min_samples": min_samples,
                "terminal_holdout_min_decision_cohorts": 5,
                "terminal_holdout_preserves_whole_decision_timestamps": True,
                "terminal_selected_policy_min_samples": min_samples,
                "terminal_selected_policy_min_decision_cohorts": 5,
                "note": (
                    "The 80-row floor proves neither a fitted probability model nor actionability. "
                    "With REQUIRE_CONF_GATE=1, at least 300 exact-policy labeled rows plus accepted "
                    "purged OOF skill, a whole-timestamp terminal holdout, and positive monetary "
                    "evidence for the exact confidence-selected subset are required."
                ),
            },
            "last_reco_ts": last_reco_ts,
            "collect_errors_10m": collect_errors_10m,
            "admin_key_configured": bool(settings.admin_api_key),
            "sentiment": {
                "regime": sent.get("regime"),
                "strength": sent.get("strength"),
                "ewma_6h": sent.get("ewma", {}).get("6h"),
                "flags": sent.get("flags"),
                "data_quality": sent.get("data_quality"),
            },
            "market_shock": market_shock,
            "llm_reviewer": {
                "enabled": bool(getattr(settings, "llm_reviewer_enabled", False)),
                "mode": getattr(settings, "llm_reviewer_mode", "advisory"),
                "provider": getattr(settings, "llm_reviewer_provider", "ollama"),
                "model": getattr(settings, "llm_reviewer_model", None),
                "tf_secs": list(getattr(settings, "llm_reviewer_tf_secs", []) or []),
                "candles_per_tf": int(getattr(settings, "llm_reviewer_candles_per_tf", 32) or 32),
                "max_candidates": int(getattr(settings, "llm_reviewer_max_candidates", 24) or 24),
                "max_workers": int(getattr(settings, "llm_reviewer_max_workers", 2) or 2),
                "async_mode": True,
                "min_confidence": float(getattr(settings, "llm_reviewer_min_confidence", 0.65) or 0.65),
                "cadence_sec": int(getattr(settings, "llm_reviewer_cadence_sec", 300) or 300),
                "pending_timeout_sec": int(getattr(settings, "llm_reviewer_pending_timeout_sec", 900) or 900),
                "keep_alive": str(getattr(settings, "llm_reviewer_keep_alive", "90s") or "90s"),
                "worker": llm_async_status,
                "thread": background_threads.get("llm_reviewer") or {},
            },
            "collector": {
                **collector_last_cycle,
                "max_workers": int(getattr(settings, "collector_max_workers", 1) or 1),
                "futures_max_workers": int(getattr(settings, "futures_collect_max_workers", 1) or 1),
                "warmup": collector_warmup,
                "thread": collector_thread_state,
                "cycle_age_sec": collector_cycle_age_sec,
                "state": collector_runtime_state,
            },
            "backfill": {
                **backfill_last_cycle,
                "thread": backfill_thread_state,
            },
            "futures_meta": {
                **futures_meta_last_cycle,
                "thread": futures_meta_thread_state,
            },
            "background_threads": background_threads,
        }


def main():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
