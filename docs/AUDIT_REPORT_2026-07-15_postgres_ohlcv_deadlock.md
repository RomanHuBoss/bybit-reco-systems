# Audit iteration 248 - PostgreSQL OHLCV deadlock and collector transaction ordering

## 1. Result

The reported PostgreSQL error was independently confirmed as a real production defect, not harmless log noise. The application already had deadlock classification and rollback/retry code, but the actual hot collector and backfill OHLCV paths bypassed it by calling `db.upsert_ohlcv(..., commit=False)` and committing later. Backfill bootstrap also converted nondeterministic network completion order into database lock order. The hot 1-minute loop additionally rewrote 4-hour derived rows even when its 1-hour source had not changed, creating unnecessary overlap with the backfill loop.

The fix is a patch release, v1.0.60. It creates canonical, retry-capable OHLCV transaction boundaries and removes unrelated hot-path 4-hour rewrites.

## 2. Input

- ZIP: `bybit-reco-systems-main(2).zip`
- Input SHA-256: `14c084e72710c04e8a242752fb9097fe42227e8f68132bb5a0f185a552db8b52`
- Project root: `bybit-reco-systems-main`
- Original FastAPI version: `1.0.59`
- New FastAPI version: `1.0.60`
- Highest previous regression iteration: 247
- Current regression iteration: 248

Archive safety checks passed: 311 entries, one root, no absolute/traversal paths, external symlinks, duplicate paths or nested archives.

## 3. Project fingerprint

Matched the expected Bybit Recommender project:

- recommendation/audit service, not OMS/EMS;
- Bybit `category=linear`, USDT perpetual, `futures_grid` scope;
- FastAPI in `app/main.py`;
- canonical directional semantics in `app/trading_semantics.py`;
- dual SQLite/PostgreSQL persistence;
- static frontend in `app/ui/static/`;
- no production private order create/amend/cancel endpoints found.

## 4. Goal and acceptance criteria

After this iteration, overlapping hot/backfill OHLCV writes must use the same deterministic primary-key order and a transaction boundary that can roll back and replay a PostgreSQL deadlock victim.

Acceptance criteria:

1. No collector/backfill OHLCV call site uses `commit=False`.
2. Bootstrap rows are persisted as one canonical batch, independent of `as_completed()` order.
3. Derived rows are persisted as one canonical batch per target timeframe.
4. A localized deadlock on the first OHLCV statement is rolled back and retried successfully in both hot and backfill paths.
5. The normal 1-minute hot path does not rewrite 4-hour rows when 1-hour data was not touched.
6. Existing collector, backfill, PostgreSQL translation and DB retry regressions remain green.
7. SQLite/PostgreSQL schema, API, env, recommendation lifecycle and trading math remain unchanged.

## 5. Sources reviewed

- user-provided iteration prompt and reported log;
- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- latest audit reports for policy-conditioned calibration, outcome-scope readiness, lineage reset, bounded censoring and stale calibrator behavior;
- `app/collector.py`, relevant `app/main.py`, `app/db.py`, `app/db_backend.py`, migrations and collector/DB regression tests;
- relevant risk/grid/outcome/recommender contracts to ensure the patch did not change trading semantics.

## 6. Affected data flow

`collector/backfill network tasks` -> unordered task completion -> sanitized rows -> aggregate in memory -> canonical OHLCV dedupe/order -> retry-capable DB transaction -> derived-source touch map -> only affected derived TFs -> supervisor diagnostics.

The database key remains `(venue, symbol, tf_sec, ts)`.

## 7. Baseline environment and inventory

- Python: 3.13.5
- Node: v22.16.0
- Production Python files: 24
- Test files before iteration: 192
- Docs before new report: 70
- Frontend files: 3
- Migration SQL files: 2
- API routes: 24, including 7 mutating routes
- Explicit background loops: collector, backfill, futures metadata, sentiment, recommender and optional LLM reviewer
- DB engines: SQLite and PostgreSQL compatibility layer

## 8. Baseline commands and exact results

