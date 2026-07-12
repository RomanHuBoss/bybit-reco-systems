# Audit iteration: grid cost-layer separation

## 1. Release identity

- Input ZIP: `bybit-reco-systems-1.0.35-bybit-cross-margin-safety.zip`
- Input SHA-256: `7565b2878569711c7928d0e36681344d2e47592878bb254d4b4445ceff7612ae`
- Source version: `1.0.35`
- Source outcome contract: `grid_label_v16`
- New version: `1.0.36`
- New outcome contract: `grid_label_v17`
- Scope: separate recurring grid-fill fees from one-time market friction and inventory-time funding.

## 2. Project fingerprint

The archive matches Bybit Recommender:

- `README.md`, `CHANGELOG.md`, `requirements*.txt`, `main.py`;
- `app/main.py`, `app/recommender.py`, `app/grid_math.py`, `app/outcomes.py`, `app/trading_semantics.py`;
- SQLite/PostgreSQL persistence and both reference migration SQL files;
- FastAPI recommendation/audit service;
- `futures_grid`, Bybit Linear USDT Perpetual scope;
- frontend under `app/ui/static/`;
- no private order-create/amend/cancel implementation.

No project-fingerprint mismatch was found.

## 3. Goal and acceptance criteria

After this iteration, a completed grid pair must be charged only for the two recurring grid-fill fee legs. Bid/ask spread and slippage must remain one-time market setup/terminal-exit friction, while funding must remain signed inventory-time Total P&L.

Acceptance criteria:

1. Cost model exposes recurring grid fee separately from market friction.
2. Grid Profit per completed pair does not allocate horizon funding.
3. Grid spacing and density do not multiply horizon funding or market friction by grid count/cycle count.
4. Neutral completed-pair outcome does not pay spread/slippage on resting fills.
5. Directional initial market inventory and resting TP use different cost layers.
6. Live spread remains a liquidity gate while live per-grid edge uses recurring fee.
7. Outcome target is bumped so incompatible old proxy labels are not mixed with corrected labels.

## 4. Sources and independent economic invariant

Primary implementation sources:

- `app/recommender.py`
- `app/grid_math.py`
- `app/outcomes.py`
- `app/main.py`
- relevant tests, README and trading documentation.

External primary references:

- Bybit, *P&L Calculations (Futures Grid Bot)*: Grid Profit is the price interval multiplied by quantity and completed trades, less trading fees; Total P&L additionally includes unrealized P&L and funding.
- Bybit, *Funding Fee Calculation*: funding is calculated from position value and funding rate at funding events.

Independent invariant:

```text
completed grid pair PnL
= adjacent interval PnL
- fee on resting opening fill
- fee on resting closing fill
```

The following must not be repeated once per completed pair:

```text
bid/ask spread
market-entry slippage
terminal-exit slippage
full-horizon funding
```

Those costs depend on market setup/exit or position-time inventory, not on the number of completed grid pairs.

## 5. Baseline environment and inventory

- Python: `3.13.5`
- Node: `22.16.0`
- Production Python files: 23
- Baseline test files: 167
- Baseline tests collected: 985
- Frontend files: 3
- Migration SQL files: 2
- Persistence: SQLite and PostgreSQL compatibility layer

The input ZIP contained an empty `data/` directory, not the user's live runtime database. Local `app.db` and runtime-lock files observed during testing were generated in working copies and are excluded from the release.

## 6. Baseline checks

| Check | Result |
|---|---|
| ZIP traversal/absolute path/symlink/duplicate check | PASSED |
| Project fingerprint | PASSED |
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| Baseline exhaustive pytest | 985/985 PASSED |
| `python -m pip check` | FAILED: external MoviePy/Pillow environment conflict |
| `python -m ruff check .` | UNAVAILABLE: ruff not installed |

The monolithic harness may stop before printing a final summary. Baseline was therefore proven through deterministic non-overlapping file batches whose union matched the collected node set.

## 7. Confirmed defect

### COST-224: recurring and non-recurring cost layers were conflated

- Severity: **HIGH**
- Type: **CONFIRMED DEFECT**
- Files: `app/recommender.py`, `app/grid_math.py`, `app/outcomes.py`, `app/main.py`
- Affected flow: publication cost model -> grid spacing/density -> per-grid economics -> live execution edge -> outcome ledger -> win-rate/calibration.

### Actual behavior

The source cost model formed one value:

```text
execution_cost_bps
= round-trip fees + spread + slippage
```

Then the system subtracted that value plus the full expected horizon funding from every completed grid pair. The same combined cost widened grid spacing, reduced grid density and was reused in live per-grid edge checks.

### Expected behavior

- recurring pair cost: two resting-fill fees;
- one-time market friction: setup and terminal liquidation;
- funding: signed cash flow from actual inventory at funding-event times.

### Financial impact

The error grows with completed cycle count. Example with five completed pairs:

```text
gross interval per pair       40 bps
recurring fee per pair        10 bps
one-time spread/slippage      20 bps
horizon adverse funding       40 bps

Correct total stress:
5*40 - 5*10 - 20 - 40 = +90 bps

Old repeated-cost model:
5*(40 - 30 - 40) = -150 bps
```

The exact magnitude depends on inventory and market entry/exit, but the old model can flip the sign of an active grid and systematically depress win rate/calibration.

### Trading/risk impact

