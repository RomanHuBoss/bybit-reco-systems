from __future__ import annotations

import math
import secrets
from typing import Any

from . import db
from .features import compute_features_from_ohlcv
from .regime import classify_regime
from .risk import gate_candidate

BOT_TYPES = [
    "trend_follow",
    "mean_reversion",
    "grid",
    "breakout",
    "mm_scalp",
    "dca",
    "hedge",
]

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def _heur_expected_rr(bot_type: str, f: dict[str, Any], venue: str) -> float:
    # Very rough RR proxy from regime and ATR
    atr_pct = float(f.get("atr_pct") or 0.0)
    trend = float(f.get("trend_strength") or 0.0)
    rng = float(f.get("range_score") or 0.0)
    v_z = float(f.get("volume_z") or 0.0)
    base = 1.2
    if bot_type == "trend_follow":
        base += 0.8 * trend
    elif bot_type == "breakout":
        base += 0.4 * max(0.0, v_z/3.0) + 0.6 * trend
    elif bot_type == "mean_reversion":
        base += 0.5 * rng
    elif bot_type == "grid":
        base += 0.4 * rng - 0.3 * trend
    elif bot_type == "mm_scalp":
        base += 0.2 - 5.0 * atr_pct
    elif bot_type == "dca":
        base += 0.2
    elif bot_type == "hedge":
        base = 1.0
    return float(_clamp(base, 0.6, 3.5))

def _cost_penalty_bps(f: dict[str, Any], taker_fee_bps: float) -> float:
    spread = f.get("spread_bps")
    spread = float(spread) if spread is not None else 8.0
    return float(spread + taker_fee_bps)

def _direction_for_bot(bot_type: str, f: dict[str, Any]) -> str:
    slope = float(f.get("slope") or 0.0)
    if bot_type in ("trend_follow", "breakout", "mm_scalp", "grid"):
        return "long" if slope >= 0 else "short"
    if bot_type == "mean_reversion":
        return "short" if slope > 0 else "long"
    if bot_type == "dca":
        return "long"
    if bot_type == "hedge":
        return "hedge"
    return "neutral"

def _mode_for_venue(venue: str, direction: str) -> tuple[str, str]:
    # account_mode, margin_mode
    if venue == "spot":
        return ("oneway", "cash")
    # linear futures
    if direction == "hedge":
        return ("hedge", "isolated")
    return ("oneway", "isolated")

def _params_for_bot(bot_type: str, f: dict[str, Any]) -> dict[str, Any]:
    atr = float(f.get("atr") or 0.0)
    atr_pct = float(f.get("atr_pct") or 0.0)
    vol_z = float(f.get("volume_z") or 0.0)
    trend = float(f.get("trend_strength") or 0.0)
    rng = float(f.get("range_score") or 0.0)

    # default risk sizing placeholders (operator can override in UI)
    risk_per_trade = 0.003 if atr_pct < 0.01 else 0.002
    sl_atr = 1.5 if bot_type in ("trend_follow","breakout") else 1.2
    if bot_type == "grid":
        sl_atr = 2.2
    if bot_type == "mm_scalp":
        sl_atr = 0.8

    tp1 = 1.2
    tp2 = 2.0
    tp3 = 3.0
    if bot_type == "mean_reversion":
        tp1, tp2, tp3 = 0.9, 1.4, 2.0
    if bot_type == "mm_scalp":
        tp1, tp2, tp3 = 0.5, 0.9, 1.2

    params = {
        "risk_per_trade": risk_per_trade,
        "sl_atr_mult": sl_atr,
        "tp_atr_mults": [tp1, tp2, tp3],
        "time_stop_min": 60 if bot_type in ("trend_follow","breakout") else 30,
        "max_slippage_bps": 8 if bot_type != "mm_scalp" else 4,
        "signal_ttl_sec": 180,
    }

    if bot_type == "grid":
        params.update({
            "grid_spacing_pct": float(_clamp(atr_pct*100*0.6, 0.15, 1.2)),
            "levels": int(_clamp(6 + rng*8, 6, 16)),
            "disable_on_breakout": True,
        })
    if bot_type == "trend_follow":
        params.update({"trail_atr_mult": 1.8, "pullback_entry": True})
    if bot_type == "breakout":
        params.update({"breakout_lookback": 60, "vol_confirm_z": 1.0, "volume_z": vol_z})
    if bot_type == "mm_scalp":
        params.update({"post_only": True, "refresh_ms": 900, "quote_spread_bps": float(_clamp((f.get("spread_bps") or 5.0)*0.8, 1.0, 8.0))})
    if bot_type == "dca":
        params.update({"step_pct": float(_clamp(atr_pct*100*0.7, 0.2, 2.0)), "max_steps": 6})
    if bot_type == "hedge":
        params.update({"hedge_ratio": 0.35, "trigger": "risk_off_or_vol_high"})

    return params

