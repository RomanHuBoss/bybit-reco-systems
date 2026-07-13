## 2026-07-14 - v1.0.56 - calibration lineage reset and dataset transparency

- Advanced recommendation lineage to `bybit-taxonomy-v7-mr-floor-temporal-cohorts`.
- Advanced bot/global calibrators to v18 and direction calibrator to v13.
- Preserved historical outcomes for audit while excluding old-model rows from current fitting.
- Split status/UI counts into historical, current-model, feature-eligible, fit-row and temporal-cohort layers.
- Added `test_iteration244_calibration_lineage_reset.py` with RED -> GREEN coverage.
- Database schema unchanged; no SQL migration required.

## 2026-07-13 - v1.0.55 - mean-reversion and temporal-evidence recovery

- Fixed a HIGH model-policy shutdown defect: the hard-coded `mean_reversion_score >= 0.55` screen had zero passes in the supplied 10,000-row PostgreSQL recommendation export (maximum `0.3510`, p95 `0.2926`). The screen is now explicit as `MEAN_REVERSION_MIN_SCORE`, default `0.25`.
- Preserved fail-closed semantics: missing/invalid multi-timeframe evidence remains `blocked`; a valid score below the candidate floor remains `no_trade`; positive monetary expectancy must still be proven separately from matured retained proxy outcomes.
- Fixed a MEDIUM diagnostic defect: a weak mean-reversion score no longer claims that commissions have proven negative expectancy.
- Fixed a HIGH temporal-evidence liveness defect: same-timestamp cross-sectional rows are collapsed to one decision cohort, then earliest-finish interval scheduling selects a maximum-cardinality pairwise non-overlapping cohort set. Transitive overlap chains can no longer freeze `temporal_cluster_count` at one indefinitely.
- Bumped bot/global calibrator identities from v16 to v17 so retained outcomes are refitted under the corrected temporal contract. Outcome contract remains `grid_label_v26`; direction calibration remains v12; model identity is unchanged.
- Removed packaged SQLite bootstrap/runtime-lock database files from the release. SQLite support and schema bootstrap code remain unchanged.
- Added `tests/test_iteration243_mean_reversion_temporal_recovery.py` with six red-to-green regressions; corrected one legacy OOF fixture whose overlapping timestamps unintentionally tested the superseded cluster contract; synchronized version/calibrator assertions.
- Updated README, trading/architecture/risk/scenario/module documentation, operator DOCX/PDF and `how_to_trade.png`.
- Baseline: 1072/1072 unique test nodes passed in six exhaustive non-overlapping batches; monolithic pytest timed out at 73% and was not counted. Post-check: 1078/1078 unique test nodes passed in six exhaustive non-overlapping batches; targeted regression 6/6 twice, PostgreSQL offline subset 24/24, SQLite fresh schema/re-init, compileall, Node syntax, release and document checks passed.
- No relational schema, migration, public API route, outcome-label or execution-boundary change. Live PostgreSQL integration was not run because no disposable test DSN was provided. Ruff remained unavailable; `pip check` retained the unrelated environment MoviePy/Pillow conflict.

## 2026-07-13 - v1.0.54 - purged OOF feature-calibration activation gate

- Fixed a HIGH model-validation fail-open defect: full-sample feature LogReg coefficients were exposed as calibrated confidence even when purged chronological OOF produced too few or zero validation predictions.
- Full feature LogReg now activates only after at least `CALIB_MIN_SAMPLES` purged OOF logits and a fitted Platt-on-top calibrator.
- Insufficient OOF evidence withholds feature coefficients and degrades to score-only Platt or conservative raw confidence; it does not bypass monetary or temporal expectancy gates.
- Added persisted/API diagnostics: `purged_oof_status`, `purged_oof_samples`, and `purged_oof_required_samples`.
- Bumped application to `1.0.54` and bot/global calibrator identities to v16. Outcome contract remains `grid_label_v26`; direction calibration remains v12; no outcome reset, DB schema, route or env change.
- Added `tests/test_iteration242_purged_oof_activation_gate.py` with concentrated-history, distributed-history and persistence red-to-green cases.
- Baseline: 1069/1069 unique test nodes passed in exhaustive deterministic batches. Post-check: 1072/1072 unique test nodes passed in 12 exhaustive non-overlapping batches.

## 2026-07-13 - v1.0.53 - horizon-boundary liquidation volume integrity

