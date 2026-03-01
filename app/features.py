from __future__ import annotations

import math
from typing import Any

def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b

def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    v = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(v)

def _sma(xs: list[float], n: int) -> float:
    if not xs:
        return 0.0
    n = max(1, min(n, len(xs)))
    return sum(xs[-n:]) / n

def compute_features_from_ohlcv(ohlcv_rows: list[dict[str, Any]] | list[Any], ticker: dict[str, Any] | None) -> dict[str, Any] | None:
    # expects rows ordered old->new with keys close/high/low/volume/ts
    if not ohlcv_rows or len(ohlcv_rows) < 30:
        return None

    closes = [float(r["close"]) for r in ohlcv_rows]
    highs = [float(r["high"]) for r in ohlcv_rows]
    lows = [float(r["low"]) for r in ohlcv_rows]
    vols = [float(r["volume"]) for r in ohlcv_rows]

    last = closes[-1]
    rets = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            rets.append(math.log(closes[i] / closes[i-1]))
    rv = _std(rets[-60:]) * math.sqrt(60)  # rough

    # ATR (very rough)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    atr = _sma(trs, 14)
    atr_pct = atr / last if last else 0.0

    sma_fast = _sma(closes, 20)
    sma_slow = _sma(closes, 60)
    slope = (sma_fast - sma_slow) / last if last else 0.0

    # Trend strength proxy
    trend_strength = min(1.0, abs(slope) * 20.0)  # heuristic scaling

    # Range score
    range_score = max(0.0, 1.0 - trend_strength)

    # Volume anomaly (zscore on last 30)
    v_window = vols[-60:]
    v_mean = sum(v_window) / len(v_window)
    v_std = _std(v_window)
    v_z = (vols[-1] - v_mean) / (v_std + 1e-9)

    # Spread (bps) from ticker
    spread_bps = None
    bid = ask = None
    if ticker:
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if bid and ask and (bid + ask) > 0:
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 1e4 if mid else None

    return {
        "price": last,
        "rv": rv,
        "atr": atr,
        "atr_pct": atr_pct,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "slope": slope,
        "trend_strength": trend_strength,
        "range_score": range_score,
        "volume_z": float(v_z),
        "spread_bps": float(spread_bps) if spread_bps is not None else None,
        "bid": bid,
        "ask": ask,
        "ts_last": int(ohlcv_rows[-1]["ts"]),
    }


# ── Liquidity tier ────────────────────────────────────────────────────────────
# vol24h in USD (turnover24h from ticker)

LIQUIDITY_TIERS = {
    "high":   20_000_000,   # > $20M/day  — all bots OK
    "medium":  2_000_000,   # > $2M/day   — grid OK, martingale reduced
    "low":       500_000,   # > $500K/day — spot grid only, small params
    # below $500K → "micro": grid forbidden
}

def liquidity_tier(turnover24h_usd: float | None) -> str:
    if turnover24h_usd is None:
        return "unknown"
    v = float(turnover24h_usd)
    if v >= LIQUIDITY_TIERS["high"]:
        return "high"
    if v >= LIQUIDITY_TIERS["medium"]:
        return "medium"
    if v >= LIQUIDITY_TIERS["low"]:
        return "low"
    return "micro"


# ── Funding rate signal ───────────────────────────────────────────────────────

def funding_signal(funding_rate: float | None) -> dict[str, Any]:
    """
    funding_rate: raw Bybit value, e.g. 0.0001 = 0.01% per 8h
    Returns:
      value: raw funding rate
      annualized_pct: rough annual cost (3×/day × 365)
      signal: 'bullish' | 'bearish' | 'neutral'
        bullish  = funding < -0.01%  (longs being paid)
        bearish  = funding > +0.03%  (longs paying high premium)
        neutral  = otherwise
      carry_cost_bps_8h: abs(funding_rate) × 10000 — cost per 8h in bps
    """
    if funding_rate is None:
        return {"value": None, "annualized_pct": None, "signal": "unknown", "carry_cost_bps_8h": None}

    fr = float(funding_rate)
    annualized = fr * 3 * 365 * 100  # % per year

    if fr < -0.0001:
        signal = "bullish"   # shorts overpaying → price pressure up
    elif fr > 0.0003:
        signal = "bearish"   # longs overpaying → crowded long, fragile
    else:
        signal = "neutral"

    return {
        "value": fr,
        "annualized_pct": round(annualized, 2),
        "signal": signal,
        "carry_cost_bps_8h": round(abs(fr) * 10000, 4),
    }