def _score_bot(bot_type: str, f: dict[str, Any], venue: str, taker_fee_bps: float) -> tuple[float, float, dict[str, Any]]:
    trend = float(f.get("trend_strength") or 0.0)
    rng = float(f.get("range_score") or 0.0)
    atr_pct = float(f.get("atr_pct") or 0.0)
    v_z = float(f.get("volume_z") or 0.0)
    spread = f.get("spread_bps")
    spread = float(spread) if spread is not None else 8.0

    cost_bps = _cost_penalty_bps(f, taker_fee_bps=taker_fee_bps)
    cost_penalty = _clamp(cost_bps / 30.0, 0.0, 1.5)  # scaled

    # rule score per bot
    rule = 0.0
    pos = []
    neg = []

    def add_pos(name, val, w, txt):
        pos.append({"feature": name, "value": val, "weight": w, "text": txt})
    def add_neg(name, val, w, txt):
        neg.append({"feature": name, "value": val, "weight": w, "text": txt})

    if bot_type == "trend_follow":
        rule = 1.4*trend + 0.4*max(0.0, v_z/3.0) - 0.7*rng - 1.0*max(0.0, atr_pct-0.02)*20
        add_pos("trend_strength", trend, 1.4, "сильная трендовость")
        add_pos("volume_z", v_z, 0.4, "подтверждение объёмом")
        add_neg("range_score", rng, -0.7, "флет снижает качество тренда")
    elif bot_type == "mean_reversion":
        rule = 1.3*rng - 0.9*trend - 0.5*max(0.0, v_z/3.0) - 0.4*max(0.0, atr_pct-0.015)*20
        add_pos("range_score", rng, 1.3, "флет/диапазон благоприятен")
        add_neg("trend_strength", trend, -0.9, "тренд повышает риск продолжения движения")
        add_neg("volume_z", v_z, -0.5, "импульсный объём против mean-revert")
    elif bot_type == "grid":
        rule = 1.2*rng - 1.1*trend - 0.6*max(0.0, atr_pct-0.012)*20 - 0.3*max(0.0, v_z/3.0)
        add_pos("range_score", rng, 1.2, "флет подходит для grid")
        add_neg("trend_strength", trend, -1.1, "тренд ломает сетку")
        add_neg("atr_pct", atr_pct, -0.6, "слишком высокая волатильность для grid")
    elif bot_type == "breakout":
        rule = 0.9*trend + 0.9*max(0.0, v_z/2.5) - 0.3*rng
        add_pos("trend_strength", trend, 0.9, "подготовка к движению")
        add_pos("volume_z", v_z, 0.9, "аномалия объёма")
    elif bot_type == "mm_scalp":
        rule = 0.6*(1.0 - _clamp(spread/12.0, 0.0, 1.0)) - 1.2*_clamp(atr_pct/0.015, 0.0, 2.0)
        add_pos("spread_bps", spread, 0.6, "узкий спред повышает edge")
        add_neg("atr_pct", atr_pct, -1.2, "высокая волатильность опасна для скальпа")
    elif bot_type == "dca":
        rule = 0.3 + 0.2*rng - 0.4*_clamp(atr_pct/0.02, 0.0, 2.0)
        add_pos("range_score", rng, 0.2, "флет лучше для усреднения")
        add_neg("atr_pct", atr_pct, -0.4, "высокая волатильность увеличивает риск просадки")
    elif bot_type == "hedge":
        rule = 0.2 + 0.6*_clamp(atr_pct/0.02, 0.0, 2.0) + 0.2*trend
        add_pos("atr_pct", atr_pct, 0.6, "рост волатильности => хедж")
        add_pos("trend_strength", trend, 0.2, "направленный риск => хедж")

    # confidence from rule and penalties
    raw = rule - 0.7*cost_penalty
    confidence = _clamp(_sigmoid(raw), 0.0, 1.0)
    score = _clamp((raw / 2.2), -1.0, 1.0)

    reasons = {
        "summary": "rules-based scoring (MVP); расширяется ML-калибратором",
        "top_positive_factors": sorted(pos, key=lambda x: abs(x["weight"]), reverse=True)[:4],
        "top_negative_factors": sorted(neg, key=lambda x: abs(x["weight"]), reverse=True)[:4],
        "cost_model": {"spread_bps": spread, "taker_fee_bps": taker_fee_bps, "total_cost_bps": cost_bps},
    }
    return float(score), float(confidence), reasons

