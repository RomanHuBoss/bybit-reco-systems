# Bybit Recommender — Technical Specification

Version: V3.8 (2026-03)

---

## 1. Architecture overview

```
┌──────────────────────────────────────────────────────┐
│  Background threads (daemon)                         │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │
│  │ collector    │  │ sentiment    │  │ reco      │  │
│  │ thread       │  │ thread       │  │ thread    │  │
│  │ (20s cycle)  │  │ (60s cycle)  │  │ (30s cyc) │  │
│  └──────┬───────┘  └──────┬───────┘  └─────┬─────┘  │
│         │                 │                │         │
│         └─────────────────┴────────────────┘         │
│                           │                          │
│                     SQLite (WAL)                      │
│                           │                          │
│  ┌────────────────────────┴─────────────────────┐    │
│  │  FastAPI (sync endpoints, short-lived conns) │    │
│  └────────────────────────┬─────────────────────┘    │
│                           │                          │
│                    Operator UI (JS)                   │
└──────────────────────────────────────────────────────┘
```

### Threading model
- All three threads are daemon threads started at FastAPI `startup`.
- Each thread acquires its own short-lived SQLite connection (`closing(_get_conn())`).
- SQLite is opened in WAL mode — concurrent reads/writes are safe.
- FastAPI handlers also use short-lived connections — no shared state.

---

## 2. Data collection (collector.py)

### Symbols
Configured via `SYMBOLS_SPOT` and `SYMBOLS_LINEAR` in `.env`.  
Auto-disable: if Bybit returns `symbol invalid` / `Not supported symbols` the symbol is
added to `_DISABLED_SYMBOLS` (in-process set) and skipped for the rest of the session.

### OHLCV
- Timeframes collected: `1m, 15m, 30m, 1h, 4h, 1d`
- Bybit v5 kline intervals: `1, 15, 30, 60, 240, D` (`"1440"` is invalid — use `"D"`)
- Stored in `ohlcv(venue, symbol, tf_sec, ts, open, high, low, close, volume)`

### Futures metadata (throttled to 15 min)
- Funding rate: `/v5/market/tickers` → `funding_rate(symbol, ts, funding_rate, next_funding_ts)`
- Open interest: `/v5/market/open-interest` (1h intervals, 48 candles) → `open_interest(symbol, ts, oi)`

---

## 3. Direction engine (direction.py) — V2.9

### Indicators (per timeframe)
| Indicator | Signal range | Weight |
|---|---|---|
| MA slope (EMA20 vs EMA50 normalized) | [-1, 1] | 0.35 |
| MACD histogram (normalized × 900) | [-1, 1] | 0.30 |
| RSI (normalized: (RSI-50)/30) | [-1, 1] | 0.27 |
| BB %B (position in Bollinger Band) | [-1, 1] | 0.08 |

### Aggregation
- **Tactical** score: 15m + 30m + 1h (short-term momentum)
- **Structural** score: 4h + 1d (trend backdrop)
- **All** score: weighted combination
- **Coherence**: fraction of TFs with the same sign as the aggregate
- **Structural veto**: if structural score disagrees strongly with tactical (>0.4 gap),
  direction confidence is capped

### Output
```python
{
  "direction": "long" | "short" | "neutral",
  "bias": "long" | "short" | "neutral",   # weaker signal
  "direction_confidence": float,           # raw [0, 1]
  "direction_confidence_calibrated": float, # Platt-scaled
  "scores": {"tactical": float, "structural": float, "all": float},
  "strength": {"tactical": float, "structural": float, "all": float},
  "coherence": float,
  "regime": "trend" | "range" | "unknown",
  "structural_veto_applied": bool,
  "tf_used": [int, ...]
}
```

### BTC beta adjustment
If `|r(symbol, BTC)| > 0.80` (is_btc_driven): `dir_conf × 0.88`  
Purpose: high BTC correlation means the symbol's direction signal reflects BTC, not its own momentum.

---

## 4. Scoring (recommender.py)

### Score formula per bot type

**spot_grid / futures_grid**:
```
raw = 1.4×range - 1.0×trend - 0.6×clamp(atr/0.015, 0,2) + 0.2×sent - 0.35×cost_penalty
```

