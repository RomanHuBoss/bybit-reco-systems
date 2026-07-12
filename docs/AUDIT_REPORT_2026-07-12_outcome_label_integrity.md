# Audit iteration: outcome label integrity and exact funding-window precedence

## 1. Iteration identity

- Input ZIP: `bybit-reco-systems-1.0.26-total-pnl-finalization.zip`
- Input SHA-256: `6cc3cb8f95fb54512c953b4a14344a9376459360ba7cdd6ee0d6d3d61f8f048e`
- Source version: `1.0.26`
- Release version: `1.0.27`
- Source outcome contract: `grid_label_v7`
- Release outcome contract: `grid_label_v8`
- Regression package: `tests/test_iteration215_outcome_label_integrity.py`
- Date: 2026-07-12

## 2. Project fingerprint

The archive matched the expected Bybit Recommender fingerprint:

- `README.md`, `CHANGELOG.md`, `requirements*.txt`, `main.py` present;
- FastAPI application in `app/main.py`;
- canonical directional semantics in `app/trading_semantics.py`;
- outcome implementation in `app/outcomes.py`;
- frontend in `app/ui/static/`;
- SQLite and PostgreSQL support present;
- bot scope remains `futures_grid`, Bybit `linear`, USDT perpetual;
- recommendation/audit-only boundary remains intact;
- no private order-create/amend/cancel endpoint was found in production code.

Archive safety inspection found one project root, no absolute paths, no `../` traversal, no external symlinks, no duplicate/conflicting entries and no nested archive requiring extraction.

## 3. Goal

After this iteration, an outcome must be labelled by the sign of its liquidation-equivalent net PnL, exact funding timestamps must take precedence over stale aggregate event estimates, duplicate cost aliases must not conceal a stricter valid execution cost, and malformed OHLC rows must not mature an outcome.

## 4. Acceptance criteria

1. Positive finite net PnL is `success=1` unless a kill-switch was breached.
2. Small profitable LONG and SHORT outcomes are not rejected by an unrelated movement threshold.
3. A profitable NEUTRAL residual position is not rejected merely because no complete adjacent pair closed.
4. A known exact funding schedule with no event inside the horizon charges zero funding.
5. An exact event inside the horizon still charges adverse funding against actual inventory.
6. Conflicting duplicate cost models resolve conservatively to the maximum valid execution cost.
7. Boolean, malformed and non-finite cost aliases cannot hide a valid stricter cost.
8. An impossible OHLC geometry makes the 1-minute horizon incomplete.
9. Outcome target is bumped to `grid_label_v8`; incompatible proxy outcomes/calibrators are not mixed.

All nine criteria are covered by the new regression file.

## 5. Sources read

The iteration reviewed the current executable code and relevant contracts, including:

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- the latest outcome audit reports through v1.0.26;
- `app/main.py`, `app/outcomes.py`, `app/grid_math.py`, `app/recommender.py`, `app/trading_semantics.py`, `app/db.py`, `app/db_backend.py`;
- outcome, funding, temporal-lineage and persistence regression tests;
- official Bybit futures-grid PnL and funding documentation for external semantic comparison.

Code and executable tests were treated as runtime evidence, not as proof that the existing expectation was correct.

## 6. Affected data flow

`recommendation.params / trade_plan`
→ cost and funding extraction
→ strict OHLC horizon validation
→ arithmetic-grid inventory ledger
→ exact funding-event application or conservative unknown-schedule fallback
→ terminal liquidation-equivalent PnL
→ `success` and `ret`
→ `reco_outcomes`
→ calibration/statistics cohorts.

The API, recommendation lifecycle, execution preflight and database schema were not changed.

## 7. Baseline environment and results

- Python: `3.13.5`
- Node: `v22.16.0`
- Baseline version: `1.0.26`
- Baseline test collection: `908`
- Baseline full suite: `908 passed in 25.13s`
- `python -m compileall -q app tests main.py`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- `python -m pip check`: environment conflict only — MoviePy 2.2.1 requires Pillow `<12`, environment has Pillow 12.2.0
- Ruff: UNAVAILABLE in the environment

The baseline was green, so the defects were outside the prior regression contracts rather than ordinary pre-existing test failures.

## 8. Confirmed defects

### OUT-215-01 — positive net PnL could be labelled as failure

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- File/function: `app/outcomes.py`, `_grid_outcome`
- Violated invariant: `success` must represent the sign of final net outcome, while kill-switch remains an independent veto.

Actual v1.0.26 behavior used a second activity gate:

- directional outcome required approximately 0.1% movement;
- neutral outcome required a completed adjacent pair;
- therefore `ret > 0` and `success=0` could coexist.

Independent reproductions:

- LONG `100 → 100.08`: `ret=+0.0004`, `success=0`;
- SHORT `100 → 99.92`: `ret=+0.0004`, `success=0`;
- NEUTRAL sell at 101, terminal mark/close at 100.5: `ret=+0.0025`, `success=0`.

