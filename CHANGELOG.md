# Changelog

## 2026-07-12 - v1.0.23 - temporal market-data and calibration lineage

- Fixed a HIGH freshness defect: Bybit V5 envelope time is preserved for ticker rows, so stale/cached snapshots cannot be relabelled with local receipt time.
- Fixed HIGH temporal-integrity defects: OHLCV start times must be exact whole-second boundaries aligned to the requested timeframe; boolean/fractional feature timestamps are rejected instead of truncated.
- Fixed a HIGH proxy-label defect: outcomes require the exact next 1-minute entry candle, a complete contiguous horizon, and the exact horizon-boundary exit candle; gaps no longer move entry/exit to a later market.
- Fixed a HIGH calibration leakage defect: rows without a valid, already-matured `label_available_ts` are excluded, and malformed persisted outcome numerics are sanitized/ignored without crashing the cycle.
- Bumped `OUTCOME_LABEL_VERSION` to `grid_label_v4`; the existing startup reset removes incompatible proxy outcomes/calibrators while preserving recommendations, bot audit rows, trades and exact execution evidence.
- Added `tests/test_iteration211_temporal_data_lineage.py` with seven red-to-green regressions. Baseline: 877/877 tests passed in exhaustive batches. Post-check counts are recorded in the audit report.

## 2026-07-11 - v1.0.22 - no-trade semantics and shadow outcome continuity

- Fixed a HIGH lifecycle/status defect: valid-but-weak `MEAN_REVERSION_EDGE_UNCONFIRMED` is now `no_trade`, not a hard technical `blocked`; missing mandatory mean-reversion evidence remains fail-closed `blocked`.
- Fixed a HIGH learning deadlock: explicitly opted-in `no_trade` candidates with a complete trade plan and no hard risk/data blocks now mature into `shadow_no_trade` proxy outcomes. Blocked, malformed, pending and legacy no-trade rows remain excluded.
- Added outcome-sample diagnostics (`shadow_no_trade_total`, `actionable_total`, `executed_audit_total`) and corrected the operator UI so proxy outcomes are not described as real exchange execution.
- Clarified that an unfitted calibrator does not itself block publication; raw confidence is used until a bot-specific fit exists.
- Added `tests/test_iteration210_no_recommendation_state.py` with three red-to-green regressions. Baseline: 874 passed. Post-check: 877 passed.

## 2026-07-11 - v1.0.21 - outcome capital normalization and daily loss budget

- Fixed a HIGH proxy-label defect: a directional per-leg TP touch can no longer mark the whole grid successful when unresolved end-of-horizon inventory is losing. Whole-grid success now requires matched oscillation cycles, intact kill-switch geometry and positive net proxy economics.
- Fixed a HIGH econometric defect: completed-leg profit and execution costs are normalized by committed grid capital (`grid_count`) instead of treating every one-order percentage as a return on the full grid.
- Added a HIGH execution risk guard: conservative loss to the adverse kill-switch, including explicit execution friction, must fit inside the remaining `max_daily_dd_usdt - daily_dd` budget. Otherwise execution is blocked with `DAILY_LOSS_BUDGET_EXCEEDED`.
- Bumped `OUTCOME_LABEL_VERSION` from `grid_label_v2` to `grid_label_v3`; the next startup clears legacy proxy outcomes and associated calibrators so incompatible labels are not mixed. Recommendations, bot audit rows, exact execution evidence and DB schema are unchanged.
- Updated the two historical tests that encoded the invalid contract “one TP leg = successful whole grid”; added `tests/test_iteration209_outcome_capital_and_daily_risk.py` with four red-to-green regressions.
- Baseline: 870 tests passed. Post-check: 874 tests passed; compileall and Node syntax passed; ruff unavailable in the environment; `pip check` reported an unrelated global Pillow/moviepy mismatch.

## 2026-07-11 - v1.0.20 - independent mean-reversion edge gate

- Closed a HIGH model defect: low trend / flat moving-average slope is no longer treated as sufficient evidence of a profitable grid range. The recommender now measures anti-persistence independently through lag-1 return autocorrelation, a four-step variance ratio and sign-reversal frequency across multiple closed timeframes.
- Added fail-closed publication codes `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` and `MEAN_REVERSION_EDGE_UNCONFIRMED`. A driftless/random-walk-like path is blocked even when the legacy `1 - trend_strength` range proxy is high.
- Changed model identity to `bybit-taxonomy-v3-mean-reversion` and calibration keys to v4. Calibration now accepts only outcomes from the current model with an explicit independent mean-reversion feature snapshot, preventing old coefficients/outcomes from reintroducing the former range tautology.
- Corrected operator semantics: legacy `expected_rr` remains API-compatible but is labelled as a heuristic capture/volatility proxy, not an actual reward-to-loss ratio or profitability proof.
- No database schema, migration, route or environment-variable change. Existing v3 calibrator rows remain stored but are no longer loaded by the v4 keys; the new calibrator remains unfitted until enough v3-model outcomes mature.
- Baseline: 862 collected / 862 passed. Targeted RED on pristine code: collection error because `mean_reversion_diagnostics` was absent (an earlier seven-test RED run produced seven expected failures). Targeted GREEN: 8 passed. Final counts are recorded in the bundled internal release audit.

## 2026-07-11 - v1.0.19 - risk sizing integrity

- Closed a HIGH configuration defect: built-in and fallback risk limits now match the shipped 100-500 USDT operator profile even when `.env` is absent (1 bot, daily DD 10 USDT, 90-minute cooldown, 500 USDT max notional, 100 USDT max margin, leverage 3-5x).
- Closed a HIGH sizing defect: provisional quantity no longer rounds up to an invented 0.001 step; a 25 USDT BTCUSDT target remains 25 USDT until live instrument filters are known.
- Closed a HIGH execution-boundary defect: live qty alignment is down-only. minQty/minNotional insufficiency is blocked instead of silently increasing exposure.
- No schema, migration, public route, environment-variable or frontend contract change. Existing explicit `RISK_LIMITS_JSON` overrides remain supported.
- Baseline: 858 passed. Targeted RED: 4 failed. Targeted GREEN: 4 passed, repeated twice. Working-copy post-check: 862 collected and 862 passed. Clean-ZIP monolithic run timed out at 75% without a failure summary, so all exact 862 collected nodes were rerun in disjoint deterministic groups and passed. PostgreSQL dialect/locking suite: 20 passed; fresh/repeated SQLite bootstrap: 17 tables. Ruff was unavailable and `pip check` retained the unrelated environment-level MoviePy/Pillow conflict.

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