- Fixed a HIGH proxy-liquidity fail-open defect: the horizon-open segment reused the preceding minute's remaining candle-volume budget.
- Gap-crossed orders at the exact horizon open now consume the boundary candle's own volume, not historical liquidity from the prior minute.
- Terminal residual liquidation consumes the same boundary-minute budget; kill-switch liquidation consumes the remaining breach-candle budget.
- Outcomes wait until the boundary candle is complete and store `label_available_ts = horizon_end_ts + 60`; the configured strategy horizon itself is unchanged.
- Added `tests/test_iteration241_horizon_boundary_liquidity.py` with four red-to-green cases.
- Bumped application to `1.0.53`, outcome contract to `grid_label_v26`, bot/global calibrators to v15 and direction calibrator to v12; model identity, DB schema, routes and env are unchanged.
- Baseline: 1065/1065 test nodes passed in exhaustive batches. Post-check: 1069/1069 test nodes passed.

## 2026-07-13 - v1.0.52 - conservative kill-switch liquidation bound

- Fixed a HIGH proxy-PnL fail-open defect: an intrabar kill-switch breach liquidated residual inventory exactly at the trigger boundary even when the same candle proved continued adverse movement.
- After processing resting fills up to the boundary, residual short inventory on an upper breach is closed at the observed candle high; residual long inventory on a lower breach is closed at the observed candle low. Favorable continuation does not receive extra credit and keeps the boundary price.
- Added `kill_switch_fill_confirmation=adverse_observed_extreme_v1` plus boundary, observed-extreme and liquidation-price diagnostics. Gap-through-stop paths remain unavailable.
- Bumped application to `1.0.52`, outcome contract to `grid_label_v25`, bot/global calibrators to v14 and direction calibrator to v11; model identity, DB schema, routes and env are unchanged.
- Added `tests/test_iteration240_kill_switch_slippage_bound.py` and updated legacy stop fixtures that encoded perfect boundary execution.
- Baseline: 1062/1062 tests passed. Post-check: 1065/1065 tests passed.

## 2026-07-13 - v1.0.51 - historical simulation boundary

- Removed the runtime Bybit instrument-metadata dependency from recommendation publication and historical outcome labeling.
- Missing current `tickSize`, `qtyStep`, `minOrderQty`, `minNotional` or instrument metadata no longer blocks a recommendation or excludes a matured proxy outcome.
- Removed `exchange_normalizer` from `run_recommender_once` and removed recommendation-thread metadata prefetch/normalization.
- Added explicit `historical_proxy_only` metadata: no order submission, no runtime execution validation and no exchange fill attestation.
- Preserved explicit preflight snapping as a separate optional operator diagnostic; it cannot mutate persisted recommendation geometry or calibration evidence.
- Bumped application to `1.0.51`, model identity to `bybit-taxonomy-v6-historical-proxy-shadow-roots`, outcome contract to `grid_label_v24`, bot/global calibrators to v13 and direction calibrator to v10.
- Added `tests/test_iteration239_historical_simulation_boundary.py` and rewrote iteration236 to enforce the corrected architecture.
- DB schema, migrations, public routes and env contract are unchanged.
- Baseline: 1059/1059 unique nodes passed in exhaustive non-overlapping batches; monolithic run timed out without summary and was not counted.
- Post-check: 1062/1062 unique nodes passed in six 177-node batches; SQLite fresh/re-init/upgrade, PostgreSQL offline subset, DOCX/PDF/PNG render checks passed.

## 2026-07-13 - v1.0.50 - intrabar replacement-order timing fail-closed

- Fixed a HIGH proxy-execution fail-open defect: a replacement order created after a modeled parent fill could be filled again later in the same one-minute candle, implicitly assuming zero bot latency and known order-placement timing.
- Replacement orders are now pending until the next candle. If the current candle would cross a newly created replacement, the outcome is unavailable with `intrabar_replacement_fill_timing_unobservable` rather than choosing the optimistic immediate-placement path.
- Preserved confirmed grid cycles when the parent fill and replacement fill occur in separate candles; completed-cycle PnL, costs, funding and volume-capacity formulas are unchanged.
- Bumped outcome contract to `grid_label_v23`, bot/global calibrators to v12 and direction calibrator to v9; schema, routes, model identity and env remain unchanged.
- Added `tests/test_iteration238_intrabar_replacement_latency.py` and corrected three legacy fixtures that encoded zero-latency same-candle replacement fills while preserving their topology/capital assertions.
- Baseline: 1056/1056 unique test nodes passed through exhaustive non-overlapping batches; one 176-node harness batch required four deterministic 44-node sub-batches.
- Post-check: 1059/1059 unique test nodes passed in six exhaustive non-overlapping batches; targeted regression 3/3, PostgreSQL offline subset 24/24 and release-artifact subset 16/16 passed.
- SQLite fresh/re-init and v1.0.49→v1.0.50 upgrade passed with sentinel preservation and legacy calibrator reset. Ruff was unavailable; `pip check` retained the external MoviePy/Pillow conflict.

