# Audit iteration: directional trend first-touch event model

## 1. Release identity

- Input ZIP: `bybit-reco-systems-1.2.0-strategy-profitability-router.zip`
- Input SHA-256: `d54786ba167e6c6cfd2c46f431d881276a600d4e745eeb4dbb22a9fa1d6abda7`
- Input version: `1.2.0`
- Release version: `1.3.0`
- Version source of truth: `FastAPI(..., version="1.3.0")` in `app/main.py`
- Project root: `bybit-reco-systems-main`
- Iteration regression number: `268`
- Scope date: 2026-07-19

## 2. Project fingerprint

Confirmed before modification:

- Bybit Recommender recommendation/audit service;
- Bybit V5 Linear USDT Perpetual scope;
- `futures_grid` and `directional_trend` strategy families;
- no private order create/amend/cancel implementation;
- SQLite and PostgreSQL-compatible persistence;
- FastAPI in `app/main.py`;
- frontend in `app/ui/static/`;
- canonical directional semantics in `app/trading_semantics.py`;
- grid and trend use separate outcome/calibration contracts.

Archive safety checks found no absolute paths, traversal entries, external symlinks, duplicate/conflicting entries or suspicious nested archives. The input ZIP was never modified.

## 3. Iteration objective

After this iteration, `directional_trend` must not be selected merely because a binary success model or terminal return appears favourable. The system must:

1. preserve the first objectively observable event as `TP_FIRST`, `SL_FIRST` or `HORIZON_EXIT`;
2. censor same-minute TP+SL paths as `AMBIGUOUS` rather than guessing order;
3. estimate the three event probabilities on exact-policy, temporally valid evidence;
4. keep the terminal validation block outside the deployed model fit;
5. calculate plan-specific monetary first-touch expectancy;
6. select trend only when conservative TP-before-SL ordering and the EV lower bound are positive;
7. keep grid and trend models/outcomes separate;
8. remain recommendation/audit-only.

## 4. Acceptance criteria

- `reco_outcomes.event_type` exists after fresh bootstrap and v1.2.0 database upgrade.
- Trend outcome emits `TP_FIRST`, `SL_FIRST`, or `HORIZON_EXIT`; same-candle dual touch is censored as `AMBIGUOUS`.
- Missing exact `label_available_ts` cannot enter the event-model sample.
- Holdout boundaries preserve whole decision timestamps and purge any label unavailable at validation start.
- The terminal holdout is never included in the deployed softmax fit.
- Model activation requires terminal multiclass log-loss better than a train-frequency null baseline.
- Probability uncertainty is finite-sample and is not artificially capped at 20%.
- Conservative EV moves uncertainty removed from TP into the economically worst alternative exit.
- Router rejects trend without a ready first-touch model, supported TP-first ordering and positive EV/lower bound.
- UI, status, operator DOCX/PDF/PNG and iterative PDF prompt expose the new contract.
- Full baseline and post-check suites pass with no removed test coverage.

## 5. Sources read

Relevant implementation and documentation were read before and during the change:

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`;
- latest audit reports for operator/outcome lineage, directional trend and profitability router;
- root `Bybit_Recommender_Iteration_Prompt.pdf`;
- `app/recommender.py`, `outcomes.py`, `calibration.py`, `strategy_router.py`, `trading_semantics.py`;
- `app/db.py`, `db_backend.py`, both reference migration files;
- `app/main.py` and `app/ui/static/app.js`;
- relevant regression, dual-database and release-document tests.

## 6. Baseline environment and commands

- Python: 3.13.5
- Node: 22.16.0

Commands:

```bash
python -m pip check
python -m compileall -q app tests main.py
python -m ruff check .
node --check app/ui/static/app.js
python -m pytest -q
```

Baseline results:

- `compileall`: PASSED
- Node syntax: PASSED
- monolithic pytest: **1237 passed in 46.27s**
- exhaustive 16-batch pytest: **1237/1237 passed**, no duplicate or omitted nodes
- `pip check`: FAILED because the shared environment has MoviePy 2.2.1 requiring Pillow `<12`, while Pillow 12.2.0 is installed
- Ruff: UNAVAILABLE (`No module named ruff`)

## 7. Data-flow map

```text
closed market data / feature snapshot
        ↓
directional_trend recommendation + immutable TP/SL plan
        ↓
continuous 1m outcome path
        ├─ TP first → TP_FIRST
        ├─ SL first → SL_FIRST
        ├─ neither → HORIZON_EXIT
        └─ same-candle/unknown order → AMBIGUOUS, censored
        ↓
reco_outcomes.event_type + net return + MFE/MAE + exit diagnostics
        ↓
exact-policy v2 evidence, exact label_available_ts
        ↓
