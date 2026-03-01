# Bybit Trading Bot Recommender (Scenario B) — Specification

## 1. Purpose

This system provides the operator with **explicit, unambiguous recommendations** mapped to **Bybit Trading Bot UI categories**:
- which Bybit bot type to run,
- on which symbol (`*/USDT`) and venue (Spot / USDT Perpetual),
- which direction mode (long / short / neutral / hedge),
- with parameter hints (grid spacing, levels, price range, leverage, DCA step, etc.),
- with confidence, regime context, and explainability.

**Scenario B:** the operator launches bots manually in the Bybit UI.  
The project **does not** place orders, manage positions, or start Bybit bots via API.

---

## 2. What the project does / does not do

### Does
- Collects public Bybit market data:
  - tickers (bid/ask/last/volume)
  - OHLCV klines for: **1m, 15m, 30m, 1h, 4h, 1d**
- Computes features and market regime snapshots.
- Computes **direction** (long/short/neutral) using:
  - multi-timeframe indicator aggregation (15m..1d)
  - soft scoring + coherence + structural veto
  - tactical vs structural direction scores
- Collects **real global sentiment** (no keys):
  - Alternative.me Fear & Greed Index
  - RSS headlines sentiment (lexicon-based)
- Builds **multi-horizon sentiment** (EWMA 1h/6h/1d/7d) and consolidates into:
  - risk_on / neutral / risk_off regime + strength
  - flags: panic/euphoria
- Produces a **ranked** set of candidates, but publishes **only the best recommendation per (venue, symbol)**.
- Provides a **Vanilla JS operator panel** and a JSON view for each recommendation.
- Stores all data into SQLite (audit-ready).

### Does not
- Does not connect to private Bybit account data (balances/positions).
- Does not start/stop Bybit built-in bots automatically.
- Does not execute trades.

---

## 3. Bybit bot taxonomy mapping

Recommendations are in Bybit-style categories:

| Internal bot_type | Bybit UI category | Venue |
|---|---|---|
| `spot_grid` | Spot Grid Bot | spot |
| `futures_grid` | Futures Grid Bot | linear |
| `dca_bot` | DCA Bot | spot/linear |
| `futures_martingale` | Futures Martingale | linear |
| `futures_combo` | Futures Combo | linear |

---

## 4. Data model (SQLite)

Database path: `DB_PATH` (default `./data/app.db`)

Core tables:
- `ohlcv(venue, symbol, tf_sec, ts, open, high, low, close, volume)`
- `ticker_snap(venue, symbol, ts, last, bid, ask, vol24h, turnover24h)`
- `sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)`
- `features(venue, symbol, ts, features_json)`
- `market_regime(ts, regime_json)`
- `recommendations(rec_id, ts, venue, symbol, bot_type, direction, ... , params_json, reasons_json, status, ...)`
- `decision_log(ts, action, rec_id, operator, details_json)`
- `reco_outcomes(...)` (forward-return labels for calibration)
- `app_config(key, value_json, updated_ts)` (stores calibration coefficients)

Status values:
- `recommended` — best per (venue,symbol)
- `suppressed` — other candidates (audit)
- `blocked` — failed risk gates
- `no_trade` — low confidence/score

---

## 5. Market data collection

Collector interval: `COLLECT_INTERVAL_SEC`.

For each configured symbol:
- tickers: `/v5/market/tickers`
- klines: `/v5/market/kline` for intervals: 1, 15, 30, 60, 240, 1440

All market endpoints are public (no keys).

---

## 6. Sentiment pipeline

Collection loop: every 60 seconds.

Inputs:
- Fear & Greed Index (Alternative.me)
- RSS feeds (CoinDesk, Cointelegraph) — lexicon-based polarity

Output:
- writes raw points (`crypto_fng`, `crypto_news_rss`) and combined point `crypto` to `sentiment` table.