def run_recommender_once(conn, settings) -> dict[str, Any]:
    # Build features for configured symbols
    features_all: list[dict[str, Any]] = []
    symbol_feature_map: dict[tuple[str,str], dict[str, Any]] = {}

    for venue in settings.venues:
        symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
        for sym in symbols:
            rows = db.get_latest_ohlcv(conn, venue, sym, tf_sec=60, limit=180)
            trow = db.get_latest_ticker(conn, venue, sym)
            ticker = dict(trow) if trow else None
            f = compute_features_from_ohlcv([dict(r) for r in rows], ticker)
            if not f:
                continue
            ts_f = int(f["ts_last"])
            db.insert_features(conn, venue, sym, ts_f, f)
            features_all.append(f)
            symbol_feature_map[(venue, sym)] = f

    # Regime snapshot
    regime = classify_regime(features_all)
    db.insert_regime(conn, db.now_ts(), regime)

    # Candidate recommendations
    limits = db.get_active_risk_limits(conn) or settings.risk_limits
    model_version = "rules-v1"
    ts_now = db.now_ts()

    recs: list[dict[str, Any]] = []

    for (venue, sym), f in symbol_feature_map.items():
        taker_fee_bps = settings.taker_fee_bps_spot if venue == "spot" else settings.taker_fee_bps_linear

        for bot_type in BOT_TYPES:
            # basic feasibility gating
            spread = f.get("spread_bps")
            spread = float(spread) if spread is not None else 12.0
            atr_pct = float(f.get("atr_pct") or 0.0)
            rng = float(f.get("range_score") or 0.0)
            trend = float(f.get("trend_strength") or 0.0)

            feasibility_blocks = []
            if bot_type in ("mm_scalp", "grid") and (spread > 12.0):
                feasibility_blocks.append({"code":"SPREAD_TOO_WIDE", "msg": f"spread_bps={spread:.2f} too wide for {bot_type}"})
            if bot_type == "mm_scalp" and atr_pct > 0.018:
                feasibility_blocks.append({"code":"VOL_TOO_HIGH", "msg": f"atr_pct={atr_pct:.4f} too high for mm_scalp"})
            if bot_type == "grid" and trend > 0.55:
                feasibility_blocks.append({"code":"TREND_TOO_STRONG", "msg": f"trend_strength={trend:.2f} too high for grid"})
            if bot_type == "breakout" and rng > 0.75 and abs(float(f.get('slope') or 0.0)) < 0.001:
                feasibility_blocks.append({"code":"NO_BREAKOUT_SETUP", "msg":"range is too stable; breakout not confirmed"})
            if venue == "spot" and bot_type in ("hedge",) :
                feasibility_blocks.append({"code":"VENUE_NOT_SUPPORTED", "msg":"hedge bot requires derivatives (linear)"})

            # score
            score, conf, reasons = _score_bot(bot_type, f, venue, taker_fee_bps=taker_fee_bps)
            expected_rr = _heur_expected_rr(bot_type, f, venue)
            direction = _direction_for_bot(bot_type, f)
            account_mode, margin_mode = _mode_for_venue(venue, direction)

            blocks = feasibility_blocks + gate_candidate(conn, venue, sym, limits)

            status = "recommended"
            if blocks:
                status = "blocked"
            if score < settings.min_score_to_recommend or conf < settings.min_conf_to_recommend:
                status = "no_trade"

            risk_score = float(_clamp(atr_pct/0.02, 0.0, 1.0))

            rec_id = f"R-{ts_now}-{venue}-{sym}-{bot_type}-{secrets.token_hex(4)}"
            reasons2 = dict(reasons)
            reasons2["regime"] = regime
            reasons2["risk_checks"] = {"passed": len(blocks)==0, "blocks": blocks}

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
                "params": _params_for_bot(bot_type, f),
                "reasons": reasons2,
                "blocks": blocks,
                "status": status,
                "ttl_sec": 180,
                "model_version": model_version,
                "features_ref_ts": int(f["ts_last"]),
            })

    # Decide NO-TRADE if nothing good
    # (Keep writing all recs; API can filter)
    # Persist
    if recs:
        db.insert_recommendations(conn, recs)
        db.log_decision(conn, "PUBLISH", None, None, {"count": len(recs), "model_version": model_version, "regime": regime})

    return {"regime": regime, "count": len(recs)}
