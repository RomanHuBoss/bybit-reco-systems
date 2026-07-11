# Changelog

## 2026-07-11 - v1.0.18 - outcome/funding integrity

- Closed a HIGH proxy-label defect: a directional per-leg TP touch can no longer override any lower/upper kill-switch breach in the same outcome horizon. A stopped grid therefore cannot become a positive calibration label through an isolated profitable leg.
- Closed a HIGH funding-cost defect: fractional, boolean and non-positive `funding_interval_min` values are no longer rounded into a confirmed exchange schedule. They use the conservative 8-hour fallback, remain explicitly uncertain and carry `fallback_8h_invalid_interval` provenance.
- Closed a MEDIUM temporal-integrity defect: fractional or missing `recommendations.ts` / `features_ref_ts` values are skipped with `OUTCOME_SKIP_INVALID_TEMPORAL_FIELDS` instead of being truncated into synthetic chronology.
- No database schema, migration, public API, environment variable or frontend contract change. Existing operator DOCX/PDF/PNG artifacts remain compatible; Markdown trading/risk/infographic sources were synchronized.
- Baseline: 855 passed in 24.01s. Targeted RED: 3 failed. Targeted GREEN: 3 passed, repeated twice. Relevant outcome/funding suite: 117 passed. Post-check collection: 858 tests. The monolithic post-check stalled at 75% without a failure summary; exhaustive deterministic execution in six disjoint 143-test batches covered the exact collected set and produced 858 passed.
- `compileall` and Node syntax passed. PostgreSQL translation/locking suite: 21 passed; fresh SQLite bootstrap created 17 tables. Ruff remained unavailable. `pip check` retained the unrelated environment-level MoviePy/Pillow conflict. Live PostgreSQL integration was skipped because no verified disposable DSN was supplied.

## 2026-07-11 - v1.0.17 - exact-evidence negative-expectancy stop gate

- Closed a HIGH control-loop defect: accumulated exact realised losses were descriptive only and did not prevent the next operator execution.
- Execution preflight now blocks after five consecutive independent losses for the same symbol/direction, or after predefined direction/symbol/portfolio cohorts show negative total and median net PnL with a positive-bot rate below 50%.
- Observations are stopped bots with exact execution evidence only, newest-first, finite-valued, deduplicated by immutable `publication_root_rec_id`, separated by direction and scoped to the explicit `model_version`.
- `/api/v1/validation/live-evidence` now exposes the same strategy-health policy/metrics while continuing to state that it does not prove live edge.
- No database schema or migration change. Existing evidence rows are consumed directly; no `.env` action is required.
- Baseline: 849 passed. Targeted RED: 4 failed / 2 passed. Targeted GREEN: 6 passed. Full post-check: 855 passed in 34.19s. `compileall` and Node syntax passed; Ruff remained unavailable; `pip check` retained the environment-level MoviePy/Pillow conflict.

## 2026-07-11 - v1.0.16 - execution evidence and realised PnL integrity

- Added an immutable execution-evidence ledger linking each Bybit execution/funding event directly to `bot_id` and the originating `rec_id`, with exchange `execId`/transaction id uniqueness and partial-fill-safe `orderId` linkage.
- Added authenticated evidence ingestion/export and a descriptive live-validation dataset; it explicitly does not claim live edge without chronological comparison and sufficient independent observations.
- Corrected realised accounting to `gross fill PnL + signed funding - signed fee`. Fill slippage is measured against a separately timestamped pre-submit/decision benchmark and reported diagnostically; it is not subtracted again from PnL already based on actual fill prices.
- Legacy `/trades` rows now retain signed funding and execution-quality diagnostics, while risk, drawdown and cooldown use one de-duplicated realised stream. Mixing legacy rows and exact execution evidence for the same bot is blocked.
- Added additive SQLite/PostgreSQL schema updates, exact-id idempotency, sensitive-read API-key protection, and a release builder that excludes runtime databases including `data/app.runtime_locks.sqlite`.
- Baseline: 840 passed. Final RED: 9 failed on v1.0.15. Targeted GREEN: 9 passed. Full post-check: 849 passed. `compileall` and Node syntax passed; Ruff remained unavailable; `pip check` retained the environment-level MoviePy/Pillow conflict.

## 2026-07-11 - v1.0.15 - signal durability and immutable recommendation identity

