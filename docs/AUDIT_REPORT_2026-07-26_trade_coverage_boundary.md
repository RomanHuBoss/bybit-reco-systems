# Audit iteration: publicTrade coverage boundary

## 1. Input and version

- Input ZIP: `bybit-reco-systems-1.4.9-public-trade-ordering.zip`
- Input SHA-256: `50873206a002a719021b236299fc0fc7fe0c0f122225a93b7e64bd4cee7e5df7`
- Project root: `bybit-reco-systems-main`
- Baseline version: `1.4.9`
- Release version: `1.4.10`
- Version class: patch
- Iteration test number: `280`

## 2. Project fingerprint

Confirmed:

- Bybit Recommender recommendation/audit service;
- strategy families `futures_grid` and `directional_trend`;
- public/read-only Bybit Linear USDT scope;
- SQLite and PostgreSQL persistence paths;
- FastAPI source of truth in `app/main.py`;
- frontend in `app/ui/static/`;
- no private Bybit order-create/amend/cancel endpoint added.

## 3. Goal

After this iteration, a valid first `publicTrade.{symbol}` message where `data[].T` equals the envelope `ts` must not crash `market_trade_stream`. The service must preserve its conservative exclusive coverage boundary, persist the trade/session, and remain fail-closed for incomplete candle evidence.

## 4. Acceptance criteria

1. `record_market_trade_stream_batch()` accepts `oldest_trade_ts_ms == message_ts_ms`.
2. The initial span remains conservative: start is `oldest_trade_ts_ms + 1`.
3. The end is never earlier than start; equality creates a zero-width span.
4. The supervised WebSocket session processes and closes such a span without exception.
5. Model, outcome-label and observation provenance remain unchanged.
6. No DB schema or `.env` migration is required.
7. Related trade/funding tests and the exhaustive full suite pass.

## 5. Read sources and data flow

Relevant sources inspected:

- `app/trade_stream.py` — parser and session loop;
- `app/db.py` — trade persistence, coverage validation and path retrieval;
- `app/main.py` — supervised background thread;
- `app/outcomes.py` — coverage consumption and replay boundary;
- `tests/test_iteration278_funding_recovery_trade_journal.py`;
- `tests/test_iteration279_public_trade_ordering.py`;
- current README, CHANGELOG, architecture, trading logic, scenarios, known risks and modules docs;
- user-provided production traceback.

Data path:

`publicTrade payload -> parse_public_trade_message -> record_market_trade_stream_batch -> upsert_market_trades -> insert_market_trade_coverage -> get_market_trade_path -> grid outcome replay`.

## 6. Baseline environment

- Python: `3.13.5`
- Node: `22.16.0`
- Production Python files: 27
- Test files before iteration: 225
- Documentation files before new report: 104
- Frontend files: 3
- Migration SQL files: 2
- Maximum previous iteration number: 279

## 7. Baseline checks

