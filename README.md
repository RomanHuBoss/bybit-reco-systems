# Bybit Trading Bot Recommender (Scenario B)

Operator-facing recommender that outputs **Bybit Trading Bot UI-mapped** recommendations
(Spot Grid, Futures Grid, DCA, Futures Martingale, Futures Combo) based on:
- multi-timeframe direction consensus (15m..1d),
- multi-horizon global + per-symbol sentiment (1h..7d),
- volatility / liquidity / cost proxies,
- strict risk gates,
- Platt-scaled calibrated confidence.

**Scenario B:** you launch bots manually in the Bybit UI.  
This project **does not trade** and does not require Bybit API keys.

---

## Features

- Public Bybit data collector:
  - tickers + OHLCV (1m / 15m / 30m / 1h / 4h / 1d)
  - funding rate + open interest (linear, throttled to 15 min)
- Direction engine (V2.9):
  - soft indicator aggregation (MA slope / MACD / RSI / BB%B)
  - tactical vs structural scores, coherence, structural veto
- Sentiment (multi-source):
  - Fear & Greed Index (Alternative.me)
  - RSS headlines polarity (CoinDesk / Cointelegraph)
  - Reddit per-symbol (BTC/ETH/SOL/XRP/DOGE)
  - CoinGecko trending + price momentum
  - multi-horizon EWMA 1h/6h/1d/7d, risk_on/off/neutral
  - per-symbol effective sentiment (50/50 blend)
- Bybit-bot taxonomy mapping (5 bot types)
- Per-bot Platt calibration (5 separate scalers + 1 global + 1 direction)
- **Periodic re-calibration every 60 min** — models retrain as new outcomes arrive
- Best-per-(venue, symbol) publishing:
  - best → `recommended`, others → `suppressed` (full audit trail)
- Operator UI (Vanilla JS, Russian):
  - status filters (recommended / blocked / no_trade / suppressed)
  - Details pane with inline **Обновить** button
  - **Скопировать параметры** (Bybit UI params → clipboard)
  - Risk status / Decision log / Health / Outcomes modals
  - In-place ✓/✗ row update (no table flicker)
- SQLite storage (append-only, audit-friendly)
- Telegram alerts (optional)

---

## Quickstart

### 1) Requirements
- Python 3.11+

### 2) Install
```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

### 3) Configure
```bash
cp .env.example .env
```

Key settings in `.env`:
```ini
SYMBOLS_SPOT=BTCUSDT,ETHUSDT,...
SYMBOLS_LINEAR=BTCUSDT,ETHUSDT,...
CALIB_MIN_SAMPLES=80        # lower for faster initial calibration
STALE_DATA_MAX_SEC=300
MIN_CONF_TO_RECOMMEND=0.52
```

### 4) Run
```bash
python main.py
```

- UI:      http://127.0.0.1:8000/
- Swagger: http://127.0.0.1:8000/docs

---

## Operator workflow

1. Open UI `/`
2. Watch the **latest snapshot** of recommendations (default filter: `recommended`)
3. Click **Детали** — read scoring breakdown, sentiment, direction signals, risk gates
4. Click **Обновить** in the Details pane to refresh without reloading the table
5. Click **Скопировать параметры** — copies `params` JSON for direct paste into Bybit UI:
   - Grid: `price_range_lower/upper` = Bybit "Ценовой диапазон"
   - `grid_spacing_pct` = шаг сетки, `grid_levels` = кол-во уровней
6. Click **✓** (executed) or **✗** (ignored) — row updates in-place, logged to `decision_log`

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `COLLECT_INTERVAL_SEC` | `20` | Market data pull interval |
| `RECO_INTERVAL_SEC` | `30` | Recommendation refresh interval |
| `OUTCOME_HORIZON_SEC` | `1800` | Forward label horizon (overridden per bot type) |
| `CALIB_MIN_SAMPLES` | `80` | Minimum outcomes for Platt fitting |
| `MIN_CONF_TO_RECOMMEND` | `0.52` | Confidence gate for `recommended` status |
| `MIN_SCORE_TO_RECOMMEND` | `0.08` | Score gate |
| `REQUIRE_CONF_GATE` | `1` | Enable/disable confidence gate |
| `STALE_DATA_MAX_SEC` | `300` | Max data age before a symbol is skipped |
| `TAKER_FEE_BPS_SPOT` | `10` | Spot taker fee proxy (bps) |
| `TAKER_FEE_BPS_LINEAR` | `6` | Linear taker fee proxy (bps) |
| `TELEGRAM_BOT_TOKEN` | `` | Optional; empty = alerts disabled |
| `TELEGRAM_CHAT_ID` | `` | Telegram chat ID for alerts |

---

## Calibration

Two-stage **LogReg + Platt** calibration, re-fit automatically every 60 minutes.
Feature weights are learned from actual outcomes — no hand-tuned score weights.

```
P(success) = Platt( LogReg([range_score, trend, atr_pct, sentiment,
                              dir_conf, coherence, spread_bps, score]) )
