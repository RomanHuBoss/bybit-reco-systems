from __future__ import annotations

import math
import secrets
from typing import Any

from . import db
from .features import compute_features_from_ohlcv
from .regime import classify_regime
from .risk import gate_candidate
from .direction import vote_for_tf, aggregate_direction
from .sentiment_features import compute_sentiment_agg
from .calibration import fit_platt, PlattScaler, save_platt_to_db, load_platt_from_db

BOT_TYPES_BYBIT = [
    "spot_grid",
    "futures_grid",
    "dca_bot",
    "futures_martingale",
    "futures_combo",
]

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

    if bot_type in ("spot_grid", "futures_grid"):
        # neutral unless there is a meaningful directional drift
        if rng >= 0.65 and trend <= 0.25 and abs(slope) <= 0.0015:
            return "neutral"
        return "long" if slope >= 0 else "short"

    if bot_type in ("futures_martingale",):
        return "long" if slope >= 0 else "short"

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
) -> dict[str, Any]:
    atr_pct_1m = float(f.get("atr_pct") or 0.0)
    atr_pct = float(atr_pct_for_grid) if atr_pct_for_grid is not None else atr_pct_1m

    risk_per_trade = 0.003 if atr_pct < 0.01 else 0.002
    if global_sent < -0.4:
        risk_per_trade *= 0.7

    if bot_type in ("spot_grid", "futures_grid"):
        spread_bps = float(f.get("spread_bps") or 8.0)
        total_cost_bps = spread_bps + float(taker_fee_bps)

        base_step_pct = atr_pct * 100.0 * 0.6
        min_step_pct = max(0.08, (total_cost_bps * 1.8) / 100.0)
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
        if direction == "long":
            lower_mul, upper_mul = 1.20, 0.80
        elif direction == "short":
            lower_mul, upper_mul = 0.80, 1.20
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
            "leverage": leverage,
            "price_range_lower": price_range_lower,
            "price_range_upper": price_range_upper,
            "investment_risk_per_trade": risk_per_trade,
            "notes": "Levels/spacing рассчитываются по ATR% старшего ТФ (если доступно), с учётом cost-floor. Диапазон: price_range_lower/upper.",
        }

    if bot_type == "dca_bot":
        step_pct = float(_clamp(atr_pct_1m * 100.0 * 0.7, 0.2, 2.0))
        max_orders = 6 if global_sent >= -0.4 else 4
        return {
            "bybit_category": "DCA Bot",
            "direction": "long",
            "dca_step_pct": step_pct,
            "max_orders": max_orders,
            "take_profit_mode": "auto",
            "investment_risk_per_trade": risk_per_trade,
        }

    if bot_type == "futures_martingale":
        leverage = 2 if global_sent < 0 else 3
        step_pct = float(_clamp(atr_pct_1m * 100.0 * 0.9, 0.3, 2.5))
        max_steps = 5 if global_sent >= -0.4 else 4
        return {
            "bybit_category": "Futures Martingale",
            "direction": "long_or_short",
            "step_pct": step_pct,
            "max_steps": max_steps,
            "leverage": leverage,
            "investment_risk_per_trade": risk_per_trade * 0.7,
            "warning": "Мартингейл сильно увеличивает риск. Используйте только при высокой согласованности направления.",
        }

    if bot_type == "futures_combo":
        return {
            "bybit_category": "Futures Combo",
            "mode": "hedge",
            "allocation": {"leg1": 0.6, "leg2": 0.4},
            "investment_risk_per_trade": risk_per_trade * 0.6,
            "notes": "Комбо трактуем как hedge/carry подсказку.",
        }

    return {"investment_risk_per_trade": risk_per_trade}

