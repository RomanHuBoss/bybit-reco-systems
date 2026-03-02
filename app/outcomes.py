from __future__ import annotations

from . import db

# Per-bot-type outcome horizons
# Grid bots live hours/days — 30m is meaningless for them
# DCA accumulates over a full day
# Martingale resolves within an hour in most cases
BOT_HORIZONS: dict[str, int] = {
    "spot_grid":          4 * 3600,   # 4h
    "futures_grid":       4 * 3600,   # 4h
    "dca_bot":           24 * 3600,   # 24h
    "futures_martingale": 1 * 3600,   # 1h
    "futures_combo":      2 * 3600,   # 2h
}
HORIZON_SEC_DEFAULT = 30 * 60  # fallback only

# Grid bots: success = price stayed inside the recommended range
# Directional bots (dca, martingale): success = price moved in direction
GRID_BOTS = {"spot_grid", "futures_grid"}
DIRECTIONAL_BOTS = {"dca_bot", "futures_martingale", "futures_combo"}


def _get_close_at_or_after(conn, venue: str, symbol: str, ts: int) -> float | None:
    cur = conn.execute(
        """SELECT close FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts>=?
           ORDER BY ts ASC LIMIT 1""",
        (venue, symbol, ts),
    )
    r = cur.fetchone()
    return float(r["close"]) if r else None


def _get_price_range_in_window(
    conn, venue: str, symbol: str, ts_start: int, ts_end: int
) -> tuple[float, float] | None:
    """Returns (min_close, max_close) over the horizon window."""
    cur = conn.execute(
        """SELECT MIN(low) as lo, MAX(high) as hi FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60
           AND ts>=? AND ts<=?""",
        (venue, symbol, ts_start, ts_end),
    )
    r = cur.fetchone()
    if not r or r["lo"] is None:
        return None
    return float(r["lo"]), float(r["hi"])


def _get_rec_params(conn, rec_id: str) -> dict | None:
    """Fetch params_json for a recommendation (contains grid range)."""
    cur = conn.execute(
        "SELECT params_json FROM recommendations WHERE rec_id=?", (rec_id,)
    )
    r = cur.fetchone()
    if not r:
        return None
    import json
    try:
        return json.loads(r["params_json"])
    except Exception:
        return None


def compute_outcomes_once(
    conn, horizon_sec: int = HORIZON_SEC_DEFAULT, max_to_process: int = 2000
) -> int:
    """horizon_sec is used as fallback only — BOT_HORIZONS takes precedence per bot_type."""
    # Use minimum per-bot horizon as SQL filter — avoids re-processing recs
    # whose effective horizon hasn't passed yet. min=1h (martingale), max=24h (DCA).
    min_horizon = min(BOT_HORIZONS.values())  # 1h
    cur = conn.execute(
        """SELECT rec_id, ts, venue, symbol, bot_type, direction
           FROM recommendations
           WHERE ts <= ?
           ORDER BY ts ASC LIMIT ?""",  # ASC: process oldest first — most likely past horizon
        (db.now_ts() - min_horizon, max_to_process),
    )
    rows = cur.fetchall()
    done = 0

    for r in rows:
        rec_id  = r["rec_id"]
        if db.outcome_exists(conn, rec_id):
            continue

        bot_type  = r["bot_type"]
        venue     = r["venue"]
        symbol    = r["symbol"]
        direction = r["direction"]
        ts0       = int(r["ts"])
        # Use per-bot-type horizon; fall back to global horizon_sec
        effective_horizon = BOT_HORIZONS.get(bot_type, horizon_sec)
        # Early skip: effective horizon hasn't passed yet
        if db.now_ts() < ts0 + effective_horizon:
            continue
        ts_exit   = ts0 + effective_horizon

        entry = _get_close_at_or_after(conn, venue, symbol, ts0)
        if entry is None or entry == 0:
            continue

        # ── Grid bots: success = price stayed inside recommended range ──────
        if bot_type in GRID_BOTS:
            params = _get_rec_params(conn, rec_id)
            lo = params.get("price_range_lower") if params else None
            hi = params.get("price_range_upper") if params else None

            exitp = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if exitp is None:
                continue

            ret = (exitp - entry) / entry  # raw return for reference

            if lo and hi and lo > 0 and hi > lo:
                # Success: exit price is still inside the grid range
                # Also check that price didn't spike far outside during the window
                price_window = _get_price_range_in_window(conn, venue, symbol, ts0, ts_exit)
                if price_window:
                    min_p, max_p = price_window
                    # Penalise if price left the range at any point (wick outside)
                    range_breach = (min_p < lo * 0.995) or (max_p > hi * 1.005)
                    in_range_at_exit = lo <= exitp <= hi
                    success = 1 if (in_range_at_exit and not range_breach) else 0
                else:
                    success = 1 if lo <= exitp <= hi else 0
            else:
                # No range data in params — fall back to flat/small return as proxy
                # Grid makes money when price oscillates, not trends strongly
                success = 1 if abs(ret) < 0.015 else 0  # < 1.5% net move = grid-friendly

        # ── Directional bots: success = price moved in direction ─────────────
        elif bot_type in DIRECTIONAL_BOTS:
            exitp = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if exitp is None:
                continue

            ret = (exitp - entry) / entry

            if bot_type == "futures_combo" or direction == "hedge":
                # Combo/hedge: success = significant price movement in either direction
                # (both legs capture volatility; flat market = loss)
                success = 1 if abs(ret) > 0.008 else 0  # >0.8% move = hedge profitable
            elif direction not in ("long", "short"):
                continue
            else:
                if direction == "short":
                    ret = -ret
                success = 1 if ret > 0 else 0

        else:
            continue

        db.insert_outcome(conn, {
            "rec_id":       rec_id,
            "ts":           ts0,
            "venue":        venue,
            "symbol":       symbol,
            "bot_type":     bot_type,
            "direction":    direction,
            "horizon_sec":  effective_horizon,
            "entry_close":  float(entry),
            "exit_close":   float(exitp),   # exitp already fetched above — no extra query
            "ret":          float(ret),
            "success":      int(success),
        })
        done += 1

    return done