- Every actionable `futures_grid` publication now requires two different, forward-moving closed evidence snapshots; a high one-cycle score can no longer bypass confirmation.
- Repeated recommender cycles on the same `features_ref_ts`, stale/out-of-order evidence and legacy persistence state no longer manufacture independent confirmation.
- The details refresh button now reloads the exact selected immutable `rec_id`; a newer row for the same pair can no longer silently replace it with `no_trade`, another status or another direction.
- Documentation now states that raw confidence is a nonlinear heuristic of launch-score, while calibrated confidence still targets proxy outcomes and does not prove live profitability.
- No database schema, migration, public route or environment-variable change. Existing persistence JSON upgrades conservatively: legacy state must observe a new closed snapshot before publication.
- Baseline: 838 passed. Targeted RED: 2 failed. Targeted GREEN: 2 passed (repeated twice). Relevant suite: 100 passed. Full post-check: 840 passed. `compileall` and Node syntax passed; Ruff was unavailable; `pip check` retained the environment-level MoviePy/Pillow conflict.

## 2026-07-11 - v1.0.14 - live execution spread/economics revalidation

- Costed Linear USDT futures-grid recommendations now require a valid live best bid/ask pair at operator execution; `lastPrice` remains usable for range drift but can no longer stand in for an executable spread.
- Execution preflight recomputes spread and slippage from the current bid/ask, preserves the greater stored/configured round-trip fee floor and any conservative residual execution cost, then subtracts adverse funding cost.
- The publication gates are reapplied to live economics: spread must be at most 14 bps, net edge at least 2 bps, and gross edge must cover execution cost by more than 1.10x.
- New fail-closed codes: `LIVE_SPREAD_UNAVAILABLE`, `LIVE_SPREAD_TOO_WIDE`, `LIVE_EXECUTION_EDGE_NON_POSITIVE`, `LIVE_EXECUTION_EDGE_TOO_THIN`, and `LIVE_GROSS_EDGE_BELOW_COSTS`.
- Baseline: 833 passed. Targeted RED: 4 failed / 1 passed; targeted GREEN: 5 passed. Full post-check: 838 passed. Ruff remains at the same 9 pre-existing findings, with no new findings.
- No schema, migration, public route, environment variable or frontend contract change. Legacy/manual payloads without `cost_model` keep their documented compatibility path. Live PostgreSQL integration remained untested because no verified disposable DSN was supplied.

## 2026-07-11 - v1.0.13 - Bybit response/request integer integrity

- Bybit V5 responses now require a present exact-integer `retCode`; missing, null, boolean, empty, collection and fractional zero-like values can no longer masquerade as success.
- Malformed `retCode` follows the existing retryable response-shape path and fails closed after retry exhaustion.
- Kline/open-interest limits and millisecond time boundaries reject boolean and fractional inputs instead of truncating them with `int()`.
- Negative and inverted request windows are blocked before network access; exact integral numeric values remain compatible.
- Baseline: 810 passed. Post-check: 833 passed; 23 new regression items. Ruff remains at the same 9 pre-existing findings, with no new findings.
- No schema, migration, public route, environment variable, frontend contract or operator lifecycle change. Live PostgreSQL integration remained untested because no verified disposable DSN was supplied.

## 2026-07-11 - v1.0.12 - strict temporal/funding integer semantics

- Bybit ticker, OHLCV and open-interest timestamps are no longer silently truncated from fractional values into valid integer keys.
- `fundingIntervalHour` and `fundingInterval` now require exact whole-hour/integer-minute semantics; malformed metadata remains unavailable and therefore fail-closed.
- Fractional funding/OI rows can no longer overwrite a valid SQLite/PostgreSQL logical key after coercion.
- Purged calibration rejects fractional recommendation and label-availability timestamps instead of manufacturing chronology through `int()`.
- Fractional label horizons fall back to the canonical 12-hour futures-grid horizon; unknown funding schedules use the conservative possible-event count.
- Funding cashflow accepts only exact integer event counts.
- Baseline: 800 passed. Post-check: 810 passed; 10 new regression items. Ruff remains at the same 9 pre-existing findings, with no new findings.
- No schema, migration, public API, environment variable or operator lifecycle change. Live PostgreSQL integration remained untested because no verified disposable DSN was supplied.

## 2026-06-18 - History/order-label regression audit

- The «История и динамика» table now shows newest publications first while the timeline remains chronological.
- Canonical direction normalization now reaches proxy-outcome return and TP calculations.
- Boolean label horizons no longer mature futures-grid outcomes at six hours.
- Valid zero coherence is preserved in expected R:R instead of being replaced by a neutral default.
- Full regression suite: 767 passed.

## 2026-06-15 - Audit delivery consistency

- Restored release artifact manifest consistency: operator guide DOCX/PDF, operator infographic source, PNG quick-reference, and changelog are shipped with the repository.
- Kept the execution boundary unchanged: this project remains a recommendation/audit service, not OMS/EMS.
- No fail-open changes were introduced.

## Current safety profile

- Bybit Linear USDT futures grid only.
- One running bot per account/symbol by default.
- Shipped actionable leverage interval: min_leverage=3, max_leverage=5.
- Execution preflight remains fail-closed on missing trade plan, stale market data, invalid Bybit metadata, invalid directional TP/SL geometry, and insufficient economic edge.