- false `GRID_NET_PROFIT_*` blocks;
- unnecessarily wide grids and lower cycle opportunities;
- false live-edge blocks after spread refresh;
- pessimistically biased proxy outcomes.

This defect does not prove that the strategy is profitable. It proves that old proxy statistics did not cleanly measure the strategy's economics.

### Why existing tests missed it

Historical tests used the implementation's combined `execution_cost_bps + expected_funding_bps` as their oracle. They proved internal consistency, not the independent Bybit P&L identity. Several old expectations explicitly required funding to widen every grid interval and reduce density.

## 8. RED evidence

Command on pristine source plus only the new test:

```bash
python -m pytest -q tests/test_iteration224_grid_cost_layer_separation.py --tb=short
```

Result:

```text
7 failed in 1.02s
```

Material RED lines:

```text
KeyError: 'grid_round_trip_fee_bps'
grid_spacing_cost_floor_bps: 64.5 instead of 11.0
neutral return: 0.0034925 instead of 0.0044975
directional return: 0.00351005 instead of 0.00401759
LIVE_EXECUTION_EDGE_NON_POSITIVE present unexpectedly
```

## 9. Fix

### `app/recommender.py`

Added explicit fields:

- `grid_round_trip_fee_bps`
- `one_time_market_friction_bps`
- `market_round_trip_cost_bps`

The legacy `execution_cost_bps` remains a conservative market round-trip alias for compatibility. Grid spacing, density and per-pair economics use only recurring grid fees. Funding remains an approval/Total-P&L diagnostic and guard, not a per-pair charge.

### `app/grid_math.py`

`grid_leg_economics` now publishes:

- Grid Profit after recurring two-fill fees;
- separate adverse-funding Total-P&L stress;
- `funding_allocated_to_grid_leg=false`.

### `app/outcomes.py`

The ledger now uses:

- market half-leg rate for initial directional market inventory;
- grid fee half-leg rate for resting level fills;
- market half-leg rate for terminal residual liquidation;
- existing inventory/event funding calculation.

Legacy payloads lacking explicit fee fields retain a conservative market-cost fallback instead of silently becoming free.

### `app/main.py`

Live spread remains an independent liquidity gate. Live per-grid edge and gross-cost coverage use the recurring fee floor. Funding remains owned by the dedicated fresh-schedule/inventory guard.

## 10. GREEN evidence

Command:

```bash
python -m pytest -q tests/test_iteration224_grid_cost_layer_separation.py
```

Result:

```text
7 passed
```

Related economics/funding/outcome suite:

```text
40 passed
```

Full post-check collection:

```text
992 tests collected
```

Exhaustive non-overlapping batches:

```text
249 + 248 + 248 + 247 = 992
992/992 passed
```

## 11. Changed files

### Production

- `app/recommender.py`
- `app/grid_math.py`
- `app/outcomes.py`
- `app/main.py`

### Tests

- new `tests/test_iteration224_grid_cost_layer_separation.py`
- corrected historical oracles that multiplied funding/market friction into each pair
- synchronized current version/outcome-contract assertions

### Documentation/operator artifacts

- `README.md`
- `CHANGELOG.md`
- `docs/TRADING_LOGIC.md`
- `docs/KNOWN_RISKS.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- operator DOCX/PDF
- `how_to_trade.png`

## 12. Database/schema compatibility

- No schema change.
- No migration SQL change.
- SQLite and PostgreSQL support retained.
- Existing recommendations/trades/exact execution evidence remain intact.
- On first v1.0.36 start, `grid_label_v17` version guard removes incompatible proxy outcomes/calibrators only.

## 13. API/config compatibility

- No API route removed or renamed.
- Existing cost aliases remain available.
- New cost fields are additive.
- No `.env` action required.
- No private Bybit order endpoint added.

## 14. Operator-document verification

- DOCX rendered to five PNG pages and visually reviewed page by page.
- PDF regenerated from the verified DOCX, rendered to five PNG pages and reviewed.
- Infographic updated to v1.0.36 / `grid_label_v17`.
- No clipped, overlapping or blank pages observed.

## 15. What could not be verified

- The input release did not contain the user's live `data/app.db`; the displayed monthly statistics could not be recalculated record by record.
- No live PostgreSQL integration was run without a verified disposable DSN.
- Exact maker/taker mix, queue priority, partial fills and real fee tier remain unavailable to OHLCV proxy outcomes.
- The strategy's live edge is not proven by this correction.

## 16. Residual risks

1. One-time market-friction allocation remains a conservative proxy when exact initial/terminal fills are unavailable.
2. Funding depends on actual inventory and schedule; proxy reconstruction remains less reliable than exact transaction-log evidence.
3. A strategy can remain unprofitable after mathematically correct cost allocation because gross mean-reversion capture may be insufficient.
4. Conclusions should be based on new `grid_label_v17` outcomes and exact execution evidence, not mixed historical labels.

## 17. Rollback

1. Stop the application.
2. Restore code version `1.0.35`.
3. Restore the `data/app.db` backup taken before first v1.0.36 start.
4. Do not reuse the runtime-lock database.

## 18. Recommended next work package

Ingest a copy of the actual runtime database and decompose exact and proxy PnL by:

- completed grid count;
- gross interval capture;
- recurring fill fees;
- one-time market friction;
- inventory-time funding;
- symbol, direction and regime;
- actionable versus shadow cohort;
- chronological out-of-sample period.

Only that decomposition can distinguish remaining implementation error from absence of strategy edge.
