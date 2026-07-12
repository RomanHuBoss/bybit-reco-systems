# Audit iteration: neutral one-way commitment integrity

## 1. Input and release identity

- Input ZIP: `bybit-reco-systems-1.0.31-grid-order-quantity-gap-stop.zip`
- Input SHA-256: `b0f22ce320b1e23214ed2d18026d8023dbb00d1d9092ae6d76bbf5595cead146`
- Input version: `1.0.31`
- Input outcome target: `grid_label_v12`
- New version: `1.0.32`
- New outcome target: `grid_label_v13`
- Planned release ZIP: `bybit-reco-systems-1.0.32-neutral-one-way-commitment.zip`
- Iteration test: `tests/test_iteration220_neutral_one_way_commitment.py`

The input archive passed duplicate-path, traversal, absolute-path, symlink and nested-archive checks. It was expanded into separate pristine, RED and working copies. The input ZIP was not modified.

## 2. Project fingerprint

Fingerprint: PASS.

The root contains the expected Bybit Recommender files, including `README.md`, `CHANGELOG.md`, `requirements*.txt`, `main.py`, the FastAPI application, canonical trading/grid modules, dual-persistence modules, frontend assets, tests, documentation and both SQL initialization files.

Verified boundaries:

- supported bot type: `futures_grid`;
- venue scope: Bybit linear USDT perpetual;
- recommendation/audit service, not OMS/EMS;
- SQLite and PostgreSQL remain supported;
- frontend remains in `app/ui/static/`;
- no private create/amend/cancel order endpoint was found in production code.

## 3. Iteration objective and acceptance criteria

Objective:

> After this iteration, neutral arithmetic-grid capital, margin, risk and proxy-return normalization must follow one-way commitment semantics: all opposite resting orders remain visible, while the reserved commitment is the more expensive directional opening stack rather than the sum of both mutually exclusive sides.

Acceptance criteria:

1. Exact-level neutral 99/100/101 topology reports two active orders but one committed/max-position slot and 101 USDT commitment per unit qty.
2. Off-grid reference 100.5 reports three active orders, two committed/max-position slots and 199 USDT commitment per unit qty.
3. Recommender and auto-snap publish separate active, committed and max-position fields.
4. Strict preflight accepts a correct one-way neutral commitment and rejects malformed/conflicting slot fields.
5. Runtime notional/margin caps and daily-loss fallback use maximum one-way position slots, not all opposite resting orders.
6. Neutral outcome return uses one-way committed investment as denominator.
7. LONG/SHORT commitment behavior remains unchanged.
8. The new regression test is RED on 1.0.31 and GREEN on 1.0.32; the complete suite remains green.

## 4. Sources reviewed

Project sources included:

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- the latest audit reports, especially the v1.0.30 and v1.0.31 commitment/ledger reports;
- `app/grid_math.py`, `app/recommender.py`, `app/outcomes.py`, relevant `app/main.py` risk/preflight/snap paths;
- related iteration tests and release-artifact tests.

External primary source:

- Bybit Help Center, “Order Cost (USDT Perpetual & Futures)”: in one-way mode, when opposite limit orders are active simultaneously, funds are reserved according to the order with the higher order cost rather than the sum of both sides.

## 5. Baseline environment and inventory

- Python: `3.13.5`
- Node: `22.16.0`
- Application version: `1.0.31`
- Outcome target: `grid_label_v12`
- Production Python files under `app/`: 23
- Test files: 163 before iteration220
- Documentation files: 43
- Frontend files: 3
- Migration SQL files: 2
- Highest existing iteration number: 219

Baseline commands and results:

- `python -m pip check`: FAILED because the shared environment contains MoviePy/Pillow version conflict; unrelated to this repository diff.
- `python -m compileall -q app tests main.py`: PASSED.
- `python -m ruff check .`: UNAVAILABLE; `ruff` is not installed.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest --collect-only -q`: 953 collected.
- `python -m pytest -q`: 953 passed in 27.56 seconds, exit code 0.

The input ZIP contained an empty runtime `data/app.db` and runtime-lock database. Counts for recommendations, outcomes, bots, trades and OHLCV were zero. These runtime files are excluded from the new release.

## 6. Affected data-flow map

1. `app/grid_math.py::arithmetic_grid_commitment` resolves grid levels, resting Buy/Sell topology, initial directional inventory and commitment.
2. `app/recommender.py` derives qty, active orders, total committed notional, worst-position notional and margin.
3. `app/main.py::_snap_reco_payload_to_bybit_meta` preserves exchange-rounded sizing/economics.
4. `app/main.py::_validate_trade_plan_against_bybit_meta` validates active orders, committed slots, maximum position slots, total notional and margin.
5. `app/main.py::_execution_runtime_size_risk_blocks` and `_execution_daily_loss_budget_guard` enforce operator caps.
6. `app/outcomes.py::_grid_outcome` uses canonical committed notional to normalize liquidation-equivalent total PnL.
7. Outcome-version guard separates incompatible proxy labels and calibrators.

## 7. Confirmed defects

### NOC-001 — CRITICAL — neutral commitment summed mutually exclusive sides

Type: CONFIRMED DEFECT.

Affected function: `app/grid_math.py::arithmetic_grid_commitment`.

For neutral 99/100/101 at reference 100 and unit qty:

- active Buy order: 99;
- active Sell order: 101;
- v1.0.31 committed notional: `99 + 101 = 200`;
- one-way committed notional: `max(99, 101) = 101`.

For reference 100.5:

- Buy stack: `99 + 100 = 199`;
- Sell stack: `101`;
- v1.0.31: 300;
- correct one-way commitment: 199.

Financial effect: neutral required capital/margin could be overstated by nearly 100%, while normalized return was understated by nearly 50% for symmetric grids. This can suppress recommendations and make a valid neutral result appear materially worse.

Why tests missed it: prior tests correctly distinguished `N` vs `N+1` active levels but treated every opposite resting order as simultaneously financed capital. The test oracle reproduced the defect.

### NOC-002 — HIGH — risk paths reused active-order count as one-way exposure

Type: CONFIRMED DEFECT.

Affected paths:

- Bybit payload snap;
- execution runtime notional/margin caps;
- daily-loss fallback;
- strict execution preflight.

The code multiplied worst price by total active orders. For neutral one-way mode, that is not maximum directional position size. The correct count is the larger of Buy-opening and Sell-opening slots.

Effect: false over-limit blocks, inflated daily-loss estimates and inconsistent operator fields.

### NOC-003 — HIGH — neutral proxy returns used the inflated both-side denominator

Type: CONFIRMED DEFECT.

Affected function: `app/outcomes.py::_grid_outcome` through the canonical topology helper.

A zero-cost 100 -> 101 -> 100 neutral cycle earned 1 USDT. v1.0.31 normalized it by 200 and returned 0.5%; the one-way investment is 101, so the internally consistent return is `1/101 = 0.990099%`.

Effect: win/loss sign normally remained unchanged, but return magnitude, averages, calibration features and strategy comparisons were systematically biased downward.

### NOC-004 — MEDIUM — one field represented three different quantities

Type: CONFIRMED GAP.

`estimated_active_orders` was used for resting-order count, capital slots and maximum position slots. Those quantities coincide in some directional layouts but not in neutral one-way grids.

Effect: backend components could not distinguish a correct two-sided order topology from simultaneous capital exposure.

## 8. Claims not established

This iteration does not establish:

- that the strategy is profitable;
- that Bybit will reserve exactly the proxy amount in a live account with existing positions/orders;
- that proxy OHLCV outcomes reproduce queue priority, partial fills, fee tier or private-account margin state;
- that all remaining mathematical defects have been found;
- production readiness for automatic order execution.

The current project remains recommendation/audit-only.

## 9. Implemented fix

### Canonical topology and commitment

`arithmetic_grid_commitment` now returns separate fields:

- `active_order_count`: all resting Buy and Sell orders;
- `committed_slot_count`: slots in the larger directional opening stack;
- `max_abs_position_slots`: maximum one-way inventory slots;
- `buy_opening_notional_per_qty`;
- `sell_opening_notional_per_qty`;
- `committed_notional_per_qty`.

For NEUTRAL:

```text
committed_notional_per_qty = max(sum(Buy opening prices), sum(Sell opening prices))
committed_slot_count = max(Buy opening count, Sell opening count)
max_abs_position_slots = committed_slot_count
```

LONG/SHORT retain initial directional inventory plus adverse-side opening commitment.

### Propagation

The separated contract is consumed by:

- generated sizing/economics;
- operator sheet and trade plan;
- Bybit auto-snap;
- strict preflight;
- runtime notional/margin caps;
- daily-loss fallback;
- proxy-outcome return denominator.

Preflight now rejects non-integer or topology-inconsistent committed/max-position fields with dedicated diagnostic codes.

### Versioning

- FastAPI version: `1.0.32`.
- Outcome target: `grid_label_v13`.

The target bump is required because historical neutral returns used a different capital denominator.

## 10. RED -> GREEN evidence

RED command on pristine 1.0.31 plus only the new test:

```bash
python -m pytest -q tests/test_iteration220_neutral_one_way_commitment.py
```

RED result:

```text
8 failed in 1.58s
committed_slot_count: 2 instead of 1
committed_slot_count off-grid: 3 instead of 2
missing estimated_committed_slots
neutral return: 0.005 instead of 1/101
neutral daily-loss notional: 202 instead of 101
TOTAL_NOTIONAL_GRID_COUNT_MISMATCH for correct one-way payload
```

GREEN command:

```bash
python -m pytest -q tests/test_iteration220_neutral_one_way_commitment.py
```

GREEN result:

```text
8 passed in 1.17s
```

Related mathematical suite:

```text
94 passed
```

Old tests were changed only where their independent oracle explicitly summed both mutually exclusive neutral sides or expected the obsolete source label. Directional monetary assertions remain unchanged.

## 11. Database and compatibility

- No database schema change.
- `migrations/init.sql` and `migrations/init_postgres.sql` are unchanged.
- Fresh SQLite bootstrap: 16 user tables.
- Repeated SQLite bootstrap: 16 user tables.
- Existing marker row survived repeated initialization.
- PostgreSQL dialect/row-locking/deadlock tests: 18 passed.
- Live PostgreSQL integration: SKIPPED because no explicitly disposable DSN was provided.

At first 1.0.32 startup, the existing version guard deletes only incompatible proxy outcomes and related calibrators. It preserves recommendations, bot instances, trades, exact execution evidence and risk settings.

## 12. API, configuration and security boundary

- No route change.
- No JSON field removal. New sizing/economics fields are additive.
- No environment-variable change.
- No private Bybit order create/amend/cancel path added.
- No real credentials used.
- SQLite support remains.
- PostgreSQL support remains.
- Project remains recommendation/audit-only.

## 13. Documentation synchronization

Updated:

- `README.md`;
- `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `docs/SCENARIOS.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX/PDF;
- `how_to_trade.png`.

The operator documentation distinguishes resting-order count from one-way committed/max-position slots and identifies `grid_label_v13` as the current proxy target. DOCX and PDF contain five visually inspected pages; the embedded infographic was also inspected at full resolution.

## 14. Final post-check

Post-change results before release packaging:

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest --collect-only -q`: 961 collected.
- Pre-documentation monolithic `python -m pytest -q`: 961 passed in 26.86 seconds.
- Final monolithic run after documentation updates: TIMED OUT at 89% without a failure summary and is not counted as a pass.
- Final exhaustive file batches: `240 + 236 + 235 + 250 = 961`, all passed; the union equals the collected set.
- iteration220 repeated: PASSED.
- related commitment/outcome/risk suite: 94 passed.
- PostgreSQL dialect/locking suite: 18 passed.
- SQLite fresh and repeated bootstrap: PASSED, 16/16 tables.
- forbidden private order endpoint scan: no production match.
- `ruff`: UNAVAILABLE.
- `pip check`: external MoviePy/Pillow conflict remains.

