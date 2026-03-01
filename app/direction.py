from __future__ import annotations

import math
from typing import Any

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def _sign(x: float, thr: float) -> int:
    if x > thr:
        return 1
    if x < -thr:
        return -1
    return 0

def _ema(xs: list[float], period: int) -> list[float]:
    if not xs:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(alpha * x + (1.0 - alpha) * out[-1])
    return out

def _sma(xs: list[float], period: int) -> float:
    if not xs:
        return 0.0
    period = max(1, min(period, len(xs)))
    return sum(xs[-period:]) / period

def rsi14(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 2:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = closes[-i] - closes[-i-1]
        if ch >= 0:
            gains += ch
        else:
            losses += -ch
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def macd_hist(closes: list[float]) -> float:
    if len(closes) < 60:
        return 0.0
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd = [a - b for a, b in zip(ema12, ema26)]
    signal = _ema(macd, 9)
    return float(macd[-1] - signal[-1])

def ma_slope(closes: list[float], fast: int = 20, slow: int = 60) -> float:
    if len(closes) < slow + 2:
        return 0.0
    f = _sma(closes, fast)
    s = _sma(closes, slow)
    p = closes[-1] if closes[-1] else 1.0
    return float((f - s) / p)

def atr_pct(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 2:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    atr = _sma(trs, period)
    p = closes[-1] if closes[-1] else 1.0
    return float(atr / p)

# TF weights: structural TFs dominate
TF_WEIGHTS = {
    15*60: 0.8,
    30*60: 1.0,
    60*60: 1.5,
    240*60: 2.0,
    24*60*60: 3.0,
}

def vote_for_tf(closes: list[float], highs: list[float], lows: list[float]) -> dict[str, Any]:
    # Raw indicators
    slope = ma_slope(closes)
    rsi = rsi14(closes)
    hist = macd_hist(closes)
    ap = atr_pct(highs, lows, closes)

    # Soft contributions (normalized)
    # Slope normalized by ATR% to make TFs more comparable
    slope_norm = slope / max(1e-6, ap)
    # Increased sensitivity: 0.22 vs old 0.15 — slope is the most reliable trend signal
    slope_c = _clamp(slope_norm * 0.22, -1.0, 1.0)

    # MACD hist normalized by price
    p = closes[-1] if closes[-1] else 1.0
    hist_norm = hist / max(1e-9, p)
    hist_c = _clamp(hist_norm * 900.0, -1.0, 1.0)

    # RSI centered at 50, tightened normalization for more signal pull
    rsi_c = _clamp((rsi - 50.0) / 30.0, -1.0, 1.0)

    # Bollinger Band %B: price position relative to 20-period BB
    # Acts as mean-reversion vs momentum confirmator
    closes_20 = closes[-20:] if len(closes) >= 20 else closes
    sma20 = sum(closes_20) / len(closes_20)
    std20 = math.sqrt(sum((x - sma20) ** 2 for x in closes_20) / len(closes_20)) if len(closes_20) > 1 else 1e-9
    bb_b = _clamp((closes[-1] - (sma20 - 2 * std20)) / max(1e-9, 4 * std20), 0.0, 1.0)  # 0=lower band, 1=upper band
    bb_c = _clamp((bb_b - 0.5) * 2.0, -1.0, 1.0)  # centered: -1=oversold, +1=overbought

    # Soft directional score — slope dominates; MACD/RSI/BB supplement
    score = 0.55 * slope_c + 0.22 * hist_c + 0.15 * rsi_c + 0.08 * bb_c
    score = float(_clamp(score, -1.0, 1.0))

    # Trendiness proxy: combination of absolute slope and MACD agreement
    trend_strength = float(_clamp(abs(slope_c) * 0.75 + abs(hist_c) * 0.25, 0.0, 1.0))

    # Neutral veto: lowered threshold — only veto very flat markets
    neutral_veto = 0.0
    if trend_strength < 0.15:
        neutral_veto = 0.8
    elif trend_strength < 0.25:
        neutral_veto = 0.4

    return {
        "atr_pct": float(ap),
        "slope": float(slope),
        "slope_norm": float(slope_norm),
        "macd_hist": float(hist),
        "rsi14": float(rsi),
        "contrib": {"ma_slope": float(slope_c), "macd": float(hist_c), "rsi": float(rsi_c)},
        "score": float(score),
        "trend_strength": float(trend_strength),
        "neutral_veto": float(neutral_veto),
    }

def _aggregate_signed(tf_map: dict[int, dict[str, Any]], tf_secs: list[int]) -> float:
    num = 0.0
    den = 0.0
    for tf in tf_secs:
        info = tf_map.get(tf)
        if not info:
            continue
        w = float(TF_WEIGHTS.get(tf, 1.0))
        num += w * float(info.get("score", 0.0))
        den += w
    return float(num / den) if den > 0 else 0.0

def aggregate_direction(tf_map: dict[int, dict[str, Any]]) -> dict[str, Any]:
    # Define groups
    tactical_tfs = [15*60, 30*60, 60*60]
    structural_tfs = [240*60, 24*60*60]
    all_tfs = [15*60, 30*60, 60*60, 240*60, 24*60*60]

    # Weighted scores
    s_tactical = _aggregate_signed(tf_map, tactical_tfs)
    s_structural = _aggregate_signed(tf_map, structural_tfs)
    s_all = _aggregate_signed(tf_map, all_tfs)

    # Coherence: how much (weighted) TF signs agree with structural sign
    thr = 0.10  # tightened sign threshold (was 0.12) — more responsive to moderate signals
    struct_sign = _sign(s_structural, thr)
    agree = 0.0
    total = 0.0
    used = []
    for tf in all_tfs:
        info = tf_map.get(tf)
        if not info:
            continue
        w = float(TF_WEIGHTS.get(tf, 1.0))
        tf_sign = _sign(float(info.get("score", 0.0)), thr)
        if tf_sign != 0:
            total += w
            if struct_sign != 0 and tf_sign == struct_sign:
                agree += w
        used.append(tf)
    coherence = float(agree / total) if total > 0 and struct_sign != 0 else 0.5

    # Regime (trend/range/transition): based on trend_strength and coherence
    trendiness = 0.0
    den = 0.0
    for tf in all_tfs:
        info = tf_map.get(tf)
        if not info:
            continue
        w = float(TF_WEIGHTS.get(tf, 1.0))
        trendiness += w * float(info.get("trend_strength", 0.0))
        den += w
    trendiness = float(trendiness / den) if den > 0 else 0.0

    if trendiness >= 0.48 and coherence >= 0.50:
        regime = "trend"
    elif trendiness <= 0.25:
        regime = "range"
    else:
        regime = "transition"

    # Direction decision with structural veto
    # If structural is strong and coherent is decent, prefer structural direction
    direction = "neutral"
    bias = "neutral"

    # raw strengths
    strength_all = float(_clamp(abs(s_all), 0.0, 1.0))
    strength_struct = float(_clamp(abs(s_structural), 0.0, 1.0))
    strength_tact = float(_clamp(abs(s_tactical), 0.0, 1.0))

    # Bias is sign of all score (fallback structural)
    if s_all >= 0:
        bias = "long"
    else:
        bias = "short"

    # Default neutral if weak signal or range regime
    if strength_all < 0.12 or regime == "range":
        direction = "neutral"
    else:
        direction = "long" if s_all > 0 else "short"

    # Structural veto logic
    veto_applied = False
    if struct_sign != 0 and strength_struct >= 0.18:
        desired = "long" if struct_sign > 0 else "short"
        if direction != desired:
            if coherence >= 0.55:
                direction = desired
                veto_applied = True
            else:
                direction = "neutral"
                veto_applied = True

    # Confidence combines: strength + coherence + regime
    # Base lowered from 0.45 → 0.30 to increase dynamic range.
    # Now spans 0.30–0.99 instead of 0.55–0.99 — much better discrimination.
    base_conf = 0.30 + 0.52 * strength_all + 0.18 * _clamp(coherence, 0.0, 1.0)
    if regime == "trend":
        base_conf += 0.08  # was 0.05
    elif regime == "range":
        base_conf -= 0.08  # was 0.10
    direction_confidence = float(_clamp(base_conf, 0.0, 0.99))

    return {
        "direction": direction,                    # long/short/neutral
        "bias": bias,                              # long/short
        "direction_confidence": direction_confidence,
        "scores": {"tactical": s_tactical, "structural": s_structural, "all": s_all},
        "strength": {"tactical": strength_tact, "structural": strength_struct, "all": strength_all},
        "coherence": coherence,
        "regime": regime,                          # trend/range/transition
        "regime_confidence": float(_clamp(0.35 + 0.65*max(trendiness, 1-trendiness), 0.0, 1.0)),
        "structural_veto_applied": veto_applied,
        "tf_used": used,
    }
