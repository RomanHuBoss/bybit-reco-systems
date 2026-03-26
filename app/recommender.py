from __future__ import annotations

import math
import secrets
import time
from typing import Any

from . import db
from .features import compute_features_from_ohlcv, liquidity_tier, funding_signal, oi_trend, btc_beta
from .regime import classify_regime
from .risk import gate_candidate, compute_risk_status as _compute_risk_status
from .direction import vote_for_tf, aggregate_direction
from .sentiment_features import compute_sentiment_agg, compute_symbol_sentiment_map
from .shock_guard import compute_market_shock, apply_market_shock_gate, compute_symbol_fast_veto, APP_CONFIG_KEY as MARKET_SHOCK_APP_KEY
from .outcomes import BOT_HORIZONS
from .bot_types import SUPPORTED_BOT_TYPES
from .calibration import (
    fit_platt, PlattScaler, save_platt_to_db, load_platt_from_db, BOT_CALIB_KEYS,
    LogRegScaler, fit_logreg, save_logreg_to_db, load_logreg_from_db,
    extract_features, GLOBAL_LOGREG_KEY, CALIB_REFIT_INTERVAL_SEC,
)
# Note: calibrators use db.get_outcomes_with_recs (single JOIN query) to avoid N+1 pattern

BOT_TYPES_BYBIT = list(SUPPORTED_BOT_TYPES)
MAX_FUNDING_STALENESS_SEC = 60 * 60
MAX_OI_STALENESS_SEC = 3 * 60 * 60
UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS: frozenset[str] = frozenset()

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



