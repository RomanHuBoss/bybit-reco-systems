# Audit iteration: dynamic off-grid bridge topology

## 1. Release identity

- Input ZIP: `bybit-reco-systems-1.0.32-neutral-one-way-commitment.zip`
- Input SHA-256: `1ad59bd7c76de5e208f33463f16966384bc91e85fff07d640005bd9eafc99d74`
- Source version: `1.0.32`
- New version: `1.0.33`
- Source outcome target: `grid_label_v13`
- New outcome target: `grid_label_v14`
- Planned release: `bybit-reco-systems-1.0.33-dynamic-bridge-topology.zip`

## 2. Project fingerprint

Fingerprint matched Bybit Recommender: FastAPI application in `app/main.py`; `futures_grid`; Bybit Linear USDT Perpetual; recommendation/audit-only boundary; SQLite and PostgreSQL persistence; frontend in `app/ui/static`; canonical direction helpers in `app/trading_semantics.py`; no private order placement lifecycle.

Archive safety checks found one root directory and no absolute paths, traversal, conflicting duplicate paths, external symlinks or suspicious nested archives.

## 3. Goal and acceptance criteria

After this iteration, an arithmetic grid with `N` intervals must expose `N+1` prices but exactly `N` initial resting orders. One pivot/bridge price is idle, both on-grid and between-level references. A replacement order may occupy the bridge only after a neighbouring fill.

Acceptance criteria:

1. Official N=5 example topology produces five initial orders, not six.
2. NEUTRAL/LONG off-grid entry leaves the nearest upper bridge idle.
3. SHORT off-grid entry leaves the nearest lower bridge idle.
4. Directional initial inventory excludes the nonexistent bridge-side close lot.
5. Immediate price movement to the idle bridge produces no fill or PnL.
6. An adjacent fill may create a replacement order on the bridge and complete a later pair.
7. Recommender, auto-snap, preflight, runtime risk, daily-loss guard and outcome normalization use the same topology.
8. New RED regression tests fail on 1.0.32 and pass on 1.0.33; all existing tests remain green after correcting obsolete oracles.

## 4. Sources read

- Current ZIP source code and schemas.
- README, CHANGELOG, `.env.example`, requirements files.
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, operator quick reference and recent audit reports.
- `app/grid_math.py`, `recommender.py`, `main.py`, `outcomes.py`, `risk.py`, `trading_semantics.py`, DB/backend modules and related regression tests.
- Official Bybit Futures Grid examples for Long, Short and Neutral dynamic order placement and official one-way order-cost semantics.

## 5. Affected data flow

`range/reference/grid_count/direction`
→ `arithmetic_grid_commitment`
→ generated sizing/economics
→ Bybit metadata auto-snap
→ strict execution preflight
→ runtime notional/margin and daily-loss limits
→ persisted trade plan
→ initial outcome orders/inventory
→ fees/funding/PnL
→ proxy return and calibration target.

## 6. Baseline environment and inventory

- Python: `3.13.5`
- Node: `22.16.0`
- Production Python files: 24 including root `main.py`
- Test files: 165 before the new test
- Frontend files: 3
- Migration SQL files: 2
- Persistence: SQLite and PostgreSQL compatibility layer
- `pip check`: pre-existing environment conflict, MoviePy 2.2.1 requires Pillow <12 while Pillow 12.2.0 is installed.
- `ruff`: unavailable in the environment.

## 7. Baseline commands and results

- `python -m compileall -q app tests main.py` — PASSED.
- `node --check app/ui/static/app.js` — PASSED.
- `python -m pytest -q` — 961 collected, 961 passed in 27.77 s.
- `python -m pip check` — FAILED only for the unrelated MoviePy/Pillow environment conflict above.

## 8. Confirmed defect DBT-221-01

- Severity: **critical**
- Type: **CONFIRMED DEFECT**
- Primary file: `app/grid_math.py`
- Function: `arithmetic_grid_commitment`
- Violated invariants: grid geometry, sizing, outcome fidelity, risk consistency.

### Input

Arithmetic range 10,000–30,000; `grid_count=5`; reference 20,000. Prices are 10,000, 14,000, 18,000, 22,000, 26,000, 30,000.

### Actual behavior in 1.0.32

For a between-level reference the helper placed orders on all six prices. It therefore reported six active orders and also created an excess directional initial-position lot for LONG/SHORT.

### Expected behavior

Dynamic Futures Grid has five initial orders. One adjacent bridge level is empty:

- NEUTRAL/LONG: buys 10k/14k/18k, no initial order at 22k, sells 26k/30k.
- SHORT: buys 10k/14k, no initial order at 18k, sells 22k/26k/30k.

The bridge may become populated only after the adjacent fill creates a replacement order.

### Financial and risk impact

For the neutral official-size example, the old one-way committed price sum was `max(42k, 78k)=78k`; the corrected value is `max(42k, 56k)=56k`. Required capital/margin was overstated by 39.3%, while the same PnL return was understated by 28.2% relative to the old denominator.

For LONG, commitment fell from 102k to 82k per unit quantity; for SHORT, from 138k to 118k. More importantly, the old outcome ledger could record a fill at the bridge before an order existed, creating phantom trades, fees and funding-relevant inventory.