Financial/model impact: positive observations were inserted into the losing class, lowering win rate and contaminating calibration targets. This could make a viable configuration appear systematically worse and could also distort class probabilities.

Why old tests missed it: previous tests covered larger moves and complete neutral pairs, not positive outcomes below the hidden activity threshold.

Fix: remove the unrelated mode-activity gate. `success=1` now requires finite liquidation-equivalent net PnL above numerical epsilon and no kill-switch breach.

Residual risk: OHLCV outcomes remain a proxy and do not reconstruct exact exchange fills.

### OUT-215-02 — phantom funding after the outcome horizon

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- File/function: `app/outcomes.py`, funding schedule handling inside `_grid_outcome`
- Violated invariant: exact event time has precedence over aggregate estimates.

Input:

- exact `next_funding_ts` one hour after a two-minute horizon;
- valid interval and rate;
- stale `expected_funding_events=1` retained in the payload.

Actual v1.0.26 behavior: no exact event was generated inside the window, then the code fell back to the stale aggregate estimate and charged `-0.0005`.

Expected behavior: a known exact schedule with no in-window event charges zero.

Financial/model impact: flat or profitable positions could become artificial losses solely because an out-of-window event estimate overrode known timing.

Why old tests missed it: they tested exact in-window funding and unknown-schedule fallback, but not the known-schedule/no-event boundary.

Fix: introduce explicit `exact_schedule_known`. Aggregate `expected_funding_events` fallback is used only when exact scheduling is unavailable, not when an exact schedule exists but produces zero in-window events.

Preservation test: an exact in-window event still charges adverse funding against actual open inventory.

### OUT-215-03 — duplicate cost alias could hide a stricter cost

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- File/function: `app/outcomes.py`, `_extract_cost_components`
- Violated invariant: contradictory aliases cannot make economics less conservative.

Reproductions:

- `params.cost_model.execution_cost_bps=0` plus `trade_plan.cost_model.execution_cost_bps=20` resolved to zero;
- boolean primary value plus valid 20 bps secondary value resolved to the generic 15 bps fallback rather than the valid 20 bps.

Financial/model impact: fees/slippage could be understated and losing outcomes could be recorded as profitable.

Why old tests missed it: they validated individual blocks and malformed single values, not contradictory duplicate sources.

Fix:

- inspect every supported cost block;
- reject boolean, non-finite and negative numerics;
- select the maximum valid execution/net cost;
- select the most adverse valid signed funding candidate;
- preserve legacy decomposition only when required by the stored payload.

This is fail-closed for economics without changing field names or API schema.

### OUT-215-04 — malformed OHLC geometry could mature a label

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- File/functions: `app/outcomes.py`, `_has_complete_1m_window`, `_grid_outcome`
- Violated invariant: malformed market data must not produce an actionable/statistical result.

A legacy/manual row with `high < open` and `high < close` still satisfied the previous completeness check because only timestamps and row count were considered.

Impact: poisoned rows could fabricate fills, PnL and labels, contaminating calibration and historical statistics.

Fix: add strict candle validation for finite positive OHLC values and geometry:

- `high >= max(open, close, low)`;
- `low <= min(open, close, high)`.

Invalid rows make the horizon incomplete and are also rejected inside the ledger path.

## 9. Unconfirmed claim

The claim that the strategy is intrinsically unprofitable was **not confirmed**. The release ZIP did not contain the user’s active monthly SQLite database or the screenshot/data series that produced the observed statistics. Therefore the actual historical sample could not be recalculated under `grid_label_v8`.

The confirmed defects are sufficient to invalidate direct comparison of prior v7 win-rate/calibration with new v8 outcomes, but they do not establish a positive edge.

## 10. Implementation summary

### Production

- `app/outcomes.py`
  - sign-consistent success classification;
  - exact funding-window precedence;
  - conservative duplicate cost-alias resolution;
  - strict OHLC geometry validation;
  - small internal cleanup to signed `position_slots` terminology.
- `app/main.py`
  - FastAPI version `1.0.27`;
  - `OUTCOME_LABEL_VERSION="grid_label_v8"`.

### Tests

- Added `tests/test_iteration215_outcome_label_integrity.py` with nine independent checks.
- Updated only stale version-contract assertions in iterations 209, 211, 213 and 214.

### Documentation and operator artifacts

Updated:

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- `docs/instrukciya_operatora_bybit_recommender.pdf`;
- `how_to_trade.png`.

The DOCX/PDF are five pages and the embedded v1.0.27 infographic was visually inspected after regeneration.

## 11. RED → GREEN evidence

RED command on pristine v1.0.26 plus only the new test:

```bash
python -m pytest -q tests/test_iteration215_outcome_label_integrity.py
```

