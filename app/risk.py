from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone

try:
    # Python 3.9+ (PEP 615). Requires system tzdata or the "tzdata" pip package.
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore
from dataclasses import dataclass
from typing import Any

from . import db

@dataclass
class RiskStatus:
    limits: dict[str, Any]
    active_bots: int
    daily_pnl: float
    daily_dd: float
    cooldown_active: bool
    symbol_bot_counts: dict[str, int]


def _limit_int(limits: dict[str, Any], key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(limits.get(key, default))
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return int(value)


def _limit_float(limits: dict[str, Any], key: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(limits.get(key, default))
    except Exception:
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return float(value)


def _normalize_risk_limits(active: Any, fallback_limits: dict[str, Any]) -> dict[str, Any]:
    fallback = dict(fallback_limits or {})
    if not isinstance(active, dict):
        return fallback
    merged = dict(fallback)
    merged.update(active)
    merged["max_concurrent_bots"] = _limit_int(merged, "max_concurrent_bots", int(fallback.get("max_concurrent_bots", 4) or 4), minimum=1, maximum=100000)
    merged["max_daily_dd_usdt"] = _limit_float(merged, "max_daily_dd_usdt", float(fallback.get("max_daily_dd_usdt", 200.0) or 200.0), minimum=0.0, maximum=1e12)
    merged["cooldown_after_loss_min"] = _limit_int(merged, "cooldown_after_loss_min", int(fallback.get("cooldown_after_loss_min", 30) or 30), minimum=0, maximum=7 * 24 * 60)
    merged["max_symbol_bots"] = _limit_int(merged, "max_symbol_bots", int(fallback.get("max_symbol_bots", 1) or 1), minimum=1, maximum=100000)
    return merged

def day_start_ts_utc() -> int:
    """Day boundary used for daily PnL/DD limits.

    Default is UTC midnight, but can be overridden via env var RISK_DAY_TZ
    (IANA timezone name, e.g. "UTC", "America/Chicago").
    """
    tz_name = (os.getenv("RISK_DAY_TZ", "UTC") or "UTC").strip()

    # Be robust on minimal systems (or Windows) where zoneinfo has no tz database.
    # If tz cannot be resolved, fall back to fixed UTC instead of crashing.
    tz = timezone.utc
    if tz_name.upper() in {"UTC", "Z", "ETC/UTC", "ETC/GMT", "GMT"}:
        tz = timezone.utc
    elif ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())

def get_risk_limits(conn, fallback_limits: dict[str, Any]) -> dict[str, Any]:
    active = db.get_active_risk_limits(conn)
    return _normalize_risk_limits(active, fallback_limits)

def compute_risk_status(conn, limits: dict[str, Any]) -> RiskStatus:
    active_bots = db.get_active_bots(conn)
    n_active = len(active_bots)

    start = day_start_ts_utc()
    daily_pnl = db.sum_daily_pnl(conn, start)

    # True realised intraday drawdown = peak-to-trough drop of cumulative net PnL,
    # not merely the negative value of current day net PnL. Otherwise a sequence like
    # +300, -250 leaves daily_pnl positive while hiding a $250 drawdown.
    cur = conn.execute(
        """SELECT ts, (pnl - fee) AS net_pnl
           FROM trades WHERE ts >= ? ORDER BY ts ASC, trade_id ASC""",
        (start,),
    )
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in cur.fetchall():
        try:
            net_pnl = float(row["net_pnl"] or 0.0)
        except Exception:
            net_pnl = 0.0
        if not math.isfinite(net_pnl):
            net_pnl = 0.0
        cumulative += net_pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    daily_dd = float(max_dd)

    # cooldown: use the latest realised loss from trades first; fall back to explicit LOSS log entry.
    # The previous implementation only looked for action='LOSS', but no code path emits that
    # action, so cooldown_after_loss_min was effectively dead and never blocked candidates.
    limits = _normalize_risk_limits(limits, limits)
    cooldown_min = _limit_int(limits, "cooldown_after_loss_min", 0, minimum=0, maximum=7 * 24 * 60)
    cooldown_active = False
    if cooldown_min > 0:
        last_loss_ts = None

        cur = conn.execute(
            """SELECT ts FROM trades WHERE (pnl - fee) < 0 ORDER BY ts DESC LIMIT 1"""
        )
        row = cur.fetchone()
        if row:
            last_loss_ts = int(row["ts"])

        cur = conn.execute(
            """SELECT ts FROM decision_log WHERE action='LOSS' ORDER BY ts DESC LIMIT 1"""
        )
        row = cur.fetchone()
        if row:
            ts_loss = int(row["ts"])
            last_loss_ts = max(last_loss_ts or 0, ts_loss)

        if last_loss_ts:
            cooldown_active = (db.now_ts() - last_loss_ts) < cooldown_min * 60

    symbol_counts: dict[str, int] = {}
    for b in active_bots:
        key = f"{b['venue']}:{b['symbol']}"
        symbol_counts[key] = symbol_counts.get(key, 0) + 1

    return RiskStatus(
        limits=limits,
        active_bots=n_active,
        daily_pnl=daily_pnl,
        daily_dd=daily_dd,
        cooldown_active=cooldown_active,
        symbol_bot_counts=symbol_counts,
    )

def gate_candidate(conn, venue: str, symbol: str, limits: dict[str, Any], cached_status=None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    # Accept pre-computed risk status to avoid re-querying DB per (symbol, bot_type)
    rs = cached_status if cached_status is not None else compute_risk_status(conn, limits)

    limits = _normalize_risk_limits(limits, limits)
    max_conc = _limit_int(limits, "max_concurrent_bots", 999999, minimum=1, maximum=100000)
    if rs.active_bots >= max_conc:
        blocks.append({"code":"MAX_CONCURRENT_BOTS", "msg": f"active_bots={rs.active_bots} >= limit={max_conc}"})

    max_dd = _limit_float(limits, "max_daily_dd_usdt", 1e18, minimum=0.0, maximum=1e18)
    if rs.daily_dd >= max_dd:
        blocks.append({"code":"MAX_DD_DAY", "msg": f"daily_dd={rs.daily_dd:.2f} >= limit={max_dd:.2f}"})

    if rs.cooldown_active:
        blocks.append({"code":"COOLDOWN_ACTIVE", "msg":"cooldown after losses is active"})

    max_symbol_bots = _limit_int(limits, "max_symbol_bots", 999999, minimum=1, maximum=100000)
    active_for_symbol = rs.symbol_bot_counts.get(f"{venue}:{symbol}", 0)
    if active_for_symbol >= max_symbol_bots:
        blocks.append({"code":"MAX_SYMBOL_BOTS", "msg": f"{venue}:{symbol} active={active_for_symbol} >= limit={max_symbol_bots}"})

    return blocks
