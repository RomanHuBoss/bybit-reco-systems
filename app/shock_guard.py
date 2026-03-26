from __future__ import annotations

import math
from statistics import median
from typing import Any

from . import db

APP_CONFIG_KEY = "market_shock_state_v1"
_FAST_VETO_STATE: dict[tuple[str, str, str], dict[str, Any]] = {}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _drop_open_candle(rows: list[dict[str, Any]] | list[Any], tf_sec: int, ts_now: int) -> list[dict[str, Any]] | list[Any]:
    if not rows:
        return rows
    newest = rows[0]
    ts = int(newest["ts"])
    if ts + int(tf_sec) > int(ts_now):
        return rows[1:]
    return rows


def _pct_change_from_rows(rows_desc: list[dict[str, Any]] | list[Any], lookback_bars: int) -> float | None:
    if not rows_desc or len(rows_desc) <= lookback_bars:
        return None
    now_px = float(rows_desc[0]["close"])
    prev_px = float(rows_desc[lookback_bars]["close"])
    if prev_px <= 0:
        return None
    return (now_px - prev_px) / prev_px


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return float(median(vals))


def _safe_num(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _severity_rank(severity: str) -> int:
    return {"normal": 0, "guarded": 1, "lockdown": 2}.get(str(severity or "normal"), 0)


def _state_meta(state: str) -> tuple[str, str, str]:
    state = str(state or "normal")
    mapping = {
        "normal": ("normal", "neutral", "NORMAL"),
        "amber_down": ("guarded", "down", "GUARDED"),
        "red_down": ("lockdown", "down", "LOCKDOWN"),
        "amber_up": ("guarded", "up", "GUARDED"),
        "red_up": ("lockdown", "up", "LOCKDOWN"),
        "chaos": ("lockdown", "two_sided", "LOCKDOWN"),
    }
    return mapping.get(state, ("normal", "neutral", "NORMAL"))


def _title_map(state: str) -> str:
    return {
        "normal": "Нормальный режим",
        "amber_down": "Осторожно: рынок давит вниз",
        "red_down": "Локдаун: медвежий удар",
        "amber_up": "Осторожно: рынок выстреливает вверх",
        "red_up": "Локдаун: бычий squeeze",
        "chaos": "Локдаун: хаотичный рынок",
    }.get(str(state or "normal"), "Нормальный режим")


def _operator_note_map(state: str) -> str:
    return {
        "normal": "Новые входы разрешены в обычном режиме.",
        "amber_down": "Не открывать neutral/long grid без очень веского ручного подтверждения. Short — только при явном тренде и дисциплине стопов.",
        "red_down": "Не открывать новые grid-боты. Действующие long/neutral пересмотреть вручную; держать только reduce-only логику на стороне биржи.",
        "amber_up": "Не открывать neutral/short grid без очень веского ручного подтверждения. Long — только при явном тренде и дисциплине стопов.",
        "red_up": "Не открывать новые grid-боты. Действующие short/neutral пересмотреть вручную; держать только reduce-only логику на стороне биржи.",
        "chaos": "Никаких новых входов: высокая вероятность пилы и переворотов.",
    }.get(str(state or "normal"), "Новые входы разрешены в обычном режиме.")


def _stabilize_market_shock(raw: dict[str, Any], prev: dict[str, Any] | None, now_ts: int, hold_sec: int) -> dict[str, Any]:
    out = dict(raw or {})
    prev = dict(prev or {})
    raw_state = str(out.get("state") or "normal")
    raw_severity = str(out.get("severity") or _state_meta(raw_state)[0])
    prev_state = str(prev.get("state") or "normal")
    prev_severity = str(prev.get("severity") or _state_meta(prev_state)[0])
    prev_ts = int(prev.get("ts", 0) or 0)
    prev_fresh = prev_ts > 0 and now_ts - prev_ts <= max(int(hold_sec), 60)

    applied = False
    mode = "pass_through"
    note = None
    stable_state = raw_state

    if prev_fresh and _severity_rank(prev_severity) > _severity_rank(raw_severity):
        if prev_severity == "lockdown":
            stable_state = prev_state
            applied = True
            mode = "release_cooldown"
            note = "Lockdown is released only after the market stays calmer for several cycles."
        elif prev_severity == "guarded" and raw_severity == "normal" and now_ts - prev_ts <= max(int(hold_sec // 2), 60):
            stable_state = prev_state
            applied = True
            mode = "guarded_release_cooldown"
            note = "Guarded mode is released with a short cooldown to avoid oscillation around the threshold."

    if stable_state != raw_state:
        severity, bias, action = _state_meta(stable_state)
        out["state"] = stable_state
        out["severity"] = severity
        out["bias"] = bias
        out["entry_mode"] = action.lower()
        out["title"] = _title_map(stable_state)
        out["operator_note"] = _operator_note_map(stable_state)
        out["lockdown"] = bool(stable_state in {"red_down", "red_up", "chaos"})
        reasons = list(out.get("reasons") or [])
        reasons.insert(0, {
            "code": "STATE_HOLD",
            "msg": note or "Previous market-shock state is temporarily held for stability.",
            "weight": 1,
        })
        out["reasons"] = reasons

    out["raw_state"] = raw_state
    out["stabilization"] = {
        "applied": bool(applied),
        "mode": mode,
        "note": note,
        "hold_sec": int(max(int(hold_sec), 60)),
        "previous_state": prev_state if prev_fresh else None,
    }
    out["ts"] = int(now_ts)
    return out


def _stabilize_fast_veto(raw: dict[str, Any], prev: dict[str, Any] | None, now_ts: int, release_sec: int) -> dict[str, Any]:
    out = dict(raw or {})
    prev = dict(prev or {})
    prev_ts = int(prev.get("ts", 0) or 0)
    prev_triggered = bool(prev.get("triggered"))
    prev_state = str(prev.get("state") or "normal")
    prev_fresh = prev_ts > 0 and now_ts - prev_ts <= max(int(release_sec), 60)

    if not bool(out.get("triggered")) and prev_triggered and prev_fresh:
        out["triggered"] = True
        out["state"] = prev_state if prev_state != "normal" else "cooldown"
        blocks = list(out.get("blocks") or [])
        blocks.insert(0, {
            "code": "FAST_VETO_RELEASE_COOLDOWN",
            "msg": "Быстрый veto удерживается ещё несколько циклов, чтобы не отпускать вход сразу после импульса.",
        })
        out["blocks"] = blocks
        out["stabilization"] = {
            "applied": True,
            "mode": "release_cooldown",
            "release_sec": int(max(int(release_sec), 60)),
            "previous_state": prev_state,
        }
    else:
        out["stabilization"] = {
            "applied": False,
            "mode": "pass_through",
            "release_sec": int(max(int(release_sec), 60)),
            "previous_state": prev_state if prev_fresh else None,
        }
    out["ts"] = int(now_ts)
    return out


def _symbol_snapshot(conn, venue: str, symbol: str, ts_now: int) -> dict[str, Any] | None:
    rows = db.get_latest_ohlcv(conn, venue, symbol, tf_sec=60, limit=40)
    rows = _drop_open_candle(rows, tf_sec=60, ts_now=ts_now)
    if not rows or len(rows) < 16:
        return None
    return {
        "symbol": symbol,
        "price": float(rows[0]["close"]),
        "r1m": _pct_change_from_rows(rows, 1),
        "r3m": _pct_change_from_rows(rows, 3),
        "r5m": _pct_change_from_rows(rows, 5),
        "r15m": _pct_change_from_rows(rows, 15),
        "rows_desc": rows,
    }


def compute_market_shock(
    conn,
    settings,
    sent_agg: dict[str, Any],
    symbol_feature_map: dict[tuple[str, str], dict[str, Any]],
    ts_now: int,
) -> dict[str, Any]:
    """Compute symbol-agnostic market stress regime for manual operator gating.

    States:
      normal, amber_down, red_down, amber_up, red_up, chaos
    """
    venue = "linear" if "linear" in getattr(settings, "venues", []) else "spot"
    symbols = list(getattr(settings, "symbols_linear", []) or getattr(settings, "symbols_spot", []))
    snaps: list[dict[str, Any]] = []
    for sym in symbols:
        snap = _symbol_snapshot(conn, venue, sym, ts_now)
        if not snap:
            continue
        feat = symbol_feature_map.get((venue, sym), {}) or {}
        snap["volume_z"] = _safe_num(feat.get("volume_z"), 0.0)
        snap["spread_bps"] = _safe_num(feat.get("spread_bps"), 0.0)
        snaps.append(snap)

    btc = next((s for s in snaps if s["symbol"] == "BTCUSDT"), None)
    eth = next((s for s in snaps if s["symbol"] == "ETHUSDT"), None)

    r5_vals = [float(s["r5m"]) for s in snaps if s.get("r5m") is not None]
    abs_r5_vals = [abs(v) for v in r5_vals]
    vol_z_vals = [float(s.get("volume_z") or 0.0) for s in snaps]
    spread_vals = [float(s.get("spread_bps") or 0.0) for s in snaps if s.get("spread_bps") is not None]

    active = len(snaps)
    breadth_down = (
        sum(1 for s in snaps if (s.get("r5m") or 0.0) <= -0.008 and (s.get("r15m") or 0.0) <= -0.012) / active
        if active else 0.0
    )
    breadth_up = (
        sum(1 for s in snaps if (s.get("r5m") or 0.0) >= 0.008 and (s.get("r15m") or 0.0) >= 0.012) / active
        if active else 0.0
    )
    breadth_mixed = (
        sum(1 for s in snaps if abs((s.get("r5m") or 0.0)) >= 0.010 and ((s.get("r5m") or 0.0) * (s.get("r15m") or 0.0) < 0.0)) / active
        if active else 0.0
    )

    median_r5 = _median(r5_vals)
    median_abs_r5 = _median(abs_r5_vals)
    median_vol_z = _median(vol_z_vals)
    median_spread = _median(spread_vals)

    sent_regime = str(sent_agg.get("regime") or "neutral")
    sent_strength = _safe_num(sent_agg.get("strength"), 0.0)
    sent_flags = dict(sent_agg.get("flags") or {})

    down_signals: list[dict[str, Any]] = []
    up_signals: list[dict[str, Any]] = []

    def add_signal(bucket: list[dict[str, Any]], code: str, msg: str, weight: int) -> None:
        bucket.append({"code": code, "msg": msg, "weight": int(weight)})

    if btc and (btc.get("r5m") is not None) and float(btc["r5m"]) <= -0.016:
        add_signal(down_signals, "BTC_5M_DUMP", f"BTC 5m={float(btc['r5m'])*100:.2f}%", 2)
    elif btc and (btc.get("r5m") is not None) and float(btc["r5m"]) <= -0.009:
        add_signal(down_signals, "BTC_5M_WEAK", f"BTC 5m={float(btc['r5m'])*100:.2f}%", 1)

    if eth and (eth.get("r5m") is not None) and float(eth["r5m"]) <= -0.020:
        add_signal(down_signals, "ETH_5M_DUMP", f"ETH 5m={float(eth['r5m'])*100:.2f}%", 2)
    elif eth and (eth.get("r5m") is not None) and float(eth["r5m"]) <= -0.012:
        add_signal(down_signals, "ETH_5M_WEAK", f"ETH 5m={float(eth['r5m'])*100:.2f}%", 1)

    if btc and (btc.get("r5m") is not None) and float(btc["r5m"]) >= 0.016:
        add_signal(up_signals, "BTC_5M_SQUEEZE", f"BTC 5m=+{float(btc['r5m'])*100:.2f}%", 2)
    elif btc and (btc.get("r5m") is not None) and float(btc["r5m"]) >= 0.009:
        add_signal(up_signals, "BTC_5M_UP", f"BTC 5m=+{float(btc['r5m'])*100:.2f}%", 1)

    if eth and (eth.get("r5m") is not None) and float(eth["r5m"]) >= 0.020:
        add_signal(up_signals, "ETH_5M_SQUEEZE", f"ETH 5m=+{float(eth['r5m'])*100:.2f}%", 2)
    elif eth and (eth.get("r5m") is not None) and float(eth["r5m"]) >= 0.012:
        add_signal(up_signals, "ETH_5M_UP", f"ETH 5m=+{float(eth['r5m'])*100:.2f}%", 1)

    if breadth_down >= 0.68:
        add_signal(down_signals, "MARKET_BREADTH_DOWN", f"breadth_down={breadth_down:.0%}", 2)
    elif breadth_down >= 0.50:
        add_signal(down_signals, "MARKET_BREADTH_DOWN_WEAK", f"breadth_down={breadth_down:.0%}", 1)

    if breadth_up >= 0.68:
        add_signal(up_signals, "MARKET_BREADTH_UP", f"breadth_up={breadth_up:.0%}", 2)
    elif breadth_up >= 0.50:
        add_signal(up_signals, "MARKET_BREADTH_UP_WEAK", f"breadth_up={breadth_up:.0%}", 1)

    if median_r5 <= -0.010 and median_vol_z >= 1.0:
        add_signal(down_signals, "IMPULSE_DOWN", f"median_r5={median_r5*100:.2f}% vol_z={median_vol_z:.2f}", 2)
    elif median_r5 <= -0.006:
        add_signal(down_signals, "IMPULSE_DOWN_WEAK", f"median_r5={median_r5*100:.2f}%", 1)

    if median_r5 >= 0.010 and median_vol_z >= 1.0:
        add_signal(up_signals, "IMPULSE_UP", f"median_r5=+{median_r5*100:.2f}% vol_z={median_vol_z:.2f}", 2)
    elif median_r5 >= 0.006:
        add_signal(up_signals, "IMPULSE_UP_WEAK", f"median_r5=+{median_r5*100:.2f}%", 1)

    if sent_flags.get("panic") or (sent_regime == "risk_off" and sent_strength >= 0.45):
        add_signal(down_signals, "SENTIMENT_RISK_OFF", f"sentiment={sent_regime} strength={sent_strength:.2f}", 1)
    if sent_flags.get("euphoria") or (sent_regime == "risk_on" and sent_strength >= 0.45):
        add_signal(up_signals, "SENTIMENT_RISK_ON", f"sentiment={sent_regime} strength={sent_strength:.2f}", 1)

    down_weight = sum(int(s["weight"]) for s in down_signals)
    up_weight = sum(int(s["weight"]) for s in up_signals)

    chaos = bool(
        active >= 6
        and median_abs_r5 >= 0.012
        and median_vol_z >= 1.1
        and breadth_mixed >= 0.20
        and abs(median_r5) <= 0.004
    )

    if chaos:
        state = "chaos"
        severity = "lockdown"
        bias = "two_sided"
        action = "LOCKDOWN"
        reasons = [
            {"code": "CHAOS", "msg": f"mixed breadth={breadth_mixed:.0%}, median_abs_r5={median_abs_r5*100:.2f}%", "weight": 3}
        ]
    elif down_weight >= 4 and down_weight > up_weight:
        state = "red_down"
        severity = "lockdown"
        bias = "down"
        action = "LOCKDOWN"
        reasons = down_signals
    elif up_weight >= 4 and up_weight > down_weight:
        state = "red_up"
        severity = "lockdown"
        bias = "up"
        action = "LOCKDOWN"
        reasons = up_signals
    elif down_weight >= 2 and down_weight > up_weight:
        state = "amber_down"
        severity = "guarded"
        bias = "down"
        action = "GUARDED"
        reasons = down_signals
    elif up_weight >= 2 and up_weight > down_weight:
        state = "amber_up"
        severity = "guarded"
        bias = "up"
        action = "GUARDED"
        reasons = up_signals
    else:
        state = "normal"
        severity = "normal"
        bias = "neutral"
        action = "NORMAL"
        reasons = []

    raw = {
        "ts": int(ts_now),
        "state": state,
        "severity": severity,
        "bias": bias,
        "entry_mode": action.lower(),
        "title": _title_map(state),
        "operator_note": _operator_note_map(state),
        "lockdown": bool(state in {"red_down", "red_up", "chaos"}),
        "metrics": {
            "active_symbols": active,
            "breadth_down": round(breadth_down, 4),
            "breadth_up": round(breadth_up, 4),
            "breadth_mixed": round(breadth_mixed, 4),
            "median_r5m": round(median_r5, 6),
            "median_abs_r5m": round(median_abs_r5, 6),
            "median_volume_z": round(median_vol_z, 4),
            "median_spread_bps": round(median_spread, 4),
            "btc_r5m": round(float(btc["r5m"]), 6) if btc and btc.get("r5m") is not None else None,
            "btc_r15m": round(float(btc["r15m"]), 6) if btc and btc.get("r15m") is not None else None,
            "eth_r5m": round(float(eth["r5m"]), 6) if eth and eth.get("r5m") is not None else None,
            "eth_r15m": round(float(eth["r15m"]), 6) if eth and eth.get("r15m") is not None else None,
        },
        "reasons": reasons,
        "sentiment": {
            "regime": sent_regime,
            "strength": round(sent_strength, 4),
            "flags": sent_flags,
        },
    }
    prev_state = db.get_app_config_json(conn, APP_CONFIG_KEY, default={}) or {}
    hold_sec = max(int(getattr(settings, "reco_interval_sec", 20)) * 4, 180)
    return _stabilize_market_shock(raw, prev_state, int(ts_now), hold_sec)


def compute_symbol_fast_veto(conn, venue: str, symbol: str, ts_now: int, direction: str, feature_row: dict[str, Any] | None = None) -> dict[str, Any]:
    global _FAST_VETO_STATE
    snap = _symbol_snapshot(conn, venue, symbol, ts_now)
    if not snap:
        return {
            "state": "unknown",
            "triggered": False,
            "blocks": [],
            "metrics": {"r1m": None, "r3m": None, "r5m": None, "volume_z": None},
            "stabilization": {"applied": False, "mode": "no_data", "release_sec": 120, "previous_state": None},
            "ts": int(ts_now),
        }
    volume_z = _safe_num((feature_row or {}).get("volume_z"), 0.0)
    r1m = float(snap.get("r1m") or 0.0)
    r3m = float(snap.get("r3m") or 0.0)
    r5m = float(snap.get("r5m") or 0.0)
    blocks: list[dict[str, Any]] = []
    state = "normal"

    if direction == "long" and (r1m <= -0.006 and r3m <= -0.010 and r5m <= -0.018 and volume_z >= 0.75):
        state = "down_break"
        blocks.append({
            "code": "FAST_BREAK_DOWN",
            "msg": f"{symbol}: 1m={r1m*100:.2f}%, 3m={r3m*100:.2f}%, 5m={r5m*100:.2f}% при volume_z={volume_z:.2f} — long запрещён",
        })
    elif direction == "short" and (r1m >= 0.006 and r3m >= 0.010 and r5m >= 0.018 and volume_z >= 0.75):
        state = "up_break"
        blocks.append({
            "code": "FAST_BREAK_UP",
            "msg": f"{symbol}: 1m=+{r1m*100:.2f}%, 3m=+{r3m*100:.2f}%, 5m=+{r5m*100:.2f}% при volume_z={volume_z:.2f} — short запрещён",
        })
    elif direction == "neutral" and abs(r5m) >= 0.020 and volume_z >= 0.90:
        state = "volatility_spike"
        blocks.append({
            "code": "FAST_VOLATILITY_SPIKE",
            "msg": f"{symbol}: abs(5m)={abs(r5m)*100:.2f}% при volume_z={volume_z:.2f} — neutral/grid вход запрещён",
        })

    raw = {
        "state": state,
        "triggered": bool(blocks),
        "blocks": blocks,
        "metrics": {
            "r1m": round(r1m, 6),
            "r3m": round(r3m, 6),
            "r5m": round(r5m, 6),
            "volume_z": round(volume_z, 4),
        },
    }
    key = (str(venue), str(symbol), str(direction or "neutral"))
    stabilized = _stabilize_fast_veto(raw, _FAST_VETO_STATE.get(key), int(ts_now), release_sec=120)
    _FAST_VETO_STATE[key] = {
        "ts": int(ts_now),
        "state": str(stabilized.get("state") or "normal"),
        "triggered": bool(stabilized.get("triggered")),
    }
    return stabilized


def apply_market_shock_gate(market_shock: dict[str, Any], venue: str, bot_type: str, direction: str) -> list[dict[str, Any]]:
    state = str((market_shock or {}).get("state") or "normal")
    title = str((market_shock or {}).get("title") or state)
    reasons = market_shock.get("reasons") or []
    reason_tail = "; ".join(str(r.get("msg") or r.get("code") or "") for r in reasons[:3])
    suffix = f" ({reason_tail})" if reason_tail else ""

    if state in {"red_down", "red_up", "chaos"}:
        return [{
            "code": "MARKET_LOCKDOWN",
            "msg": f"{title}: новые входы заблокированы{suffix}",
        }]

    if state == "amber_down" and direction in {"long", "neutral"}:
        return [{
            "code": "MARKET_GUARDED_DOWN",
            "msg": f"{title}: {direction} вход заблокирован{suffix}",
        }]

    if state == "amber_up" and direction in {"short", "neutral"}:
        return [{
            "code": "MARKET_GUARDED_UP",
            "msg": f"{title}: {direction} вход заблокирован{suffix}",
        }]

    return []
