from __future__ import annotations

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
    return active if active else fallback_limits

def compute_risk_status(conn, limits: dict[str, Any]) -> RiskStatus:
    active_bots = db.get_active_bots(conn)
    n_active = len(active_bots)

    start = day_start_ts_utc()
    daily_pnl = db.sum_daily_pnl(conn, start)
    daily_dd = -daily_pnl if daily_pnl < 0 else 0.0

    # cooldown: use the latest realised loss from trades first; fall back to explicit LOSS log entry.
    # The previous implementation only looked for action='LOSS', but no code path emits that
    # action, so cooldown_after_loss_min was effectively dead and never blocked candidates.
    cooldown_min = int(limits.get("cooldown_after_loss_min", 0))
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

    max_conc = int(limits.get("max_concurrent_bots", 999999))
    if rs.active_bots >= max_conc:
        blocks.append({"code":"MAX_CONCURRENT_BOTS", "msg": f"active_bots={rs.active_bots} >= limit={max_conc}"})

    max_dd = float(limits.get("max_daily_dd_usdt", 1e18))
    if rs.daily_dd >= max_dd:
        blocks.append({"code":"MAX_DD_DAY", "msg": f"daily_dd={rs.daily_dd:.2f} >= limit={max_dd:.2f}"})

    if rs.cooldown_active:
        blocks.append({"code":"COOLDOWN_ACTIVE", "msg":"cooldown after losses is active"})

    max_symbol_bots = int(limits.get("max_symbol_bots", 999999))
    active_for_symbol = rs.symbol_bot_counts.get(f"{venue}:{symbol}", 0)
    if active_for_symbol >= max_symbol_bots:
        blocks.append({"code":"MAX_SYMBOL_BOTS", "msg": f"{venue}:{symbol} active={active_for_symbol} >= limit={max_symbol_bots}"})

    return blocks