## 2026-07-13 - v1.0.49 - aggregate candle-volume fill capacity

- Fixed a HIGH proxy-execution fail-open defect: a crossed limit order was assumed fully filled even when its requested base quantity exceeded the entire observed one-minute Bybit kline volume.
- Added a cumulative per-candle volume budget for initial directional inventory and every simulated grid fill; impossible full-fill paths are now outcome-unavailable instead of profitable/losing labels.
- Preserve valid trade-through outcomes when observed aggregate volume is sufficient; this check remains a necessary capacity bound and does not claim queue or price-level liquidity.
- Bumped outcome contract to `grid_label_v22`, bot/global calibrators to v11 and direction calibrator to v8; model identity, DB schema, routes and env remain unchanged.
- Added `tests/test_iteration237_proxy_fill_volume_capacity.py` with five red-to-green regressions, including wildcard cleanup of all legacy `logreg_*` and `platt_direction_*` state on a label-contract reset.
- Baseline: 1051/1051 test nodes passed in exhaustive batches. Post-check: 1056/1056 test nodes passed in six exhaustive non-overlapping batches; monolithic pytest did not return a final summary within the harness.

## 2026-07-13 - v1.0.48 - exchange-normalized proxy execution

- Persist the Bybit-normalized arithmetic grid before recommendation publication, using public instrument filters already required by execution preflight.
- Block and exclude Linear futures-grid recommendations when tick/quantity/minimum-order geometry cannot be proven.
- Require strict side-aware trade-through instead of exact OHLC touch when reconstructing resting-limit fills.
- Bump model identity to `bybit-taxonomy-v5-exchange-normalized-shadow-roots`, bot/global calibrators to v10, direction calibrator to v7 and outcome contract to `grid_label_v21`.
- Add iteration235/236 red-to-green regression coverage and preserve old economic/topology tests with executable price paths.
- Baseline: 1045 tests passed in exhaustive batches. Post-check: 1051/1051 test nodes passed in exhaustive batches; monolithic pytest did not return a final summary within the harness.
- No DB schema, migration, API route or environment-variable change.

## 2026-07-13 - v1.0.46 - funding receipt is not canonical alpha

- Fixed a HIGH model/economics fail-open defect: positive settled funding receipts could turn a flat or losing grid proxy into `success=1` and positive calibration return.
- Canonical proxy `ret` now charges every adverse settled funding cashflow but excludes positive receipts from monetary expectancy and win-rate labels; signed settled funding remains diagnostic only.
- Bumped `OUTCOME_LABEL_VERSION` to `grid_label_v19` and reset incompatible proxy outcomes plus bot/global/direction calibrators on startup.
- Fixed the reset path to delete the current `DIRECTION_CALIBRATION_KEY`; the previous hard-coded legacy v4 key could leave a direction calibrator trained on superseded labels.
- Added `tests/test_iteration234_funding_receipt_not_alpha.py` with four red-to-green checks and corrected the old settled-funding fixture that encoded receipt-as-alpha behavior.
- No relational schema, public route or environment-variable change.

## 2026-07-13 - v1.0.45 - cross-symbol temporal independence

- Fixed a HIGH model-validation fail-open defect: contemporaneous outcomes from many correlated symbols were counted as independent monetary evidence.
- Replaced row-count-only monetary proof with overlap-component temporal clustering over `[ts, label_available_ts]`; overlapping and transitively overlapping horizons contribute one cluster mean.
- Required at least 20 effective non-overlapping temporal clusters for the default `CALIB_MIN_SAMPLES=80`, plus positive one-sided 95% lower bounds at both row and temporal-cluster levels.
- Bumped bot/global calibrator identities to `logreg_futures_grid_v9` and `logreg_global_v9` so v8 coefficients cannot bypass the new evidence contract.
- Added `tests/test_iteration233_cross_symbol_temporal_dependence.py` with four red-to-green regressions, including clock-boundary overlap and persistence.
- Baseline: 1037/1037 unique test nodes passed in exhaustive deterministic batches. Post-check: 1041/1041 unique test nodes passed in exhaustive deterministic batches.
- No DB schema, migration, public route, environment-variable or outcome-label-version change. Removed an accidentally packaged runtime-lock SQLite file from the release.

## 2026-07-13 — 1.0.44 — terminal execution-evidence finalization

