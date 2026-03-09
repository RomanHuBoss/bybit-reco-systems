from __future__ import annotations

import json
import secrets
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .settings import load_settings
from .bybit_client import BybitPublicClient
from .collector import collect_once, collect_futures_once
from .alerts import check_and_alert
from .sentiment import collect_sentiment_once
from .outcomes import compute_outcomes_once
from .recommender import run_recommender_once
from .risk import get_risk_limits, compute_risk_status, gate_candidate
from .security import is_authorized
from . import db

settings = load_settings()


def _get_conn():
    return db.connect(settings.db_path)


def _bootstrap_db() -> None:
    with closing(_get_conn()) as conn:
        db.init_db(conn)
        active = db.get_active_risk_limits(conn)
        if not active:
            db.upsert_risk_limits(conn, version="bootstrap", limits=settings.risk_limits, is_active=True)


_bootstrap_db()

app = FastAPI(title="Bybit Recommender (Scenario B)", version="1.0.0")

static_dir = Path(__file__).resolve().parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


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
    if rec.get("bot_type") == "futures_combo":
        raise HTTPException(status_code=409, detail="futures_combo is intentionally non-executable until a real two-leg PnL/execution model exists")

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
    db.insert_bot_instance(conn, bot)
    db.update_recommendation_status(conn, rec_id, "executed", operator)
    db.log_decision(conn, "BOT_STARTED", rec_id, operator, {"bot_id": bot["bot_id"], "symbol": bot["symbol"], "bot_type": bot["bot_type"]})
    created = db.get_bot_instance(conn, bot["bot_id"])
    return created or bot, False


@app.get("/api/v1/recommendations")
def api_recommendations(
    venue: str | None = None,
    top_n: int = 20,
    min_conf: float = 0.0,
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

        items = db.get_recommendations(conn, venue=venue, top_n=top_n, min_conf=min_conf, statuses=statuses, snapshot_ts=snapshot_ts)

        no_trade = True
        if snapshot_ts is not None:
            cur = conn.execute(
                """SELECT COUNT(*) AS c FROM recommendations
                   WHERE ts=? AND (? IS NULL OR venue=?) AND status='recommended'""",
                (snapshot_ts, venue, venue),
            )
            no_trade = int(cur.fetchone()["c"]) == 0

        cur = conn.execute("SELECT regime_json FROM market_regime ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
        regime = json.loads(row["regime_json"]) if row else {
            "vol_state": "unknown",
            "trend_state": "unknown",
            "risk_state": "unknown",
            "confidence": 0.0,
        }

        return {"ts": int(time.time()), "regime": regime, "items": items, "no_trade": no_trade}


@app.get("/api/v1/recommendations/{rec_id}")
def api_reco_details(rec_id: str) -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        r = db.get_recommendation_by_id(conn, str(rec_id))
        if not r:
            raise HTTPException(status_code=404, detail="rec_id not found")
        return r


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
def api_sentiment_get(scope: str, key: str, limit: int = 120) -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        series = db.get_sentiment_series(conn, scope, key, limit=limit)
        return {"scope": scope, "key": key, "items": series}


def _collector_thread():
    client = BybitPublicClient(settings.bybit_base_url)
    _last_futures_collect = 0.0
    FUTURES_COLLECT_INTERVAL = 900
    try:
        while True:
            with closing(_get_conn()) as conn:
                for venue in settings.venues:
                    symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
                    try:
                        collect_once(conn, client, venue, symbols)
                    except Exception as e:
                        db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "symbol": "UNKNOWN", "err": str(e)})
            if time.time() - _last_futures_collect >= FUTURES_COLLECT_INTERVAL:
                with closing(_get_conn()) as conn:
                    try:
                        collect_futures_once(conn, client, settings.symbols_linear)
                        _last_futures_collect = time.time()
                    except Exception as e:
                        db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": "linear", "symbol": "UNKNOWN", "field": "futures_meta", "err": str(e)})
            time.sleep(settings.collect_interval_sec)
    finally:
        client.close()