whole-timestamp terminal holdout + availability purge
        ↓
three-class softmax validation and pre-holdout deployment fit
        ↓
plan-specific TP/SL/timeout payoffs and conservative first-touch EV
        ↓
strategy-profitability-router-v2
        ├─ trend evidence sufficient and EV lower > 0 → comparable candidate
        └─ otherwise → no_trade / shadow evidence
```

## 8. Confirmed defects and gaps

### FT-001 — HIGH — CONFIRMED GAP

- Files: `app/outcomes.py`, `app/calibration.py`, `app/strategy_router.py`
- Prior behaviour: trend outcome had binary `success` and a net return, but the profitability router had no independent probability contract proving that TP was likely to be reached before SL.
- Risk: a profitable timeout or broad binary hit rate could be mistaken for TP-first trend quality; win probability could hide an adverse payoff ratio.
- Expected behaviour: explicit first-touch event classes and plan-specific monetary EV.
- Fix: event-type persistence, three-class model and router requirements.

### FT-002 — HIGH — CONFIRMED DEFECT DURING IMPLEMENTATION

- File/function: `app/trend_events.py`, `fit_trend_event_model()` around lines 196–398.
- Reproducer: new regression showed deployment fit length 120 while purged pre-holdout fit length was 96.
- Actual behaviour before final fix: the untouched terminal block was used again by a final full-data fit.
- Risk: final validation leakage and optimistic probability estimates.
- Fix: deployment fit is restricted to the same purged pre-holdout train; holdout remains untouched.

### FT-003 — HIGH — CONFIRMED DEFECT DURING IMPLEMENTATION

- File/function: `app/trend_events.py`, evidence preparation in `fit_trend_event_model()`.
- Actual behaviour before final fix: rows without exact `label_available_ts` were accepted using an assumed horizon.
- Risk: a label could be treated as known before its actual availability, violating temporal causality.
- Fix: missing, malformed or too-early availability timestamps are excluded; purge compares exact availability against holdout start.

### FT-004 — HIGH — CONFIRMED DEFECT DURING IMPLEMENTATION

- File/function: `app/trend_events.py`, probability uncertainty and `build_trend_event_assessment()` around lines 449–557.
- Actual behaviour before final fix:
  - uncertainty was capped at 20% even for a small holdout;
  - probability mass removed from TP was always assigned to SL, although timeout could have the worse payoff.
- Risk: positive lower EV could be reported even when a valid adverse allocation made it negative.
- Fix:
  - validation calibration gap plus simultaneous finite-sample Hoeffding term, without cosmetic 20% cap;
  - released TP uncertainty is assigned to the economically worst of SL and timeout.

### FT-005 — MEDIUM — CONFIRMED GAP

- Files: `app/db.py`, `migrations/init.sql`, `migrations/init_postgres.sql`.
- Prior behaviour: the exact trend event was not a materialized outcome field.
- Risk: binary labels could not reconstruct event order reliably, and event-specific models could silently mix semantics.
- Fix: additive `event_type TEXT NOT NULL DEFAULT 'LEGACY_BINARY'`, runtime SQLite migration and API/read-model exposure.

### FT-006 — MEDIUM — CONFIRMED DOCUMENTATION GAP

- Files: root iterative PDF prompt and all active project/operator documents.
- Prior behaviour: the iterative prompt still declared `futures_grid` as the only supported bot type and did not require first-touch multiclass validation.
- Risk: a future audit iteration could remove or misclassify the trend contract, or approve a binary model without TP-before-SL evidence.
- Fix: rebuilt 36-page v1.3.0 iterative PDF plus maintainable Markdown source; updated operator DOCX/PDF, infographic and active technical documents.

## 9. Red → green evidence

### Initial feature RED

```bash
python -m pytest -q tests/test_iteration268_trend_first_touch_event_model.py
```

On pristine v1.2.0:

```text
ModuleNotFoundError: No module named 'app.trend_events'
1 error during collection
```

### Adversarial RED

After the first implementation, four additional independent tests failed:

```text
missing label_available_ts: fitted=True instead of False
terminal deployment fit: 120 rows instead of purged 96 rows
probability_error_bound: 0.0833 instead of an uncapped finite-sample bound
EV lower bound: +0.0012 although worst-exit allocation was negative
```

### Final GREEN

```bash
python -m pytest -q tests/test_iteration268_trend_first_touch_event_model.py
```

Result, twice:

```text
15 passed
15 passed
```

The test checks outcome chronology, ambiguous censoring, schema, normalized probabilities, model fit/persistence, exact availability, untouched holdout, uncertainty, worst-exit EV, router gates, UI/status and release documents including the iterative PDF.

## 10. Implementation summary

### Production

- Added `app/trend_events.py`.
- Upgraded trend contracts to `directional_trend_v2`, `directional_trend_label_v2`, `logreg_directional_trend_v2`.
- Added `strategy-profitability-router-v2` first-touch gates.
- Added event-type outcome diagnostics and persistence.
- Added status/UI first-touch readiness and probability fields.
- Preserved binary LogReg only as supplementary evidence; it cannot replace the event model.

### Database

- Added `reco_outcomes.event_type` to SQLite/PostgreSQL reference schemas.
- Added idempotent runtime bootstrap for existing SQLite databases.
- Legacy rows receive `LEGACY_BINARY` and remain excluded from v2 event fitting.

### Tests

- Added `tests/test_iteration268_trend_first_touch_event_model.py` with 15 tests.
- Updated current-version assertions and existing trend/router fixtures minimally for v2 contracts.
- Preserved all previous tests.

### Documentation

Updated:

- `README.md`, `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `KNOWN_RISKS.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md` and `how_to_trade.png`;
- operator DOCX/PDF;
- root `Bybit_Recommender_Iteration_Prompt.pdf`;
- new `docs/Bybit_Recommender_Iteration_Prompt.md` source;
- legacy audit prompt receives an explicit v1.3.0 override pointer.