- Fixed a HIGH live-validation fail-open defect: a stopped bot with any single execution event no longer counts as finalized exact PnL.
- Exact execution summaries now reconcile signed Buy/Sell quantities, expose `buy_qty`, `sell_qty`, `net_position_qty`, `position_flat`, `execution_ledger_complete`, and `total_pnl_finalized`.
- Live-validation and the `LIVE_VALIDATION_*` stop gate accept only stopped bots whose complete execution ledger is flat within a strict numeric tolerance.
- Partial/unmatched fills remain visible for audit but are excluded with machine-readable reasons such as `residual_position` or `execution_ledger_incomplete`.
- Added `tests/test_iteration232_execution_evidence_finalization.py` with three red-to-green checks and updated exact-evidence fixtures to use balanced opening/closing fills.
- No relational schema, migration, environment variable, public route removal, proxy outcome label, or calibrator identity changed.

## 2026-07-13 — 1.0.43 — uncertainty-bounded monetary expectancy gate

- Actionable `futures_grid` now requires a strictly positive one-sided 95% lower confidence bound for recency-weighted proxy return, not merely a positive sample mean.
- Added Kish effective sample size, weighted return standard deviation, lower-bound and confidence-level diagnostics to persisted calibration state and recommendation audit payloads.
- `unknown`, `insufficient`, and `uncertain` monetary evidence now produces explicit shadow `no_trade` code `PROXY_MONETARY_EXPECTANCY_UNPROVEN`; raw confidence cannot bypass missing evidence even when `REQUIRE_CONF_GATE=1`.
- Preserved confirmed negative expectancy as the stronger `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE` veto until new evidence has a positive lower bound.
- Bumped bot/global calibrator identities to `logreg_futures_grid_v8` / `logreg_global_v8`; direction calibration remains `platt_direction_v6`.
- Added `tests/test_iteration231_expectancy_uncertainty_gate.py` with nine red-to-green checks, including an end-to-end recommender assertion.
- Updated three legacy calibration tests whose tiny synthetic cohorts encoded the superseded “positive mean is sufficient” assumption while retaining their OOF/sanitization purpose.
- Baseline: 1025 passed. Post-check: 1034/1034 passed; SQLite fresh/upgrade and 24-test PostgreSQL offline subset passed; no schema, migration, API removal, or env changes.

## 2026-07-13 — 1.0.42 — stale calibrator fail-closed

- Исправлена HIGH model/risk fail-open ошибка: stale positive bot/global/direction calibrators больше не сохраняются бессрочно после исчезновения текущей retained outcome-выборки.
- После hourly refit состояние `insufficient` деактивирует положительные коэффициенты и перезаписывает cache, поэтому restart не восстанавливает unsupported calibrated confidence.
- Stale negative monetary expectancy остаётся консервативным `NO_TRADE` veto до появления новой положительной evidence-выборки.
- Calibrator keys обновлены до bot/global v7 и direction v6 для немедленной проверки текущих данных при upgrade; outcome labels не сбрасываются.
- Добавлен `tests/test_iteration230_stale_calibrator_fail_closed.py`: 3 red→green tests для bot, global и direction cache.
- Post-check: 1025 tests passed исчерпывающими непересекающимися batches; compileall и JavaScript syntax check passed. DB schema, migrations, API и env не менялись. Ruff отсутствовал; `pip check` сохранил внешний MoviePy/Pillow conflict.

## 2026-07-13 — 1.0.41 — independent shadow-outcome roots

- Исправлена HIGH-ошибка model validation: повторные `shadow_no_trade` публикации внутри одного label horizon больше не создают независимые outcome roots.
- Добавлена horizon-aware lineage для counterfactual no-trade rows: audit history сохраняется, но только первый root размечается outcome worker.
- Model identity обновлена до `bybit-taxonomy-v4-independent-shadow-roots`; bot/global calibrator keys — v6, direction key — v5, чтобы исключить ранее перекрывающуюся выборку.
- Новый regression suite `tests/test_iteration229_shadow_outcome_independence.py`: 6 tests; reproducer показывает 80 roots до fix и 1 root + 79 audit children после fix.
- Post-check: 1022 tests passed; compileall и JavaScript syntax check passed. DB schema, migrations, API и env не менялись. Live PostgreSQL integration не выполнялся без явно disposable DSN.

## 2026-07-13 - v1.0.40 - Monetary-expectancy calibration gate

- Confirmed a HIGH model/risk fail-open defect: LogReg/Platt optimized binary `success` while ignoring the magnitude stored in `reco_outcomes.ret`. An 80% hit-rate cohort with 160 x `+0.1%` and 40 x `-5%` had mean return `-0.92%`, yet v1.0.39 fitted an actionable probability model.
- Calibration now requires finite matured proxy returns, computes recency-weighted mean return plus 20% lower-tail expected shortfall, and refuses to fit when monetary expectancy is non-positive after the matured-return sample floor; class balance is checked separately for probability fitting.
- A persisted negative expectancy state is retained even though `fitted=false`; fresh cache loading cannot silently discard it.
- The recommender converts confirmed negative bot-specific proxy expectancy into `no_trade` code `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE`; deterministic hard blocks still take precedence.
- Bumped LogReg cache keys from v4 to v5 to prevent reuse of hit-rate-only coefficients. No DB schema, API, env, or outcome-label migration is required.
- Added `tests/test_iteration228_monetary_expectancy_calibration.py` and updated calibration fixtures to include explicit monetary returns.
- Post-check: `1016/1016` tests passed; compileall and Node syntax passed. Ruff was unavailable in the installed environment; `pip check` retained the unrelated MoviePy/Pillow environment conflict.

