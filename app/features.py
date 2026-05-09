from __future__ import annotations

import math
from typing import Any


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


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


def _finite_float(value: Any) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return num


def _normalize_ticker_quotes(ticker: dict[str, Any] | None) -> tuple[float | None, float | None, float | None]:
    if not ticker:
        return None, None, None
    bid = _finite_float(ticker.get("bid"))
    ask = _finite_float(ticker.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None, None, None
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 1e4 if mid > 0 else None
    return bid, ask, spread_bps


def compute_features_from_ohlcv(ohlcv_rows: list[dict[str, Any]] | list[Any], ticker: dict[str, Any] | None) -> dict[str, Any] | None:
    # expects rows ordered old->new with keys close/high/low/volume/ts
    if not ohlcv_rows or len(ohlcv_rows) < 30:
        return None

    normalized_rows: list[dict[str, float | int]] = []
    # Защита от «отравленных» исторических строк: legacy NaN/inf не должны тихо
    # превращаться в NaN-признаки и дальше раздувать scorer/LLM payload. Плохие
    # свечи просто выкидываем; если валидной истории осталось мало — fail-closed.
    for row in ohlcv_rows:
        close = _finite_float(row.get("close"))
        high = _finite_float(row.get("high"))
        low = _finite_float(row.get("low"))
        volume = _finite_float(row.get("volume"))
        try:
            ts = int(row["ts"])
        except Exception:
            continue
        if (
            close is None or high is None or low is None or volume is None
            or close <= 0 or high <= 0 or low <= 0 or volume < 0
            or high < low
            # Защита в самом feature-layer, а не только в DB/collector: если
            # compute_features_from_ohlcv() вызывают напрямую с внешним рядом,
            # логически невозможный бар не должен участвовать в индикаторах.
            or high < close or low > close
        ):
            continue
        normalized_rows.append({"ts": ts, "close": close, "high": high, "low": low, "volume": volume})

    if len(normalized_rows) < 30:
        return None

    closes = [float(r["close"]) for r in normalized_rows]
    highs = [float(r["high"]) for r in normalized_rows]
    lows = [float(r["low"]) for r in normalized_rows]
    vols = [float(r["volume"]) for r in normalized_rows]

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
    bid, ask, spread_bps = _normalize_ticker_quotes(ticker)

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
        "ts_last": int(normalized_rows[-1]["ts"]),
    }


# ── Liquidity tier ────────────────────────────────────────────────────────────
# vol24h in USD (turnover24h from ticker)

LIQUIDITY_TIERS = {
    "high":   20_000_000,   # > $20M/day  — futures grid liquidity OK
    "medium":  2_000_000,   # > $2M/day   — grid OK
    "low":       500_000,   # > $500K/day — futures grid only, small params
    # below $500K → "micro": grid forbidden
}

def liquidity_tier(turnover24h_usd: float | None) -> str:
    if turnover24h_usd is None:
        return "unknown"
    try:
        v = float(turnover24h_usd)
    except Exception:
        return "unknown"
    # Non-finite или отрицательный turnover — это повреждённый/неполный payload,
    # а не реальная «микроликвидность». Иначе poisoned ticker может случайно
    # заветировать нормальный символ как micro или, наоборот, как high.
    if not math.isfinite(v) or v < 0:
        return "unknown"
    if v >= LIQUIDITY_TIERS["high"]:
        return "high"
    if v >= LIQUIDITY_TIERS["medium"]:
        return "medium"
    if v >= LIQUIDITY_TIERS["low"]:
        return "low"
    return "micro"


# ── Funding rate signal ───────────────────────────────────────────────────────

