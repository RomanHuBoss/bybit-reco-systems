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


def _tp_sl_success(
    conn,
    venue: str,
    symbol: str,
    entry: float,
    direction: str,
    ts_start: int,
    ts_end: int,
    atr_1h: float,
    total_cost_bps: float = 15.0,
) -> int | None:
    """Determine martingale success by TP/SL hit using 1m candles.

    TP distance = max(cost_floor * 1.5, 0.30 * atr_1h)
    SL distance = 2.0 * TP distance  (1:2 risk-reward)

    Returns 1 (TP hit first), 0 (SL hit first or neither hit), None if no data.

    Why this matters vs simple direction_ret > 0:
    - A tiny 0.01% move 'in direction' counted as success before, even losing after fees.
    - Now we need a meaningful move that covers costs and delivers edge.
    - Checks candle-by-candle so the order of TP/SL hit matters.
    """
    cost_floor = total_cost_bps / 10_000
    tp_pct = max(cost_floor * 1.5, 0.30 * atr_1h)
    sl_pct = 2.0 * tp_pct

    if direction == "long":
        tp_price = entry * (1.0 + tp_pct)
        sl_price = entry * (1.0 - sl_pct)
    else:  # short
        tp_price = entry * (1.0 - tp_pct)
        sl_price = entry * (1.0 + sl_pct)

    # Walk 1m candles in order; check which level is touched first
    cur = conn.execute(
        """SELECT high, low FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60
           AND ts>=? AND ts<=?
           ORDER BY ts ASC""",
        (venue, symbol, ts_start, ts_end),
    )
    rows = cur.fetchall()
    if not rows:
        return None

    for row in rows:
        hi = float(row["high"])
        lo = float(row["low"])
        if direction == "long":
            if hi >= tp_price and lo <= sl_price:
                # Both touched in same candle — use mid-point heuristic (ambiguous)
                return 1 if (hi - tp_price) >= (sl_price - lo) else 0
            if hi >= tp_price:
                return 1
            if lo <= sl_price:
                return 0
        else:  # short
            if lo <= tp_price and hi >= sl_price:
                return 1 if (tp_price - lo) >= (hi - sl_price) else 0
            if lo <= tp_price:
                return 1
            if hi >= sl_price:
                return 0

    # Neither level hit in the window — not a success
    return 0


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

        effective_horizon = BOT_HORIZONS.get(bot_type, horizon_sec)
        if db.now_ts() < ts0 + effective_horizon:
            continue
        ts_exit = ts0 + effective_horizon

        entry = _get_close_at_or_after(conn, venue, symbol, ts0)
        if entry is None or entry == 0:
            continue

        price_ret: float
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
                    range_breach     = (min_p < lo * 0.995) or (max_p > hi * 1.005)
                    in_range_at_exit = lo <= exitp <= hi
                    success = 1 if (in_range_at_exit and not range_breach) else 0
                else:
                    success = 1 if lo <= exitp <= hi else 0
            else:
                success = 1 if abs(price_ret) < 0.015 else 0

        # ── Directional bots ────────────────────────────────────────────────
        elif bot_type in DIRECTIONAL_BOTS:
            params = _get_rec_params(conn, rec_id)

            ep = _get_close_at_or_after(conn, venue, symbol, ts_exit)
            if ep is None:
                continue
            exitp     = ep
            price_ret = (exitp - entry) / entry

            if bot_type == "futures_combo" or direction == "hedge":
                # ATR-relative threshold for volatility-scaled success.
                atr_thresh = 0.010
                if params:
                    vol = (params.get("trade_plan") or {}).get("volatility") or {}
                    atr_1h = float(vol.get("atr_pct_1h") or vol.get("atr_pct_used") or 0.0)
                    if atr_1h > 0:
                        atr_thresh = max(0.010, atr_1h * 0.6)
                success = 1 if abs(price_ret) > atr_thresh else 0

            elif bot_type == "futures_martingale" and direction in ("long", "short"):
                # TP/SL success: use 1m candles to check whether TP was hit before SL.
                # This is far more meaningful than "price moved 0.01% in direction":
                #   - TP distance = max(1.5x cost floor, 0.30x 1h ATR)
                #   - SL distance = 2x TP  (1:2 risk-reward typical for martingale)
                # Falls back to simple direction check if 1m data unavailable.
                vol = {}
                total_cost_bps = 15.0
                if params:
                    tp = (params.get("trade_plan") or {}) if params else {}
                    vol = tp.get("volatility") or {}
                    cm  = tp.get("cost_model") or (params or {}).get("cost_model") or {}
                    total_cost_bps = float(cm.get("total_cost_bps") or 15.0)
                atr_1h = float(vol.get("atr_pct_1h") or vol.get("atr_pct_used") or 0.02)

                tp_sl = _tp_sl_success(
                    conn, venue, symbol, entry, direction,
                    ts0, ts_exit, atr_1h, total_cost_bps,
                )
                if tp_sl is not None:
                    success = tp_sl
                else:
                    # No 1m data — fall back to cost-adjusted direction check
                    direction_ret = -price_ret if direction == "short" else price_ret
                    cost_floor = total_cost_bps / 10_000
                    success = 1 if direction_ret > cost_floor * 1.5 else 0

            elif bot_type == "dca_bot" and direction in ("long", "short"):
                # DCA success: price moved enough to cover costs and deliver minimum edge.
                # Simple "close > entry" counted as success even for ±0.01% moves,
                # which don't reflect DCA mechanics (averaging in, rebound after drawdown).
                # Minimum threshold: 1.5x total costs OR 0.3% absolute, whichever is larger.
                total_cost_bps = 15.0
                if params:
                    tp = (params.get("trade_plan") or {})
                    cm = tp.get("cost_model") or params.get("cost_model") or {}
                    total_cost_bps = float(cm.get("total_cost_bps") or 15.0)
                cost_floor = total_cost_bps / 10_000
                min_edge   = max(cost_floor * 1.5, 0.003)   # 1.5x costs OR 0.3% min
                direction_ret = -price_ret if direction == "short" else price_ret
                success = 1 if direction_ret > min_edge else 0

            elif direction not in ("long", "short"):
                continue
            else:
                # Other directional bots — legacy cost-adjusted direction check
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
