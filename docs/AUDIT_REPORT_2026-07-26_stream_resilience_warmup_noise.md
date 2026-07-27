# Audit iteration: WebSocket reconnect resilience and warm-up journal coalescing

## 1. Input

- Input ZIP: `bybit-reco-systems-1.4.10-trade-coverage-boundary.zip`
- Input SHA-256: `c195424a7583c869a23d08d5c663af0cb30a4cd5b0260e8d107cd3ae17acc94e`
- Source version: `1.4.10`
- Release version: `1.4.11`
- Iteration test: `tests/test_iteration281_stream_resilience_warmup_noise.py`

## 2. Project fingerprint

Confirmed Bybit Recommender recommendation/audit service with:

- FastAPI application in `app/main.py`;
- strategy families `futures_grid` and `directional_trend`;
- Bybit Linear USDT perpetual scope;
- SQLite and PostgreSQL persistence;
- public/read-only Bybit client and no private order create/amend/cancel endpoints;
- frontend in `app/ui/static/`;
- existing `market_trade`, `market_trade_coverage` and funding-repair observability from v1.4.8-v1.4.10.

## 3. Goal and acceptance criteria

After this iteration the system must:

1. Treat WebSocket network/keepalive disconnects as normal session boundaries, not background worker crashes.
2. Reconnect within bounded backoff without bridging unknown coverage.
3. Tolerate short DB/network stalls through wider keepalive settings and bounded message buffering.
4. Reduce write pressure by committing multiple trade messages per transaction.
5. Use REST recent-trade only while the primary WebSocket is unavailable.
6. Record one warm-up decision per continuous state episode rather than the same 61-field payload every two minutes.
7. Preserve all model, outcome, recommendation and calibration lineage.
8. Pass the new RED->GREEN package, relevant suites, compile/frontend checks and exhaustive test union.

## 4. Evidence supplied by the operator

The Windows traceback showed `websockets.exceptions.ConnectionClosedError: sent 1011 ... keepalive ping timeout` followed by `TimeoutError: timed out while closing connection`. The outer supervisor classified this transport event as `background thread crashed: market_trade_stream`.

The supplied diagnostic JSON showed:

- 35/35 symbols stale and 0 ready at the captured warm-up snapshot;
- current runtime owner `RRMPC:18100`, while the most recent completed collector cycle still belonged to the prior process `RRMPC:19508`;
- current process had the collector lock but had not completed its own collector/publication cycle;
- the hot collector simultaneously polled 35 symbols and received 35,000 REST trade rows, while inserting only 825 new rows;
- trade journal already contained 1,010,592 rows and 3,036 coverage spans;
- market trade WebSocket spans were otherwise open and recent for the configured symbols.

Interpretation: the yellow `RECO_WARMUP_SKIP` was a fail-closed startup/readiness state, not a Bybit rejection. Repeating it every 120 seconds was an operator-UI defect. Always-on REST trade polling duplicated the active WebSocket source and added avoidable API/DB pressure to the same hot collector responsible for ticker/OHLCV freshness.

## 5. Baseline

Environment:

- Python: `3.13.5`
- Node: `22.16.0`
- No production credentials or production database used.
- Tests used temporary SQLite databases and mocks/fixtures.

Commands/results:

- `python -m compileall -q app tests main.py`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- `python -m pytest --collect-only -q`: 1347 collected
- monolithic `pytest -q`: TIMED OUT in the harness around 69%; not counted as a full run
- exhaustive deterministic 20-batch union: 1347 passed, 0 failed

## 6. Confirmed defects and gaps

### WS-281-A — expected network disconnect reported as worker crash

- Severity: HIGH
- Type: CONFIRMED DEFECT
- Files: `app/trade_stream.py`, `app/main.py`
- Actual behavior: `ConnectionClosedError` or closing timeout escaped the session; the supervised wrapper logged `background thread crashed` and `COLLECT_ERROR`.
- Expected: close coverage, persist session diagnostics, reconnect; no error flood.
- Impact: false system-error status, repeated restart logs, unnecessary operator alarm and gaps during restart delay.

### WS-281-B — keepalive/backpressure settings too aggressive for synchronous DB consumer

- Severity: HIGH
- Type: CONFIRMED GAP
- File: `app/trade_stream.py`
- Actual behavior: `ping_timeout=10`, default `max_queue=16`, and one commit per message.
- Expected: enough queue and timeout headroom for short DB/network stalls; bounded batch commits.
- Impact: false keepalive failure under local PostgreSQL/write pressure.

### WS-281-C — REST trade ingestion runs concurrently with healthy WebSocket

- Severity: HIGH
- Type: CONFIRMED DEFECT
- Files: `app/main.py`, `app/collector.py` call path
- Actual behavior: `MARKET_TRADE_JOURNAL_ENABLED=1` caused recent-trade polling for every symbol every hot cycle even while WebSocket coverage was active.
- Expected: REST is source-isolated fallback only.
- Impact: 35,000 REST rows per captured cycle, database/API pressure and delayed OHLCV freshness.

### WS-281-D — unchanged warm-up state floods decision journal

- Severity: MEDIUM
- Type: CONFIRMED DEFECT
- Files: `app/main.py`, `app/ui/static/app.js`
- Actual behavior: identical `RECO_WARMUP_SKIP` payload logged every configured 120 seconds; nested samples flattened into dozens of fields.
- Expected: log state transition, material signature changes, and recovery once.
- Impact: decision-log noise obscures actual errors and recommendation-linked events.