def _expected_rr(bot_type: str, f: dict[str, Any]) -> float:
    atr_pct = float(f.get("atr_pct") or 0.0)
    if bot_type in ("spot_grid","futures_grid"):
        return float(_clamp(1.2 - 8*atr_pct, 0.6, 2.0))
    if bot_type == "dca_bot":
        return float(_clamp(1.3 - 6*atr_pct, 0.7, 2.2))
    if bot_type == "futures_martingale":
        return float(_clamp(1.1 - 10*atr_pct, 0.5, 1.6))
    if bot_type == "futures_combo":
        return 1.0
    return 1.0

def _score(bot_type: str, venue: str, f: dict[str, Any], taker_fee_bps: float, global_sent: float) -> tuple[float, float, dict[str, Any]]:
    trend = float(f.get("trend_strength") or 0.0)
    rng = float(f.get("range_score") or 0.0)
    atr_pct = float(f.get("atr_pct") or 0.0)
    spread = f.get("spread_bps")
    spread = float(spread) if spread is not None else 8.0

    cost_bps = spread + taker_fee_bps
    cost_penalty = _clamp(cost_bps / 30.0, 0.0, 1.5)

    sent = float(global_sent)
    pos, neg = [], []
    def add_pos(name, val, w, txt): pos.append({"feature": name, "value": val, "weight": w, "text": txt})
    def add_neg(name, val, w, txt): neg.append({"feature": name, "value": val, "weight": w, "text": txt})

    rule = 0.0

    if bot_type == "spot_grid":
        rule = 1.4*rng - 1.0*trend - 0.6*_clamp(atr_pct/0.015, 0.0, 2.0) + 0.2*max(-0.5, min(0.5, sent))
        add_pos("range_score", rng, 1.4, "флет/диапазон подходит для Spot Grid")
        add_neg("trend_strength", trend, -1.0, "сильный тренд опасен для grid")
        add_neg("atr_pct", atr_pct, -0.6, "высокая волатильность ухудшает grid")
        add_pos("global_sentiment", sent, 0.2, "сентимент влияет на риск-режим")
    elif bot_type == "futures_grid":
        rule = 1.2*rng - 0.9*trend - 0.7*_clamp(atr_pct/0.018, 0.0, 2.0) + 0.2*sent
        add_pos("range_score", rng, 1.2, "флет подходит для Futures Grid")
        add_neg("trend_strength", trend, -0.9, "тренд ломает сетку")
        add_neg("atr_pct", atr_pct, -0.7, "волатильность повышает риск ликвидации")
        add_pos("global_sentiment", sent, 0.2, "сентимент учитывается")
    elif bot_type == "dca_bot":
        rule = 0.4 + 0.5*_clamp(0.5 + sent, 0.0, 1.0) - 0.7*_clamp(atr_pct/0.02, 0.0, 2.0)
        add_pos("global_sentiment", sent, 0.5, "нейтральный/позитивный сентимент поддерживает DCA")
        add_neg("atr_pct", atr_pct, -0.7, "высокая волатильность повышает риск просадки")
    elif bot_type == "futures_martingale":
        rule = 0.8*rng - 0.8*_clamp(atr_pct/0.018, 0.0, 2.0) + 0.4*_clamp(sent+0.2, 0.0, 1.0) - 0.2*trend
        add_pos("range_score", rng, 0.8, "мартингейл только в диапазоне")
        add_neg("atr_pct", atr_pct, -0.8, "волатильность опасна для мартингейла")
        add_pos("global_sentiment", sent, 0.4, "негативный сентимент блокирует мартингейл")
        add_neg("trend_strength", trend, -0.2, "тренд увеличивает риск")
    elif bot_type == "futures_combo":
        rule = 0.3 + 0.7*_clamp(-sent, 0.0, 1.0) + 0.4*_clamp(atr_pct/0.02, 0.0, 2.0)
        add_pos("global_sentiment", sent, 0.7, "risk-off сентимент => комбо/хедж")
        add_pos("atr_pct", atr_pct, 0.4, "рост волатильности => хеджирование")

    raw = rule - 0.7*cost_penalty
    score = float(_clamp(raw / 2.2, -1.0, 1.0))
    conf0 = float(_clamp(_sigmoid(raw), 0.0, 1.0))
    reasons = {
        "summary": "Рекомендация в терминах Bybit Trading Bot (Scenario B). Направление определяется голосованием индикаторов на 15m/30m/1h/4h/1d. Сентимент — multi-horizon EWMA (1h/6h/1d/7d) с консолидацией risk_on/off/neutral. Уверенность калибруется (Platt) по 30-мин forward доходности из OHLCV.",
        "top_positive_factors": sorted(pos, key=lambda x: abs(x["weight"]), reverse=True)[:5],
        "top_negative_factors": sorted(neg, key=lambda x: abs(x["weight"]), reverse=True)[:5],
        "cost_model": {"spread_bps": spread, "taker_fee_bps": taker_fee_bps, "total_cost_bps": cost_bps},
        "global_sentiment": sent,
    }
    return score, conf0, reasons

