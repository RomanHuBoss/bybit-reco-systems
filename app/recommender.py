from __future__ import annotations

import math
import secrets
from typing import Any

from . import db
from .features import compute_features_from_ohlcv, liquidity_tier, funding_signal, oi_trend, btc_beta
from .regime import classify_regime
from .risk import gate_candidate, compute_risk_status as _compute_risk_status
from .direction import vote_for_tf, aggregate_direction
from .sentiment_features import compute_sentiment_agg, compute_symbol_sentiment_map
from .outcomes import BOT_HORIZONS
from .calibration import (
    fit_platt, PlattScaler, save_platt_to_db, load_platt_from_db, BOT_CALIB_KEYS,
    LogRegScaler, fit_logreg, save_logreg_to_db, load_logreg_from_db,
    extract_features, GLOBAL_LOGREG_KEY, CALIB_REFIT_INTERVAL_SEC,
)
# Note: calibrators use db.get_outcomes_with_recs (single JOIN query) to avoid N+1 pattern

BOT_TYPES_BYBIT = [
    "spot_grid",
    "futures_grid",
    "dca_bot",
    "futures_martingale",
    "futures_combo",
]

def _fmt_tf(tf_sec: int) -> str:
    if tf_sec % 86400 == 0:
        d = tf_sec // 86400
        return f"{d}d"
    if tf_sec % 3600 == 0:
        h = tf_sec // 3600
        return f"{h}h"
    if tf_sec % 60 == 0:
        m = tf_sec // 60
        return f"{m}m"
    return f"{tf_sec}s"

def _round_price(x: float | None, decimals: int = 6) -> float | None:
    if x is None:
        return None
    try:
        return float(round(float(x), decimals))
    except Exception:
        return None

