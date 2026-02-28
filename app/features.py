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