## 7. RED -> GREEN evidence

RED command on pristine v1.4.10 plus only the new tests:

```bash
python -m pytest -q tests/test_iteration281_stream_resilience_warmup_noise.py
```

RED result:

```text
5 failed
ConnectionClosedError: no close frame received or sent
assert 10 >= 30
assert True is False
AttributeError: module 'app.main' has no attribute '_next_warmup_decision_event'
assert 1 == 2
```

GREEN command after production changes:

```bash
python -m pytest -q tests/test_iteration281_stream_resilience_warmup_noise.py
```

GREEN result:

```text
5 passed
```

## 8. Implementation

### `app/trade_stream.py`

- catches `websockets.exceptions.ConnectionClosed`, outer `TimeoutError` and `OSError` as normal session ends;
- preserves malformed payload and DB/invariant errors as exceptions;
- exposes process-local active/session/last-message/disconnect state;
- uses protocol ping interval 20 seconds and minimum timeout 60 seconds;
- sends Bybit application heartbeat `{"op":"ping"}`;
- increases message queue default to 256;
- commits after 32 messages or 0.5 seconds;
- closes each coverage span with explicit disconnect reason and never bridges reconnects.

### `app/main.py`

- runs repeated sessions internally with 2–30 second bounded backoff;
- expected disconnect no longer returns into the crash-oriented supervisor;
- exposes reconnect state in background diagnostics;
- disables REST recent-trade polling while the current process reports an active WebSocket session;
- re-enables REST fallback automatically on disconnect or stream disable;
- implements compact transition-based warm-up decision state machine;
- emits `RECO_WARMUP_RECOVERED` once on restoration;
- adds stream runtime and fallback state to `/api/v1/status`.

### `app/settings.py` and `.env.example`

Additive optional variables:

- `MARKET_TRADE_STREAM_PING_INTERVAL_SEC=20`
- `MARKET_TRADE_STREAM_PING_TIMEOUT_SEC=60`
- `MARKET_TRADE_STREAM_CLOSE_TIMEOUT_SEC=2`
- `MARKET_TRADE_STREAM_MAX_QUEUE=256`
- `MARKET_TRADE_STREAM_COMMIT_BATCH_MESSAGES=32`
- `MARKET_TRADE_STREAM_COMMIT_BATCH_SEC=0.5`
- `MARKET_TRADE_STREAM_RECONNECT_MIN_SEC=2`
- `MARKET_TRADE_STREAM_RECONNECT_MAX_SEC=30`

No operator change is required because defaults are embedded.

### Frontend

- Russian action labels for `RECO_WARMUP_SKIP` and `RECO_WARMUP_RECOVERED`.

## 9. Model, outcome and database compatibility

This is not a new trading model.

Unchanged:

- `RECOMMENDER_MODEL_VERSION=bybit-taxonomy-v13-log-symmetric-direction`
- `OUTCOME_LABEL_VERSION=grid_label_v26`
- `GRID_INTRABAR_OBSERVATION_VERSION=grid_intrabar_observation_v3`
- feature schema, score, direction, grid/trend geometry, risk policy and strategy router

Consequences:

- recommendations and outcomes are not deleted or reset;
- calibrator artifacts are not cleared;
- no new calibration/model lineage is started;
- no database schema migration is required;
- old decision-log errors remain immutable audit history;
- new WebSocket sessions create new coverage spans as before.

## 10. Post-check

- `python -m compileall -q app tests main.py`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- targeted iteration281: 5 passed
- relevant settings/runtime/funding/trade/UI suite: 54 passed
- funding/trade iterations 278–281: 30 passed
- `python -m pytest --collect-only -q`: 1352 collected
- monolithic `pytest -q`: TIMED OUT in harness around 69%; not claimed as full
- exhaustive deterministic 20-batch union: 1352 passed, 0 failed, 0 skipped

## 11. Database actions

None. Back up the database before deployment as normal operational practice. Do not delete recommendations, outcomes, market trades, coverage spans or decision history.

## 12. Configuration actions

None required. Existing `.env` remains valid. Optional tuning variables are documented in `.env.example`.

## 13. Security boundary

- public/read-only WebSocket and REST endpoints only;
- no private order endpoints or credentials added;
- no auto-execution or OMS/EMS behavior added;
- public trades remain price chronology evidence, not queue priority or actual fill proof.

## 14. Unverified

- live long-duration WebSocket soak under the operator's Windows/proxy/PostgreSQL environment;
- disposable live PostgreSQL integration (no test DSN supplied);
- ruff if unavailable in the execution environment.

## 15. Residual risks

- sustained DB throughput below incoming public-trade rate can still cause reconnects and observational gaps;
- any disconnect interval remains unknown and cannot be reconstructed as WebSocket evidence;
- REST fallback remains weaker and source-isolated;
- startup handover remains fail-closed until the new process completes its own collection and publication cycle.

## 16. Rollback

1. Stop v1.4.11.
2. Restore v1.4.10 application files.
3. Restart normally.
4. Do not roll back or delete the database; schema is unchanged.

## 17. Recommended next work package

Run a measured Windows/PostgreSQL soak test and add rate/lag telemetry: messages/sec, trades/sec, DB commit latency, queue saturation proxy, reconnect frequency and table growth. Do not change observation semantics until measured data shows the next bottleneck.
