from __future__ import annotations

import math
import os
from datetime import datetime, timezone

try:
    # Python 3.9+ (PEP 615). Requires system tzdata or the "tzdata" pip package.
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore
from dataclasses import dataclass
from typing import Any

from . import db
from .grid_math import strict_integer

BYBIT_FUTURES_GRID_MAX_CONCURRENT_BOTS = 50
BYBIT_FUTURES_GRID_MAX_SYMBOL_BOTS = 50
BYBIT_FUTURES_GRID_MIN_LEVERAGE_DEFAULT = 3
BYBIT_FUTURES_GRID_MAX_LEVERAGE_DEFAULT = 5
BYBIT_FUTURES_GRID_MAX_POSITION_NOTIONAL_USDT_DEFAULT = 500.0
BYBIT_FUTURES_GRID_MAX_MARGIN_PER_BOT_USDT_DEFAULT = 100.0

DEFAULT_RISK_LIMITS: dict[str, Any] = {
    "max_concurrent_bots": 1,
    "max_daily_dd_usdt": 10.0,
    "cooldown_after_loss_min": 90,
    "max_symbol_bots": 1,
    "min_leverage": BYBIT_FUTURES_GRID_MIN_LEVERAGE_DEFAULT,
    "max_leverage": BYBIT_FUTURES_GRID_MAX_LEVERAGE_DEFAULT,
    "max_position_notional_usdt": BYBIT_FUTURES_GRID_MAX_POSITION_NOTIONAL_USDT_DEFAULT,
    "max_margin_per_bot_usdt": BYBIT_FUTURES_GRID_MAX_MARGIN_PER_BOT_USDT_DEFAULT,
}


@dataclass
class RiskStatus:
    limits: dict[str, Any]
    active_bots: int
    daily_pnl: float
    daily_dd: float
    cooldown_active: bool
    symbol_bot_counts: dict[str, int]


def _safe_default_int(value: Any, default: int) -> int:
    parsed = strict_integer(value)
    return int(default) if parsed is None else int(parsed)


def _safe_default_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        num = float(value)
    except Exception:
        num = float(default)
    if not math.isfinite(num):
        return float(default)
    return float(num)