RED result:

```text
8 failed, 1 passed in 0.50s
```

Representative failures:

```text
LONG +4 bps return: success 0 instead of 1
SHORT +4 bps return: success 0 instead of 1
NEUTRAL +25 bps residual return: success 0 instead of 1
known schedule/no in-window funding: -0.0005 instead of 0
cost aliases 0 vs 20 bps: selected 0 instead of 20
boolean alias vs 20 bps: selected 15 instead of 20
malformed candle: horizon complete instead of incomplete
missing grid_label_v8 / version 1.0.27
```

GREEN command on working v1.0.27:

```bash
python -m pytest -q tests/test_iteration215_outcome_label_integrity.py
```

GREEN result, repeated deterministically:

```text
9 passed in 0.38s
9 passed in 0.40s
```

Relevant outcome/funding suite:

```text
58 passed in 1.39s
```

## 12. Database and compatibility

- No database schema change.
- `migrations/init.sql` unchanged.
- `migrations/init_postgres.sql` unchanged.
- Fresh SQLite bootstrap: 16 application tables.
- Repeated SQLite bootstrap: 16 application tables; idempotent.
- PostgreSQL translation/locking/deadlock regression package: 24 passed.
- Live PostgreSQL integration: SKIPPED because no explicitly disposable test DSN was supplied.

At first v1.0.27 startup, the existing label-version guard removes only incompatible proxy `reco_outcomes` and associated calibrators. It preserves recommendations, bot instances, trades, exact execution evidence and risk settings.

## 13. API, configuration and security compatibility

- API routes and response fields: unchanged.
- Frontend contract: unchanged.
- Environment variables: unchanged.
- Database schema: unchanged.
- Recommendation/audit-only boundary: unchanged.
- No private Bybit order create/amend/cancel endpoint added.
- No credentials, `.env`, runtime database or lock database is included in the release ZIP.

## 14. Post-check results

- Test collection: `917 tests collected in 1.00s`
- Full test suite: `917 passed in 23.86s`
- New regression, run 1: `9 passed in 0.38s`
- New regression, run 2: `9 passed in 0.40s`
- Relevant outcome/funding suite: `58 passed in 1.39s`
- PostgreSQL dialect/locking/deadlock suite: `24 passed in 1.97s`
- SQLite fresh/repeated bootstrap: `16 / 16` application tables
- `compileall`: PASSED
- frontend JavaScript syntax: PASSED
- private order endpoint scan: none found
- DOCX visual check: PASSED, five pages
- PDF visual check: PASSED, five pages
- `pip check`: FAILED only for the pre-existing host MoviePy/Pillow conflict
- Ruff: UNAVAILABLE

## 15. What could not be verified

- The user’s actual month of outcomes could not be recomputed because the active runtime DB was not included.
- Live PostgreSQL behavior was not exercised without a safe disposable DSN.
- Real Bybit fills, queue priority, partial fills, maker/taker mix and exact intrabar order remain outside OHLCV proxy evidence.
- Future funding rates cannot be known from historical snapshots; unknown schedules remain conservatively estimated.
- Passing tests and corrected accounting do not prove live profitability.

## 16. Residual risks

1. Close-to-close one-minute inference cannot determine the true order of multiple intrabar level touches.
2. Cost selection is intentionally conservative; duplicated stale high-cost aliases may understate performance until payload lineage is cleaned.
3. Calibration requires a new chronologically independent v8 sample; old v7 targets must not be pooled.
4. Statistical viability must be evaluated separately by symbol, direction, regime and actionable cohort, with exact execution evidence where available.

## 17. Rollback

1. Stop the application.
2. Restore the v1.0.26 code.
3. Restore the `data/app.db` backup made before the first v1.0.27 startup if old v7 proxy outcomes/calibrators must be retained.
4. Do not restore a stale runtime-lock database.

## 18. Recommended next work package

After enough `grid_label_v8` observations accumulate, perform a locked chronological validation package:

- reconcile proxy outcomes with exact execution evidence;
- calculate net expectancy and confidence intervals by symbol/direction/regime;
- separate actionable from shadow cohorts;
- test stability after fees and adverse funding;
- determine whether the strategy has an out-of-sample edge or should be disabled.

## 19. Final release verification

The clean release was built with exactly one root directory, `bybit-reco-systems-main`.

- archive integrity (`unzip -t`): PASSED;
- release file count: 238;
- `.env`, credentials, bytecode, caches, runtime DB and runtime-lock DB: absent;
- project fingerprint after re-extraction: PASSED;
- `compileall` from re-extracted archive: PASSED;
- frontend JavaScript syntax from re-extracted archive: PASSED;
- iteration215 from re-extracted archive: `9 passed`;
- collection from re-extracted archive: `917 tests`;
- report and DOCX/PDF/PNG operator artifacts: present.
