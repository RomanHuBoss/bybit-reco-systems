# Audit iteration 238 — intrabar replacement-order timing

## 1. Iteration title

**Fail-closed proxy execution when replacement-order activation time is not observable inside a one-minute candle.**

## 2. Input ZIP

`bybit-reco-systems-1.0.49-proxy-fill-volume-capacity.zip`

## 3. Input ZIP SHA-256

`8764ae72f8d4cdbbf2e536f42d8d6798965ccc1db184979490e9e487397dcc56`

## 4. Source version

`1.0.49`, from the `version=` argument of the FastAPI application in `app/main.py`.

Source outcome/calibration identities:

- outcome label: `grid_label_v22`;
- bot calibrator: `logreg_futures_grid_v11`;
- global calibrator: `logreg_global_v11`;
- direction calibrator: `platt_direction_v8`;
- model identity: `bybit-taxonomy-v5-exchange-normalized-shadow-roots`.

## 5. New version

`1.0.50` — patch release.

New identities:

- outcome label: `grid_label_v23`;
- bot calibrator: `logreg_futures_grid_v12`;
- global calibrator: `logreg_global_v12`;
- direction calibrator: `platt_direction_v9`;
- model identity unchanged.

## 6. Project fingerprint

The archive contains one root, `bybit-reco-systems-main`, and matches the Bybit Recommender fingerprint:

- `README.md`, `CHANGELOG.md`, requirements files and `main.py` are present;
- FastAPI application is created in `app/main.py`;
- supported bot type is `futures_grid`;
- supported venue scope is Bybit Linear USDT perpetual;
- service remains recommendation/audit-only, not OMS/EMS;
- SQLite and PostgreSQL support remain present;
- canonical directional helpers remain in `app/trading_semantics.py`;
- frontend remains under `app/ui/static/`;
- both reference migration SQL files are present.

Input archive safety checks found no absolute paths, `..` traversal, external symlinks or conflicting duplicate entries.

## 7. Iteration objective

After this iteration, the proxy outcome engine must not assume that a replacement grid order was submitted, acknowledged and queue-active immediately after its parent fill inside the same one-minute OHLCV candle. A cycle that depends on such unobservable intrabar timing must be excluded from evidence rather than resolved through the optimistic zero-latency path.

## 8. Acceptance criteria

1. A parent fill and a crossing of its newly created replacement inside the same one-minute candle produce no outcome label.
2. The diagnostic reason is `intrabar_replacement_fill_timing_unobservable`.
3. A replacement crossed in a later candle remains labelable and preserves the existing completed-cycle PnL formula.
4. Path snapshots include pending replacement orders so OHLC ambiguity checks remain deterministic.
5. Old proxy outcomes and calibrators cannot be reused under the changed target contract.
6. DB schema, public routes and environment-variable contract remain unchanged.
7. The new regression is red on pristine 1.0.49 and green after the production fix.
8. Every collected test node passes after the change.

## 9. Sources read

The audit used the current ZIP as the source of runtime truth and reviewed the relevant parts of:

- `README.md`, `CHANGELOG.md`, `.env.example` and requirements files;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md` and `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- the five most recent available audit reports;
- `app/outcomes.py`, `app/grid_math.py`, `app/trading_semantics.py`, `app/recommender.py`, `app/calibration.py`, `app/main.py`, `app/db.py`, `app/db_backend.py`, `app/bybit_client.py`, `app/risk.py` and settings/security modules;
- outcome/grid regression tests from iterations 208–237 and release-artifact tests.

No production credentials or private Bybit order endpoint were used.

## 10. Affected data-flow map

`exchange-normalized recommendation` → `matured 1m OHLCV` → `_grid_outcome()` → `resting parent order fill` → `replacement order creation` → `intrabar path reconstruction` → `proxy success/ret` → `reco_outcomes` → `monetary calibration` → `confidence/readiness` → `publication gate`.

The defect was between replacement creation and the next observable candle boundary.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `v22.16.0`;
- project root: `bybit-reco-systems-main`;
- production Python files under `app/`: 23;
- test files after the iteration: 182;
- documentation files: 60 before the new report;
- frontend files: 3;
- migration SQL files: 2;
- API route decorators: 22, of which 6 are mutating POST routes;
- supervised background threads: collector, backfill, futures metadata, sentiment, recommender and optional LLM reviewer.

