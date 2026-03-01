from __future__ import annotations

from typing import Any

def classify_regime(features_by_symbol: list[dict[str, Any]]) -> dict[str, Any]:
    # aggregate simple stats
    if not features_by_symbol:
        return {"vol_state":"unknown","trend_state":"unknown","risk_state":"unknown","confidence":0.0}

    atr_pcts = [f.get("atr_pct", 0.0) for f in features_by_symbol if f.get("atr_pct") is not None]
    trend = [f.get("trend_strength", 0.0) for f in features_by_symbol if f.get("trend_strength") is not None]
    spreads = [f.get("spread_bps", 0.0) for f in features_by_symbol if f.get("spread_bps") is not None]

    avg_atr = sum(atr_pcts) / max(1, len(atr_pcts))
    avg_trend = sum(trend) / max(1, len(trend))
    avg_spread = sum(spreads) / max(1, len(spreads)) if spreads else None

    # vol bins (heuristic)
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
    # Based on agreement between symbols — low variance = high confidence
    n = max(1, len(atr_pcts))

    def _cv(vals: list[float]) -> float:
        """Coefficient of variation (std/mean). 0 = perfect agreement."""
        if len(vals) < 2:
            return 0.0
        mean = sum(vals) / len(vals)
        if mean == 0:
            return 0.0
        variance = sum((x - mean) ** 2 for x in vals) / len(vals)
        return (variance ** 0.5) / mean

    cv_atr   = _cv(atr_pcts)   # 0 = all symbols same vol tier
    cv_trend = _cv(trend)       # 0 = all symbols same trend strength

    # Agreement score: 1.0 = perfect, 0.0 = complete disagreement
    agreement = max(0.0, 1.0 - 0.5 * cv_atr - 0.5 * min(cv_trend, 1.0))

    # Sample size bonus: more symbols → more reliable
    sample_bonus = min(0.10, (n - 1) * 0.005)

    confidence = round(min(0.95, max(0.20, 0.45 + 0.40 * agreement + sample_bonus)), 3)

    return {
        "vol_state": vol_state,
        "trend_state": trend_state,
        "risk_state": risk_state,
        "avg_atr_pct": avg_atr,
        "avg_trend_strength": avg_trend,
        "avg_spread_bps": avg_spread,
        "confidence": confidence,
        "confidence_detail": {
            "agreement": round(agreement, 3),
            "cv_atr": round(cv_atr, 3),
            "cv_trend": round(cv_trend, 3),
            "n_symbols": n,
        },
    }
