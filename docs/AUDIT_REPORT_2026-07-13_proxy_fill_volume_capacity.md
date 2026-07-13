# Audit iteration 237 — aggregate candle-volume capacity for proxy fills

## 1. Iteration identity

- **Input ZIP:** `bybit-reco-systems-1.0.48-exchange-normalized-proxy-execution.zip`
- **Input SHA-256:** `88d9c6c22a3d1cc28834a451024fa9c4b8e3d95338aebdda8394afc229cacf00`
- **Source version:** `1.0.48`
- **New version:** `1.0.49`
- **Source outcome contract:** `grid_label_v21`
- **New outcome contract:** `grid_label_v22`
- **Regression number:** iteration 237
- **Scope:** proxy fill quantity versus aggregate one-minute candle volume; complete calibrator cleanup when the outcome contract changes.

## 2. Project fingerprint

The archive contains the expected Bybit Recommender project root and required files: FastAPI application in `app/main.py`, futures-grid recommendation logic, canonical trading semantics, SQLite/PostgreSQL persistence, public Bybit client, static frontend, migrations, tests and operator artifacts. The service remains recommendation/audit-only and no private order create/amend/cancel endpoint was added.

## 3. Goal and acceptance criteria

After this iteration the system must not create a proxy fill that is physically larger than all quantity traded in the corresponding one-minute candle. This is demonstrated when:

1. one order larger than candle volume makes the outcome unavailable;
2. several fills cannot cumulatively consume more than candle volume;
3. initial directional inventory must fit the entry candle volume;
4. sufficient volume preserves the existing confirmed-cycle PnL contract;
5. changing the outcome contract removes all historical `logreg_*` and `platt_direction_*` calibrators;
6. the complete collected test set passes without weakening fail-closed behavior.

## 4. Baseline environment

- Python: `3.13.5`
- Node: `v22.16.0`
- `python -m compileall -q app tests main.py`: **PASSED**
- `node --check app/ui/static/app.js`: **PASSED**
- `python -m pip check`: **FAILED, pre-existing environment issue** — MoviePy 2.2.1 requires Pillow `<12`, while Pillow 12.2.0 is installed.
- `python -m ruff check .`: **UNAVAILABLE** — Ruff is not installed in the execution environment.
- `pytest --collect-only -q`: **1051 unique nodes**.
- Monolithic pytest: **TIMED OUT without final summary**, therefore not counted.
- Exhaustive non-overlapping baseline batches: **1051/1051 passed**.

## 5. Confirmed defect ITER237-01

- **Type:** CONFIRMED DEFECT
- **Severity:** HIGH
- **Files:** `app/outcomes.py`, principally `_iter_1m_candles()`, `_is_valid_outcome_candle()` and `_grid_outcome()`
- **Invariant violated:** a proxy execution cannot consume more base quantity than the entire observed candle traded; unknown or impossible execution evidence must fail closed.

### Actual behavior

The prior strict trade-through contract proved only that price moved beyond a resting limit. Each crossed order was then treated as fully filled regardless of order quantity and candle volume.

Independent reproducers on the unmodified source showed:

1. `qty_per_order=10`, candle volume `1`: a complete profitable cycle returned `(1, 0.005)`;
2. two one-unit fills in a candle with total volume `1.5`: the ledger returned a completed negative outcome instead of unavailable evidence;
3. an initial LONG inventory of `10` in a candle with volume `5`: the outcome returned `(0, 0.0)` instead of rejecting the impossible entry;
4. a valid high-volume cycle had no volume-confirmation diagnostics.

### Expected behavior

Aggregate candle volume is a necessary capacity bound. A full proxy fill is unavailable when its quantity, or cumulative simulated quantity in that minute, exceeds total observed volume. This does not claim price-level liquidity or queue execution; it only prevents a mathematically impossible full fill.

### Financial and model impact

The defect could manufacture completed cycles and positive returns from orders larger than the whole market quantity observed during that minute. Those labels feed win rate, monetary expectancy, confidence calibration and publication gates. The direction of bias is optimistic because impossible fills are treated as realised rather than unknown.

### Why existing tests missed it

Earlier tests concentrated on price-path ambiguity, strict trade-through, fees, funding and grid topology. Their candles generally used large placeholder volumes, and no test compared persisted order quantity with aggregate candle volume.

## 6. Confirmed defect ITER237-02

- **Type:** CONFIRMED DEFECT
- **Severity:** MEDIUM
- **File:** `app/main.py::_bootstrap_db()`
- **Invariant violated:** an outcome-label change must invalidate every calibrator trained on the previous target.

The reset path deleted only a hard-coded set of current and legacy keys. Older `logreg_*` or `platt_direction_*` records could remain in `app_config`. They were not loaded by the current identities, but could later be revived by rollback or compatibility code and contradicted the documented full reset.

The new reset deletes both complete key families with parameterised `LIKE` predicates. A simulated `grid_label_v21` to `grid_label_v22` upgrade removed all old v1/v4/v7/v10 calibrator keys while preserving an unrelated configuration probe.

## 7. Red → green evidence

### Red command

```bash
cd red
python -m pytest -q tests/test_iteration237_proxy_fill_volume_capacity.py
```

### Material red result