`pip check` reports an environment-level conflict: MoviePy 2.2.1 requires Pillow `<12`, while Pillow 12.2.0 is installed. The project dependency files were not changed in this scope. Ruff is unavailable in the environment (`No module named ruff`).

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
- `pip check`: FAILED due to the external MoviePy/Pillow conflict described above.
- compileall: PASSED.
- Ruff: UNAVAILABLE.
- JavaScript syntax: PASSED.
- collection: `1056 tests collected`.
- monolithic pytest: TIMED OUT/NO FINAL SUMMARY in the harness and was not counted as a pass.
- exhaustive baseline: `1056/1056` unique nodes passed. Five 176-node batches completed directly; the harness-stalled fifth batch was exhaustively replaced by four deterministic non-overlapping 44-node batches. The union equals the collected node set.

## 13. Confirmed defects and gaps

### IR-238 — zero-latency same-candle replacement fill

- Severity: **high**.
- Type: **CONFIRMED DEFECT**.
- File: `app/outcomes.py`.
- Function: `_grid_outcome()`, especially replacement creation and `process_segment()`.
- Relevant fixed ranges: approximately lines 914–923, 1029–1061, 1071–1129, 1181–1225, 1255–1260, 1373–1378 and 1409–1413.

Input reproducer:

```text
Neutral arithmetic grid: 99 / 100 / 101
Candle 1: O=100, H=101.1, L=99.9, C=99.9
Candle 2: O=H=L=C=101.5
```

Data path:

1. Sell 101 is an initially resting order.
2. Candle high above 101 confirms the parent Sell in the proxy model.
3. The model immediately creates replacement Buy 100.
4. The same candle low below 100 is then treated as proof that the replacement filled.
5. A completed positive cycle is written even though OHLCV contains no parent-fill time or replacement submit/ack time.

Actual pristine behavior:

```text
_grid_outcome(...) == (1, 0.0025)
```

Expected safe behavior:

```text
_grid_outcome(...) is None
reason = intrabar_replacement_fill_timing_unobservable
```

Violated invariants:

- fail-closed on unobservable mandatory execution facts;
- proxy outcome must not reconstruct queue timing that is absent from OHLCV;
- calibration evidence must not assume zero bot/network/exchange latency;
- a favorable path must not be chosen when both filled and not-yet-active paths are compatible with the same candle.

Financial/trading impact:

- manufactured completed cycles and positive proxy return;
- inflated proxy win rate and monetary expectancy;
- potentially narrower confidence bounds and earlier fitted calibration;
- possible transition from shadow `no_trade` toward actionable status using execution evidence that was not observable.

Why existing tests missed it:

Several legacy fixtures explicitly expected a parent fill and its replacement fill in the same candle. Those tests validated PnL/topology/capital formulas but encoded zero-latency order activation as an unstated assumption.

Regression test:

`tests/test_iteration238_intrabar_replacement_latency.py`

Red command:

```text
python -m pytest -q tests/test_iteration238_intrabar_replacement_latency.py
```

Red result on pristine production code plus the new test:

```text
assert (1, 0.0025) is None
2 failed, 1 passed
```

Green result:

```text
3 passed in 0.37s
```

Residual risk after the fix:

The next-candle activation rule is deliberately conservative and still does not reproduce actual submit latency, acknowledgement latency, queue priority or partial fills. Exchange-attested fills remain authoritative.

## 14. Unconfirmed claims

- The release archive does not prove that the strategy is inherently or mathematically always loss-making.
- It also does not prove positive live edge because it contains no representative terminally reconciled execution database.
- This iteration did not find evidence that fees, funding, committed-capital denominator or completed-cycle arithmetic were changed by the replacement-timing fix.
- Real order acknowledgement latency and queue position were not tested because the project is not an OMS/EMS and no safe private exchange test environment was supplied.

## 15. Fix plan

1. Keep initial resting orders active as before.
2. Store orders created by fills in a separate pending collection.
3. Do not activate pending orders until the next candle boundary.
4. If the current candle path crosses a pending replacement, return outcome-unavailable with an explicit reason rather than choosing a fill/no-fill path.
5. Include pending orders in path snapshots and equivalence checks.
6. Preserve separate-candle completed-cycle economics.
7. Bump outcome/calibrator identities and update regression/documentation artifacts.

## 16. Actual file diff

### Production

- `app/outcomes.py`: pending replacement lifecycle, same-candle ambiguity veto and diagnostics.
- `app/main.py`: version `1.0.50`, outcome contract `grid_label_v23`.
- `app/calibration.py`: bot/global keys v12.
- `app/recommender.py`: direction key v9.

### Tests

- added `tests/test_iteration238_intrabar_replacement_latency.py`;
- updated three same-candle zero-latency fixtures while preserving their original economic/topology purpose;
- synchronized version/identity assertions in affected regression files.

### Documentation and operator artifacts

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX/PDF and root PNG infographic;
- this report.

