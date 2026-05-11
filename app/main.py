from __future__ import annotations

import copy
import json
import math
import os
import secrets
import threading
import socket
import time
from functools import partial
from contextlib import closing, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
from .outcomes import compute_outcomes_once
from .recommender import run_recommender_once, run_llm_review_sweep_once, LLM_REVIEW_ASYNC_STATUS_APP_KEY
from .risk import get_risk_limits, compute_risk_status, gate_candidate, normalize_risk_limits
from .security import is_authorized
from . import db
from .db_backend import describe_target
from .bot_types import sql_in_clause
from .grid_math import estimate_linear_liq_price, liquidation_buffer_pct, quantize_step
import logging

logger = logging.getLogger(__name__)
settings = load_settings()
RUNTIME_OWNER = f"{socket.gethostname()}:{os.getpid()}"
PROCESS_STARTED_TS = int(time.time())
OUTCOME_LABEL_VERSION = "grid_label_v2"
INSTRUMENT_META_CACHE_TTL_SEC = 15 * 60
INSTRUMENT_META_NEGATIVE_CACHE_TTL_SEC = 30
SUPPORTED_RECOMMENDER_GRID_TYPE = "arithmetic"
BYBIT_FUTURES_GRID_MIN_COUNT = 2
BYBIT_FUTURES_GRID_MAX_COUNT = 400
EXECUTION_FUNDING_MAX_STALENESS_SEC = 60 * 60
EXECUTION_FUNDING_WORSE_DELTA_BLOCK_BPS = 3.0
EXECUTION_FUNDING_EXTREME_BPS = 6.0
BACKGROUND_THREAD_STATE_APP_KEY_PREFIX = "runtime_thread_state:"
BACKGROUND_THREAD_RESTART_DELAY_SEC = 5.0
BACKGROUND_THREAD_ERROR_ACTIONS = {
    "collector": "COLLECT_ERROR",
    "reco": "RECO_ERROR",
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


def _bootstrap_db() -> None:
    with closing(_get_conn()) as conn:
        db.init_db(conn)
        active = db.get_active_risk_limits(conn)
        if not active:
            bootstrap_limits = normalize_risk_limits(settings.risk_limits, settings.risk_limits)
            db.upsert_risk_limits(conn, version="bootstrap", limits=bootstrap_limits, is_active=True)

        current_label_version = db.get_app_config_json(conn, "outcome_label_version")
        if current_label_version != OUTCOME_LABEL_VERSION:
            from .calibration import BOT_CALIB_KEYS, GLOBAL_LOGREG_KEY

            deleted_outcomes = conn.execute("DELETE FROM reco_outcomes").rowcount
            keys_to_delete = [GLOBAL_LOGREG_KEY, *BOT_CALIB_KEYS.values(), "platt_direction_v3"]
            qmarks = ",".join("?" for _ in keys_to_delete)
            deleted_calibrators = 0
            if qmarks:
                deleted_calibrators = conn.execute(
                    f"DELETE FROM app_config WHERE key IN ({qmarks})",
                    keys_to_delete,
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
            "category": str(info.get("category") or category).strip().lower() or category,
            "symbol": str(info.get("symbol") or symbol_norm).strip().upper() or symbol_norm,
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
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


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


def _existing_trade_matches_request(existing: dict[str, Any] | None, *, bot_id: str, symbol: str, ts: int | None, pnl: float, fee: float, meta: dict[str, Any]) -> bool:
    if not existing:
        return False
    if str(existing.get("bot_id") or "") != str(bot_id):
        return False
    if str(existing.get("symbol") or "") != str(symbol):
        return False
    if ts is not None and _safe_int(existing.get("ts"), 0) != int(ts):
        return False
    try:
        pnl_match = math.isclose(float(existing.get("pnl") or 0.0), float(pnl), rel_tol=1e-12, abs_tol=1e-12)
        fee_match = math.isclose(float(existing.get("fee") or 0.0), float(fee), rel_tol=1e-12, abs_tol=1e-12)
    except Exception:
        return False
    if not (pnl_match and fee_match):
        return False
    return (existing.get("meta") or {}) == (meta or {})


def _llm_status_from_reasons_dict(reasons: Any) -> str:
    if not isinstance(reasons, dict):
        return "none"
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else None
    if not isinstance(llm_review, dict):
        return "none"
    return str(llm_review.get("status") or "none").strip().lower() or "none"


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

    # Shape-normalized legacy rows with malformed JSON payloads are exposed as
    # empty params/reasons/blocks by the API. Keep that defensive fail-open
    # contract intact instead of rebuilding JSON objects from invalid storage.
    params_payload = out.get("params")
    if not isinstance(params_payload, dict) or not params_payload:
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
        out["status"] = "blocked"

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


def _augment_reco_for_ui(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    venue = str(out.get("venue") or "")
    symbol = str(out.get("symbol") or "")
    try:
        bybit_meta = _fetch_bybit_instrument_meta(venue, symbol) if venue and symbol else {}
    except Exception:
        bybit_meta = {}
    if not isinstance(out.get("params"), dict) or not out.get("params"):
        out["bybit_meta"] = bybit_meta
        out["bybit_plan_validation"] = _validate_trade_plan_against_bybit_meta(out, bybit_meta)
        out["bybit_operator_guard"] = {
            "ok": True,
            "critical": False,
            "errors": [],
            "warnings": [{
                "code": "PAYLOAD_UNAVAILABLE_FOR_OPERATOR_GUARD",
                "msg": "params_json пустой или повреждён; operator guard не меняет audit-status в списке, но execution-preflight всё равно заблокирует запуск без полного trade_plan.",
            }],
            "meta_checked": bool(bybit_meta),
            "snapped_levels": {},
        }
        _apply_llm_effective_pending_guard(out)
        return out
    out = _snap_reco_payload_to_bybit_meta(out, bybit_meta)
    out["bybit_meta"] = bybit_meta
    out["bybit_plan_validation"] = _validate_trade_plan_against_bybit_meta(out, bybit_meta)
    out["bybit_operator_guard"] = _validate_trade_plan_against_bybit_meta(out, bybit_meta, require_meta=True)
    _merge_bybit_operator_guard_into_ui_payload(out, out["bybit_operator_guard"])
    _apply_llm_effective_pending_guard(out)
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
        effective_status = str(item.get("status") or "").strip().lower()
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

    sizing_candidates = [
        params.get("sizing") if isinstance(params.get("sizing"), dict) else {},
        plan.get("sizing") if isinstance(plan.get("sizing"), dict) else {},
        operator_sheet.get("sizing") if isinstance(operator_sheet, dict) and isinstance(operator_sheet.get("sizing"), dict) else {},
    ]
    auto_snap_allowed = any(
        str(candidate.get("basis") or "").strip() == "minimum_viable_operator_default"
        or (
            isinstance(candidate.get("exchange_filter_assumption"), dict)
            and str(candidate["exchange_filter_assumption"].get("mode") or "").strip() == "fallback_qty_step_until_bybit_preflight"
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
        grid_count = int(_safe_int_or_none(params.get("grid_count")) or _safe_int_or_none(plan.get("grid_count")) or _safe_int_or_none(params.get("grid_levels")) or 0)
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
        # per-leg edge after fees, spread, slippage and funding.
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
        min_required_qty = max(0.0, float(min_order_qty or 0.0))
        if min_notional is not None and notional_price is not None and notional_price > 0:
            min_required_qty = max(min_required_qty, float(min_notional) / float(notional_price))
        raw_qty = max(float(order_qty or 0.0), min_required_qty)
        snapped_qty = snap(raw_qty, qty_step, mode="up")
        if snapped_qty is not None and snapped_qty > 0:
            for mapping in sizing_maps:
                for key in ("qty_per_order", "order_qty"):
                    if key in mapping or key == "qty_per_order":
                        mapping[key] = float(snapped_qty)
            if reference_price is not None and reference_price > 0:
                order_notional = float(snapped_qty) * float(reference_price)
                grid_count = int(_safe_int_or_none(params.get("grid_count")) or _safe_int_or_none(params.get("grid_levels")) or _safe_int_or_none(plan.get("grid_count")) or 1)
                leverage_used = float(params.get("leverage") or 1.0) or 1.0
                total_notional = order_notional * max(1, grid_count)
                margin_required = total_notional / max(1.0, leverage_used)
                for mapping in sizing_maps:
                    for key in ("order_notional_usdt", "order_notional"):
                        if key in mapping or key == "order_notional_usdt":
                            mapping[key] = float(order_notional)
                    if "estimated_total_order_notional_usdt" in mapping:
                        mapping["estimated_total_order_notional_usdt"] = float(total_notional)
                    if "estimated_margin_required_usdt" in mapping:
                        mapping["estimated_margin_required_usdt"] = float(margin_required)
                    if "estimated_max_position_notional_usdt" in mapping:
                        mapping["estimated_max_position_notional_usdt"] = float(total_notional)
                risk_report = params.get("risk_report") if isinstance(params.get("risk_report"), dict) else None
                if risk_report is not None:
                    risk_report["capital_required_usdt"] = float(margin_required)
                if isinstance(operator_sheet, dict) and isinstance(operator_sheet.get("economics"), dict):
                    operator_sheet["economics"]["capital_required_usdt"] = float(margin_required)

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
        value = _finite_float_or_none(raw)
        if value is not None and value > 0:
            return int(min(max(value, 6 * 3600), 48 * 3600))
    for raw in (
        params.get("label_horizon_hours"),
        plan.get("label_horizon_hours"),
        _first_mapping(plan.get("expected_horizon")).get("label_horizon_hours"),
    ):
        value = _finite_float_or_none(raw)
        if value is not None and value > 0:
            return int(min(max(value * 3600.0, 6 * 3600), 48 * 3600))
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
    if interval_sec <= 0 or horizon_sec <= 0:
        return 0
    next_ts = _finite_float_or_none(next_funding_ts)
    if next_ts is None or next_ts <= 0:
        # If Bybit/DB has the funding interval but not the next event timestamp,
        # execution preflight must not assume that only one event can occur. The
        # first event may be minutes away and a futures grid can carry inventory
        # across every boundary in the label horizon. Match recommendation-time
        # economics: use a conservative ceil(horizon / interval), capped only to
        # avoid absurd legacy horizons.
        return min(32, max(1, int(math.ceil(float(horizon_sec) / float(interval_sec)))))
    # Bybit and some fixtures can provide ms timestamps; normalize defensively.
    if next_ts > 10_000_000_000:
        next_ts = next_ts / 1000.0
    next_int = int(next_ts)
    now_int = int(now_ts)
    while next_int <= now_int:
        next_int += int(interval_sec)
    horizon_end = now_int + int(horizon_sec)
    events = 0
    ts = next_int
    while ts <= horizon_end and events < 32:
        events += 1
        ts += int(interval_sec)
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

    ts = _safe_int(funding.get("ts"), default=0)
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

    interval_min = _finite_float_or_none(funding.get("funding_interval_min"))
    if interval_min is None or interval_min <= 0:
        return [{"code": "FUNDING_INTERVAL_UNAVAILABLE_AT_EXECUTION", "msg": f"{symbol}: funding_interval_min отсутствует; нельзя оценить carry до горизонта сделки."}]
    interval_sec = int(round(interval_min * 60.0))
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



def _trade_plan_price_context(rec: dict[str, Any]) -> dict[str, Any]:
    """Достаёт ценовой контекст trade_plan в едином виде для preflight.

    В execution-time проверках нельзя повторять парсинг JSON руками в нескольких
    местах: любое расхождение между Bybit-валидацией и live-price guard создаёт
    окно, где один слой считает сетку допустимой, а другой уже не видит её
    границы. Helper намеренно возвращает только finite-числа или None.
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
    return {
        "params": params,
        "plan": plan,
        "levels": levels,
        "range": range_levels,
        "kill_switch": kill_switch,
        "grid_step": grid_step,
        "tp_per_leg": tp_per_leg,
        "reference_price": _finite_float_or_none(plan.get("reference_price")),
        "range_lower": _finite_float_or_none(range_levels.get("lower")),
        "range_upper": _finite_float_or_none(range_levels.get("upper")),
        "kill_switch_lower": _finite_float_or_none(kill_switch.get("lower")),
        "kill_switch_upper": _finite_float_or_none(kill_switch.get("upper")),
        "grid_step_abs": _finite_float_or_none(grid_step.get("step_abs")),
        "grid_type": str(params.get("grid_type") or plan.get("grid_type") or "").strip().lower(),
        "grid_levels": _safe_int_or_none(params.get("grid_count")) or _safe_int_or_none(plan.get("grid_count")) or _safe_int_or_none(params.get("grid_levels")),
        "tp_per_leg_abs": _finite_float_or_none(tp_per_leg.get("abs")),
        "tp_per_leg_pct": _finite_float_or_none(tp_per_leg.get("pct")),
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


def _execution_live_price_blocks(conn, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Fail-closed защита от исполнения сетки по уже уехавшей цене.

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
    leverage = _finite_float_or_none(params.get("leverage"))

    bot_type = str(rec.get("bot_type") or "").strip()
    venue = str(rec.get("venue") or "").strip().lower()
    direction = str(rec.get("direction") or "").strip().lower()
    account_mode = str(rec.get("account_mode") or "").strip().lower()
    margin_mode = str(rec.get("margin_mode") or params.get("margin_mode") or "").strip().lower()
    meta_category = str((meta or {}).get("category") or "").strip().lower()
    meta_symbol = str((meta or {}).get("symbol") or "").strip().upper()
    meta_status = str((meta or {}).get("status") or "").strip()
    meta_contract_type = str((meta or {}).get("contract_type") or "").strip()
    meta_quote_coin = str((meta or {}).get("quote_coin") or "").strip().upper()
    meta_settle_coin = str((meta or {}).get("settle_coin") or "").strip().upper()
    meta_delivery_time = _finite_float_or_none((meta or {}).get("delivery_time"))
    meta_is_pre_listing = (meta or {}).get("is_pre_listing")
    rec_symbol = str(rec.get("symbol") or "").strip().upper()

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    snapped: dict[str, str] = {}

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
    if not plan:
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
        if account_mode == "one_way":
            warnings.append({"code": "ACCOUNT_MODE_LEGACY_ALIAS", "msg": "account_mode=one_way трактуется как legacy-алиас позиции/position-mode; штатное значение этой ревизии — account_mode=unified."})
        elif account_mode and account_mode != "unified":
            warnings.append({"code": "ACCOUNT_MODE_UNEXPECTED", "msg": f"futures_grid обычно ожидает account_mode=unified, получено {account_mode}."})
        # Проектная логика, risk-gates и operator guidance собраны вокруг isolated futures-grid.
        # Поддержку cross/hedge-mode здесь лучше явно блокировать, чем молча притворяться совместимой.
        if not margin_mode:
            errors.append({"code": "MARGIN_MODE_MISSING", "msg": "futures_grid требует явный margin_mode=isolated; legacy/manual recommendation без режима исполнения блокируется fail-closed."})
        elif margin_mode != "isolated":
            errors.append({"code": "MARGIN_MODE_UNSUPPORTED", "msg": f"futures_grid в этом проекте поддерживается только в margin_mode=isolated, получено {margin_mode}."})
        if rec_symbol and not _is_exact_linear_usdt_symbol(rec_symbol):
            errors.append({"code": "USDT_PERPETUAL_SYMBOL_REQUIRED", "msg": f"futures_grid поддерживается только для точных alphanumeric USDT perpetual symbols без разделителей, получено symbol={rec_symbol}."})

    if bot_type == "futures_grid" and meta:
        def _meta_target():
            return errors if require_meta else warnings

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

    if meta_symbol and rec_symbol and meta_symbol != rec_symbol:
        errors.append({
            "code": "BYBIT_META_SYMBOL_MISMATCH",
            "msg": f"Metadata Bybit получена для symbol={meta_symbol}, тогда как recommendation ожидает symbol={rec_symbol}; применять такие ограничения опасно.",
        })

    if meta_category and venue and meta_category != venue:
        errors.append({
            "code": "BYBIT_META_CATEGORY_MISMATCH",
            "msg": f"Metadata Bybit получена для category={meta_category}, тогда как recommendation ожидает venue={venue}; применять такие ограничения небезопасно.",
        })

    if meta and meta_status and meta_status.lower() != "trading":
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
    tp_abs = ctx["tp_per_leg_abs"]
    tp_pct = ctx["tp_per_leg_pct"]

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

    # Linear USDT perpetual supports leverage. Do not blanket-block leverage > 1;
    # instead enforce Bybit leverageFilter above and require a liquidation buffer
    # estimate. For neutral grids validate the worse side because inventory can
    # accumulate either long or short as the range is traversed.
    if leverage is not None and venue == "linear" and leverage > 1:
        economics = params.get("economics") if isinstance(params.get("economics"), dict) else {}
        liq_buffer_pct = _finite_float_or_none(economics.get("liquidation_buffer_pct"))
        if liq_buffer_pct is None and reference_price is not None:
            candidate_buffers: list[float] = []
            sides = ("long", "short") if direction == "neutral" else (direction,)
            for side in sides:
                estimated = estimate_linear_liq_price(side, reference_price, leverage)
                liq = float(estimated) if estimated is not None else None
                buf = liquidation_buffer_pct(side, reference_price, liq) if liq is not None else None
                if buf is not None:
                    candidate_buffers.append(float(buf))
            if candidate_buffers:
                liq_buffer_pct = min(candidate_buffers)
        if liq_buffer_pct is None:
            warnings.append({
                "code": "LIQUIDATION_BUFFER_NOT_ESTIMATED",
                "msg": "Leverage > 1 требует оценки worst-side liquidation buffer; точная ликвидация зависит от risk tier и маржи аккаунта.",
            })
        elif liq_buffer_pct < 12.0:
            errors.append({
                "code": "LIQUIDATION_BUFFER_TOO_LOW",
                "msg": f"Оценочный worst-side liquidation buffer={liq_buffer_pct:.2f}% слишком мал для запуска futures grid с leverage={leverage}.",
            })

    sizing = plan.get("sizing") if isinstance(plan.get("sizing"), dict) else {}
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
    qty_source, order_qty = _first_finite_from_mapping(sizing, qty_keys)
    if order_qty is None:
        qty_source, order_qty = _first_finite_from_mapping(params, qty_keys)
    notional_source, order_notional = _first_finite_from_mapping(sizing, notional_keys)
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

    if order_qty is not None and order_notional is not None and reference_price is not None and reference_price > 0:
        implied_notional = order_qty * reference_price
        tolerance = max(0.01, abs(implied_notional) * 0.005)
        if abs(implied_notional - order_notional) > tolerance:
            errors.append({
                "code": "ORDER_QTY_NOTIONAL_MISMATCH",
                "msg": f"{qty_source or 'order_qty'} * reference_price = {implied_notional:.12g} USDT, но {notional_source or 'order_notional'}={order_notional:.12g}; sizing payload внутренне несогласован.",
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


def _execution_preflight(
    conn,
    rec: dict[str, Any],
    *,
    now_ts: int | None = None,
    bybit_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = int(now_ts or time.time())
    blocks: list[dict[str, Any]] = []

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
        "market_shock": market_shock,
        "fast_veto": fast_veto,
        "bybit_meta": bybit_meta,
        "bybit_validation": bybit_validation,
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
    if per_tf_budget <= 0 and bool(getattr(settings, "backfill_full_sweep_on_warmup", True)):
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


app = FastAPI(title="Bybit Recommender (Scenario B)", version="1.0.11", lifespan=lifespan)

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
    ts: int | None = None
    sentiment: float = Field(..., ge=-1.0, le=1.0, allow_inf_nan=False)
    velocity: float = Field(0.0, allow_inf_nan=False)
    volume: int = Field(1, ge=0)
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
    ts: int | None = None
    pnl: float = Field(..., allow_inf_nan=False, description="Gross realized PnL before fee; net PnL is computed as pnl - fee")
    fee: float = Field(0.0, ge=0.0, allow_inf_nan=False, description="Exchange fees for this trade; deducted from pnl to compute net")
    operator: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    stop_bot: bool = False


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

    publication_root_rec_id = str(rec.get("publication_root_rec_id") or rec_id).strip() or rec_id
    if publication_root_rec_id:
        # Reuse only a live bot from the same publication chain. Re-attaching a later
        # `active` recommendation to a historical *stopped* bot makes the API claim the
        # signal was executed while leaving the operator with no running position.
        chain_existing = db.get_bot_by_publication_root(conn, publication_root_rec_id, status="running")
        if chain_existing:
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
    exec_blocks = gate_candidate(conn, rec["venue"], rec["symbol"], limits)
    if exec_blocks:
        db.log_decision(conn, "EXECUTION_BLOCKED", rec_id, operator, {"blocks": exec_blocks})
        codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in exec_blocks)
        raise HTTPException(status_code=409, detail=f"execution blocked by current risk limits: {codes}")

    rec_for_execution = _snap_reco_payload_to_bybit_meta(rec, bybit_meta)
    preflight = _execution_preflight(conn, rec_for_execution, now_ts=int(time.time()), bybit_meta=bybit_meta)
    preflight_blocks = list(preflight.get("blocks") or [])
    if preflight_blocks:
        db.log_decision(
            conn,
            "EXECUTION_PRECHECK_BLOCKED",
            rec_id,
            operator,
            {
                "blocks": preflight_blocks,
                "market_shock": preflight.get("market_shock"),
                "fast_veto": preflight.get("fast_veto"),
                "bybit_validation": preflight.get("bybit_validation"),
            },
        )
        codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in preflight_blocks)
        raise HTTPException(status_code=409, detail=f"execution blocked by preflight checks: {codes}")

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
    if mode == "latest_operator":
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
        augmented_items = [_augment_reco_for_ui(item) for item in raw_items]
        items = _filter_operator_items_by_effective_status(augmented_items, statuses, top_n)

        status_counts = db.get_recommendation_status_counts(conn, venue=venue, snapshot_ts=snapshot_ts)
        snapshot_age_sec = None if snapshot_ts is None else max(0, int(time.time()) - int(snapshot_ts))
        snapshot_is_stale = bool(snapshot_age_sec is not None and snapshot_age_sec > max(180, int(settings.reco_interval_sec) * 3))
        no_trade = not any(str(item.get("status") or "").strip().lower() in {"recommended", "active"} for item in items)

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
            "llm_status_counts": llm_status_counts,
        }


@app.get("/api/v1/recommendations/{rec_id}")
def api_reco_details(rec_id: str) -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        r = db.get_recommendation_by_id(conn, str(rec_id))
        if not r:
            raise HTTPException(status_code=404, detail="rec_id not found")
        return _augment_reco_for_ui(r)


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
                meta=req.meta,
            ):
                trade_summary = db.get_bot_trade_summary(conn, bot_id)
                realized_pnl_gross = float(trade_summary["realized_pnl_gross"])
                realized_fee = float(trade_summary["realized_fee"])
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
            db.log_decision(conn, "TRADE_RECORDED", bot.get("origin_rec_id"), operator, {"bot_id": bot_id, "trade_id": trade_id, "insert_result": insert_result, "pnl": req.pnl, "fee": req.fee, "stop_bot": req.stop_bot}, commit=False)
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
            "bot_status": "stopped" if req.stop_bot else bot["status"],
        }


@app.get("/api/v1/trades")
def api_trades(bot_id: str | None = None, limit: int = 200) -> dict[str, Any]:
    limit = _bounded_limit(limit, default=200, max_value=1000)
    with closing(_get_conn()) as conn:
        items = db.list_trades(conn, bot_id=bot_id, limit=limit)
        return {"items": items, "count": len(items)}


@app.get("/api/v1/outcomes/stats")
def api_outcomes_stats() -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        return db.get_outcomes_stats(conn, require_llm_verdict=bool(getattr(settings, "llm_reviewer_enabled", False)))


@app.get("/api/v1/health/symbols")
def api_symbol_health() -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        items, meta = _load_symbol_health(conn)
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
    lock_ttl = max(120, settings.collect_interval_sec * 20)
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
    lock_ttl = max(120, settings.collect_interval_sec * 20)
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
    _last_outcomes = 0.0
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
                        result = run_recommender_once(conn, settings, heartbeat=heartbeat)
                        if not heartbeat():
                            raise RuntimeLockLostError("reco runtime lock lost")
                        if time.time() - _last_outcomes >= int(getattr(settings, "outcomes_interval_sec", 60) or 60):
                            compute_outcomes_once(
                                conn,
                                horizon_sec=settings.outcome_horizon_fallback_sec,
                                max_to_process=int(getattr(settings, "outcomes_max_to_process", 200) or 200),
                            )
                            _last_outcomes = time.time()
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
    while True:
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
            for name in ("collector", "backfill", "futures_meta", "sentiment", "reco", "llm_reviewer")
        }
        now_ts_int = db.now_ts()
        collector_cycle_started_ts = int(collector_last_cycle.get("started_ts") or 0)
        collector_cycle_age_sec = None if collector_cycle_started_ts <= 0 else max(0, now_ts_int - collector_cycle_started_ts)
        collector_thread_state = background_threads.get("collector") or {}
        backfill_thread_state = background_threads.get("backfill") or {}
        futures_meta_thread_state = background_threads.get("futures_meta") or {}
        collector_runtime_state = "unknown"
        if str(collector_thread_state.get("state") or "").lower() == "error":
            collector_runtime_state = "error"
        elif collector_cycle_age_sec is not None and collector_cycle_age_sec > max(int(settings.collect_interval_sec) * 6, int(settings.stale_data_max_sec)):
            collector_runtime_state = "stalled"
        elif collector_last_cycle:
            collector_runtime_state = "ok"
        elif str(collector_thread_state.get("state") or "").lower() == "running":
            collector_runtime_state = "starting"

        global_model = load_logreg_from_db(conn, GLOBAL_LOGREG_KEY)
        calib_fitted = bool(global_model and global_model.fitted)
        calib_n = int(global_model.n_samples) if global_model and global_model.fitted else 0
        calib_logreg = bool(global_model and global_model.fitted and len(global_model.coef) > 0)

        min_samples = int(settings.calib_min_samples)
        logreg_min_samples = 300

        _supported_sql, _supported_params = sql_in_clause("bot_type")
        require_llm_outcome_verdict = bool(getattr(settings, "llm_reviewer_enabled", False))
        cur = conn.execute(
            f"""SELECT o.bot_type, o.success, o.ts, r.status, r.reasons_json, r.is_outcome_label_root
                   FROM reco_outcomes o
                   JOIN recommendations r ON r.rec_id = o.rec_id
                   WHERE {_supported_sql.replace('bot_type', 'o.bot_type')}""",
            _supported_params,
        )
        outcome_stats_by_bot: dict[str, dict[str, Any]] = {}
        outcome_stats_7d_by_bot: dict[str, dict[str, int]] = {}
        outcome_count = 0
        for row in cur.fetchall():
            if row["is_outcome_label_root"] is not None and not bool(int(row["is_outcome_label_root"] or 0)):
                continue
            if require_llm_outcome_verdict and not db.is_outcome_eligible_under_llm_mode(row["status"], row["reasons_json"]):
                continue
            bot_type = str(row["bot_type"])
            success = int(row["success"] or 0)
            stat = outcome_stats_by_bot.setdefault(bot_type, {"total": 0, "wins": 0})
            stat["total"] += 1
            stat["wins"] += success
            outcome_count += 1
            if int(row["ts"] or 0) >= db.now_ts() - 7 * 86400:
                recent = outcome_stats_7d_by_bot.setdefault(bot_type, {"total": 0, "wins": 0, "losses": 0})
                recent["total"] += 1
                recent["wins"] += success

        for stat in outcome_stats_by_bot.values():
            total = int(stat["total"])
            wins = int(stat["wins"])
            losses = max(0, total - wins)
            minority_class_count = min(wins, losses)
            effective_samples = max(0, 2 * minority_class_count)
            win_rate = float(wins / total) if total else None
            if win_rate is None or win_rate <= 0.0 or win_rate >= 1.0:
                class_entropy_bits = 0.0
            else:
                class_entropy_bits = float(-(win_rate * math.log2(win_rate) + (1.0 - win_rate) * math.log2(1.0 - win_rate)))
            stat.update({
                "losses": losses,
                "minority_class_count": minority_class_count,
                "effective_samples": effective_samples,
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "class_entropy_bits": round(class_entropy_bits, 4),
            })

        for stat in outcome_stats_7d_by_bot.values():
            total = int(stat["total"])
            wins = int(stat["wins"])
            stat["losses"] = max(0, total - wins)

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
        for bt, key in BOT_CALIB_KEYS.items():
            m = load_logreg_from_db(conn, key)
            fitted = bool(m and m.fitted)
            logreg_active = bool(m and m.fitted and len(m.coef) > 0)
            stats = outcome_stats_by_bot.get(bt, {"total": 0, "wins": 0, "win_rate": None})
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
                confidence_mode = "bot_platt"
            bot_status[bt] = {
                "fitted": fitted,
                "logreg_active": logreg_active,
                "n_samples": int(m.n_samples) if m and m.fitted else 0,
                "rows_dropped_for_fit": max(0, int(stats["total"]) - int(m.n_samples)) if m and m.fitted and logreg_active else 0,
                "last_fit_ts": int(m.saved_ts) if m and m.fitted else 0,
                "confidence_mode": confidence_mode,
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
            }

        last_reco_ts = db.get_latest_reco_ts(conn)
        cur = conn.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR' AND ts >= ?", (db.now_ts() - 600,))
        collect_errors_10m = int(cur.fetchone()["c"])
        sent = compute_sentiment_agg(conn, scope="global", key="crypto")
        market_shock = _get_app_config_mapping(conn, MARKET_SHOCK_APP_KEY, default={"state": "normal", "title": "Нормальный режим", "severity": "normal", "entry_mode": "normal", "operator_note": "Новые входы разрешены в обычном режиме.", "reasons": [], "metrics": {}})

        inference_ready_bot_count = sum(1 for info in bot_status.values() if bool(info.get("fitted")))
        inference_supported_bot_count = len(bot_status)
        if inference_ready_bot_count == 0:
            confidence_mode_in_use = "raw_only"
        elif inference_ready_bot_count == inference_supported_bot_count:
            confidence_mode_in_use = "bot_specific_only"
        else:
            confidence_mode_in_use = "mixed_bot_and_raw"

        return {
            "calibrator_fitted": calib_fitted,
            "calibrator_logreg": calib_logreg,
            "calibrator_n": calib_n,
            "global_calibrator_diagnostic_only": True,
            "inference_calibration_mode": confidence_mode_in_use,
            "confidence_mode_in_use": confidence_mode_in_use,
            "outcome_label_version": OUTCOME_LABEL_VERSION,
            "inference_ready_bot_count": inference_ready_bot_count,
            "inference_supported_bot_count": inference_supported_bot_count,
            "calibrator_params": {
                "a": float(global_model.platt.a) if global_model and global_model.fitted else None,
                "b": float(global_model.platt.b) if global_model and global_model.fitted else None,
            },
            "bot_calibrators": bot_status,
            "outcome_count": outcome_count,
            "calib_min_samples": min_samples,
            "calib_logreg_min_samples": logreg_min_samples,
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
