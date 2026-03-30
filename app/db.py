from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable
from .bot_types import is_supported_bot_type, sql_in_clause
import logging

logger = logging.getLogger(__name__)

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
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
    except Exception:
        logger.debug("PRAGMA setup error", exc_info=True)
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    sql = MIGRATION_INIT_SQL.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.execute("""CREATE TABLE IF NOT EXISTS runtime_locks (
      lock_key TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      heartbeat_ts INTEGER NOT NULL
    )""")
    conn.commit()

def now_ts() -> int:
    return int(time.time())


def _json_loads_or_default(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        loaded = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return default
    return loaded


def _finite_float_or_default(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(num):
        return float(default)
    return num


def _require_finite_float(name: str, value: Any, *, minimum: float | None = None) -> float:
    try:
        num = float(value)
    except Exception as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(num):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and num < float(minimum):
        raise ValueError(f"{name} must be >= {minimum}")
    return num


def set_app_config_json(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)",
        (str(key), json.dumps(value, ensure_ascii=False), now_ts()),
    )
    conn.commit()


def get_app_config_json(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    cur = conn.execute("SELECT value_json FROM app_config WHERE key=?", (str(key),))
    row = cur.fetchone()
    if not row:
        return default
    try:
        return _json_loads_or_default(row["value_json"], default)
    except Exception:
        return default

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



def _is_valid_ticker_row(row: Any) -> bool:
    try:
        ts = int(row["ts"] or 0)
    except Exception:
        return False
    if ts <= 0:
        return False

    last = row["last"]
    bid = row["bid"]
    ask = row["ask"]
    turnover = row["turnover24h"]
    vol24h = row["vol24h"]

    def _optional_non_negative(value: Any, *, strictly_positive: bool = False) -> float | None:
        if value in (None, ""):
            return None
        try:
            num = float(value)
        except Exception:
            return None
        if not math.isfinite(num):
            return None
        if strictly_positive and num <= 0:
            return None
        if not strictly_positive and num < 0:
            return None
        return num

    last_num = _optional_non_negative(last, strictly_positive=True)
    if last not in (None, "") and last_num is None:
        return False
    bid_num = _optional_non_negative(bid, strictly_positive=True)
    ask_num = _optional_non_negative(ask, strictly_positive=True)
    if bid not in (None, "") and bid_num is None:
        return False
    if ask not in (None, "") and ask_num is None:
        return False
    if bid_num is not None and ask_num is not None and ask_num < bid_num:
        return False
    if turnover not in (None, "") and _optional_non_negative(turnover) is None:
        return False
    if vol24h not in (None, "") and _optional_non_negative(vol24h) is None:
        return False
    return True


def _sanitize_ticker_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    if not _is_valid_ticker_row(payload):
        # Historical rows may contain crossed quotes or non-finite values.
        # Keep the turnover/volume snapshot but drop the quote fields so cost-model
        # falls back to conservative defaults instead of treating bad quotes as free liquidity.
        bid = _finite_float_or_default(payload.get('bid'), float('nan'))
        ask = _finite_float_or_default(payload.get('ask'), float('nan'))
        last = _finite_float_or_default(payload.get('last'), float('nan'))
        if not math.isfinite(last) or last <= 0:
            payload['last'] = None
        if not (math.isfinite(bid) and bid > 0):
            payload['bid'] = None
        if not (math.isfinite(ask) and ask > 0):
            payload['ask'] = None
        if payload.get('bid') is not None and payload.get('ask') is not None and float(payload['ask']) < float(payload['bid']):
            payload['bid'] = None
            payload['ask'] = None
        if payload.get('vol24h') is not None and not math.isfinite(_finite_float_or_default(payload.get('vol24h'), float('nan'))):
            payload['vol24h'] = None
        if payload.get('turnover24h') is not None and not math.isfinite(_finite_float_or_default(payload.get('turnover24h'), float('nan'))):
            payload['turnover24h'] = None
    return payload

def _is_valid_ohlcv_row(row: Any) -> bool:
    try:
        ts = int(row["ts"] or 0)
        open_px = float(row["open"])
        high_px = float(row["high"])
        low_px = float(row["low"])
        close_px = float(row["close"])
        volume = float(row["volume"] or 0.0)
    except Exception:
        return False
    if ts <= 0:
        return False
    vals = (open_px, high_px, low_px, close_px, volume)
    if not all(math.isfinite(v) for v in vals):
        return False
    if min(open_px, high_px, low_px, close_px) <= 0:
        return False
    if volume < 0:
        return False
    if high_px < max(open_px, close_px, low_px):
        return False
    if low_px > min(open_px, close_px, high_px):
        return False
    return True


def get_latest_ohlcv(conn: sqlite3.Connection, venue: str, symbol: str, tf_sec: int, limit: int = 240) -> list[sqlite3.Row]:
    safe_limit = max(1, int(limit))
    fetch_limit = max(safe_limit, min(5000, safe_limit * 4))
    cur = conn.execute(
        """SELECT * FROM ohlcv WHERE venue=? AND symbol=? AND tf_sec=? ORDER BY ts DESC LIMIT ?""",
        (venue, symbol, tf_sec, fetch_limit),
    )
    # IMPORTANT CONTRACT:
    #   Returned rows are ordered newest -> oldest (ts DESC), matching the SQL.
    #   Callers that need oldest -> newest (e.g. indicator calculations) must reverse().
    #
    # Defensive filtering: historical DB rows may contain malformed OHLCV values from
    # prior builds or manual imports. Over-fetch before filtering so a cluster of bad
    # newest bars does not starve callers of older valid history.
    valid_rows = [row for row in cur.fetchall() if _is_valid_ohlcv_row(row)]
    return valid_rows[:safe_limit]

def get_latest_ticker(conn: sqlite3.Connection, venue: str, symbol: str) -> dict[str, Any] | None:
    cur = conn.execute(
        """SELECT * FROM ticker_snap WHERE venue=? AND symbol=? ORDER BY ts DESC LIMIT 1""",
        (venue, symbol),
    )
    return _sanitize_ticker_row(cur.fetchone())

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
    return _json_loads_or_default(row["features_json"], None) if row else None

def get_active_bots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    cur = conn.execute(
        f"""SELECT * FROM bot_instances
               WHERE status='running' AND {_supported_sql}""",
        _supported_params,
    )
    return list(cur.fetchall())


def count_active_bots_for_symbol(conn: sqlite3.Connection, venue: str, symbol: str) -> int:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    cur = conn.execute(
        f"""SELECT COUNT(1) AS c FROM bot_instances
               WHERE status='running' AND venue=? AND symbol=? AND {_supported_sql}""",
        (venue, symbol, *_supported_params),
    )
    return int(cur.fetchone()["c"])


def insert_bot_instance(conn: sqlite3.Connection, bot: dict[str, Any]) -> str:
    """Insert a bot instance without replacing an existing logical bot.

    Returns:
      - ``inserted`` when a new row is written
      - ``duplicate_origin`` when another row with the same origin_rec_id already exists
      - ``duplicate_bot_id`` when the exact bot_id already exists with identical payload

    Raises:
      ValueError when the same bot_id already exists with different payload.
      sqlite3.IntegrityError for other unexpected uniqueness conflicts.
    """
    payload = (
        bot["bot_id"],
        int(bot["started_ts"]),
        bot.get("stopped_ts"),
        bot["venue"],
        bot["symbol"],
        bot["bot_type"],
        json.dumps(bot["mode"], ensure_ascii=False),
        json.dumps(bot["params"], ensure_ascii=False),
        json.dumps(bot["state"], ensure_ascii=False),
        bot["status"],
        bot.get("origin_rec_id"),
    )

    cur = conn.execute(
        """SELECT started_ts, stopped_ts, venue, symbol, bot_type,
                  mode_json, params_json, state_json, status, origin_rec_id
           FROM bot_instances WHERE bot_id=?""",
        (bot["bot_id"],),
    )
    row = cur.fetchone()
    if row:
        existing = (
            int(row["started_ts"]),
            row["stopped_ts"],
            row["venue"],
            row["symbol"],
            row["bot_type"],
            row["mode_json"],
            row["params_json"],
            row["state_json"],
            row["status"],
            row["origin_rec_id"],
        )
        incoming = payload[1:]
        if existing == incoming:
            return "duplicate_bot_id"
        raise ValueError(f"bot_id={bot['bot_id']} already exists with different payload")

    try:
        conn.execute(
            """INSERT INTO bot_instances(
                bot_id, started_ts, stopped_ts, venue, symbol, bot_type,
                mode_json, params_json, state_json, status, origin_rec_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        conn.commit()
        return "inserted"
    except sqlite3.IntegrityError:
        origin_rec_id = bot.get("origin_rec_id")
        if origin_rec_id:
            existing = get_bot_by_origin_rec(conn, str(origin_rec_id))
            if existing is not None:
                return "duplicate_origin"
        raise

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
        "mode": _json_loads_or_default(r["mode_json"], {}),
        "params": _json_loads_or_default(r["params_json"], {}),
        "state": _json_loads_or_default(r["state_json"], {}),
        "status": r["status"],
        "origin_rec_id": r["origin_rec_id"],
    }


def get_bot_instance(conn: sqlite3.Connection, bot_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM bot_instances WHERE bot_id=?", (bot_id,))
    bot = _decode_bot_row(cur.fetchone())
    if bot and not is_supported_bot_type(bot.get("bot_type")):
        return None
    return bot


def get_bot_by_origin_rec(conn: sqlite3.Connection, origin_rec_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM bot_instances WHERE origin_rec_id=? ORDER BY started_ts DESC LIMIT 1", (origin_rec_id,))
    return _decode_bot_row(cur.fetchone())


def list_bot_instances(conn: sqlite3.Connection, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if status:
        cur = conn.execute("SELECT * FROM bot_instances WHERE status=? ORDER BY started_ts DESC LIMIT ?", (status, limit))
    else:
        cur = conn.execute("SELECT * FROM bot_instances ORDER BY started_ts DESC LIMIT ?", (limit,))
    return [bot for r in cur.fetchall() if (bot := _decode_bot_row(r)) and is_supported_bot_type(bot.get("bot_type"))]


def update_bot_state(conn: sqlite3.Connection, bot_id: str, patch: dict[str, Any], merge: bool = True) -> bool:
    cur = conn.execute("SELECT state_json FROM bot_instances WHERE bot_id=?", (bot_id,))
    row = cur.fetchone()
    if not row:
        return False
    state = _json_loads_or_default(row["state_json"], {})
    state = {**state, **patch} if merge else dict(patch)
    conn.execute("UPDATE bot_instances SET state_json=? WHERE bot_id=?", (json.dumps(state, ensure_ascii=False), bot_id))
    conn.commit()
    return True


def insert_trade(conn: sqlite3.Connection, trade: dict[str, Any]) -> str:
    """Insert a trade in a conflict-aware way.

    Returns:
      - "inserted" when a new trade row is written
      - "duplicate" when the same trade_id already exists with identical payload

    Raises:
      ValueError when the same trade_id already exists with different payload.
    """
    payload = (
        trade["trade_id"],
        trade["bot_id"],
        int(trade["ts"]),
        trade["symbol"],
        _require_finite_float("pnl", trade.get("pnl") or 0.0),
        _require_finite_float("fee", trade.get("fee") or 0.0, minimum=0.0),
        json.dumps(trade.get("meta") or {}, ensure_ascii=False),
    )
    cur = conn.execute(
        """SELECT bot_id, ts, symbol, pnl, fee, meta_json
           FROM trades WHERE trade_id=?""",
        (trade["trade_id"],),
    )
    row = cur.fetchone()
    if row:
        existing = (
            row["bot_id"],
            int(row["ts"]),
            row["symbol"],
            float(row["pnl"]),
            float(row["fee"]),
            row["meta_json"],
        )
        incoming = payload[1:]
        if existing == incoming:
            return "duplicate"
        raise ValueError(f"trade_id={trade['trade_id']} already exists with different payload")

    conn.execute(
        """INSERT INTO trades(trade_id, bot_id, ts, symbol, pnl, fee, meta_json)
           VALUES(?,?,?,?,?,?,?)""",
        payload,
    )
    conn.commit()
    return "inserted"


def get_trade_by_id(conn: sqlite3.Connection, trade_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,))
    r = cur.fetchone()
    if not r:
        return None
    return {
        "trade_id": r["trade_id"],
        "bot_id": r["bot_id"],
        "ts": int(r["ts"]),
        "symbol": r["symbol"],
        "pnl": _finite_float_or_default(r["pnl"], 0.0),
        "fee": _finite_float_or_default(r["fee"], 0.0),
        "meta": _json_loads_or_default(r["meta_json"], {}),
    }


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
            "pnl": _finite_float_or_default(r["pnl"], 0.0),
            "fee": _finite_float_or_default(r["fee"], 0.0),
            "meta": _json_loads_or_default(r["meta_json"], {}),
        })
    return out



def get_bot_trade_summary(conn: sqlite3.Connection, bot_id: str) -> dict[str, Any]:
    cur = conn.execute(
        """SELECT ts, pnl, fee
           FROM trades WHERE bot_id=?
           ORDER BY ts ASC, trade_id ASC""",
        (bot_id,),
    )
    trade_count = 0
    realized_pnl_gross = 0.0
    realized_fee = 0.0
    last_trade_ts: int | None = None
    for row in cur.fetchall():
        trade_count += 1
        realized_pnl_gross += _finite_float_or_default(row["pnl"], 0.0)
        realized_fee += _finite_float_or_default(row["fee"], 0.0)
        try:
            last_trade_ts = int(row["ts"])
        except Exception:
            pass
    realized_pnl_net = realized_pnl_gross - realized_fee
    return {
        "trade_count": trade_count,
        "realized_pnl_gross": realized_pnl_gross,
        "realized_fee": realized_fee,
        "realized_pnl_net": realized_pnl_net,
        "realized_pnl": realized_pnl_net,
        "last_trade_ts": last_trade_ts,
    }


def _recommended_row_passes_conf_filter(row: sqlite3.Row, min_conf: float, strict_min_conf: bool = False) -> bool:
    if str(row["status"] or "") != "recommended":
        return True
    conf = float(row["confidence"] or 0.0)
    if strict_min_conf:
        return conf >= float(min_conf)
    try:
        reasons = _json_loads_or_default(row["reasons_json"], {})
    except Exception:
        reasons = {}
    confidence_model = reasons.get("confidence_model") if isinstance(reasons, dict) else {}
    if not isinstance(confidence_model, dict):
        confidence_model = {}
    gate_applied = confidence_model.get("confidence_gate_applied")
    if gate_applied is None:
        source = str(confidence_model.get("source") or "")
        fitted = bool(confidence_model.get("fitted"))
        gate_applied = bool(fitted and source not in ("", "raw", "raw_proxy"))
    if not bool(gate_applied):
        return True
    return conf >= float(min_conf)


def get_recommendations(
    conn: sqlite3.Connection,
    venue: str | None,
    top_n: int,
    min_conf: float,
    statuses: list[str] | None = None,
    snapshot_ts: int | None = None,
    strict_min_conf: bool = False,
) -> list[dict[str, Any]]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if snapshot_ts is not None:
        q = f"""SELECT * FROM recommendations WHERE ts = ? AND {_supported_sql}"""
        params: list[Any] = [snapshot_ts, *_supported_params]
    else:
        # Use 24h window so executed/ignored/expired recs remain visible for audit
        q = f"""SELECT * FROM recommendations WHERE ts > ? AND {_supported_sql}"""
        params: list[Any] = [now_ts() - 86400, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    if statuses is not None:
        if not statuses:
            # Empty list → caller wants no statuses → return nothing
            return []
        placeholders = ",".join("?" for _ in statuses)
        q += f" AND status IN ({placeholders})"
        params.extend(statuses)
    q += " ORDER BY CASE status WHEN 'recommended' THEN 0 WHEN 'executed' THEN 1 WHEN 'ignored' THEN 2 WHEN 'blocked' THEN 3 WHEN 'no_trade' THEN 4 WHEN 'suppressed' THEN 5 ELSE 6 END, confidence DESC, score DESC, ts DESC"
    cur = conn.execute(q, params)
    rows = []
    for r in cur.fetchall():
        if not _recommended_row_passes_conf_filter(r, min_conf=min_conf, strict_min_conf=strict_min_conf):
            continue
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
            "params": _json_loads_or_default(r["params_json"], {}),
            "reasons": _json_loads_or_default(r["reasons_json"], {}),
            "blocks": _json_loads_or_default(r["blocks_json"], []),
            "status": r["status"],
            "ttl_sec": r["ttl_sec"],
            "model_version": r["model_version"],
            "features_ref_ts": r["features_ref_ts"],
        })
        if len(rows) >= int(top_n):
            break
    return rows

def get_recommendation_by_id(conn: sqlite3.Connection, rec_id: str) -> dict[str, Any] | None:
    cur = conn.execute("""SELECT * FROM recommendations WHERE rec_id=?""", (rec_id,))
    r = cur.fetchone()
    if not r or not is_supported_bot_type(r["bot_type"]):
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
        "params": _json_loads_or_default(r["params_json"], {}),
        "reasons": _json_loads_or_default(r["reasons_json"], {}),
        "blocks": _json_loads_or_default(r["blocks_json"], []),
        "status": r["status"],
        "ttl_sec": r["ttl_sec"],
        "model_version": r["model_version"],
        "features_ref_ts": r["features_ref_ts"],
    }


def update_recommendation_review(
    conn: sqlite3.Connection,
    rec_id: str,
    *,
    reasons: dict[str, Any],
    status: str | None = None,
) -> bool:
    cur = conn.execute("SELECT status FROM recommendations WHERE rec_id=?", (rec_id,))
    row = cur.fetchone()
    if not row:
        return False
    current = str(row["status"])
    new_status = current
    if status and status in OPERATOR_STATUSES:
        allowed = _ALLOWED_STATUS_TRANSITIONS.get(current, {current})
        if status in allowed:
            new_status = status
    conn.execute(
        "UPDATE recommendations SET reasons_json=?, status=? WHERE rec_id=?",
        (json.dumps(reasons, ensure_ascii=False), new_status, rec_id),
    )
    conn.commit()
    return True



def acquire_runtime_lock(conn: sqlite3.Connection, lock_key: str, owner: str, ttl_sec: int = 90) -> bool:
    """Cross-process best-effort leader lock backed by SQLite."""
    now = now_ts()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner, heartbeat_ts FROM runtime_locks WHERE lock_key=?",
            (lock_key,),
        ).fetchone()
        should_claim = False
        if row is None:
            should_claim = True
        else:
            row_owner = str(row["owner"] or "")
            heartbeat_ts = int(row["heartbeat_ts"] or 0)
            if row_owner == owner or (now - heartbeat_ts) > max(5, int(ttl_sec)):
                should_claim = True

        if should_claim:
            conn.execute(
                "INSERT OR REPLACE INTO runtime_locks(lock_key, owner, heartbeat_ts) VALUES(?,?,?)",
                (lock_key, owner, now),
            )
            conn.commit()
            return True
        conn.commit()
        return False
    except sqlite3.OperationalError:
        try:
            conn.rollback()
        except Exception:
            logger.debug("rollback error", exc_info=True)
        return False


def release_runtime_lock(conn: sqlite3.Connection, lock_key: str, owner: str) -> None:
    conn.execute(
        "DELETE FROM runtime_locks WHERE lock_key=? AND owner=?",
        (lock_key, owner),
    )
    conn.commit()

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
    return _json_loads_or_default(row["limits_json"], None) if row else None

def insert_sentiment_point(conn: sqlite3.Connection, scope: str, key: str, ts: int, sentiment: float, velocity: float, volume: int, sources: dict, tags: list[str]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        (scope, key, ts, float(sentiment), float(velocity), int(volume), json.dumps(sources, ensure_ascii=False), json.dumps(tags, ensure_ascii=False)),
    )
    conn.commit()


def insert_sentiment_points(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        [
            (
                row["scope"],
                row["key"],
                int(row["ts"]),
                float(row["sentiment"]),
                float(row.get("velocity") or 0.0),
                int(row.get("volume") or 0),
                json.dumps(row.get("sources") or {}, ensure_ascii=False),
                json.dumps(row.get("tags") or [], ensure_ascii=False),
            )
            for row in rows
        ],
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
            "sources": _json_loads_or_default(r["sources_json"], {}),
            "tags": _json_loads_or_default(r["tags_json"], []),
        })
    return out

def sum_daily_gross_pnl(conn: sqlite3.Connection, day_start_ts: int) -> float:
    cur = conn.execute("SELECT pnl FROM trades WHERE ts>=?", (day_start_ts,))
    return sum(_finite_float_or_default(row["pnl"], 0.0) for row in cur.fetchall())


def sum_daily_fees(conn: sqlite3.Connection, day_start_ts: int) -> float:
    cur = conn.execute("SELECT fee FROM trades WHERE ts>=?", (day_start_ts,))
    return sum(_finite_float_or_default(row["fee"], 0.0) for row in cur.fetchall())


def sum_daily_pnl(conn: sqlite3.Connection, day_start_ts: int) -> float:
    """Net daily PnL after fees.

    Risk limits must use net economics, not gross trade PnL.
    Ignore non-finite rows so one corrupted trade cannot poison the whole day's risk status.
    """
    cur = conn.execute("SELECT pnl, fee FROM trades WHERE ts>=?", (day_start_ts,))
    total = 0.0
    for row in cur.fetchall():
        total += _finite_float_or_default(row["pnl"], 0.0) - _finite_float_or_default(row["fee"], 0.0)
    return total



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
        "sources": _json_loads_or_default(r["sources_json"], {}),
        "tags": _json_loads_or_default(r["tags_json"], []),
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
            reasons = _json_loads_or_default(row["reasons_json"], {})
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
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if venue:
        cur = conn.execute(f"""SELECT MAX(ts) AS m FROM recommendations WHERE venue=? AND {_supported_sql}""", [venue, *_supported_params])
    else:
        cur = conn.execute(f"""SELECT MAX(ts) AS m FROM recommendations WHERE {_supported_sql}""", _supported_params)
    r = cur.fetchone()
    return int(r["m"]) if r and r["m"] is not None else None


def list_recent_reco_snapshot_ts(conn: sqlite3.Connection, venue: str | None = None, limit: int = 50) -> list[int]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    q = f"""SELECT DISTINCT ts FROM recommendations WHERE {_supported_sql}"""
    params: list[Any] = [*_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(max(1, int(limit)))
    cur = conn.execute(q, params)
    return [int(r["ts"]) for r in cur.fetchall() if r["ts"] is not None]


def get_recent_llm_review_candidates(
    conn: sqlite3.Connection,
    *,
    venue: str | None = None,
    recent_sec: int = 3600,
    limit: int = 60,
    snapshot_ts: int | None = None,
) -> list[dict[str, Any]]:
    cutoff_ts = max(0, now_ts() - max(60, int(recent_sec)))
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if snapshot_ts is not None:
        q = f"""SELECT * FROM recommendations
            WHERE ts = ? AND status='recommended' AND {_supported_sql}"""
        params: list[Any] = [int(snapshot_ts), *_supported_params]
    else:
        q = f"""SELECT * FROM recommendations
            WHERE ts >= ? AND status='recommended' AND {_supported_sql}"""
        params = [cutoff_ts, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    q += " ORDER BY ts DESC, confidence DESC, score DESC LIMIT ?"
    params.append(max(1, int(limit)))
    cur = conn.execute(q, params)
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        out.append({
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
            "params": _json_loads_or_default(r["params_json"], {}),
            "reasons": _json_loads_or_default(r["reasons_json"], {}),
            "blocks": _json_loads_or_default(r["blocks_json"], []),
            "status": r["status"],
            "ttl_sec": r["ttl_sec"],
            "model_version": r["model_version"],
            "features_ref_ts": r["features_ref_ts"],
        })
    return out




def get_llm_status_counts(
    conn: sqlite3.Connection,
    *,
    venue: str | None,
    min_conf: float,
    statuses: list[str] | None = None,
    snapshot_ts: int | None = None,
    strict_min_conf: bool = False,
) -> dict[str, int]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if snapshot_ts is not None:
        q = f"""SELECT status, confidence, reasons_json FROM recommendations WHERE ts = ? AND {_supported_sql}"""
        params: list[Any] = [snapshot_ts, *_supported_params]
    else:
        q = f"""SELECT status, confidence, reasons_json FROM recommendations WHERE ts > ? AND {_supported_sql}"""
        params = [now_ts() - 86400, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    if statuses is not None:
        if not statuses:
            return {"ok": 0, "pending": 0, "error": 0, "skipped": 0, "none": 0, "other": 0}
        placeholders = ",".join("?" for _ in statuses)
        q += f" AND status IN ({placeholders})"
        params.extend(statuses)

    counts = {"ok": 0, "pending": 0, "error": 0, "skipped": 0, "none": 0, "other": 0}
    cur = conn.execute(q, params)
    for row in cur.fetchall():
        if not _recommended_row_passes_conf_filter(row, min_conf=min_conf, strict_min_conf=strict_min_conf):
            continue
        review = _extract_llm_review_snapshot(row["reasons_json"])
        llm_status = str((review or {}).get("status") or "none").strip().lower()
        if llm_status not in counts:
            llm_status = "other"
        counts[llm_status] += 1
    return counts

def get_recommendation_status_counts(
    conn: sqlite3.Connection,
    venue: str | None = None,
    snapshot_ts: int | None = None,
) -> dict[str, int]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if snapshot_ts is not None:
        q = f"""SELECT status, COUNT(*) AS c FROM recommendations WHERE ts = ? AND {_supported_sql}"""
        params: list[Any] = [snapshot_ts, *_supported_params]
    else:
        q = f"""SELECT status, COUNT(*) AS c FROM recommendations WHERE ts > ? AND {_supported_sql}"""
        params = [now_ts() - 86400, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    q += " GROUP BY status"
    cur = conn.execute(q, params)
    counts = {k: 0 for k in ("recommended", "blocked", "no_trade", "suppressed", "expired", "executed", "ignored")}
    for row in cur.fetchall():
        counts[str(row["status"])] = int(row["c"] or 0)
    return counts


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
TERMINAL_RECOMMENDATION_STATUSES = {"executed", "ignored", "expired", "blocked", "no_trade", "suppressed"}
_ALLOWED_STATUS_TRANSITIONS = {
    # recommendation engine / async reviewers may still downgrade a recommended idea
    # before an operator explicitly acts on it.
    "recommended": {"recommended", "executed", "ignored", "blocked", "no_trade", "suppressed", "expired"},
    "executed": {"executed"},
    "ignored": {"ignored"},
    "expired": {"expired"},
    "blocked": {"blocked"},
    "no_trade": {"no_trade"},
    "suppressed": {"suppressed"},
}


def update_recommendation_status(
    conn: sqlite3.Connection, rec_id: str, status: str, operator: str | None = None
) -> bool:
    if status not in OPERATOR_STATUSES:
        return False
    cur = conn.execute("SELECT status FROM recommendations WHERE rec_id=?", (rec_id,))
    row = cur.fetchone()
    if not row:
        return False
    current = str(row["status"])
    allowed = _ALLOWED_STATUS_TRANSITIONS.get(current, {current})
    if status not in allowed:
        return False
    if status == current:
        return True
    cur = conn.execute(
        """UPDATE recommendations SET status=? WHERE rec_id=?""",
        (status, rec_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return False
    log_decision(conn, "STATUS_UPDATE", rec_id, operator, {"old_status": current, "new_status": status})
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

def _normalize_direction(direction: Any, fallback: str = "neutral") -> str:
    value = str(direction or fallback).strip().lower()
    return value if value in ("long", "short", "neutral") else fallback


def _parse_reasons_json(reasons_json: str | None) -> dict[str, Any]:
    try:
        reasons = _json_loads_or_default(reasons_json, {})
    except Exception:
        reasons = {}
    return reasons if isinstance(reasons, dict) else {}



def _extract_llm_review_snapshot(reasons_json: str | None) -> dict[str, Any] | None:
    reasons = _parse_reasons_json(reasons_json)
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else None
    if not isinstance(llm_review, dict):
        return None

    agree = llm_review.get("agree_with_engine")
    if isinstance(agree, str):
        agree = agree.strip().lower() in {"1", "true", "yes", "y"}
    elif not isinstance(agree, bool):
        agree = None

    confidence_raw = llm_review.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else None
    except Exception:
        confidence = None

    risk_flags_raw = llm_review.get("risk_flags")
    if isinstance(risk_flags_raw, list):
        risk_flags = [str(x) for x in risk_flags_raw[:8]]
    elif isinstance(risk_flags_raw, str) and risk_flags_raw.strip():
        risk_flags = [risk_flags_raw.strip()]
    else:
        risk_flags = []

    review_ts_raw = llm_review.get("review_ts")
    try:
        review_ts = int(review_ts_raw) if review_ts_raw is not None else None
    except Exception:
        review_ts = None

    cache_age_raw = llm_review.get("cache_age_sec")
    try:
        cache_age_sec = int(cache_age_raw) if cache_age_raw is not None else None
    except Exception:
        cache_age_sec = None

    return {
        "status": str(llm_review.get("status") or "unknown"),
        "provider": llm_review.get("provider"),
        "model": llm_review.get("model"),
        "mode": llm_review.get("mode"),
        "gate_decision": llm_review.get("gate_decision"),
        "agree_with_engine": agree,
        "confidence": confidence,
        "thesis_direction": _normalize_direction(llm_review.get("thesis_direction"), fallback="neutral"),
        "execution_direction": _normalize_direction(llm_review.get("execution_direction"), fallback="neutral"),
        "regime_view": str(llm_review.get("regime_view") or "unknown"),
        "summary": llm_review.get("summary"),
        "error": llm_review.get("error"),
        "risk_flags": risk_flags,
        "cached": bool(llm_review.get("cached")),
        "cache_age_sec": cache_age_sec,
        "source": llm_review.get("source"),
        "review_ts": review_ts,
    }



def _extract_outcome_directions(outcome_direction: Any, reco_direction: Any, reasons_json: str | None) -> dict[str, Any]:
    raw_direction = None
    execution_direction = None

    reasons = _parse_reasons_json(reasons_json)

    execution_constraints = reasons.get("execution_constraints") if isinstance(reasons.get("execution_constraints"), dict) else {}
    direction_agg = reasons.get("direction_agg") if isinstance(reasons.get("direction_agg"), dict) else {}

    raw_direction = (
        execution_constraints.get("raw_direction")
        or direction_agg.get("raw_direction")
        or direction_agg.get("direction_before_execution")
        or reco_direction
        or outcome_direction
    )
    execution_direction = (
        execution_constraints.get("executable_direction")
        or execution_constraints.get("execution_direction")
        or reco_direction
        or outcome_direction
    )

    raw_direction = _normalize_direction(raw_direction, fallback=_normalize_direction(outcome_direction))
    execution_direction = _normalize_direction(execution_direction, fallback=_normalize_direction(outcome_direction))

    if execution_direction == "neutral" and raw_direction == "short":
        neutral_source = "spot_short_neutralized"
    elif execution_direction == "neutral" and raw_direction == "neutral":
        neutral_source = "true_neutral"
    elif execution_direction == "neutral":
        neutral_source = "other_neutralized"
    else:
        neutral_source = None

    return {
        "raw_direction": raw_direction,
        "execution_direction": execution_direction,
        "neutral_source": neutral_source,
    }


def _accumulate_stat(bucket: dict[str, Any], success: Any, ret: Any) -> None:
    bucket["total"] += 1
    bucket["wins"] += int(success or 0)
    bucket["ret_sum"] += float(ret or 0.0)
    bucket["abs_ret_sum"] += abs(float(ret or 0.0))



def _materialize_stat_rows(grouped: dict[tuple[Any, ...], dict[str, Any]], key_names: list[str], *, sort_key) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, stat in grouped.items():
        total = int(stat["total"])
        wins = int(stat["wins"])
        row = {name: key[idx] for idx, name in enumerate(key_names)}
        row.update({
            "total": total,
            "wins": wins,
            "losses": max(0, total - wins),
            "win_rate": round(wins / total, 3) if total else 0.0,
            "avg_ret": round((float(stat["ret_sum"]) / total) * 100.0, 3) if total else 0.0,
            "avg_abs_ret": round((float(stat["abs_ret_sum"]) / total) * 100.0, 3) if total else 0.0,
        })
        rows.append(row)
    return sorted(rows, key=sort_key)



def get_outcomes_recent_enriched(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    _supported_sql, _supported_params = sql_in_clause("o.bot_type")
    cur = conn.execute(
        f"""SELECT o.rec_id, o.ts, o.venue, o.symbol, o.bot_type, o.direction,
                     o.horizon_sec, o.entry_close, o.exit_close, o.ret, o.success,
                     r.direction AS reco_direction, r.status AS reco_status,
                     r.score, r.confidence, r.expected_rr, r.reasons_json
              FROM reco_outcomes o
              LEFT JOIN recommendations r ON r.rec_id = o.rec_id
              WHERE {_supported_sql}
              ORDER BY o.ts DESC
              LIMIT ?""",
        [*_supported_params, int(limit)],
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        dirs = _extract_outcome_directions(row["direction"], row["reco_direction"], row["reasons_json"])
        llm_review = _extract_llm_review_snapshot(row["reasons_json"])
        out.append({
            "rec_id": row["rec_id"],
            "ts": int(row["ts"]),
            "venue": row["venue"],
            "symbol": row["symbol"],
            "bot_type": row["bot_type"],
            "direction": _normalize_direction(row["direction"]),
            "raw_direction": dirs["raw_direction"],
            "execution_direction": dirs["execution_direction"],
            "neutral_source": dirs["neutral_source"],
            "horizon_sec": int(row["horizon_sec"]),
            "entry_close": float(row["entry_close"]),
            "exit_close": float(row["exit_close"]),
            "ret": float(row["ret"]),
            "success": int(row["success"]),
            "reco_status": row["reco_status"],
            "score": float(row["score"]) if row["score"] is not None else None,
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
            "expected_rr": float(row["expected_rr"]) if row["expected_rr"] is not None else None,
            "llm_review": llm_review,
        })
    return out



def get_outcomes_stats(conn: sqlite3.Connection) -> dict:
    """Aggregate win-rate / return proxies and expose raw vs execution direction splits.

    Neutral execution can hide two very different realities:
    true neutral thesis and spot short neutralisation (raw short -> execution neutral).
    The UI needs both axes to avoid mixing them together.
    """
    _supported_sql, _supported_params = sql_in_clause("o.bot_type")
    cur = conn.execute(
        f"""SELECT o.rec_id, o.ts, o.venue, o.symbol, o.bot_type, o.direction,
                     o.ret, o.success,
                     r.direction AS reco_direction, r.reasons_json
              FROM reco_outcomes o
              LEFT JOIN recommendations r ON r.rec_id = o.rec_id
              WHERE {_supported_sql}
              ORDER BY o.ts DESC""",
        _supported_params,
    )

    summary_bucket = {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0}
    by_bot_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_symbol_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_raw_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_execution_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_pair_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_llm_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}

    true_neutral_total = 0
    spot_short_neutralized_total = 0
    llm_summary = {
        "present_total": 0,
        "ok_total": 0,
        "agree_total": 0,
        "disagree_total": 0,
        "error_total": 0,
        "skipped_total": 0,
        "veto_total": 0,
    }

    rows = cur.fetchall()
    for row in rows:
        dirs = _extract_outcome_directions(row["direction"], row["reco_direction"], row["reasons_json"])
        raw_direction = dirs["raw_direction"]
        execution_direction = dirs["execution_direction"]
        neutral_source = dirs["neutral_source"]
        success = int(row["success"] or 0)
        ret = float(row["ret"] or 0.0)
        llm_review = _extract_llm_review_snapshot(row["reasons_json"])

        if neutral_source == "true_neutral":
            true_neutral_total += 1
        elif neutral_source == "spot_short_neutralized":
            spot_short_neutralized_total += 1

        if llm_review:
            llm_summary["present_total"] += 1
            llm_status = str(llm_review.get("status") or "unknown")
            llm_agree = llm_review.get("agree_with_engine")
            llm_gate = str(llm_review.get("gate_decision") or "")
            if llm_status == "ok":
                llm_summary["ok_total"] += 1
                if llm_agree is True:
                    llm_summary["agree_total"] += 1
                elif llm_agree is False:
                    llm_summary["disagree_total"] += 1
            elif llm_status == "error":
                llm_summary["error_total"] += 1
            elif llm_status == "skipped":
                llm_summary["skipped_total"] += 1
            if llm_gate == "veto":
                llm_summary["veto_total"] += 1

            llm_bucket_key = (
                llm_status,
                _normalize_direction(llm_review.get("execution_direction"), fallback="neutral"),
                "agree" if llm_agree is True else "disagree" if llm_agree is False else "unknown",
                llm_gate or "pass",
            )
            stat = by_llm_bucket.setdefault(llm_bucket_key, {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0})
            _accumulate_stat(stat, success, ret)

        _accumulate_stat(summary_bucket, success, ret)

        for bucket, key in (
            (by_bot_bucket, (row["bot_type"], raw_direction, execution_direction)),
            (by_symbol_bucket, (row["symbol"], row["bot_type"], raw_direction, execution_direction)),
            (by_raw_bucket, (raw_direction,)),
            (by_execution_bucket, (execution_direction,)),
            (by_pair_bucket, (raw_direction, execution_direction, neutral_source or "")),
        ):
            stat = bucket.setdefault(key, {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0})
            _accumulate_stat(stat, success, ret)

    total = int(summary_bucket["total"])
    wins = int(summary_bucket["wins"])
    summary = {
        "total": total,
        "wins": wins,
        "losses": max(0, total - wins),
        "win_rate": round(wins / total, 3) if total else None,
        "avg_ret": round((float(summary_bucket["ret_sum"]) / total) * 100.0, 3) if total else 0.0,
        "avg_abs_ret": round((float(summary_bucket["abs_ret_sum"]) / total) * 100.0, 3) if total else 0.0,
        "true_neutral_total": int(true_neutral_total),
        "spot_short_neutralized_total": int(spot_short_neutralized_total),
    }

    by_bot = _materialize_stat_rows(
        by_bot_bucket,
        ["bot_type", "raw_direction", "execution_direction"],
        sort_key=lambda row: (-row["total"], row["bot_type"], row["raw_direction"], row["execution_direction"]),
    )
    by_symbol = _materialize_stat_rows(
        by_symbol_bucket,
        ["symbol", "bot_type", "raw_direction", "execution_direction"],
        sort_key=lambda row: (-row["total"], row["symbol"], row["bot_type"], row["raw_direction"], row["execution_direction"]),
    )
    by_raw_direction = _materialize_stat_rows(
        by_raw_bucket,
        ["raw_direction"],
        sort_key=lambda row: (-row["total"], row["raw_direction"]),
    )
    by_execution_direction = _materialize_stat_rows(
        by_execution_bucket,
        ["execution_direction"],
        sort_key=lambda row: (-row["total"], row["execution_direction"]),
    )
    direction_pairs = _materialize_stat_rows(
        by_pair_bucket,
        ["raw_direction", "execution_direction", "neutral_source"],
        sort_key=lambda row: (-row["total"], row["raw_direction"], row["execution_direction"], row.get("neutral_source") or ""),
    )
    llm_alignment = _materialize_stat_rows(
        by_llm_bucket,
        ["llm_status", "llm_execution_direction", "llm_alignment", "llm_gate_decision"],
        sort_key=lambda row: (-row["total"], row["llm_status"], row["llm_execution_direction"], row["llm_alignment"], row["llm_gate_decision"]),
    )

    return {
        "summary": summary,
        "llm_summary": llm_summary,
        "by_bot": by_bot,
        "by_symbol": by_symbol,
        "by_raw_direction": by_raw_direction,
        "by_execution_direction": by_execution_direction,
        "direction_pairs": direction_pairs,
        "llm_alignment": llm_alignment,
        "recent": get_outcomes_recent_enriched(conn, limit=120),
    }


# ── Symbol health ─────────────────────────────────────────────────────────────

def get_symbol_health(
    conn: sqlite3.Connection,
    symbols_spot: list[str],
    symbols_linear: list[str],
    stale_sec: int = 300,
    *,
    active_venues: list[str] | None = None,
) -> list[dict]:
    """
    Returns health status for each configured symbol on active venues:
      last_candle_ts: newest 1m candle timestamp
      last_ticker_ts: newest ticker timestamp
      age_sec:        seconds since last candle
      status:         'ok' | 'stale' | 'missing' | 'disabled'
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
    error_counts: dict[tuple[str, str], int] = {}
    for row in cur.fetchall():
        try:
            d = _json_loads_or_default(row["details_json"], {})
            venue = str(d.get("venue") or "")
            sym = str(d.get("symbol") or "UNKNOWN")
            key = (venue, sym)
            error_counts[key] = error_counts.get(key, 0) + 1
        except Exception:
            pass

    # disabled symbols in last 24h
    cur = conn.execute(
        """SELECT details_json FROM decision_log
           WHERE action='SYMBOL_DISABLED' AND ts >= ?""",
        (now - 86400,),
    )
    disabled_syms: set[tuple[str, str]] = set()
    for row in cur.fetchall():
        try:
            d = _json_loads_or_default(row["details_json"], {})
            disabled_syms.add((str(d.get("venue") or ""), str(d.get("symbol") or "")))
        except Exception:
            logger.debug("health: error_counts parse error", exc_info=True)

    # stale skip counts per symbol in last hour
    cur = conn.execute(
        """SELECT details_json FROM decision_log
           WHERE action='STALE_DATA_SKIP' AND ts >= ?""",
        (now - 3600,),
    )
    stale_counts: dict[tuple[str, str], int] = {}
    for row in cur.fetchall():
        try:
            d = _json_loads_or_default(row["details_json"], {})
            venue = str(d.get("venue") or "")
            sym = str(d.get("symbol") or "UNKNOWN")
            key = (venue, sym)
            stale_counts[key] = stale_counts.get(key, 0) + 1
        except Exception:
            logger.debug("health: disabled_syms parse error", exc_info=True)

    active_set = {str(v or "").strip().lower() for v in (active_venues or ["spot", "linear"])}
    venue_symbols: list[tuple[str, list[str]]] = []
    if "spot" in active_set:
        venue_symbols.append(("spot", symbols_spot))
    if "linear" in active_set:
        venue_symbols.append(("linear", symbols_linear))

    for venue, symbols in venue_symbols:
        for sym in symbols:
            # last 1m candle
            cur2 = conn.execute(
                """SELECT MAX(ts) as m FROM ohlcv WHERE venue=? AND symbol=? AND tf_sec=60""",
                (venue, sym),
            )
            r = cur2.fetchone()
            last_ts = int(r["m"]) if r and r["m"] else None
            cur3 = conn.execute(
                """SELECT MAX(ts) as m FROM ticker_snap WHERE venue=? AND symbol=?""",
                (venue, sym),
            )
            rt = cur3.fetchone()
            last_ticker_ts = int(rt["m"]) if rt and rt["m"] else None
            age_sec = (now - last_ts) if last_ts else None
            is_disabled = (venue, sym) in disabled_syms

            if is_disabled and (last_ts is None or age_sec is None or age_sec > stale_sec):
                status = "disabled"
            elif last_ts is None:
                status = "missing"
            elif age_sec > stale_sec:
                status = "stale"
            else:
                status = "ok"

            result.append({
                "venue":           venue,
                "symbol":          sym,
                "last_candle_ts":  last_ts,
                "last_ticker_ts":  last_ticker_ts,
                "age_sec":         age_sec,
                "status":          status,
                "error_count_10m": error_counts.get((venue, sym), 0),
                "stale_skips_1h":  stale_counts.get((venue, sym), 0),
                "disabled":        is_disabled,
            })

    status_rank = {"disabled": 0, "missing": 1, "stale": 2, "ok": 3}
    return sorted(result, key=lambda x: (status_rank.get(str(x.get("status") or ""), 9), x["symbol"]))

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

    # ohlcv: prune by timeframe, not with one flat horizon.
    # Recommender requires up to 80 daily candles for the 1d vote. A single 30d cutoff
    # silently kills the 1d branch after the system has been running for a month, because
    # tf=86400 can never accumulate enough history again. Keep short TFs compact, but
    # retain enough slow-TF history for the actual inference contract.
    now = now_ts()
    cutoff_ohlcv_1m = now - 30 * 86400
    cutoff_ohlcv_15_30m = now - 90 * 86400
    cutoff_ohlcv_1h_4h = now - 180 * 86400
    cutoff_ohlcv_1d = now - 400 * 86400
    cur = conn.execute(
        """DELETE FROM ohlcv
           WHERE (tf_sec = 60 AND ts < ?)
              OR (tf_sec IN (900, 1800) AND ts < ?)
              OR (tf_sec IN (3600, 14400) AND ts < ?)
              OR (tf_sec >= 86400 AND ts < ?)""",
        (cutoff_ohlcv_1m, cutoff_ohlcv_15_30m, cutoff_ohlcv_1h_4h, cutoff_ohlcv_1d),
    )
    deleted["ohlcv"] = cur.rowcount

    # ticker_snap: keep 2 days (only latest snapshot is used at inference time)
    cur = conn.execute("DELETE FROM ticker_snap WHERE ts < ?", (now_ts() - 2 * 86400,))
    deleted["ticker_snap"] = cur.rowcount

    # funding_rate: keep 7 days (only current value used; history not queried)
    cur = conn.execute("DELETE FROM funding_rate WHERE ts < ?", (cutoff,))
    deleted["funding_rate"] = cur.rowcount

    # runtime_locks: drop stale leader locks so dead workers do not block takeover
    cur = conn.execute("DELETE FROM runtime_locks WHERE heartbeat_ts < ?", (now_ts() - 2 * 86400,))
    deleted["runtime_locks"] = cur.rowcount

    # open_interest: keep 7 days (oi_trend uses last 48 1h candles = 2 days)
    cur = conn.execute("DELETE FROM open_interest WHERE ts < ?", (cutoff,))
    deleted["open_interest"] = cur.rowcount

    conn.commit()
    return deleted



def count_visible_recommendations(
    conn: sqlite3.Connection,
    venue: str | None,
    min_conf: float,
    snapshot_ts: int | None = None,
    strict_min_conf: bool = False,
) -> int:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if snapshot_ts is not None:
        q = f"""SELECT status, confidence, reasons_json FROM recommendations WHERE ts = ? AND {_supported_sql} AND status='recommended'"""
        params: list[Any] = [snapshot_ts, *_supported_params]
    else:
        q = f"""SELECT status, confidence, reasons_json FROM recommendations WHERE ts > ? AND {_supported_sql} AND status='recommended'"""
        params = [now_ts() - 86400, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    cur = conn.execute(q, params)
    count = 0
    for r in cur.fetchall():
        if _recommended_row_passes_conf_filter(r, min_conf=min_conf, strict_min_conf=strict_min_conf):
            count += 1
    return count
