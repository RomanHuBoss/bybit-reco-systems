from __future__ import annotations

import json
import math
import os
import secrets
import threading
import socket
from functools import lru_cache
import time
from contextlib import closing, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .settings import load_settings
from .shock_guard import APP_CONFIG_KEY as MARKET_SHOCK_APP_KEY
from .bybit_client import BybitPublicClient
from .collector import collect_once, collect_futures_once
from .alerts import check_and_alert
from .sentiment import collect_sentiment_once
from .outcomes import compute_outcomes_once
from .recommender import run_recommender_once
from .risk import get_risk_limits, compute_risk_status, gate_candidate
from .security import is_authorized
from . import db
from .bot_types import SUPPORTED_BOT_TYPES, is_supported_bot_type, sql_in_clause
import logging

logger = logging.getLogger(__name__)
settings = load_settings()
RUNTIME_OWNER = f"{socket.gethostname()}:{os.getpid()}"
OUTCOME_LABEL_VERSION = "grid_label_v2"


def _get_conn():
    return db.connect(settings.db_path)


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
            db.upsert_risk_limits(conn, version="bootstrap", limits=settings.risk_limits, is_active=True)

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



@lru_cache(maxsize=512)
def _fetch_bybit_instrument_meta(venue: str, symbol: str) -> dict[str, Any]:
    category = "linear" if str(venue or "").lower() == "linear" else "spot"
    client = BybitPublicClient(settings.bybit_base_url)
    try:
        info = client.get_instrument_info(category, symbol)
    except Exception as exc:
        logger.warning("instrument meta fetch failed for %s/%s: %s", venue, symbol, exc)
        return {}
    finally:
        try:
            client.close()
        except Exception:
            pass

    if not info:
        return {}

    price_filter = info.get("priceFilter") or {}
    lot_filter = info.get("lotSizeFilter") or {}
    return {
        "category": category,
        "symbol": symbol,
        "tick_size": price_filter.get("tickSize"),
        "min_price": price_filter.get("minPrice"),
        "max_price": price_filter.get("maxPrice"),
        "qty_step": lot_filter.get("qtyStep"),
        "min_order_qty": lot_filter.get("minOrderQty"),
        "price_scale": info.get("priceScale"),
    }


def _augment_reco_for_ui(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    venue = str(out.get("venue") or "")
    symbol = str(out.get("symbol") or "")
    try:
        out["bybit_meta"] = _fetch_bybit_instrument_meta(venue, symbol) if venue and symbol else {}
    except Exception:
        out["bybit_meta"] = {}
    return out

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_collector_thread, daemon=True).start()
    threading.Thread(target=_sentiment_thread, daemon=True).start()
    threading.Thread(target=_reco_thread, daemon=True).start()
    yield


app = FastAPI(title="Bybit Recommender (Scenario B)", version="1.0.2", lifespan=lifespan)

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
    sentiment: float = Field(..., ge=-1.0, le=1.0)
    velocity: float = 0.0
    volume: int = 1
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
    pnl: float = Field(..., description="Gross realized PnL before fee; net PnL is computed as pnl - fee")
    fee: float = Field(0.0, ge=0.0, description="Exchange fees for this trade; deducted from pnl to compute net")
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
    rec = db.get_recommendation_by_id(conn, rec_id)
    if not rec:
        raise HTTPException(status_code=404, detail="rec_id not found")

    existing = db.get_bot_by_origin_rec(conn, rec_id)
    if existing:
        if rec.get("status") != "executed":
            db.update_recommendation_status(conn, rec_id, "executed", operator)
        return existing, True

    ttl_sec = int(rec.get("ttl_sec") or 0)
    rec_ts = int(rec.get("ts") or 0)
    if ttl_sec > 0 and rec_ts > 0 and int(time.time()) > rec_ts + ttl_sec:
        db.update_recommendation_status(conn, rec_id, "expired", operator)
        raise HTTPException(status_code=409, detail="recommendation already expired")

    if rec["status"] in {"blocked", "no_trade", "suppressed", "expired", "ignored"}:
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
    insert_result = db.insert_bot_instance(conn, bot)
    if insert_result == "duplicate_origin":
        existing = db.get_bot_by_origin_rec(conn, rec_id)
        if existing:
            if rec.get("status") != "executed":
                db.update_recommendation_status(conn, rec_id, "executed", operator)
            return existing, True
        raise HTTPException(status_code=409, detail="bot creation conflicted with an existing origin_rec_id")

    db.update_recommendation_status(conn, rec_id, "executed", operator)
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
    )
    created = db.get_bot_instance(conn, bot["bot_id"])
    return created or bot, False


