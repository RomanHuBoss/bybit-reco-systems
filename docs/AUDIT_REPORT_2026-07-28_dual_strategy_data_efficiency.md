# Audit report — v1.5.0 dual-strategy data efficiency and horizon-aligned lineage

## 1. Input and release identity

- Input ZIP: `bybit-reco-real.zip.zip`
- Input SHA-256: `f15d86c349dbd43164c9c666264af4c63302a873a6f34846bef443f7f6b8c077`
- Input version: `1.4.13`
- Output version: `1.5.0`
- Project root: one Bybit Recommender root
- Supported strategy families after the iteration: `futures_grid`, `directional_trend`
- Exchange scope: Bybit V5 `category=linear`, USDT perpetual, recommendation/audit service only
- Persistence: SQLite and PostgreSQL

Archive inspection found no traversal paths, absolute entries, duplicate/conflicting paths, symlinks or nested archives. The input archive was not modified; separate pristine, RED and working copies were used.

## 2. User requirement and goal

The user requested a complete improvement iteration after observing that the strategy produced almost no actionable evidence while the database grew rapidly. The user explicitly required that grid must not be removed. Therefore the acceptance statement was:

> After this iteration the system must continue to evaluate and learn both grid and trend, while current recommendation state, outcome roots and market data are persisted in proportion to their decision value rather than at the former refresh frequency.

Acceptance criteria:

1. Both `futures_grid` and `directional_trend` remain canonical and independently keyed.
2. Changes to ranking/TF semantics start a new model lineage; old outcomes are retained but not mixed.
3. Repeated unchanged no-trade cycles do not append 70 immutable recommendation rows.
4. Router shadow competitors do not create overlapping outcome roots each minute.
5. Identical OHLCV rows do not create conflict updates; derived steady state is bounded.
6. Backfill, ticker/funding snapshots and public trade chronology have bounded cadences/scopes.
7. Grid chronology remains available for open grid outcome windows; trend does not consume raw tape.
8. SQLite fresh/upgrade and PostgreSQL dialect contracts remain compatible.

## 3. Baseline

Environment:

- Python `3.13.5`
- Node `22.16.0`
- Input tests collected: `1364`

Baseline commands:

- `python -m compileall -q app tests main.py` — PASSED
- `node --check app/ui/static/app.js` — PASSED
- `python -m pip check` — FAILED due pre-existing shared-environment conflict: MoviePy 2.2.1 requires Pillow `<12`, installed Pillow 12.2.0
- `python -m ruff check .` — UNAVAILABLE (`No module named ruff`)
- monolithic `python -m pytest -q` — TIMED OUT by the harness near 73%; no full baseline-pass claim is made

A pre-existing release inconsistency was also reproduced: `requirements-dev.txt` was referenced by README/tests but absent from the input archive.

## 4. Evidence from the supplied runtime exports

The supplied runtime diagnostics showed approximately:

- 1,061,091 immutable recommendation rows;
- 4,007,363 public trade rows;
- 1,146 retained outcomes;
- 47 current-model outcomes and 4 calibration-eligible outcomes;
- zero actionable recommendations in the current snapshot.

The current-policy export contained 48 trend roots but only 4 calibration-eligible rows. The sample covered 17 timestamps in one temporal cluster, so row count materially overstated independent evidence. This is an absence of proven edge, not proof of live profitability or loss.

## 5. Confirmed defects and gaps

### DSDE-01 — unconditional immutable recommendation refreshes

- Severity: high
- Type: CONFIRMED DEFECT
- Source: `app/recommender.py`, `app/db.py`
- Actual behavior: every 60-second cycle inserted all grid/trend rows into the immutable ledger, including unchanged `no_trade` refreshes.
- Impact: roughly 70 rows/minute for 35 symbols and two strategies, large JSON/index growth, poor audit signal-to-noise.
- Fix: additive `recommendation_latest` current-state table plus material-event/outcome-root immutable ledger.

### DSDE-02 — router shadow competitor root overlap

- Severity: high
- Type: CONFIRMED DEFECT
- Source: `_is_shadow_no_trade_outcome_candidate()` and stored-row counterpart
- Actual behavior: `sample_role=shadow_competitor`, produced by the strategy router, was excluded from open shadow-root reuse logic, even though it represents the same pseudo-position semantics.
- Impact: overlapping labels and repeated roots could inflate apparent sample size and recommendation storage.
- Fix: both `shadow_no_trade` and `shadow_competitor` reuse one open strategy/symbol root until the 12h horizon closes.

### DSDE-03 — PostgreSQL OHLCV no-op write amplification

- Severity: high
- Type: CONFIRMED DEFECT
- Source: `db.upsert_ohlcv()`, `_derive_local_tf_rows()`
- Actual behavior: unconditional conflict updates created row versions even when OHLCV values were unchanged; derived TF repeatedly read 360–500 source rows.
- Impact: WAL, dead tuples, index churn, autovacuum pressure and cross-worker contention.
- Fix: deterministic dedupe, conditional conflict update and two-bucket steady-state recomputation with bounded cold bootstrap.

