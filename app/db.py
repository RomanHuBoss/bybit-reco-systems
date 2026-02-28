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
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
    return list(cur.fetchall())[::-1]  # oldest -> newest

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

def get_recommendations(conn: sqlite3.Connection, venue: str | None, top_n: int, min_conf: float) -> list[dict[str, Any]]:
    q = """SELECT * FROM recommendations WHERE ts > ?"""
    params: list[Any] = [now_ts() - 3600]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    q += " AND confidence>=? ORDER BY score DESC LIMIT ?"
    params.extend([min_conf, top_n])
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