@app.get("/api/v1/recommendations")
def api_recommendations(
    venue: str | None = None,
    top_n: int = 20,
    min_conf: float | None = None,
    show_recommended: bool = True,
    show_blocked: bool = False,
    show_no_trade: bool = False,
    show_suppressed: bool = False,
    snapshot: str = "latest",
) -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        statuses: list[str] = []
        if show_recommended:
            statuses.append("recommended")
        if show_blocked:
            statuses.append("blocked")
        if show_no_trade:
            statuses.append("no_trade")
        if show_suppressed:
            statuses.append("suppressed")

        snapshot_ts = None
        if snapshot == "latest":
            snapshot_ts = db.get_latest_reco_ts(conn, venue=venue)

        effective_min_conf = settings.min_conf_to_recommend if min_conf is None else float(min_conf)
        strict_min_conf = min_conf is not None
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
        regime = json.loads(row["regime_json"]) if row else {
            "vol_state": "unknown",
            "trend_state": "unknown",
            "risk_state": "unknown",
            "confidence": 0.0,
        }

        return {
            "ts": int(time.time()),
            "snapshot_ts": snapshot_ts,
            "snapshot_age_sec": snapshot_age_sec,
            "snapshot_is_stale": snapshot_is_stale,
            "regime": regime,
            "items": items,
            "no_trade": no_trade,
            "min_conf": float(effective_min_conf),
            "status_counts": status_counts,
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
    with closing(_get_conn()) as conn:
        db.upsert_risk_limits(conn, version=req.version, limits=req.limits, is_active=True)
        db.log_decision(conn, "UPDATE_LIMITS", None, None, {"version": req.version, "limits": req.limits})
        return {"ok": True, "version": req.version}


@app.post("/api/v1/recommendations/{rec_id}/action")
def api_reco_action(rec_id: str, req: RecoActionRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key)
    allowed = {"executed", "ignored"}
    if req.action not in allowed:
        return {"ok": False, "error": f"action must be one of {allowed}"}
    with closing(_get_conn()) as conn:
        if req.action == "executed":
            bot, existed = _materialize_bot_from_rec(conn, rec_id, req.operator)
            return {"ok": True, "rec_id": rec_id, "new_status": "executed", "bot_id": bot["bot_id"], "bot": bot, "idempotent": existed}
        ok = db.update_recommendation_status(conn, rec_id, req.action, req.operator)
        return {"ok": ok, "rec_id": rec_id, "new_status": req.action}


@app.get("/api/v1/bots")
def api_bots(status: str | None = None, limit: int = 200) -> dict[str, Any]:
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
        bot = db.get_bot_instance(conn, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="bot_id not found")
        ok = db.stop_bot(conn, bot_id)
        if not ok:
            return {"ok": False, "bot_id": bot_id, "status": bot["status"]}
        db.update_bot_state(conn, bot_id, {"stop_reason": req.reason, "stopped_by": req.operator, "stopped_ts": int(time.time())})
        db.log_decision(conn, "BOT_STOPPED", bot.get("origin_rec_id"), req.operator, {"bot_id": bot_id, "reason": req.reason})
        return {"ok": True, "bot_id": bot_id, "status": "stopped"}


@app.post("/api/v1/bots/{bot_id}/trades")
def api_record_trade(bot_id: str, req: BotTradeRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key)
    with closing(_get_conn()) as conn:
        bot = db.get_bot_instance(conn, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="bot_id not found")
        if str(bot.get("status") or "") != "running":
            raise HTTPException(status_code=409, detail=f"cannot record trade for bot status={bot.get('status')}")

        trade_id = req.trade_id or f"T-{int(time.time())}-{secrets.token_hex(4)}"
        ts = req.ts or int(time.time())
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
            insert_result = db.insert_trade(conn, trade)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        trade_summary = db.get_bot_trade_summary(conn, bot_id)
        realized_pnl_gross = float(trade_summary["realized_pnl_gross"])
        realized_fee = float(trade_summary["realized_fee"])
        realized_pnl_net = float(trade_summary["realized_pnl_net"])
        db.update_bot_state(
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
                "last_operator": req.operator,
            },
        )
        if req.stop_bot:
            db.stop_bot(conn, bot_id)
            db.update_bot_state(conn, bot_id, {"stop_reason": "stop_bot_on_trade", "stopped_by": req.operator, "stopped_ts": int(time.time())})
        db.log_decision(conn, "TRADE_RECORDED", bot.get("origin_rec_id"), req.operator, {"bot_id": bot_id, "trade_id": trade_id, "insert_result": insert_result, "pnl": req.pnl, "fee": req.fee, "stop_bot": req.stop_bot})
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
        items = db.get_symbol_health(conn, settings.symbols_spot, settings.symbols_linear, stale_sec=settings.stale_data_max_sec)
        n_ok = sum(1 for i in items if i["status"] == "ok")
        n_stale = sum(1 for i in items if i["status"] == "stale")
        n_missing = sum(1 for i in items if i["status"] == "missing")
        n_errors = sum(i["error_count_10m"] for i in items)
        return {
            "ts": int(time.time()),
            "summary": {"ok": n_ok, "stale": n_stale, "missing": n_missing, "errors_10m": n_errors},
            "llm_reviewer": {
                "enabled": bool(getattr(settings, "llm_reviewer_enabled", False)),
                "mode": getattr(settings, "llm_reviewer_mode", "advisory"),
                "provider": getattr(settings, "llm_reviewer_provider", "ollama"),
                "model": getattr(settings, "llm_reviewer_model", None),
                "tf_secs": list(getattr(settings, "llm_reviewer_tf_secs", []) or []),
                "candles_per_tf": int(getattr(settings, "llm_reviewer_candles_per_tf", 32) or 32),
                "max_candidates": int(getattr(settings, "llm_reviewer_max_candidates", 2) or 2),
                "min_confidence": float(getattr(settings, "llm_reviewer_min_confidence", 0.65) or 0.65),
                "cadence_sec": int(getattr(settings, "llm_reviewer_cadence_sec", 300) or 300),
            },
            "symbols": items,
        }


