# Audit report: cross-symbol temporal independence

**Date:** 2026-07-13  
**Input ZIP:** `bybit-reco-systems-1.0.44-terminal-execution-finalization.zip`  
**Input SHA-256:** `101ca6b6ad24375b07311fb62cb0965c4b5f9f0620b113dad8b9596929a44df7`  
**Source version:** `1.0.44`  
**New version:** `1.0.45`

## 1. Iteration objective

After this iteration, cross-sectional outcomes that use overlapping future market intervals must not manufacture independent monetary evidence. A large symbol universe in one market block must remain shadow `no_trade` until enough non-overlapping temporal evidence exists and its one-sided monetary lower bound is positive.

## 2. Acceptance criteria

1. Eighty symbols sharing one 12-hour outcome interval count as one temporal cluster, not 80 independent experiments.
2. Directly or transitively overlapping `[ts, label_available_ts]` intervals remain one cluster even when they cross an arbitrary wall-clock bucket boundary.
3. Default `CALIB_MIN_SAMPLES=80` requires at least 20 effective temporal clusters.
4. Both row-level and temporal-cluster one-sided 95% lower bounds must be positive before monetary evidence becomes `positive`.
5. Temporal diagnostics survive calibrator persistence and malformed numeric values remain fail-closed.
6. v8 calibrators cannot bypass the new contract; bot/global identities advance to v9.
7. SQLite fresh/re-init and v1.0.44 upgrade preserve data; PostgreSQL dialect/locking checks remain green.
8. The clean release ZIP contains one project root and excludes runtime databases, locks, caches and secrets.

## 3. Project fingerprint

Fingerprint matched Bybit Recommender:

- recommendation/audit service, not OMS/EMS;
- `futures_grid`, Bybit `category=linear`, USDT perpetual;
- FastAPI application in `app/main.py`;
- canonical directional semantics in `app/trading_semantics.py`;
- SQLite and PostgreSQL compatibility paths;
- frontend in `app/ui/static/`;
- required docs, operator artifacts and migration SQL present.

The input archive was structurally safe: 273 entries, one project root, no absolute paths, traversal, symlink escape or duplicate paths. It did, however, contain `data/app.runtime_locks.sqlite`; this release-hygiene defect is removed from the output archive.

## 4. Sources reviewed

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- recent audit reports;
- `app/calibration.py`, `recommender.py`, `outcomes.py`, `db.py`, `db_backend.py`, `main.py`, `settings.py`;
- outcome, calibration, temporal-validation, PostgreSQL and release regression tests.

## 5. Baseline environment and inventory

- Python `3.13.5`;
- Node `v22.16.0`;
- production Python files: 23;
- test files: 176 before this iteration;
- docs files: 56 before the new report;
- frontend files: 3;
- migration SQL files: 2;
- DB backends: SQLite and PostgreSQL/psycopg translation layer;
- maximum previous regression iteration: 232.

## 6. Baseline commands and results

- `python -m pip check` - **FAILED** due to a pre-existing environment conflict: MoviePy 2.2.1 requires Pillow `<12`, installed Pillow is 12.2.0.
- `python -m compileall -q app tests main.py` - **PASSED**.
- `python -m ruff check .` - **UNAVAILABLE** (`No module named ruff`).
- `node --check app/ui/static/app.js` - **PASSED**.
- `python -m pytest --collect-only -q` - 1037 unique nodes.
- Monolithic pytest did not emit a final summary within the harness limit and was not counted as successful.
- Exhaustive deterministic non-overlapping batches covered the full collected set: **1037/1037 passed**.

## 7. Confirmed defect

### ID: CAL-TEMP-233

- **Severity:** HIGH
- **Type:** CONFIRMED DEFECT / model-validation and financial fail-open
- **Primary file:** `app/calibration.py`
- **Function:** `fit_logreg()` monetary evidence gate
- **Affected path:** matured proxy outcomes -> weighted monetary diagnostics -> fitted bot/global calibrator -> recommendation actionability

### Reproducer

Input cohort:

- 80 different symbols;
- all use the same 12-hour `[ts, label_available_ts]` interval;
- 40 returns of `+3%` and 40 returns of `-1%`;
- observed mean `+1%`;
- row-level effective sample size `80`;
- row-level one-sided 95% lower bound `+0.629879%`.

### Actual v1.0.44 behavior

`fit_logreg()` treated every symbol row as independent:

- `return_samples=80`;
- `expectancy_status=positive`;
- `fitted=true`.

The model therefore interpreted one common market path as 80 independent experiments. A broad but correlated crypto universe could satisfy the monetary proof threshold after a single regime/horizon.

### Expected behavior

All outcomes whose future observation intervals overlap directly or transitively must contribute one temporal experiment. The reproducer has `temporal_cluster_count=1`, so evidence is `insufficient`, calibration remains unfitted and the recommendation remains shadow `no_trade`.

### Violated invariant and impact

The violated invariant is temporal independence: a statistical sample may only use information available in non-overlapping validation windows as independent degrees of freedom. The defect overstated effective sample size, narrowed uncertainty artificially, accelerated model readiness and could make an unproven strategy actionable. It did not prove that the strategy was profitable; it made the evidence for profitability invalid.

### Why existing tests missed it

v1.0.41 deduplicated repeated shadow roots only within the same symbol/publication chain. Existing calibration tests varied rows and timestamps but did not construct a same-horizon, many-symbol cohort. Row count, class balance and row-level lower bounds were all internally correct, so the suite remained green while the independence assumption was wrong.

## 8. Red -> green evidence

### Red

Command on pristine v1.0.44 with only the new test added:

```bash
python -m pytest -q tests/test_iteration233_cross_symbol_temporal_dependence.py
```

Essential output:

```text
AttributeError: 'LogRegScaler' object has no attribute 'temporal_cluster_count'
3 failed in 0.28s
```

### Green

Final targeted command:

```bash
python -m pytest -q tests/test_iteration233_cross_symbol_temporal_dependence.py
```

Result:

```text
4 passed
```

The fourth regression proves that overlapping horizons straddling a clock-bucket boundary remain one component.

## 9. Implemented correction

`app/calibration.py` now:

1. validates each matured interval using exact integer `ts < label_available_ts`;
2. sorts intervals and merges direct and transitive overlaps into connected components;
3. computes one recency-weighted mean return per component;
4. assigns one recency weight per component, so symbol count cannot increase degrees of freedom;
5. calculates temporal cluster count, Kish effective cluster count, cluster return dispersion and one-sided 95% lower bound;
6. requires both row-level and temporal-cluster evidence to pass;
7. persists all new diagnostics in the existing calibrator JSON.

The default temporal floor is:

```text
minimum_temporal_clusters = min(20, ceil(CALIB_MIN_SAMPLES / 4))
```

For the supported minimum `CALIB_MIN_SAMPLES=80`, this is 20 clusters.

`app/recommender.py` adds operator diagnostics:

- `time_clusters=current/minimum`;
- `row_lower_bound`;
- `time_cluster_lower_bound`.

Bot/global calibration identities are now:

- `logreg_futures_grid_v9`;
- `logreg_global_v9`.

Old v8 coefficients therefore cannot silently authorize the new evidence contract.

## 10. Changed files

### Production

- `app/calibration.py`
- `app/recommender.py`
- `app/main.py`

### Tests

- new `tests/test_iteration233_cross_symbol_temporal_dependence.py`;
- narrowly corrected temporal fixtures and version/key assertions in related regression tests.

### Documentation and operator artifacts

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX, PDF and `how_to_trade.png`;
- this audit report.

### Database/migrations/frontend

No schema, migration or frontend-code change.

## 11. Database and compatibility

- Fresh SQLite initialization - **PASSED**.
- Repeated SQLite initialization with sentinel preservation - **PASSED**.
- Upgrade of a DB initialized by v1.0.44 through v1.0.45 - **PASSED**, sentinel preserved.
- PostgreSQL offline support, locking, savepoint and dialect suite - **24 passed**.
- Live PostgreSQL integration - **SKIPPED** because no explicitly disposable DSN was provided.