def _fit_calibrator(conn, min_samples: int) -> PlattScaler:
    outs = db.get_outcomes_recent(conn, limit=4000)
    xs, ys = [], []
    for o in outs:
        r = db.get_recommendation_by_id(conn, o["rec_id"])
        if not r:
            continue
        xs.append(float(r["score"]))
        ys.append(int(o["success"]))
    return fit_platt(xs, ys) if len(xs) >= min_samples else PlattScaler(fitted=False)


def _fit_direction_calibrator(conn, min_samples: int) -> PlattScaler:
    # Fit on realized outcomes using signed direction score (direction_agg.scores.all).
    outs = db.get_outcomes_recent(conn, limit=5000)
    xs, ys = [], []
    for o in outs:
        r = db.get_recommendation_by_id(conn, o["rec_id"])
        if not r:
            continue
        # only direction-sensitive bots
        if r.get("bot_type") not in ("spot_grid","futures_grid","futures_martingale"):
            continue
        reasons = r.get("reasons") or {}
        d = reasons.get("direction_agg") or {}
        x = float((d.get("scores") or {}).get("all", 0.0))
        # use only if direction is not neutral
        dir_final = str(d.get("direction") or "neutral")
        if dir_final == "neutral":
            continue
        xs.append(x)
        ys.append(int(o["success"]))
    return fit_platt(xs, ys) if len(xs) >= min_samples else PlattScaler(fitted=False)

def _load_or_fit_direction_calibrator(conn, min_samples: int) -> PlattScaler:
    scaler = _fit_direction_calibrator(conn, min_samples=min_samples)
    if scaler.fitted:
        save_platt_to_db(conn, "platt_direction_v2", scaler)
        return scaler
    saved = load_platt_from_db(conn, "platt_direction_v2")
    if saved and saved.fitted:
        return saved
    return scaler

def _load_or_fit_calibrator(conn, min_samples: int) -> PlattScaler:
    scaler = _fit_calibrator(conn, min_samples=min_samples)
    if scaler.fitted:
        save_platt_to_db(conn, "platt_bybit_v2", scaler)
        return scaler
    saved = load_platt_from_db(conn, "platt_bybit_v2")
    if saved and saved.fitted:
        return saved
    return scaler

