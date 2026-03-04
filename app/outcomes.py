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

GRID_BOTS        = {"spot_grid", "futures_grid"}
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
    """Returns (min_low, max_high) over the horizon window using 1m candles."""
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
    conn, horizon_sec: int = HORIZON_SEC_DEFAULT, max_to_process: int = 500
) -> int:
    """Compute and store outcomes for recommendations whose horizon has passed.

    BUG FIX: original query fetched oldest N recs regardless of whether they already
    had outcomes, so once the table grew beyond max_to_process rows the function would
    scan the same already-processed records every cycle and never reach new ones.

    Fix: LEFT JOIN reco_outcomes and filter WHERE o.rec_id IS NULL — only unprocessed
    recs are fetched. max_to_process reduced to 500 (was 2000) because the JOIN is
    efficient and we no longer waste slots on already-processed records.
    """
    # Use minimum per-bot horizon so we don't skip any bot type prematurely.
    min_horizon = min(BOT_HORIZONS.values())  # 1h (martingale)

    # KEY FIX: exclude already-processed rec_ids at the SQL level via LEFT JOIN.
    # Without this, once the recommendations table has > max_to_process rows the
    # oldest rows (all already processed) fill the entire result set and nothing new
    # ever gets processed — outcomes freeze permanently.
    cur = conn.execute(
        """SELECT r.rec_id, r.ts, r.venue, r.symbol, r.bot_type, r.direction
           FROM recommendations r
           LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
           WHERE r.ts <= ? AND o.rec_id IS NULL
           AND r.status NOT IN ('blocked', 'no_trade', 'suppressed')
           ORDER BY r.ts ASC LIMIT ?""",
        (db.now_ts() - min_horizon, max_to_process),
    )
    rows = cur.fetchall()
    done = 0

    for r in rows:
        rec_id    = r["rec_id"]
        bot_type  = r["bot_type"]
        venue     = r["venue"]
        symbol    = r["symbol"]
        direction = r["direction"]
        ts0       = int(r["ts"])

        # Use per-bot-type horizon; fall back to global horizon_sec
        effective_horizon = BOT_HORIZONS.get(bot_type, horizon_sec)
        # Skip if this specific bot's horizon hasn't elapsed yet
        if db.now_ts() < ts0 + effective_horizon:
            continue
        ts_exit = ts0 + effective_horizon

        entry = _get_close_at_or_after(conn, venue, symbol, ts0)
        if entry is None or entry == 0:
            continue

        price_ret: float   # actual price return (exit-entry)/entry — always stored as-is
        success:   int
        exitp:     float

        # ── Grid bots: success = price stayed inside recommended range ──────
        if bot_type in GRID_BOTS:
            params = _get_rec_params(conn, rec_id)
            lo = params.get("price_range_lower") if params else None
            hi = params.get("price_range_upper") if params else None

            ep = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp     = ep
            price_ret = (exitp - entry) / entry

            if lo and hi and lo > 0 and hi > lo:
                price_window = _get_price_range_in_window(conn, venue, symbol, ts0, ts_exit)
                if price_window:
                    min_p, max_p = price_window
                    range_breach    = (min_p < lo * 0.995) or (max_p > hi * 1.005)
                    in_range_at_exit = lo <= exitp <= hi
                    success = 1 if (in_range_at_exit and not range_breach) else 0
                else:
                    success = 1 if lo <= exitp <= hi else 0
            else:
                # No range in params — flat/small return is grid-friendly
                success = 1 if abs(price_ret) < 0.015 else 0

        # ── Directional bots: success = price moved in direction ─────────────
        elif bot_type in DIRECTIONAL_BOTS:
            ep = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp     = ep
            price_ret = (exitp - entry) / entry  # actual price return — stored unchanged

            if bot_type == "futures_combo" or direction == "hedge":
                # Both legs capture volatility; flat market = loss
                success = 1 if abs(price_ret) > 0.008 else 0
            elif direction not in ("long", "short"):
                continue
            else:
                # BUG FIX: original code mutated `ret = -ret` for short before storing,
                # so avg_ret in stats showed sign-flipped values instead of actual returns.
                # Now: direction_ret is used only for success determination; price_ret is stored.
                direction_ret = -price_ret if direction == "short" else price_ret
                success = 1 if direction_ret > 0 else 0

        else:
            continue

        db.insert_outcome(conn, {
            "rec_id":      rec_id,
            "ts":          ts0,
            "venue":       venue,
            "symbol":      symbol,
            "bot_type":    bot_type,
            "direction":   direction,
            "horizon_sec": effective_horizon,
            "entry_close": float(entry),
            "exit_close":  float(exitp),
            "ret":         float(price_ret),  # always actual price return
            "success":     int(success),
        })
        done += 1

    return done
