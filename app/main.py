from __future__ import annotations

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
from .shock_guard import APP_CONFIG_KEY as MARKET_SHOCK_APP_KEY
from .bybit_client import BybitPublicClient
from .collector import collect_once, collect_backfill_once, collect_futures_once, RuntimeLockLostError
from .alerts import check_and_alert
from .sentiment import collect_sentiment_once
from .outcomes import compute_outcomes_once
from .recommender import run_recommender_once, run_llm_review_sweep_once, LLM_REVIEW_ASYNC_STATUS_APP_KEY
from .risk import get_risk_limits, compute_risk_status, gate_candidate, normalize_risk_limits
from .security import is_authorized
from . import db
from .bot_types import SUPPORTED_BOT_TYPES, is_supported_bot_type, sql_in_clause
import logging

logger = logging.getLogger(__name__)
settings = load_settings()
RUNTIME_OWNER = f"{socket.gethostname()}:{os.getpid()}"
PROCESS_STARTED_TS = int(time.time())
OUTCOME_LABEL_VERSION = "grid_label_v2"
INSTRUMENT_META_CACHE_TTL_SEC = 15 * 60
INSTRUMENT_META_NEGATIVE_CACHE_TTL_SEC = 30
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
        time.sleep(next_run - now)
    # If the previous iteration overran, do not add another full sleep on top.
    return max(next_run + interval, time.monotonic())


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
logger.info("db_path=%s", Path(settings.db_path).resolve())
logger.info("runtime_lock_db_path=%s", Path(settings.runtime_lock_db_path).resolve())


def _fetch_bybit_instrument_meta(venue: str, symbol: str) -> dict[str, Any]:
    category = "linear" if str(venue or "").lower() == "linear" else "spot"
    cache_key = (str(venue or "").lower(), str(symbol or "").upper())
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
        info = client.get_instrument_info(category, symbol)
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
        meta = {
            "category": category,
            "symbol": symbol,
            "tick_size": price_filter.get("tickSize"),
            "min_price": price_filter.get("minPrice"),
            "max_price": price_filter.get("maxPrice"),
            "qty_step": lot_filter.get("qtyStep"),
            "min_order_qty": lot_filter.get("minOrderQty"),
            "price_scale": info.get("priceScale"),
        }
        cache_ok = True

    with _instrument_meta_lock:
        _instrument_meta_cache[cache_key] = (time.time(), dict(meta), cache_ok)
    return meta