@app.get("/api/v1/decisions")
def api_decisions(limit: int = 200) -> list[dict[str, Any]]:
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
                "details": json.loads(r["details_json"]),
            })
        return out


@app.post("/api/v1/sentiment")
def api_sentiment_put(req: SentimentPointRequest, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    _require_admin_key(x_api_key)
    with closing(_get_conn()) as conn:
        ts = req.ts or int(time.time())
        db.insert_sentiment_point(conn, req.scope, req.key, ts, req.sentiment, req.velocity, req.volume, req.sources, req.tags)
        db.log_decision(conn, "SENTIMENT_PUT", None, None, {"scope": req.scope, "key": req.key, "ts": ts, "sentiment": req.sentiment})
        return {"ok": True, "ts": ts}


@app.get("/api/v1/sentiment")
def api_sentiment_get(scope: str = "global", key: str = "crypto", limit: int = 120) -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        series = db.get_sentiment_series(conn, scope, key, limit=limit)
        return {"scope": scope, "key": key, "items": series}


def _collector_thread():
    client = BybitPublicClient(settings.bybit_base_url)
    _last_futures_collect = 0.0
    lock_key = "runtime:collector"
    lock_ttl = max(60, settings.collect_interval_sec * 4)
    next_run = _interval_loop_start(settings.collect_interval_sec)
    try:
        while True:
            with closing(_get_conn()) as conn:
                has_lock = db.acquire_runtime_lock(conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
            if has_lock:
                with closing(_get_conn()) as conn:
                    for venue in settings.venues:
                        symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
                        try:
                            collect_once(conn, client, venue, symbols)
                        except Exception as e:
                            db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "err": str(e)})
                if time.time() - _last_futures_collect >= settings.futures_collect_interval_sec:
                    with closing(_get_conn()) as conn:
                        try:
                            collect_futures_once(conn, client, settings.symbols_linear)
                            _last_futures_collect = time.time()
                        except Exception as e:
                            db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": "linear", "symbol": "UNKNOWN", "field": "futures_meta", "err": str(e)})
            next_run = _interval_loop_wait(next_run, settings.collect_interval_sec)
    finally:
        client.close()


