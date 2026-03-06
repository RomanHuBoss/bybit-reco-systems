# Bybit Recommender — Technical Specification

Version: V4.1-docsync (2026-03-06)

This document describes the **current branch as attached**, including implementation caveats that should be known to anyone maintaining or presenting the system.

---

## 1. Architecture overview

```text
collector thread   -> public market data -> SQLite
sentiment thread   -> sentiment series   -> SQLite
reco thread        -> features + score + calibration + publish -> SQLite
FastAPI            -> operator API/UI over the same SQLite database
```

Threads are daemonized and use short-lived SQLite connections. The database runs in WAL mode.

Nominal loop intervals from `settings.py`:
- collector: `20s`
- recommender: `20s`
- sentiment: `60s`

---

## 2. Data collection

### Market data
- `ticker`: last price, spread proxy, turnover
- `ohlcv`: `1m / 15m / 30m / 1h / 4h / 1d`
- `funding_rate` for `linear`
- `open_interest` for `linear`

### Venues
- `spot`
- `linear` (USDT perpetual)

### Staleness
A symbol is skipped if the freshest `1m` candle is older than `STALE_DATA_MAX_SEC`.

---

## 3. Direction engine (`direction.py`)

### Inputs per timeframe
For each timeframe the system derives a soft directional vote from:
- MA slope
- MACD
- RSI
- Bollinger %B

### Aggregation outputs
The multi-timeframe aggregator returns:
- `direction`
- `bias`
- `scores.{tactical,structural,all}`
- `strength.{tactical,structural,all}`
- `coherence`
- `trendiness`
- `regime`
- `regime_confidence`
- `tf_used`

### Important current behavior
- `trendiness` is the unsigned multi-timeframe trend proxy used later by scoring, regime classification and some gates.
- `direction_confidence` is raw confidence in `[0,1]`.
- A separate direction calibrator may add `direction_confidence_calibrated`.

---

## 4. Feature construction (`features.py` + `recommender.py`)

Per symbol / venue the recommender composes a feature dict that includes:
- price
- spread
- turnover24h
- ATR proxies (`1m`, plus slower TFs when available)
- funding signal (`linear` only)
- OI trend (`linear` only)
- BTC beta / correlation
- multi-TF direction aggregate under `_direction_agg`

The recommendation engine prefers `1h ATR` over `1m ATR` for scoring, grid sizing and many risk heuristics.

---

## 5. Regime classification (`regime.py`)

The current branch classifies market regime from the symbol set using:
- average ATR
- average spread
- average multi-TF `trendiness`

### Outputs
- `vol_state`: `low / normal / high`
- `trend_state`: `ranging / mixed / trending`
- `risk_state`: `risk_on / neutral / risk_off`
- `confidence`
- agreement diagnostics (`cv_atr`, `cv_trend`, `n_symbols`)

### Important alignment
This branch uses `direction_agg.trendiness` when available, rather than substituting signed direction strength.

---

## 6. Sentiment system (`sentiment.py` + `sentiment_features.py`)

### Sources
- Fear & Greed
- RSS headlines
- Reddit
- CoinGecko trending
- CoinGecko momentum

### Aggregation
Global sentiment is stored as EWMA statistics on horizons:
- `1h`
- `6h`
- `1d`
- `7d`

The system derives:
- `regime = risk_on / neutral / risk_off`
- `strength`
- `panic`
- `euphoria`

### Per-symbol sentiment
`compute_symbol_sentiment_map()` returns:

```text
{ SYMBOL: (symbol_sentiment, n_points) }
```

The recommender uses adaptive blending:

```text
sym_weight = clip(n_points / 20, 0.1, 0.5)
effective_sent = (1 - sym_weight) * global_sent + sym_weight * symbol_sent
```

If a symbol has no local sentiment points, `effective_sent = global_sent`.

---

## 7. Scoring (`recommender.py`)

### Raw score formulas

