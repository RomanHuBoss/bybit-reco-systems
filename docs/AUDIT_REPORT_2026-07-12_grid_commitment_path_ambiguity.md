# Audit iteration: exact grid commitment and path ambiguity

## 1. Iteration

Bybit Recommender v1.0.29 -> v1.0.30. Scope: arithmetic-grid capital commitment, sizing/preflight parity, outcome return normalization and two-sided OHLC path ambiguity.

## 2. Input ZIP

`bybit-reco-systems-1.0.29-grid-ledger-topology-stop.zip`

## 3. Input SHA-256

`b0b0c18f545a118b1eba499b6dceb066279793ebc42b375568dcdd9a7350f3ac`

## 4. Versions

- Original FastAPI version: `1.0.29`
- Original outcome target: `grid_label_v10`
- New FastAPI version: `1.0.30`
- New outcome target: `grid_label_v11`
- Version source of truth: `app/main.py`

## 5. Project fingerprint

PASS. The archive contains the required recommendation/audit-only FastAPI application, Bybit Linear USDT perpetual scope, `futures_grid`, SQLite/PostgreSQL persistence, canonical trading/grid modules, frontend files, tests, docs and both reference migration SQL files. No private Bybit order-create/amend/cancel endpoint was found in production code.

## 6. Goal

After this iteration the same arithmetic topology must determine:

1. generated active-order count;
2. committed notional and margin;
3. auto-snapped sizing;
4. strict execution preflight;
5. runtime worst-case caps;
6. outcome return denominator;
7. availability of labels from two-sided OHLC candles.

No layer may independently substitute `grid_count × reference × qty` when the executable topology contains a different number or price composition of committed slots.

## 7. Acceptance criteria

- `grid_count` remains the number of arithmetic intervals.
- Reference exactly on a level produces `N` active orders; reference between levels produces `N+1` active levels.
- Directional commitment includes initial inventory plus adverse-side opening orders at actual prices.
- Recommender, auto-snap, preflight, runtime caps and outcomes agree on commitment.
- Off-grid outcome return is divided by exact committed notional.
- Two admissible high/low orderings are simulated independently.
- Path-dependent ledger/stop/PnL produces no proxy label.
- New regression is RED on v1.0.29 and GREEN on v1.0.30.
- Full post-check is green.

## 8. Sources read