Multi-horizon features:
- EWMA sentiment for 1h/6h/1d/7d (half-life = horizon)
- consolidated `risk_on/risk_off/neutral` + strength
- flags: `panic`, `euphoria`

Exposed in `reasons.sentiment_agg`.

---

## 7. Direction engine (V2.9)

Direction is determined from multi-timeframe indicator aggregation across:
- 15m / 30m / 1h / 4h / 1d

Indicators:
- MA slope (SMA20–SMA60 normalized by ATR%)
- MACD histogram (normalized)
- RSI14 (soft centered vote)

Key mechanisms:
- **soft contributions** instead of hard thresholds
- **tactical vs structural** direction scores
- **coherence**: agreement with structural sign
- **structural veto**: 4h/1d can override or neutralize if incoherent
- outputs:
  - `direction`: long/short/neutral
  - `direction_confidence`
  - `regime`: trend/range/transition
  - `regime_confidence`

Exposed in `reasons.direction_agg`.

---

## 8. Recommendation generation

For each (venue, symbol):
1) compute features (from 1m data) + direction aggregation (from 15m..1d)
2) generate Bybit-bot candidates (taxonomy)
3) apply feasibility rules and risk gates
4) score candidates + compute confidence
5) select **best** by:
   1) higher confidence
   2) tie-break by score
6) store all candidates:
   - best as `recommended`
   - the rest as `suppressed` (audit)

---

## 9. Confidence and calibration

Two calibrations exist:

1) **Recommendation confidence**
- Platt scaling on `score` -> success label
- persisted key: `platt_bybit_v2`

2) **Direction confidence**
- Platt scaling on `direction_agg.scores.all` -> success label
- persisted key: `platt_direction_v2`

Outcome labeling:
- horizon: `OUTCOME_HORIZON_SEC` (default 1800)
- success: forward return sign aligned with direction

---

## 10. Grid-specific parameter hints (operator fills Bybit UI)

For grid bots the recommender outputs:
- `grid_spacing_pct` (>= cost-based floor)
- `grid_levels` (derived from span/step; varies across symbols)
- `price_range_lower`, `price_range_upper` (fill Bybit “Ценовой диапазон”)
- `leverage` (futures)

Notes:
- Grid direction defaults to `neutral` in range regime. Bias and strength are also shown.

---

## 11. API

### Recommendations
- `GET /api/v1/recommendations`
  Params:
  - `venue` (optional)
  - `top_n`
  - `min_conf`
  - status toggles: `show_recommended`, `show_blocked`, `show_no_trade`, `show_suppressed`
  - `snapshot=latest` (default) — returns only the latest recommendation snapshot

- `GET /api/v1/recommendations/{rec_id}`

### Risk
- `GET /api/v1/risk/status`
- `POST /api/v1/risk/limits`

### Journal
- `GET /api/v1/decisions?limit=200`

### Sentiment
- `GET /api/v1/sentiment?scope=global&key=crypto`

---

## 12. UI

Static UI at `/`:
- status filters (recommended/blocked/no_trade/suppressed)
- details pane in Russian
- JSON button per recommendation
- Buttons: Риски, Журнал

---

## 13. Configuration (.env)

Key variables:
- `DB_PATH`
- `COLLECT_INTERVAL_SEC`, `RECO_INTERVAL_SEC`
- `SYMBOLS_SPOT`, `SYMBOLS_LINEAR`
- thresholds: `MIN_CONF_TO_RECOMMEND`, `MIN_SCORE_TO_RECOMMEND`
- fees: `TAKER_FEE_BPS_SPOT`, `TAKER_FEE_BPS_LINEAR`
- calibration: `OUTCOME_HORIZON_SEC`, `CALIB_MIN_SAMPLES`

---

## 14. Known limitations
- Sentiment sources are limited and lexicon-based (no LLM/transformer).
- No private account context (position-aware recommendations not available).
- Confidence labels are simplistic (forward-return sign, no transaction costs/slippage).

