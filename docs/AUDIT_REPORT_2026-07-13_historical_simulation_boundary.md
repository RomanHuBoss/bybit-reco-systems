# Audit iteration: historical-only simulation boundary

## 1. Input

- Input ZIP: `bybit-reco-systems-1.0.50-intrabar-replacement-timing.zip`
- Input SHA-256: `4b8fe2c222865bd9b9508b674a8fea5ee1f48d4632d48112d368c3b493d70037`
- Project root: `bybit-reco-systems-main`
- Source version: `1.0.50`
- New version: `1.0.51`
- New regression iteration: `239`

## 2. Project fingerprint

Fingerprint matched Bybit Recommender: FastAPI application, `futures_grid`, Bybit Linear USDT scope, SQLite/PostgreSQL persistence, recommendation/audit boundary, frontend under `app/ui/static/`, canonical trading semantics and operator artifacts were present. The ZIP had one root, no traversal entries and passed `unzip -t`.

## 3. Goal

After this iteration the system must behave as a historical recommendation/outcome simulator:

1. no order submission or runtime fill attestation;
2. no current Bybit instrument-metadata dependency in recommendation publication;
3. no mandatory exchange snapshot in historical outcome labeling;
4. no change from `recommended/no_trade` to `blocked` solely because current instrument metadata is unavailable;
5. every recommendation explicitly states the historical-proxy boundary;
6. conservative OHLCV fill/economics/statistical rules remain unchanged;
7. explicit preflight helpers remain separate optional diagnostics.

## 4. Data-flow map

Before:

`recommender -> current instruments-info prefetch -> exchange_normalizer -> snapped persisted geometry or blocked -> mandatory exchange_execution_snapshot -> outcomes -> calibration`

After:

`recommender -> persisted historical model geometry -> conservative OHLCV outcome -> calibration`

Separate optional path retained:

`operator explicit preflight -> current Bybit filter diagnostics`

The optional path does not mutate recommendation publication, status, outcome eligibility or calibration evidence.

## 5. Baseline environment

- Python: `3.13.5`
- Node: `22.16.0`
- `pip check`: FAILED because MoviePy 2.2.1 requires Pillow `<12`, while the environment contains Pillow 12.2.0.
- Ruff: UNAVAILABLE (`No module named ruff`).
- `compileall`: PASSED.
- JavaScript syntax check: PASSED.

Baseline pytest monolithic execution did not return a final summary within the harness limit and was not counted as successful. An exhaustive non-overlapping batch run covered all collected nodes:

- collected: 1059;
- unique: 1059;
- batches: `177 + 177 + 177 + 176 + 176 + 176`;
- passed: 1059;
- failed/errors/skipped/xfailed/xpassed: 0.

## 6. Confirmed defect

### ITER239-01 — Runtime exchange-executability coupled to historical simulation

- Severity: HIGH
- Type: CONFIRMED DEFECT / architecture and model-data integrity
- Files: `app/main.py`, `app/recommender.py`, `app/outcomes.py`
- Functions: `_reco_thread`, `run_recommender_once`, `compute_outcomes_once`

### Actual behavior

Version 1.0.50 fetched current public Bybit instrument metadata before each recommendation cycle, normalized persisted recommendation geometry to current filters, blocked a recommendation when metadata was unavailable or the plan did not pass current minimums, and refused to label current-model outcomes without `params.exchange_execution_snapshot`.

### Expected behavior

The service only models historical outcomes. Current runtime filters must not be treated as historical truth, an execution attestation or a mandatory model gate. Missing current metadata must not block a historical recommendation or suppress a matured proxy outcome.

### Broken invariants

- recommendation/audit-only boundary;
- temporal correctness: current filters could be applied to an earlier modeled timestamp;
- separation of model evidence from operator execution diagnostics;
- documentation truthfulness.

### Why prior tests did not catch it

Iteration236 explicitly encoded the incorrect expectation that current-filter normalization was mandatory before publication and that outcomes without an exchange snapshot must be skipped. The implementation and tests were internally consistent but contradicted the clarified system role.

## 7. Red -> green evidence

New test: `tests/test_iteration239_historical_simulation_boundary.py`

Red command:

```bash
python -m pytest -q tests/test_iteration239_historical_simulation_boundary.py
```

Red result on pristine 1.0.50:

```text
assert processed == 1
E assert 0 == 1
assert "exchange_normalizer" not in signature.parameters
E assert 'exchange_normalizer' not in ...
KeyError: 'simulation_scope'
3 failed
```