- `README.md`, `CHANGELOG.md`, `.env.example`
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`
- latest audit reports through `AUDIT_REPORT_2026-07-12_grid_ledger_topology_and_stop.md`
- `app/grid_math.py`, `recommender.py`, `outcomes.py`, relevant sizing/preflight/runtime sections of `app/main.py`
- associated regression tests and operator artifacts
- official Bybit Futures Grid P&L/getting-started/FAQ documentation for interval and investment semantics

## 9. Affected data flow

`range/reference/direction/grid_count` -> canonical arithmetic topology -> generated qty/active levels/commitment -> snapped payload -> strict Bybit validation -> runtime notional/margin caps -> OHLCV inventory ledger -> liquidation-equivalent net PnL -> normalized outcome -> calibration/statistics.

## 10. Baseline environment

- Python `3.13.5`
- Node `v22.16.0`
- Production Python files: 23
- Baseline test files: 161
- Baseline collected tests: 936
- Documentation files: 41
- Frontend files: 3
- Migration SQL files: 2
- Database backends: SQLite and PostgreSQL/psycopg translation layer

`pip check` had one pre-existing environment conflict: MoviePy 2.2.1 requires Pillow `<12`, while Pillow 12.2.0 is installed. `ruff` was unavailable in the environment.

## 11. Baseline checks

- ZIP safety/traversal/duplicate-path check: PASSED
- `python -m compileall -q app tests main.py`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- `python -m pytest --collect-only -q`: 936 collected
- Monolithic baseline run: TIMED OUT after partial progress, no failure summary
- Exhaustive deterministic baseline batches:
  - 234 passed in 6.36 s
  - 234 passed in 13.20 s
  - 234 passed in 6.08 s
  - 234 passed in 5.93 s
  - union: 936/936 passed

## 12. Confirmed defects

### GRID-COMMIT-001 — CRITICAL — interval count used as committed-slot count

- Type: CONFIRMED DEFECT
- Files: `app/recommender.py`, `app/main.py`, `app/outcomes.py`
- Runtime behavior: sizing and outcome used `grid_count` as if it were always the number of funded/active slots.
- Counterexample: a two-interval grid has three price levels. When reference lies between levels, all three are active; directional commitment is initial position plus adverse-side opening orders.
- Financial impact: margin and worst-case notional understated; normalized return overstated. At `N=2`, the denominator error can approach 50%.
- Why old tests missed it: expectations explicitly asserted `estimated_active_orders == grid_count` and divided PnL by `reference × grid_count`.
- Expected behavior: derive the exact topology from range, reference, direction and interval count.

### GRID-COMMIT-002 — HIGH — auto-snap reintroduced the old capital model

- Type: CONFIRMED DEFECT
- File: `app/main.py`, `_snap_reco_payload_to_bybit_meta`
- Runtime behavior: even when generated sizing was corrected, auto-snap rewrote total/worst-case notional as `order_notional × grid_count` and `max_price × qty × grid_count`.
- Trading/risk impact: snapped payload, preflight and runtime caps could disagree; a corrected recommendation could be made understated again before operator display/execution validation.
- Expected behavior: auto-snap must call the same commitment helper.

### GRID-PATH-003 — HIGH — two-sided OHLC produced an impossible third result

- Type: CONFIRMED DEFECT
- File: `app/outcomes.py`
- Runtime behavior: when both high and low excursions were material, endpoint-only processing could return a cash/inventory/PnL state produced by neither `O→H→L→C` nor `O→L→H→C`.
- Minimal example: neutral range 98–102, entry/open 99, high 99.5, low 98, close 98.5. High-first and low-first produce different valid ledgers, while old code returned `(success=0, ret=0)`.
- Model/data impact: fabricated labels polluted win rate and calibration.
- Expected behavior: simulate both admissible paths; keep the label only when complete ledger state is equivalent.

### GRID-PATH-004 — HIGH — stop-candle chronology was invented

- Type: CONFIRMED DEFECT
- File: `app/outcomes.py`
- Runtime behavior: a candle containing both a protective-boundary hit and an opposite grid excursion was assigned one assumed chronology.
- Impact: pre-stop completed trades and stop PnL could be omitted or invented.
- Expected behavior: compare stop-first and opposite-excursion-first states; differing states are unavailable.

## 13. Unconfirmed claims

- No claim of strategy profitability was confirmed.
- No evidence proves that every mathematical defect has been found.
- No live fill/queue/partial-fill reconstruction was attempted.
- The working database shipped in the release archive contained no user trading history suitable for recalculation of the user's observed month.

## 14. Red test

New file: `tests/test_iteration218_grid_commitment_and_path_ambiguity.py`

Command on pristine v1.0.29 plus only the new tests:

```bash
python -m pytest -q tests/test_iteration218_grid_commitment_and_path_ambiguity.py
```

Result:

```text
9 failed in 1.13s
```

Representative RED evidence:

```text
estimated_active_orders: 11 instead of 12
ACTIVE_ORDERS_GRID_COUNT_MISMATCH
TOTAL_NOTIONAL_GRID_COUNT_MISMATCH
Long off-grid return: 0.00248756 instead of 0.00166945
Short off-grid return: 0.00251256 instead of 0.00166389
ambiguous two-sided candle: (0, 0.0) instead of unavailable
ambiguous stop candle: (0, -0.005) instead of unavailable
auto-snap active orders: 11 instead of 12
```

## 15. Implementation

### `app/grid_math.py`

Added `arithmetic_grid_commitment`, returning:

- `N+1` prices;
- exact on-level/off-level classification;
- buy/sell index sets;
- initial LONG/SHORT slots;
- active-order count;
- maximum position slots;
- committed notional per unit quantity.

### `app/recommender.py`

Generated sizing/economics now use exact commitment and publish diagnostic model fields.

### `app/main.py`

- auto-snap preserves exact commitment;
- strict preflight validates active orders and total notional against executable topology;
- runtime caps derive worst-case notional from committed active levels;
- version/target bumped to `1.0.30` / `grid_label_v11`.

### `app/outcomes.py`

- return denominator is exact committed notional;
- full ledger snapshots can be simulated/restored;
- high-first and low-first paths are compared;
- path-dependent outcomes return unavailable.

## 16. Test changes

Old tests were updated only where their oracle encoded the disproved model. Expectations now use independent monetary sums, for example:

- LONG 90–110, N=20, ref=100 commitment: `10×100 + sum(90..99) = 1945`;
- SHORT counterpart: `10×100 + sum(101..110) = 2055`;
- LONG off-grid 99–101, N=2, ref=100.5 commitment: `100.5 + 99 + 100 = 299.5`.

Cohort/lineage/LLM tests were given path-unambiguous candles because those tests do not assert intrabar sequencing.

## 17. Green evidence

```bash
python -m pytest -q tests/test_iteration218_grid_commitment_and_path_ambiguity.py
```

```text
9 passed
```

Related economics/outcome/preflight package:

```text
91 passed
```

## 18. Database/schema compatibility

No schema change and no migration SQL change.

- Fresh SQLite bootstrap: 16 application tables
- Repeated SQLite bootstrap: 16 application tables
- PostgreSQL dialect/locking/deadlock tests: 18 passed
- Live PostgreSQL integration: SKIPPED; no confirmed disposable DSN was supplied

The outcome target bump causes the existing version guard to clear only incompatible proxy outcomes and related calibrators. Recommendations, bot instances, trades, exact execution evidence and risk settings are preserved.

## 19. API/config compatibility

- No route change
- No JSON field removal
- Added sizing diagnostics are backward-compatible
- No `.env` variable change
- No status/lifecycle change

## 20. Security and execution boundary

The service remains recommendation/audit-only. Static search found no private order create/amend/cancel endpoint or equivalent production method. No credentials are required or included.

## 21. Documentation artifacts

Updated:

- `README.md`, `CHANGELOG.md`
- `docs/TRADING_LOGIC.md`, `KNOWN_RISKS.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- operator DOCX/PDF
- root `how_to_trade.png`

