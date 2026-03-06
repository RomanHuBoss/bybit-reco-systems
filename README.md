# Bybit Trading Bot Recommender (Scenario B)

Operator-facing recommender that maps public Bybit market data into **Bybit Trading Bot UI-style** launch suggestions:
- Spot Grid
- Futures Grid
- DCA Bot
- Futures Martingale
- Futures Combo

The system **does not place trades**. It produces scored recommendations, suggested bot parameters, execution notes, and calibrated confidence. Scenario B assumes the operator launches bots manually in the Bybit UI.

---

## What the current code does

- Collects public Bybit data:
  - tickers
  - OHLCV: `1m / 15m / 30m / 1h / 4h / 1d`
  - funding rate + open interest for `linear` instruments
- Builds a multi-timeframe direction signal (`15m..1d`):
  - MA slope
  - MACD
  - RSI
  - BB%B
  - tactical vs structural aggregation
  - coherence / trendiness / regime classification
- Computes global and per-symbol sentiment:
  - Fear & Greed
  - RSS headlines
  - Reddit (selected assets)
  - CoinGecko trending
  - CoinGecko momentum
  - EWMA horizons `1h / 6h / 1d / 7d`
- Scores 5 bot archetypes with hand-crafted formulas and strict feasibility gates
- Applies confidence calibration:
  - per-bot LogReg + Platt when enough outcomes exist
  - global fallback
  - separate Platt calibration for direction confidence (`platt_direction_v3`)
- Stores recommendation rows, reasons, outcomes and operator actions in SQLite
- Serves a small operator UI (Vanilla JS)

---

## Current implementation details that matter

### 1) Confidence stack

Raw confidence is produced from the hand-crafted score:

```text
conf_raw = sigmoid(raw_score × 2.5)
```

Then the engine tries the following calibrators in order:

```text
bot LogReg+Platt -> bot score-only Platt -> global LogReg+Platt -> global score-only Platt -> raw
```

The final confidence is not a fixed `50/50` blend in this branch. It is **adaptive**:

```text
cal_weight = 0.10 + 0.40 × clip(n_samples / 300, 0, 1)
confidence = (1 - cal_weight) × conf_raw + cal_weight × conf_calibrated
```

So the branch trusts raw confidence more during cold start and approaches a 50/50 blend only when the active calibrator has enough data.

### 2) Direction-confidence calibration

Direction confidence is calibrated separately from bot confidence.

- input: raw `direction_confidence` in `[0,1]`
- model key: `platt_direction_v3`
- this avoids mixing strong `short` setups with failed `long` setups, which happened when signed direction score was used as the Platt input

### 3) Extended calibrator feature space

`app/calibration.py` currently defines the following canonical feature vector:

```text
[
  range_score,
  trend_strength,
  atr_pct_norm,
  effective_sentiment,
  dir_conf,
  coherence,
  spread_bps_norm,
  score,
  oi_4h_norm,
  funding_norm,
  liq_tier_num,
  btc_corr,
  regime_conf,
]
```

Saved model keys in the current branch are `*_v3`.

### 4) Outcome labeling

Outcome horizons are bot-specific:

| Bot | Horizon |
|---|---:|
| `spot_grid` | 4h |
| `futures_grid` | 4h |
| `dca_bot` | 24h |
| `futures_martingale` | 1h |
| `futures_combo` | 2h |

Label logic in the current code:
- **grid bots**: success if price stayed inside the recommended range over the horizon
- **futures_martingale**: TP/SL-style outcome using 1m candles when available; fallback to cost-adjusted directional check
- **dca_bot**: success only if directional move exceeds a minimum edge over transaction cost floor
- **futures_combo / hedge**: success if absolute move exceeds an ATR-relative threshold

### 5) Sentiment blend

This branch no longer uses a hard `0.5 / 0.5` global/symbol blend.

If per-symbol sentiment exists, the symbol weight grows with the number of symbol points:

```text
sym_weight = clip(n_points / 20, 0.1, 0.5)
effective_sent = (1 - sym_weight) × global_sent + sym_weight × symbol_sent
```

So symbol sentiment never gets more than 50% weight and never gets less than 10% when present.

### 6) Persistence gate

`futures_martingale` and `dca_bot` pass through an extra persistence gate: the first `recommended` appearance is downgraded to `suppressed`; a repeated appearance in the next cycle is required for promotion.

Current implementation note:
- the state is tracked in-memory inside `recommender.py`
- the window is hard-coded to `120` seconds in this branch

### 7) Trade plan output

Every recommendation gets a human-facing `params.trade_plan` block for the UI details pane. It includes:
- reference price
- volatility snapshot
- regime snapshot
- expected holding horizon
- ATR-scaled control levels
- close / kill-switch conditions

This is an **operator guide**, not an execution engine and not a profit guarantee.

---

## Quickstart

### Requirements
- Python 3.11+

