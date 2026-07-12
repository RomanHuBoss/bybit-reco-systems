# Audit iteration: neutral opening-order margin reservation

## 1. Input and release identity

- Input ZIP: `bybit-reco-systems-1.0.33-dynamic-bridge-topology.zip`
- Input SHA-256: `e1fd89fa3e4988599462dc93fc203239d138f2820bf1ed6ceebe540399cb7481`
- Source version: `1.0.33`
- Source outcome contract: `grid_label_v14`
- New version: `1.0.34`
- New outcome contract: `grid_label_v15`
- Final release ZIP: `bybit-reco-systems-1.0.34-neutral-opening-margin-reservation.zip`
- Regression iteration: `222`
- Scope: NEUTRAL initial-order commitment, sizing, margin, preflight and outcome-return denominator.

## 2. Project fingerprint

The archive matches Bybit Recommender:

- FastAPI application in `app/main.py`;
- supported bot type `futures_grid`;
- Bybit Linear USDT perpetual scope;
- recommendation/audit-only boundary, no private order placement;
- SQLite and PostgreSQL persistence;
- canonical directional/grid helpers in `app/trading_semantics.py` and `app/grid_math.py`;
- frontend in `app/ui/static/`;
- required operator DOCX/PDF/PNG artifacts present.

Archive safety checks found no absolute paths, traversal, symlinks or duplicate/conflicting entries.

## 3. Goal and acceptance criteria

After this iteration the system must reserve deterministic NEUTRAL commitment for every initial Buy and Sell opening order, while keeping maximum one-way position exposure separate.

Acceptance criteria:

1. Exact-level NEUTRAL 99/100/101 commits both initial orders (`99 + 101`) but reports one maximum position slot.
2. The official-style N=5 example commits all five initial orders and keeps the idle bridge absent.
3. LONG and SHORT commitment remains unchanged.
4. Recommender sizing and Bybit metadata snap publish identical commitment fields.
5. Strict preflight rejects the legacy max-side payload.
6. Outcome percentage return is normalized by full initial NEUTRAL commitment.
7. Version/label are bumped to `1.0.34` / `grid_label_v15`.
8. Full test suite and clean re-extracted release pass.

## 4. Sources read