def _drop_open_candle(rows: list[dict[str, Any]] | list[Any], tf_sec: int, ts_now: int) -> list[Any]:
    """Remove the latest still-open candle from newest-first OHLCV rows.

    Collector runs more often than candle close boundaries, and Bybit kline payloads
    can include the currently forming candle. Using it in features / direction creates
    unstable recommendations and train-label mismatch. We therefore only score on the
    last fully closed candle for every timeframe.
    """
    if not rows:
        return rows
    try:
        newest_ts = int(rows[0]["ts"])
    except Exception:
        return rows
    if newest_ts <= 0 or tf_sec <= 0:
        return rows
    if ts_now < newest_ts + tf_sec:
        return rows[1:]
    return rows


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
    nfts_out: int | None = None
    if venue == "linear" and fr is not None and horizon_sec > 0:
        now = int(ts_now or 0)
        nfts = int(next_funding_ts or 0)
        funding_interval_sec = 8 * 3600
        # Defensive normalization for legacy/state payloads that may still carry
        # Bybit's millisecond timestamp even if the client was already fixed.
        if nfts > 10**11:
            nfts //= 1000
        if now > 0 and nfts > 0:
            # If the stored next_funding_ts is already in the past (e.g. collector has
            # not refreshed yet after a funding event), roll it forward to the next
            # actual future event instead of charging a stale funding event immediately.
            while nfts <= now:
                nfts += funding_interval_sec
            horizon_end = now + horizon_sec
            if horizon_end >= nfts:
                expected_funding_events = 1 + max(0, (horizon_end - nfts) // funding_interval_sec)
            nfts_out = nfts
        else:
            expected_funding_events = 1 if horizon_sec >= funding_interval_sec else 0
            nfts_out = nfts if nfts > 0 else None
        expected_funding_bps = directional_funding_bps_8h * expected_funding_events

    execution_cost_bps = max(0.0, fee_bps_round_trip + spread_bps_used + slippage_bps)
    net_cost_bps = execution_cost_bps + expected_funding_bps

    return {
        "spread_bps": spread_bps_used,
        "spread_missing": spread_missing,
        "fee_bps_round_trip": fee_bps_round_trip,
        "slippage_bps": float(slippage_bps),
        "execution_cost_bps": float(execution_cost_bps),
        "funding_rate": fr,
        "direction": direction,
        "directional_funding_bps_8h": float(directional_funding_bps_8h),
        "next_funding_ts": int(nfts_out) if nfts_out else (int(next_funding_ts) if next_funding_ts else None),
        "expected_funding_events": int(expected_funding_events),
        "expected_funding_bps": float(expected_funding_bps),
        # Canonical cost floor for scoring / RR / labels must reflect unavoidable
        # execution friction only. Funding carry stays explicit in net_cost_bps.
        "total_cost_bps": float(execution_cost_bps),
        "net_cost_bps": float(net_cost_bps),
        "horizon_sec": int(horizon_sec),
    }


def _funding_score_adjustment(direction: str, fr_sig: dict[str, Any], cost_model: dict[str, Any]) -> float:
    """Event-aware funding adjustment for the heuristic score.

    Funding should only affect the score if the trade horizon is actually expected to
    cross one or more funding events. Otherwise we create an economic signal that the
    execution model never realises. The adjustment is also direction-aware: expensive
    carry should penalise the side that is expected to *pay* funding, while received
    funding can be mildly supportive.
    """
    if direction not in ("long", "short"):
        return 0.0
    if int(cost_model.get("expected_funding_events") or 0) <= 0:
        return 0.0

    expected_bps = float(cost_model.get("expected_funding_bps") or 0.0)
    if expected_bps >= 8.0:
        return -0.08
    if expected_bps >= 4.0:
        return -0.05
    if expected_bps >= 1.5:
        return -0.02
    if expected_bps <= -8.0:
        return 0.05
    if expected_bps <= -3.0:
        return 0.03
    if expected_bps <= -1.0:
        return 0.015

    sig = str(fr_sig.get("signal") or "unknown")
    if direction == "long" and sig == "bullish":
        return 0.01
    if direction == "short" and sig == "bearish":
        return 0.01
    return 0.0


def _extreme_funding_block(direction: str, fr_sig: dict[str, Any], cost_model: dict[str, Any]) -> dict[str, Any] | None:
    """Return a feasibility block when expected funding carry is too expensive.

    `expected_funding_bps` is already direction-aware:
      * positive  -> this side is expected to PAY funding over the label horizon;
      * negative  -> this side is expected to RECEIVE funding.

    The previous implementation hard-blocked only expensive longs, which created a
    directional asymmetry: positive funding could suppress many long ideas, while an
    equally expensive short under negative funding was still allowed through. The gate
    must be keyed off *who pays*, not off the semantic label long/short.
    """
    if direction not in ("long", "short"):
        return None
    if fr_sig.get("value") is None:
        return None
    if int(cost_model.get("expected_funding_events") or 0) <= 0:
        return None

    expected_bps = float(cost_model.get("expected_funding_bps") or 0.0)
    if expected_bps < 6.0:
        return None

    side = "long" if direction == "long" else "short"
    return {
        "code": "FUNDING_EXTREME",
        "msg": f"expected_funding_bps={expected_bps:.2f} over horizon — {side} pays too much funding carry",
    }


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
    def _value_or_default(value: Any, default: float) -> float:
        return float(default if value is None else value)

    liq_map = {"micro": 0.0, "low": 0.33, "medium": 0.67, "high": 1.0, "unknown": 0.5}
    trendiness = abs(float(direction_agg.get("trendiness") or 0.0))
    dir_conf = direction_agg.get("direction_confidence_calibrated")
    if dir_conf is None:
        dir_conf = direction_agg.get("direction_confidence")
    spread_bps = cost_model.get("spread_bps")
    if spread_bps is None:
        spread_bps = cost_model.get("execution_cost_bps") or cost_model.get("total_cost_bps")
    return {
        "range_score": _clamp(1.0 - trendiness, 0.0, 1.0),
        "trend_strength": _clamp(trendiness, 0.0, 1.0),
        "atr_pct_norm": _clamp(float(atr_pct) / 0.10, 0.0, 2.0),
        "effective_sentiment": _clamp(float(effective_sent), -1.0, 1.0),
        "dir_conf": _clamp(_value_or_default(dir_conf, 0.5), 0.0, 1.0),
        "coherence": _clamp(_value_or_default(direction_agg.get("coherence"), 0.5), 0.0, 1.0),
        "spread_bps_norm": _clamp(_value_or_default(spread_bps, 8.0) / 10.0, 0.0, 5.0),
        "score": _clamp(float(score), -1.0, 1.0),
        "oi_4h_norm": _clamp(_value_or_default(oi_sig.get("oi_4h_chg_pct"), 0.0) / 10.0, -3.0, 3.0),
        "funding_norm": _clamp(_value_or_default(cost_model.get("expected_funding_bps"), 0.0) / 20.0, -2.0, 2.0),
        "liq_tier_num": float(liq_map.get(str(liq_tier).lower(), 0.67)),
        "btc_corr": _clamp(_value_or_default(beta_info.get("correlation"), 0.0), -1.0, 1.0),
        "regime_conf": _clamp(_value_or_default(direction_agg.get("regime_confidence"), 0.5), 0.0, 1.0),
    }

def _build_trade_plan(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    direction: str,
    params: dict[str, Any],
    cost_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Human/actionable execution guide shown in the UI 'Details' panel."""

    price = float(f.get("price") or 0.0) or None
    atr_pct_1m = float(f.get("atr_pct") or 0.0)
    atr_pct_15m = float(f.get("_atr_pct_15m") or 0.0)
    atr_pct_1h = float(f.get("_atr_pct_1h") or 0.0)
    atr_pct_4h = float(f.get("_atr_pct_4h") or 0.0)
    atr_pct_slow = atr_pct_1h if atr_pct_1h > 0 else atr_pct_1m
    atr_source = "1h" if atr_pct_1h > 0 else "1m"

    atr_abs_used = (price * atr_pct_slow) if (price is not None and atr_pct_slow > 0) else None
    decision_tfs = {"macro": "1h", "entry": "15m", "monitor": "1m"}
    horizon = {"min_hours": 6, "max_hours": 48}

    d = f.get("_direction_agg") or {}
    regime = str(d.get("regime") or "unknown")
    regime_conf = float(d.get("regime_confidence") or 0.0)
    if regime_conf >= 0.75:
        horizon = {"min_hours": max(1, int(horizon["min_hours"] * 0.8)), "max_hours": int(horizon["max_hours"] * 0.85)}
    elif regime_conf <= 0.35:
        horizon = {"min_hours": int(horizon["min_hours"] * 1.0), "max_hours": int(horizon["max_hours"] * 0.6)}

    lower = params.get("price_range_lower")
    upper = params.get("price_range_upper")
    ks_pad = (0.6 * atr_abs_used) if (atr_abs_used is not None and atr_abs_used > 0) else None
    lower_ks = (float(lower) - ks_pad) if (lower is not None and ks_pad is not None) else None
    upper_ks = (float(upper) + ks_pad) if (upper is not None and ks_pad is not None) else None

    step_pct = params.get("grid_spacing_pct")
    step_abs = (price * float(step_pct) / 100.0) if (price is not None and step_pct is not None) else None
    tp_leg_abs = (0.7 * step_abs) if step_abs is not None else (0.25 * atr_abs_used if atr_abs_used else None)

    plan: dict[str, Any] = {
        "reference_price": _round_price(price, decimals=10),
        "decision_timeframes": decision_tfs,
        "expected_horizon": {**horizon, "basis": "heuristics(bot_type)+regime_confidence", "label_horizon_hours": int(params.get("label_horizon_hours") or (BOT_HORIZONS.get(bot_type, 6 * 3600) // 3600))},
        "volatility": {
            "atr_pct_1m": atr_pct_1m,
            "atr_pct_15m": atr_pct_15m if atr_pct_15m > 0 else None,
            "atr_pct_1h": atr_pct_1h if atr_pct_1h > 0 else None,
            "atr_pct_4h": atr_pct_4h if atr_pct_4h > 0 else None,
            "atr_pct_used": atr_pct_slow,
            "atr_abs_used": _round_price(atr_abs_used, decimals=10) if atr_abs_used is not None else None,
            "atr_source": atr_source,
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
        "levels": {
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
        },
        "close_conditions": [
            "Выход цены за kill_switch (признак пробоя диапазона).",
            "Истечение expected_horizon.max_hours без возврата в диапазон/без набора прибыли.",
            "Рост trendiness/regime='trend' (по direction_agg) — сетку лучше остановить.",
        ],
        "notes": "Ориентиры уровней масштабируются по ATR старшего ТФ (предпочтительно 1h, fallback = 1m). Это подсказка для запуска/контроля бота, а не обещание результата.",
    }

    if venue == "linear" and int(params.get("leverage") or 1) > 1:
        ks = plan["levels"].get("kill_switch") or {}
        span_note = params.get("range_span_pct_total")
        span_str = f"{float(span_note):.2f}" if span_note is not None else "n/a"
        plan["notes"] += (
            f" Для futures_grid с leverage={int(params.get('leverage') or 1)} и span≈{span_str}% проверьте, что liquidation price лежит за пределами kill_switch "
            f"[{ks.get('lower')}, {ks.get('upper')}]."
        )

    return plan


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def _make_factor(feature: str, value: Any, weight: float, msg: str) -> dict[str, Any]:
    return {
        "feature": feature,
        "value": float(value) if isinstance(value, (int, float)) and value is not None else value,
        "weight": float(weight),
        "msg": msg,
        "text": msg,
    }


def _direction(bot_type: str, agg: dict[str, Any]) -> str:
    raw_direction = str((agg or {}).get("direction") or "neutral").lower()
    if bot_type == "spot_grid":
        if raw_direction == "long":
            return "long"
        return "neutral"
    if bot_type == "futures_grid":
        if raw_direction in ("long", "short", "neutral"):
            return raw_direction
        return "neutral"
    return "neutral"


def _stable_range_score(f: dict[str, Any], agg: dict[str, Any]) -> tuple[float, dict[str, float | str]]:
    raw_range = _clamp(float(f.get("range_score") or 0.0), 0.0, 1.0)
    trendiness = _clamp(float((agg or {}).get("trendiness") or f.get("trend_strength") or 0.0), 0.0, 1.0)
    coherence = _clamp(float((agg or {}).get("coherence") or 0.5), 0.0, 1.0)
    regime = str((agg or {}).get("regime") or "unknown")

    multi_tf_range = _clamp(1.0 - trendiness, 0.0, 1.0)
    if regime == "range":
        multi_tf_range = _clamp(multi_tf_range + 0.06 * coherence, 0.0, 1.0)
    elif regime == "trend":
        multi_tf_range = _clamp(multi_tf_range - 0.04 * max(0.0, coherence - 0.5), 0.0, 1.0)

    stable = _clamp(0.20 * raw_range + 0.80 * multi_tf_range, 0.0, 1.0)
    return stable, {
        "raw_range_score_1m": float(raw_range),
        "multi_tf_range_score": float(multi_tf_range),
        "trendiness": float(trendiness),
        "coherence": float(coherence),
        "regime": regime,
    }


def _score(
    bot_type: str,
    venue: str,
    f: dict[str, Any],
    taker_fee_bps: float,
    global_sent: float,
    cost_model: dict[str, Any] | None = None,
    sentiment_has_data: bool = True,
) -> tuple[float, float, dict[str, Any]]:
    cost_model = dict(cost_model or {})
    agg = dict(f.get("_direction_agg") or {})
    direction = _direction(bot_type, agg)

    def _num(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return float(default)
            return float(value)
        except Exception:
            return float(default)

    strengths = agg.get("strength") or {}
    if isinstance(strengths, dict):
        direction_strength = abs(_num(strengths.get("all"), 0.0))
    else:
        direction_strength = abs(_num(strengths, 0.0))

    range_score, range_meta = _stable_range_score(f, agg)
    trend_strength = _clamp(_num(agg.get("trendiness"), _num(f.get("trend_strength"), 0.0)), 0.0, 1.0)
    coherence = _clamp(_num(agg.get("coherence"), 0.5), 0.0, 1.0)
    regime_conf = _clamp(_num(agg.get("regime_confidence"), 0.0), 0.0, 1.0)
    atr_pct = max(0.0, _num(f.get("_atr_pct_1h"), _num(f.get("atr_pct"), 0.0)))
    atr_penalty = _clamp(atr_pct / 0.06, 0.0, 2.0)
    effective_sent = _clamp(_num(global_sent, 0.0), -1.0, 1.0)
    spread = max(0.0, _num(cost_model.get("spread_bps"), _num(f.get("spread_bps"), 0.0)))
    execution_cost_bps = max(
        0.0,
        _num(
            cost_model.get("execution_cost_bps"),
            _num(cost_model.get("total_cost_bps"), max(0.0, spread + 2.0 * float(taker_fee_bps))),
        ),
    )
    cost_penalty = _clamp(execution_cost_bps / 20.0, 0.0, 2.5)

    pos: list[dict[str, Any]] = []
    neg: list[dict[str, Any]] = []

    def add_pos(feature: str, value: Any, weight: float, msg: str) -> None:
        pos.append(_make_factor(feature, value, weight, msg))

    def add_neg(feature: str, value: Any, weight: float, msg: str) -> None:
        neg.append(_make_factor(feature, value, weight, msg))

    raw = 0.0
    if bot_type == "spot_grid":
        raw += 1.55 * range_score
        raw += 0.25 * coherence
        raw += 0.18 * regime_conf
        raw -= 1.15 * trend_strength
        raw -= 0.65 * atr_penalty
        raw -= 0.35 * cost_penalty

        if direction == "long":
            raw += 0.10 * effective_sent
            raw += 0.08 * direction_strength
        elif sentiment_has_data:
            raw += 0.04 * (1.0 - min(1.0, abs(effective_sent)))
        else:
            raw -= 0.04

        if range_score > 0.0:
            add_pos("range_score", range_score, 1.55 * range_score, "выраженный диапазон подходит для spot grid")
        if coherence > 0.0:
            add_pos("coherence", coherence, 0.25 * coherence, "таймфреймы согласованы")
        if regime_conf > 0.0:
            add_pos("regime_confidence", regime_conf, 0.18 * regime_conf, "режим оценён с приемлемой уверенностью")
        if direction == "long" and effective_sent > 0.0:
            add_pos("effective_sentiment", effective_sent, 0.10 * effective_sent, "сентимент поддерживает long bias")
        elif direction == "long" and effective_sent < 0.0:
            add_neg("effective_sentiment", abs(effective_sent), 0.10 * effective_sent, "сентимент против long bias")
        elif direction == "neutral" and sentiment_has_data:
            add_pos("effective_sentiment", 1.0 - min(1.0, abs(effective_sent)), 0.04 * (1.0 - min(1.0, abs(effective_sent))), "сентимент не мешает нейтральной сетке")
        elif direction == "neutral":
            add_neg("sentiment_data_availability", 0, -0.04, "нет сентимент-данных — нейтральный bias менее надёжен")
        if direction == "long" and direction_strength > 0.0:
            add_pos("direction_strength", direction_strength, 0.08 * direction_strength, "есть умеренный directional bias без потери grid-логики")

        if trend_strength > 0.0:
            add_neg("trend_strength", trend_strength, -1.15 * trend_strength, "сильный тренд ухудшает grid")
        if atr_pct > 0.0:
            add_neg("atr_pct", atr_pct, -0.65 * atr_penalty, "повышенная волатильность делает диапазон менее устойчивым")
        if execution_cost_bps > 0.0:
            add_neg("execution_cost_bps", execution_cost_bps, -0.35 * cost_penalty, "издержки исполнения снижают net capture")
        if spread > 0.0:
            add_neg("spread_bps", spread, -0.15 * min(1.0, spread / 5.0), "спред уменьшает эффективность сетки")

    elif bot_type == "futures_grid":
        raw += 1.35 * range_score
        raw += 0.22 * coherence
        raw += 0.16 * regime_conf
        raw -= 1.00 * trend_strength
        raw -= 0.75 * atr_penalty
        raw -= 0.40 * cost_penalty

        if direction == "long":
            raw += 0.12 * effective_sent
            raw += 0.10 * direction_strength
        elif direction == "short":
            raw -= 0.12 * effective_sent
            raw += 0.10 * direction_strength
        elif sentiment_has_data:
            raw += 0.05 * (1.0 - min(1.0, abs(effective_sent) * 1.5))
        else:
            raw -= 0.05

        if range_score > 0.0:
            add_pos("range_score", range_score, 1.35 * range_score, "диапазон подходит для futures grid")
        if coherence > 0.0:
            add_pos("coherence", coherence, 0.22 * coherence, "таймфреймы согласованы")
        if regime_conf > 0.0:
            add_pos("regime_confidence", regime_conf, 0.16 * regime_conf, "режим оценён с приемлемой уверенностью")
        if direction in ("long", "short") and direction_strength > 0.0:
            add_pos("direction_strength", direction_strength, 0.10 * direction_strength, "есть исполнимый directional bias для futures grid")
        if direction == "long" and effective_sent > 0.0:
            add_pos("effective_sentiment", effective_sent, 0.12 * effective_sent, "сентимент поддерживает long bias")
        elif direction == "long" and effective_sent < 0.0:
            add_neg("effective_sentiment", abs(effective_sent), 0.12 * effective_sent, "сентимент против long bias")
        elif direction == "short" and effective_sent < 0.0:
            add_pos("effective_sentiment", abs(effective_sent), 0.12 * abs(effective_sent), "сентимент поддерживает short bias")
        elif direction == "short" and effective_sent > 0.0:
            add_neg("effective_sentiment", effective_sent, -0.12 * effective_sent, "сентимент против short bias")
        elif direction == "neutral" and sentiment_has_data:
            add_pos("effective_sentiment", 1.0 - min(1.0, abs(effective_sent) * 1.5), 0.05 * (1.0 - min(1.0, abs(effective_sent) * 1.5)), "сентимент не мешает нейтральной сетке")
        elif direction == "neutral":
            add_neg("sentiment_data_availability", 0, -0.05, "нет сентимент-данных — нейтральный futures bias менее надёжен")

        if trend_strength > 0.0:
            add_neg("trend_strength", trend_strength, -1.00 * trend_strength, "сильный тренд ломает grid")
        if atr_pct > 0.0:
            add_neg("atr_pct", atr_pct, -0.75 * atr_penalty, "высокая волатильность повышает риск range break")
        if execution_cost_bps > 0.0:
            add_neg("execution_cost_bps", execution_cost_bps, -0.40 * cost_penalty, "издержки исполнения и funding давят на net result")
        if spread > 0.0:
            add_neg("spread_bps", spread, -0.18 * min(1.0, spread / 5.0), "спред ухудшает fills")
    else:
        raw = 0.0

    score = float(_clamp(raw / 2.2, -1.0, 1.0))
    conf0 = float(_clamp(_sigmoid(raw * 2.1), 0.0, 1.0))
    reasons = {
        "summary": "Рекомендация оценивает пригодность символа для grid-стратегии: ищется диапазонный рынок с контролируемой волатильностью, приемлемыми издержками и исполнимым bias по направлению.",
        "top_positive_factors": sorted(pos, key=lambda x: abs(float(x.get("weight") or 0.0)), reverse=True)[:5],
        "top_negative_factors": sorted(neg, key=lambda x: abs(float(x.get("weight") or 0.0)), reverse=True)[:5],
        "cost_model": {
            **cost_model,
            "spread_bps": spread,
            "taker_fee_bps": float(taker_fee_bps),
            "execution_cost_bps": float(cost_model.get("execution_cost_bps") or execution_cost_bps),
            "total_cost_bps": float(cost_model.get("total_cost_bps") or execution_cost_bps),
            "net_cost_bps": float(cost_model.get("net_cost_bps") or execution_cost_bps),
        },
        "score_components": {
            "range_score": float(range_score),
            "range_score_meta": dict(range_meta),
            "trend_strength": float(trend_strength),
            "coherence": float(coherence),
            "regime_confidence": float(regime_conf),
            "atr_penalty": float(atr_penalty),
            "cost_penalty": float(cost_penalty),
        },
        "effective_sentiment": effective_sent,
    }
    return score, conf0, reasons


def _expected_rr(bot_type: str, f: dict[str, Any], cost_model: dict[str, Any] | None = None) -> float:
    cost_model = dict(cost_model or {})
    agg = dict(f.get("_direction_agg") or {})
    range_score, _ = _stable_range_score(f, agg)
    trend_strength = _clamp(float(agg.get("trendiness") or f.get("trend_strength") or 0.0), 0.0, 1.0)
    coherence = _clamp(float(agg.get("coherence") or 0.5), 0.0, 1.0)
    atr_pct = max(0.0, float(f.get("_atr_pct_1h") or f.get("atr_pct") or 0.0))
    # RR must reflect the same economics as scoring/labels: execution costs are always
    # paid, while futures funding carry can materially hurt or help the setup over the
    # label horizon. Keep execution friction in the risk proxy, but use net_cost_bps in
    # the numerator so expensive long carry lowers RR and received funding can improve it.
    net_cost_pct = float(
        cost_model.get("net_cost_bps")
        or cost_model.get("total_cost_bps")
        or cost_model.get("execution_cost_bps")
        or 0.0
    ) / 10000.0
    execution_cost_pct = max(
        0.0,
        float(cost_model.get("execution_cost_bps") or cost_model.get("total_cost_bps") or 0.0) / 10000.0,
    )

    gross_capture = max(0.0, (0.55 * range_score + 0.15 * coherence - 0.20 * trend_strength) * max(atr_pct, 0.0025))
    net_capture = gross_capture - net_cost_pct
    risk_proxy = max(max(atr_pct, 0.0025) * 1.5, execution_cost_pct * 2.0, 1e-6)
    return float(_clamp(net_capture / risk_proxy, 0.0, 3.0))


def _mode(venue: str, direction: str) -> tuple[str, str]:
    if venue == "spot":
        return "spot", "cash"
    if venue == "linear":
        return "unified", "isolated"
    return venue, "default"


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
    cost_model = dict(cost_model or {})
    price = float(f.get("price") or 0.0)
    if price <= 0:
        price = 1.0

    atr_pct = float(atr_pct_for_grid or f.get("atr_pct") or 0.0)
    atr_pct = max(atr_pct, 0.0015)
    agg = dict(f.get("_direction_agg") or {})
    range_score, _ = _stable_range_score(f, agg)
    dir_strength = _clamp(abs(float(direction_bias_strength or 0.0)), 0.0, 1.0)

    execution_cost_bps = max(
        0.0,
        float(cost_model.get("total_cost_bps") or cost_model.get("execution_cost_bps") or max(0.0, float(taker_fee_bps) * 2.0)),
    )
    cost_floor_pct = max(execution_cost_bps / 10000.0, 0.0001)
    min_spacing_pct = max((cost_floor_pct / 0.70) * 1.25, 0.0008)
    vol_spacing_pct = max(atr_pct * (0.45 + 0.25 * range_score), 0.0010)
    grid_spacing_pct_frac = max(min_spacing_pct, vol_spacing_pct)

    base_levels = 10
    if range_score >= 0.80:
        base_levels += 2
    elif range_score <= 0.45:
        base_levels -= 2
    if execution_cost_bps >= 18.0:
        base_levels -= 2
    elif execution_cost_bps <= 8.0:
        base_levels += 1
    if dir_strength >= 0.60:
        base_levels -= 1
    grid_levels = max(4, min(14, int(base_levels)))

    range_span_pct_total = max(grid_spacing_pct_frac * max(grid_levels - 1, 4) * 1.15, atr_pct * (3.0 + 2.0 * range_score))
    half_span = range_span_pct_total / 2.0

    down_mult = 1.0
    up_mult = 1.0
    if direction == "long":
        down_mult = 0.90
        up_mult = 1.10
    elif direction == "short":
        down_mult = 1.10
        up_mult = 0.90

    lower = price * max(0.01, 1.0 - half_span * down_mult)
    upper = price * (1.0 + half_span * up_mult)
    if upper <= lower:
        lower = price * (1.0 - half_span)
        upper = price * (1.0 + half_span)

    params: dict[str, Any] = {
        "bot_type": bot_type,
        "venue": venue,
        "direction": direction,
        "direction_bias": direction_bias,
        "direction_bias_strength": float(dir_strength),
        "effective_sentiment": float(_clamp(float(global_sent), -1.0, 1.0)),
        "price_ref": _round_price(price, decimals=10),
        "price_range_lower": _round_price(lower, decimals=10),
        "price_range_upper": _round_price(upper, decimals=10),
        "range_span_pct_total": float(range_span_pct_total * 100.0),
        "grid_spacing_pct": float(grid_spacing_pct_frac * 100.0),
        "grid_levels": int(grid_levels),
        "label_horizon_hours": int(BOT_HORIZONS.get(bot_type, 6 * 3600) // 3600),
        "cost_model": dict(cost_model),
    }

    if venue == "linear":
        leverage = 1
        if direction != "neutral" and dir_strength >= 0.20 and atr_pct <= 0.035:
            leverage = 2
        if direction != "neutral" and dir_strength >= 0.45 and atr_pct <= 0.020 and execution_cost_bps <= 10.0:
            leverage = 3
        if atr_pct >= 0.05 or execution_cost_bps >= 18.0:
            leverage = 1
        params["leverage"] = int(leverage)
        params["margin_mode"] = "isolated"
    else:
        params["leverage"] = 1
        params["margin_mode"] = "cash"

    return params

# ── Persistence gate state ───────────────────────────────────────────────────
# Tracks consecutive recommended cycles for the SAME logical signal.
# The original implementation keyed only by (venue, symbol, bot_type) and therefore
# could accidentally confirm a freshly flipped short using a previous long signal.
# We include direction in the signature and require a consecutive-cycle hit within
# an interval-derived freshness window.
_prev_recommended: dict[tuple, dict[str, int]] = {}
PERSISTENCE_BOTS: set[str] = {"spot_grid", "futures_grid"}
PERSISTENCE_STATE_APP_KEY = "reco_persistence_gate_v1"
DIRECTION_STATE_APP_KEY = "reco_direction_stability_v1"


def _load_prev_recommended(conn) -> dict[tuple, dict[str, int]]:
    raw = db.get_app_config_json(conn, PERSISTENCE_STATE_APP_KEY, default={}) or {}
    out: dict[tuple, dict[str, int]] = {}
    if not isinstance(raw, dict):
        return out
    for key, state in raw.items():
        if not isinstance(key, str) or not isinstance(state, dict):
            continue
        parts = key.split("|")
        if len(parts) != 4:
            continue
        venue, sym, bot_type, direction = parts
        try:
            out[(venue, sym, bot_type, direction)] = {"ts": int(state.get("ts", 0) or 0), "count": int(state.get("count", 0) or 0)}
        except Exception:
            continue
    return out


def _save_prev_recommended(conn, state: dict[tuple, dict[str, int]], fresh_gap: int) -> None:
    now = int(time.time())
    payload: dict[str, dict[str, int]] = {}
    ttl = max(int(fresh_gap) * 3, 600)
    for key, meta in (state or {}).items():
        if not isinstance(key, tuple) or len(key) != 4 or not isinstance(meta, dict):
            continue
        ts = int(meta.get("ts", 0) or 0)
        count = int(meta.get("count", 0) or 0)
        if ts <= 0 or count <= 0 or now - ts > ttl:
            continue
        payload["|".join(str(x) for x in key)] = {"ts": ts, "count": count}
    db.set_app_config_json(conn, PERSISTENCE_STATE_APP_KEY, payload)


def _advance_persistence_gate(venue: str, sym: str, bot_type: str, direction: str, now_ts: int, fresh_gap: int) -> int:
    global _prev_recommended
    pkey = (venue, sym, bot_type, direction)
    state = _prev_recommended.get(pkey) or {"ts": 0, "count": 0}
    if now_ts - int(state.get("ts", 0)) <= fresh_gap:
        state = {"ts": now_ts, "count": int(state.get("count", 0)) + 1}
    else:
        state = {"ts": now_ts, "count": 1}
    _prev_recommended[pkey] = state
    for other_dir in ("long", "short", "neutral"):
        other_key = (venue, sym, bot_type, other_dir)
        if other_key != pkey:
            _prev_recommended.pop(other_key, None)
    return int(state.get("count", 0))


def _reset_persistence_gate(venue: str, sym: str, bot_type: str) -> None:
    global _prev_recommended
    for other_dir in ("long", "short", "neutral"):
        _prev_recommended.pop((venue, sym, bot_type, other_dir), None)


_direction_state_cache: dict[tuple[str, str], dict[str, Any]] = {}


def _load_direction_state(conn) -> dict[tuple[str, str], dict[str, Any]]:
    raw = db.get_app_config_json(conn, DIRECTION_STATE_APP_KEY, default={}) or {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, state in raw.items():
        if not isinstance(key, str) or not isinstance(state, dict):
            continue
        parts = key.split("|", 1)
        if len(parts) != 2:
            continue
        venue, sym = parts
        out[(venue, sym)] = {
            "ts": int(state.get("ts", 0) or 0),
            "direction": str(state.get("direction") or "neutral"),
            "bias": str(state.get("bias") or "neutral"),
            "score_all": float(state.get("score_all", 0.0) or 0.0),
            "trendiness": float(state.get("trendiness", 0.0) or 0.0),
            "coherence": float(state.get("coherence", 0.0) or 0.0),
        }
    return out


def _save_direction_state(conn, state: dict[tuple[str, str], dict[str, Any]], fresh_gap: int) -> None:
    now = int(time.time())
    ttl = max(int(fresh_gap) * 8, 1800)
    payload: dict[str, dict[str, Any]] = {}
    for key, meta in (state or {}).items():
        if not isinstance(key, tuple) or len(key) != 2 or not isinstance(meta, dict):
            continue
        ts = int(meta.get("ts", 0) or 0)
        if ts <= 0 or now - ts > ttl:
            continue
        payload[f"{key[0]}|{key[1]}"] = {
            "ts": ts,
            "direction": str(meta.get("direction") or "neutral"),
            "bias": str(meta.get("bias") or "neutral"),
            "score_all": float(meta.get("score_all", 0.0) or 0.0),
            "trendiness": float(meta.get("trendiness", 0.0) or 0.0),
            "coherence": float(meta.get("coherence", 0.0) or 0.0),
        }
    db.set_app_config_json(conn, DIRECTION_STATE_APP_KEY, payload)


def _stabilize_direction_agg(
    agg: dict[str, Any],
    prev_state: dict[str, Any] | None,
    now_ts: int,
    fresh_gap: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stable = dict(agg or {})
    scores = dict(stable.get("scores") or {})
    strengths = dict(stable.get("strength") or {})
    raw_direction = str(stable.get("direction") or "neutral")
    raw_bias = str(stable.get("bias") or "neutral")
    score_all = float(scores.get("all") or 0.0)
    strength_all = float(strengths.get("all") or 0.0)
    trendiness = float(stable.get("trendiness") or 0.0)
    coherence = float(stable.get("coherence") or 0.0)
    regime = str(stable.get("regime") or "unknown")

    enter_thr = 0.14
    exit_thr = 0.09
    flip_thr = 0.18
    trend_enter_thr = 0.28

    stable["raw_direction"] = raw_direction
    stable["raw_bias"] = raw_bias

    prev = dict(prev_state or {})
    prev_ts = int(prev.get("ts", 0) or 0)
    prev_fresh = prev_ts > 0 and now_ts - prev_ts <= max(int(fresh_gap), 60)
    prev_direction = str(prev.get("direction") or "neutral") if prev_fresh else "neutral"

    applied = False
    mode = "pass_through"
    note = None

    if raw_direction in ("long", "short") and (abs(score_all) < enter_thr or trendiness < trend_enter_thr):
        stable["direction"] = "neutral"
        applied = True
        mode = "enter_deadband"
        note = "Directional thesis is not strong enough to leave neutral state yet."

    if regime == "range" and abs(score_all) < flip_thr:
        stable["direction"] = "neutral"
        if raw_direction != "neutral":
            applied = True
            mode = "range_neutrality_hold"
            note = "Range regime keeps the longer-horizon thesis neutral until the break becomes clearer."

    if prev_direction in ("long", "short"):
        current_direction = str(stable.get("direction") or "neutral")
        same_sign_but_weaker = current_direction == "neutral" and abs(score_all) >= exit_thr and regime != "range"
        weak_opposite_flip = current_direction in ("long", "short") and current_direction != prev_direction and (
            abs(score_all) < flip_thr or strength_all < 0.18 or coherence < 0.58
        )
        if same_sign_but_weaker or weak_opposite_flip:
            stable["direction"] = prev_direction
            stable["bias"] = prev_direction
            applied = True
            mode = "hysteresis_hold"
            note = "Previous directional thesis is held until the opposite move proves itself across cycles."

    final_direction = str(stable.get("direction") or "neutral")
    if final_direction == "neutral" and raw_bias in ("long", "short") and abs(score_all) >= exit_thr:
        stable["bias"] = raw_bias
    elif final_direction in ("long", "short"):
        stable["bias"] = final_direction

    stable["direction_stability"] = {
        "applied": bool(applied),
        "mode": mode,
        "note": note,
        "previous_direction": prev_direction,
        "fresh_gap_sec": int(max(int(fresh_gap), 60)),
        "enter_threshold": float(enter_thr),
        "exit_threshold": float(exit_thr),
        "flip_threshold": float(flip_thr),
    }

    state_out = {
        "ts": int(now_ts),
        "direction": str(stable.get("direction") or "neutral"),
        "bias": str(stable.get("bias") or "neutral"),
        "score_all": float(score_all),
        "trendiness": float(trendiness),
        "coherence": float(coherence),
    }
    return stable, state_out


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
        if bt in UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS:
            result[bt] = LogRegScaler(fitted=False)
            continue
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
        if bt in UNSUPPORTED_STATISTICAL_CALIBRATION_BOTS:
            calibrators[bt] = LogRegScaler(fitted=False)
            continue
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
    """Fit direction calibrator on supported directional outcomes.

    We calibrate the *raw direction confidence* (or unsigned strength fallback),
    not the signed aggregate score. This preserves symmetry between strong longs
    and strong shorts and makes the resulting value a true probability-like metric.
    """
    rows = db.get_outcomes_with_recs(conn, limit=5000)
    xs, ys = [], []
    for row in rows:
        if row["bot_type"] != "futures_grid":
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
    global _prev_recommended, _direction_state_cache
    _fresh_gap = max(45, int(settings.reco_interval_sec * 2.5))
    _prev_recommended = _load_prev_recommended(conn)
    _direction_state_cache = _load_direction_state(conn)
    sent_agg = compute_sentiment_agg(conn, scope="global", key="crypto")
    # Primary sentiment for scoring: adaptive blend from compute_sentiment_agg.
    # Falls back to 6h EWMA for backward compatibility with older snapshots.
    global_sent = float(sent_agg.get("effective_score", sent_agg.get("ewma", {}).get("6h", 0.0)))
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
    btc_1h_rows = _drop_open_candle(btc_1h_rows, tf_sec=3600, ts_now=ts_now)
    if not btc_1h_rows:
        btc_1h_rows = db.get_latest_ohlcv(conn, "linear", "BTCUSDT", tf_sec=3600, limit=50)
        btc_1h_rows = _drop_open_candle(btc_1h_rows, tf_sec=3600, ts_now=ts_now)
    # Reverse to oldest-first for log-return calculations in btc_beta
    btc_1h_closes = [float(r["close"]) for r in reversed(btc_1h_rows)] if btc_1h_rows else []

    for venue in settings.venues:
        symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
        for sym in symbols:
            rows = db.get_latest_ohlcv(conn, venue, sym, tf_sec=60, limit=220)
            rows = _drop_open_candle(rows, tf_sec=60, ts_now=ts_now)
            if not rows or len(rows) < 80:
                continue
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
                rows_tf = _drop_open_candle(rows_tf, tf_sec=tf, ts_now=ts_now)
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
            agg, _dir_state = _stabilize_direction_agg(agg, _direction_state_cache.get((venue, sym)), ts_now, _fresh_gap)
            _direction_state_cache[(venue, sym)] = _dir_state
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
                _sym_rows = _drop_open_candle(_sym_rows, tf_sec=3600, ts_now=ts_now)
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

    market_shock = compute_market_shock(conn, settings, sent_agg, symbol_feature_map, ts_now)
    db.set_app_config_json(conn, MARKET_SHOCK_APP_KEY, market_shock)

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
            if bot_type == "futures_grid" and venue != "linear":
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
            if fr_data and (ts_now - int(fr_data.get("ts") or 0) > MAX_FUNDING_STALENESS_SEC):
                fr_data = None
            oi_rows  = db.get_oi_series(conn, sym, limit=48)  if venue == "linear" else []
            if oi_rows:
                latest_oi_ts = int((oi_rows[0] or {}).get("ts") or 0)
                if latest_oi_ts <= 0 or (ts_now - latest_oi_ts > MAX_OI_STALENESS_SEC):
                    oi_rows = []
            fr_sig   = funding_signal(fr_data["funding_rate"] if fr_data else None)
            oi_sig   = oi_trend(oi_rows)
            raw_direction = str((f.get('_direction_agg', {}) or {}).get('direction') or 'neutral')
            direction = _direction(bot_type, f.get('_direction_agg', {}))
            spot_short_neutralized = bool(bot_type == "spot_grid" and raw_direction == "short" and direction == "neutral")
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

            # Compute calibrated direction confidence once and reuse it everywhere
            # in this cycle (gates, feature snapshot, stored reasons, UI details).
            # Using raw confidence in one branch and calibrated confidence elsewhere
            # creates contradictory allow/block decisions for the same signal.
            _dir_agg_raw = dict(f.get("_direction_agg", {}))
            _xdir_pre = _raw_direction_confidence(_dir_agg_raw)
            _dir_conf_pre = dir_calibrator.predict(_xdir_pre) if dir_calibrator.fitted else _xdir_pre
            _dir_agg_cal = dict(_dir_agg_raw)
            _dir_agg_cal["direction_confidence_calibrated"] = _dir_conf_pre

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
            _global_sent_has_data = bool((sent_agg.get("data_quality") or {}).get("has_data"))
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
            sentiment_has_data = bool(_global_sent_has_data or _sym_n > 0)


            feasibility_blocks = []

            # ── Data completeness / liquidity gates ──
            if turnover is None:
                feasibility_blocks.append({"code": "LIQUIDITY_UNKNOWN",
                    "msg": "нет turnover24h — ликвидность не подтверждена, cost-model ненадёжен"})
            elif liq_tier == "micro":
                feasibility_blocks.append({"code": "LIQUIDITY_TOO_LOW",
                    "msg": f"turnover24h={turnover} USD < $500K — торговля на неликвидном символе искажает fills/статистику"})
                if venue == "linear":
                    feasibility_blocks.append({"code": "LIQUIDITY_LOW_FUTURES",
                        "msg": f"turnover24h={turnover} USD < $2M — для futures grid нужна повышенная осторожность по ликвидности"})
            elif venue == "linear" and liq_tier == "low":
                feasibility_blocks.append({"code": "LIQUIDITY_LOW_FUTURES",
                    "msg": f"turnover24h={turnover} USD < $2M — для futures grid нужна повышенная осторожность по ликвидности"})
            if spread is None:
                feasibility_blocks.append({"code": "SPREAD_UNKNOWN",
                    "msg": "bid/ask отсутствуют — нельзя надёжно оценить execution cost"})

            # ── Funding rate gate (futures only) ──
            # Gate must be keyed off the *payer* side, not only off semantic longs.
            # `expected_funding_bps` is direction-aware already, so positive values mean
            # this exact setup is expected to pay funding over the label horizon.
            if venue == "linear":
                funding_block = _extreme_funding_block(direction, fr_sig, cost_model)
                if funding_block is not None:
                    feasibility_blocks.append(funding_block)

            if bot_type in ("spot_grid","futures_grid") and spread is not None and spread > 14.0:
                feasibility_blocks.append({"code":"SPREAD_TOO_WIDE", "msg": f"spread_bps={spread:.2f} слишком широкий для grid"})
            # If symbol is highly correlated to BTC, direction is less independent
            beta_info = f.get("_btc_beta", {})
            if beta_info.get("is_btc_driven") and sym != "BTCUSDT":
                _dir_conf_pre = float(_clamp(_dir_conf_pre * 0.88, 0.0, 0.99))
                _dir_agg_cal["direction_confidence_calibrated"] = _dir_conf_pre
            # Block threshold 0.05 = 5% 1h ATR. Old value 0.018 was calibrated for 1m ATR
            # and blocked ALL symbols since typical 1h ATR for small caps is 3–8%.
            # ── Risk gate — uses cached risk_status (computed once per cycle) ──
            risk_blocks = gate_candidate(conn, venue, sym, limits, cached_status=_cached_risk_status)
            feasibility_blocks.extend(risk_blocks)
            feasibility_blocks.extend(apply_market_shock_gate(market_shock, venue, bot_type, direction))
            fast_veto = compute_symbol_fast_veto(conn, venue, sym, ts_now, direction, feature_row=f)
            feasibility_blocks.extend(fast_veto.get("blocks") or [])

            f_for_score = dict(f)
            f_for_score["_direction_agg"] = dict(_dir_agg_cal)
            score, conf0, reasons = _score(
                bot_type,
                venue,
                f_for_score,
                taker_fee_bps=taker_fee_bps,
                global_sent=effective_sent,
                cost_model=cost_model,
                sentiment_has_data=sentiment_has_data,
            )

            # ── Funding + OI score adjustments ──
            if venue == "linear":
                score = _clamp(score + _funding_score_adjustment(direction, fr_sig, cost_model), -1.0, 1.0)

            if str((market_shock or {}).get("severity") or "normal") == "guarded" and not feasibility_blocks:
                score = float(_clamp(score * 0.92, -1.0, 1.0))

            conf_raw = float(conf0)
            # ── Two-stage calibration: LogReg(features) → Platt ──────────────────
            # Build a temporary reasons-like dict so extract_features() can work
            # with the current (not yet stored) feature set.
            # ── Compute dir_conf_cal FIRST so extract_features gets the calibrated
            # value — matching what was stored in reasons_json during training.
            # (Previously dir_conf_cal was computed after extract_features, causing
            # a train/inference skew: model trained on calibrated conf, inferred on raw.)
            _dir_agg_for_cal = dict(_dir_agg_cal)
            feature_snapshot = _build_feature_snapshot(
                score=score,
                atr_pct=atr_pct,
                effective_sent=effective_sent,
                cost_model=cost_model,
                direction_agg=_dir_agg_for_cal,
                oi_sig=oi_sig,
                liq_tier=liq_tier,
                beta_info=beta_info,
            )

            _reasons_for_cal = {
                "effective_sentiment": effective_sent,
                "cost_model": cost_model,
                "direction_agg": _dir_agg_for_cal,  # includes calibrated dir_conf
                "feature_snapshot": dict(feature_snapshot),
                "top_positive_factors": (reasons.get("top_positive_factors") or []),
                "top_negative_factors": (reasons.get("top_negative_factors") or []),
            }
            _row_for_cal = {"score": score, "reasons": _reasons_for_cal, "success": 0}
            _fv = extract_features(_row_for_cal)

            bot_cal = bot_calibrators.get(bot_type)
            if bot_cal and bot_cal.fitted and len(bot_cal.coef) > 0 and _fv is not None:
                conf_cal = float(bot_cal.predict(_fv))
                _cal_source = "bot_logreg"
                _active_cal = bot_cal
            elif bot_cal and bot_cal.fitted:
                conf_cal = float(bot_cal.predict_score_only(score))
                _cal_source = "bot_platt"
                _active_cal = bot_cal
            else:
                # Do NOT fall back to a cross-bot/global calibrator for inference.
                # Outcome labels are bot-mechanics-specific (grid/range), so a pooled probability creates pseudo-statistical confidence.
                conf_cal = float(conf0)
                _cal_source = "raw"
                _active_cal = None
            # Adaptive blend: calibration weight grows with n_samples.
            # Raw-only mode keeps weight=0. Once a bot-specific calibrator exists,
            # the blend ramps up gradually so a freshly-fitted model does not fully
            # override the heuristic score on a still-small sample.
            _heur_cap = None
            if _active_cal is not None and _active_cal.fitted:
                _n_cal = int(_active_cal.n_samples)
                _cal_weight = float(_clamp(_n_cal / 300.0, 0.0, 1.0)) * 0.40 + 0.10
            else:
                _n_cal = 0
                _cal_weight = 0.0
            conf = float(_clamp((1.0 - _cal_weight) * conf_raw + _cal_weight * conf_cal, 0.0, 1.0))

            # Heuristic-only confidence must stay visibly conservative.
            if _active_cal is None:
                _heur_cap = {
                    "spot_grid": 0.72,
                    "futures_grid": 0.70,
                }.get(bot_type, 0.70)
                conf = float(min(conf, _heur_cap))

            # Context completeness penalty — reduce confidence when key signals are missing.
            # The system already falls back gracefully; this makes the uncertainty explicit.
            _ctx_mult = 1.0
            if not f.get("_atr_pct_1h"):          _ctx_mult *= 0.92  # no 1h ATR
            if not sentiment_has_data:            _ctx_mult *= 0.94  # missing sentiment is uncertainty, not true neutral
            if venue == "linear" and oi_sig.get("oi_now") is None: _ctx_mult *= 0.96  # no OI data
            if venue == "linear" and fr_sig.get("value") is None:  _ctx_mult *= 0.98  # no funding data
            _dir_tf_count = len((f.get("_direction_agg") or {}).get("tf_used") or [])
            if _dir_tf_count < 3:                  _ctx_mult *= 0.93  # sparse TF coverage
            if _ctx_mult < 1.0:
                conf = float(_clamp(conf * _ctx_mult, 0.0, 1.0))

            # OI unwinding → reduce confidence
            if venue == "linear" and oi_sig["signal"] == "caution":
                conf = float(_clamp(conf * 0.88, 0.0, 1.0))
            if spot_short_neutralized:
                # The model had bearish directional intent, but spot execution cannot express
                # a naked short. Make that loss of executable expressiveness visible.
                conf = float(_clamp(conf * 0.90, 0.0, 1.0))
            if str((market_shock or {}).get("severity") or "normal") == "guarded":
                conf = float(_clamp(conf * 0.93, 0.0, 1.0))

            expected_rr = _expected_rr(bot_type, f, cost_model=cost_model)
            account_mode, margin_mode = _mode(venue, direction)

            blocks = list(feasibility_blocks)  # risk_blocks already included via feasibility_blocks.extend()

            confidence_gate_applied = bool(settings.require_conf_gate and _active_cal is not None)

            status = "recommended"
            if blocks:
                status = "blocked"
            elif score < settings.min_score_to_recommend:
                status = "no_trade"
            elif confidence_gate_applied and conf < settings.min_conf_to_recommend:
                status = "no_trade"

            risk_score = float(_clamp(atr_pct/0.10, 0.0, 1.0))

            params = _params(
                bot_type,
                venue,
                f,
                global_sent=effective_sent,
                direction=direction,
                taker_fee_bps=taker_fee_bps,
                direction_bias=str(_dir_agg_cal.get("bias", "neutral")),
                direction_bias_strength=float((_dir_agg_cal.get("strength", {}) or {}).get("all", 0.0) if isinstance(_dir_agg_cal.get("strength"), dict) else float(_dir_agg_cal.get("strength", 0.0))),
                atr_pct_for_grid=f.get("_atr_pct_1h"),
                cost_model=cost_model,
            )
            # Add execution guide for UI "Details" panel.
            params["trade_plan"] = _build_trade_plan(bot_type, venue, f, direction, params, cost_model=cost_model)
            params["operator_sheet"] = {
                "mode": direction,
                "venue": venue,
                "bot_type": bot_type,
                "symbol": sym,
                "price_ref": params.get("price_ref"),
                "range_lower": params.get("price_range_lower"),
                "range_upper": params.get("price_range_upper"),
                "grid_levels": params.get("grid_levels"),
                "grid_spacing_pct": params.get("grid_spacing_pct"),
                "leverage": params.get("leverage"),
                "margin_mode": params.get("margin_mode"),
                "kill_switch": (params.get("trade_plan") or {}).get("levels", {}).get("kill_switch", {}),
                "tp_per_leg": (params.get("trade_plan") or {}).get("levels", {}).get("tp_per_leg", {}),
                "market_shock_state": (market_shock or {}).get("state"),
                "market_shock_title": (market_shock or {}).get("title"),
                "operator_note": (market_shock or {}).get("operator_note"),
            }
            params["decision_context"] = {
                "thesis_direction": raw_direction,
                "execution_direction": direction,
                "market_shock_state": (market_shock or {}).get("state"),
                "fast_veto_state": (fast_veto or {}).get("state"),
            }

            rec_id = f"R-{ts_now}-{venue}-{sym}-{bot_type}-{secrets.token_hex(4)}"
            reasons2 = dict(reasons)
            reasons2["regime"] = regime
            reasons2["risk_checks"] = {"passed": len(blocks)==0, "blocks": blocks}
            thesis_ok = bool(score >= settings.min_score_to_recommend and (not confidence_gate_applied or conf >= settings.min_conf_to_recommend))
            reasons2["decision_layers"] = {
                "thesis_status": "favored" if thesis_ok else "unfavorable",
                "execution_status": "blocked" if blocks else "allowed",
                "final_status": status,
                "score_threshold": float(settings.min_score_to_recommend),
                "confidence_threshold": float(settings.min_conf_to_recommend),
                "confidence_gate_applied": bool(confidence_gate_applied),
            }
            reasons2["sentiment_agg"] = sent_agg
            reasons2["market_shock"] = market_shock
            reasons2["fast_veto"] = fast_veto
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
            reasons2["feature_snapshot"] = dict(feature_snapshot)
            reasons2["symbol_sentiment"] = {
                "value": float(sym_sent) if sym_sent is not None else None,
                "effective": float(effective_sent),
                "global": float(global_sent),
                "global_has_data": bool(_global_sent_has_data),
                "any_data": bool(sentiment_has_data),
                "blended": sym_sent is not None,
                "symbol_weight": float(_sym_weight),
                "global_weight": float(1.0 - _sym_weight),
                "n_points": int(_sym_n),
            }
            # Reuse the already calibrated direction aggregate built before the bot loop.
            dtmp = dict(_dir_agg_cal)
            dtmp["direction_confidence_model"] = {"type":"platt_scaling","fitted": dir_calibrator.fitted, "a": getattr(dir_calibrator,"a",None), "b": getattr(dir_calibrator,"b",None)}
            reasons2["direction_agg"] = dtmp
            reasons2["execution_constraints"] = {
                "spot_short_neutralized": bool(spot_short_neutralized),
                "raw_direction": raw_direction,
                "executable_direction": direction,
                "note": (
                    "spot_grid не может выразить naked short на spot; bearish bias сохранён только как контекст"
                    if spot_short_neutralized else None
                ),
            }
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
                "heuristic_cap": float(_heur_cap) if _heur_cap is not None else None,
                "calibration_weight": float(_cal_weight),
                "confidence_gate_applied": bool(confidence_gate_applied),
                "note": (
                    "Raw heuristic confidence; treat it as an operator signal, not as calibrated probability."
                    if _active_cal is None else (
                        "Bot-specific LogReg + Platt calibration is active."
                        if _cal_source == "bot_logreg" else "Bot-specific Platt-only calibration is active."
                    )
                ),
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

    status_counts = {"recommended": 0, "blocked": 0, "no_trade": 0, "suppressed": 0}

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

        # Apply persistence gate only to FINAL published recommendations.
        # This avoids confirming a bot that was internally recommended but then
        # suppressed by the cross-bot best-per-symbol selector.
        for r in recs:
            if r.get("bot_type") not in PERSISTENCE_BOTS:
                continue
            if r.get("status") == "recommended":
                count = _advance_persistence_gate(
                    str(r.get("venue") or ""),
                    str(r.get("symbol") or ""),
                    str(r.get("bot_type") or ""),
                    str(r.get("direction") or "neutral"),
                    ts_now,
                    _fresh_gap,
                )
                if count < 2:
                    r["status"] = "suppressed"
            else:
                _reset_persistence_gate(
                    str(r.get("venue") or ""),
                    str(r.get("symbol") or ""),
                    str(r.get("bot_type") or ""),
                )

        _save_prev_recommended(conn, _prev_recommended, _fresh_gap)
        _save_direction_state(conn, _direction_state_cache, _fresh_gap)

        for r in recs:
            st = str(r.get("status") or "")
            if st in status_counts:
                status_counts[st] += 1
        db.insert_recommendations(conn, recs)
        db.log_decision(
            conn,
            "PUBLISH",
            None,
            None,
            {
                "count_all": len(recs),
                "count_best": len(best_map),
                "count_recommended": status_counts["recommended"],
                "count_blocked": status_counts["blocked"],
                "count_no_trade": status_counts["no_trade"],
                "count_suppressed": status_counts["suppressed"],
                "model_version": model_version,
                "regime": regime,
                "global_sentiment_6h": global_sent,
                "sentiment_regime": sent_agg.get("regime"),
                "sentiment_strength": sent_agg.get("strength"),
                "calibrator_fitted": calibrator.fitted,
            },
        )

    return {
        "regime": regime,
        "count": len(recs),
        "count_recommended": status_counts["recommended"],
        "count_blocked": status_counts["blocked"],
        "count_no_trade": status_counts["no_trade"],
        "count_suppressed": status_counts["suppressed"],
        "global_sentiment_6h": global_sent,
        "sentiment_regime": sent_agg.get("regime"),
        "sentiment_strength": sent_agg.get("strength"),
        "calibrator_fitted": calibrator.fitted,
    }