# ── OI trend ──────────────────────────────────────────────────────────────────

def oi_trend(oi_series: list[dict[str, Any]]) -> dict[str, Any]:
    """
    oi_series: [{ts, oi}] newest-first (from db.get_oi_series)
    Returns:
      oi_now:    latest OI value
      oi_24h_chg_pct: % change vs 24h ago
      oi_4h_chg_pct:  % change vs 4h ago
      trend:    'growing' | 'falling' | 'stable'
      signal:   'bullish' | 'bearish' | 'neutral'
        price up + OI growing  → healthy trend (bullish)
        price down + OI growing → capitulation / shorts piling in (bearish)
        OI falling             → position unwinding (neutral/caution)
    """
    empty = {"oi_now": None, "oi_24h_chg_pct": None, "oi_4h_chg_pct": None,
             "trend": "unknown", "signal": "unknown"}
    if not oi_series or len(oi_series) < 2:
        return empty

    # series is newest-first
    oi_now = float(oi_series[0]["oi"])

    def _pct_chg(old: float) -> float | None:
        if old == 0:
            return None
        return (oi_now - old) / old * 100.0

    oi_4h = float(oi_series[min(3, len(oi_series)-1)]["oi"])   # ~4h back
    oi_24h = float(oi_series[min(23, len(oi_series)-1)]["oi"])  # ~24h back

    chg_4h  = _pct_chg(oi_4h)
    chg_24h = _pct_chg(oi_24h)

    # trend based on 24h change
    if chg_24h is not None:
        if chg_24h > 3.0:
            trend = "growing"
        elif chg_24h < -3.0:
            trend = "falling"
        else:
            trend = "stable"
    else:
        trend = "stable"

    # signal requires price context — provided in recommender
    # here we return raw; recommender combines with price direction
    return {
        "oi_now": oi_now,
        "oi_24h_chg_pct": round(chg_24h, 2) if chg_24h is not None else None,
        "oi_4h_chg_pct":  round(chg_4h, 2)  if chg_4h  is not None else None,
        "trend": trend,
        "signal": "pending",  # set in recommender after combining with price direction
    }


# ── BTC beta / correlation ────────────────────────────────────────────────────

def btc_beta(
    symbol_closes: list[float],
    btc_closes: list[float],
    window: int = 24,
) -> dict[str, Any]:
    """
    Rolling correlation and beta of symbol vs BTC over last `window` 1h candles.
    Returns:
      correlation: Pearson r [-1, 1]
      beta:        price sensitivity (symbol_ret / btc_ret slope)
      is_btc_driven: True if |correlation| > 0.80 — signal mostly reflects BTC
      independent_signal: True if |correlation| < 0.50 — symbol has own driver
    """
    empty = {"correlation": None, "beta": None, "is_btc_driven": False,
             "independent_signal": True, "window": window}

    if len(symbol_closes) < window + 1 or len(btc_closes) < window + 1:
        return empty

    # log returns
    def _rets(closes: list[float]) -> list[float]:
        c = closes[-(window + 1):]
        return [math.log(c[i] / c[i-1]) for i in range(1, len(c)) if c[i-1] > 0]

    sym_r = _rets(symbol_closes)
    btc_r = _rets(btc_closes)
    n = min(len(sym_r), len(btc_r), window)
    if n < 8:
        return empty

    sym_r = sym_r[-n:]
    btc_r = btc_r[-n:]

    mean_s = sum(sym_r) / n
    mean_b = sum(btc_r) / n
    cov = sum((s - mean_s) * (b - mean_b) for s, b in zip(sym_r, btc_r)) / n
    var_s = sum((s - mean_s) ** 2 for s in sym_r) / n
    var_b = sum((b - mean_b) ** 2 for b in btc_r) / n

    std_s = math.sqrt(max(var_s, 1e-12))
    std_b = math.sqrt(max(var_b, 1e-12))

    corr = float(_clamp(cov / (std_s * std_b), -1.0, 1.0))
    beta = float(cov / max(var_b, 1e-12))

    return {
        "correlation": round(corr, 3),
        "beta": round(beta, 3),
        "is_btc_driven": abs(corr) > 0.80,
        "independent_signal": abs(corr) < 0.50,
        "window": n,
    }
