from __future__ import annotations

import json
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .settings import load_settings
from .bybit_client import BybitPublicClient
from .collector import collect_once
from .sentiment import collect_sentiment_once
from .outcomes import compute_outcomes_once
from .recommender import run_recommender_once
from .risk import get_risk_limits, compute_risk_status
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

app = FastAPI(title="Bybit Recommender (Scenario B)", version="0.9.3")

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

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (static_dir / "index.html").read_text(encoding="utf-8")

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

        items = db.get_recommendations(
            conn,
            venue=venue,
            top_n=top_n,
            min_conf=min_conf,
            statuses=statuses if statuses else None,
            snapshot_ts=snapshot_ts,
        )

        no_trade = True
        if snapshot_ts is not None:
            cur = conn.execute(
                """SELECT COUNT(*) AS c FROM recommendations
                   WHERE ts=? AND (? IS NULL OR venue=?) AND status='recommended'""",
                (snapshot_ts, venue, venue),
            )
            no_trade = (int(cur.fetchone()["c"]) == 0)

        cur = conn.execute("""SELECT regime_json FROM market_regime ORDER BY ts DESC LIMIT 1""")
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
        rec_id = str(rec_id)
        r = db.get_recommendation_by_id(conn, rec_id)
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
def api_update_risk_limits(req: UpdateRiskLimitsRequest) -> dict[str, Any]:
    with closing(_get_conn()) as conn:
        db.upsert_risk_limits(conn, version=req.version, limits=req.limits, is_active=True)
        db.log_decision(conn, "UPDATE_LIMITS", None, None, {"version": req.version, "limits": req.limits})
        return {"ok": True, "version": req.version}

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
def api_sentiment_put(req: SentimentPointRequest) -> dict[str, Any]:
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
    try:
        while True:
            with closing(_get_conn()) as conn:
                for venue in settings.venues:
                    symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
                    try:
                        collect_once(conn, client, venue, symbols)
                    except Exception as e:
                        db.log_decision(conn, "COLLECT_ERROR", None, None, {"venue": venue, "err": str(e)})
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
        time.sleep(60)

def _reco_thread():
    while True:
        with closing(_get_conn()) as conn:
            try:
                run_recommender_once(conn, settings)
                compute_outcomes_once(conn, horizon_sec=settings.outcome_horizon_sec)
            except Exception as e:
                db.log_decision(conn, "RECO_ERROR", None, None, {"err": str(e)})
        time.sleep(settings.reco_interval_sec)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=_collector_thread, daemon=True).start()
    threading.Thread(target=_sentiment_thread, daemon=True).start()
    threading.Thread(target=_reco_thread, daemon=True).start()

def main():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)

if __name__ == "__main__":
    main()


@app.get("/api/v1/status")
def api_status() -> dict[str, Any]:
    """System health: calibrator state, outcome progress, sentiment, collect errors."""
    with closing(_get_conn()) as conn:
        from .calibration import load_platt_from_db
        platt = load_platt_from_db(conn, "platt_bybit_v2")
        calib_fitted = bool(platt and platt.fitted)
        calib_a = float(platt.a) if platt and platt.fitted else None
        calib_b = float(platt.b) if platt and platt.fitted else None

        cur = conn.execute("SELECT COUNT(*) AS c FROM reco_outcomes")
        outcome_count = int(cur.fetchone()["c"])

        last_reco_ts = db.get_latest_reco_ts(conn)

        # Count collect errors in last 10 min
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR' AND ts >= ?",
            (db.now_ts() - 600,)
        )
        collect_errors_10m = int(cur.fetchone()["c"])

        # Latest sentiment
        from .sentiment_features import compute_sentiment_agg
        sent = compute_sentiment_agg(conn, scope="global", key="crypto")

        return {
            "calibrator_fitted": calib_fitted,
            "calibrator_params": {"a": calib_a, "b": calib_b},
            "outcome_count": outcome_count,
            "calib_min_samples": settings.calib_min_samples,
            "last_reco_ts": last_reco_ts,
            "collect_errors_10m": collect_errors_10m,
            "sentiment": {
                "regime": sent.get("regime"),
                "strength": sent.get("strength"),
                "ewma_6h": sent.get("ewma", {}).get("6h"),
                "flags": sent.get("flags"),
            },
        }