Green command after the fix:

```bash
python -m pytest -q tests/test_iteration239_historical_simulation_boundary.py
```

Green result:

```text
3 passed
```

## 8. Implementation

### Production

- Removed `exchange_normalizer` from `run_recommender_once`.
- Removed recommendation-thread current instrument metadata prefetch and normalization wiring.
- Removed mandatory exchange-snapshot validation from `compute_outcomes_once`.
- Removed the publication normalizer that mutated/blocked recommendations using current Bybit filters.
- Added immutable semantics under `reasons.simulation_scope`:
  - `mode=historical_proxy_only`;
  - `runtime_order_submission=false`;
  - `runtime_execution_validation=not_performed`;
  - `exchange_fill_attestation=not_available`;
  - `fill_model=conservative_ohlcv_proxy`.
- Added `params.simulation_model` diagnostics.
- Preserved explicit `_snap_reco_payload_to_bybit_meta` and Bybit plan validation for separate preflight diagnostics.

### Identity reset

- Application: `1.0.51`
- Model: `bybit-taxonomy-v6-historical-proxy-shadow-roots`
- Outcome: `grid_label_v24`
- Bot calibrator: `logreg_futures_grid_v13`
- Global calibrator: `logreg_global_v13`
- Direction calibrator: `platt_direction_v10`

The identity reset prevents evidence generated under the v1.0.48-v1.0.50 current-filter coupling from being mixed with the corrected historical-only contract.

### Tests

- Added iteration239.
- Reworked iteration236 from mandatory publication normalization to explicit-preflight separation.
- Updated version/identity assertions in affected regression tests.

### Documentation

Updated README, CHANGELOG, ARCHITECTURE, TRADING_LOGIC, KNOWN_RISKS, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, operator DOCX/PDF and root PNG infographic. Historical v1.0.48 behavior is marked superseded rather than silently erased.

## 9. Compatibility

### Database

- No schema or migration change.
- Fresh SQLite initialization: PASSED.
- Reinitialization: PASSED.
- Simulated existing SQLite upgrade `grid_label_v23 -> grid_label_v24`: PASSED.
- Existing proxy outcomes and all `logreg_*` / `platt_direction_*` keys were removed as intended.
- Unrelated sentinel configuration was preserved.
- PostgreSQL offline translation/locking subset: 18 passed.
- Live PostgreSQL integration: SKIPPED; no explicitly disposable DSN was supplied.

### API/config

- Public routes unchanged.
- JSON fields are additive (`simulation_scope`, `simulation_model`).
- `.env` changes: none.
- Explicit execution-preflight endpoints/helpers remain available, but their diagnostics are not part of historical publication/outcome logic.

## 10. Post-check

- collected: 1062;
- unique: 1062;
- exhaustive batches: `177 x 6`;
- passed: 1062;
- failed/errors/skipped/xfailed/xpassed: 0.
- New regression: 3 passed.
- Related identity/outcome/docs subset: 36 passed.
- Documentation/release subset: 17 passed.
- PostgreSQL offline subset: 18 passed.
- `compileall`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- DOCX render: 8 pages, all inspected; no blank/clipped/overlapping pages.
- PDF render: 8 pages.
- PNG infographic: visually inspected; no clipping.

## 11. Security/release boundary

- No private Bybit order create/amend/cancel methods were added.
- No production credentials were used.
- `.env`, local databases, runtime-lock databases, caches and bytecode must be excluded from the release ZIP.

## 12. Residual risks

- OHLCV proxy fills cannot establish queue position, price-level liquidity, partial fills or actual latency.
- Current recommendation geometry may not match later exchange filters; this is now an explicit limitation, not a hidden blocker.
- Optional external execution-evidence ingestion remains in the repository for post-hoc audit, but it is outside the recommendation/outcome runtime contract.
- Positive proxy expectancy is not proof of live profitability.

## 13. Rollback

Code rollback to 1.0.50 requires no database schema rollback, but restores the confirmed current-metadata coupling and can again block/suppress historical evidence based on runtime instrument filters. Calibrator/outcome backups from the old contract must not be reused for trading claims.

## 14. Recommended next work package

Audit historical instrument-constraint provenance as an optional dataset feature: if contemporaneous tick/quantity rules are genuinely stored with historical data, measure sensitivity of proxy results to model discretization without converting those constraints into a runtime publication gate.