#### `spot_grid`
```text
raw = 1.4*range - 1.0*trend - 0.6*clip(atr/0.06, 0, 2)
      + 0.2*clip(sent, -0.5, 0.5) - 0.35*cost_penalty
```

#### `futures_grid`
```text
raw = 1.2*range - 0.9*trend - 0.7*clip(atr/0.06, 0, 2)
      + 0.2*sent - 0.35*cost_penalty
```

#### `dca_bot`
```text
raw = 0.4 + 0.5*clip(0.5 + sent, 0, 1)
      - 0.7*clip(atr/0.12, 0, 2) - 0.35*cost_penalty
```

#### `futures_martingale`
```text
raw = 0.8*range
      - 0.8*clip(atr/0.06, 0, 2)
      + 0.4*clip(sent + 0.2, 0, 1)
      - 0.2*trend
      + 0.3*coherence*dir_strength
      - 0.35*cost_penalty
```

#### `futures_combo`
```text
raw = 0.7*clip(-sent, 0, 1)
      + 0.4*clip(atr/0.06, 0, 2)
      - 0.35*cost_penalty
```

### Shared definitions
- `range = 1 - trendiness`
- `trend = trendiness`
- `cost_penalty = clip((spread_bps + taker_fee_bps) / 50, 0, 1)`
- `score = clip(raw / 1.5, -1, 1)`
- `conf_raw = sigmoid(raw * 2.5)`

### Linear-only score adjustments
After bot-specific scoring, `linear` instruments may get small score shifts from funding:
- bullish funding: `+0.04`
- bearish funding: `-0.06`

---

## 8. Feasibility gates

The engine may block or downgrade a candidate using:
- liquidity gates
- spread gate
- stale-data gate
- trend-too-strong for grids
- martingale-specific direction / panic / ATR blocks
- DCA panic block
- risk-limit gates from `risk.py`
- confidence gate (`MIN_CONF_TO_RECOMMEND`) when enabled

Status flow:
- `recommended`
- `blocked`
- `no_trade`
- `suppressed`

### Persistence gate
`futures_martingale` and `dca_bot` require repeated recommendation before promotion.

Current implementation caveat:
- the in-memory key is `(venue, symbol, bot_type)`
- the window is hard-coded to `120s`
- direction is not part of the persistence key in this branch

---

## 9. Calibration (`calibration.py`)

### 9.1 Model stack

Primary calibration for bot confidence is two-stage:

```text
features -> LogReg -> logit(p) -> Platt -> conf_calibrated
```

If there is not enough data, the branch degrades to:

```text
Platt(score) -> raw sigmoid(score)
```

### 9.2 Canonical feature vector

Current canonical order:

| idx | feature |
|---:|---|
| 0 | `range_score` |
| 1 | `trend_strength` |
| 2 | `atr_pct_norm` |
| 3 | `effective_sentiment` |
| 4 | `dir_conf` |
| 5 | `coherence` |
| 6 | `spread_bps_norm` |
| 7 | `score` |
| 8 | `oi_4h_norm` |
| 9 | `funding_norm` |
| 10 | `liq_tier_num` |
| 11 | `btc_corr` |
| 12 | `regime_conf` |

### 9.3 Recency weighting

Both `fit_platt()` and the LogReg fitting path apply exponential recency weighting.
Default half-life in the helper is `21 days`.

### 9.4 Degenerate-label guard

`fit_platt()` refuses to fit when:
- `n < min_samples`
- win rate `< 5%`
- win rate `> 95%`

In that case it returns `fitted=False`.

### 9.5 Storage keys

| Key | Meaning |
|---|---|
| `logreg_global_v3` | global calibrator |
| `logreg_spot_grid_v3` | per-bot |
| `logreg_futures_grid_v3` | per-bot |
| `logreg_dca_v3` | per-bot |
| `logreg_martingale_v3` | per-bot |
| `logreg_combo_v3` | per-bot |
| `platt_direction_v3` | direction-confidence calibrator |

