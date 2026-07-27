# Audit report — v1.4.13 PostgreSQL market-trade deadlock and restart handover

Date: 2026-07-27  
Input release: `bybit-reco-systems-1.4.12-ws-heartbeat-pg-recovery.zip`  
Output release: `bybit-reco-systems-1.4.13-pg-trade-lock-restart-takeover.zip`  
Work package: serialize market-trade persistence, bound REST fallback transactions, reclaim provably dead local runtime leases, and enforce journal retention while WebSocket is primary.

## Lineage and persisted evidence

This is an operational patch, not a new trading model. It does not change:

- `RECOMMENDER_MODEL_VERSION`;
- feature schema or score calculation;
- grid/trend selection logic or geometry;
- risk policy;
- `OUTCOME_LABEL_VERSION = grid_label_v26`;
- `GRID_INTRABAR_OBSERVATION_VERSION = grid_intrabar_observation_v3`;
- calibration policy fingerprint semantics.

No recommendations, outcomes, calibrators, funding settlements, market trades, coverage spans or decision-log rows are reset. No schema migration is required.

## Input verification

- Input SHA-256: `412a36cd3ad08cbdf5b7cb864ae9e4f528259d3e4edbb769239d3a5816756281`.
- ZIP traversal check: passed.
- ZIP integrity test: passed.
- Exactly one project root: passed.
- Pristine, red and working copies were created before changes.

## Evidence from supplied diagnostics

The supplied diagnostic showed one PostgreSQL `DeadlockDetected` in `market_trade_stream`. PostgreSQL reported a cycle between two transactions while adding a tuple to a `market_trade` index. The adjacent decision log showed `DB_PRUNE`, and startup readiness temporarily reported 0/35 symbols before recovering to 35/35.

The final diagnostic state was healthy: 35 symbols ready, no stale symbols, current-process collector ownership, active WebSocket stream, REST fallback disabled and zero errors in the ten-minute health window. Therefore the warm-up event was transient restart handover, while the deadlock was a real concurrency defect.

## Confirmed defects

### HIGH — overlapping PostgreSQL writers could deadlock on `market_trade`

During a reconnect boundary, REST fallback could begin a universe sweep while WebSocket reconnected. Both paths used independent PostgreSQL transactions and upserted overlapping unique keys. WebSocket batched several messages in one transaction; REST held one transaction across several symbols. Different tuple-lock acquisition order could form the observed cycle.

### MEDIUM — REST fallback held a transaction across network calls

`record_market_trade_poll(..., commit=False)` was called for every symbol, with commit delayed until the collector cycle completed. This increased lock duration and allowed a reconnecting WebSocket writer to overlap for much longer than necessary.

### MEDIUM — abrupt same-host restart waited the full runtime-lock TTL

If Windows terminated the previous process without executing shutdown cleanup, the new process could not distinguish a dead local owner from a live owner and waited the normal collector lease TTL (400 seconds). During that interval candles became stale and recommender warm-up correctly failed closed.

### MEDIUM — trade-journal retention did not run while WebSocket remained healthy

Trade pruning lived inside the REST fallback collector. Since REST fallback is intentionally disabled while WebSocket is active, the configured 72-hour retention was not enforced in the normal steady state.

## Production changes

### `app/db.py`

- Added one PostgreSQL transaction-scoped advisory lock for all `market_trade` / `market_trade_coverage` INSERT, UPSERT and DELETE operations.
- SQLite remains unchanged because its writer serialization already provides the required exclusion.
- Sorted normalized trade rows by `(venue, symbol, trade_id)` before `executemany` so unique-index tuple locks are requested deterministically.
- Applied the shared lock to WebSocket ingestion, REST fallback ingestion and journal pruning.

### `app/collector.py`

- REST fallback now commits each symbol independently.
- A failed symbol is rolled back before its diagnostic is written.
- Journal prune commits independently.

### `app/main.py`

