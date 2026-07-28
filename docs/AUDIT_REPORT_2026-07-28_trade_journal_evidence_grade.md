# Audit report - v1.5.1 trade-journal evidence grading

## 1. Iteration identity

- Input ZIP: `bybit-reco-systems-1.5.0-dual-strategy-data-efficiency.zip`
- Input SHA-256: `2168ffee75223bf1f8cb88235ad3657a4fd8891d8d8a38dcf07dbfc060f90926`
- Input version: `1.5.0`
- Output version: `1.5.1`
- Project fingerprint: Bybit Linear USDT recommendation/audit service; SQLite/PostgreSQL; `futures_grid` and `directional_trend`; FastAPI in `app/main.py`; frontend in `app/ui/static/`.

## 2. Goal and acceptance criteria

After this iteration the system must:

1. keep both grid and trend strategies unchanged;
2. never treat REST recent-trade overlap as exact intrabar delivery order;
3. keep exact replay for uninterrupted session-isolated `publicTrade` WebSocket evidence;
4. keep persistent WebSocket/OHLC disagreement fail-closed;
5. expose observed trade OHLC and mismatching fields in diagnostics;
6. requeue legacy REST-derived `trade_journal_ohlcv_mismatch` censorship once;
7. preserve trading model, risk policy and target-label lineages;
8. pass the complete offline test corpus.

## 3. Confirmed defect

### TJ-285 - REST overlap was over-graded as exact chronology

- Type: CONFIRMED DEFECT
- Severity: HIGH for model/data integrity; no direct order-execution impact because the project remains audit/recommendation-only.
- Files: `app/outcomes.py`, `app/db.py`.
- Trigger: a full-minute `market_trade_coverage` row sourced from `rest_recent_trade_v1` plus multiple trades sharing the same execution timestamp and sequence.
- Previous behavior: `get_market_trade_path()` sorted REST rows by `(trade_ts_ms, seq, trade_id)`. `_grid_outcome()` interpreted the resulting first/last row as exact candle open/close and censored the outcome as `trade_journal_ohlcv_mismatch` when lexical trade-ID order disagreed with OHLC.
- Evidence from the operator screenshot: the coverage ID begins with `trade:`, identifying the REST fallback lane; the affected recommendation belongs to historical v13 lineage.
- External contract: Bybit documents ascending matched-time order for `publicTrade.{symbol}` WebSocket messages. The REST recent-trade contract exposes trade time, sequence and execution ID, but does not define exact delivery order among equal-time/equal-sequence rows.
- Expected behavior: REST overlap is bootstrap/gap evidence only. Exact intrabar replay requires one uninterrupted WebSocket session covering the whole candle.

### Financial/model impact

A false mismatch removed otherwise usable grid roots from the evidence sample. It did not create a false profitable label; it reduced data efficiency and could bias grid calibration by unnecessary censorship.

## 4. Implementation

### `app/outcomes.py`

- Observation provenance: `grid_intrabar_observation_v3` -> `grid_intrabar_observation_v4`.
- Exact replay now requires `source == websocket_public_trade_v1`.
- REST coverage falls back to canonical OHLC path-equivalence.
- Successful outcome diagnostics include ignored non-exact coverage IDs.
- Persistent WebSocket/OHLC mismatch now records:
  - source;
  - ordering basis;
  - observed trade open/high/low/close;
  - exact mismatching fields.

### `app/db.py`

Added `requeue_rest_trade_ohlcv_mismatches()`:

- selects only censored `trade_journal_ohlcv_mismatch` rows without a persisted outcome;
- requeues only REST-provenance rows (`trade:` coverage ID or explicit REST source);
- leaves WebSocket mismatch censorship final;
- is idempotent and requires no schema change.

### Version and documentation

Updated backend version and frontend cache to `1.5.1`; synchronized README, CHANGELOG, architecture, trading logic, modules, scenarios, known risks and infographic source.

## 5. Lineage and database compatibility

Unchanged:

- `RECOMMENDER_MODEL_VERSION = bybit-taxonomy-v14-horizon-aligned-dual-strategy`;
- trend suffix `directional-trend-v7`;
- `grid_label_v26`;
- `directional_trend_label_v2`;
- risk/profile semantics;
- SQLite/PostgreSQL schema.

Existing recommendations and outcomes are not deleted. This is an observation-provenance repair, not a new trading model. Legacy REST-derived censored roots can be recalculated under v4; true WebSocket mismatches remain censored.

## 6. RED -> GREEN evidence

New test file: `tests/test_iteration285_trade_journal_evidence_grade.py`.

RED on pristine v1.5.0:

```text
3 failed
- REST mismatch returned None instead of OHLC fallback outcome
- WebSocket mismatch lacked source/delta diagnostics
- requeue_rest_trade_ohlcv_mismatches was absent
```

GREEN on v1.5.1:

```text
22 passed
```

This targeted result includes iteration 285 plus the full funding/trade-journal iteration 278 suite.

## 7. Post-check

- `pytest --collect-only -q`: 1379 tests collected.
- Exhaustive deterministic two-batch run:
  - batch A: 635 passed;
  - batch B: 744 passed;
  - union: 1379 passed, 0 failed, 0 skipped.
- Relevant trade/funding/stream suite: 56 passed before the final legacy-requeue addition; final targeted suite: 22 passed.
- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pip check`: FAILED because the shared environment has pre-existing `moviepy 2.2.1` / `Pillow 12.2.0` incompatibility; this release did not change those packages.
- `ruff`: not run; unavailable in the environment.
- Live Bybit and production PostgreSQL tests: not run; no disposable DSN or production-network test was used.

## 8. Residual risks

- Public WebSocket chronology proves public price order, not queue priority, actual fills, partial fills, latency or replacement-order activation.
- A true WebSocket/OHLC mismatch remains censored. The added diagnostics are intended to distinguish a source inconsistency from a REST ordering artifact.
- Legacy REST-derived rows are requeued in bounded batches during outcome maintenance; a large historical backlog may require several cycles.

## 9. Upgrade and rollback

Upgrade:

1. stop v1.5.0;
2. back up the database;
3. replace project files with v1.5.1;
4. keep the existing `.env`;
5. start normally; no manual SQL is required.

Rollback:

- stop v1.5.1 and restore v1.5.0 files;
- no database rollback is required because schema is unchanged.

## 10. Recommended next work package

Freeze v14 strategy and v4 observation semantics. Collect enough independent grid and trend roots before changing thresholds again; review censorship rates by exact evidence source and compare monetary results against score-only/null baselines.