## 2026-07-12 - v1.0.39 - Tail-loss exact-evidence stop gate

- Fixed a HIGH/P0 fail-open defect in the exact-evidence execution stop gate.
- Negative cumulative realised net PnL after the predefined independent-bot sample floor now blocks direction/symbol/portfolio execution even when median PnL and win rate remain positive.
- Preserved the five-consecutive-loss guard, publication-root deduplication, explicit model-version scoping and exact-evidence-only input contract.
- Added `tests/test_iteration227_tail_risk_stop_gate.py` with an independent 7-small-wins/1-large-loss reproducer plus positive-total and minimum-sample controls.
- No database schema, API route, environment variable or outcome-label version changed.
- Post-check: 1008/1008 collected nodes passed through exhaustive non-overlapping batches (252+252+126+126+252); targeted regression 3/3 passed twice; relevant suite 13/13 and DB/dialect subset 33/33 passed. The monolithic run timed out after 92% without a final summary and is not counted as a pass.
- `ruff`/`pip check` environment limitations are reported in the iteration audit rather than hidden.

## 2026-07-12 - v1.0.38 - Outcome dependency diagnostics

- Distinguished transient missing funding settlements from permanently invalid grid contracts.
- Added `OUTCOME_WAIT_FUNDING_SETTLEMENT` with `reason`, missing settlement timestamp and position slots.
- Added structured reasons for invalid funding aliases, grid-count conflicts, range/kill-switch geometry and entry/range mismatch.
- Added per-recommendation cooldown for unavailable-outcome diagnostics.
- Kept `OUTCOME_LABEL_VERSION=grid_label_v18`; no outcome/calibrator reset is required for this patch.
- Added `tests/test_iteration226_outcome_wait_diagnostics.py` (4 regression tests).
- Post-check: 1005/1005 tests passed through four non-overlapping batches; PostgreSQL/dialect/write-retry subset 20/20 passed.
- `ruff` unavailable; global `pip check` retains the pre-existing MoviePy/Pillow conflict.

## 2026-07-12 - v1.0.37 - Settled funding outcome integrity

- Added public Bybit funding-history parsing and a paginated 35-day settlement backfill.
- Added additive SQLite/PostgreSQL `funding_settlement` persistence and range queries.
- Historical grid outcomes now use signed settled funding cashflows; ticker forecasts remain approval-only.
- Missing settlement data blocks a non-flat outcome instead of fabricating P&L from a forecast.
- Bumped FastAPI to `1.0.37` and outcome target to `grid_label_v18`.
- Added `test_iteration225_settled_funding_outcomes.py`; post-check: 1001/1001 tests passed.

## 2026-07-12 - v1.0.36 - Grid cost-layer separation

- Fixed a HIGH economics/outcome defect: spread, slippage and full-horizon funding are no longer charged to every completed grid pair.
- Added explicit `grid_round_trip_fee_bps`, `one_time_market_friction_bps` and `market_round_trip_cost_bps` layers while retaining the legacy market-cost alias for compatibility.
- Grid spacing, density, publication economics and live gross/fee coverage now use only recurring two-fill grid fees; live spread, terminal friction and adverse funding remain separate fail-closed gates/stresses.
- Outcome ledger charges market friction only to initial directional market inventory and terminal residual liquidation; resting grid fills pay their own fee legs, and funding remains inventory/time based.
- Bumped FastAPI to `1.0.36` and outcome target to `grid_label_v17`; no API route, database schema or environment variable changed.
- Added `tests/test_iteration224_grid_cost_layer_separation.py`; post-check collected 992 tests and passed all 992 through exhaustive non-overlapping batches.

## 2026-07-12 - v1.0.35 - Bybit cross-margin safety contract