- Added conservative `hostname:pid` parsing and OS-level same-host PID liveness checks.
- At lifespan startup, deletes only exact runtime-lock rows whose same-host owner PID is proven dead.
- Remote owners, malformed owners, permission-denied checks, uncertain liveness and live PIDs remain untouched.
- Added hourly market-trade retention in a separate transaction from ordinary technical-data pruning.
- Bumped application version to `1.4.13`.

### Frontend and documentation

- Updated static cache build to `1.4.13`.
- Updated README, CHANGELOG, ARCHITECTURE, TRADING_LOGIC, MODULES, SCENARIOS and KNOWN_RISKS.

## RED → GREEN evidence

### RED command

```bash
pytest -q tests/test_iteration283_market_trade_deadlock_restart_takeover.py
```

### RED result on unmodified v1.4.12

```text
8 failed in 0.48s
```

Representative failures:

```text
assert calls == ["lock", "lock"]       # actual []
assert commit_flags == [True, True]      # actual [False, False]
AttributeError: _reclaim_dead_local_runtime_locks
AttributeError: _prune_technical_data_once
```

### GREEN command

```bash
pytest -q tests/test_iteration283_market_trade_deadlock_restart_takeover.py
```

### GREEN result

```text
8 passed in 0.47s
```

The tests cover:

1. shared lock entry for REST and WebSocket;
2. per-symbol REST commits;
3. same-host dead-PID reclamation without stealing live/remote leases;
4. reclamation before worker startup;
5. retention while WebSocket is primary;
6. actual PostgreSQL advisory-lock SQL contract;
7. deterministic unique-key ordering;
8. shared lock entry for pruning.

## Baseline

- Baseline collected: 1356 tests.
- Baseline full deterministic union: 1356 passed, 0 failed, 0 skipped, across 20 non-overlapping file batches.
- Targeted baseline relevant to funding/trade stream: 38 passed.

## Post-check

- Post-change collected: 1364 tests.
- Full deterministic union: 1364 passed, 0 failed, 0 skipped, across 20 non-overlapping file batches.
- Funding/trade-stream related suite: 42 passed.
- New regression suite: 8 passed; deterministic repeat passed.
- `python -m compileall -q app tests`: passed.
- `node --check app/ui/static/app.js`: passed.
- Monolithic `pytest -q` was attempted and timed out in the execution harness at approximately 74%; it is not counted as a complete run. The non-overlapping batch union covers all 1364 collected test nodes.
- `ruff`: unavailable (`No module named ruff`).
- `pip check`: existing external environment conflict only — MoviePy 2.2.1 requires Pillow <12 while the host has Pillow 12.2.0.

## Database and configuration actions

- No SQL migration.
- No table recreation.
- No data deletion beyond the already configured retention policy.
- No `.env` changes required.
- A database backup before deployment remains recommended.

## What was not verified

- No disposable live PostgreSQL DSN was available for a true two-session deadlock stress test.
- No long-duration Windows/PostgreSQL/Bybit WebSocket soak test was performed.
- Cross-host stale-owner detection remains intentionally TTL-based.

## Residual risks

- The advisory lock serializes public-trade writer transactions. Under severe PostgreSQL latency, writers can queue; transactions are deliberately bounded to limit this delay.
- PID reuse is handled conservatively: if another live local process has inherited the same PID, the stale lease is preserved until TTL.
- An older pre-1.4.13 process does not participate in the advisory-lock contract. Operators must fully stop the old Python process before starting the new release.

## Rollback

1. Stop v1.4.13 and verify that its Python process has exited.
2. Restore v1.4.12 application files.
3. Restart against the same database.

No database rollback is required because schema and persisted contracts are unchanged.

## Commit message

```text
fix(market-data): serialize PostgreSQL trade ingestion and reclaim dead local leases

- guard WebSocket, REST fallback and retention with one xact advisory lock
- commit REST trade snapshots per symbol and sort unique-key upserts
- reclaim only provably dead same-host runtime owners before worker startup
- enforce trade-journal retention while WebSocket remains primary
- preserve model, outcome and calibration lineage
```