def _sentiment_thread():
    while True:
        with closing(_get_conn()) as conn:
            try:
                pts = collect_sentiment_once()
                for p in pts:
                    db.insert_sentiment_point(conn, p["scope"], p["key"], p["ts"], p["sentiment"], p["velocity"], p["volume"], p["sources"], p["tags"])
                db.log_decision(conn, "SENTIMENT_COLLECT", None, None, {"count": len(pts)})
            except Exception as e:
                db.log_decision(conn, "SENTIMENT_ERROR", None, None, {"err": str(e)})
        time.sleep(settings.sentiment_interval_sec)


def _reco_thread():
    _last_prune = 0.0
    PRUNE_INTERVAL = 3600
    while True:
        result = {}
        with closing(_get_conn()) as conn:
            try:
                result = run_recommender_once(conn, settings)
                compute_outcomes_once(conn, horizon_sec=settings.outcome_horizon_sec)
            except Exception as e:
                db.log_decision(conn, "RECO_ERROR", None, None, {"err": str(e)})

        with closing(_get_conn()) as conn:
            try:
                db.expire_stale_recommendations(conn)
            except Exception:
                pass

        if time.time() - _last_prune >= PRUNE_INTERVAL:
            with closing(_get_conn()) as conn:
                try:
                    deleted = db.prune_old_data(conn, retain_days=7)
                    db.log_decision(conn, "DB_PRUNE", None, None, deleted)
                    _last_prune = time.time()
                except Exception:
                    pass

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
                check_and_alert(token=settings.telegram_token, chat_id=settings.telegram_chat_id, symbol_health=health, collect_errors_10m=err_count, reco_count=int(result.get("count", 0)))
            except Exception:
                pass

        time.sleep(settings.reco_interval_sec)


@app.on_event("startup")
async def startup_event():
    threading.Thread(target=_collector_thread, daemon=True).start()
    threading.Thread(target=_sentiment_thread, daemon=True).start()
    threading.Thread(target=_reco_thread, daemon=True).start()


@app.get("/api/v1/status")
def api_status() -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        from .calibration import load_logreg_from_db, GLOBAL_LOGREG_KEY, BOT_CALIB_KEYS
        from .sentiment_features import compute_sentiment_agg

        global_model = load_logreg_from_db(conn, GLOBAL_LOGREG_KEY)
        calib_fitted = bool(global_model and global_model.fitted)
        calib_n = int(global_model.n_samples) if global_model and global_model.fitted else 0
        calib_logreg = bool(global_model and global_model.fitted and len(global_model.coef) > 0)

        min_samples = int(settings.calib_min_samples)
        logreg_min_samples = 300

        cur = conn.execute(
            """SELECT bot_type, COUNT(*) AS total, COALESCE(SUM(success), 0) AS wins
                   FROM reco_outcomes
                   GROUP BY bot_type"""
        )
        outcome_stats_by_bot: dict[str, dict[str, Any]] = {}
        outcome_count = 0
        for row in cur.fetchall():
            total = int(row["total"] or 0)
            wins = int(row["wins"] or 0)
            outcome_count += total
            win_rate = float(wins / total) if total else None
            outcome_stats_by_bot[str(row["bot_type"])] = {
                "total": total,
                "wins": wins,
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
            }

        def _bot_gate(total: int, win_rate: float | None, fitted: bool) -> tuple[bool, str | None]:
            if fitted:
                return True, None
            if total < min_samples:
                return False, "not_enough_samples"
            if win_rate is None:
                return False, "not_enough_samples"
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
                float(stats["win_rate"]) if stats["win_rate"] is not None else None,
                fitted,
            )
            bot_status[bt] = {
                "fitted": fitted,
                "logreg_active": logreg_active,
                "n_samples": int(m.n_samples) if m and m.fitted else 0,
                "outcomes_total": int(stats["total"]),
                "wins": int(stats["wins"]),
                "win_rate": stats["win_rate"],
                "eligible_for_fit": bool(eligible),
                "unfitted_reason": unfitted_reason,
                "min_samples": min_samples,
                "logreg_min_samples": logreg_min_samples,
            }

        last_reco_ts = db.get_latest_reco_ts(conn)
        cur = conn.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR' AND ts >= ?", (db.now_ts() - 600,))
        collect_errors_10m = int(cur.fetchone()["c"])
        sent = compute_sentiment_agg(conn, scope="global", key="crypto")

        return {
            "calibrator_fitted": calib_fitted,
            "calibrator_logreg": calib_logreg,
            "calibrator_n": calib_n,
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
        }


def main():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
