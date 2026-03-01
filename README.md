# Bybit Trading Bot Recommender (Scenario B)

Operator-facing recommender that outputs **Bybit Trading Bot UI-mapped** recommendations (Spot Grid, Futures Grid, DCA, Futures Martingale, Futures Combo) based on:
- multi-timeframe direction consensus (15m..1d),
- multi-horizon global sentiment (1h..7d),
- volatility/liquidity/cost proxies,
- strict risk gates,
- explainability.

**Scenario B:** you launch bots manually in the Bybit UI.  
This project **does not trade** and does not require Bybit API keys.

---

## Features

- Public Bybit data collector:
  - tickers + OHLCV (1m/15m/30m/1h/4h/1d)
- Direction engine (V2.9):
  - soft indicator aggregation (MA slope / MACD / RSI)
  - tactical vs structural direction
  - coherence + structural veto
  - outputs direction + confidence + regime
- Real global sentiment:
  - Fear & Greed Index (Alternative.me)
  - RSS headlines polarity (CoinDesk/Cointelegraph)
  - multi-horizon EWMA 1h/6h/1d/7d + risk_on/off/neutral
- Bybit-bot taxonomy mapping
- Best-per-(venue,symbol) publishing:
  - best => `recommended`
  - others => `suppressed` (audit)
- Operator UI (Vanilla JS):
  - filters by status
  - details pane (RU)
  - per-item JSON view
  - Risk status + Decision log
- SQLite storage (audit-friendly)

---

## Quickstart

### 1) Requirements
- Python 3.11+

### 2) Install
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
# source .venv/bin/activate

pip install -r requirements.txt
```

### 3) Configure
```bash
cp .env.example .env
```

Edit `.env`:
- `SYMBOLS_SPOT=BTCUSDT,ETHUSDT,...`
- `SYMBOLS_LINEAR=BTCUSDT,ETHUSDT,...`

### 4) Run
```bash
python main.py
```

UI:
- http://127.0.0.1:8000/

Swagger:
- http://127.0.0.1:8000/docs

---

## How to use (operator workflow)

1) Open UI `/`
2) Watch the **latest snapshot** of recommendations:
   - default filter: `recommended`
3) Click **Детали** to read reasons
4) Click **JSON** to copy the full object:
   - `params` contains Bybit UI parameter hints
   - for Grid: `price_range_lower/upper` = Bybit “Ценовой диапазон”

---

## Configuration reference

Important vars:
- `COLLECT_INTERVAL_SEC` — market data pull interval
- `RECO_INTERVAL_SEC` — recommendation refresh interval
- `OUTCOME_HORIZON_SEC` — forward label horizon (default 1800s)
- `CALIB_MIN_SAMPLES` — minimum outcomes for Platt fitting
- `MIN_CONF_TO_RECOMMEND` — main operator filter
- `TAKER_FEE_BPS_SPOT`, `TAKER_FEE_BPS_LINEAR` — cost proxy

---

## What "confidence" means here

There are two calibrated probabilities:
- **recommendation confidence**: calibrated vs forward outcome for the recommendation score
- **direction confidence**: calibrated vs forward outcome for the direction signal

Calibration uses Platt scaling and persists coefficients in SQLite:
- `app_config.platt_bybit_v2`
- `app_config.platt_direction_v2`

---

## Project layout

```
app/
  main.py            # FastAPI app + background loops
  collector.py       # Bybit public data collector
  recommender.py     # taxonomy, scoring, risk gates
  direction.py       # V2.9 direction engine
  sentiment.py       # raw sentiment collectors
  sentiment_features.py  # multi-horizon EWMA + voting
  outcomes.py        # labels for calibration
  db.py              # sqlite helpers + schema init
  ui/static/         # Vanilla JS UI
migrations/
  init.sql
main.py              # root entrypoint
SPEC.md              # detailed specification
```

---

## Development notes

- The system uses only public endpoints. No Bybit keys needed.
- For faster calibration, you can reduce `OUTCOME_HORIZON_SEC` (e.g. 600) and `CALIB_MIN_SAMPLES`.
- The recommendation API returns the latest snapshot by default (`snapshot=latest`) to avoid duplicates.

---

## Roadmap ideas

- Better sentiment sources (Twitter/Reddit/Telegram aggregation) + dispersion-aware confidence
- Symbol-level sentiment (BTC/ETH specific)
- Transaction-cost aware outcome labeling (spread/fees)
- Regime-specific calibration (separate models for trend vs range)
- Position-aware recommendations (requires private Bybit account integration)

---

## Disclaimer

This software provides informational recommendations only. It is not financial advice.



## Performance notes
Background collectors run in separate threads; API requests use short-lived SQLite connections. UI uses debounced requests and cancels in-flight calls.
