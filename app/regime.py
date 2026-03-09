from __future__ import annotations

from typing import Any

def classify_regime(features_by_symbol: list[dict[str, Any]]) -> dict[str, Any]:
    # aggregate simple stats
    if not features_by_symbol:
        return {"vol_state":"unknown","trend_state":"unknown","risk_state":"unknown","confidence":0.0}

    atr_pcts = [f.get("atr_pct", 0.0) for f in features_by_symbol if f.get("atr_pct") is not None]
    # Keep regime classification on the same trend metric that the scorer and gates use:
    # multi-timeframe unsigned trendiness from direction_agg.
    # Fall back to 1m trend_strength only when direction_agg is not available.
    trend = []
    for f in features_by_symbol:
        d = f.get("_direction_agg") or {}
        t = d.get("trendiness")
        if t is None:
            t = f.get("trend_strength")
        if t is not None:
            trend.append(float(t))
    spreads = [f.get("spread_bps", 0.0) for f in features_by_symbol if f.get("spread_bps") is not None]

    # 1h ATR — same source the scorer uses; only present after multi-TF pass
    atr_pcts_1h = [f["_atr_pct_1h"] for f in features_by_symbol
                   if f.get("_atr_pct_1h") is not None and f["_atr_pct_1h"] > 0]

    avg_atr = sum(atr_pcts) / max(1, len(atr_pcts))
    avg_trend = sum(trend) / max(1, len(trend))
    avg_spread = sum(spreads) / max(1, len(spreads)) if spreads else None
    avg_atr_1h = sum(atr_pcts_1h) / max(1, len(atr_pcts_1h)) if atr_pcts_1h else None

    # vol_state: prefer 1h ATR (matches scorer) over 1m ATR (regime display was 59× lower).
    # Thresholds align with _score() normalizers (0.06 = "normal/high" boundary).
    if avg_atr_1h is not None:
        if avg_atr_1h < 0.02:
            vol_state = "low"
        elif avg_atr_1h < 0.06:
            vol_state = "normal"
        else:
            vol_state = "high"
    else:
        # Fallback: 1m ATR (pre-multi-TF-pass or symbol without 1h data)
        if avg_atr < 0.003:
            vol_state = "low"
        elif avg_atr < 0.01:
            vol_state = "normal"
        else:
            vol_state = "high"

    if avg_trend > 0.6:
        trend_state = "trending"
    elif avg_trend < 0.35:
        trend_state = "ranging"
    else:
        trend_state = "mixed"

    # risk_state proxy: if spreads widen and vol high => risk_off-ish
    if vol_state == "high" and (avg_spread is not None and avg_spread > 8):
        risk_state = "risk_off"
    elif vol_state in ("low","normal") and (avg_spread is None or avg_spread < 6):
        risk_state = "risk_on"
    else:
        risk_state = "neutral"

    # ── Dynamic confidence ───────────────────────────────────────────────────
    # Confidence must reflect BOTH agreement and sample size / coverage. With one symbol
    # there is no cross-sectional confirmation, so high confidence would be pseudo-statistical.
    atr_n = len(atr_pcts) if atr_pcts else len(features_by_symbol)
    trend_n = len(trend) if trend else len(features_by_symbol)
    effective_n = max(1, min(atr_n, trend_n, len(features_by_symbol)))

    def _cv(vals: list[float]) -> float:
        """Coefficient of variation (std/mean). 0 = perfect agreement."""
        if len(vals) < 2:
            return 0.0
        mean = sum(vals) / len(vals)
        if mean == 0:
            return 0.0
        variance = sum((x - mean) ** 2 for x in vals) / len(vals)
        return (variance ** 0.5) / mean

    cv_atr = _cv(atr_pcts)       # 0 = all symbols same vol tier
    cv_trend = _cv(trend)        # 0 = all symbols same trend strength

    # Agreement score: 1.0 = perfect, 0.0 = complete disagreement.
    agreement = max(0.0, 1.0 - 0.5 * cv_atr - 0.5 * min(cv_trend, 1.0))
    sample_factor = max(0.0, min(1.0, (effective_n - 1) / 5.0))
    raw_conf = 0.25 + 0.40 * agreement + 0.20 * sample_factor
    cap = 0.40 if effective_n <= 1 else (0.52 if effective_n == 2 else (0.64 if effective_n == 3 else (0.75 if effective_n == 4 else 0.85)))
    confidence = round(min(cap, max(0.20, raw_conf)), 3)

    return {
        "vol_state": vol_state,
        "trend_state": trend_state,
        "risk_state": risk_state,
        "avg_atr_pct": avg_atr,
        "avg_atr_pct_1h": avg_atr_1h,  # 1h ATR used for vol_state classification
        "avg_trend_strength": avg_trend,
        "avg_spread_bps": avg_spread,
        "confidence": confidence,
        "confidence_detail": {
            "agreement": round(agreement, 3),
            "cv_atr": round(cv_atr, 3),
            "cv_trend": round(cv_trend, 3),
            "n_symbols": len(features_by_symbol),
            "n_effective": effective_n,
        },
    }