| Check | Result |
|---|---|
| `python --version` | PASSED: Python 3.13.5 |
| `node --version` | PASSED: v22.16.0 |
| `python -m pip check` | FAILED due to external environment conflict: MoviePy 2.2.1 requires Pillow `<12`; environment has Pillow 12.2.0 |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 1117 tests collected |
| monolithic `python -m pytest -q` | TIMED OUT after reaching 58%, no failure summary; not counted as pass |
| exhaustive non-overlapping interleaved batches | 160 + 160 + 160 + 160 + 159 + 159 + 159 = 1117; all 1117 passed |

No production-like `.env`, Bybit credentials, network smoke test or live database was used.

## 9. Confirmed defects

### PG-OHLCV-248-01 - HIGH - CONFIRMED DEFECT

- Files: `app/db.py`, `app/collector.py`
- Runtime path: `collect_once()` / `collect_backfill_once()` -> `db.upsert_ohlcv(commit=False)` -> later caller `conn.commit()`
- Actual behavior: `_commit_write_with_retry()` was used only for `commit=True`; a PostgreSQL deadlock during `executemany()` escaped immediately, leaving the transaction aborted until the outer thread rolled it back and logged `COLLECT_ERROR`.
- Expected behavior: rollback the deadlock-victim transaction and replay the same canonical batch.
- Why prior tests missed it: iteration 142 tested `db.upsert_ohlcv(commit=True)` directly, not the production collector call sites, all of which used `commit=False`.
- Impact: collection/backfill liveness failure and stale/missing market-data windows. Because recommendation gates are fail-closed, the direct expected effect is blocked/no-trade rather than silent unsafe execution; persistent failures can nevertheless invalidate model readiness and operator confidence.

### PG-OHLCV-248-02 - HIGH - CONFIRMED DEFECT

- File: `app/collector.py`
- Runtime path: derived bootstrap tasks returned through `as_completed()` and were written one result at a time inside one transaction.
- Actual behavior: network completion order became PostgreSQL unique-index lock order. A concurrent worker could acquire overlapping primary-key tuples in a different symbol/timeframe order and form a cycle.
- Expected behavior: aggregate the complete transaction first, then deduplicate/sort once by the canonical key.
- Impact: repeatable deadlock risk under cold-start/full-sweep warmup, especially with multiple symbols and workers.

### PG-OHLCV-248-03 - MEDIUM - CONFIRMED DEFECT

- File: `app/collector.py`
- Runtime path: normal hot collector configured for 1m API fetch still iterated all derived mappings, including 4h from 1h.
- Actual behavior: it re-read and rewrote 4h rows each hot cycle despite not touching 1h; backfill independently owned 1h/4h maintenance.
- Expected behavior: derive only target timeframes whose source timeframe changed in the current cycle.
- Impact: avoidable database load, larger lock graphs and increased deadlock probability. No mathematical candle formula change was required.

## 10. RED -> GREEN evidence

New test: `tests/test_iteration248_postgres_ohlcv_transaction_order.py`.

RED command on pristine production code with only the new test added:

```bash
python -m pytest -q tests/test_iteration248_postgres_ohlcv_transaction_order.py
```

RED result: `2 failed in 1.12s`.

Essential failure:

```text
app/collector.py:1071: db.upsert_ohlcv(conn, derived_rows, commit=False)
sqlite3.OperationalError: обнаружена взаимоблокировка
```

The backfill test failed analogously at the per-task bootstrap `commit=False` call.

GREEN command:

```bash
python -m pytest -q tests/test_iteration248_postgres_ohlcv_transaction_order.py
```

GREEN result: `2 passed in 0.67s`.

The tests independently calculate expected canonical key order, inject a first-statement deadlock victim, require one rollback, and verify retry success. They do not use production output as an oracle.

## 11. Implementation

### Production

- `app/collector.py`
  - API OHLCV batches now commit through `db.upsert_ohlcv(..., commit=True)`.
  - Bootstrap results are accumulated before one canonical committed upsert.
  - Derived rows are accumulated per target timeframe before one canonical committed upsert.
  - Diagnostic log writes use a separate transaction.
  - `touched_by_source_tf` restricts derivation to source series modified during the cycle.
  - Normal hot 1m wiring derives 15m/30m but no longer rewrites 4h.
- `app/main.py`
  - FastAPI version advanced to 1.0.60.

### Tests

- Added `tests/test_iteration248_postgres_ohlcv_transaction_order.py`.
- Synchronized existing static release-version assertions to 1.0.60.

### Documentation

