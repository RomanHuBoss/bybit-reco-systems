from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .settings import load_settings
from .bybit_client import BybitPublicClient
from .collector import collect_once
from .recommender import run_recommender_once
from . import db
from .risk import get_risk_limits, compute_risk_status

settings = load_settings()
conn = db.connect(settings.db_path)
db.init_db(conn)

# Ensure an active risk limits row exists
active = db.get_active_risk_limits(conn)
if not active:
    db.upsert_risk_limits(conn, version="bootstrap", limits=settings.risk_limits, is_active=True)

app = FastAPI(title="Bybit Recommender (Local MVP)", version="0.1.0")

static_dir = Path(__file__).resolve().parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

class ActivateBotRequest(BaseModel):
    rec_id: str
    dry_run: bool = True
    override_params: dict[str, Any] = Field(default_factory=dict)
    operator: str = "operator"

class StopBotRequest(BaseModel):
    bot_id: str
    operator: str = "operator"

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

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (static_dir / "index.html").read_text(encoding="utf-8")

@app.get("/api/v1/recommendations")
def api_recommendations(venue: str | None = None, top_n: int = 20, min_conf: float = 0.0) -> dict[str, Any]:
    items = db.get_recommendations(conn, venue=venue, top_n=top_n, min_conf=min_conf)

    # compute NO-TRADE flag if no items with status recommended
    has_recommended = any(it["status"] == "recommended" for it in items)
    # load latest regime
    cur = conn.execute("""SELECT regime_json FROM market_regime ORDER BY ts DESC LIMIT 1""")
    row = cur.fetchone()
    regime = json.loads(row["regime_json"]) if row else {"vol_state":"unknown","trend_state":"unknown","risk_state":"unknown","confidence":0.0}

    return {"ts": int(time.time()), "regime": regime, "items": items, "no_trade": (not has_recommended)}

@app.get("/api/v1/recommendations/{rec_id}")
def api_reco_details(rec_id: str) -> dict[str, Any]:
    r = db.get_recommendation_by_id(conn, rec_id)
    if not r:
        raise HTTPException(status_code=404, detail="rec_id not found")
    return r

@app.get("/api/v1/bots")
def api_bots() -> list[dict[str, Any]]:
    rows = conn.execute("""SELECT * FROM bot_instances ORDER BY started_ts DESC LIMIT 200""").fetchall()
    out = []
    for r in rows:
        out.append({
            "bot_id": r["bot_id"],
            "started_ts": r["started_ts"],
            "stopped_ts": r["stopped_ts"],
            "venue": r["venue"],
            "symbol": r["symbol"],
            "bot_type": r["bot_type"],
            "mode": json.loads(r["mode_json"]),
            "params": json.loads(r["params_json"]),
            "state": json.loads(r["state_json"]),
            "status": r["status"],
            "origin_rec_id": r["origin_rec_id"],
        })
    return out

@app.post("/api/v1/bots/activate")
def api_activate_bot(req: ActivateBotRequest) -> dict[str, Any]:
    rec = db.get_recommendation_by_id(conn, req.rec_id)
    if not rec:
        raise HTTPException(status_code=404, detail="rec_id not found")

    # enforce gates again
    limits = get_risk_limits(conn, settings.risk_limits)
    blocks = []
    if rec["venue"] != "spot":  # spot doesn't use some gates, but keep uniform
        pass
    blocks.extend(rec.get("blocks") or [])
    blocks.extend([])

    if rec["status"] == "blocked":
        raise HTTPException(status_code=409, detail={"msg":"recommendation is blocked", "blocks": blocks})

    bot_id = f"B-{int(time.time())}-{os.getpid()}-{req.rec_id[-6:]}"
    params = dict(rec["params"])
    params.update(req.override_params or {})

    bot = {
        "bot_id": bot_id,
        "started_ts": int(time.time()),
        "stopped_ts": None,
        "venue": rec["venue"],
        "symbol": rec["symbol"],
        "bot_type": rec["bot_type"],
        "mode": {"direction": rec["direction"], "account_mode": rec["account_mode"], "margin_mode": rec["margin_mode"], "dry_run": req.dry_run},
        "params": params,
        "state": {"note": "MVP bot runtime stub; integrate execution engine for production"},
        "status": "running",
        "origin_rec_id": req.rec_id,
    }
    db.insert_bot_instance(conn, bot)
    db.log_decision(conn, "ACTIVATE", req.rec_id, req.operator, {"bot_id": bot_id, "dry_run": req.dry_run, "override_params": req.override_params})
    return {"bot_id": bot_id, "status": "running", "dry_run": req.dry_run}

@app.post("/api/v1/bots/stop")
def api_stop_bot(req: StopBotRequest) -> dict[str, Any]:
    ok = db.stop_bot(conn, req.bot_id)
    if not ok:
        raise HTTPException(status_code=404, detail="bot_id not found or not running")
    db.log_decision(conn, "STOP", None, req.operator, {"bot_id": req.bot_id})
    return {"bot_id": req.bot_id, "status": "stopped"}

@app.get("/api/v1/risk/status")
def api_risk_status() -> dict[str, Any]:
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
def api_update_risk_limits(req: UpdateRiskLimitsRequest) -> dict[str, Any]:
    db.upsert_risk_limits(conn, version=req.version, limits=req.limits, is_active=True)
    db.log_decision(conn, "UPDATE_LIMITS", None, None, {"version": req.version, "limits": req.limits})
    return {"ok": True, "version": req.version}

@app.post("/api/v1/sentiment")
def api_sentiment_put(req: SentimentPointRequest) -> dict[str, Any]:
    ts = req.ts or int(time.time())
    db.insert_sentiment_point(conn, req.scope, req.key, ts, req.sentiment, req.velocity, req.volume, req.sources, req.tags)
    db.log_decision(conn, "SENTIMENT_PUT", None, None, {"scope": req.scope, "key": req.key, "ts": ts, "sentiment": req.sentiment})
    return {"ok": True, "ts": ts}

@app.get("/api/v1/sentiment")
def api_sentiment_get(scope: str, key: str, limit: int = 120) -> dict[str, Any]:
    series = db.get_sentiment_series(conn, scope, key, limit=limit)
    return {"scope": scope, "key": key, "items": series}

async def _collector_loop():
    client = BybitPublicClient(settings.bybit_base_url)
    try:
        while True:
            for venue in settings.venues:
                symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
                try:
                    collect_once(conn, client, venue, symbols)
                except Exception as e:
                    db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "err": str(e)})
            await asyncio.sleep(settings.collect_interval_sec)
    finally:
        client.close()

async def _reco_loop():
    while True:
        try:
            run_recommender_once(conn, settings)
        except Exception as e:
            db.log_decision(conn, "RECO_ERROR", None, None, {"err": str(e)})
        await asyncio.sleep(settings.reco_interval_sec)

@app.on_event("startup")
async def startup_event():
    # start background loops
    asyncio.create_task(_collector_loop())
    asyncio.create_task(_reco_loop())

def main():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)

if __name__ == "__main__":
    main()