Final ZIP verification and its SHA-256 are recorded in the user-facing release response after clean re-extraction and test execution.

## 15. Limitations and residual risks

- The supplied ZIP contained no user trading history, so the displayed live-instance statistics could not be recalculated.
- The one-way commitment proxy does not know existing private positions, conditional orders or exchange-side balance offsets.
- OHLCV proxy outcomes still cannot prove exact fills, queue priority, partial fills, maker/taker mix, actual fee tier, gap execution or liquidation behavior.
- New `grid_label_v13` statistics must not be mixed with prior targets.
- A positive proxy return is not evidence of live alpha.

## 16. Rollback

1. Stop the application.
2. Restore the v1.0.31 code.
3. Restore the `data/app.db` backup made before first v1.0.32 startup if old v12 proxy outcomes/calibrators are needed.
4. Do not restore stale runtime lock databases.

No SQL rollback is required because the schema did not change.

## 17. Recommended next work package

After enough `grid_label_v13` records accumulate, compare neutral proxy commitment and PnL against exact execution evidence:

- active Buy/Sell orders;
- exchange-reported order cost and available balance;
- peak one-way inventory;
- realized fees and funding;
- proxy denominator versus actual committed margin;
- symbol/regime-specific out-of-sample return.

Only after this reconciliation should launch thresholds or claims about strategy viability be changed.