```text
assert (1, 0.005) is None
assert (0, -0.0029999999999999714) is None
assert (0, 0.0) is None
KeyError: 'fill_volume_confirmation'
assert 'logreg_%' in bootstrap
5 failed in 0.55s
```

### Green command

```bash
cd working
python -m pytest -q tests/test_iteration237_proxy_fill_volume_capacity.py
```

### Green result

```text
5 passed in 0.37s
```

## 8. Implementation

### Production

`app/outcomes.py` now:

- reads and validates `ohlcv.volume` with each one-minute candle;
- resolves the current persisted per-order quantity from the exchange-normalised recommendation;
- maintains a per-candle aggregate quantity budget;
- charges initial LONG/SHORT inventory against the first tradeable candle;
- charges every simulated grid fill against the current candle;
- returns unavailable evidence with explicit diagnostics when capacity is exceeded;
- records `fill_volume_confirmation=aggregate_candle_volume_cap_v1` when quantity evidence is available.

`app/main.py` now removes every `logreg_%` and `platt_direction_%` key whenever the label contract changes.

### Version and identity

- FastAPI: `1.0.49`
- Outcome label: `grid_label_v22`
- Bot calibrator: `logreg_futures_grid_v11`
- Global calibrator: `logreg_global_v11`
- Direction calibrator: `platt_direction_v8`
- Model identity: unchanged

## 9. Changed files

### Production

- `app/outcomes.py`
- `app/main.py`
- `app/calibration.py`
- `app/recommender.py`

### Tests

- added `tests/test_iteration237_proxy_fill_volume_capacity.py`;
- minimally updated iteration 234 reset-contract assertion for family-based cleanup;
- synchronised version/outcome/calibrator identity assertions.

### Documentation and operator artifacts

- `README.md`
- `CHANGELOG.md`
- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- operator DOCX/PDF
- `how_to_trade.png`
- this audit report

### Database/migrations/frontend

No relational schema, migration SQL, public API route, frontend source or environment-variable contract changed.

## 10. Database compatibility

- Fresh SQLite initialisation: **PASSED**; second initialisation is idempotent.
- Existing SQLite simulation from `grid_label_v21`: **PASSED**.
- Outcome rows were removed as required by the incompatible target change.
- All historical calibrator key families were removed.
- Unrelated `app_config` data was preserved.
- PostgreSQL translation/locking regression subset: **PASSED, 18 tests**.
- Live PostgreSQL integration: **SKIPPED** because no explicitly disposable DSN was provided.

No manual database action is required. Startup performs the outcome/calibrator reset.

## 11. Post-check

- Final collection: **1056 unique test nodes**.
- Monolithic pytest: **TIMED OUT without final summary; not counted**.
- Six non-overlapping batches of 176 nodes: **1056/1056 passed**.
- Targeted production/PostgreSQL/reset subset: **27 passed**.
- Python compileall: **PASSED**.
- JavaScript syntax: **PASSED**.
- DOCX rendered to eight page images and visually inspected: **PASSED**.
- PDF/operator PNG visual check: **PASSED**.
- Private Bybit order endpoint static search: **PASSED**.
- Release archive integrity and re-extracted tests: recorded after packaging below.

## 12. API, configuration and security boundaries

- No new route or payload field is required from the operator.
- No `.env` change is required.
- No private Bybit credentials or order methods were introduced.
- Volume comes from the already persisted public one-minute OHLCV stream.
- Malformed, boolean, negative or non-finite volume cannot become valid execution capacity.

## 13. Residual risks

Aggregate candle volume is only a necessary upper bound. It does not prove:

- volume traded at the exact order level;
- queue priority;
- partial-fill sequence;
- maker/taker status;
- market impact;
- whether simultaneous strategies competed for the same candle volume;
- terminal horizon liquidation capacity.

The current fallback preserves legacy/manual outcomes that lack a persisted order quantity; current exchange-normalised recommendations do persist quantity and receive the hard capacity gate. Legacy evidence must not be represented as exchange-attested execution.

## 14. Rollback

Rollback to v1.0.48 requires no schema downgrade, but restores the confirmed defect and should not be used for model validation. If rollback is unavoidable, keep recommendations in paper/shadow mode and discard proxy labels whose simulated quantity was not checked against candle volume.

## 15. Recommended next work package

Add a conservative participation/partial-fill model and terminal-liquidation capacity check, then measure proxy fills against terminally reconciled exchange fills. Aggregate volume should be reduced by a configured participation cap and shared across simultaneous recommendations before any remaining proxy result is used for monetary calibration.

## 16. Release verification

- Output archive: `bybit-reco-systems-1.0.49-proxy-fill-volume-capacity.zip`
- `unzip -t`: **PASSED**.
- Exactly one root directory: **PASSED** (`bybit-reco-systems-main`).
- Project fingerprint after clean re-extraction: **PASSED**.
- Forbidden release artifacts (`.env`, local DB/SQLite files, runtime locks, bytecode and pytest caches): **ABSENT**.
- Re-extracted compileall: **PASSED**.
- Re-extracted JavaScript syntax check: **PASSED**.
- Re-extracted iteration 234/237 tests: **9 passed**.
- Version/outcome/calibrator identity consistency: **PASSED**.
- Final SHA-256 is supplied with the delivered archive because embedding it inside the archive would change the archive checksum.