**dca_bot**:
```
raw = 0.4 + 0.5×clamp(0.5+sent, 0,1) - 0.7×clamp(atr/0.02, 0,2) - 0.35×cost_penalty
```

**futures_martingale**:
```
raw = 0.8×range - 0.8×clamp(atr/0.018,0,2) + 0.4×clamp(sent+0.2,0,1)
      - 0.2×trend + 0.3×coherence×dir_strength - 0.35×cost_penalty
```

**futures_combo**:
```
raw = 0.3 + 0.7×clamp(-sent,0,1) + 0.4×clamp(atr/0.02,0,2) - 0.35×cost_penalty
```

Where:
- `range = max(0, 1 - trend)` — derived from multi-TF trend strength
- `trend` = `|strength.all|` from direction aggregation
- `cost_penalty = clamp(cost_bps/50, 0, 1)`
- `score = clamp(raw / 1.5, -1, 1)`

### Funding rate adjustments (linear only)
- Signal `bullish` (longs being paid): `score += 0.04`
- Signal `bearish` (crowded long):     `score -= 0.06`

### Raw confidence
```
conf0 = clamp(sigmoid(raw × 2.5), 0, 1)
```

### Calibrated confidence
```
conf_cal = bot_calibrator.predict(score)   # if fitted
         | global_calibrator.predict(score) # fallback
         | conf0                            # uncalibrated
conf = clamp(0.5 × conf0 + 0.5 × conf_cal, 0, 1)
```
OI unwinding signal: `conf × 0.88`

---

## 5. Feasibility gates

| Code | Condition |
|---|---|
| `LIQUIDITY_TOO_LOW` | turnover24h < $500K |
| `LIQUIDITY_LOW_FUTURES` | turnover24h < $2M + bot ∈ {martingale, combo} |
| `FUNDING_EXTREME` | funding_rate > 0.06%/8h |
| `SPREAD_TOO_WIDE` | spread_bps > 14 for grid bots |
| `TREND_TOO_STRONG` | multi_tf_trend_strength > 0.60 for grid bots |
| `MARTINGALE_BLOCKED` | atr > 0.018 OR panic OR risk_off strong OR sent < -0.45 |
| `DIR_CONF_TOO_LOW` | dir_conf < 0.65 (martingale only) |
| `DCA_BLOCKED_PANIC` | sent < -0.70 OR panic flag |
| `MAX_CONCURRENT_BOTS` | total running bots ≥ limit |
| `MAX_DD_DAY` | daily drawdown ≥ limit |
| `COOLDOWN_ACTIVE` | in cooldown period after drawdown |
| `MAX_SYMBOL_BOTS` | too many bots on same symbol |

---

## 6. Grid price range calculation

```
span_target_pct  = clamp(atr_1h × 100 × 25, 1, 12)   # target range width
grid_spacing_pct = clamp(max(atr×100×0.6, min_step), 0.08, 2.5)
levels           = clamp(round(span/spacing) + 1, 6, 60)
span_actual_pct  = spacing × (levels - 1)
half             = span_actual_pct / 2

# Directional skew (CORRECTED in V3.8):
long:    lower_pct = half × 0.80,  upper_pct = half × 1.20  # range extends upward
short:   lower_pct = half × 1.20,  upper_pct = half × 0.80  # range extends downward
neutral: lower_pct = upper_pct = half

price_range_lower = price × (1 - lower_pct/100)
price_range_upper = price × (1 + upper_pct/100)
```

ATR used: 1h ATR (`atr_pct_1h`) preferred over 1m ATR for more stable grid width.

---

## 7. Calibration (calibration.py)

### Algorithm: Platt Scaling (logistic regression on scores)

```
P(success | score) = sigmoid(a × score + b)
```

Gradient descent, 300 iterations, lr=0.06, overflow-clamped `z ∈ [-500, +500]`.

### Persistence & freshness
- Stored in `app_config` table as JSON `{a, b, fitted, ts}`.
- `ts` = unix timestamp of last fit.
- Re-fit condition: `time.now() - ts >= CALIB_REFIT_INTERVAL_SEC (3600s)`.
- Re-fit uses last 4000–6000 outcome rows (JOIN query, not N+1).
- If re-fit fails (insufficient data): stale model is retained as fallback.