```

Platt is fitted on **log-odds** of LogReg output (temperature scaling), not raw probabilities.

### Degradation by outcome count

| Count | Mode |
|---|---|
| < 80 | Raw sigmoid only |
| 80–299 | Platt on score (legacy) |
| ≥ 300 | Full LogReg + Platt |

### Storage keys in `app_config`

| Key | Scope |
|---|---|
| `logreg_global_v1` | Global LogReg+Platt fallback |
| `logreg_spot_grid_v1` | Per-bot: Spot Grid |
| `logreg_futures_grid_v1` | Per-bot: Futures Grid |
| `logreg_dca_v1` | Per-bot: DCA Bot |
| `logreg_martingale_v1` | Per-bot: Futures Martingale |
| `logreg_combo_v1` | Per-bot: Futures Combo |
| `platt_direction_v2` | Direction confidence (Platt-only) |

Priority at inference: per-bot LogReg+Platt → per-bot Platt-only → global → raw sigmoid.  
Final `confidence = 0.5 × conf_raw + 0.5 × conf_calibrated` (avoids cold-start overconfidence).

The UI shows **Увер ⚠** with a progress bar until `CALIB_MIN_SAMPLES` outcomes are
collected, then switches to **Увер ✓**.

---

## Outcome horizons (per bot type)

| Bot | Horizon |
|---|---|
| spot_grid / futures_grid | 4h (grids live for hours) |
| dca_bot | 24h |
| futures_martingale | 1h |
| futures_combo | 2h |

Grid success criterion: price stayed **inside** the recommended range for the full horizon
(not directional movement). Directional bots: `ret > 0` in the direction.

---

## Project layout

```
app/
  main.py               # FastAPI app + background threads
  collector.py          # Bybit public data collector
  recommender.py        # taxonomy, scoring, calibration, risk gates
  calibration.py        # PlattScaler — fit / save / load / overflow-safe predict
  direction.py          # V2.9 direction engine (15m..1d)
  sentiment.py          # raw sentiment collectors (F&G, RSS, Reddit, CoinGecko)
  sentiment_features.py # multi-horizon EWMA + per-symbol blending
  outcomes.py           # forward labels for calibration
  features.py           # OHLCV feature extraction, BTC beta
  regime.py             # market regime classification
  risk.py               # risk gates + position limits
  alerts.py             # Telegram alerting
  db.py                 # SQLite helpers + schema init
  settings.py           # env-based settings
  ui/static/
    index.html          # single-page operator UI
    app.js              # Vanilla JS — all UI logic
    styles.css          # dark theme styles
migrations/
  init.sql              # full schema DDL
main.py                 # root entrypoint (uvicorn)
SPEC.md                 # detailed specification
CHANGELOG.md            # version history
```

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Operator UI |
| GET | `/api/v1/recommendations` | Latest snapshot with filters |
| GET | `/api/v1/recommendations/{rec_id}` | Full detail for one rec |
| POST | `/api/v1/recommendations/{rec_id}/action` | Mark executed/ignored |
| GET | `/api/v1/status` | Calibrator state, outcome count, sentiment, errors |
| GET | `/api/v1/health/symbols` | Per-symbol data freshness |
| GET | `/api/v1/outcomes/stats` | Win-rate breakdown by bot / symbol |
| GET | `/api/v1/decisions` | Decision log (last 200) |
| GET | `/api/v1/risk/status` | Active risk limits and position counts |
| POST | `/api/v1/risk/limits` | Update risk limits |
| GET/POST | `/api/v1/sentiment` | Sentiment series read/write |

---

## Development notes

- Only public Bybit endpoints are used. No API keys required.
- For faster initial calibration: set `OUTCOME_HORIZON_SEC=600` and `CALIB_MIN_SAMPLES=30`.
- The `snapshot=latest` parameter in `/api/v1/recommendations` pins the response to the most
  recent recommender cycle timestamp — prevents mixing stale and fresh rows.
- Calibrators stored in `app_config` will be re-fit on first startup after upgrade
  (their `ts` field will be missing → treated as age=∞ → immediate refit).

---

## Roadmap

- Better sentiment sources (Twitter/Telegram aggregation, dispersion-aware confidence)
- Regime-specific calibration (separate models for trend vs range)
- UI symbol disable toggle (without `.env` edit + restart)
- Position-aware recommendations (requires private Bybit account integration)

---

## Disclaimer

This software provides informational recommendations only. It is not financial advice.