## 11. Database/schema compatibility

- Fresh SQLite bootstrap: PASSED; `event_type` present.
- Upgrade from a database created by pristine v1.2.0: PASSED; `event_type` added automatically.
- PostgreSQL SQL/dialect/locking offline tests: PASSED in the focused 68-test suite.
- Live PostgreSQL integration: SKIPPED because no explicitly disposable DSN was provided.
- Migration is additive and idempotent; no destructive rewrite is required.

## 12. API and configuration compatibility

- Existing routes and fields are preserved.
- New status/outcome fields are additive.
- `.env` contract is unchanged.
- No private Bybit order endpoint or SDK order-submission call was added.
- `directional_trend` still creates recommendation/audit artifacts only; `exchange_order_submitted=false` remains mandatory.

## 13. Post-check

- `compileall`: PASSED
- Node syntax: PASSED
- monolithic pytest: **1252 passed in 45.27s**
- exhaustive 16-batch pytest: **1252/1252 passed**, union equals collected set
- new regression twice: **15 + 15 passed**
- focused first-touch/router/PostgreSQL/persistence suite: **68 passed**
- fresh and upgraded SQLite checks: PASSED
- private order endpoint scan: no matches in production code
- operator DOCX/PDF render: **16 pages**, visually reviewed
- iterative prompt PDF render: **36 pages**, visually reviewed
- infographic: visually reviewed
- `pip check`: external MoviePy/Pillow conflict remains
- Ruff: unavailable

## 14. What was not verified

- Live PostgreSQL integration without a disposable test DSN.
- Real private Bybit account state, orders, fills or reconciliation.
- Live profitability or production edge.
- Queue priority, market impact and exact intraminute TP/SL order beyond available 1m data.
- External executor behaviour.

## 15. Residual risks

- The probability uncertainty value is a conservative terminal-validation proxy, not a formal transaction-level confidence interval.
- Same-minute TP+SL observations are censored; a high censoring rate may create selection bias and reduce effective evidence.
- `HORIZON_EXIT` payoff currently uses a pre-holdout empirical mean/lower bound, not a separate conditional return regression.
- The 12-hour trend horizon is versioned but not proven optimal.
- Sparse or regime-shifted classes may keep the model correctly unfitted for a long period.
- Proxy costs and funding can understate adverse real execution.

## 16. Rollback

1. Stop the service.
2. Restore the v1.2.0 application archive.
3. Retain the database; v1.2.0 ignores the additive `event_type` column.
4. Restart and verify collector/outcome-worker ownership and health.
5. Do not delete outcome history solely for rollback.

## 17. Recommended next work package

After sufficient v2 exact-policy evidence accumulates, perform a frozen-policy evaluation of:

- multiclass calibration curves per event;
- censoring rate and sensitivity;
- conditional timeout-return modelling;
- paired grid-versus-trend utility by market regime;
- 6h/12h/24h horizon comparison with purged walk-forward and separate lineages.

Do not relax the first-touch gate merely to increase the number of actionable recommendations.

## 18. Commit message

```text
feat(trend-outcomes): require first-touch probability and positive event EV

- persist TP_FIRST, SL_FIRST and HORIZON_EXIT while censoring ambiguous paths
- fit an exact-policy three-class trend model with untouched terminal holdout
- reject labels without exact availability and use conservative uncertainty
- route trend only when TP-first ordering and monetary EV lower bound are positive
- update SQLite/PostgreSQL bootstrap, UI, operator docs and iterative PDF prompt
- validate 1237 baseline and 1252 post-change tests
```
