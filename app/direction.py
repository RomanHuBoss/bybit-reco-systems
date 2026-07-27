from __future__ import annotations

import math
from typing import Any

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _finite_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        num = float(value)
    except Exception:
        return float(default)
    return float(num) if math.isfinite(num) else float(default)

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

def _log_levels(prices: list[float]) -> list[float]:
    return [math.log(float(value)) for value in prices]


def rsi14(closes: list[float], period: int = 14) -> float:
    """Wilder RSI(14) on log-price changes.

    Log-price deltas are exactly antisymmetric under a mirrored return path, so
    equally strong LONG and SHORT moves receive equal-magnitude contributions.
    """
    if len(closes) < period + 2:
        return 50.0

    levels = _log_levels(closes)
    deltas = [float(levels[i] - levels[i - 1]) for i in range(1, len(levels))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def macd_hist(closes: list[float]) -> float:
    # 26 EMA + 9 EMA(signal) needs ~35 observations for a non-degenerate value.
    # Use log prices so a mirrored return path produces the exact opposite signal.
    if len(closes) < 35:
        return 0.0
    levels = _log_levels(closes)
    ema12 = _ema(levels, 12)
    ema26 = _ema(levels, 26)
    macd = [a - b for a, b in zip(ema12, ema26)]
    signal = _ema(macd, 9)
    return float(macd[-1] - signal[-1])

def ma_slope(closes: list[float], fast: int = 20, slow: int = 60) -> float:
    if len(closes) < slow + 2:
        return 0.0
    levels = _log_levels(closes)
    return float(_sma(levels, fast) - _sma(levels, slow))

def atr_pct(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Log true range, approximately equal to relative ATR for small moves."""
    if len(closes) < period + 2:
        return 0.0
    log_highs = _log_levels(highs)
    log_lows = _log_levels(lows)
    log_closes = _log_levels(closes)
    trs = []
    for i in range(1, len(log_closes)):
        tr = max(
            log_highs[i] - log_lows[i],
            abs(log_highs[i] - log_closes[i - 1]),
            abs(log_lows[i] - log_closes[i - 1]),
        )
        trs.append(tr)
    return float(_sma(trs, period))


def mean_reversion_diagnostics(closes: list[float], *, max_returns: int = 160) -> dict[str, Any]:
    """Measure anti-persistence independently from absence of trend.

    A flat MA slope is not evidence that a grid has positive oscillation edge: a
    driftless martingale has near-zero trend too.  This diagnostic therefore uses
    three path properties that are independent of the MA/MACD trend proxy:
    lag-1 return autocorrelation, a four-step variance ratio, and sign reversals.
    Values near a random walk remain low; repeatable anti-persistence scores high.
    """
    clean: list[float] = []
    for value in closes or []:
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except Exception:
            continue
        if math.isfinite(number) and number > 0.0:
            clean.append(number)
    if len(clean) < 42:
        return {
            "mean_reversion_score": 0.0,
            "mean_reversion_evidence_valid": False,
            "return_autocorr_lag1": None,
            "variance_ratio_4": None,
            "sign_reversal_rate": None,
            "mean_reversion_observations": max(0, len(clean) - 1),
        }

    returns = [math.log(clean[i] / clean[i - 1]) for i in range(1, len(clean))]
    returns = returns[-max(40, int(max_returns)):]
    n = len(returns)
    mean_r = sum(returns) / n
    var1 = sum((value - mean_r) ** 2 for value in returns) / max(1, n - 1)
    if not math.isfinite(var1) or var1 <= 1e-16:
        return {
            "mean_reversion_score": 0.0,
            "mean_reversion_evidence_valid": False,
            "return_autocorr_lag1": None,
            "variance_ratio_4": None,
            "sign_reversal_rate": None,
            "mean_reversion_observations": n,
        }

    left = returns[:-1]
    right = returns[1:]
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    cov = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    den_left = sum((a - mean_left) ** 2 for a in left)
    den_right = sum((b - mean_right) ** 2 for b in right)
    corr = cov / math.sqrt(max(1e-30, den_left * den_right))
    corr = _clamp(float(corr), -1.0, 1.0)

    q = 4
    q_returns = [sum(returns[index - q + 1:index + 1]) for index in range(q - 1, n)]
    mean_q = sum(q_returns) / len(q_returns)
    var_q = sum((value - mean_q) ** 2 for value in q_returns) / max(1, len(q_returns) - 1)
    variance_ratio = max(0.0, float(var_q / (q * var1)))

    signs = [1 if value > 0.0 else (-1 if value < 0.0 else 0) for value in returns]
    pairs = [(a, b) for a, b in zip(signs, signs[1:]) if a and b]
    reversal_rate = (sum(1 for a, b in pairs if a != b) / len(pairs)) if pairs else 0.0

    autocorr_component = _clamp((-corr - 0.02) / 0.45, 0.0, 1.0)
    variance_ratio_component = _clamp((0.98 - variance_ratio) / 0.50, 0.0, 1.0)
    reversal_component = _clamp((reversal_rate - 0.52) / 0.28, 0.0, 1.0)
    score = _clamp(
        0.45 * autocorr_component
        + 0.35 * variance_ratio_component
        + 0.20 * reversal_component,
        0.0,
        1.0,
    )
    return {
        "mean_reversion_score": float(score),
        "mean_reversion_evidence_valid": True,
        "return_autocorr_lag1": float(corr),
        "variance_ratio_4": float(variance_ratio),
        "sign_reversal_rate": float(reversal_rate),
        "mean_reversion_observations": int(n),
    }

# TF weights: structural TFs dominate
TF_WEIGHTS = {
    # The outcome horizon is 12h. Tactical 15m-1h data therefore carries most
    # of the directional information, 4h confirms structure, and the 1d bar is
    # only a slow context/veto input rather than the dominant vote.
    15*60: 1.0,
    30*60: 1.25,
    60*60: 2.0,
    240*60: 2.25,
    24*60*60: 0.75,
}


def _safe_ohlc_vectors(
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> tuple[list[float], list[float], list[float]]:
    """Sanitize OHLC vectors before indicator voting.

    The recommender already feeds closed candles, but this function is also used
    by tests and future adapters.  It therefore cannot assume non-empty, equally
    sized, finite positive inputs.  Bad rows are skipped; malformed standalone
    calls fail neutral instead of raising or leaking a distorted directional sign.
    """
    n = min(len(closes or []), len(highs or []), len(lows or []))
    if n <= 0:
        return [], [], []
    c_out: list[float] = []
    h_out: list[float] = []
    l_out: list[float] = []
    for raw_c, raw_h, raw_l in zip(list(closes)[-n:], list(highs)[-n:], list(lows)[-n:]):
        if any(isinstance(value, bool) for value in (raw_c, raw_h, raw_l)):
            continue
        try:
            c = float(raw_c)
            h = float(raw_h)
            l = float(raw_l)
        except Exception:
            continue
        if not (math.isfinite(c) and math.isfinite(h) and math.isfinite(l)):
            continue
        if c <= 0 or h <= 0 or l <= 0:
            continue
        high = max(h, c, l)
        low = min(h, c, l)
        c_out.append(c)
        h_out.append(high)
        l_out.append(low)
    return c_out, h_out, l_out


def _neutral_tf_vote(reason: str = "insufficient_or_invalid_ohlc") -> dict[str, Any]:
    return {
        "atr_pct": 0.0,
        "slope": 0.0,
        "slope_norm": 0.0,
        "macd_hist": 0.0,
        "rsi14": 50.0,
        "contrib": {"ma_slope": 0.0, "macd": 0.0, "rsi": 0.0, "bollinger": 0.0},
        "indicator_space": "log_price_v1",
        "score": 0.0,
        "trend_strength": 0.0,
        "neutral_veto": 0.8,
        "mean_reversion_score": 0.0,
        "mean_reversion_evidence_valid": False,
        "return_autocorr_lag1": None,
        "variance_ratio_4": None,
        "sign_reversal_rate": None,
        "mean_reversion_observations": 0,
        "data_quality": reason,
    }

def vote_for_tf(closes: list[float], highs: list[float], lows: list[float]) -> dict[str, Any]:
    closes, highs, lows = _safe_ohlc_vectors(closes, highs, lows)
    if len(closes) < 2:
        return _neutral_tf_vote()

    # Raw indicators
    slope = ma_slope(closes)
    rsi = rsi14(closes)
    hist = macd_hist(closes)
    ap = atr_pct(highs, lows, closes)
    mean_reversion = mean_reversion_diagnostics(closes)

    # Soft contributions (normalized)
    # Slope normalized by ATR% to make TFs more comparable
    slope_norm = slope / max(1e-6, ap)
    # Increased sensitivity: 0.22 vs old 0.15 — slope is the most reliable trend signal
    slope_c = _clamp(slope_norm * 0.22, -1.0, 1.0)

    # MACD histogram and ATR are both in log-price units. Their ratio is
    # scale-free and mirror-symmetric across LONG/SHORT paths.
    hist_norm = hist / max(1e-9, ap)
    hist_c = _clamp(hist_norm * 4.0, -1.0, 1.0)

    # RSI centered at 50, tightened normalization for more signal pull
    rsi_c = _clamp((rsi - 50.0) / 30.0, -1.0, 1.0)

    # Bollinger Band %B: price position relative to 20-period BB
    # Acts as mean-reversion vs momentum confirmator
    log_closes = _log_levels(closes)
    closes_20 = log_closes[-20:] if len(log_closes) >= 20 else log_closes
    sma20 = sum(closes_20) / len(closes_20)
    std20 = math.sqrt(sum((x - sma20) ** 2 for x in closes_20) / len(closes_20)) if len(closes_20) > 1 else 1e-9
    bb_b = _clamp((log_closes[-1] - (sma20 - 2 * std20)) / max(1e-9, 4 * std20), 0.0, 1.0)  # 0=lower band, 1=upper band
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
        "contrib": {
            "ma_slope": float(slope_c),
            "macd": float(hist_c),
            "rsi": float(rsi_c),
            "bollinger": float(bb_c),
        },
        "indicator_space": "log_price_v1",
        "score": float(score),
        "trend_strength": float(trend_strength),
        "neutral_veto": float(neutral_veto),
        **mean_reversion,
    }

def _aggregate_signed(tf_map: dict[int, dict[str, Any]], tf_secs: list[int]) -> float:
    num = 0.0
    den = 0.0
    for tf in tf_secs:
        info = tf_map.get(tf)
        if not info:
            continue
        w = float(TF_WEIGHTS.get(tf, 1.0))
        num += w * _finite_float(info.get("score"), 0.0)
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
    total_possible = 0.0
    used = []
    for tf in all_tfs:
        info = tf_map.get(tf)
        if not info:
            continue
        w = float(TF_WEIGHTS.get(tf, 1.0))
        total_possible += w
        tf_sign = _sign(_finite_float(info.get("score"), 0.0), thr)
        if tf_sign != 0:
            total += w
            if struct_sign != 0 and tf_sign == struct_sign:
                agree += w
        used.append(tf)
    if total > 0 and struct_sign != 0:
        agree_ratio = agree / total
        active_coverage = _clamp(total / max(total_possible, 1e-9), 0.0, 1.0)
        coherence = float(0.5 + (agree_ratio - 0.5) * active_coverage)
    else:
        coherence = 0.5

    # Regime (trend/range/transition): based on trend_strength and coherence
    trendiness = 0.0
    den = 0.0
    for tf in all_tfs:
        info = tf_map.get(tf)
        if not info:
            continue
        w = float(TF_WEIGHTS.get(tf, 1.0))
        trendiness += w * _finite_float(info.get("trend_strength"), 0.0)
        den += w
    trendiness = float(trendiness / den) if den > 0 else 0.0

    mr_num = 0.0
    mr_den = 0.0
    mr_tf_count = 0
    for tf in all_tfs:
        info = tf_map.get(tf)
        if not info or info.get("mean_reversion_evidence_valid") is not True:
            continue
        score_value = _finite_float(info.get("mean_reversion_score"), -1.0)
        if score_value < 0.0:
            continue
        w = float(TF_WEIGHTS.get(tf, 1.0))
        mr_num += w * _clamp(score_value, 0.0, 1.0)
        mr_den += w
        mr_tf_count += 1
    total_mr_weight = sum(float(TF_WEIGHTS.get(tf, 1.0)) for tf in all_tfs)
    mean_reversion_score = float(mr_num / mr_den) if mr_den > 0.0 else 0.0
    mean_reversion_coverage = float(mr_den / total_mr_weight) if total_mr_weight > 0.0 else 0.0
    mean_reversion_evidence_valid = bool(mr_tf_count >= 3 and mean_reversion_coverage >= 0.40)

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

    # Bias is a weaker directional hint than `direction`, but it still should
    # remain neutral when the aggregate score itself is near-zero.
    all_sign = _sign(s_all, thr)
    if all_sign > 0:
        bias = "long"
    elif all_sign < 0:
        bias = "short"
    else:
        bias = "neutral"

    # Direction policy:
    # - weak aggregate stays neutral;
    # - clear trend regime can publish a directional thesis immediately;
    # - coherent range regime may also publish a *range-biased* directional thesis
    #   when both tactical and structural TFs point the same way strongly enough.
    range_biased = False
    range_bias_direction = "neutral"
    if regime == "range" and all_sign != 0 and struct_sign == all_sign:
        # Requirements are intentionally stricter than for ordinary directional breakout
        # direction: directional range is only allowed when the bias is visible on
        # both tactical and structural TFs and the multi-TF stack is coherent.
        if strength_all >= 0.15 and strength_struct >= 0.10 and coherence >= 0.62:
            range_biased = True
            range_bias_direction = "long" if all_sign > 0 else "short"

    if strength_all < 0.12:
        direction = "neutral"
    elif regime == "range":
        direction = range_bias_direction if range_biased else "neutral"
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

    # Regime confidence should be LOW near regime boundaries and HIGH when
    # trendiness is clearly in-range or clearly trending. The previous formula
    # (max(trendiness, 1-trendiness)) was almost always >= 0.5, making the
    # metric artificially high and uninformative.
    if regime == "trend":
        # Trend is only valid when both trendiness and coherence are sufficiently high.
        trend_part = _clamp((trendiness - 0.48) / 0.30, 0.0, 1.0)   # 0 at threshold, ~1 when clearly trending
        coh_part   = _clamp((coherence  - 0.50) / 0.35, 0.0, 1.0)
        regime_conf = 0.25 + 0.75 * (0.65 * trend_part + 0.35 * coh_part)
    elif regime == "range":
        range_part = _clamp((0.25 - trendiness) / 0.25, 0.0, 1.0)   # 1 near 0, 0 at boundary
        regime_conf = 0.25 + 0.75 * range_part
    else:  # transition
        mid = (0.25 + 0.48) / 2.0
        half_width = (0.48 - 0.25) / 2.0
        trans_part = 1.0 - _clamp(abs(trendiness - mid) / max(1e-9, half_width), 0.0, 1.0)
        regime_conf = 0.25 + 0.75 * trans_part
    regime_confidence = float(_clamp(regime_conf, 0.0, 1.0))

    return {
        "direction": direction,                    # long/short/neutral
        "bias": bias,                              # long/short
        "direction_mode": (
            "range_biased" if range_biased and direction in ("long", "short") else (
                "directional_grid_bias" if direction in ("long", "short") else "neutral"
            )
        ),
        "direction_confidence": direction_confidence,
        "scores": {"tactical": s_tactical, "structural": s_structural, "all": s_all},
        "strength": {"tactical": strength_tact, "structural": strength_struct, "all": strength_all},
        "trendiness": trendiness,                  # multi-TF trendiness proxy (NOT signed)
        "coherence": coherence,
        "regime": regime,                          # trend/range/transition
        "regime_confidence": regime_confidence,
        "mean_reversion_score": mean_reversion_score,
        "mean_reversion_evidence_valid": mean_reversion_evidence_valid,
        "mean_reversion_tf_count": int(mr_tf_count),
        "mean_reversion_tf_coverage": mean_reversion_coverage,
        "structural_veto_applied": veto_applied,
        "tf_used": used,
    }