New fields live inside existing `app_config.value_json`; no migration or operator DB action is required.

## 12. API and configuration compatibility

- FastAPI version changed from `1.0.44` to `1.0.45`.
- No route or required request-field removal.
- No new environment variable.
- `.env.example` unchanged.
- `OUTCOME_LABEL_VERSION=grid_label_v18` unchanged because outcome target mathematics did not change.
- Model/calibrator cache identity changed intentionally to prevent incompatible v8 reuse.

## 13. Security and execution boundary

Static checks confirm no Bybit private order create/amend/cancel endpoint or equivalent SDK execution method was added. The project remains recommendation/audit-only. No real credentials, `.env`, production DB or runtime lock DB belong in the release.

## 14. Post-check results

- `python -m pytest --collect-only -q` - 1041 unique nodes.
- Exhaustive deterministic non-overlapping batches: `209 + 208 + 208 + 208 + 208 = 1041`; **1041/1041 passed**.
- iteration233 - **4 passed**.
- PostgreSQL offline subset - **24 passed**.
- `python -m compileall -q app tests main.py` - **PASSED**.
- `node --check app/ui/static/app.js` - **PASSED**.
- SQLite fresh/re-init/upgrade - **PASSED**.
- DOCX rendered to 7 pages and every page visually inspected.
- PDF rendered to 7 pages and every page visually inspected.
- Updated standalone infographic visually inspected.

## 15. Unconfirmed claims

This iteration does not prove that the strategy is intrinsically profitable or intrinsically unprofitable. The release archive contains no representative production execution database from which exact live expectancy can be estimated. The confirmed finding is narrower and material: prior calibrated evidence could overstate independence and therefore could not substantiate the claimed edge.

## 16. Residual risks

1. Non-overlapping 12-hour clusters can still share a persistent multi-day market regime; overlap correction is necessary but not sufficient for full independence.
2. A normal one-sided bound on cluster means is not a substitute for block bootstrap, purged walk-forward or regime-stratified validation.
3. Proxy `ret` remains an OHLCV/grid-ledger counterfactual, not exchange-attested fills.
4. Cross-symbol concentration, BTC beta and factor exposure still require explicit portfolio-level analysis.
5. Exact execution validation remains dependent on a complete external reconciliation adapter.
6. With 14-day retention and a 12-hour horizon, only about 28 non-overlapping temporal windows are available at once; evidence accumulation is therefore intentionally slow and may frequently remain `no_trade`.

## 17. Operational effect

After upgrade, bot/global v8 calibrators are not reused. Until the current retained cohort contains enough non-overlapping temporal clusters and both lower bounds are positive, the expected safe state is:

```text
expectancy_status = insufficient or uncertain
fitted = false
status = no_trade
sample_role = shadow_no_trade
```

This is not a runtime failure. It means the strategy has not yet demonstrated statistically defensible monetary evidence.

## 18. User actions

- Replace the application with v1.0.45 and restart it.
- No DB migration or `.env` edit is required.
- Do not override `PROXY_MONETARY_EXPECTANCY_UNPROVEN`.
- Treat old calibrated confidence/readiness as incompatible with the new contract.
- Keep the system in paper/shadow mode until exact execution walk-forward validation is available.

## 19. Rollback

Code rollback to v1.0.44 requires no schema rollback, but it restores the confirmed cross-symbol pseudoreplication defect and is not recommended. Existing audit records remain intact under either version.

## 20. Recommended next work package

Build a dependence-aware monetary walk-forward layer over temporal cluster returns and finalized exact executions:

- purged/embargoed blocks;
- stationary or moving-block bootstrap lower confidence bound;
- symbol/factor concentration and BTC-beta decomposition;
- exact fees, settled funding, slippage and capital-at-risk;
- comparison against `no_trade`, simple grid and passive baselines;
- drawdown and expected shortfall by market regime.

Only this work can distinguish a genuinely absent edge from a strategy whose previous validation was merely defective.