- Fixed a CRITICAL exchange-contract defect: Bybit Futures Grid Bot is now modelled as `account_mode=unified`, `margin_mode=cross`, `position_mode=one_way`; generated and legacy `isolated` payloads no longer masquerade as executable Grid Bot plans.
- Replaced the standalone isolated-position liquidation-price gate with deterministic cross-margin bot-equity stress at both kill-switch boundaries. The stress includes exact grid commitment, leverage, adverse inventory loss, execution cost and maintenance reserve, while crediting neither funding receipts nor hypothetical grid profit.
- Aligned generator, leverage selection, operator decision context, strict execution preflight and frontend risk labels on one cross-margin contract.
- Preserved the generic isolated liquidation helper only as a non-Grid utility; no production Futures Grid path calls it.
- Bumped FastAPI to `1.0.35` and outcome target to `grid_label_v16`; no API route, database schema or environment variable changed.
- Added `tests/test_iteration223_bybit_cross_margin_contract.py`: 8/8 RED on v1.0.34 and 8/8 GREEN after the fix.
- Baseline: 977/977 passed. Post-fix working suite: 985/985 passed before release-document synchronization.

## 2026-07-12 - v1.0.34 - neutral opening-order margin reservation

- Fixed a CRITICAL neutral-grid sizing defect: every initial Buy and Sell order is opening/margin-bearing because NEUTRAL starts flat; commitment now sums both initial opening stacks instead of reserving only the larger side.
- Kept one-way maximum position separate: `max_abs_position_slots` remains the larger directional stack, while `committed_slot_count` covers all initial opening orders.
- Aligned recommender sizing, Bybit metadata snap, strict preflight, runtime limits and outcome return normalization on the corrected commitment contract.
- Rejected legacy max-side neutral payloads fail-closed through topology parity checks.
- Bumped FastAPI to `1.0.34` and outcome target to `grid_label_v15`; no API-route, database-schema or environment-variable change.
- Added `tests/test_iteration222_neutral_full_opening_commitment.py`: 8 independent checks; 7 RED failures on v1.0.33 and 8/8 GREEN after the fix.
- Full post-check: 977/977 tests passed. Previous v1.0.32 documentation/tests asserting max-side reservation are explicitly superseded rather than silently retained.

## 2026-07-12 - v1.0.33 - dynamic off-grid bridge topology

- Corrected arithmetic-grid initial topology when reference lies between levels: N intervals create N+1 prices but exactly N initial orders, with one adjacent bridge level intentionally idle.
- Removed the phantom bridge order and matching excess LONG/SHORT initial inventory lot from sizing, auto-snap, strict preflight, runtime caps, daily-loss fallback and OHLCV outcome ledger.
- Preserved neutral one-way commitment while recalculating the larger opening stack from the actual dynamic order set.
- Added `idle_grid_index` to canonical commitment diagnostics and fail-closed parity tests.
- Bumped FastAPI to `1.0.33` and outcome target to `grid_label_v14`; no route, database-schema or environment-variable change.
- Added `tests/test_iteration221_off_grid_bridge_topology.py`: 8 RED failures on v1.0.32, 8 GREEN after the fix.
- Baseline: 961/961 passed. Post-check: 969/969 passed. `ruff` unavailable; global `pip check` retains an unrelated MoviePy/Pillow environment conflict.

## 2026-07-12 - v1.0.32 - neutral one-way commitment integrity

- Fixed neutral arithmetic-grid commitment: opposite resting Buy/Sell stacks remain active, but one-way capital reservation uses the more expensive directional opening stack instead of summing mutually exclusive sides.
- Separated `estimated_active_orders`, `estimated_committed_slots`, and `estimated_max_position_slots` across generated sizing/economics, auto-snap, strict preflight, runtime caps and daily-loss fallback.
- Corrected neutral proxy-return denominator from both-side notional to one-way committed investment; LONG/SHORT commitment is unchanged.
- Added fail-closed preflight checks for malformed or topology-inconsistent committed/max-position slot fields.
- Bumped FastAPI to `1.0.32` and outcome target to `grid_label_v13`; no route, database-schema or environment-variable change.
- Added `tests/test_iteration220_neutral_one_way_commitment.py`: 8 RED-to-GREEN regressions.
- Baseline: 953/953 passed. Post-check: 961/961 passed; PostgreSQL dialect/locking 18/18; SQLite fresh/repeated bootstrap 16/16.

## 2026-07-12 - v1.0.31 - grid order quantity and gap-stop integrity

- Fixed a CRITICAL directional-ledger defect: multiple equal-quantity lots resting at the same grid price are now aggregated instead of collapsed into one side-only order.
- Corrected repeated LONG/SHORT cycle PnL, per-leg execution friction and inventory-at-funding after adjacent replacement orders merge with an initial directional TP.
- Close-to-open or horizon gaps that jump beyond a kill-switch are now unlabelable; the proxy no longer assumes execution at a skipped protective boundary.
- Corrected the daily-loss fallback derivation to use canonical active-order topology (`N` on-grid, `N+1` off-grid) rather than unconditional `grid_count`.
- Added `tests/test_iteration219_grid_order_quantity_and_gap_stop.py` with 8 RED-to-GREEN regressions.
- Bumped FastAPI to `1.0.31` and outcome target to `grid_label_v12`; no route, schema or environment-variable change.
- Baseline: 945/945 passed. Final post-check after documentation synchronization: 953/953 passed; SQLite and PostgreSQL dialect checks passed.