Project sources:

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/TRADING_LOGIC.md`, `KNOWN_RISKS.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`;
- the latest audit reports, especially v1.0.32 and v1.0.33;
- `app/grid_math.py`, `app/recommender.py`, `app/main.py`, `app/outcomes.py`, `app/risk.py`;
- relevant sizing, preflight, outcome and version regression tests.

External primary sources:

- Bybit Help Center, *Introduction to Futures Grid Bot on Bybit*;
- Bybit Help Center, *P&L Calculations (Futures Grid Bot)*;
- Bybit Help Center, *FAQ - Futures Grid Bot*.

The decisive external invariant is that a NEUTRAL bot starts flat and all initial Buy and Sell orders require margin for execution. One-way mode limits simultaneous net position; it does not make the opposite initial opening stack free.

## 5. Baseline environment and inventory

- Python: `3.13.5`
- Node: `22.16.0`
- Production Python files: `23`
- Test files: `165` before iteration222, `166` after it
- Frontend files: `3`
- Migration SQL files: `2`
- API routes: `22`, including `6` mutating routes
- Database backends: SQLite and PostgreSQL compatibility layer

Baseline commands:

```text
python -m compileall -q app tests main.py     PASSED
node --check app/ui/static/app.js             PASSED
python -m pytest -q                           969 passed in 27.23s
python -m pip check                           FAILED (external MoviePy/Pillow conflict)
```

The `pip check` failure was pre-existing and unrelated: installed MoviePy requires Pillow below 12 while the shared environment has Pillow 12.2.0. Ruff was not installed in the environment.

## 6. Confirmed defect

### NMR-001 - CRITICAL - CONFIRMED DEFECT

**Files / flow**

- `app/grid_math.py::arithmetic_grid_commitment`
- `app/recommender.py::_params`
- `app/main.py::_snap_reco_payload_to_bybit_meta`
- `app/main.py::_validate_trade_plan_against_bybit_meta`
- `app/outcomes.py::_grid_outcome` through the canonical helper

**Input**

NEUTRAL grid, flat initial position, with initial Buy and Sell opening orders on opposite sides of reference.

**Actual v1.0.33 behavior**

```python
committed_price_sum = max(buy_opening_price_sum, sell_opening_price_sum)
committed_slot_count = max(len(buy_indices), len(sell_indices))
```

This treated one initial opening stack as if it required no commitment.

**Expected behavior**

```python
committed_price_sum = buy_opening_price_sum + sell_opening_price_sum
committed_slot_count = len(buy_indices) + len(sell_indices)
max_abs_position_slots = max(len(buy_indices), len(sell_indices))
```

Reservation and maximum net position are different quantities.

**Independent examples**

Exact-level 99/100/101:

```text
Initial orders: Buy 99, Sell 101
v1.0.33 commitment: 101, 1 slot
Correct commitment: 200, 2 opening-order slots
Maximum one-way position: 1 slot
```

N=5, levels 10k/14k/18k/22k/26k/30k, reference 20k:

```text
Initial orders: Buy 10k, 14k, 18k; Sell 26k, 30k; bridge 22k idle
v1.0.33 commitment: max(42k, 56k) = 56k
Correct opening-order commitment: 42k + 56k = 98k
Maximum one-way position: 3 slots
```

The old formula understated the deterministic initial order-notional floor by 42.9% in this example and overstated percentage return by a factor of 98/56 = 1.75.

**Financial / risk impact**

- qty and required margin could be sized against insufficient capital;
- strict preflight accepted stale max-side commitment fields;
- runtime notional/margin caps evaluated a smaller commitment than the published initial order set;
- NEUTRAL proxy-return denominator was too small, inflating percentage returns;
- the defect is safety-negative even though it makes historical percentage performance look better, not worse.

**Why previous tests missed it**

Iteration220 introduced tests asserting max-side reservation. Those tests were internally consistent with the implementation but used the wrong economic oracle. A green suite therefore proved contract consistency, not exchange correctness. This iteration changes those stale expectations explicitly and adds an independent calculation that does not call the production helper as its oracle.

## 7. RED to GREEN evidence

New test:

```text
tests/test_iteration222_neutral_full_opening_commitment.py
```

RED command on pristine v1.0.33 plus only the new test:

```text
python -m pytest -q tests/test_iteration222_neutral_full_opening_commitment.py
```

RED result:

```text
7 failed, 1 passed in 1.07s
committed_slot_count: 1 instead of 2
N=5 committed_slot_count: 3 instead of 5
generated committed slots: 6 instead of 11
neutral return: 0.00990099 instead of 0.005
legacy max-side payload was accepted
version/label remained 1.0.33/grid_label_v14
```

The one passing control verified that directional LONG/SHORT commitment was not part of the defect.

GREEN command:

```text
python -m pytest -q tests/test_iteration222_neutral_full_opening_commitment.py
```

GREEN result:

```text
8 passed
```

## 8. Production changes

### `app/grid_math.py`

- NEUTRAL commitment now sums all actual initial Buy/Sell opening prices.
- `committed_slot_count` now counts all initial neutral opening orders.
- `max_abs_position_slots` remains the larger one-way directional stack.
- Dynamic bridge topology remains unchanged: N intervals, N+1 prices, one idle bridge, N initial orders.

### `app/recommender.py`

- Published model identifier changed to `neutral_all_initial_opening_orders`.
- Existing sizing/economics fields now receive the corrected canonical values.

### `app/main.py`

- Auto-snap preserves the corrected model identifier and commitment.
- Strict preflight compares stored values with the full initial-order topology and rejects legacy max-side payloads.
- Version changed to `1.0.34`.
- Outcome contract changed to `grid_label_v15`.

`app/outcomes.py` did not require a direct formula change because it already consumes `arithmetic_grid_commitment`; its denominator changes through the canonical helper.

## 9. Tests changed

- Added iteration222 with eight independent checks.
- Corrected historical tests that explicitly asserted max-side neutral commitment.
- Retained independent directional controls.
- Updated version/label contract assertions.

No test was weakened to allow malformed payloads. Legacy max-side payload acceptance is now a direct failure expectation.

## 10. Documentation and operator artifacts

Updated:

- `README.md` and `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`, `KNOWN_RISKS.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX and PDF;
- `how_to_trade.png`.