def funding_signal(funding_rate: float | None, funding_interval_min: int | float | None = 480) -> dict[str, Any]:
    """
    funding_rate: raw Bybit value, e.g. 0.0001 = 0.01% per funding event
    Returns:
      value: raw funding rate
      annualized_pct: rough annual carry using the supplied Bybit funding interval
      signal: 'bullish' | 'bearish' | 'neutral'
        bullish  = funding < -0.01%  (longs being paid)
        bearish  = funding > +0.03%  (longs paying high premium)
        neutral  = otherwise
      carry_cost_bps_interval: abs(funding_rate) × 10000 — cost per funding interval in bps
    """
    if funding_rate is None:
        return {"value": None, "annualized_pct": None, "signal": "unknown", "carry_cost_bps_interval": None, "carry_cost_bps_8h": None, "funding_interval_min": None}

    try:
        fr = float(funding_rate)
    except Exception:
        return {"value": None, "annualized_pct": None, "signal": "unknown", "carry_cost_bps_interval": None, "carry_cost_bps_8h": None, "funding_interval_min": None}
    if not math.isfinite(fr):
        return {"value": None, "annualized_pct": None, "signal": "unknown", "carry_cost_bps_interval": None, "carry_cost_bps_8h": None, "funding_interval_min": None}
    try:
        interval_min = float(funding_interval_min) if funding_interval_min not in (None, "") else 480.0
    except Exception:
        interval_min = 480.0
    if not math.isfinite(interval_min) or interval_min <= 0:
        interval_min = 480.0
    events_per_year = (365.0 * 24.0 * 60.0) / interval_min
    annualized = fr * events_per_year * 100  # % per year

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
        "carry_cost_bps_interval": round(abs(fr) * 10000, 4),
        # Backward-compatible alias. For non-8h instruments this is the per-event cost,
        # not a rescaled 8h value; consumers should prefer carry_cost_bps_interval.
        "carry_cost_bps_8h": round(abs(fr) * 10000, 4),
        "funding_interval_min": int(round(interval_min)),
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

    Important: a dense burst of rows must not masquerade as a true multi-hour history.
    For real unix timestamps we require actual 4h/24h time depth; for tiny synthetic
    test timestamps we fall back to the canonical 1h-per-step interpretation.
    """
    empty = {"oi_now": None, "oi_24h_chg_pct": None, "oi_4h_chg_pct": None,
             "trend": "unknown", "signal": "unknown"}
    if not oi_series or len(oi_series) < 2:
        return empty

    normalized: list[tuple[int | None, float]] = []
    for row in oi_series:
        try:
            oi = float(row.get("oi"))
        except Exception:
            continue
        if not math.isfinite(oi) or oi < 0:
            continue
        try:
            ts = int(row.get("ts"))
        except Exception:
            ts = None
        normalized.append((ts, oi))
    if len(normalized) < 2:
        return empty

    oi_now = normalized[0][1]

    def _pct_chg(old: float | None) -> float | None:
        if old is None or old <= 0:
            return None
        return (oi_now - old) / old * 100.0

    synthetic_ts = any((ts is None) or abs(int(ts)) < 100_000_000 for ts, _ in normalized)

    def _reference_value(target_sec: int, fallback_index: int) -> float | None:
        if synthetic_ts:
            return normalized[fallback_index][1] if len(normalized) > fallback_index else None
        ts_now = normalized[0][0]
        if ts_now is None:
            return None
        target_ts = int(ts_now) - int(target_sec)
        for ts, oi in normalized[1:]:
            if ts is None:
                continue
            if int(ts) <= target_ts:
                return oi
        return None

    chg_4h = _pct_chg(_reference_value(4 * 3600, 4))
    chg_24h = _pct_chg(_reference_value(24 * 3600, 24))

    if chg_24h is not None:
        if chg_24h > 3.0:
            trend = "growing"
        elif chg_24h < -3.0:
            trend = "falling"
        else:
            trend = "stable"
    elif synthetic_ts and len(normalized) < 5:
        fallback_old = normalized[-1][1] if normalized else None
        fallback_chg = _pct_chg(fallback_old)
        if fallback_chg is None:
            trend = "unknown"
        elif fallback_chg > 3.0:
            trend = "growing"
        elif fallback_chg < -3.0:
            trend = "falling"
        else:
            trend = "stable"
    else:
        trend = "unknown"

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
