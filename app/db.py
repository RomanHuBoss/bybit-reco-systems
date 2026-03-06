from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

MIGRATION_INIT_SQL = Path(__file__).resolve().parent.parent / "migrations" / "init.sql"

def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # Multiple background threads write concurrently (collector/sentiment/recommender/outcomes).
    # Use a longer SQLite busy timeout to avoid transient "database is locked" write failures.
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    sql = MIGRATION_INIT_SQL.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()

def now_ts() -> int:
    return int(time.time())

def upsert_ohlcv(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO ohlcv(venue,symbol,tf_sec,ts,open,high,low,close,volume)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        [(r["venue"], r["symbol"], r["tf_sec"], r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows],
    )
    conn.commit()

def insert_tickers(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO ticker_snap(venue,symbol,ts,last,bid,ask,vol24h,turnover24h)
           VALUES(?,?,?,?,?,?,?,?)""",
        [(r["venue"], r["symbol"], r["ts"], r.get("last"), r.get("bid"), r.get("ask"), r.get("vol24h"), r.get("turnover24h")) for r in rows],
    )
    conn.commit()

def insert_features(conn: sqlite3.Connection, venue: str, symbol: str, ts: int, features: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO features(venue,symbol,ts,features_json) VALUES(?,?,?,?)""",
        (venue, symbol, ts, json.dumps(features, ensure_ascii=False)),
    )
    conn.commit()

def insert_regime(conn: sqlite3.Connection, ts: int, regime: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO market_regime(ts, regime_json) VALUES(?,?)""",
        (ts, json.dumps(regime, ensure_ascii=False)),
    )
    conn.commit()

def insert_recommendations(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO recommendations(
            rec_id,ts,venue,symbol,bot_type,direction,account_mode,margin_mode,
            score,confidence,expected_rr,risk_score,
            params_json,reasons_json,blocks_json,status,ttl_sec,model_version,features_ref_ts
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(
            r["rec_id"], r["ts"], r["venue"], r["symbol"], r["bot_type"], r["direction"], r["account_mode"], r["margin_mode"],
            r["score"], r["confidence"], r["expected_rr"], r["risk_score"],
            json.dumps(r["params"], ensure_ascii=False),
            json.dumps(r["reasons"], ensure_ascii=False),
            json.dumps(r["blocks"], ensure_ascii=False),
            r["status"], r["ttl_sec"], r["model_version"], r["features_ref_ts"]
        ) for r in rows],
    )
    conn.commit()

def log_decision(conn: sqlite3.Connection, action: str, rec_id: str | None, operator: str | None, details: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO decision_log(ts, action, rec_id, operator, details_json) VALUES(?,?,?,?,?)""",
        (now_ts(), action, rec_id, operator, json.dumps(details, ensure_ascii=False)),
    )
    conn.commit()

def get_latest_ohlcv(conn: sqlite3.Connection, venue: str, symbol: str, tf_sec: int, limit: int = 240) -> list[sqlite3.Row]:
    cur = conn.execute(
        """SELECT * FROM ohlcv WHERE venue=? AND symbol=? AND tf_sec=? ORDER BY ts DESC LIMIT ?""",
        (venue, symbol, tf_sec, limit),
    )
    # IMPORTANT CONTRACT:
    #   Returned rows are ordered newest -> oldest (ts DESC), matching the SQL.
    #   Callers that need oldest -> newest (e.g. indicator calculations) must reverse().
    return list(cur.fetchall())

def get_latest_ticker(conn: sqlite3.Connection, venue: str, symbol: str) -> sqlite3.Row | None:
    cur = conn.execute(
        """SELECT * FROM ticker_snap WHERE venue=? AND symbol=? ORDER BY ts DESC LIMIT 1""",
        (venue, symbol),
    )
    return cur.fetchone()

def get_latest_features_ts(conn: sqlite3.Connection, venue: str, symbol: str) -> int | None:
    cur = conn.execute(
        """SELECT ts FROM features WHERE venue=? AND symbol=? ORDER BY ts DESC LIMIT 1""",
        (venue, symbol),
    )
    row = cur.fetchone()
    return int(row["ts"]) if row else None

def get_latest_features(conn: sqlite3.Connection, venue: str, symbol: str) -> dict[str, Any] | None:
    cur = conn.execute(
        """SELECT features_json FROM features WHERE venue=? AND symbol=? ORDER BY ts DESC LIMIT 1""",
        (venue, symbol),
    )
    row = cur.fetchone()
    return json.loads(row["features_json"]) if row else None

def get_active_bots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute("""SELECT * FROM bot_instances WHERE status='running'""")
    return list(cur.fetchall())

def count_active_bots_for_symbol(conn: sqlite3.Connection, venue: str, symbol: str) -> int:
    cur = conn.execute("""SELECT COUNT(1) AS c FROM bot_instances WHERE status='running' AND venue=? AND symbol=?""", (venue, symbol))
    return int(cur.fetchone()["c"])

def insert_bot_instance(conn: sqlite3.Connection, bot: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO bot_instances(
            bot_id, started_ts, stopped_ts, venue, symbol, bot_type,
            mode_json, params_json, state_json, status, origin_rec_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            bot["bot_id"], bot["started_ts"], bot.get("stopped_ts"),
            bot["venue"], bot["symbol"], bot["bot_type"],
            json.dumps(bot["mode"], ensure_ascii=False),
            json.dumps(bot["params"], ensure_ascii=False),
            json.dumps(bot["state"], ensure_ascii=False),
            bot["status"], bot.get("origin_rec_id")
        ),
    )
    conn.commit()

def stop_bot(conn: sqlite3.Connection, bot_id: str) -> bool:
    cur = conn.execute("""SELECT bot_id FROM bot_instances WHERE bot_id=? AND status='running'""", (bot_id,))
    if not cur.fetchone():
        return False
    conn.execute("""UPDATE bot_instances SET status='stopped', stopped_ts=? WHERE bot_id=?""", (now_ts(), bot_id))
    conn.commit()
    return True

def _decode_bot_row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if not r:
        return None
    return {
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
    }


def get_bot_instance(conn: sqlite3.Connection, bot_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM bot_instances WHERE bot_id=?", (bot_id,))
    return _decode_bot_row(cur.fetchone())


def get_bot_by_origin_rec(conn: sqlite3.Connection, origin_rec_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM bot_instances WHERE origin_rec_id=? ORDER BY started_ts DESC LIMIT 1", (origin_rec_id,))
    return _decode_bot_row(cur.fetchone())


def list_bot_instances(conn: sqlite3.Connection, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if status:
        cur = conn.execute("SELECT * FROM bot_instances WHERE status=? ORDER BY started_ts DESC LIMIT ?", (status, limit))
    else:
        cur = conn.execute("SELECT * FROM bot_instances ORDER BY started_ts DESC LIMIT ?", (limit,))
    return [_decode_bot_row(r) for r in cur.fetchall()]


def update_bot_state(conn: sqlite3.Connection, bot_id: str, patch: dict[str, Any], merge: bool = True) -> bool:
    cur = conn.execute("SELECT state_json FROM bot_instances WHERE bot_id=?", (bot_id,))
    row = cur.fetchone()
    if not row:
        return False
    state = json.loads(row["state_json"]) if row["state_json"] else {}
    state = {**state, **patch} if merge else dict(patch)
    conn.execute("UPDATE bot_instances SET state_json=? WHERE bot_id=?", (json.dumps(state, ensure_ascii=False), bot_id))
    conn.commit()
    return True


def insert_trade(conn: sqlite3.Connection, trade: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO trades(trade_id, bot_id, ts, symbol, pnl, fee, meta_json)
           VALUES(?,?,?,?,?,?,?)""",
        (
            trade["trade_id"],
            trade["bot_id"],
            int(trade["ts"]),
            trade["symbol"],
            float(trade.get("pnl") or 0.0),
            float(trade.get("fee") or 0.0),
            json.dumps(trade.get("meta") or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def list_trades(conn: sqlite3.Connection, bot_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if bot_id:
        cur = conn.execute(
            "SELECT * FROM trades WHERE bot_id=? ORDER BY ts DESC LIMIT ?",
            (bot_id, limit),
        )
    else:
        cur = conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))
    out = []
    for r in cur.fetchall():
        out.append({
            "trade_id": r["trade_id"],
            "bot_id": r["bot_id"],
            "ts": r["ts"],
            "symbol": r["symbol"],
            "pnl": r["pnl"],
            "fee": r["fee"],
            "meta": json.loads(r["meta_json"]),
        })
    return out



def get_recommendations(conn: sqlite3.Connection, venue: str | None, top_n: int, min_conf: float, statuses: list[str] | None = None, snapshot_ts: int | None = None) -> list[dict[str, Any]]:
    if snapshot_ts is not None:
        q = """SELECT * FROM recommendations WHERE ts = ?"""
        params: list[Any] = [snapshot_ts]
    else:
        # Use 24h window so executed/ignored/expired recs remain visible for audit
        q = """SELECT * FROM recommendations WHERE ts > ?"""
        params: list[Any] = [now_ts() - 86400]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    # Apply min_conf only to status=recommended so blocked/no_trade/suppressed are still visible.
    q += " AND (status != ? OR confidence >= ?)"
    params.extend(["recommended", min_conf])
    if statuses is not None:
        if not statuses:
            # Empty list → caller wants no statuses → return nothing
            return []
        placeholders = ",".join("?" for _ in statuses)
        q += f" AND status IN ({placeholders})"
        params.extend(statuses)
    q += " ORDER BY score DESC LIMIT ?"
    params.append(top_n)
    cur = conn.execute(q, params)
    rows = []
    for r in cur.fetchall():
        rows.append({
            "rec_id": r["rec_id"],
            "ts": r["ts"],
            "venue": r["venue"],
            "symbol": r["symbol"],
            "bot_type": r["bot_type"],
            "direction": r["direction"],
            "account_mode": r["account_mode"],
            "margin_mode": r["margin_mode"],
            "score": r["score"],
            "confidence": r["confidence"],
            "expected_rr": r["expected_rr"],
            "risk_score": r["risk_score"],
            "params": json.loads(r["params_json"]),
            "reasons": json.loads(r["reasons_json"]),
            "blocks": json.loads(r["blocks_json"]),
            "status": r["status"],
            "ttl_sec": r["ttl_sec"],
            "model_version": r["model_version"],
            "features_ref_ts": r["features_ref_ts"],
        })
    return rows

def get_recommendation_by_id(conn: sqlite3.Connection, rec_id: str) -> dict[str, Any] | None:
    cur = conn.execute("""SELECT * FROM recommendations WHERE rec_id=?""", (rec_id,))
    r = cur.fetchone()
    if not r:
        return None
    return {
        "rec_id": r["rec_id"],
        "ts": r["ts"],
        "venue": r["venue"],
        "symbol": r["symbol"],
        "bot_type": r["bot_type"],
        "direction": r["direction"],
        "account_mode": r["account_mode"],
        "margin_mode": r["margin_mode"],
        "score": r["score"],
        "confidence": r["confidence"],
        "expected_rr": r["expected_rr"],
        "risk_score": r["risk_score"],
        "params": json.loads(r["params_json"]),
        "reasons": json.loads(r["reasons_json"]),
        "blocks": json.loads(r["blocks_json"]),
        "status": r["status"],
        "ttl_sec": r["ttl_sec"],
        "model_version": r["model_version"],
        "features_ref_ts": r["features_ref_ts"],
    }

def upsert_risk_limits(conn: sqlite3.Connection, version: str, limits: dict[str, Any], is_active: bool = True) -> None:
    if is_active:
        conn.execute("""UPDATE risk_limits SET is_active=0""")
    conn.execute(
        """INSERT INTO risk_limits(version, limits_json, is_active, created_ts) VALUES(?,?,?,?)""",
        (version, json.dumps(limits, ensure_ascii=False), 1 if is_active else 0, now_ts()),
    )
    conn.commit()

def get_active_risk_limits(conn: sqlite3.Connection) -> dict[str, Any] | None:
    cur = conn.execute("""SELECT limits_json FROM risk_limits WHERE is_active=1 ORDER BY created_ts DESC LIMIT 1""")
    row = cur.fetchone()
    return json.loads(row["limits_json"]) if row else None

def insert_sentiment_point(conn: sqlite3.Connection, scope: str, key: str, ts: int, sentiment: float, velocity: float, volume: int, sources: dict, tags: list[str]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        (scope, key, ts, float(sentiment), float(velocity), int(volume), json.dumps(sources, ensure_ascii=False), json.dumps(tags, ensure_ascii=False)),
    )
    conn.commit()

def get_sentiment_series(conn: sqlite3.Connection, scope: str, key: str, limit: int = 120) -> list[dict[str, Any]]:
    cur = conn.execute(
        """SELECT * FROM sentiment WHERE scope=? AND key=? ORDER BY ts DESC LIMIT ?""",
        (scope, key, limit),
    )
    out = []
    for r in cur.fetchall()[::-1]:
        out.append({
            "scope": r["scope"],
            "key": r["key"],
            "ts": r["ts"],
            "sentiment": r["sentiment"],
            "velocity": r["velocity"],
            "volume": r["volume"],
            "sources": json.loads(r["sources_json"]),
            "tags": json.loads(r["tags_json"]),
        })
    return out

def sum_daily_pnl(conn: sqlite3.Connection, day_start_ts: int) -> float:
    cur = conn.execute("""SELECT COALESCE(SUM(pnl),0.0) AS s FROM trades WHERE ts>=?""", (day_start_ts,))
    return float(cur.fetchone()["s"])



def get_latest_sentiment(conn: sqlite3.Connection, scope: str, key: str) -> dict[str, Any] | None:
    cur = conn.execute(
        """SELECT * FROM sentiment WHERE scope=? AND key=? ORDER BY ts DESC LIMIT 1""",
        (scope, key),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "scope": r["scope"],
        "key": r["key"],
        "ts": r["ts"],
        "sentiment": r["sentiment"],
        "velocity": r["velocity"],
        "volume": r["volume"],
        "sources": json.loads(r["sources_json"]),
        "tags": json.loads(r["tags_json"]),
    }


def get_outcomes_with_recs(conn: sqlite3.Connection, limit: int = 6000) -> list[dict[str, Any]]:
    """Returns outcomes joined with rec score/bot_type/direction/reasons in one query.
    Replaces N+1 pattern of get_outcomes_recent + get_recommendation_by_id per row.
    """
    cur = conn.execute(
        """SELECT o.rec_id, o.ts, o.venue, o.symbol, o.bot_type, o.direction,
                  o.success, o.ret,
                  r.score, r.reasons_json
           FROM reco_outcomes o
           JOIN recommendations r ON r.rec_id = o.rec_id
           ORDER BY o.ts DESC LIMIT ?""",
        (limit,),
    )
    out = []
    for row in cur.fetchall():
        try:
            reasons = json.loads(row["reasons_json"]) if row["reasons_json"] else {}
        except Exception:
            reasons = {}
        out.append({
            "rec_id":    row["rec_id"],
            "ts":        row["ts"],
            "venue":     row["venue"],
            "symbol":    row["symbol"],
            "bot_type":  row["bot_type"],
            "direction": row["direction"],
            "success":   int(row["success"]),
            "ret":       float(row["ret"]),
            "score":     float(row["score"]),
            "reasons":   reasons,
        })
    return out

def insert_outcome(conn: sqlite3.Connection, o: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO reco_outcomes(
            rec_id, ts, venue, symbol, bot_type, direction, horizon_sec,
            entry_close, exit_close, ret, success
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            o["rec_id"], o["ts"], o["venue"], o["symbol"], o["bot_type"], o["direction"], o["horizon_sec"],
            o["entry_close"], o["exit_close"], o["ret"], o["success"]
        ),
    )
    conn.commit()

def outcome_exists(conn: sqlite3.Connection, rec_id: str) -> bool:
    cur = conn.execute("""SELECT 1 FROM reco_outcomes WHERE rec_id=? LIMIT 1""", (rec_id,))
    return cur.fetchone() is not None

def get_outcomes_recent(conn: sqlite3.Connection, limit: int = 2000) -> list[dict[str, Any]]:
    cur = conn.execute("""SELECT * FROM reco_outcomes ORDER BY ts DESC LIMIT ?""", (limit,))
    out = []
    for r in cur.fetchall():
        out.append({k: r[k] for k in r.keys()})
    return out


def get_latest_reco_ts(conn: sqlite3.Connection, venue: str | None = None) -> int | None:
    if venue:
        cur = conn.execute("""SELECT MAX(ts) AS m FROM recommendations WHERE venue=?""", (venue,))
    else:
        cur = conn.execute("""SELECT MAX(ts) AS m FROM recommendations""")
    r = cur.fetchone()
    return int(r["m"]) if r and r["m"] is not None else None


# ── funding rate ──────────────────────────────────────────────────────────────

def upsert_funding_rate(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO funding_rate(symbol, ts, funding_rate, next_funding_ts)
           VALUES(?,?,?,?)""",
        [(r["symbol"], r["ts"], r["funding_rate"], r.get("next_funding_ts")) for r in rows],
    )
    conn.commit()

def get_latest_funding_rate(conn: sqlite3.Connection, symbol: str) -> dict | None:
    cur = conn.execute(
        """SELECT * FROM funding_rate WHERE symbol=? ORDER BY ts DESC LIMIT 1""",
        (symbol,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {"symbol": r["symbol"], "ts": r["ts"], "funding_rate": r["funding_rate"],
            "next_funding_ts": r["next_funding_ts"]}

# ── open interest ─────────────────────────────────────────────────────────────

def upsert_open_interest(conn: sqlite3.Connection, symbol: str, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO open_interest(symbol, ts, oi) VALUES(?,?,?)""",
        [(symbol, r["ts"], r["oi"]) for r in rows],
    )
    conn.commit()

def get_oi_series(conn: sqlite3.Connection, symbol: str, limit: int = 48) -> list[dict]:
    cur = conn.execute(
        """SELECT ts, oi FROM open_interest WHERE symbol=? ORDER BY ts DESC LIMIT ?""",
        (symbol, limit),
    )
    return [{"ts": r["ts"], "oi": r["oi"]} for r in cur.fetchall()]


# ── Operator actions ──────────────────────────────────────────────────────────

OPERATOR_STATUSES = {"executed", "ignored", "recommended", "blocked", "no_trade", "suppressed", "expired"}

def update_recommendation_status(
    conn: sqlite3.Connection, rec_id: str, status: str, operator: str | None = None
) -> bool:
    if status not in OPERATOR_STATUSES:
        return False
    cur = conn.execute(
        """UPDATE recommendations SET status=? WHERE rec_id=?""",
        (status, rec_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return False
    log_decision(conn, "STATUS_UPDATE", rec_id, operator, {"new_status": status})
    return True


# ── TTL expiry ────────────────────────────────────────────────────────────────

def expire_stale_recommendations(conn: sqlite3.Connection) -> int:
    """Mark recommended recs as expired if ts + ttl_sec < now.
    Only expires status='recommended' — operator-set statuses are preserved.
    Returns count of expired rows.
    """
    ts_now = now_ts()
    cur = conn.execute(
        """UPDATE recommendations
           SET status='expired'
           WHERE status='recommended'
             AND (ts + ttl_sec) < ?""",
        (ts_now,),
    )
    conn.commit()
    expired = cur.rowcount
    if expired > 0:
        log_decision(conn, "TTL_EXPIRED", None, None, {"count": expired, "ts": ts_now})
    return expired


# ── Outcomes stats ────────────────────────────────────────────────────────────

def get_outcomes_stats(conn: sqlite3.Connection) -> dict:
    """Aggregate win-rate by bot_type, symbol, and regime from reco_outcomes.
    Regime is joined from market_regime by matching the closest ts.
    """
    # Overall
    cur = conn.execute(
        """SELECT bot_type, direction,
                  COUNT(*) as total,
                  SUM(success) as wins,
                  AVG(ret) as avg_ret,
                  AVG(ABS(ret)) as avg_abs_ret
           FROM reco_outcomes
           GROUP BY bot_type, direction
           ORDER BY bot_type, direction"""
    )
    by_bot: list[dict] = []
    for r in cur.fetchall():
        total = int(r["total"])
        wins  = int(r["wins"])
        by_bot.append({
            "bot_type":  r["bot_type"],
            "direction": r["direction"],
            "total":     total,
            "wins":      wins,
            "win_rate":  round(wins / total, 3) if total else 0,
            "avg_ret":   round(float(r["avg_ret"] or 0) * 100, 3),
            "avg_abs_ret": round(float(r["avg_abs_ret"] or 0) * 100, 3),
        })

    # By symbol
    cur = conn.execute(
        """SELECT symbol, bot_type,
                  COUNT(*) as total,
                  SUM(success) as wins,
                  AVG(ret) as avg_ret
           FROM reco_outcomes
           GROUP BY symbol, bot_type
           ORDER BY total DESC"""
    )
    by_symbol: list[dict] = []
    for r in cur.fetchall():
        total = int(r["total"])
        wins  = int(r["wins"])
        by_symbol.append({
            "symbol":   r["symbol"],
            "bot_type": r["bot_type"],
            "total":    total,
            "wins":     wins,
            "win_rate": round(wins / total, 3) if total else 0,
            "avg_ret":  round(float(r["avg_ret"] or 0) * 100, 3),
        })

    # Summary
    cur = conn.execute(
        """SELECT COUNT(*) as total, SUM(success) as wins FROM reco_outcomes"""
    )
    r = cur.fetchone()
    total = int(r["total"] or 0)
    wins  = int(r["wins"]  or 0)
    summary = {
        "total":    total,
        "wins":     wins,
        "win_rate": round(wins / total, 3) if total else None,
    }

    return {"summary": summary, "by_bot": by_bot, "by_symbol": by_symbol}


# ── Symbol health ─────────────────────────────────────────────────────────────

def get_symbol_health(conn: sqlite3.Connection, symbols_spot: list[str], symbols_linear: list[str], stale_sec: int = 300) -> list[dict]:
    """
    Returns health status for each configured symbol:
      last_candle_ts: newest 1m candle timestamp
      last_ticker_ts: newest ticker timestamp
      age_sec:        seconds since last candle
      status:         'ok' | 'stale' | 'missing'
      error_count_10m: COLLECT_ERRORs for this symbol in last 10 min
      disabled:       True if SYMBOL_DISABLED in last 24h
    """
    now = now_ts()
    result: list[dict] = []

    # collect recent errors per symbol
    cur = conn.execute(
        """SELECT details_json FROM decision_log
           WHERE action='COLLECT_ERROR' AND ts >= ?""",
        (now - 600,),
    )
    error_counts: dict[str, int] = {}
    for row in cur.fetchall():
        try:
            d = json.loads(row["details_json"])
            sym = d.get("symbol", "UNKNOWN")
            error_counts[sym] = error_counts.get(sym, 0) + 1
        except Exception:
            pass

    # disabled symbols in last 24h
    cur = conn.execute(
        """SELECT details_json FROM decision_log
           WHERE action='SYMBOL_DISABLED' AND ts >= ?""",
        (now - 86400,),
    )
    disabled_syms: set[str] = set()
    for row in cur.fetchall():
        try:
            d = json.loads(row["details_json"])
            disabled_syms.add(d.get("symbol", ""))
        except Exception:
            pass

    # stale skip counts per symbol in last hour
    cur = conn.execute(
        """SELECT details_json FROM decision_log
           WHERE action='STALE_DATA_SKIP' AND ts >= ?""",
        (now - 3600,),
    )
    stale_counts: dict[str, int] = {}
    for row in cur.fetchall():
        try:
            d = json.loads(row["details_json"])
            sym = d.get("symbol", "UNKNOWN")
            stale_counts[sym] = stale_counts.get(sym, 0) + 1
        except Exception:
            pass

    for venue, symbols in [("spot", symbols_spot), ("linear", symbols_linear)]:
        for sym in symbols:
            # last 1m candle
            cur2 = conn.execute(
                """SELECT MAX(ts) as m FROM ohlcv WHERE venue=? AND symbol=? AND tf_sec=60""",
                (venue, sym),
            )
            r = cur2.fetchone()
            last_ts = int(r["m"]) if r and r["m"] else None
            age_sec = (now - last_ts) if last_ts else None

            if last_ts is None:
                status = "missing"
            elif age_sec > stale_sec:
                status = "stale"
            else:
                status = "ok"

            result.append({
                "venue":           venue,
                "symbol":          sym,
                "last_candle_ts":  last_ts,
                "age_sec":         age_sec,
                "status":          status,
                "error_count_10m": error_counts.get(sym, 0),
                "stale_skips_1h":  stale_counts.get(sym, 0),
                "disabled":        sym in disabled_syms,
            })

    return sorted(result, key=lambda x: (x["status"] != "missing", x["status"] != "stale", x["symbol"]))

def prune_old_data(conn: sqlite3.Connection, retain_days: int = 7) -> dict[str, int]:
    """Prune old rows from high-growth tables. Call periodically (e.g. once per hour).
    Retains retain_days of data. Returns count of deleted rows per table.
    """
    cutoff = now_ts() - retain_days * 86400
    cutoff_14d = now_ts() - 14 * 86400  # sentiment: keep 14 days for EWMA
    deleted = {}

    # features: keep 1 day (used only for current cycle reference)
    cur = conn.execute("DELETE FROM features WHERE ts < ?", (now_ts() - 86400,))
    deleted["features"] = cur.rowcount

    # market_regime: keep 7 days
    cur = conn.execute("DELETE FROM market_regime WHERE ts < ?", (cutoff,))
    deleted["market_regime"] = cur.rowcount

    # decision_log: keep 7 days
    cur = conn.execute("DELETE FROM decision_log WHERE ts < ?", (cutoff,))
    deleted["decision_log"] = cur.rowcount

    # sentiment: keep 14 days (EWMA uses 7d window with margin)
    cur = conn.execute("DELETE FROM sentiment WHERE ts < ?", (cutoff_14d,))
    deleted["sentiment"] = cur.rowcount

    # recommendations: keep 14 days — MUST match outcomes retention.
    # get_outcomes_with_recs() uses an INNER JOIN on rec_id.
    # If recs are pruned at 7d but outcomes live 14d, the JOIN silently drops
    # all outcomes whose rec was already pruned → calibrator loses half its training data.
    cur = conn.execute("DELETE FROM recommendations WHERE ts < ? AND status NOT IN ('executed','ignored')", (cutoff_14d,))
    deleted["recommendations"] = cur.rowcount

    # reco_outcomes: keep 14 days (calibrator uses up to 6000 recent outcomes)
    cur = conn.execute("DELETE FROM reco_outcomes WHERE ts < ?", (cutoff_14d,))
    deleted["reco_outcomes"] = cur.rowcount

    # ohlcv: keep 30 days. Without pruning this grows at ~216K rows/day
    # (30 symbols × 5 TFs × 1440 1m candles/day). INSERT OR REPLACE only
    # refreshes the last ~220-420 candles — older rows accumulate indefinitely.
    # 30d is far more than any indicator needs (max window: 420 candles ≈ 7h for 1m).
    cutoff_30d = now_ts() - 30 * 86400
    cur = conn.execute("DELETE FROM ohlcv WHERE ts < ?", (cutoff_30d,))
    deleted["ohlcv"] = cur.rowcount

    # ticker_snap: keep 2 days (only latest snapshot is used at inference time)
    cur = conn.execute("DELETE FROM ticker_snap WHERE ts < ?", (now_ts() - 2 * 86400,))
    deleted["ticker_snap"] = cur.rowcount

    # funding_rate: keep 7 days (only current value used; history not queried)
    cur = conn.execute("DELETE FROM funding_rate WHERE ts < ?", (cutoff,))
    deleted["funding_rate"] = cur.rowcount

    # open_interest: keep 7 days (oi_trend uses last 48 1h candles = 2 days)
    cur = conn.execute("DELETE FROM open_interest WHERE ts < ?", (cutoff,))
    deleted["open_interest"] = cur.rowcount

    conn.commit()
    return deleted