- Updated README, CHANGELOG, KNOWN_RISKS, ARCHITECTURE, MODULES and SCENARIOS.
- Added this audit report.

## 12. Relevant regression result

The collector/backfill/DB subset, including iteration 142 deadlock hardening, iterations 63/65/76/79/80/83/86 and `tests/test_logic.py`, passed: `117 passed in 6.25s` before the version/doc synchronization.

## 13. Post-check commands and exact results

| Check | Result |
|---|---|
| `python -m pytest --collect-only -q` | 1119 tests collected |
| monolithic `python -m pytest -q` | TIMED OUT after reaching 70%, no failure summary; not counted as pass |
| exhaustive non-overlapping interleaved batches | 160 + 160 + 160 + 160 + 160 + 160 + 159 = 1119; all 1119 passed |
| iteration 248 targeted, run 1 | 2 passed in 0.76s |
| iteration 248 targeted, run 2 | 2 passed in 0.75s |
| PostgreSQL offline translation/locking/deadlock subset | 20 passed in 1.09s |
| collector/backfill/DB relevant subset | 117 passed in 6.25s before version-only synchronization; covered again by final 1119-node run |
| fresh SQLite init | PASSED: 20 tables |
| repeated SQLite init | PASSED: 20 tables; OHLCV PK remains `venue,symbol,tf_sec,ts` |
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| collector `upsert_ohlcv(..., commit=False)` scan | PASSED: none found |
| production private order endpoint scan | PASSED: none found |
| FastAPI version consistency | PASSED: 1.0.60 |
| `python -m pip check` | FAILED only on the pre-existing environment-level MoviePy/Pillow conflict |
| Ruff | UNAVAILABLE: module not installed |

## 14. Database and compatibility

- No schema change.
- No migration action required.
- `migrations/init.sql` and `migrations/init_postgres.sql` unchanged.
- Existing SQLite and PostgreSQL databases remain compatible.
- Transaction granularity changed: OHLCV persistence is committed independently from noncritical decision-log rows. This is deliberate to keep the OHLCV retry graph bounded and preserve market data if a later diagnostic write fails.

## 15. API, configuration and security boundaries

- API fields/routes/status semantics unchanged.
- Environment variables unchanged.
- Recommendation/risk/grid/outcome/calibration math unchanged.
- No private Bybit order endpoint or auto-execution capability added.
- No secrets or runtime DB files are included in the release.

## 16. Economic viability conclusion

The source archive cannot prove either profitability or structural unprofitability. It contains no runtime database, frozen current-policy cohort, real reconciled fills or complete observed fee/spread/slippage/funding evidence. The codebase is best classified as an **unvalidated research/recommendation and audit system**, not a proven trading strategy and not an OMS.

The reported month-long lack of stable results is compatible with two independent factors visible in the project: repeated policy/model lineage changes reset usable evidence, and fail-closed gates can keep the service in shadow/no-trade. The deadlock was an additional genuine liveness defect, but fixing it does not establish an economic edge.

A go/no-go economic decision requires a frozen-policy shadow interval and export of the actual runtime DB with current-lineage outcome roots, censoring reasons, temporal clusters, net returns after all costs, purged OOF/terminal holdout metrics and comparison against simple baselines.

## 17. Unverified items and residual risks

- Live two-session PostgreSQL integration: SKIPPED because no explicitly disposable test DSN was supplied.
- Real Bybit network behavior: SKIPPED; offline mocks/fixtures only.
- Monolithic full-suite process: TIMED OUT at 58%; exhaustive non-overlapping batches were used instead.
- Ruff: UNAVAILABLE in the current environment.
- Profitability/live edge: NOT ESTABLISHED.
- A real PostgreSQL system can still deadlock for unrelated tables or future write paths; SQLSTATE monitoring remains required.

## 18. Rollback

1. Stop the application.
2. Restore the prior v1.0.59 application files/ZIP.
3. Restart with the existing database; no DB rollback or migration is required.
4. Expect the original OHLCV deadlock risk to return under overlapping hot/backfill activity.

## 19. Recommended next work package

Run a controlled disposable PostgreSQL concurrency test with two independent connections executing realistic hot/backfill batches, collect `pg_locks`/deadlock diagnostics, and then audit the actual runtime database for collection gaps and current-policy economic evidence. This should be done before any claim of production readiness or profitability.