### Training data
- Source: `reco_outcomes` JOIN `recommendations`
- Features (x): stored `score` (post-adjustment, matching inference distribution)
- Labels (y): `success` flag (bot-type-specific criterion, see Outcome Labeling)

---

## 8. Outcome labeling (outcomes.py)

### Horizons per bot type
| Bot type | Horizon |
|---|---|
| spot_grid, futures_grid | 4h = 14400s |
| dca_bot | 24h = 86400s |
| futures_martingale | 1h = 3600s |
| futures_combo | 2h = 7200s |

### Success criterion
- **Grid bots**: price stayed within `[price_range_lower × 0.995, price_range_upper × 1.005]`
  for the full horizon (checked against 1m candle min/max).
  Fallback (no range stored): `|ret| < 1.5%` over horizon.
- **Directional bots**: `ret > 0` in the direction of the recommendation.

---

## 9. Sentiment (sentiment.py + sentiment_features.py)

### Sources
| Source | Scope | Update |
|---|---|---|
| Fear & Greed Index | global | hourly |
| RSS CoinDesk/Cointelegraph | global + per-symbol | per sentiment cycle |
| Reddit (BTC/ETH/SOL/XRP/DOGE) | per-symbol | per sentiment cycle |
| CoinGecko trending | per-symbol | per sentiment cycle |
| CoinGecko price momentum | per-symbol | per sentiment cycle |

### Per-symbol blend weights
`coingecko_momentum`: 0.45, `reddit`: 0.30, `news_rss`: 0.15, `coingecko_trending`: 0.10

### Effective sentiment
```
effective_sent = 0.5 × global_6h_ewma + 0.5 × symbol_sent  # if symbol data exists
               = global_6h_ewma                              # otherwise
```

### EWMA horizons
1h (λ≈0.9), 6h (λ≈0.98), 1d (λ≈0.995), 7d (λ≈0.999)

### Regime classification
`risk_on` (ewma_6h ≥ 0.15), `risk_off` (≤ -0.15), `neutral` otherwise  
Flags: `panic` (strength ≥ 0.5 + risk_off), `euphoria` (strength ≥ 0.5 + risk_on)

---

## 10. Database schema (SQLite, WAL mode)

Key tables:
- `ohlcv(venue, symbol, tf_sec, ts, open, high, low, close, volume)` — PK(venue,symbol,tf_sec,ts)
- `ticker(venue, symbol, ts, price, spread_bps, turnover24h)` — PK(venue,symbol)
- `recommendations(rec_id, ts, venue, symbol, bot_type, direction, score, confidence, ...)`
- `reco_outcomes(outcome_id, rec_id, ts, success, ret, horizon_sec, ...)`
- `market_regime(ts, regime_json)`
- `features(venue, symbol, ts, features_json)`
- `funding_rate(symbol, ts, funding_rate, next_funding_ts)` — PK(symbol,ts)
- `open_interest(symbol, ts, oi)` — PK(symbol,ts)
- `sentiment_data(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)`
- `decision_log(id, ts, action, rec_id, operator, details_json)`
- `bot_instances(bot_id, venue, symbol, bot_type, status, started_ts, stopped_ts)`
- `risk_limits(id, ts, version, limits_json, is_active)`
- `app_config(key, value_json, updated_ts)` — stores Platt scalers

---

## 11. Known limitations

- Bybit public API rate limits: collector sleeps `COLLECT_INTERVAL_SEC` between cycles.
  Aggressive interval (< 10s) risks HTTP 429.
- SQLite single-writer: heavy concurrent writes should be avoided; the daemon threads
  are sequential per cycle so this is not an issue in practice.
- Calibration quality degrades until ~80 outcomes are collected (≈1–2 days at normal volume).
  During this period confidence values are raw sigmoids and should be treated as rough estimates.
- Reddit API (`/r/{coin}/hot.json`) is rate-limited and may return 429 occasionally.
  Failures are logged as `SENTIMENT_ERROR` and the cycle continues.
