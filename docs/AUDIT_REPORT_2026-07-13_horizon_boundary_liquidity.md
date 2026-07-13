# Audit iteration 241 - horizon-boundary and liquidation volume integrity

## 1. Iteration name

Horizon-boundary and liquidation volume integrity.

## 2. Input ZIP

`bybit-reco-systems-1.0.52-kill-switch-slippage-bound.zip`

## 3. Input SHA-256

`e8269902d0475ad8ecdda963c6f279fbd242092398e7fd1f5bda3bc94fd3c1d4`

## 4. Initial version

FastAPI `1.0.52`; outcome contract `grid_label_v25`; bot/global calibrators v14; direction calibrator v11.

## 5. New version

FastAPI `1.0.53`; outcome contract `grid_label_v26`; bot/global calibrators v15; direction calibrator v12.

## 6. Project fingerprint

PASSED. The archive contains the expected Bybit Recommender root, FastAPI application, historical futures-grid outcome model, SQLite/PostgreSQL persistence, frontend, migrations, tests and operator artifacts. No private order create/amend/cancel implementation was added.

## 7. Iteration goal

After this iteration, a historical proxy outcome must not reuse liquidity from the wrong minute or liquidate residual inventory in quantity greater than the observed volume of the relevant candle. The strategy horizon remains unchanged, while evidence availability waits until the boundary candle volume is complete.

## 8. Acceptance criteria

1. Horizon-open gap fills use the exact boundary candle volume, not the previous candle budget.
2. Terminal residual liquidation consumes the remaining boundary candle capacity.
3. Kill-switch residual liquidation consumes the remaining breach-candle capacity.
4. A capacity failure survives intrabar path simulation and produces no label.
5. `label_available_ts` is `horizon_end_ts + 60` while `horizon_sec` is unchanged.
6. No runtime order submission or current-exchange executability dependency is introduced.
7. Full regression, SQLite, PostgreSQL-offline, documentation and repacked-ZIP checks pass.

## 9. Sources read