### Database/frontend/migrations

No relational schema, migration SQL, frontend source or route contract changed.

## 17. Red → green evidence

Red independently reproduced on pristine source before applying the production fix:

```text
FAILED test_same_candle_replacement_fill_is_unavailable_without_order_timestamps
E assert (1, 0.0025) is None
```

The contract-bump assertion also failed on pristine v1.0.49, as expected.

After the fix:

```text
3 passed in 0.37s
```

Related outcome-importing suite after correcting superseded zero-latency fixtures:

```text
165 passed
```

## 18. Database/schema compatibility

- No schema change.
- Fresh SQLite initialization: PASSED, 17 application tables.
- Repeated initialization: PASSED.
- Simulated existing SQLite v1.0.49 → v1.0.50 bootstrap: PASSED.
- Sentinel app configuration survived the upgrade.
- `outcome_label_version` changed from `grid_label_v22` to `grid_label_v23`.
- historical `logreg_*` and `platt_direction_*` calibration entries were removed on label reset.
- PostgreSQL live integration: SKIPPED because no explicitly disposable DSN was supplied.
- PostgreSQL translation/locking/publication safety subset: `24 passed`.

## 19. API compatibility

- 22 route decorators remain present.
- Six mutating POST endpoints remain unchanged.
- No route was added, removed or renamed.
- No new private exchange execution method was introduced.
- The outcome diagnostic is additive and internal/audit-facing.

## 20. Config/environment compatibility

No environment variable was added, removed or renamed. Existing `.env.example` remains sufficient. No operator configuration action is required.

## 21. Security boundary

- Service remains recommendation/audit-only.
- Static production scan found no `/v5/order/create`, amend, cancel or equivalent private order method.
- No `.env`, private key or credential file is included in the release.
- The fix makes the proxy more conservative and does not add network calls.

## 22. Post-check commands and exact results

Commands include:

```text
python -m pip check
python -m compileall -q app tests main.py
python -m ruff check .
node --check app/ui/static/app.js
python -m pytest --collect-only -q
python -m pytest -q tests/test_iteration238_intrabar_replacement_latency.py
# all collected nodes split into six deterministic non-overlapping batches
```

Final results:

- collection: `1059 tests collected`;
- targeted iteration 238: `3 passed`;
- exhaustive final suite: `177 + 177 + 177 + 176 + 176 + 176 = 1059`, all passed;
- related outcome suite: `165 passed`;
- PostgreSQL offline subset: `24 passed`;
- release-artifact subset: `16 passed`;
- compileall: PASSED;
- JavaScript syntax: PASSED;
- SQLite fresh/re-init/upgrade: PASSED;
- Ruff: UNAVAILABLE;
- `pip check`: FAILED only for the pre-existing environment MoviePy/Pillow conflict.

An initial final-batch attempt exposed two stale test assertions that still expected calibrator v11. They were updated to the new v12 identity; the production behavior was not weakened. The entire final node set was then rerun and passed.

## 23. Not verified and reasons

- Live PostgreSQL behavior: no safe disposable DSN.
- Actual Bybit order submit/acknowledgement and queue timing: outside the recommendation-only boundary and no private credentials were used.
- Real partial fills and price-level liquidity: unavailable in one-minute OHLCV.
- Live profitability: no representative exchange-attested execution dataset in the release.
- Ruff: tool unavailable in the environment.

## 24. Residual risks

1. Activating a replacement at the next candle boundary is conservative, not an exact execution simulator.
2. A replacement could genuinely have filled later in the same minute; such observations are intentionally discarded rather than optimistically labelled.
3. Strict trade-through and aggregate candle-volume checks still do not prove level-specific liquidity or queue priority.
4. Multiple bots may compete for the same market volume outside this single-outcome proxy model.
5. Positive proxy expectancy remains insufficient without terminal exchange reconciliation and block/purged monetary validation.

## 25. Rollback procedure

No schema rollback is required. Restore the previous 1.0.49 code and restart the service. This rollback is not recommended because it restores the confirmed zero-latency same-candle replacement assumption. Do not restore v11/v8 calibrators or `grid_label_v22` outcomes for trading decisions after operating under v1.0.50.

## 26. Recommended next work package

Build a latency/partial-fill attribution layer against exchange-attested events. For each grid parent/replacement pair, compare recommendation-time snapped levels with actual submit timestamp, acknowledgement timestamp, fill timestamp, partial quantity, queue outcome, fees/rebates, funding, residual position and terminal exact net PnL. Measure how often the one-minute proxy overstates completed cycles, then run purged/block-bootstrap validation on exact monetary results.