## 2026-07-12 - v1.0.30 - exact grid commitment and path ambiguity

- Added one canonical arithmetic-grid commitment model for recommender sizing, auto-snap, strict preflight, runtime caps and outcome normalization.
- Corrected off-grid arithmetic geometry: `grid_count=N` intervals can expose `N+1` active price levels; directional commitment includes initial inventory plus adverse-side opening orders.
- Corrected `estimated_total_order_notional`, margin and worst-case notional; old `N × reference × qty` payloads are rejected when inconsistent with the persisted range/reference/direction.
- Replaced fabricated two-sided OHLC sequencing with dual-path simulation. If `O-H-L-C` and `O-L-H-C` produce different ledger/stop/PnL states, the proxy outcome is unavailable.
- Added `tests/test_iteration218_grid_commitment_and_path_ambiguity.py` (9 RED→GREEN cases) and updated stale tests that encoded the old capital oracle.
- Bumped FastAPI to `1.0.30` and outcome target to `grid_label_v11`; no route, schema or environment-variable change.
- Post-check: 945 collected, 945 passed; PostgreSQL dialect/locking and SQLite bootstrap checks passed.

## 2026-07-12 - v1.0.29 - grid ledger topology and protective-stop finalization

- Fixed non-grid-line LONG/SHORT initialization: the nearest adjacent TP order and its matching initial directional slot are no longer skipped.
- Split observable minute movement into previous-close -> open and open -> close segments; count single-sided intraminute excursions only when OHLC chronology is unambiguous.
- Made kill-switch breach terminal: fills stop at the protective boundary, residual inventory is liquidated there, and later candles/funding cannot repair the outcome.
- Reject missing/inside-range kill-switch geometry and dual-boundary intrabar ambiguity instead of fabricating a label.
- Bumped FastAPI to `1.0.29` and outcome target to `grid_label_v10`; no route, schema, frontend or environment-variable change.
- Added `tests/test_iteration217_grid_ledger_topology_and_stop.py`: 9 tests, RED 8 failures on v1.0.28, GREEN 9/9.
- Baseline: 927/927 passed. Post-check: 936/936 passed.

## 2026-07-12 - v1.0.28 - post-publication entry and grid-contract integrity

- Outcome entry now uses the first exact 1m candle open strictly after recommendation publication, preventing historical pre-publication fills when the recommender cycle is delayed.
- Invalid or contradictory grid contracts are no longer persisted as flat/loss outcomes: conflicting grid-count aliases, conflicting ranges and malformed explicit range fields are unlabelable.
- Duplicated funding blocks must form one internally consistent model; invalid or conflicting aliases cannot be merged field-by-field into synthetic carry.
- Bumped `OUTCOME_LABEL_VERSION` to `grid_label_v9` and FastAPI version to `1.0.28`; no schema, route, frontend or environment-variable change.
- Added `tests/test_iteration216_outcome_entry_contract_integrity.py` with 10 RED-to-GREEN regressions; post-check: 927 tests passed.
- Updated operator Markdown, DOCX/PDF and PNG artifacts. Live PostgreSQL integration was not run without a confirmed disposable DSN; dialect/locking tests remain green.

## 2026-07-12 - v1.0.27 - outcome label integrity and funding-window precedence

- Fixed a HIGH label-integrity defect: every finite positive liquidation-equivalent total net PnL is now a win unless the kill-switch was breached. The residual mode-activity/0.1% drift gate no longer converts positive LONG, SHORT or NEUTRAL outcomes into losses.
- Fixed a HIGH phantom-funding defect: an exact known schedule with zero events inside the horizon charges zero; stale aggregate `expected_funding_events` is used only when the exact schedule is unavailable.
- Fixed a HIGH cost-alias defect: duplicated outcome cost models resolve to the maximum valid non-negative execution cost, so zero/boolean/malformed aliases cannot hide a stricter cost.
- Hardened temporal input: malformed OHLC geometry makes the 1m horizon incomplete rather than materializing a fabricated loss.
- Bumped `OUTCOME_LABEL_VERSION` to `grid_label_v8` and FastAPI version to `1.0.27`; no schema, route, frontend or environment-variable change.
- Added `tests/test_iteration215_outcome_label_integrity.py` with nine checks (eight RED on v1.0.26, one preservation check). Baseline: 908 passed. Post-check: 917 passed.

## 2026-07-12 - v1.0.26 - inventory-aware total-PnL finalization