def _json_loads_or_default(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
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
    """
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a non-empty string")
    return normalized


def _normalize_tag_list(tags: list[str] | None) -> list[str]:
    """Убирает мусорные/дублирующиеся теги, сохраняя порядок живых значений."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = str(raw or "").strip()
        if not tag or tag in seen:
            continue
        out.append(tag)
        seen.add(tag)
    return out


def _existing_trade_matches_request(existing: dict[str, Any] | None, *, bot_id: str, symbol: str, ts: int | None, pnl: float, fee: float, meta: dict[str, Any]) -> bool:
    if not existing:
        return False
    if str(existing.get("bot_id") or "") != str(bot_id):
        return False
    if str(existing.get("symbol") or "") != str(symbol):
        return False
    if ts is not None and int(existing.get("ts") or 0) != int(ts):
        return False
    try:
        pnl_match = math.isclose(float(existing.get("pnl") or 0.0), float(pnl), rel_tol=1e-12, abs_tol=1e-12)
        fee_match = math.isclose(float(existing.get("fee") or 0.0), float(fee), rel_tol=1e-12, abs_tol=1e-12)
    except Exception:
        return False
    if not (pnl_match and fee_match):
        return False
    return (existing.get("meta") or {}) == (meta or {})


def _augment_reco_for_ui(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    venue = str(out.get("venue") or "")
    symbol = str(out.get("symbol") or "")
    try:
        out["bybit_meta"] = _fetch_bybit_instrument_meta(venue, symbol) if venue and symbol else {}
    except Exception:
        out["bybit_meta"] = {}
    return out


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
    while True:
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
            if treat_return_as_error:
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
        settings.symbols_spot,
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
        settings.symbols_spot,
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
    threading.Thread(target=partial(_run_supervised_background_target, "collector", _collector_thread), name="collector", daemon=True).start()
    threading.Thread(target=partial(_run_supervised_background_target, "backfill", _backfill_thread), name="backfill", daemon=True).start()
    threading.Thread(target=partial(_run_supervised_background_target, "futures_meta", _futures_meta_thread), name="futures_meta", daemon=True).start()
    threading.Thread(target=partial(_run_supervised_background_target, "sentiment", _sentiment_thread), name="sentiment", daemon=True).start()
    threading.Thread(target=partial(_run_supervised_background_target, "reco", _reco_thread), name="reco", daemon=True).start()
    threading.Thread(target=partial(_run_supervised_background_target, "llm_reviewer", _llm_reviewer_thread), name="llm_reviewer", daemon=True).start()
    yield


app = FastAPI(title="Bybit Recommender (Scenario B)", version="1.0.9", lifespan=lifespan)

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


def _require_admin_key(x_api_key: str | None) -> None:
    if not is_authorized(settings.admin_api_key, x_api_key):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def _is_supported_execution_direction(bot_type: str, venue: str, direction: str) -> bool:
    if bot_type == "spot_grid":
        return venue == "spot" and direction in ("neutral", "long")
    if bot_type == "futures_grid":
        return venue == "linear" and direction in ("neutral", "long", "short")
    return False


def _materialize_bot_from_rec(conn, rec_id: str, operator: str | None = None) -> tuple[dict[str, Any], bool]:
    db.begin_immediate(conn)
    rec = db.get_recommendation_by_id(conn, rec_id)
    if not rec:
        raise HTTPException(status_code=404, detail="rec_id not found")

    existing = db.get_bot_by_origin_rec(conn, rec_id)
    if existing:
        if rec.get("status") != "executed":
            db.update_recommendation_status(conn, rec_id, "executed", operator)
        return existing, True

    publication_root_rec_id = str(rec.get("publication_root_rec_id") or rec_id).strip() or rec_id
    if publication_root_rec_id:
        # Reuse only a live bot from the same publication chain. Re-attaching a later
        # `active` recommendation to a historical *stopped* bot makes the API claim the
        # signal was executed while leaving the operator with no running position.
        chain_existing = db.get_bot_by_publication_root(conn, publication_root_rec_id, status="running")
        if chain_existing:
            if rec.get("status") != "executed":
                db.update_recommendation_status(conn, rec_id, "executed", operator)
            return chain_existing, True

    ttl_sec = int(rec.get("ttl_sec") or 0)
    rec_ts = int(rec.get("ts") or 0)
    if ttl_sec > 0 and rec_ts > 0 and int(time.time()) > rec_ts + ttl_sec:
        db.update_recommendation_status(conn, rec_id, "expired", operator)
        raise HTTPException(status_code=409, detail="recommendation already expired")

    if rec["status"] in {"blocked", "no_trade", "suppressed", "pending", "expired", "ignored"}:
        raise HTTPException(status_code=409, detail=f"recommendation status={rec['status']} cannot be executed")
    if not _is_supported_execution_direction(str(rec.get("bot_type") or ""), str(rec.get("venue") or ""), str(rec.get("direction") or "")):
        raise HTTPException(status_code=409, detail="recommendation direction is not executable for this bot_type/venue")

    # Re-check current risk limits at execution time.
    # Recommendation-time gates are only a snapshot; by the moment an operator clicks
    # execute, active bot count / symbol cap / cooldown / day DD may already have changed.
    limits = get_risk_limits(conn, settings.risk_limits)
    exec_blocks = gate_candidate(conn, rec["venue"], rec["symbol"], limits)
    if exec_blocks:
        db.log_decision(conn, "EXECUTION_BLOCKED", rec_id, operator, {"blocks": exec_blocks})
        codes = ", ".join(str(b.get("code") or "UNKNOWN") for b in exec_blocks)
        raise HTTPException(status_code=409, detail=f"execution blocked by current risk limits: {codes}")

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
        "params": rec["params"],
        "state": {
            "created_from_rec_id": rec_id,
            "operator": operator,
            "trade_count": 0,
            "realized_pnl": 0.0,
            "realized_pnl_gross": 0.0,
            "realized_pnl_net": 0.0,
            "realized_fee": 0.0,
            "last_trade_ts": None,
        },
        "status": "running",
        "origin_rec_id": rec_id,
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
        snapshot_ts = _resolve_recommendation_snapshot_ts(
            conn,
            venue,
            snapshot,
            min_conf=effective_min_conf,
            strict_min_conf=strict_min_conf,
            requested_statuses=statuses,
        )
        items = db.get_recommendations(
            conn,
            venue=venue,
            top_n=top_n,
            min_conf=effective_min_conf,
            statuses=statuses,
            snapshot_ts=snapshot_ts,
            strict_min_conf=strict_min_conf,
        )

        no_trade = True
        status_counts = db.get_recommendation_status_counts(conn, venue=venue, snapshot_ts=snapshot_ts)
        snapshot_age_sec = None if snapshot_ts is None else max(0, int(time.time()) - int(snapshot_ts))
        snapshot_is_stale = bool(snapshot_age_sec is not None and snapshot_age_sec > max(180, int(settings.reco_interval_sec) * 3))
        if snapshot_ts is not None:
            no_trade = db.count_visible_recommendations(
                conn,
                venue=venue,
                min_conf=effective_min_conf,
                snapshot_ts=snapshot_ts,
                strict_min_conf=strict_min_conf,
            ) == 0

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
def api_update_risk_limits(req: UpdateRiskLimitsRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key)
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
def api_reco_action(rec_id: str, req: RecoActionRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key)
    operator = _normalized_optional_text(req.operator, field_name="operator")
    allowed = {"executed", "ignored"}
    if req.action not in allowed:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(allowed)}")
    with closing(_get_conn()) as conn:
        db.begin_immediate(conn)
        if req.action == "executed":
            bot, existed = _materialize_bot_from_rec(conn, rec_id, operator)
            return {"ok": True, "rec_id": rec_id, "new_status": "executed", "bot_id": bot["bot_id"], "bot": bot, "idempotent": existed}
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
def api_stop_bot(bot_id: str, req: BotStopRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key)
    with closing(_get_conn()) as conn:
        db.begin_immediate(conn)
        bot = db.get_bot_instance(conn, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="bot_id not found")
        if str(bot.get("status") or "") == "stopped":
            return {"ok": True, "bot_id": bot_id, "status": "stopped", "idempotent": True}
        try:
            ok = db.stop_bot(conn, bot_id, commit=False)
            if not ok:
                _rollback_quietly(conn)
                return {"ok": False, "bot_id": bot_id, "status": bot["status"]}
            operator = _normalized_optional_text(req.operator, field_name="operator")
            reason = _normalized_optional_text(req.reason, field_name="reason")
            state_updated = db.update_bot_state(conn, bot_id, {"stop_reason": reason, "stopped_by": operator, "stopped_ts": int(time.time())}, commit=False)
            if not state_updated:
                raise RuntimeError("bot state update failed after stop")
            db.log_decision(conn, "BOT_STOPPED", bot.get("origin_rec_id"), operator, {"bot_id": bot_id, "reason": reason}, commit=False)
            conn.commit()
        except Exception:
            _rollback_quietly(conn)
            raise
        return {"ok": True, "bot_id": bot_id, "status": "stopped", "idempotent": False}


@app.post("/api/v1/bots/{bot_id}/trades")
def api_record_trade(bot_id: str, req: BotTradeRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key)
    _ensure_json_payload_has_only_finite_numbers(req.meta, field_name="meta")
    with closing(_get_conn()) as conn:
        db.begin_immediate(conn)
        bot = db.get_bot_instance(conn, bot_id)
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
                stop_ok = db.stop_bot(conn, bot_id, commit=False)
                if not stop_ok:
                    raise HTTPException(status_code=409, detail="bot status changed during trade finalization")
                stop_state_updated = db.update_bot_state(conn, bot_id, {"stop_reason": "stop_bot_on_trade", "stopped_by": operator, "stopped_ts": int(time.time())}, commit=False)
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
        return db.get_outcomes_stats(conn)


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
def api_sentiment_put(req: SentimentPointRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key)
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
        while True:
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
                lock_lost = False
                with closing(_get_conn()) as conn:
                    heartbeat = _make_runtime_lock_heartbeat(lock_key)
                    for venue in settings.venues:
                        symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
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
                            lock_lost = True
                            cycle_stats["lock_lost"] = True
                            _rollback_quietly(conn)
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "field": "runtime_lock", "err": str(e)})
                            break
                        except Exception as e:
                            _rollback_quietly(conn)
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "err": str(e)})
                        if not heartbeat():
                            lock_lost = True
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
        while True:
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
                lock_lost = False
                with closing(_get_conn()) as conn:
                    heartbeat = _make_runtime_lock_heartbeat(lock_key)
                    for venue in settings.venues:
                        symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
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
                            lock_lost = True
                            cycle_stats["lock_lost"] = True
                            _rollback_quietly(conn)
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "field": "runtime_lock", "err": str(e)})
                            break
                        except Exception as e:
                            _rollback_quietly(conn)
                            _log_decision_fresh("COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "field": "backfill", "err": str(e)})
                        if not heartbeat():
                            lock_lost = True
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
        while True:
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
    while True:
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
    while True:
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
        backfill_last_cycle = _get_app_config_mapping(conn, "backfill_last_cycle", default={})
        futures_meta_last_cycle = _get_app_config_mapping(conn, "futures_meta_last_cycle", default={})
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
        cur = conn.execute(
            f"""SELECT o.bot_type, COUNT(*) AS total, COALESCE(SUM(o.success), 0) AS wins
                   FROM reco_outcomes o
                   JOIN recommendations r ON r.rec_id = o.rec_id
                   WHERE {_supported_sql.replace('bot_type', 'o.bot_type')} AND COALESCE(r.is_outcome_label_root, 1) = 1
                   GROUP BY o.bot_type""",
            _supported_params,
        )
        outcome_stats_by_bot: dict[str, dict[str, Any]] = {}
        outcome_count = 0
        for row in cur.fetchall():
            total = int(row["total"] or 0)
            wins = int(row["wins"] or 0)
            losses = max(0, total - wins)
            minority_class_count = min(wins, losses)
            effective_samples = max(0, 2 * minority_class_count)
            outcome_count += total
            win_rate = float(wins / total) if total else None
            if win_rate is None or win_rate <= 0.0 or win_rate >= 1.0:
                class_entropy_bits = 0.0
            else:
                class_entropy_bits = float(-(win_rate * math.log2(win_rate) + (1.0 - win_rate) * math.log2(1.0 - win_rate)))
            outcome_stats_by_bot[str(row["bot_type"])] = {
                "total": total,
                "wins": wins,
                "losses": losses,
                "minority_class_count": minority_class_count,
                "effective_samples": effective_samples,
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "class_entropy_bits": round(class_entropy_bits, 4),
            }

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

        cur = conn.execute(
            f"""SELECT o.bot_type, COUNT(*) AS total, COALESCE(SUM(o.success), 0) AS wins
                   FROM reco_outcomes o
                   JOIN recommendations r ON r.rec_id = o.rec_id
                   WHERE o.ts >= ? AND {_supported_sql.replace('bot_type', 'o.bot_type')} AND COALESCE(r.is_outcome_label_root, 1) = 1
                   GROUP BY o.bot_type""",
            [db.now_ts() - 7 * 86400, *_supported_params],
        )
        outcome_stats_7d_by_bot: dict[str, dict[str, int]] = {}
        for row in cur.fetchall():
            total = int(row["total"] or 0)
            wins = int(row["wins"] or 0)
            outcome_stats_7d_by_bot[str(row["bot_type"])] = {
                "total": total,
                "wins": wins,
                "losses": max(0, total - wins),
            }

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