### Why prior tests missed it

Previous iteration tests treated “N intervals create N+1 prices” as equivalent to “N+1 initial orders”. Their oracle duplicated the defect. The tests accurately enforced internal parity but not the exchange’s dynamic order-placement sequence.

## 9. RED evidence

New regression file: `tests/test_iteration221_off_grid_bridge_topology.py`.

Command on the unchanged 1.0.32 red copy:

```text
python -m pytest -q tests/test_iteration221_off_grid_bridge_topology.py
```

Result:

```text
8 failed in 0.52s
```

Representative failures:

- NEUTRAL sell indices `[3,4,5]` instead of `[4,5]`.
- LONG initial slots `3` instead of `2`.
- SHORT buy indices `[0,1,2]` instead of `[0,1]`.
- Moving only to the idle bridge produced nonzero PnL instead of no fill.
- Dynamic neutral cycle used return denominator 78,000 instead of 56,000.

## 10. Fix

- `app/grid_math.py`: between-level NEUTRAL/LONG excludes `cell+1`; SHORT excludes `cell`; added `idle_grid_index`; initial directional inventory derives from actual close-side order count.
- `app/recommender.py`: defensive fallback and commitment diagnostics use N initial orders.
- `app/main.py`: auto-snap fallback and strict preflight messages use N-order dynamic topology; version/target bumped.
- `app/outcomes.py`: documentation and normalization contract explicitly use one idle bridge.
- Existing tests with N+1-order oracles were minimally corrected using independent monetary values and dynamic paths.

## 11. GREEN evidence

```text
python -m pytest -q tests/test_iteration221_off_grid_bridge_topology.py
8 passed in 0.39s
```

Related regression package:

```text
123 passed in 3.06s
```

Full post-check before documentation synchronization:

```text
969 collected
969 passed in 26.70s
```

Final exhaustive post-check after documentation synchronization used four non-overlapping file batches whose union equals all 969 collected nodes:

```text
228 + 273 + 204 + 264 = 969
969/969 passed
```

## 12. Production diff

### Production

- `app/grid_math.py`
- `app/recommender.py`
- `app/main.py`
- `app/outcomes.py` (contract comment only)

### Tests

- New `tests/test_iteration221_off_grid_bridge_topology.py`
- Corrected obsolete N+1 initial-order expectations in affected earlier regression fixtures.

### Documentation

- README, CHANGELOG
- `docs/TRADING_LOGIC.md`
- `docs/KNOWN_RISKS.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- operator DOCX/PDF and `how_to_trade.png`

## 13. Database/schema compatibility

No schema change. `migrations/init.sql`, `migrations/init_postgres.sql` and runtime DB bootstrap remain unchanged. Version guard changes only the outcome target and deletes incompatible proxy outcomes/calibrators on first startup. Recommendations, bot instances, trades, exact execution evidence and risk settings are preserved.

## 14. API/config compatibility

No route, JSON field removal, environment-variable or persistence-backend change. `idle_grid_index` is an internal diagnostic returned by the canonical helper; existing public trade-plan fields remain compatible. New generated sizing values may be smaller because the phantom bridge order is removed.

## 15. Security and execution boundary

The project remains recommendation/audit-only. No private Bybit create/amend/cancel order endpoint or SDK equivalent was added. No credentials, `.env` or production database are included in the release.

## 16. Post-check plan/results

Required final checks:

- `pip check` — pre-existing unrelated conflict only.
- compileall — passed.
- JavaScript syntax — passed.
- full pytest — 969/969 passed.
- targeted regression twice — passed.
- SQLite fresh bootstrap — 17 project tables; repeated bootstrap preserved all tables and a control row.
- PostgreSQL translation/dialect/locking/deadlock tests — 24/24 passed.
- no private order endpoints/secrets/runtime DB — passed.
- DOCX/PDF render and all-page visual inspection — passed.
- final ZIP integrity, one root, clean re-extraction and test run — passed.

## 17. Limitations and residual risks

- OHLCV cannot reveal queue priority, partial fills, actual maker/taker status or multiple same-minute oscillations.
- The exact exchange order layout may depend on bot creation price rounding and private account state; external execution must revalidate live values.
- The fix corrects deterministic initial topology but does not prove strategy profitability.
- Historical targets before `grid_label_v14` must not be mixed with the new sample.

## 18. Operator action

1. Stop the application.
2. Back up the current `data/app.db`.
3. Replace code with v1.0.33 while retaining the user database.
4. Do not copy an old runtime-lock database.
5. Start the application; allow the target guard to clear only incompatible proxy outcomes/calibrators.
6. Evaluate only newly accumulated `grid_label_v14` outcomes and compare them with exact execution evidence.

## 19. Rollback

Stop v1.0.33, restore v1.0.32 code and restore the database backup created before first v1.0.33 startup if the previous proxy outcome/calibrator set is required.

## 20. Recommended next work package

Validate generated order topology against captured read-only exchange bot/order snapshots from a disposable test account, then compare exact initial orders, replacement transitions and committed margin against `arithmetic_grid_commitment`. Do not add auto-execution.