def _pct_dist(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    try:
        if b == 0:
            return None
        return float((a - b) / b * 100.0)
    except Exception:
        return None



def _estimate_cost_model(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    taker_fee_bps: float,
    direction: str,
    funding_rate: float | None = None,
    next_funding_ts: int | None = None,
    ts_now: int | None = None,
) -> dict[str, Any]:
    """Approximate round-trip execution costs used in scoring/params/outcomes.

    Funding is direction-sensitive and event-based. We do not pro-rate abs(rate)
    over the whole horizon because that creates fake carry costs on trades that exit
    before the next funding timestamp and gets long/short economics backwards.
    """
    spread_bps = f.get("spread_bps")
    spread_bps = float(spread_bps) if spread_bps is not None else None

    if spread_bps is None:
        fallback_spread = 10.0 if venue == "spot" else 8.0
        spread_bps_used = fallback_spread
        spread_missing = True
    else:
        spread_bps_used = max(0.0, float(spread_bps))
        spread_missing = False

    fee_bps_round_trip = max(0.0, float(taker_fee_bps)) * 2.0
    slippage_bps = max(1.0 if venue == "spot" else 0.8, spread_bps_used * (0.35 if bot_type in ("spot_grid", "futures_grid") else 0.50))

    horizon_sec = BOT_HORIZONS.get(bot_type, 0)
    fr = float(funding_rate) if funding_rate is not None else None
    directional_funding_bps_8h = 0.0
    if fr is not None:
        if direction == "long":
            directional_funding_bps_8h = fr * 10000.0
        elif direction == "short":
            directional_funding_bps_8h = -fr * 10000.0

    expected_funding_events = 0
    expected_funding_bps = 0.0
    if venue == "linear" and fr is not None and horizon_sec > 0:
        now = int(ts_now or 0)
        nfts = int(next_funding_ts or 0)
        if now > 0 and nfts > 0:
            if now + horizon_sec >= nfts:
                expected_funding_events = 1
                if now + horizon_sec >= nfts + 8 * 3600:
                    expected_funding_events = 2
        else:
            expected_funding_events = 1 if horizon_sec >= 8 * 3600 else 0
        expected_funding_bps = directional_funding_bps_8h * expected_funding_events

    total_cost_bps = fee_bps_round_trip + spread_bps_used + slippage_bps + max(0.0, expected_funding_bps)

    return {
        "spread_bps": spread_bps_used,
        "spread_missing": spread_missing,
        "fee_bps_round_trip": fee_bps_round_trip,
        "slippage_bps": float(slippage_bps),
        "funding_rate": fr,
        "direction": direction,
        "directional_funding_bps_8h": float(directional_funding_bps_8h),
        "next_funding_ts": int(next_funding_ts) if next_funding_ts else None,
        "expected_funding_events": int(expected_funding_events),
        "expected_funding_bps": float(expected_funding_bps),
        "total_cost_bps": float(total_cost_bps),
        "horizon_sec": int(horizon_sec),
    }


def _funding_score_adjustment(direction: str, fr_sig: dict[str, Any]) -> float:
    sig = str(fr_sig.get("signal") or "unknown")
    if direction == "long":
        if sig == "bullish":
            return 0.04
        if sig == "bearish":
            return -0.06
    elif direction == "short":
        if sig == "bearish":
            return 0.04
        if sig == "bullish":
            return -0.06
    return 0.0


def _build_feature_snapshot(
    score: float,
    atr_pct: float,
    effective_sent: float,
    cost_model: dict[str, Any],
    direction_agg: dict[str, Any],
    oi_sig: dict[str, Any],
    liq_tier: str,
    beta_info: dict[str, Any],
) -> dict[str, float]:
    liq_map = {"micro": 0.0, "low": 0.33, "medium": 0.67, "high": 1.0, "unknown": 0.5}
    trendiness = abs(float(direction_agg.get("trendiness") or 0.0))
    dir_conf = direction_agg.get("direction_confidence_calibrated")
    if dir_conf is None:
        dir_conf = direction_agg.get("direction_confidence")
    return {
        "range_score": _clamp(1.0 - trendiness, 0.0, 1.0),
        "trend_strength": _clamp(trendiness, 0.0, 1.0),
        "atr_pct_norm": _clamp(float(atr_pct) / 0.10, 0.0, 2.0),
        "effective_sentiment": _clamp(float(effective_sent), -1.0, 1.0),
        "dir_conf": _clamp(float(dir_conf if dir_conf is not None else 0.5), 0.0, 1.0),
        "coherence": _clamp(float(direction_agg.get("coherence") or 0.5), 0.0, 1.0),
        "spread_bps_norm": _clamp(float(cost_model.get("spread_bps") or cost_model.get("total_cost_bps") or 8.0) / 10.0, 0.0, 5.0),
        "score": _clamp(float(score), -1.0, 1.0),
        "oi_4h_norm": _clamp(float(oi_sig.get("oi_4h_chg_pct") or 0.0) / 10.0, -3.0, 3.0),
        "funding_norm": _clamp(float(cost_model.get("directional_funding_bps_8h") or 0.0) / 20.0, -2.0, 2.0),
        "liq_tier_num": float(liq_map.get(str(liq_tier).lower(), 0.67)),
        "btc_corr": _clamp(float(beta_info.get("correlation") or 0.0), -1.0, 1.0),
        "regime_conf": _clamp(float(direction_agg.get("regime_confidence") or 0.5), 0.0, 1.0),
    }

def _build_trade_plan(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    direction: str,
    params: dict[str, Any],
    cost_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Human/actionable execution guide shown in the UI 'Details' panel.

    This is NOT a guarantee of profit and NOT a full risk model.
    It provides consistent, ATR-scaled reference levels (TP/SL/kill-switch)
    and an approximate time horizon for bot lifecycle.
    """

    price = float(f.get("price") or 0.0) or None
    atr_pct_1m = float(f.get("atr_pct") or 0.0)
    atr_pct_15m = float(f.get("_atr_pct_15m") or 0.0)
    atr_pct_1h = float(f.get("_atr_pct_1h") or 0.0)
    atr_pct_4h = float(f.get("_atr_pct_4h") or 0.0)
    atr_pct_slow = atr_pct_1h if atr_pct_1h > 0 else atr_pct_1m

    atr_abs_1h = (price * atr_pct_slow) if (price is not None and atr_pct_slow > 0) else None
    atr_abs_15m = (price * atr_pct_15m) if (price is not None and atr_pct_15m > 0) else None
    atr_abs_4h = (price * atr_pct_4h) if (price is not None and atr_pct_4h > 0) else None

    # Default timeframes
    decision_tfs = {"macro": "1h", "entry": "15m", "monitor": "1m"}

    # Horizon by bot type (heuristics)
    if bot_type in ("spot_grid", "futures_grid"):
        horizon = {"min_hours": 6, "max_hours": 48}
    elif bot_type == "dca_bot":
        horizon = {"min_hours": 12, "max_hours": 72}
    elif bot_type == "futures_martingale":
        horizon = {"min_hours": 1, "max_hours": 8}
    elif bot_type == "futures_combo":
        horizon = {"min_hours": 2, "max_hours": 12}
    else:
        horizon = {"min_hours": 2, "max_hours": 24}

    # Adjust horizon by regime confidence if present
    d = f.get("_direction_agg") or {}
    regime = str(d.get("regime") or "unknown")
    regime_conf = float(d.get("regime_confidence") or 0.0)
    if regime_conf >= 0.75:
        horizon = {"min_hours": max(1, int(horizon["min_hours"] * 0.8)), "max_hours": int(horizon["max_hours"] * 0.85)}
    elif regime_conf <= 0.35:
        horizon = {"min_hours": int(horizon["min_hours"] * 1.0), "max_hours": int(horizon["max_hours"] * 0.6)}

    def lvl(name: str, px: float | None) -> dict[str, Any]:
        return {
            "name": name,
            "price": _round_price(px, decimals=8),
            "dist_pct_from_entry": _round_price(_pct_dist(px, price), decimals=4) if (px is not None and price is not None) else None,
        }

    plan: dict[str, Any] = {
        "reference_price": _round_price(price, decimals=10),
        "decision_timeframes": decision_tfs,
        "expected_horizon": {**horizon, "basis": "heuristics(bot_type)+regime_confidence"},
        "volatility": {
            "atr_pct_1m": atr_pct_1m,
            "atr_pct_15m": atr_pct_15m if atr_pct_15m > 0 else None,
            "atr_pct_1h": atr_pct_1h if atr_pct_1h > 0 else None,
            "atr_pct_4h": atr_pct_4h if atr_pct_4h > 0 else None,
            "atr_pct_used": atr_pct_slow,
            "atr_abs_used": _round_price(atr_abs_1h, decimals=10) if atr_abs_1h is not None else None,
        },
        "regime": {
            "name": regime,
            "confidence": _round_price(regime_conf, decimals=4),
            "trendiness": _round_price(float(d.get("trendiness") or 0.0), decimals=4) if isinstance(d.get("trendiness"), (int, float)) else None,
            "coherence": _round_price(float(d.get("coherence") or 0.0), decimals=4) if isinstance(d.get("coherence"), (int, float)) else None,
        },
        "bot_type": bot_type,
        "venue": venue,
        "direction": direction,
        "cost_model": dict(cost_model or {}),
        "levels": {},
        "close_conditions": [],
        "notes": "Ориентиры уровней (TP/SL/диапазон) масштабируются по ATR старшего ТФ (предпочтительно 1h). Это подсказка для запуска/контроля бота, а не обещание результата.",
    }

    # ── Martingale: TP/SL ladder (directional reference) ──
    if bot_type in ("futures_martingale",):
        if price is not None and atr_abs_1h is not None and atr_abs_1h > 0 and direction in ("long", "short"):
            sgn = 1.0 if direction == "long" else -1.0
            sl = price - sgn * (1.0 * atr_abs_1h)
            tp1 = price + sgn * (0.9 * atr_abs_1h)
            tp2 = price + sgn * (1.6 * atr_abs_1h)
            tp3 = price + sgn * (2.3 * atr_abs_1h)
            trail = atr_abs_15m if (atr_abs_15m is not None and atr_abs_15m > 0) else (0.5 * atr_abs_1h)
            plan["levels"] = {
                "stop_loss": lvl("SL", sl),
                "take_profit": [lvl("TP1", tp1), lvl("TP2", tp2), lvl("TP3", tp3)],
                "trailing_stop": {"distance": _round_price(trail, decimals=10), "tf": "15m" if atr_abs_15m else "1h"},
                "risk_kill_switch": {
                    "max_adverse_move": _round_price(2.5 * atr_abs_1h, decimals=10),
                    "comment": "Если цена ушла против позиции сильнее ~2.5 ATR(1h), лучше принудительно остановить/закрыть бота.",
                },
            }
            plan["close_conditions"] = [
                "Достижение TP1/TP2/TP3 (можно фиксировать частями).",
                "Пробой против позиции сильнее ~2.5 ATR(1h) (risk_kill_switch).",
                "Истечение ожидаемого horizon (expected_horizon.max_hours) или смена режима/направления на противоположный.",
            ]
        else:
            plan["levels"] = {"comment": "Недостаточно данных (ATR/price/direction) для расчёта TP/SL."}
            plan["close_conditions"] = ["Недостаточно данных для уровней — используйте стандартные ограничения риска/времени."]

    # ── Grid: range + kill-switch + step ──
    elif bot_type in ("spot_grid", "futures_grid"):
        lower = params.get("price_range_lower")
        upper = params.get("price_range_upper")
        ks_pad = (0.6 * atr_abs_1h) if (atr_abs_1h is not None and atr_abs_1h > 0) else None
        lower_ks = (float(lower) - ks_pad) if (lower is not None and ks_pad is not None) else None
        upper_ks = (float(upper) + ks_pad) if (upper is not None and ks_pad is not None) else None

        step_pct = params.get("grid_spacing_pct")
        step_abs = (price * float(step_pct) / 100.0) if (price is not None and step_pct is not None) else None
        tp_leg_abs = (0.7 * step_abs) if step_abs is not None else (0.25 * atr_abs_1h if atr_abs_1h else None)

        plan["levels"] = {
            "range": {
                "lower": _round_price(float(lower), decimals=10) if lower is not None else None,
                "upper": _round_price(float(upper), decimals=10) if upper is not None else None,
            },
            "kill_switch": {
                "lower": _round_price(lower_ks, decimals=10),
                "upper": _round_price(upper_ks, decimals=10),
                "pad_abs": _round_price(ks_pad, decimals=10),
                "comment": "Если цена выходит за kill_switch — сетку лучше остановить (признак пробоя диапазона).",
            },
            "grid_step": {
                "step_pct": float(step_pct) if step_pct is not None else None,
                "step_abs": _round_price(step_abs, decimals=10),
                "comment": "Рекомендованный шаг сетки (ориентир).",
            },
            "tp_per_leg": {
                "abs": _round_price(tp_leg_abs, decimals=10),
                "pct": _round_price((tp_leg_abs / price * 100.0) if (tp_leg_abs is not None and price) else None, decimals=4),
                "comment": "Ориентир на прибыль на одну 'ногу' (часто ~0.6–0.8 от шага сетки).",
            },
        }
        plan["close_conditions"] = [
            "Выход цены за kill_switch (признак пробоя диапазона).",
            "Истечение expected_horizon.max_hours без возврата в диапазон/без набора прибыли.",
            "Рост trendiness/regime='trend' (по direction_agg) — сетку лучше остановить.",
        ]

    # ── DCA: steps + TP-from-avg + stop-out ──
    elif bot_type == "dca_bot":
        step_pct = float(params.get("dca_step_pct") or 0.0)
        max_orders = int(params.get("max_orders") or 0)
        step_abs = (price * step_pct / 100.0) if (price is not None and step_pct > 0) else None

        tp_from_avg_abs = (0.8 * atr_abs_1h) if (atr_abs_1h is not None and atr_abs_1h > 0) else None
        stop_out_abs = ((max_orders + 1) * step_abs) if (step_abs is not None and max_orders > 0) else None
        stop_out_price = (price - stop_out_abs) if (price is not None and stop_out_abs is not None) else None

        plan["levels"] = {
            "dca_step": {
                "step_pct": step_pct if step_pct > 0 else None,
                "step_abs": _round_price(step_abs, decimals=10),
                "max_orders": max_orders,
            },
            "take_profit_from_avg": {
                "abs": _round_price(tp_from_avg_abs, decimals=10),
                "pct": _round_price((tp_from_avg_abs / price * 100.0) if (tp_from_avg_abs is not None and price) else None, decimals=4),
                "comment": "TP считается от средней цены позиции (avg_entry).",
            },
            "stop_out": {
                "price": _round_price(stop_out_price, decimals=10),
                "max_adverse_abs": _round_price(stop_out_abs, decimals=10),
                "comment": "Жёсткий предел усреднений: если цена ушла ниже stop_out — лучше остановить DCA, иначе это превращается в 'держим навсегда'.",
            },
        }
        plan["close_conditions"] = [
            "Достижение take_profit_from_avg от средней цены позиции (avg_entry).",
            "Достижение stop_out (жёсткий предел усреднений).",
            "Истечение expected_horizon.max_hours при отсутствии прогресса/смене риск-режима.",
        ]

    # ── Combo/hedge: no direct TP/SL ──
    elif bot_type == "futures_combo":
        plan["levels"] = {
            "comment": "Для hedge/комбо ключевой ориентир — волатильность и риск-режим. TP/SL зависит от двух ног стратегии; используйте ATR(1h) как масштаб для контрольных уровней.",
            "atr_abs_used": _round_price(atr_abs_1h, decimals=10) if atr_abs_1h is not None else None,
        }
        plan["close_conditions"] = [
            "Истечение expected_horizon.max_hours или нормализация волатильности/сентимента.",
            "Перекос одной из ног выше допустимого риска (ручной контроль).",
        ]

    return plan

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def _direction(bot_type: str, agg: dict[str, Any]) -> str:
    if bot_type in ("spot_grid","futures_grid"):
        return str(agg.get("direction","neutral"))
    if bot_type == "futures_martingale":
        d = str(agg.get("direction","neutral"))
        if d == "neutral":
            return str(agg.get("bias","neutral"))
        return d
    if bot_type == "dca_bot":
        return "long"
    if bot_type == "futures_combo":
        return "hedge"
    return "neutral"

def _mode(venue: str, direction: str) -> tuple[str, str]:
    if venue == "spot":
        return ("oneway", "cash")
    if direction == "hedge":
        return ("hedge", "isolated")
    return ("oneway", "isolated")

def _params(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    global_sent: float,
    direction: str,
    taker_fee_bps: float,
    direction_bias: str,
    direction_bias_strength: float,
    atr_pct_for_grid: float | None,
    cost_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Use a slower volatility proxy for risk sizing/steps (1h ATR% if available).
    atr_pct_1m = float(f.get("atr_pct") or 0.0)
    atr_pct_1h = float(f.get("_atr_pct_1h") or 0.0)
    atr_pct_slow = atr_pct_1h if atr_pct_1h > 0 else atr_pct_1m
    # Grid spacing uses the TF-specific ATR% if provided (preferred), else the slow proxy.
    atr_pct = float(atr_pct_for_grid) if atr_pct_for_grid is not None else atr_pct_slow
    cost_model = dict(cost_model or {})

    risk_per_trade = 0.003 if atr_pct_slow < 0.01 else 0.002
    if global_sent < -0.4:
        risk_per_trade *= 0.7

    if bot_type in ("spot_grid", "futures_grid"):
        total_cost_bps = float(cost_model.get("total_cost_bps") or 0.0)

        base_step_pct = atr_pct * 100.0 * 0.6
        min_step_pct = max(0.08, (total_cost_bps * 1.2) / 100.0)
        grid_spacing_pct = float(_clamp(max(base_step_pct, min_step_pct), 0.08, 2.5))

        span_target_pct = float(_clamp(atr_pct * 100.0 * 25.0, 1.0, 12.0))

        levels = int(round(span_target_pct / grid_spacing_pct)) + 1
        levels = int(_clamp(levels, 6, 60))

        leverage = 1
        if venue == "linear":
            leverage = 3
            if global_sent < -0.4:
                leverage = max(1, leverage - 1)

        p = float(f.get("price") or 0.0)
        span_pct = float(grid_spacing_pct * max(1, (levels - 1)))
        half = span_pct / 2.0

        # directional skew in range selection
        # long bias → range extends more to the upside (higher upper_mul)
        # short bias → range extends more to the downside (higher lower_mul)
        if direction == "long":
            lower_mul, upper_mul = 0.80, 1.20
        elif direction == "short":
            lower_mul, upper_mul = 1.20, 0.80
        else:
            lower_mul, upper_mul = 1.0, 1.0

        lower_pct = float(_clamp(half * lower_mul, 0.25, 25.0))
        upper_pct = float(_clamp(half * upper_mul, 0.25, 25.0))

        price_range_lower = p * (1.0 - lower_pct / 100.0) if p else None
        price_range_upper = p * (1.0 + upper_pct / 100.0) if p else None

        return {
            "bybit_category": "Spot Grid Bot" if bot_type == "spot_grid" else "Futures Grid Bot",
            "direction_mode": direction,  # neutral / long / short
            "direction_bias": direction_bias,  # long / short
            "direction_bias_strength": float(_clamp(direction_bias_strength, 0.0, 1.0)),
            "atr_pct_used": atr_pct,
            "grid_spacing_pct": grid_spacing_pct,
            "grid_spacing_floor_pct": min_step_pct,
            "grid_levels": levels,
            "span_target_pct": span_target_pct,
            "range_span_pct_total": span_pct,
            "total_cost_bps": total_cost_bps,
            "cost_model": cost_model,
            "leverage": leverage,
            "price_range_lower": price_range_lower,
            "price_range_upper": price_range_upper,
            "investment_risk_per_trade": risk_per_trade,
            "notes": "Levels/spacing рассчитываются по ATR% старшего ТФ (если доступно), с учётом cost-floor. Диапазон: price_range_lower/upper.",
        }

    if bot_type == "dca_bot":
        step_pct = float(_clamp(atr_pct_slow * 100.0 * 0.7, 0.2, 2.0))
        max_orders = 6 if global_sent >= -0.4 else 4
        return {
            "bybit_category": "DCA Bot",
            "direction": "long",
            "dca_step_pct": step_pct,
            "max_orders": max_orders,
            "take_profit_mode": "avg_entry_plus_atr",
            "cost_model": cost_model,
            "investment_risk_per_trade": risk_per_trade,
        }

    if bot_type == "futures_martingale":
        leverage = 2 if global_sent < 0 else 3
        step_pct = float(_clamp(atr_pct_slow * 100.0 * 0.9, 0.3, 2.5))
        max_steps = 5 if global_sent >= -0.4 else 4
        return {
            "bybit_category": "Futures Martingale",
            "direction": direction,  # actual computed direction (long/short/neutral)
            "step_pct": step_pct,
            "max_steps": max_steps,
            "leverage": leverage,
            "cost_model": cost_model,
            "investment_risk_per_trade": risk_per_trade * 0.7,
            "warning": "Мартингейл сильно увеличивает риск. Используйте только при высокой согласованности направления.",
        }

    if bot_type == "futures_combo":
        return {
            "bybit_category": "Futures Combo",
            "mode": "hedge",
            "allocation": {"leg1": 0.6, "leg2": 0.4},
            "cost_model": cost_model,
            "investment_risk_per_trade": risk_per_trade * 0.6,
            "notes": "Комбо трактуем как hedge/carry подсказку. Полноценный PnL двух ног в проекте пока не моделируется.",
        }

    return {"investment_risk_per_trade": risk_per_trade, "cost_model": cost_model}

def _expected_rr(bot_type: str, f: dict[str, Any]) -> float:
    atr_pct_1m = float(f.get("atr_pct") or 0.0)
    atr_pct_1h = float(f.get("_atr_pct_1h") or 0.0)
    atr_pct = atr_pct_1h if atr_pct_1h > 0 else atr_pct_1m
    if bot_type in ("spot_grid","futures_grid"):
        return float(_clamp(1.2 - 8*atr_pct, 0.6, 2.0))
    if bot_type == "dca_bot":
        return float(_clamp(1.3 - 6*atr_pct, 0.7, 2.2))
    if bot_type == "futures_martingale":
        return float(_clamp(1.1 - 10*atr_pct, 0.5, 1.6))
    if bot_type == "futures_combo":
        return 1.0
    return 1.0

def _score(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    taker_fee_bps: float,
    global_sent: float,
    cost_model: dict[str, Any] | None = None,
) -> tuple[float, float, dict[str, Any]]:
    # Use multi-TF *trendiness* (unsigned) from direction_agg when available.
    # IMPORTANT: abs(direction_strength) is NOT trendiness; it is the magnitude of the signed direction score.
    dir_agg_f = f.get("_direction_agg") or {}
    t_multi = dir_agg_f.get("trendiness")
    trend = float(_clamp(float(t_multi) if t_multi is not None else float(f.get("trend_strength") or 0.0), 0.0, 1.0))
    # Range score is the complement of trendiness.
    rng = max(0.0, 1.0 - trend)

    # Volatility for scoring/risk should use a slower proxy (1h ATR% if available).
    atr_pct_1m = float(f.get("atr_pct") or 0.0)
    atr_pct_1h = float(f.get("_atr_pct_1h") or 0.0)
    atr_pct = atr_pct_1h if atr_pct_1h > 0 else atr_pct_1m
    cost_model = dict(cost_model or {})
    spread = cost_model.get("spread_bps", f.get("spread_bps"))
    spread = float(spread) if spread is not None else 8.0

    cost_bps = float(cost_model.get("total_cost_bps") or (spread + taker_fee_bps))
    # Use a softer but economically consistent penalty based on the full expected round-trip cost,
    # including slippage/funding when available. This avoids optimistic scores on expensive setups.
    cost_penalty = _clamp(cost_bps / 60.0, 0.0, 1.0)

    sent = float(global_sent)
    pos, neg = [], []
    def add_pos(name, val, w, txt): pos.append({"feature": name, "value": val, "weight": w, "text": txt})
    def add_neg(name, val, w, txt): neg.append({"feature": name, "value": val, "weight": w, "text": txt})

    rule = 0.0

    if bot_type == "spot_grid":
        # ATR normalizer 0.06 = 1h ATR at which penalty reaches 1.0 (~6% hourly = high for grid)
        rule = 1.4*rng - 1.0*trend - 0.6*_clamp(atr_pct/0.06, 0.0, 2.0) + 0.2*max(-0.5, min(0.5, sent))
        add_pos("range_score", rng, 1.4, "флет/диапазон подходит для Spot Grid")
        add_neg("trend_strength", trend, -1.0, "сильный тренд опасен для grid")
        add_neg("atr_pct", atr_pct, -0.6, "высокая волатильность ухудшает grid")
        add_pos("effective_sentiment", sent, 0.2, "сентимент влияет на риск-режим")
    elif bot_type == "futures_grid":
        # ATR normalizer 0.06: same scale as spot_grid (both receive 1h ATR)
        rule = 1.2*rng - 0.9*trend - 0.7*_clamp(atr_pct/0.06, 0.0, 2.0) + 0.2*sent
        add_pos("range_score", rng, 1.2, "флет подходит для Futures Grid")
        add_neg("trend_strength", trend, -0.9, "тренд ломает сетку")
        add_neg("atr_pct", atr_pct, -0.7, "волатильность повышает риск ликвидации")
        add_pos("effective_sentiment", sent, 0.2, "сентимент учитывается")
    elif bot_type == "dca_bot":
        # ATR normalizer 0.12: DCA is more tolerant of volatility (spot accumulation)
        rule = 0.4 + 0.5*_clamp(0.5 + sent, 0.0, 1.0) - 0.7*_clamp(atr_pct/0.12, 0.0, 2.0)
        add_pos("effective_sentiment", sent, 0.5, "нейтральный/позитивный сентимент поддерживает DCA")
        add_neg("atr_pct", atr_pct, -0.7, "высокая волатильность повышает риск просадки")
    elif bot_type == "futures_martingale":
        # Add directional coherence bonus: martingale profits only when direction is clear
        dir_info = f.get("_direction_agg", {}) if hasattr(f, "get") else {}
        dir_coherence = float((dir_info.get("coherence") or 0.5))
        dir_strength = float(((dir_info.get("strength") or {}).get("all", 0.0) if isinstance(dir_info.get("strength"), dict) else dir_info.get("strength", 0.0)))
        # ATR normalizer 0.06: matches grid scale (martingale blocked above 5% 1h ATR anyway)
        rule = 0.8*rng - 0.8*_clamp(atr_pct/0.06, 0.0, 2.0) + 0.4*_clamp(sent+0.2, 0.0, 1.0) - 0.2*trend + 0.3*dir_coherence*dir_strength
        add_pos("range_score", rng, 0.8, "мартингейл только в диапазоне")
        add_neg("atr_pct", atr_pct, -0.8, "волатильность опасна для мартингейла")
        add_pos("effective_sentiment", sent, 0.4, "негативный сентимент блокирует мартингейл")
        add_neg("trend_strength", trend, -0.2, "тренд увеличивает риск")
        add_pos("direction_coherence", dir_coherence, 0.3, "согласованность направления критична для мартингейла")
    elif bot_type == "futures_combo":
        # No unconditional baseline: combo requires either elevated ATR or negative sentiment to be justified.
        # ATR normalizer 0.06: bonus reaches max at ~12% 1h ATR (extreme vol = hedge valuable).
        rule = 0.7*_clamp(-sent, 0.0, 1.0) + 0.4*_clamp(atr_pct/0.06, 0.0, 2.0)
        add_pos("effective_sentiment", sent, 0.7, "risk-off сентимент => комбо/хедж")
        add_pos("atr_pct", atr_pct, 0.4, "рост волатильности => хеджирование")

    raw = rule - 0.35 * cost_penalty  # was 0.70 — much softer penalty
    # Divide by 1.5 (was 2.2) for more polarized [-1, +1] scores
    score = float(_clamp(raw / 1.5, -1.0, 1.0))
    # Scale raw by 2.5 before sigmoid so conf spans [0.1, 0.9] instead of [0.45, 0.55]
    conf0 = float(_clamp(_sigmoid(raw * 2.5), 0.0, 1.0))
    reasons = {
        "summary": "Рекомендация в терминах Bybit Trading Bot (Scenario B). Направление определяется голосованием индикаторов на 15m/30m/1h/4h/1d. Сентимент — multi-horizon EWMA (1h/6h/1d/7d) с консолидацией risk_on/off/neutral. Уверенность калибруется на фактических outcome-метках только там, где метка отражает механику стратегии; для futures_combo confidence intentionally remains heuristic because the project does not model full two-leg PnL.",
        "top_positive_factors": sorted(pos, key=lambda x: abs(x["weight"]), reverse=True)[:5],
        "top_negative_factors": sorted(neg, key=lambda x: abs(x["weight"]), reverse=True)[:5],
        "cost_model": {**cost_model, "spread_bps": spread, "taker_fee_bps": taker_fee_bps, "total_cost_bps": cost_bps},
        "effective_sentiment": sent,
    }
    return score, conf0, reasons

# ── Persistence gate state ───────────────────────────────────────────────────
# Tracks consecutive recommended cycles for the SAME logical signal.
# The original implementation keyed only by (venue, symbol, bot_type) and therefore
# could accidentally confirm a freshly flipped short using a previous long signal.
# We include direction in the signature and require a consecutive-cycle hit within
# an interval-derived freshness window.
_prev_recommended: dict[tuple, dict[str, int]] = {}
PERSISTENCE_BOTS = {"futures_martingale", "dca_bot"}  # bots that need 2-cycle confirmation


def _fit_global_logreg(conn, min_samples: int) -> LogRegScaler:
    """Fit global LogReg+Platt calibrator on all outcome rows."""
    rows = db.get_outcomes_with_recs(conn, limit=6000)
    return fit_logreg(rows, min_samples=min_samples)


def _fit_bot_logregs(conn, min_samples: int) -> dict[str, LogRegScaler]:
    """Fit one LogReg+Platt per bot_type."""
    from collections import defaultdict
    rows = db.get_outcomes_with_recs(conn, limit=8000)
    data: dict[str, list] = defaultdict(list)
    for row in rows:
        data[row["bot_type"]].append(row)

    result: dict[str, LogRegScaler] = {}
    for bt, bt_rows in data.items():
        model = fit_logreg(bt_rows, min_samples=min_samples)
        if model.fitted:
            save_logreg_to_db(conn, BOT_CALIB_KEYS.get(bt, f"logreg_{bt}_v1"), model)
        result[bt] = model
    return result


def _load_or_fit_global_logreg(conn, min_samples: int) -> LogRegScaler:
    """Load global calibrator; re-fit if missing or older than CALIB_REFIT_INTERVAL_SEC."""
    import time as _time
    saved = load_logreg_from_db(conn, GLOBAL_LOGREG_KEY)
    if saved and saved.fitted:
        if int(_time.time()) - saved.saved_ts < CALIB_REFIT_INTERVAL_SEC:
            return saved
    model = _fit_global_logreg(conn, min_samples=min_samples)
    if model.fitted:
        save_logreg_to_db(conn, GLOBAL_LOGREG_KEY, model)
    elif saved and saved.fitted:
        return saved  # keep stale if not enough data yet
    return model


def _load_or_fit_bot_logregs(conn, min_samples: int) -> dict[str, LogRegScaler]:
    """Load per-bot calibrators; re-fit stale or missing ones."""
    import time as _time
    now = int(_time.time())
    calibrators: dict[str, LogRegScaler] = {}
    needs_refit: list[str] = []

    for bt, key in BOT_CALIB_KEYS.items():
        saved = load_logreg_from_db(conn, key)
        if saved and saved.fitted:
            if now - saved.saved_ts < CALIB_REFIT_INTERVAL_SEC:
                calibrators[bt] = saved
                continue
        calibrators[bt] = saved if (saved and saved.fitted) else LogRegScaler(fitted=False)
        needs_refit.append(bt)

    if needs_refit:
        fitted = _fit_bot_logregs(conn, min_samples)
        for bt in needs_refit:
            if bt in fitted and fitted[bt].fitted:
                calibrators[bt] = fitted[bt]

    return calibrators


def _raw_direction_confidence(direction_agg: dict[str, Any]) -> float:
    """Monotonic signal for directional success probability.

    Use raw direction_confidence (0..1) rather than the signed aggregate score.
    A signed score is unsuitable for 1D Platt calibration because successful shorts
    naturally have negative scores and get mixed together with failed longs.
    """
    x = direction_agg.get("direction_confidence")
    if x is None:
        # Fallback: derive from unsigned strength if raw confidence is absent.
        x = (direction_agg.get("strength") or {}).get("all", 0.0)
    return float(_clamp(float(x), 0.0, 1.0))


def _fit_direction_calibrator(conn, min_samples: int) -> PlattScaler:
    """Fit direction calibrator on futures_martingale outcomes.

    We calibrate the *raw direction confidence* (or unsigned strength fallback),
    not the signed aggregate score. This preserves symmetry between strong longs
    and strong shorts and makes the resulting value a true probability-like metric.
    """
    rows = db.get_outcomes_with_recs(conn, limit=5000)
    xs, ys = [], []
    for row in rows:
        if row["bot_type"] != "futures_martingale":
            continue
        d = (row.get("reasons") or {}).get("direction_agg") or {}
        if str(d.get("direction") or "neutral") == "neutral":
            continue
        xs.append(_raw_direction_confidence(d))
        ys.append(int(row["success"]))
    return fit_platt(xs, ys, min_samples=min_samples) if len(xs) >= min_samples else PlattScaler(fitted=False)


def _load_or_fit_direction_calibrator(conn, min_samples: int) -> PlattScaler:
    """Load direction calibrator; re-fit if missing or older than CALIB_REFIT_INTERVAL_SEC."""
    import time as _time
    key = "platt_direction_v3"
    saved = load_platt_from_db(conn, key)
    if saved and saved.fitted:
        if int(_time.time()) - saved.saved_ts < CALIB_REFIT_INTERVAL_SEC:
            return saved
    scaler = _fit_direction_calibrator(conn, min_samples=min_samples)
    if scaler.fitted:
        save_platt_to_db(conn, key, scaler)
    elif saved and saved.fitted:
        return saved
    return scaler


def run_recommender_once(conn, settings) -> dict[str, Any]:
    sent_agg = compute_sentiment_agg(conn, scope="global", key="crypto")
    # Use 6h EWMA as the primary numeric sentiment input for scoring
    global_sent = float(sent_agg.get("ewma", {}).get("6h", 0.0))
    # Per-symbol sentiment map: {SYMBOL: float} blended from RSS/Reddit/CoinGecko
    symbol_sent_map: dict[str, tuple[float, int]] = compute_symbol_sentiment_map(conn)

    # LogReg+Platt calibrators (new) — replace legacy Platt-on-score
    global_calibrator  = _load_or_fit_global_logreg(conn, min_samples=settings.calib_min_samples)
    bot_calibrators    = _load_or_fit_bot_logregs(conn, min_samples=settings.calib_min_samples)
    dir_calibrator     = _load_or_fit_direction_calibrator(conn, min_samples=settings.calib_min_samples)
    # Legacy alias — used in PUBLISH log and UI status endpoint
    calibrator = global_calibrator

    features_all: list[dict[str, Any]] = []
    symbol_feature_map: dict[tuple[str,str], dict[str, Any]] = {}
    symbol_ticker_map: dict[tuple[str,str], Any] = {}  # stores trow per (venue,sym)

    ts_now = db.now_ts()  # set here for stale gate use inside feature loop

    # Load BTC 1h closes once — used for beta/correlation calculation per symbol
    btc_1h_rows = db.get_latest_ohlcv(conn, "spot", "BTCUSDT", tf_sec=3600, limit=50)
    if not btc_1h_rows:
        btc_1h_rows = db.get_latest_ohlcv(conn, "linear", "BTCUSDT", tf_sec=3600, limit=50)
    # Reverse to oldest-first for log-return calculations in btc_beta
    btc_1h_closes = [float(r["close"]) for r in reversed(btc_1h_rows)] if btc_1h_rows else []

    for venue in settings.venues:
        symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
        for sym in symbols:
            rows = db.get_latest_ohlcv(conn, venue, sym, tf_sec=60, limit=220)
            trow = db.get_latest_ticker(conn, venue, sym)
            ticker = dict(trow) if trow else None
            # get_latest_ohlcv returns newest-first (ORDER BY ts DESC).
            # compute_features_from_ohlcv and all indicator functions
            # (ma_slope, EMA, RSI, MACD, BB) require oldest-first order.
            f = compute_features_from_ohlcv([dict(r) for r in reversed(rows)], ticker)
            if not f:
                continue

            # ── Stale data gate ──────────────────────────────────────────
            # If newest 1m candle is too old, data is unreliable — skip symbol
            data_age_sec = ts_now - int(f["ts_last"])
            if data_age_sec > settings.stale_data_max_sec:
                db.log_decision(conn, "STALE_DATA_SKIP", None, None, {
                    "venue": venue, "symbol": sym,
                    "age_sec": data_age_sec, "max_sec": settings.stale_data_max_sec,
                })
                continue

            # Multi-timeframe direction voting (15m/30m/1h/4h/1d)
            tf_secs = [15*60, 30*60, 60*60, 240*60, 24*60*60]
            tf_map = {}
            atr_15m = None
            atr_30m = None
            atr_1h = None
            atr_4h = None
            atr_1d = None
            for tf in tf_secs:
                rows_tf = db.get_latest_ohlcv(conn, venue, sym, tf_sec=tf, limit=260 if tf<=3600 else 420)
                if not rows_tf or len(rows_tf) < 80:
                    continue
                # Reverse to oldest-first — get_latest_ohlcv returns newest-first.
                rows_tf_ord = list(reversed(rows_tf))
                closes_tf = [float(r["close"]) for r in rows_tf_ord]
                highs_tf = [float(r["high"]) for r in rows_tf_ord]
                lows_tf = [float(r["low"]) for r in rows_tf_ord]
                info = vote_for_tf(closes_tf, highs_tf, lows_tf)
                tf_map[tf] = info
                if tf == 60*60:
                    atr_1h = float(info.get("atr_pct") or 0.0)
                elif tf == 15*60:
                    atr_15m = float(info.get("atr_pct") or 0.0)
                elif tf == 30*60:
                    atr_30m = float(info.get("atr_pct") or 0.0)
                elif tf == 240*60:
                    atr_4h = float(info.get("atr_pct") or 0.0)
                elif tf == 24*60*60:
                    atr_1d = float(info.get("atr_pct") or 0.0)

            agg = aggregate_direction(tf_map) if tf_map else {"direction":"neutral","bias":"neutral","direction_confidence":0.5,"scores":{"tactical":0,"structural":0,"all":0},"strength":{"tactical":0,"structural":0,"all":0},"coherence":0.5,"regime":"unknown","regime_confidence":0.0,"structural_veto_applied":False,"tf_used":[]}
            f["_direction_agg"] = agg
            f["_atr_pct_1h"] = atr_1h
            f["_atr_pct_15m"] = atr_15m
            f["_atr_pct_30m"] = atr_30m
            f["_atr_pct_4h"] = atr_4h
            f["_atr_pct_1d"] = atr_1d

            # ── BTC beta ─────────────────────────────────────────────────
            if sym != "BTCUSDT" and btc_1h_closes:
                # tf_map stores vote_for_tf dicts, not raw rows — always fetch closes from DB
                _sym_rows = db.get_latest_ohlcv(conn, venue, sym, tf_sec=3600, limit=50)
                sym_1h_closes = [float(r["close"]) for r in reversed(_sym_rows)] if _sym_rows else []
                beta_info = btc_beta(sym_1h_closes, btc_1h_closes, window=24)
            else:
                beta_info = {"correlation": None, "beta": None,
                             "is_btc_driven": False, "independent_signal": True, "window": 0}
            f["_btc_beta"] = beta_info

            ts_f = int(f["ts_last"])
            db.insert_features(conn, venue, sym, ts_f, f)
            features_all.append(f)
            symbol_feature_map[(venue, sym)] = f
            symbol_ticker_map[(venue, sym)] = trow  # save for reco loop

    regime = classify_regime(features_all)
    db.insert_regime(conn, db.now_ts(), regime)

    limits = db.get_active_risk_limits(conn) or settings.risk_limits
    model_version = "bybit-taxonomy-v2"
    # ts_now already set above for stale gate — reuse it

    recs: list[dict[str, Any]] = []

    # Cache risk status once per cycle — avoids 450+ extra DB queries/cycle with 30 symbols.
    _cached_risk_status = _compute_risk_status(conn, limits)

    for (venue, sym), f in symbol_feature_map.items():
        taker_fee_bps = settings.taker_fee_bps_spot if venue == "spot" else settings.taker_fee_bps_linear

        for bot_type in BOT_TYPES_BYBIT:
            if bot_type == "spot_grid" and venue != "spot":
                continue
            if bot_type == "dca_bot" and venue != "spot":
                continue  # Bybit DCA Bot is spot-only
            if bot_type in ("futures_grid","futures_martingale","futures_combo") and venue != "linear":
                continue

            spread_raw = f.get("spread_bps")
            spread = float(spread_raw) if spread_raw is not None else None
            # Risk/scoring volatility proxy: prefer 1h ATR% (from multi-TF direction pass).
            atr_pct_1m = float(f.get("atr_pct") or 0.0)
            atr_pct_1h = float(f.get("_atr_pct_1h") or 0.0)
            atr_pct = atr_pct_1h if atr_pct_1h > 0 else atr_pct_1m

            # ── Liquidity tier — use per-(venue,sym) cached ticker ──
            _trow = symbol_ticker_map.get((venue, sym))
            turnover = float(_trow["turnover24h"]) if _trow and _trow["turnover24h"] else None
            liq_tier = liquidity_tier(turnover)

            # ── Funding rate + OI (futures only) ──
            fr_data  = db.get_latest_funding_rate(conn, sym) if venue == "linear" else None
            oi_rows  = db.get_oi_series(conn, sym, limit=48)  if venue == "linear" else []
            fr_sig   = funding_signal(fr_data["funding_rate"] if fr_data else None)
            oi_sig   = oi_trend(oi_rows)
            direction = _direction(bot_type, f.get('_direction_agg', {}))
            cost_model = _estimate_cost_model(
                bot_type=bot_type,
                venue=venue,
                f=f,
                taker_fee_bps=taker_fee_bps,
                direction=direction,
                funding_rate=(fr_data["funding_rate"] if fr_data else None),
                next_funding_ts=(fr_data["next_funding_ts"] if fr_data else None),
                ts_now=ts_now,
            )
            # Combine OI trend with price direction for final signal
            if oi_sig["trend"] == "growing":
                dir_agg_tmp = f.get("_direction_agg", {})
                price_dir = dir_agg_tmp.get("direction", "neutral")
                if price_dir == "long":
                    oi_sig["signal"] = "bullish"   # price up + OI up → healthy long
                elif price_dir == "short":
                    oi_sig["signal"] = "bearish"   # price down + OI up → shorts piling in
                else:
                    oi_sig["signal"] = "neutral"
            elif oi_sig["trend"] == "falling":
                oi_sig["signal"] = "caution"       # unwinding → reduced conviction
            else:
                oi_sig["signal"] = "neutral"

            # Adaptive sentiment blend: symbol weight grows with number of data points.
            # Few points (< 5) → 90% global / 10% symbol — don't amplify noisy signal.
            # Many points (≥ 20) → 50% global / 50% symbol — full trust.
            # MUST be computed before feasibility checks that reference effective_sent
            _sym_entry = symbol_sent_map.get(sym)
            if _sym_entry is not None:
                sym_sent, _sym_n = _sym_entry
                _sym_weight = float(_clamp(_sym_n / 20.0, 0.1, 0.5))
                effective_sent = (1.0 - _sym_weight) * global_sent + _sym_weight * sym_sent
            else:
                sym_sent = None
                _sym_n = 0
                _sym_weight = 0.0
                effective_sent = global_sent

            feasibility_blocks = []

            # ── Data completeness / liquidity gates ──
            if turnover is None:
                feasibility_blocks.append({"code": "LIQUIDITY_UNKNOWN",
                    "msg": "нет turnover24h — ликвидность не подтверждена, cost-model ненадёжен"})
            elif liq_tier == "micro":
                feasibility_blocks.append({"code": "LIQUIDITY_TOO_LOW",
                    "msg": f"turnover24h={turnover} USD < $500K — торговля на неликвидном символе искажает fills/статистику"})
            if liq_tier == "low" and bot_type in ("futures_martingale", "futures_combo"):
                feasibility_blocks.append({"code": "LIQUIDITY_LOW_FUTURES",
                    "msg": f"turnover24h={turnover} USD < $2M — мартингейл/комбо запрещён на низколиквидных"})
            if spread is None:
                feasibility_blocks.append({"code": "SPREAD_UNKNOWN",
                    "msg": "bid/ask отсутствуют — нельзя надёжно оценить execution cost"})

            # ── Funding rate gate (futures only) ──
            if venue == "linear" and fr_sig["signal"] == "bearish" and fr_sig["value"] is not None:
                if float(fr_sig["value"]) > 0.0006:   # > 0.06% per 8h = ~66% annualized — extreme
                    feasibility_blocks.append({"code": "FUNDING_EXTREME",
                        "msg": f"funding_rate={fr_sig['value']:.4f} ({fr_sig['annualized_pct']:.0f}%/year) — crowded long, высокий carry-cost"})

            if bot_type in ("spot_grid","futures_grid") and spread is not None and spread > 14.0:
                feasibility_blocks.append({"code":"SPREAD_TOO_WIDE", "msg": f"spread_bps={spread:.2f} слишком широкий для grid"})
            # Use multi-TF trendiness for gate (same source as _score uses)
            _dir_agg_gate = f.get("_direction_agg") or {}
            _multitf_trendiness = float(_dir_agg_gate.get("trendiness") or 0.0)
            if bot_type in ("spot_grid","futures_grid") and _multitf_trendiness > 0.60:
                feasibility_blocks.append({"code":"TREND_TOO_STRONG", "msg": f"multi_tf_trendiness={_multitf_trendiness:.2f} слишком сильный тренд для grid"})
            dir_tmp = f.get("_direction_agg", {})
            dir_conf = float(dir_tmp.get("direction_confidence", 0.5))

            # If symbol is highly correlated to BTC, direction is less independent
            beta_info = f.get("_btc_beta", {})
            if beta_info.get("is_btc_driven") and sym != "BTCUSDT":
                dir_conf = float(_clamp(dir_conf * 0.88, 0.0, 0.99))
            # Block threshold 0.05 = 5% 1h ATR. Old value 0.018 was calibrated for 1m ATR
            # and blocked ALL symbols since typical 1h ATR for small caps is 3–8%.
            if bot_type == "futures_martingale" and (atr_pct > 0.05 or sent_agg.get("flags", {}).get("panic") or (sent_agg.get("regime") == "risk_off" and sent_agg.get("strength", 0.0) >= 0.35) or effective_sent < -0.45 or dir_conf < 0.65):
                code = "DIR_CONF_TOO_LOW" if dir_conf < 0.65 else "MARTINGALE_BLOCKED"
                feasibility_blocks.append({"code": code, "msg": f"atr_pct={atr_pct:.4f}, sentiment6h={effective_sent:.2f}, dir_conf={dir_conf:.2f} => запрет"})
            if bot_type == "dca_bot" and (sent_agg.get("flags", {}).get("panic") or effective_sent < -0.70):
                feasibility_blocks.append({"code":"DCA_BLOCKED_PANIC", "msg": f"sentiment={effective_sent:.2f} panic => запрет"})

            # ── Risk gate — uses cached risk_status (computed once per cycle) ──
            risk_blocks = gate_candidate(conn, venue, sym, limits, cached_status=_cached_risk_status)
            feasibility_blocks.extend(risk_blocks)

            score, conf0, reasons = _score(
                bot_type,
                venue,
                f,
                taker_fee_bps=taker_fee_bps,
                global_sent=effective_sent,
                cost_model=cost_model,
            )

            # ── Funding + OI score adjustments ──
            if venue == "linear":
                score = _clamp(score + _funding_score_adjustment(direction, fr_sig), -1.0, 1.0)

            conf_raw = float(conf0)
            # ── Two-stage calibration: LogReg(features) → Platt ──────────────────
            # Build a temporary reasons-like dict so extract_features() can work
            # with the current (not yet stored) feature set.
            # ── Compute dir_conf_cal FIRST so extract_features gets the calibrated
            # value — matching what was stored in reasons_json during training.
            # (Previously dir_conf_cal was computed after extract_features, causing
            # a train/inference skew: model trained on calibrated conf, inferred on raw.)
            _dtmp_pre = dict(f.get("_direction_agg", {}))
            _xdir_pre = _raw_direction_confidence(_dtmp_pre)
            _dir_conf_pre = dir_calibrator.predict(_xdir_pre) if dir_calibrator.fitted else _xdir_pre
            _dir_agg_for_cal = dict(_dtmp_pre)
            _dir_agg_for_cal["direction_confidence_calibrated"] = _dir_conf_pre

            _reasons_for_cal = {
                "effective_sentiment": effective_sent,
                "cost_model": cost_model,
                "direction_agg": _dir_agg_for_cal,  # includes calibrated dir_conf
                "top_positive_factors": (reasons.get("top_positive_factors") or []),
                "top_negative_factors": (reasons.get("top_negative_factors") or []),
            }
            _row_for_cal = {"score": score, "reasons": _reasons_for_cal, "success": 0}
            _fv = extract_features(_row_for_cal)

            bot_cal = bot_calibrators.get(bot_type)
            if bot_type == "futures_combo":
                # Combo/hedge lacks full two-leg execution & PnL accounting in this project.
                # Using a fitted probability here would create false statistical certainty from
                # a proxy label, so keep confidence explicitly heuristic.
                conf_cal = float(conf0)
                _cal_source = "raw_proxy"
                _active_cal = None
            elif bot_cal and bot_cal.fitted and len(bot_cal.coef) > 0 and _fv is not None:
                conf_cal = float(bot_cal.predict(_fv))
                _cal_source = "bot_logreg"
                _active_cal = bot_cal
            elif bot_cal and bot_cal.fitted:
                conf_cal = float(bot_cal.predict_score_only(score))
                _cal_source = "bot_platt"
                _active_cal = bot_cal
            elif global_calibrator.fitted and len(global_calibrator.coef) > 0 and _fv is not None:
                conf_cal = float(global_calibrator.predict(_fv))
                _cal_source = "global_logreg"
                _active_cal = global_calibrator
            elif global_calibrator.fitted:
                conf_cal = float(global_calibrator.predict_score_only(score))
                _cal_source = "global_platt"
                _active_cal = global_calibrator
            else:
                conf_cal = float(conf0)
                _cal_source = "raw"
                _active_cal = None
            # Adaptive blend: calibration weight grows with n_samples.
            # At n=0 (unfitted): 10% calibrated / 90% raw — mostly raw sigmoid.
            # At n=80 (min_samples): ~22% calibrated.
            # At n≥300: 50% calibrated — full trust in the model.
            # This prevents the cold-start problem where an undertrained calibrator
            # with degenerate WR drags all confidence toward an extreme value.
            _n_cal = (_active_cal.n_samples if _active_cal is not None and _active_cal.fitted else 0)
            _cal_weight = float(_clamp(_n_cal / 300.0, 0.0, 1.0)) * 0.40 + 0.10
            conf = float(_clamp((1.0 - _cal_weight) * conf_raw + _cal_weight * conf_cal, 0.0, 1.0))

            # Context completeness penalty — reduce confidence when key signals are missing.
            # The system already falls back gracefully; this makes the uncertainty explicit.
            _ctx_mult = 1.0
            if not f.get("_atr_pct_1h"):          _ctx_mult *= 0.92  # no 1h ATR
            if venue == "linear" and oi_sig.get("oi_now") is None: _ctx_mult *= 0.96  # no OI data
            if venue == "linear" and fr_sig.get("value") is None:  _ctx_mult *= 0.98  # no funding data
            _dir_tf_count = len((f.get("_direction_agg") or {}).get("tf_used") or [])
            if _dir_tf_count < 3:                  _ctx_mult *= 0.93  # sparse TF coverage
            if _ctx_mult < 1.0:
                conf = float(_clamp(conf * _ctx_mult, 0.0, 1.0))

            # OI unwinding → reduce confidence
            if venue == "linear" and oi_sig["signal"] == "caution":
                conf = float(_clamp(conf * 0.88, 0.0, 1.0))
            if bot_type == "futures_combo":
                conf = float(min(conf, 0.68))

            expected_rr = _expected_rr(bot_type, f)
            account_mode, margin_mode = _mode(venue, direction)

            blocks = list(feasibility_blocks)  # risk_blocks already included via feasibility_blocks.extend()

            status = "recommended"
            if blocks:
                status = "blocked"
            elif score < settings.min_score_to_recommend:
                status = "no_trade"
            elif settings.require_conf_gate and conf < settings.min_conf_to_recommend:
                status = "no_trade"

            # Persistence gate for directional bots: require 2 consecutive cycles
            # for the SAME signal signature. This avoids confirming a fresh short with a stale long.
            import time as _rtime
            if status == "recommended" and bot_type in PERSISTENCE_BOTS:
                _pkey = (venue, sym, bot_type, direction)
                _now_ts = int(_rtime.time())
                _fresh_gap = max(45, int(settings.reco_interval_sec * 2.5))
                _state = _prev_recommended.get(_pkey) or {"ts": 0, "count": 0}
                if _now_ts - int(_state.get("ts", 0)) <= _fresh_gap:
                    _state = {"ts": _now_ts, "count": int(_state.get("count", 0)) + 1}
                else:
                    _state = {"ts": _now_ts, "count": 1}
                _prev_recommended[_pkey] = _state
                # remove stale opposite-direction state for same symbol/bot
                for _other_dir in ("long", "short", "neutral", "hedge"):
                    _other_key = (venue, sym, bot_type, _other_dir)
                    if _other_key != _pkey:
                        _prev_recommended.pop(_other_key, None)
                if int(_state.get("count", 0)) < 2:
                    status = "suppressed"
            elif status != "recommended":
                for _other_dir in ("long", "short", "neutral", "hedge"):
                    _prev_recommended.pop((venue, sym, bot_type, _other_dir), None)

            risk_score = float(_clamp(atr_pct/0.10, 0.0, 1.0))

            params = _params(
                bot_type,
                venue,
                f,
                global_sent=effective_sent,
                direction=direction,
                taker_fee_bps=taker_fee_bps,
                direction_bias=str(f.get("_direction_agg", {}).get("bias", "neutral")),
                direction_bias_strength=float((f.get("_direction_agg", {}).get("strength", {}) or {}).get("all", 0.0) if isinstance(f.get("_direction_agg", {}).get("strength"), dict) else float(f.get("_direction_agg", {}).get("strength", 0.0))),
                atr_pct_for_grid=f.get("_atr_pct_1h"),
                cost_model=cost_model,
            )
            # Add execution guide for UI "Details" panel.
            params["trade_plan"] = _build_trade_plan(bot_type, venue, f, direction, params, cost_model=cost_model)

            rec_id = f"R-{ts_now}-{venue}-{sym}-{bot_type}-{secrets.token_hex(4)}"
            reasons2 = dict(reasons)
            reasons2["regime"] = regime
            reasons2["risk_checks"] = {"passed": len(blocks)==0, "blocks": blocks}
            reasons2["sentiment_agg"] = sent_agg
            reasons2["btc_beta"] = f.get("_btc_beta", {})
            reasons2["liquidity"] = {
                "tier": liq_tier,
                "turnover24h_usd": turnover,
            }
            if venue == "linear":
                reasons2["funding"] = {
                    **fr_sig,
                    "direction": direction,
                    "directional_funding_bps_8h": float(cost_model.get("directional_funding_bps_8h") or 0.0),
                    "expected_funding_bps": float(cost_model.get("expected_funding_bps") or 0.0),
                    "expected_funding_events": int(cost_model.get("expected_funding_events") or 0),
                    "next_funding_ts": cost_model.get("next_funding_ts"),
                }
                reasons2["open_interest"] = oi_sig
            reasons2["feature_snapshot"] = dict(_feature_snapshot)
            reasons2["symbol_sentiment"] = {
                "value": float(sym_sent) if sym_sent is not None else None,
                "effective": float(effective_sent),
                "global": float(global_sent),
                "blended": sym_sent is not None,
                "symbol_weight": float(_sym_weight),
                "global_weight": float(1.0 - _sym_weight),
                "n_points": int(_sym_n),
            }
            # Calibrate direction confidence separately (if model fitted)
            dtmp = dict(f.get("_direction_agg", {}))  # copy — don't mutate f in-place across bot_type loop
            xdir = _raw_direction_confidence(dtmp)
            dir_conf_cal = dir_calibrator.predict(xdir) if dir_calibrator.fitted else xdir
            dtmp["direction_confidence_calibrated"] = dir_conf_cal
            dtmp["direction_confidence_model"] = {"type":"platt_scaling","fitted": dir_calibrator.fitted, "a": getattr(dir_calibrator,"a",None), "b": getattr(dir_calibrator,"b",None)}
            reasons2["direction_agg"] = dtmp
            # confidence_model reflects the calibrator ACTUALLY used (_cal_source set above).
            # Previously used _bot_cal_info presence to fill fields, which gave wrong
            # fitted/a/b when bot_cal existed-but-unfitted and global was used instead.
            reasons2["confidence_model"] = {
                "source": _cal_source,
                "type": "logreg_platt_v1" if _cal_source in ("bot_logreg", "global_logreg") else (
                    "platt_only" if _cal_source in ("bot_platt", "global_platt") else ("raw_proxy" if _cal_source == "raw_proxy" else "raw")
                ),
                "fitted": _active_cal.fitted if _active_cal is not None else False,
                "n_samples": _active_cal.n_samples if _active_cal is not None and _active_cal.fitted else 0,
                "logreg_active": _cal_source in ("bot_logreg", "global_logreg"),
                "a": getattr(getattr(_active_cal, "platt", None), "a", None) if _active_cal else None,
                "b": getattr(getattr(_active_cal, "platt", None), "b", None) if _active_cal else None,
            }

            recs.append({
                "rec_id": rec_id,
                "ts": ts_now,
                "venue": venue,
                "symbol": sym,
                "bot_type": bot_type,
                "direction": direction,
                "account_mode": account_mode,
                "margin_mode": margin_mode,
                "score": float(score),
                "confidence": float(conf),
                "expected_rr": float(expected_rr),
                "risk_score": float(risk_score),
                "params": params,
                "reasons": reasons2,
                "blocks": blocks,
                "status": status,
                "ttl_sec": max(180, settings.collect_interval_sec * 15),  # at least 15 collect cycles
                "model_version": model_version,
                "features_ref_ts": int(f["ts_last"]),
            })

    if recs:
        # Publish only one best recommendation per (venue, symbol).
        # Others are stored as 'suppressed' for audit/debug.
        STATUS_PRIORITY = {"recommended": 0, "blocked": 1, "no_trade": 2, "suppressed": 3}
        best_map: dict[tuple[str, str], dict[str, Any]] = {}
        for r in recs:
            key = (r["venue"], r["symbol"])
            cur = best_map.get(key)
            if cur is None:
                best_map[key] = r
                continue
            r_pri  = STATUS_PRIORITY.get(r["status"], 9)
            c_pri  = STATUS_PRIORITY.get(cur["status"], 9)
            if r_pri < c_pri:
                best_map[key] = r  # better status wins unconditionally
            elif r_pri == c_pri:
                if r["confidence"] > cur["confidence"] or (
                    r["confidence"] == cur["confidence"] and r["score"] > cur["score"]
                ):
                    best_map[key] = r

        for r in recs:
            key = (r["venue"], r["symbol"])
            if best_map.get(key, {}).get("rec_id") != r["rec_id"]:
                # Only suppress 'recommended' recs — preserve 'blocked'/'no_trade' for audit
                if r["status"] == "recommended":
                    r["status"] = "suppressed"

        db.insert_recommendations(conn, recs)
        db.log_decision(
            conn,
            "PUBLISH",
            None,
            None,
            {
                "count_all": len(recs),
                "count_best": len(best_map),
                "model_version": model_version,
                "regime": regime,
                "global_sentiment_6h": global_sent,
                "sentiment_regime": sent_agg.get("regime"),
                "sentiment_strength": sent_agg.get("strength"),
                "calibrator_fitted": calibrator.fitted,
            },
        )

    return {"regime": regime, "count": len(recs), "global_sentiment_6h": global_sent,
            "sentiment_regime": sent_agg.get("regime"),
            "sentiment_strength": sent_agg.get("strength"), "calibrator_fitted": calibrator.fitted}