### DSDE-04 — backfill shared the hot collector cadence

- Severity: medium
- Type: CONFIRMED DEFECT
- Source: `_backfill_thread()`
- Actual behavior: slow/gap backfill repeated every 20 seconds.
- Fix: independent `BACKFILL_INTERVAL_SEC`, default 300 seconds.

### DSDE-05 — unbounded current-state snapshot history

- Severity: medium
- Type: CONFIRMED GAP
- Source: ticker/funding persistence
- Fix: one row per time bucket while preserving the first real event timestamp; settled funding remains independent.

### DSDE-06 — raw public tape collected outside grid evidence need

- Severity: high
- Type: CONFIRMED GAP
- Source: collector and market-trade WebSocket worker
- Actual behavior: public trades were collected across the configured universe even when no open grid label needed chronology.
- Fix: capture scope is derived from waiting `futures_grid` outcome roots only and refreshed every 30 seconds. Directional trend roots never trigger raw-tape collection.

### DSDE-07 — evidence retention prioritized refresh payloads over labels

- Severity: high
- Type: CONFIRMED GAP
- Source: `prune_old_data()`
- Actual behavior: scarce outcomes/observability were deleted while very large refresh history accumulated.
- Fix: short non-root audit lane; ordinary outcomes 90 days; exact/current lineage up to 365 days; executed/ignored records remain immutable.

### DSDE-08 — horizon/score semantics required a new lineage

- Severity: high
- Type: CONFIRMED MODEL CHANGE
- Source: `app/direction.py`, `_score()`
- Actual behavior: 1d had the largest direction vote for a 12h horizon, and several correlated trend terms were weighted as quasi-independent confirmations.
- Fix: horizon-aligned TF weights and reduced correlated double-counting. Explainability weights now match the formula. Because feature aggregation/score meaning changed, v14 is a new lineage.

## 6. Model, outcome and calibration impact

This iteration **is a new trading-model/policy lineage**:

- base model: `bybit-taxonomy-v14-horizon-aligned-dual-strategy`;
- trend model: `...+directional-trend-v7`.

It does not delete old outcomes. Existing v13 and older rows remain historical archive evidence. They are excluded from v14 fit unless an explicit compatible lineage contract exists; old coefficients are not reused.

Unchanged contracts:

- grid outcome label: `grid_label_v26`;
- directional trend label: `directional_trend_label_v2`;
- intrabar observation provenance: `grid_intrabar_observation_v3`;
- grid remains enabled and fully represented in latest state, outcomes, router and execution preflight.

Operational consequence: the v14 calibrators initially have insufficient evidence. `healthy_not_actionable` and shadow/no-trade are expected until the new frozen lineage accumulates sufficient independent temporal cohorts and passes monetary, OOF and terminal-holdout gates.

## 7. Implementation summary

### Recommendation persistence

- Added `recommendation_latest` in SQLite/PostgreSQL init and runtime bootstrap.
- Key: `(venue, symbol, bot_type)` — grid and trend cannot overwrite each other.
- Latest payload updates each cycle; immutable rows are appended for first state, material transition, actionable/pending state or outcome root.
- Latest API uses the table and overlays later audited status/LLM/operator mutations for the same `rec_id`.
- Historical endpoints and operator lifecycle retain immutable recommendation identities.

### Market data

- OHLCV conflict update has a value-difference predicate.
- Collector stats report changed rows rather than input rows.
- Derived TF: 2 recent buckets in steady state; up to 96 target buckets for cold bootstrap.
- Ticker: 60-second bucket; funding forecast: 300-second bucket; original event timestamp is retained.
- Backfill default cadence: 300 seconds.

### Grid trade chronology

- `list_market_trade_capture_symbols()` returns waiting open grid roots only.
- WebSocket sessions refresh capture scope after 30 seconds without bridging sessions.
- REST recent-trade fallback polls only the same grid scope.
- Default raw trade retention: 24 hours.
- Missing/incomplete coverage remains fail-closed and may censor a grid outcome.

### Strategy semantics

MTF weights for the 12h horizon:

- 15m: 1.00
- 30m: 1.25
- 1h: 2.00
- 4h: 2.25
- 1d: 0.75

Both strategy score explanations use the same weights as the actual formula. `ranking_score` is not represented as a fitted probability.

## 8. Actual diff by file group

Production code:

- `app/recommender.py` — v14 dual-strategy ranking lineage, outcome-root reuse and material-event persistence;
- `app/db.py` — latest-state table access, conditional OHLCV writes, snapshot bucketing and evidence-first retention;
- `app/collector.py` — bounded derived recomputation, separate backfill cadence and scoped REST trade fallback;
- `app/trade_stream.py` — on-demand grid chronology scope refresh;
- `app/direction.py` — 12-hour-horizon MTF weights;
- `app/settings.py`, `app/main.py` — new cadences, retention settings, latest-state API and version;
- `app/ui/static/index.html` — release cache identity only; no grid/trend UI contract was removed.

