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

    confidence = 0.65 if vol_state != "unknown" else 0.2
    return {
        "vol_state": vol_state,
        "trend_state": trend_state,
        "risk_state": risk_state,
        "avg_atr_pct": avg_atr,
        "avg_trend_strength": avg_trend,
        "avg_spread_bps": avg_spread,
        "confidence": confidence,
    }