def run_recommender_once(conn, settings) -> dict[str, Any]:
    sent_agg = compute_sentiment_agg(conn, scope="global", key="crypto")
    # Use 6h EWMA as the primary numeric sentiment input for scoring
    global_sent = float(sent_agg.get("ewma", {}).get("6h", 0.0))

    calibrator = _load_or_fit_calibrator(conn, min_samples=settings.calib_min_samples)
    dir_calibrator = _load_or_fit_direction_calibrator(conn, min_samples=max(60, settings.calib_min_samples))

    features_all: list[dict[str, Any]] = []
    symbol_feature_map: dict[tuple[str,str], dict[str, Any]] = {}

    for venue in settings.venues:
        symbols = settings.symbols_spot if venue == "spot" else settings.symbols_linear
        for sym in symbols:
            rows = db.get_latest_ohlcv(conn, venue, sym, tf_sec=60, limit=220)
            trow = db.get_latest_ticker(conn, venue, sym)
            ticker = dict(trow) if trow else None
            f = compute_features_from_ohlcv([dict(r) for r in rows], ticker)
            if not f:
                continue
            # Multi-timeframe direction voting (15m/30m/1h/4h/1d)
            tf_secs = [15*60, 30*60, 60*60, 240*60, 24*60*60]
            tf_map = {}
            atr_1h = None
            for tf in tf_secs:
                rows_tf = db.get_latest_ohlcv(conn, venue, sym, tf_sec=tf, limit=260 if tf<=3600 else 420)
                if not rows_tf or len(rows_tf) < 80:
                    continue
                closes_tf = [float(r["close"]) for r in rows_tf]
                highs_tf = [float(r["high"]) for r in rows_tf]
                lows_tf = [float(r["low"]) for r in rows_tf]
                info = vote_for_tf(closes_tf, highs_tf, lows_tf)
                tf_map[tf] = info
                if tf == 60*60:
                    atr_1h = float(info.get("atr_pct") or 0.0)

            agg = aggregate_direction(tf_map) if tf_map else {"direction":"neutral","bias":"neutral","direction_confidence":0.5,"scores":{"tactical":0,"structural":0,"all":0},"strength":{"tactical":0,"structural":0,"all":0},"coherence":0.5,"regime":"unknown","regime_confidence":0.0,"structural_veto_applied":False,"tf_used":[]}
            f["_direction_agg"] = agg
            f["_atr_pct_1h"] = atr_1h
            ts_f = int(f["ts_last"])
            db.insert_features(conn, venue, sym, ts_f, f)
            features_all.append(f)
            symbol_feature_map[(venue, sym)] = f

    regime = classify_regime(features_all)
    db.insert_regime(conn, db.now_ts(), regime)

    limits = db.get_active_risk_limits(conn) or settings.risk_limits
    model_version = "bybit-taxonomy-v2"
    ts_now = db.now_ts()

    recs: list[dict[str, Any]] = []

    for (venue, sym), f in symbol_feature_map.items():
        taker_fee_bps = settings.taker_fee_bps_spot if venue == "spot" else settings.taker_fee_bps_linear

        for bot_type in BOT_TYPES_BYBIT:
            if bot_type == "spot_grid" and venue != "spot":
                continue
            if bot_type in ("futures_grid","futures_martingale","futures_combo") and venue != "linear":
                continue

            spread = f.get("spread_bps")
            spread = float(spread) if spread is not None else 12.0
            atr_pct = float(f.get("atr_pct") or 0.0)
            trend = float(f.get("trend_strength") or 0.0)

            feasibility_blocks = []
            if bot_type in ("spot_grid","futures_grid") and spread > 14.0:
                feasibility_blocks.append({"code":"SPREAD_TOO_WIDE", "msg": f"spread_bps={spread:.2f} слишком широкий для grid"})
            if bot_type in ("spot_grid","futures_grid") and trend > 0.60:
                feasibility_blocks.append({"code":"TREND_TOO_STRONG", "msg": f"trend_strength={trend:.2f} слишком сильный тренд для grid"})
            dir_tmp = f.get("_direction_agg", {})
            dir_conf = float(dir_tmp.get("direction_confidence", 0.5))
            if bot_type == "futures_martingale" and (atr_pct > 0.018 or sent_agg.get("flags", {}).get("panic") or (sent_agg.get("regime") == "risk_off" and sent_agg.get("strength", 0.0) >= 0.35) or global_sent < -0.45 or dir_conf < 0.65):
                feasibility_blocks.append({"code":"DIR_CONF_TOO_LOW" if dir_conf < 0.65 else "MARTINGALE_BLOCKED", "msg": f"atr_pct={atr_pct:.4f}, sentiment6h={global_sent:.2f}, dir_conf={dir_conf:.2f} => запрет"})
                # skip further checks below by continuing to scoring

                feasibility_blocks.append({"code":"MARTINGALE_BLOCKED", "msg": f"atr_pct={atr_pct:.4f} или sentiment={global_sent:.2f} => запрет"})
            if bot_type == "dca_bot" and (sent_agg.get("flags", {}).get("panic") or global_sent < -0.70):
                feasibility_blocks.append({"code":"DCA_BLOCKED_PANIC", "msg": f"sentiment={global_sent:.2f} panic => запрет"})

            score, conf0, reasons = _score(bot_type, venue, f, taker_fee_bps=taker_fee_bps, global_sent=global_sent)
            conf = calibrator.predict(score) if calibrator.fitted else conf0

            expected_rr = _expected_rr(bot_type, f)
            direction = _direction(bot_type, f.get('_direction_agg', {}))
            account_mode, margin_mode = _mode(venue, direction)

            blocks = feasibility_blocks + gate_candidate(conn, venue, sym, limits)

            status = "recommended"
            if blocks:
                status = "blocked"
            if score < settings.min_score_to_recommend:
                status = "no_trade"
            elif settings.require_conf_gate and conf < settings.min_conf_to_recommend:
                status = "no_trade"

            risk_score = float(_clamp(atr_pct/0.02, 0.0, 1.0))

            rec_id = f"R-{ts_now}-{venue}-{sym}-{bot_type}-{secrets.token_hex(4)}"
            reasons2 = dict(reasons)
            reasons2["regime"] = regime
            reasons2["risk_checks"] = {"passed": len(blocks)==0, "blocks": blocks}
            reasons2["sentiment_agg"] = sent_agg
            # Calibrate direction confidence separately (if model fitted)
            dtmp = f.get("_direction_agg", {})
            xdir = float((dtmp.get("scores") or {}).get("all", 0.0))
            dir_conf_cal = dir_calibrator.predict(xdir) if dir_calibrator.fitted else float(dtmp.get("direction_confidence", 0.5))
            dtmp["direction_confidence_calibrated"] = dir_conf_cal
            dtmp["direction_confidence_model"] = {"type":"platt_scaling","fitted": dir_calibrator.fitted, "a": getattr(dir_calibrator,"a",None), "b": getattr(dir_calibrator,"b",None)}
            reasons2["direction_agg"] = dtmp
            reasons2["confidence_model"] = {"type":"platt_scaling","fitted":calibrator.fitted,"a": getattr(calibrator,'a',None),"b": getattr(calibrator,'b',None)}

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
                "params": _params(
                    bot_type,
                    venue,
                    f,
                    global_sent=global_sent,
                    direction=direction,
                    taker_fee_bps=taker_fee_bps,
                    direction_bias=str(f.get("_direction_agg", {}).get("bias", "neutral")),
                    direction_bias_strength=float((f.get("_direction_agg", {}).get("strength", {}) or {}).get("all", 0.0) if isinstance(f.get("_direction_agg", {}).get("strength"), dict) else float(f.get("_direction_agg", {}).get("strength", 0.0))),
                    atr_pct_for_grid=f.get("_atr_pct_1h"),
                ),
                "reasons": reasons2,
                "blocks": blocks,
                "status": status,
                "ttl_sec": 180,
                "model_version": model_version,
                "features_ref_ts": int(f["ts_last"]),
            })

    if recs:
        # Publish only one best recommendation per (venue, symbol).
        # Others are stored as 'suppressed' for audit/debug.
        best_map: dict[tuple[str, str], dict[str, Any]] = {}
        for r in recs:
            key = (r["venue"], r["symbol"])
            cur = best_map.get(key)
            if (cur is None) or (r["confidence"] > cur["confidence"]) or (
                r["confidence"] == cur["confidence"] and r["score"] > cur["score"]
            ):
                best_map[key] = r

        for r in recs:
            key = (r["venue"], r["symbol"])
            if best_map.get(key, {}).get("rec_id") != r["rec_id"]:
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