def _sentiment_thread():
    lock_key = "runtime:sentiment"
    lock_ttl = max(60, settings.sentiment_interval_sec * 4)
    next_run = _interval_loop_start(settings.sentiment_interval_sec)
    while True:
        with closing(_get_conn()) as conn:
            has_lock = db.acquire_runtime_lock(conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
        if has_lock:
            with closing(_get_conn()) as conn:
                try:
                    pts = collect_sentiment_once()
                    for p in pts:
                        db.insert_sentiment_point(conn, p["scope"], p["key"], p["ts"], p["sentiment"], p["velocity"], p["volume"], p["sources"], p["tags"])
                    db.log_decision(conn, "SENTIMENT_COLLECT", None, None, {"count": len(pts)})
                except Exception as e:
                    db.log_decision(conn, "SENTIMENT_ERROR", None, None, {"err": str(e)})
        next_run = _interval_loop_wait(next_run, settings.sentiment_interval_sec)


def _reco_thread():
    _last_prune = 0.0
    _last_outcomes = 0.0
    PRUNE_INTERVAL = 3600
    lock_key = "runtime:reco"
    lock_ttl = max(60, settings.reco_interval_sec * 4)
    next_run = _interval_loop_start(settings.reco_interval_sec)
    while True:
        result = {}
        with closing(_get_conn()) as conn:
            has_lock = db.acquire_runtime_lock(conn, lock_key, RUNTIME_OWNER, ttl_sec=lock_ttl)
        if has_lock:
            with closing(_get_conn()) as conn:
                try:
                    result = run_recommender_once(conn, settings)
                    if time.time() - _last_outcomes >= int(getattr(settings, "outcomes_interval_sec", 60) or 60):
                        compute_outcomes_once(
                            conn,
                            horizon_sec=settings.outcome_horizon_fallback_sec,
                            max_to_process=int(getattr(settings, "outcomes_max_to_process", 200) or 200),
                        )
                        _last_outcomes = time.time()
                except Exception as e:
                    db.log_decision(conn, "RECO_ERROR", None, None, {"err": str(e)})

            with closing(_get_conn()) as conn:
                try:
                    db.expire_stale_recommendations(conn)
                except Exception:
                    logger.debug("expire_stale_recommendations error", exc_info=True)

            if time.time() - _last_prune >= PRUNE_INTERVAL:
                with closing(_get_conn()) as conn:
                    try:
                        deleted = db.prune_old_data(conn, retain_days=7)
                        db.log_decision(conn, "DB_PRUNE", None, None, deleted)
                        _last_prune = time.time()
                    except Exception:
                        logger.debug("prune_old_data error", exc_info=True)

            if settings.telegram_token:
                try:
                    with closing(_get_conn()) as conn:
                        health = db.get_symbol_health(conn, settings.symbols_spot, settings.symbols_linear, stale_sec=settings.stale_data_max_sec)
                        err_cur = conn.execute(
                            """SELECT COUNT(*) as c FROM decision_log
                               WHERE action='COLLECT_ERROR' AND ts >= ?""",
                            (int(time.time()) - 600,),
                        )
                        err_count = int(err_cur.fetchone()["c"])
                    check_and_alert(token=settings.telegram_token, chat_id=settings.telegram_chat_id, symbol_health=health, collect_errors_10m=err_count, reco_count=int(result.get("count_recommended", 0)))
                except Exception:
                    logger.debug("telegram alert error", exc_info=True)

        next_run = _interval_loop_wait(next_run, settings.reco_interval_sec)


@app.get("/api/v1/status")
def api_status() -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        from .calibration import load_logreg_from_db, GLOBAL_LOGREG_KEY, BOT_CALIB_KEYS, label_balance_stats
        from .sentiment_features import compute_sentiment_agg

        global_model = load_logreg_from_db(conn, GLOBAL_LOGREG_KEY)
        calib_fitted = bool(global_model and global_model.fitted)
        calib_n = int(global_model.n_samples) if global_model and global_model.fitted else 0
        calib_logreg = bool(global_model and global_model.fitted and len(global_model.coef) > 0)

        min_samples = int(settings.calib_min_samples)
        logreg_min_samples = 300

        _supported_sql, _supported_params = sql_in_clause("bot_type")
        cur = conn.execute(
            f"""SELECT bot_type, COUNT(*) AS total, COALESCE(SUM(success), 0) AS wins
                   FROM reco_outcomes
                   WHERE {_supported_sql}
                   GROUP BY bot_type""",
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
            f"""SELECT bot_type, COUNT(*) AS total, COALESCE(SUM(success), 0) AS wins
                   FROM reco_outcomes
                   WHERE ts >= ? AND {_supported_sql}
                   GROUP BY bot_type""",
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
        market_shock = db.get_app_config_json(conn, MARKET_SHOCK_APP_KEY, default={"state": "normal", "title": "Нормальный режим", "severity": "normal", "entry_mode": "normal", "operator_note": "Новые входы разрешены в обычном режиме.", "reasons": [], "metrics": {}})

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
        }


def main():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
