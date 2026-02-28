from __future__ import annotations

from . import db

HORIZON_SEC_DEFAULT = 30 * 60  # 30 minutes

def _get_close_at_or_after(conn, venue: str, symbol: str, ts: int) -> float | None:
    cur = conn.execute(
        """SELECT close FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts>=?
           ORDER BY ts ASC LIMIT 1""",
        (venue, symbol, ts),
    )
    r = cur.fetchone()
    return float(r["close"]) if r else None

def compute_outcomes_once(conn, horizon_sec: int = HORIZON_SEC_DEFAULT, max_to_process: int = 300) -> int:
    cur = conn.execute(
        """SELECT rec_id, ts, venue, symbol, bot_type, direction
           FROM recommendations
           WHERE ts <= ?
           ORDER BY ts DESC LIMIT ?""",
        (db.now_ts() - horizon_sec, max_to_process),
    )
    rows = cur.fetchall()
    done = 0
    for r in rows:
        rec_id = r["rec_id"]
        if db.outcome_exists(conn, rec_id):
            continue
        direction = r["direction"]
        if direction not in ("long","short"):
            continue

        venue = r["venue"]
        symbol = r["symbol"]
        ts0 = int(r["ts"])
        entry = _get_close_at_or_after(conn, venue, symbol, ts0)
        exitp = _get_close_at_or_after(conn, venue, symbol, ts0 + horizon_sec)
        if entry is None or exitp is None or entry == 0:
            continue

        ret = (exitp - entry) / entry
        if direction == "short":
            ret = -ret
        success = 1 if ret > 0 else 0

        db.insert_outcome(conn, {
            "rec_id": rec_id,
            "ts": ts0,
            "venue": venue,
            "symbol": symbol,
            "bot_type": r["bot_type"],
            "direction": direction,
            "horizon_sec": horizon_sec,
            "entry_close": float(entry),
            "exit_close": float(exitp),
            "ret": float(ret),
            "success": int(success),
        })
        done += 1
    return done