### Install
```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

### Configure
```bash
cp .env.example .env
```

Important defaults in the current code:

```ini
COLLECT_INTERVAL_SEC=20
RECO_INTERVAL_SEC=20
CALIB_MIN_SAMPLES=60
MIN_SCORE_TO_RECOMMEND=0.08
MIN_CONF_TO_RECOMMEND=0.52
REQUIRE_CONF_GATE=1
STALE_DATA_MAX_SEC=300
```

### Run
```bash
python main.py
```

- UI: `http://127.0.0.1:8000/`
- Swagger: `http://127.0.0.1:8000/docs`

---

## Operator workflow

1. Open `/`
2. Inspect the latest snapshot of recommendations
3. Open **Детали** to review:
   - score breakdown
   - regime / direction
   - sentiment
   - confidence model
   - trade plan
   - feasibility blocks
4. Use **Скопировать параметры** to copy bot inputs for the Bybit UI
5. Mark rows as executed / ignored for audit and later outcome analysis

---

## Configuration reference

| Variable | Default | Meaning |
|---|---:|---|
| `DB_PATH` | `./data/app.db` | SQLite database path |
| `COLLECT_INTERVAL_SEC` | `20` | market data pull cycle |
| `RECO_INTERVAL_SEC` | `20` | recommender cycle |
| `SENTIMENT_INTERVAL_SEC` | `60` | sentiment cycle |
| `CALIB_MIN_SAMPLES` | `60` | minimum rows before a calibrator can fit |
| `OUTCOME_HORIZON_SEC` | `900` | generic fallback horizon; bot-specific logic overrides it |
| `MIN_SCORE_TO_RECOMMEND` | `0.08` | score threshold |
| `MIN_CONF_TO_RECOMMEND` | `0.52` | confidence threshold |
| `REQUIRE_CONF_GATE` | `1` | whether low-confidence rows become `no_trade` |
| `STALE_DATA_MAX_SEC` | `300` | skip symbol if latest 1m candle is too old |
| `TAKER_FEE_BPS_SPOT` | `10` | spot fee proxy |
| `TAKER_FEE_BPS_LINEAR` | `6` | linear fee proxy |
| `TOP_N` | `20` | list size for some UI/API views |
| `RISK_LIMITS_JSON` | JSON | active risk guard configuration |

---

## Calibration keys stored in `app_config`

| Key | Scope |
|---|---|
| `logreg_global_v3` | global fallback |
| `logreg_spot_grid_v3` | per-bot |
| `logreg_futures_grid_v3` | per-bot |
| `logreg_dca_v3` | per-bot |
| `logreg_martingale_v3` | per-bot |
| `logreg_combo_v3` | per-bot |
| `platt_direction_v3` | direction-confidence calibrator |

Refit interval in the current code: once per hour (`3600s`).

---

## API surface

| Method | Path | Description |
|---|---|---|
| GET | `/` | operator UI |
| GET | `/api/v1/recommendations` | latest snapshot with filters |
| GET | `/api/v1/recommendations/{rec_id}` | full recommendation detail |
| POST | `/api/v1/recommendations/{rec_id}/action` | mark executed / ignored |
| GET | `/api/v1/status` | calibrator and service status |
| GET | `/api/v1/health/symbols` | symbol freshness health |
| GET | `/api/v1/outcomes/stats` | outcome / win-rate aggregates |
| GET | `/api/v1/decisions` | decision log |
| GET | `/api/v1/risk/status` | active risk state |
| POST | `/api/v1/risk/limits` | update risk limits |
| GET/POST | `/api/v1/sentiment` | sentiment read/write |

---

## Project layout

```text
app/
  main.py
  collector.py
  recommender.py
  calibration.py
  direction.py
  sentiment.py
  sentiment_features.py
  outcomes.py
  features.py
  regime.py
  risk.py
  alerts.py
  db.py
  settings.py
  ui/static/
migrations/
  init.sql
main.py
README.md
SPEC.md
CHANGELOG.md
```

---

## Known caveats in the current branch

These are documented because the code currently behaves this way and the docs should not pretend otherwise.

- `compute_symbol_sentiment_map()` returns `(sentiment, n_points)`, while its annotation/docstring still mentions `dict[str, float]`.
- The context-completeness penalty in `recommender.py` currently checks `fr_sig["rate"]`; funding blocks in this branch expose `value` and `carry_cost_bps_8h`, so the penalty logic deserves a cleanup pass.
- The persistence gate state is tracked by `(venue, symbol, bot_type)` and a hard-coded `120s` window in this branch; direction is not part of the key.
- `outcomes.py` reads `params.trade_plan.cost_model`, while the live trade-plan builder in this branch does not currently attach a `cost_model` block there.
- The online calibrator reconstruction block (`_reasons_for_cal`) is thinner than the full persisted `reasons` structure, so feature-space parity should be treated carefully during future refactors.

---

## Disclaimer

This software provides informational recommendations only. It is not financial advice.