DOCX and PDF both render to five clean pages; every page was visually inspected. The infographic was regenerated with v1.0.30 commitment/path rules and re-embedded into the DOCX/PDF.

## 22. Post-check

- `python -m compileall -q app tests main.py`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- `python -m pytest --collect-only -q`: 945 collected
- `python -m pytest -q`: 945 passed in 26.28 s
- Target regression repeated: PASSED
- PostgreSQL unit/dialect/locking tests: 18 passed
- SQLite fresh/repeated bootstrap: PASSED
- Version consistency: PASSED
- Private order endpoint search: PASSED
- Operator DOCX/PDF/PNG visual QA: PASSED
- `ruff`: UNAVAILABLE
- `pip check`: pre-existing MoviePy/Pillow conflict, unrelated to this change

## 23. Residual risks

- OHLCV cannot establish queue priority, partial fills, maker/taker status or exact intrabar chronology beyond path invariance.
- Path-dependent candles are excluded, which reduces sample size and may introduce a selection effect; exclusion counts must be monitored.
- Worst-case notional based on range maximum remains a conservative risk bound, not an exchange liquidation simulation.
- Positive proxy outcomes do not prove live edge.

## 24. Rollback

1. Stop the application.
2. Restore v1.0.29 code.
3. Restore the `data/app.db` backup taken before first v1.0.30 startup if old `grid_label_v10` outcomes/calibrators must be retained.
4. Do not restore stale runtime-lock database files.

## 25. Recommended next work package

After enough `grid_label_v11` observations accumulate, compare proxy and exact execution evidence by symbol/direction/regime. Report path-ambiguity exclusion rate, committed-capital utilization, margin error versus external executor, and net PnL disagreement before changing launch thresholds.

## 26. Commit message

```text
fix(grid): unify commitment and reject path-dependent outcomes

- derive active levels, inventory and notional from exact arithmetic topology
- use one commitment model in sizing, snap, preflight, runtime caps and outcomes
- reject two-sided OHLC candles whose admissible paths produce different ledgers
- bump outcome target to grid_label_v11
- add iteration218 RED-to-GREEN coverage and synchronize operator artifacts
```