### 9.6 Refit cadence

Refit is triggered no more than once per hour:

```text
CALIB_REFIT_INTERVAL_SEC = 3600
```

### 9.7 Important caveat in this branch

The persisted `reasons` structure used for training is richer than the temporary `_reasons_for_cal` block built online during inference. Anyone modifying the feature vector should verify train/inference parity explicitly.

---

## 10. Confidence composition in the recommender

After obtaining `conf_raw` and `conf_cal`, the branch applies adaptive blending:

```text
cal_weight = 0.10 + 0.40 * clip(n_samples / 300, 0, 1)
confidence = clip((1 - cal_weight) * conf_raw + cal_weight * conf_cal, 0, 1)
```

Then additional adjustments may apply:
- context-completeness penalty
- OI caution multiplier (`× 0.88` on `linear`)

### Current caveat
The context penalty logic in this branch checks `fr_sig["rate"]`; funding helpers expose a different shape, so this part deserves a cleanup pass.

---

## 11. Recommendation payload

Each recommendation row contains:
- identity: `rec_id`, `ts`, `venue`, `symbol`, `bot_type`
- decision fields: `direction`, `score`, `confidence`, `status`
- Bybit-facing `params`
- rich `reasons`
- feasibility `blocks`
- `ttl_sec`
- `model_version`
- `features_ref_ts`

### `params`
Contains bot-specific controls such as:
- grid range / spacing / levels
- DCA step / max orders
- martingale step / max steps / leverage
- risk-per-trade proxy

### `params.trade_plan`
Human-readable operator guide with:
- volatility snapshot
- regime snapshot
- expected horizon
- ATR-scaled levels
- close / kill-switch notes

### Current caveat
`outcomes.py` reads `params.trade_plan.cost_model`, but the trade-plan builder in the attached branch does not currently populate that block.

---

## 12. Outcome labeling (`outcomes.py`)

### Horizons
| bot_type | horizon |
|---|---:|
| `spot_grid` | 4h |
| `futures_grid` | 4h |
| `dca_bot` | 24h |
| `futures_martingale` | 1h |
| `futures_combo` | 2h |

### Success logic
- **grid bots**: stay inside recommended range; fallback to bounded return if range is missing
- **futures_combo / hedge**: `abs(ret) > atr_relative_threshold`
- **futures_martingale**: TP/SL decision on 1m candles, fallback to cost-adjusted directional return
- **dca_bot**: directional return must exceed `max(1.5 * cost_floor, 0.3%)`
- **other directional paths**: simple directional return sign

---

## 13. Database overview

Primary tables:
- `ohlcv`
- `ticker`
- `funding_rate`
- `open_interest`
- `sentiment`
- `recommendations`
- `reco_outcomes`
- `decision_log`
- `risk_limits`
- `app_config`

SQLite WAL mode is used for concurrent read-heavy workloads.

---

## 14. Known implementation caveats

These are not theoretical concerns; they describe the attached branch as-is.

1. `compute_symbol_sentiment_map()` returns tuples but still advertises `dict[str, float]` in its signature/docstring.
2. The persistence gate state does not include direction in the key.
3. The context-completeness penalty uses a funding-field name that does not match the funding block shape.
4. `outcomes.py` expects `trade_plan.cost_model`, but the current trade-plan builder does not populate it.
5. Feature-space parity between training-time `reasons` and online `_reasons_for_cal` should be treated as a regression-sensitive area.

---

## 15. Positioning / scope

The project is an operator decision-support tool, not an execution bot.
It is intentionally lightweight:
- public data only
- SQLite storage
- no paid feeds required
- no high-end compute requirement

It is suitable for controlled manual operation and iterative model hygiene, but changes to calibration, outcome labels and persistence logic should always be reviewed together because they interact directly with displayed confidence and downstream trust.
