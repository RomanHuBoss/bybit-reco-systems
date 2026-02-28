# Bybit Recommender System (Spot + USDT Perpetual) — Local MVP (Ready-to-run)

This repository contains a **ready-to-run local project** that:
- Collects public market data from **Bybit v5 public API** (no API keys required for the collector)
- Computes lightweight realtime features (volatility/trend/range, spread/fees proxy, volume anomaly)
- Generates **Top-N ranked recommendations** (rules-based scoring) with:
  - confidence, expected RR (heuristic), risk checks, explainability (reasons)
  - **NO-TRADE** when gates block or scores are too low
- Provides an operator UI (**Vanilla JS/HTML/CSS**) to view recommendations and activate bots (dry-run / production stub)
- Stores everything in **SQLite**: features, recommendations, decisions, bot instances, (optional) orders/trades

This is an MVP designed to be **extendable** into a full production stack.

---

## 1) Quickstart

### 1.1 Requirements
- Python **3.11+**
- Windows/Linux/macOS

### 1.2 Install & run
```bash
cd bybit_reco_system
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt

# Start API + background collector + recommender loop
python -m app.main
```

Open UI:
- http://127.0.0.1:8000/

API docs (Swagger):
- http://127.0.0.1:8000/docs

### 1.3 Configuration
Copy environment example:
```bash
cp .env.example .env
```

Key config (see `.env.example`):
- `DB_PATH` — path to sqlite file (default: `./data/app.db`)
- `COLLECT_INTERVAL_SEC` — collector period (default 20 sec)
- `RECO_INTERVAL_SEC` — recompute recommendations interval (default 20 sec)
- `TOP_N` — how many recommendations to publish
- `VENUES` — `spot,linear`
- `SYMBOLS_LINEAR`, `SYMBOLS_SPOT` — comma-separated symbol list (default small set)
- `RISK_LIMITS_JSON` — JSON string with risk gates (defaults in env example)
- `MASTER_KEY` — used to encrypt API keys (optional; project runs without keys)

---

## 2) Architecture

### 2.1 Runtime loop
On startup, FastAPI launches two background loops:
1) **Collector loop** (every `COLLECT_INTERVAL_SEC`):
   - fetches tickers (bid/ask/last/volume) for configured symbols
   - fetches klines (1m) for those symbols
   - stores in `ohlcv` + `ticker_snap`

2) **Recommender loop** (every `RECO_INTERVAL_SEC`):
   - loads latest data
   - computes features per symbol/venue
   - computes global regime snapshot
   - generates candidate strategy recommendations
   - applies risk gates
   - writes `recommendations` + `decision_log`

### 2.2 Modules
- `app/bybit_client.py` — Bybit public REST (httpx)
- `app/collector.py` — periodically pulls market data into SQLite
- `app/features.py` — feature computation
- `app/regime.py` — simple regime classification
- `app/risk.py` — risk limits + gates (concurrent bots, DD/day, exposure proxy)
- `app/recommender.py` — strategy universe + scoring rules + explainability
- `app/db.py` — SQLite schema init + queries
- `app/main.py` — FastAPI app + API routes + static UI

---

## 3) SQLite Schema (tables)

Created automatically on first run (see `migrations/init.sql`).

Core tables:
- `ohlcv(venue, symbol, tf_sec, ts, open, high, low, close, volume)`
- `ticker_snap(venue, symbol, ts, last, bid, ask, vol24h, turnover24h)`
- `features(venue, symbol, ts, features_json)`
- `market_regime(ts, regime_json)`
- `recommendations(rec_id, ts, venue, symbol, bot_type, direction, account_mode, margin_mode, score, confidence, expected_rr, risk_score, params_json, reasons_json, blocks_json, status, ttl_sec, model_version, features_ref_ts)`
- `decision_log(ts, action, rec_id, operator, details_json)`
- `bot_instances(bot_id, started_ts, stopped_ts, venue, symbol, bot_type, mode_json, params_json, state_json, status, origin_rec_id)`
- `trades(trade_id, bot_id, ts, symbol, pnl, fee, meta_json)` (optional for DD/day tracking)

---

## 4) API (contracts)

### 4.1 Recommendations
- `GET /api/v1/recommendations?venue=spot|linear&top_n=20&min_conf=0.0`
- `GET /api/v1/recommendations/{rec_id}`

### 4.2 Bots
- `POST /api/v1/bots/activate`
  Body:
  ```json
  {"rec_id":"R-...","dry_run":true,"override_params":{...},"operator":"telefunken"}
  ```
- `POST /api/v1/bots/stop`
  Body:
  ```json
  {"bot_id":"B-...","operator":"telefunken"}
  ```
- `GET /api/v1/bots`

### 4.3 Risk
- `GET /api/v1/risk/status`
- `POST /api/v1/risk/limits` — activate a new risk limits JSON (versioned)

### 4.4 Sentiment (MVP manual input)
- `POST /api/v1/sentiment` — write manual sentiment points to DB
- `GET /api/v1/sentiment?scope=global|symbol&key=...`

---

## 5) Extendability checklist
To move from MVP to production:
- add websocket streams (orderbook/trades/liquidations) and compute OFI/imbalances
- add real sentiment pipeline (news/social) into `sentiment` table
- add execution engine with Bybit private endpoints and safe key handling
- add ML calibrator for confidence, track outcomes and labels
- add monitoring: calibration, hitrate@N, drawdown, regime breakdown

---

## 6) Safety / risk notes
This project produces **recommendations only** and provides a stub for execution.
Do not use production execution without:
- robust order handling, retries, idempotency
- position reconciliation, risk kill-switch, and audit logging
- extensive backtesting and paper-trading



## V2 (Scenario B) changes

### V2.1 Bybit-style bot taxonomy
Recommendations are now in Bybit UI categories:
- `spot_grid` -> **Спотовый grid-бот**
- `futures_grid` -> **Фьючерсный grid-бот**
- `dca_bot` -> **DCA-бот**
- `futures_martingale` -> **Фьючерсный Мартингейл**
- `futures_combo` -> **Комбо фьючерсов** (в V2 трактуется как hedge/carry подсказка)

### V2.3 Real sentiment (no keys)
The system collects real global sentiment into `sentiment(scope='global', key='crypto')` using:
- Fear & Greed Index (Alternative.me)
- RSS headlines lexicon sentiment (CoinDesk + Cointelegraph RSS)

### V2.4 Normal confidence
Confidence is calibrated online via **Platt scaling**:
- labels come from **30-minute forward returns** computed from OHLCV (direction-aware)
- stored in `reco_outcomes`
- calibrator is fitted from last ~2000 outcomes (if >=80 samples)


### One best bot per (venue,symbol)
Only the best recommendation per (venue,symbol) is marked as `recommended`. Others are stored as `suppressed`.