- Fixed a HIGH outcome-cost defect: residual LONG/SHORT inventory now pays the missing terminal close leg, so open and fully closed horizon outcomes are compared on the same liquidation-equivalent net basis.
- Fixed a CRITICAL funding-accounting defect: adverse funding is charged against actual position value at each persisted event instead of subtracting aggregate funding bps from the full grid capital. Neutral/no-inventory paths pay zero; possible receipts remain excluded from alpha.
- Fixed a HIGH classification defect: positive net total PnL below the undocumented 5 bps threshold is no longer stored as a loss. Success requires mode activity, intact kill-switch geometry and net PnL above numerical epsilon.
- Added strict raw-rate/schedule parsing and a conservative unknown-schedule fallback based on maximum adverse inventory actually reached by the ledger.
- Bumped `OUTCOME_LABEL_VERSION` to `grid_label_v7`; startup resets only incompatible proxy outcomes/calibrators. No schema, API route, frontend or environment-variable change.
- Added `tests/test_iteration214_total_pnl_finalization.py` with eight independent red-to-green regressions and minimally updated four prior tests that encoded the old label version or omitted terminal-close costs. Post-check: 908/908 passed in four disjoint 227-test batches; compileall and Node syntax passed. Ruff was unavailable; `pip check` retained the unrelated global MoviePy/Pillow mismatch.

## 2026-07-12 - v1.0.25 - exact grid PnL ledger and interval economics

- Fixed a CRITICAL outcome defect: LONG/SHORT/NEUTRAL proxy PnL is now calculated from an explicit equal-quantity arithmetic-grid order/inventory ledger instead of a coarse paired-move count plus end-of-horizon drift penalty.
- Fixed a HIGH recommendation-economics defect: a completed grid pair earns the full adjacent interval; `fill_efficiency=0.70` is diagnostic projected opportunity capture and no longer haircuts realised gross/net profit, TP distance, cost floor or live gross-edge coverage.
- Fixed HIGH directional/neutral accounting defects: initial directional inventory, replacement orders, actual grid fill prices, per-leg execution cost and marked residual position are included; one profitable neutral pair can be successful and favourable directional movement is not forced to zero.
- Fixed a HIGH geometry-lineage defect: outcome labels use the persisted range and exact integer `grid_count`; stale `grid_spacing_pct` and cost-derived widening cannot silently rewrite historical grid geometry.
- Bumped `OUTCOME_LABEL_VERSION` to `grid_label_v6`; startup resets only incompatible proxy outcomes/calibrators while preserving recommendations, bot instances, trades and exact execution evidence. No schema, route or environment-variable change.
- Added `tests/test_iteration213_grid_pnl_ledger.py` with nine independent red-to-green checks and updated historical tests that encoded the invalid 70% haircut or drift proxy. Post-check: 900 tests passed; compileall and Node syntax passed. Ruff was unavailable; `pip check` retained an unrelated global MoviePy/Pillow mismatch.

# Changelog

## 2026-07-12 - v1.0.24 - grid outcome accounting and cohort statistics

- Fixed a CRITICAL directional proxy-PnL defect: favourable LONG/SHORT end-of-horizon movement is no longer subtracted as a loss; signed mark-to-market is applied only to estimated residual inventory.
- Fixed HIGH grid-accounting defects: repeated completed trades are no longer capped by `grid_count`, the first candle move is measured from entry/open, and each inferred completed arithmetic trade uses the full interval minus one round-trip execution cost.
- Fixed a HIGH neutral-grid defect: zero completed trades no longer incur a phantom fee or full-capital drift loss; neutral no-fill/no-inventory paths return zero.
- Added additive `cohorts.all_roots`, `cohorts.actionable` and `cohorts.shadow_no_trade` summaries. The operator headline now uses actionable outcomes; combined/shadow samples remain explicitly research controls.
- Bumped `OUTCOME_LABEL_VERSION` to `grid_label_v5`; first startup resets incompatible proxy outcomes/calibrators while preserving recommendations, bot audit rows, trades and exact execution evidence. No schema, route removal or environment-variable change.
- Added `tests/test_iteration212_grid_outcome_accounting.py` with seven red-to-green cases; corrected two old tests that encoded the wrong favourable-direction sign and one geometry-inconsistent fixed oracle.
- Updated README, trading/risk docs, operator DOCX/PDF and root infographic. Input runtime DB/lock artifacts are excluded from the release ZIP.
- Baseline: 884/884 passed in exhaustive batches. Post-check: 891/891 passed in three disjoint 297-test batches; PostgreSQL dialect/locking 18 passed; SQLite fresh/repeated and existing-copy init passed. Ruff unavailable; `pip check` retains the unrelated global MoviePy/Pillow conflict.

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