def _limit_int(limits: dict[str, Any], key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    parsed = strict_integer(limits.get(key, default))
    value = int(default) if parsed is None else int(parsed)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return int(value)


def _limit_float(limits: dict[str, Any], key: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = limits.get(key, default)
    if isinstance(raw, bool):
        value = float(default)
    else:
        try:
            value = float(raw)
        except Exception:
            value = float(default)
    if not math.isfinite(value):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return float(value)


def _normalize_risk_limits(active: Any, fallback_limits: dict[str, Any]) -> dict[str, Any]:
    fallback = dict(fallback_limits or {})
    merged = dict(fallback)
    if isinstance(active, dict):
        merged.update(active)

    # Defaults тоже нормализуем через безопасные helpers: если fallback пришёл из
    # ENV/legacy-конфига с ``NaN`` или строковым мусором, runtime не должен
    # падать и не должен возвращать наружу поломанные лимиты как есть.
    default_max_concurrent = _safe_default_int(fallback.get("max_concurrent_bots", 1), 1)
    default_max_daily_dd = _safe_default_float(fallback.get("max_daily_dd_usdt", 10.0), 10.0)
    default_cooldown_min = _safe_default_int(fallback.get("cooldown_after_loss_min", 90), 90)
    default_max_symbol_bots = _safe_default_int(fallback.get("max_symbol_bots", 1), 1)
    default_min_leverage = _safe_default_int(
        fallback.get("min_leverage", BYBIT_FUTURES_GRID_MIN_LEVERAGE_DEFAULT),
        BYBIT_FUTURES_GRID_MIN_LEVERAGE_DEFAULT,
    )
    default_max_leverage = _safe_default_int(
        fallback.get("max_leverage", BYBIT_FUTURES_GRID_MAX_LEVERAGE_DEFAULT),
        BYBIT_FUTURES_GRID_MAX_LEVERAGE_DEFAULT,
    )
    default_max_position_notional = _safe_default_float(
        fallback.get("max_position_notional_usdt", BYBIT_FUTURES_GRID_MAX_POSITION_NOTIONAL_USDT_DEFAULT),
        BYBIT_FUTURES_GRID_MAX_POSITION_NOTIONAL_USDT_DEFAULT,
    )
    default_max_margin_per_bot = _safe_default_float(
        fallback.get("max_margin_per_bot_usdt", BYBIT_FUTURES_GRID_MAX_MARGIN_PER_BOT_USDT_DEFAULT),
        BYBIT_FUTURES_GRID_MAX_MARGIN_PER_BOT_USDT_DEFAULT,
    )

    # Bybit caps Futures Grid Bot instances at 50 total. Risk limits are
    # operator guardrails, not a way to bypass exchange/product constraints;
    # fail closed by clamping the effective runtime limit to the product maximum.
    merged["max_concurrent_bots"] = _limit_int(
        merged,
        "max_concurrent_bots",
        default_max_concurrent,
        minimum=1,
        maximum=BYBIT_FUTURES_GRID_MAX_CONCURRENT_BOTS,
    )
    merged["max_daily_dd_usdt"] = _limit_float(
        merged,
        "max_daily_dd_usdt",
        default_max_daily_dd,
        minimum=0.0,
        maximum=1e12,
    )
    merged["cooldown_after_loss_min"] = _limit_int(
        merged,
        "cooldown_after_loss_min",
        default_cooldown_min,
        minimum=0,
        maximum=7 * 24 * 60,
    )
    merged["max_symbol_bots"] = _limit_int(
        merged,
        "max_symbol_bots",
        default_max_symbol_bots,
        minimum=1,
        maximum=BYBIT_FUTURES_GRID_MAX_SYMBOL_BOTS,
    )
    merged["max_leverage"] = _limit_int(
        merged,
        "max_leverage",
        default_max_leverage,
        minimum=1,
        maximum=100,
    )
    merged["min_leverage"] = _limit_int(
        merged,
        "min_leverage",
        min(default_min_leverage, int(merged["max_leverage"])),
        minimum=1,
        maximum=max(1, int(merged["max_leverage"])),
    )
    merged["max_position_notional_usdt"] = _limit_float(
        merged,
        "max_position_notional_usdt",
        default_max_position_notional,
        minimum=0.0,
        maximum=1e12,
    )
    merged["max_margin_per_bot_usdt"] = _limit_float(
        merged,
        "max_margin_per_bot_usdt",
        default_max_margin_per_bot,
        minimum=0.0,
        maximum=1e12,
    )
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

def normalize_risk_limits(limits: Any, fallback_limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """Возвращает канонический runtime-набор risk limits.

    API/ENV могут приносить строковый мусор, отрицательные значения или лишние ключи.
    Runtime всё равно умеет работать только с известным набором лимитов, поэтому для
    целостности храним и отдаём именно *effective* limits, а не сырой операторский payload.
    Иначе БД/audit показывают одно, а реально применяются другие границы после clamp.
    """
    base = _normalize_risk_limits(fallback_limits, DEFAULT_RISK_LIMITS)
    effective = _normalize_risk_limits(limits, base)
    return {
        "max_concurrent_bots": int(effective["max_concurrent_bots"]),
        "max_daily_dd_usdt": float(effective["max_daily_dd_usdt"]),
        "cooldown_after_loss_min": int(effective["cooldown_after_loss_min"]),
        "max_symbol_bots": int(effective["max_symbol_bots"]),
        "min_leverage": int(effective["min_leverage"]),
        "max_leverage": int(effective["max_leverage"]),
        "max_position_notional_usdt": float(effective["max_position_notional_usdt"]),
        "max_margin_per_bot_usdt": float(effective["max_margin_per_bot_usdt"]),
    }


def get_risk_limits(conn, fallback_limits: dict[str, Any]) -> dict[str, Any]:
    active = db.get_active_risk_limits(conn)
    return normalize_risk_limits(active, fallback_limits)

def compute_risk_status(conn, limits: dict[str, Any]) -> RiskStatus:
    active_bots = db.get_active_bots(conn)
    n_active = len(active_bots)

    start = day_start_ts_utc()
    realized_events = db.list_realized_net_events(conn, since_ts=start)
    daily_pnl = sum(float(item.get("net_pnl") or 0.0) for item in realized_events)

    # True realised intraday drawdown = peak-to-trough drop of the unified net
    # evidence stream. Evidence-grade events replace legacy aggregate rows per bot
    # so the same execution is not counted twice.
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in realized_events:
        try:
            net_pnl = float(row.get("net_pnl") or 0.0)
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
    limits = normalize_risk_limits(limits, limits)
    cooldown_min = _limit_int(limits, "cooldown_after_loss_min", 0, minimum=0, maximum=7 * 24 * 60)
    cooldown_active = False
    if cooldown_min > 0:
        last_loss_ts = None

        recent_realized = db.list_realized_net_events(
            conn,
            since_ts=max(0, db.now_ts() - int(cooldown_min) * 60),
        )
        negative_ts = [int(item["ts"]) for item in recent_realized if float(item.get("net_pnl") or 0.0) < 0.0]
        if negative_ts:
            last_loss_ts = max(negative_ts)

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

    limits = normalize_risk_limits(limits, limits)
    max_conc = _limit_int(
        limits,
        "max_concurrent_bots",
        999999,
        minimum=1,
        maximum=BYBIT_FUTURES_GRID_MAX_CONCURRENT_BOTS,
    )
    if rs.active_bots >= max_conc:
        blocks.append({"code":"MAX_CONCURRENT_BOTS", "msg": f"active_bots={rs.active_bots} >= limit={max_conc}"})

    max_dd = _limit_float(limits, "max_daily_dd_usdt", 1e18, minimum=0.0, maximum=1e18)
    if rs.daily_dd >= max_dd:
        blocks.append({"code":"MAX_DD_DAY", "msg": f"daily_dd={rs.daily_dd:.2f} >= limit={max_dd:.2f}"})

    if rs.cooldown_active:
        blocks.append({"code":"COOLDOWN_ACTIVE", "msg":"cooldown after losses is active"})

    max_symbol_bots = _limit_int(
        limits,
        "max_symbol_bots",
        999999,
        minimum=1,
        maximum=BYBIT_FUTURES_GRID_MAX_SYMBOL_BOTS,
    )
    active_for_symbol = rs.symbol_bot_counts.get(f"{venue}:{symbol}", 0)
    if active_for_symbol >= max_symbol_bots:
        blocks.append({"code":"MAX_SYMBOL_BOTS", "msg": f"{venue}:{symbol} active={active_for_symbol} >= limit={max_symbol_bots}"})

    return blocks