Historical v1.0.32 statements are marked superseded rather than silently deleted. The DOCX and PDF each render to five visually inspected pages without clipping or overlap.

## 11. Database, API and configuration compatibility

- No schema change.
- `migrations/init.sql` and `init_postgres.sql` unchanged.
- No API route or field removal.
- No new environment variable.
- Version guard removes only incompatible proxy outcomes and related calibrators on first v1.0.34 startup.
- Recommendations, bot instances, trades, exact execution evidence and risk settings are preserved.

## 12. Security and execution boundary

- No private Bybit order create/amend/cancel endpoint was added.
- Project remains recommendation/audit-only.
- No production credential, `.env`, runtime database or lock database belongs in the release ZIP.
- Private account state and actual exchange order-cost reservation remain an external executor responsibility.

## 13. Post-check results

Final values are recorded after documentation and release verification:

```text
python -m compileall -q app tests main.py     PASSED
node --check app/ui/static/app.js             PASSED
pytest collection                             977 unique nodes
iteration222, repeated                        8/8 PASSED twice
related commitment/preflight suite            49/49 PASSED
working-copy full pytest                      977/977 PASSED in 26.20s
SQLite fresh/repeated bootstrap               16 project tables; control row preserved
PostgreSQL dialect/locking tests              24/24 PASSED
DOCX render                                   5 pages, visually PASSED
PDF render                                    5 pages, visually PASSED
ZIP integrity and single-root check           PASSED
clean ZIP targeted regression                 8/8 PASSED
clean ZIP exhaustive batches                  245+244+244+244 = 977/977 PASSED
```

The monolithic pytest invocation from the re-extracted ZIP was stopped by the harness after partial progress and had no failure summary. It is not counted as passed. The same 977 unique nodes were therefore partitioned by test file into four non-overlapping deterministic batches; their union equals the collected set and all batches passed. The clean source archive contained no user/runtime database, so an upgrade against the user's actual database was not possible; no schema changed in this iteration.

## 14. Final release checksum

The final ZIP checksum is computed after packaging and reported alongside the downloadable artifact; it cannot be embedded in the ZIP without changing that checksum.

## 15. Unverified and residual risks

- No user production `data/app.db` was supplied, so the displayed month of statistics cannot be recalculated.
- The correction increases NEUTRAL committed capital and lowers percentage return for the same absolute PnL. It does not explain poor win rate by itself and does not manufacture profitability.
- OHLCV outcomes remain proxy labels: queue priority, partial fills, actual maker/taker fees, exact intrabar sequence and private margin offsets are unknown.
- Live PostgreSQL integration was not run without an explicitly disposable test DSN.
- Strategy edge remains unproven. If `grid_label_v15` and exact execution evidence remain persistently negative on an independent chronological sample, the likely conclusion is lack of edge rather than an accounting defect.

## 16. Rollback

1. Stop the application.
2. Restore the v1.0.33 code.
3. Restore the `data/app.db` backup made before the first v1.0.34 startup.
4. Do not restore a stale runtime lock database.

## 17. Recommended next work package

Use the user's actual database and exact execution evidence to perform a frozen, chronological decomposition of:

- recommendation selection rate;
- gross grid capture;
- fees, spread, slippage and funding;
- committed capital and peak inventory;
- proxy versus exact PnL;
- LONG/SHORT/NEUTRAL and symbol/regime cohorts.

That analysis is required to distinguish a corrected but unprofitable strategy from remaining implementation errors.