- ZIP traversal/absolute-path/symlink/duplicate check: PASSED.
- ZIP entries: 383; exactly one root directory.
- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pip check`: FAILED because the shared environment has `moviepy 2.2.1` requiring `pillow<12`, while Pillow 12.2.0 is installed. The project iteration did not change either package.
- `python -m ruff check .`: UNAVAILABLE (`ruff` is not installed).
- Existing related tests: 23 passed.
- Monolithic baseline `python -m pytest -q`: TIMED OUT at approximately 69% due harness limit.
- Exhaustive deterministic baseline batches: 1345 collected, 1345 passed, 0 failed, 0 skipped. The union of eight batches equals the collected node set.

## 8. Confirmed defect

### TRADE-COVERAGE-001

- Severity: HIGH
- Type: CONFIRMED DEFECT
- Files: `app/db.py`, function `record_market_trade_stream_batch()`
- Production symptom: repeated `background thread crashed: market_trade_stream` with `ValueError: invalid market trade coverage window`.
- Trigger: first WebSocket batch has `oldest data[].T == envelope ts`.
- Baseline calculation:
  - `coverage_start_ms = oldest_ms + 1`;
  - `coverage_end_ms = message_ts`;
  - when `oldest_ms == message_ts`, `start = ts + 1`, `end = ts`;
  - `insert_market_trade_coverage()` correctly rejects `end < start`.
- Root cause: the batch writer preserved an exclusive first-trade boundary but did not ensure the initial coverage end reached that boundary.
- Impact:
  - the public-trade background stream restarts repeatedly;
  - trade chronology coverage is not accumulated;
  - ambiguous grid outcomes remain unobservable;
  - repeated system-error records pollute operator diagnostics;
  - no direct order or capital risk because the service remains recommendation/audit-only and fail-closed.
- Why previous tests missed it: fixtures used `T < ts`; the equal-boundary case was absent.

## 9. RED -> GREEN evidence

New regression file: `tests/test_iteration280_market_trade_coverage_window.py`.

RED command:

```bash
python -m pytest -q tests/test_iteration280_market_trade_coverage_window.py
```

Material RED result on pristine 1.4.9 plus tests:

```text
2 failed
ValueError: invalid market trade coverage window
```

Fix:

- keep `coverage_start_ms = oldest_ms + 1`;
- compute `effective_coverage_end_ms` as the maximum of:
  - message timestamp;
  - coverage start;
  - prior coverage end, when present.

This creates `[ts+1, ts+1]` for the equal-timestamp first batch. It prevents the crash without moving the start backward or claiming the first observed millisecond as complete evidence.

GREEN command:

```bash
python -m pytest -q tests/test_iteration280_market_trade_coverage_window.py
```

GREEN result:

```text
2 passed
```

Related suite:

```text
25 passed
```

## 10. Model and outcome lineage

Unchanged:

- `RECOMMENDER_MODEL_VERSION = bybit-taxonomy-v13-log-symmetric-direction`;
- `OUTCOME_LABEL_VERSION = grid_label_v26`;
- `GRID_INTRABAR_OBSERVATION_VERSION = grid_intrabar_observation_v3`.

This patch does not change signals, features, score, target semantics, risk policy, router, calibration inputs or replay ordering. It prevents a persistence-boundary crash only.

Consequences:

- no outcome reset;
- no recommendation deletion;
- no calibrator reset;
- no mass historical rewrite;
- existing crash records remain audit history.

## 11. Database, API and configuration compatibility

- DB schema: unchanged.
- SQLite migration: not required.
- PostgreSQL migration: not required.
- Public API fields/routes: unchanged.
- `.env`: unchanged.
- Existing databases: compatible without manual SQL.
- Transaction and source/session isolation: unchanged.

## 12. Modified files

### Production

- `app/db.py`
- `app/main.py` — version only

### Frontend

- `app/ui/static/index.html` — cache/version token only

### Tests

- new `tests/test_iteration280_market_trade_coverage_window.py`
- exact current-version assertions updated from 1.4.9 to 1.4.10 in existing regression tests

### Documentation

- `README.md`
- `CHANGELOG.md`
- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- this audit report

### Database/migrations/config

- no changes

## 13. Final checks

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- New targeted test repeated: 2 passed.
- Related funding/trade suites: 25 passed.
- Final collection: 1347 tests.
- Exhaustive deterministic post-check: 1347 passed, 0 failed, 0 skipped in eight non-overlapping batches.
- Model/outcome/observation identities: unchanged.
- Exact application/frontend version consistency: PASSED.
- Private Bybit order endpoints: none added.

## 14. Not verified

- Live long-duration Bybit WebSocket smoke was not run; the regression uses deterministic offline payloads matching the production trigger.
- Disposable PostgreSQL DSN was not provided, so live PostgreSQL integration was not run. No schema or dialect-specific SQL changed.
- `ruff` was unavailable.
- Shared-environment `pip check` conflict remains unrelated to this project patch.

## 15. Residual risks

- A zero-width first span is intentionally not usable as complete candle coverage until later messages extend it.
- Public trade chronology does not prove exchange queue position, actual fills, partial fills, latency or replacement-order activation.
- Network disconnects still close the current span and create an explicit evidence gap, by design.

## 16. Operator actions

1. Stop v1.4.9.
2. Back up the database.
3. Replace application files with v1.4.10.
4. Start normally.
5. Confirm `/api/v1/status` shows the `market_trade_stream` thread running without repeated `invalid market trade coverage window` errors.

No SQL, `.env` edit, outcome deletion or model reset is required.

## 17. Rollback

Stop v1.4.10, restore v1.4.9 application files and restart. The database is unchanged. Do not delete recommendations, outcomes or audit history.

## 18. Recommended next work package

Run a bounded live-public-stream soak test with metrics for reconnect count, messages/trades per symbol, coverage-span duration and gap reasons. This should remain an observability validation and must not add private execution APIs.