Relevant code and documentation included `README.md`, `CHANGELOG.md`, `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, recent audit reports, `app/outcomes.py`, `app/main.py`, `app/calibration.py`, `app/recommender.py`, database bootstrap code, and outcome/volume/kill-switch regression tests.

## 10. Affected data flow

`closed historical 1m OHLCV` -> `entry and in-window grid ledger` -> `breach or exact horizon boundary` -> `candle-local volume budget` -> `gap fill / kill-switch close / terminal residual close` -> `proxy net return` -> `reco_outcomes` -> monetary calibration.

## 11. Baseline environment

- Python: `3.13.5`
- Node: `22.16.0`
- `pip check`: pre-existing MoviePy/Pillow conflict only
- Ruff: unavailable in the environment
- input archive safety: 296 unique entries; no traversal, duplicates or symlinks

## 12. Baseline results

- compileall: PASSED
- JavaScript syntax: PASSED
- collection: `1065 tests collected`
- monolithic run: timed out in the harness without a summary and was not counted
- exhaustive non-overlapping batches: `178 + 178 + 178 + 177 + 177 + 177 = 1065 passed`

## 13. Confirmed defects

### DEF-241-01 - horizon gap fills inherited the previous minute's volume

- Severity: HIGH
- Type: CONFIRMED DEFECT
- File/function: `app/outcomes.py::_grid_outcome`
- Actual behavior: after the in-window loop, `process_segment(exit_f)` retained `candle_volume_capacity_qty` and `candle_volume_used_qty` from the final in-window candle.
- Reproducer: previous candle volume `100`; boundary candle volume `0.5`; one-unit Buy crossed by the horizon open.
- Old result: a label was produced using the previous minute's liquidity.
- Expected: the fill is unavailable because the boundary minute cannot support one unit.
- Financial impact: impossible gap fills could alter inventory, PnL and calibration evidence.

### DEF-241-02 - terminal residual close ignored candle volume

- Severity: HIGH
- Type: CONFIRMED DEFECT
- Actual behavior: all remaining inventory was marked liquidation-equivalent at `exit_f` and charged a market-cost leg, but no observed quantity capacity was consumed.
- Reproducer: one-unit residual LONG; boundary candle volume `0.5`.
- Old result: full terminal close and stored proxy return.
- Expected: unavailable outcome.

### DEF-241-03 - kill-switch residual close ignored remaining breach-candle volume

- Severity: HIGH
- Type: CONFIRMED DEFECT
- Actual behavior: a one-unit grid fill could consume most of a `1.5`-unit candle, after which a one-unit stop close was still assumed at full size.
- Expected: grid fill and liquidation share one candle budget.

### DEF-241-04 - invalid path state could survive stop-path restoration

- Severity: HIGH
- Type: CONFIRMED DEFECT
- Actual behavior: an intrabar simulation could set `ledger_invalid=True`, but after equivalent stop-path snapshots were restored the outer flow proceeded directly to PnL finalization without a universal post-loop invalid-state check.
- Effect: a liquidation-capacity failure could be hidden by path handling.

## 14. Unconfirmed claims

The iteration does not establish an exact participation rate, queue priority, level-specific depth, partial-fill sequence or live profitability. Total candle volume remains only a necessary capacity bound.

## 15. Fix plan

- Read and validate the exact boundary candle.
- Reset volume capacity and used quantity at the minute boundary.
- Share the boundary budget between gap fills and terminal close.
- Charge kill-switch close against remaining breach-candle capacity.
- Check `ledger_invalid` after intrabar simulation.
- Delay label availability by one minute.
- Reset incompatible proxy labels and calibrators.

## 16. Actual diff by file group

### Production

- `app/outcomes.py`
- `app/main.py`
- `app/calibration.py`
- `app/recommender.py`

### Tests

- added `tests/test_iteration241_horizon_boundary_liquidity.py`
- updated prior version/identity assertions and the exact label-availability expectation

### Documentation and operator artifacts

- README, CHANGELOG and relevant docs
- operator DOCX/PDF
- root `how_to_trade.png`
- this audit report

### Database/migrations/frontend

No schema, migration, API route or frontend source change.

## 17. Red -> green evidence

Red command:

`python -m pytest -q tests/test_iteration241_horizon_boundary_liquidity.py`

Essential red result:

`4 failed` - old code returned outcomes for wrong-minute gap capacity, undersized terminal close and undersized kill-switch close, and labeled before the boundary candle completed.

Green command: same command on the working copy.

Green result: `4 passed`.

Related outcome suite after minimal expectation updates: `41 passed`.

## 18. Database/schema compatibility

- SQLite fresh initialization: required and verified in post-check
- repeated initialization: required and verified
- existing SQLite upgrade: additive code-only bootstrap; no relational schema change
- outcome label reset removes incompatible proxy outcomes and the `logreg_*` / `platt_direction_*` key families
- PostgreSQL offline translation/locking tests: required in post-check
- live PostgreSQL: skipped without an explicitly disposable DSN

## 19. API compatibility

No public route or request field was removed. The stored outcome `label_available_ts` moves one minute later; `horizon_sec` remains the configured evaluation horizon.

## 20. Configuration compatibility

No environment variable was added, removed or reinterpreted.

## 21. Security and execution boundary

The project remains historical recommendation/audit-only. The fix does not submit, amend or cancel orders and does not claim runtime fill truth. Boundary candle volume is historical proxy evidence only.

## 22. Post-check results

Final results are recorded after documentation and release packaging:

- collection: `1069 tests`
- full exhaustive test result: `1069/1069 passed`
- targeted iteration 241: `4 passed`, repeated
- compileall and JavaScript syntax: PASSED
- SQLite fresh/repeat/upgrade: PASSED
- PostgreSQL offline subset: PASSED
- DOCX and PDF: 9 pages, rendered and visually inspected
- PNG infographic: visually inspected
- repacked ZIP integrity and tests from re-extracted archive: PASSED

## 23. Not verified

- live PostgreSQL without a safe disposable DSN
- actual order-book depth, queue priority and partial fills
- production Bybit private account state
- Ruff lint because Ruff is unavailable

## 24. Residual risks

- One-minute total volume can still greatly overstate the quantity available to this strategy at a particular level.
- No participation-rate or cross-strategy shared-liquidity model exists.
- Terminal mark/close price and stop price remain OHLCV proxies, not exchange fills.
- Positive proxy expectancy is not proof of live edge.

## 25. Rollback

No schema rollback is required. Restore v1.0.52 code and restart. Do not reuse v1.0.53 `grid_label_v26` calibration under the old target contract.

## 26. Recommended next work package

Audit the remaining full-volume assumption. Introduce a documented conservative participation/partial-fill policy or keep outcomes unavailable where the model cannot establish sufficient strategy-accessible liquidity. Validate sensitivity across participation assumptions rather than selecting a favorable fixed rate.