Persistence and configuration:

- `migrations/init.sql`, `migrations/init_postgres.sql` — additive `recommendation_latest`;
- `.env.example` — bounded data-retention/cadence defaults;
- `requirements-dev.txt` — restored reproducible QA dependencies.

Regression and release documentation:

- `tests/test_iteration284_dual_strategy_data_efficiency.py`;
- minimal version/retention expectation updates in existing tests;
- `README.md`, `CHANGELOG.md`, `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- this audit report.

## 9. Database compatibility

Schema change is additive and idempotent:

- new table `recommendation_latest`;
- new indexes on latest status and rec_id;
- no destructive migration;
- existing recommendation/outcome tables are not rewritten during startup;
- first v1.5.0 recommender cycle populates current latest state.

SQLite fresh/upgrade paths and PostgreSQL SQL translation/dialect tests are included in the exhaustive test run. Live PostgreSQL integration was not run because no explicitly disposable test DSN was supplied.

PostgreSQL note: `DELETE` makes space reusable but does not necessarily shrink relation files. The service does not run `VACUUM FULL`. After a backup, an operator may run ordinary `VACUUM (ANALYZE)`; exclusive-lock compaction remains a manual DBA decision.

## 10. Config compatibility

New optional variables and defaults:

```env
BACKFILL_INTERVAL_SEC=300
TICKER_SNAPSHOT_INTERVAL_SEC=60
FUNDING_SNAPSHOT_INTERVAL_SEC=300
MARKET_TRADE_CAPTURE_REFRESH_SEC=30
MARKET_TRADE_RETENTION_HOURS=24
OUTCOME_RETENTION_DAYS=90
CURRENT_LINEAGE_RETENTION_DAYS=365
```

Existing `.env` remains loadable. An explicit old `MARKET_TRADE_RETENTION_HOURS=72` remains authoritative until the operator changes it to 24.

## 11. RED → GREEN

New regression package:

```text
tests/test_iteration284_dual_strategy_data_efficiency.py
```

RED on pristine v1.4.13:

```text
12 failed
```

Failures reproduced the old lineage, absent latest-state persistence, OHLCV no-op writes, large derived source window, missing backfill cadence, unbucketed snapshots, missing grid capture scope, shadow-competitor root overlap and old retention contract.

GREEN on v1.5.0:

```text
12 passed in 0.35s
```

Obsolete tests that asserted v13/v6 identities or the previous label-deleting retention behavior were minimally updated because those expectations contradicted the explicit new lineage/evidence-preservation contract.

## 12. Post-check

- `python -m pytest --collect-only -q`: 1376 collected
- exhaustive deterministic 16-batch union: 1376 passed, 0 failed, 0 skipped
- `python -m compileall -q app tests main.py`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- `python -m pip check`: FAILED only for the pre-existing shared MoviePy/Pillow conflict
- `python -m ruff check .`: UNAVAILABLE in the execution environment; a pinned `requirements-dev.txt` is now shipped

The batch partition covered all 230 `tests/test*.py` files exactly once. This is an exhaustive batched run, not a single monolithic run.

## 13. Security and execution boundary

- No private Bybit create/amend/cancel order endpoint was added.
- The project remains recommendation/audit-only, not OMS/EMS.
- No production credentials or `.env` are included in the release.
- Generated latest state cannot bypass immutable actionable audit identity or execution preflight.
- Grid and trend status/geometry contracts remain separate.

## 14. Unverified items and residual risk

- No live Bybit network test, long-duration WebSocket soak or production PostgreSQL load test was performed.
- Actual disk reduction depends on current data age, explicit user retention settings, PostgreSQL autovacuum and relation reuse.
- Strategy profitability remains unproven. The v14 lineage deliberately starts with no validated calibrator.
- Public trade chronology still cannot prove actual exchange fills.

## 15. Upgrade and rollback

Upgrade:

1. Stop v1.4.13 and verify that the old Python process exited.
2. Back up PostgreSQL/SQLite.
3. Replace project files with v1.5.0.
4. Install dependencies: `python -m pip install -r requirements.txt`; for QA also install `-r requirements-dev.txt`.
5. Keep the existing `.env`; optionally change raw trade retention from 72 to 24 hours.
6. Start the service. No manual SQL is required.
7. Confirm `app_version=1.5.0`, `recommendation_latest_total≈symbols×2`, both bot types present, and new v14 model identity.

Rollback:

- stop v1.5.0 and restore v1.4.13 files;
- the additive `recommendation_latest` table can remain unused;
- do not delete or reset outcomes;
- v14 outcomes remain separate from v13 by model/policy lineage.

## 16. Recommended next work package

Freeze v1.5.0 ranking/policy semantics long enough to collect independent grid and trend cohorts. The next iteration should be an evidence review, not another threshold change: compare bot-specific monetary return, temporal clusters, censoring and score-only/null baselines after sufficient v14 data exists.
