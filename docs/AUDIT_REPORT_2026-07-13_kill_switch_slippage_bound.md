# Audit iteration 240 — conservative kill-switch loss bound

## 1. Iteration title

**Remove the perfect-fill assumption at an intrabar kill-switch breach in the historical OHLCV proxy.**

## 2. Input ZIP

`bybit-reco-systems-1.0.51-historical-simulation-boundary.zip`

## 3. Input ZIP SHA-256

`9a9e31b2f87992f46bf38869100391de82a695480f40b02e13860638eadb889d`

## 4. Source version

`1.0.51`, read from the FastAPI `version=` argument in `app/main.py`.

Source identities:

- outcome contract: `grid_label_v24`;
- bot calibrator: `logreg_futures_grid_v13`;
- global calibrator: `logreg_global_v13`;
- direction calibrator: `platt_direction_v10`;
- model identity: `bybit-taxonomy-v6-historical-proxy-shadow-roots`.

## 5. New version

`1.0.52` — patch release.

New identities:

- outcome contract: `grid_label_v25`;
- bot calibrator: `logreg_futures_grid_v14`;
- global calibrator: `logreg_global_v14`;
- direction calibrator: `platt_direction_v11`;
- model identity unchanged.

## 6. Project fingerprint

The archive has one root, `bybit-reco-systems-main`, and matches the Bybit Recommender fingerprint:

- required root files, application modules, tests, documentation and migration SQL are present;
- the FastAPI application is created in `app/main.py`;
- supported bot type remains `futures_grid`;
- supported market scope remains Bybit Linear USDT perpetual;
- the project remains recommendation/audit-only, not OMS/EMS;
- SQLite and PostgreSQL support remain present;
- frontend remains in `app/ui/static/`;
- no private create/amend/cancel order route was added.

ZIP safety inspection found no absolute paths, `..` traversal, external symlinks or conflicting duplicate entries.

## 7. Iteration objective

After this iteration, a historical proxy outcome must not price residual inventory at the ideal kill-switch boundary when the same OHLCV candle proves that price continued farther in the adverse direction. The service must remain historical-only and must not claim an exact stop fill or runtime execution truth.

## 8. Acceptance criteria

1. Upper kill-switch breach with residual short inventory uses the observed candle high as the conservative liquidation bound when it is above the trigger.
2. Lower kill-switch breach with residual long inventory uses the observed candle low when it is below the trigger.
3. Favorable continuation beyond the trigger gives no price improvement; the configured boundary remains the conservative price.
4. Gap-through-stop paths remain outcome-unavailable rather than reconstructed.
5. Diagnostics expose trigger boundary, observed extreme and chosen liquidation price.
6. Old proxy outcomes/calibrators are isolated under a new outcome identity.
7. DB schema, public routes, environment variables and the historical-only system boundary remain unchanged.
8. The new test is red on pristine 1.0.51 and green after the production fix.
9. Every collected test node passes after the change.

## 9. Sources read

The audit used the current ZIP as runtime truth and reviewed the relevant portions of:

- `README.md`, `CHANGELOG.md`, requirements and `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- recent audit reports;
- `app/outcomes.py`, `app/grid_math.py`, `app/trading_semantics.py`, `app/recommender.py`, `app/calibration.py`, `app/main.py`, `app/db.py`, `app/db_backend.py`, `app/risk.py`, `app/settings.py`;
- outcome, stop, funding, volume, replacement-timing and persistence regressions.

No production credentials, private Bybit API or real order flow were used.

## 10. Affected data-flow map

`historical 1m OHLCV` → `_grid_outcome()` → intrabar path reconstruction → kill-switch trigger → residual inventory liquidation → gross proxy PnL → conservative funding/cost layer → `reco_outcomes.ret` → monetary calibration → confidence/readiness.

The defect was at the residual-inventory liquidation step.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `v22.16.0`;
- project root: `bybit-reco-systems-main`;
- baseline tests: `1062`;
- next regression number: `240`;
- DB backends: SQLite and PostgreSQL compatibility layer;
- no package manifest requiring npm/yarn commands.

`pip check` reports an environment conflict: MoviePy 2.2.1 requires Pillow `<12`, while Pillow 12.2.0 is installed. Ruff is unavailable (`No module named ruff`). Neither condition was introduced by this patch.

## 12. Baseline commands and exact results

Commands:

```text
python --version
node --version
python -m pip check
python -m compileall -q app tests main.py
python -m ruff check .
node --check app/ui/static/app.js
python -m pytest --collect-only -q
python -m pytest -q
```

Results:

- Python and Node version checks: PASSED.
- `pip check`: FAILED only for the external MoviePy/Pillow conflict.
- compileall: PASSED.
- Ruff: UNAVAILABLE.
- JavaScript syntax: PASSED.
- collection: `1062 tests collected`.
- baseline full suite: `1062 passed in 26.93s`.

## 13. Confirmed defect

### KS-240 — ideal boundary fill understates intrabar kill-switch tail loss

- Severity: **high**.
- Type: **CONFIRMED DEFECT**.
- File: `app/outcomes.py`.
- Function: `_grid_outcome()`, kill-switch branch in intrabar segment processing.
- Fixed ranges: approximately lines 940–941, 1077–1078, 1170–1187, 1200–1236, 1264–1266 and 1458–1469.

Minimal upper-breach reproducer:

```text
Neutral arithmetic grid: 99 / 100 / 101
Upper kill-switch: 102
One remaining short slot after Sell 101
Candle: O=100, H=102.5, L=100, C=102.5
Fees/funding: zero
Committed capital: 200
```

Pristine behavior:

```text
liquidation price = 102.0
proxy return = -1 / 200 = -0.005
```

The candle proves trading to 102.5 after the 102 trigger. For the remaining short, exact execution at 102 is an optimistic assumption. A conservative observable loss bound is:

```text
liquidation bound = 102.5
proxy return = -1.5 / 200 = -0.0075
```

The symmetric lower-breach case applies to residual long inventory: a candle low below the lower trigger must be used as the adverse bound.

Violated invariants:

- unknown stop execution must not be converted into an ideal fill;
- historical proxy outcomes must be conservative where OHLCV proves a worse adverse excursion;
- tail loss must not be systematically understated before monetary calibration;
- the historical-only service must not imply exchange-attested stop execution.

Financial/model impact:

- understated loss on stop events;
- inflated mean `ret` and win/loss economics;
- less negative expected shortfall;
- potentially narrower confidence bounds and earlier positive calibration;
- understated capital drawdown in precisely the range-break events most important to grid risk.

Why existing tests missed it:

Existing stop tests asserted the configured kill boundary as the liquidation price. The tests validated deterministic topology but implicitly encoded perfect stop execution, so they agreed with the defective production assumption.

Regression test:

`tests/test_iteration240_kill_switch_slippage_bound.py`

Red command:

```text
python -m pytest -q tests/test_iteration240_kill_switch_slippage_bound.py
```

Red result on pristine production code plus only the new test:

```text
upper/lower return obtained: -0.005
expected adverse observed bound: -0.0075
version remained 1.0.51 / grid_label_v24
3 failed
```

Green result:

```text
3 passed
```

Residual risk:

The observed candle extreme is a conservative historical bound, not the exact market-stop execution price. OHLCV cannot establish queue position, liquidity at the stop, gap slippage or exchange acknowledgement timing. Gaps that skip the trigger remain outcome-unavailable.

## 14. Unconfirmed claims

- The release archive does not prove that the underlying strategy is inherently loss-making in all markets.
- It also does not prove positive live edge because it contains no representative exchange-attested execution database.
- This iteration did not attempt to estimate real stop-market slippage beyond the observable OHLCV adverse bound.
- Live PostgreSQL integration was not run because no explicitly disposable DSN was supplied.

## 15. Fix plan

1. Preserve processing of resting grid orders only up to the configured kill boundary.
2. Record both the trigger boundary and the observed adverse candle extreme.
3. For residual short inventory on an upper breach, choose `max(boundary, observed high)`.
4. For residual long inventory on a lower breach, choose `min(boundary, observed low)`.
5. Do not credit favorable continuation beyond the boundary.
6. Include the new state in path snapshots/restoration and ambiguity equivalence.
7. Add explicit diagnostics and bump the target/calibrator identities.
8. Synchronize tests and operator documentation.

## 16. Actual file diff

### Production

- `app/outcomes.py`: adverse observed-extreme liquidation bound and diagnostics.
- `app/main.py`: version `1.0.52`, outcome contract `grid_label_v25`.
- `app/calibration.py`: bot/global keys v14.
- `app/recommender.py`: direction key v11.

### Tests

- added `tests/test_iteration240_kill_switch_slippage_bound.py`;
- updated two superseded boundary-fill expectations in iteration 217;
- synchronized version/identity assertions in affected tests.

### Documentation/operator artifacts

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX/PDF and root `how_to_trade.png`;
- this audit report.

### Database/frontend/migrations

No schema, migration SQL, frontend source, public route or env contract changed.

## 17. Red → green evidence

Red was independently reproduced on pristine source before the production patch:

```text
FAILED upper and lower kill-switch adverse-bound assertions
obtained -0.005, expected -0.0075
FAILED version/label identity assertion
3 failed
```

After the fix:

```text
3 passed
```

The targeted test was repeated deterministically:

```text
3 passed
3 passed
```

Related stop/outcome suite:

```text
40 passed
```

## 18. Database/schema compatibility

- relational schema: unchanged;
- `migrations/init.sql`: unchanged;
- `migrations/init_postgres.sql`: unchanged;
- fresh SQLite initialization: PASSED (`18` tables);
- repeated initialization: PASSED;
- simulated existing SQLite upgrade from `grid_label_v24` to `grid_label_v25`: PASSED;
- unrelated sentinel app configuration: preserved;
- legacy v13/v10 calibrator keys: removed by the existing target-reset family cleanup;
- PostgreSQL offline translation/locking suite: `18 passed`;
- live PostgreSQL: SKIPPED, no safe disposable DSN.

## 19. API compatibility

No route, request schema or response field was removed. New diagnostics are additive inside historical outcome evidence. FastAPI version changed from `1.0.51` to `1.0.52`.

## 20. Configuration compatibility

No environment variable was added, removed or reinterpreted. No operator configuration action is required.

## 21. Security and execution boundary

The service remains recommendation/audit-only:

- no private order create/amend/cancel implementation was added;
- no production credentials were used;
- no runtime order-execution assertion was added;
- the new price is explicitly a historical conservative proxy bound, not an exchange-attested fill.

## 22. Post-check commands and exact results

Commands included:

```text
python -m pip check
python -m compileall -q app tests main.py
python -m ruff check .
node --check app/ui/static/app.js
python -m pytest --collect-only -q
python -m pytest -q tests/test_iteration240_kill_switch_slippage_bound.py
python -m pytest -q <related outcome/stop nodes>
python -m pytest -q <all PostgreSQL-named offline tests>
python -m pytest -q
```

Results:

- `pip check`: FAILED only for the pre-existing MoviePy/Pillow conflict.
- compileall: PASSED.
- Ruff: UNAVAILABLE.
- JavaScript syntax: PASSED.
- collection: `1065 tests collected`.
- a monolithic final run stalled in the harness without a summary and was not counted as passing.
- before artifact-only edits, a complete monolithic run passed `1065` tests.
- final exhaustive verification after all code/docs/artifacts used non-overlapping deterministic batches whose union equals all `1065` collected nodes:

```text
178 + 178 + 178 + 177
+ 45 + 44 + 44 + 44
+ 45 + 44 + 44 + 44
= 1065 passed
```

- targeted new regression: `3 passed`, repeated twice;
- related stop/outcome suite: `40 passed`;
- PostgreSQL offline subset: `18 passed`;
- release/docs subset: `8 passed`;
- operator DOCX/PDF: 9 rendered pages, visually inspected; no clipping or broken glyphs;
- root PNG infographic: regenerated at `1344 × 1120` and visually inspected.

## 23. What could not be verified

- live PostgreSQL behavior without an explicitly disposable DSN;
- real stop-market fill prices, queue priority, spread impact and gap slippage;
- production Bybit account behavior or actual order submission, outside project scope;
- Ruff lint because the module is absent from the environment.

## 24. Residual risks

- Candle high/low is deliberately conservative but may overstate or understate an actual market-stop fill depending on intrabar sequence and liquidity.
- The proxy still lacks tick-level trade sequence, depth, queue priority and partial-fill evidence.
- Positive historical proxy expectancy remains insufficient proof of live profitability.
- A complete evaluation still requires terminally reconciled exchange fills, fees, settled funding, residual inventory, drawdown and block-bootstrap uncertainty.

## 25. Rollback procedure

Because no schema or env contract changed, rollback is code-only:

1. stop the service;
2. restore the previous v1.0.51 code package;
3. restart the service.

Rollback restores the confirmed optimistic perfect-boundary stop assumption. The v1.0.52 proxy outcomes/calibrators should not be reused by v1.0.51 for trading decisions because target identities differ.

## 26. Recommended next work package

Audit horizon-end and terminal-close pricing for residual inventory. Confirm that unresolved positions at maturity are not valued at an optimistic close or without a conservative liquidation-cost/slippage layer. Then compare every proxy stop/terminal close against exchange-attested execution data when such data becomes available.
