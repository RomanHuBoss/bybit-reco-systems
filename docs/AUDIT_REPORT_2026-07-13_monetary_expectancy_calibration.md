# Bybit Recommender v1.0.40 - Monetary-expectancy calibration gate

## 1. Iteration title

Monetary-expectancy calibration gate: prevent binary hit rate from approving a losing futures-grid cohort.

## 2. Input ZIP

`bybit-reco-systems-1.0.39-tail-loss-stop-gate.zip`

The original user attachment was v1.0.38. This iteration intentionally starts from the already verified v1.0.39 tail-loss-stop release produced in the preceding audit, so that the earlier fix is retained.

## 3. Input SHA-256

`1a44bf25466140b17aadb43c34fc756f815bdbb6c1f3d851a791e007062c73ab`

## 4. Source version

`1.0.39`, from the FastAPI `version=` value in `app/main.py`.

## 5. New version

`1.0.40` - patch release. Public API, DB schema, environment variables and `OUTCOME_LABEL_VERSION=grid_label_v18` are unchanged.

## 6. Project fingerprint

Matched Bybit Recommender:

- required root files and modules present;
- `futures_grid` only;
- Bybit `category=linear`, USDT perpetual scope;
- recommendation/audit service, not OMS/EMS;
- SQLite and PostgreSQL support retained;
- FastAPI app in `app/main.py`;
- frontend in `app/ui/static/`;
- directional source of truth in `app/trading_semantics.py`;
- no private Bybit order create/amend/cancel endpoint found.

Inventory before this iteration:

- 23 top-level production Python files;
- 171 test files;
- 51 documentation files;
- 3 frontend files;
- 2 migration SQL files;
- highest prior iteration test: 227.

## 7. Goal

After this iteration, a matured bot-specific proxy cohort with non-positive monetary expectancy must not fit or supply an actionable probability model, even when its binary win rate is high. The negative state must survive persistence and must produce an explicit strategy `no_trade` reason.

## 8. Acceptance criteria

1. An 80% hit-rate cohort with mean return `-0.92%` does not fit LogReg/Platt.
2. A 95% hit-rate cohort with rare large losses is still evaluated by the monetary gate before class-balance rejection.
3. A positive monetary cohort remains eligible for the existing probability-calibration checks.
4. Missing, boolean and non-finite `ret` values are not accepted as monetary observations.
5. Negative expectancy state round-trips through `app_config` and loads while `fitted=false`.
6. A stale negative state can be replaced by current positive monetary evidence; the gate is not permanently latched.
7. Negative bot-specific expectancy emits `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE` and becomes `no_trade` unless a harder block applies.
8. Full regression suite remains green and the release archive passes clean re-extraction checks.

## 9. Sources read

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- recent audit reports, especially tail-loss stop, no-recommendation shadow outcomes, mean-reversion edge, temporal lineage and grid outcome accounting;
- `app/calibration.py`, `outcomes.py`, `recommender.py`, `db.py`, `db_backend.py`, `main.py`, `risk.py`, `grid_math.py`, `trading_semantics.py`;
- calibration, temporal, persistence, PostgreSQL and release-artifact tests;
- supplied project-specific iteration procedure.

## 10. Affected data flow

`reco_outcomes.ret` and `success` -> `db.get_outcomes_with_recs()` -> current-model and feature-schema filter -> `calibration.fit_logreg()` -> v5 `LogRegScaler` expectancy state -> `app_config` persistence -> `_load_or_fit_bot_logregs()` -> `_calibration_expectancy_no_trade_reason()` -> recommendation `no_trade` status and `reasons.confidence_model` diagnostics.

## 11. Baseline environment

- Python: `3.13.5`
- Node: `v22.16.0`
- DB live integration: no disposable PostgreSQL DSN supplied; dialect/translation tests only.
- No production credentials or runtime DB used.

## 12. Baseline commands and results

From pristine v1.0.39:

- `python -m pip check` - **FAILED**, unrelated environment conflict: MoviePy 2.2.1 requires Pillow `<12`, installed Pillow 12.2.0.
- `python -m compileall -q app tests main.py` - **PASSED**.
- `python -m ruff check .` - **UNAVAILABLE**, `No module named ruff`.
- `node --check app/ui/static/app.js` - **PASSED**.
- `python -m pytest -q` - **1008 passed**, no failures.

The ZIP contains no authoritative runtime trade database. Therefore live profitability, drawdown and exact fill economics cannot be inferred from the release artifact.

## 13. Confirmed defects and gaps

### ME-001 - Binary success calibration ignored monetary magnitude

- Severity: **HIGH**
- Type: **CONFIRMED DEFECT / model-risk fail-open**
- Files: `app/calibration.py`, `app/recommender.py`
- Functions: `fit_logreg`, `_fit_bot_logregs`, `_load_or_fit_bot_logregs`, recommendation calibration branch
- Input reproducer: 200 matured rows; 160 wins with `ret=+0.001`, 40 losses with `ret=-0.05`.
- Binary result: win rate `80%`.
- Monetary result: arithmetic mean `-0.0092` (`-0.92%`).
- v1.0.39 behavior: fitted score-only Platt model with approximately 0.78-0.80 success probability; `ret` was not read by `fit_logreg`.
- Expected behavior: non-positive matured monetary evidence must not support an actionable probability model.
- Violated invariants: fail-closed economics; proxy calibration must not turn negative economics into positive confidence; tail magnitude must not be discarded.
- Financial impact: a frequent-small-win / rare-large-loss grid could be promoted by confidence while losing money in aggregate.
- Trading/risk impact: recommendation publication could remain actionable before enough exact live evidence accumulated for the v1.0.39 stop gate.
- Model/data impact: optimization target and operator objective were misaligned; classification accuracy/hit rate was treated as a substitute for monetary utility.
- Why tests missed it: existing calibration fixtures supplied `success` and checked class balance/temporal leakage, but generally omitted `ret`; no independent asymmetric-payoff oracle existed.

Relevant corrected code:

- expectancy state: `app/calibration.py:331-349`;
- weighted mean and 20% lower-tail expected shortfall: `app/calibration.py:399-445`;
- strict return sanitation and monetary gate before class-balance gate: `app/calibration.py:650-752`;
- persistence: `app/calibration.py:854-958`;
- v5 cache keys: `app/calibration.py:995-1004`;
- no-trade policy and cache lifecycle: `app/recommender.py:3080-3191`;
- publication use: `app/recommender.py:3903-3906`;
- UI/API diagnostics payload: `app/recommender.py:4222-4232`.

### ME-LIM-001 - Profitability cannot be proven from the release ZIP

- Severity: **HIGH informational limitation**
- Type: **DOCUMENTED LIMITATION**
- Evidence: no runtime DB, exact fills, complete fee/funding ledger or capital curve is shipped.
- Consequence: neither “profitable” nor “apriori unprofitable” is a defensible conclusion from code and proxy outcomes alone.
- Required evidence: prospective exact-fill walk-forward with net PnL, exposure-normalized return, drawdown, expected shortfall, turnover/cost attribution and a no-trade/simple-grid benchmark.

## 14. Unconfirmed claims

- “The whole project is apriori unprofitable” - **not proven**. The release lacks authoritative execution data, and market edge is an empirical property, not derivable from test pass count.
- “This was the only critical defect” - **not claimed**. This iteration closes one independently reproducible objective-mismatch package.
- “Positive v5 proxy expectancy proves live alpha” - **false and explicitly prohibited**. It is only an eligibility condition for calibration.

## 15. Fix plan

1. Add a monetary state to `LogRegScaler`.
2. Sanitize finite `ret` alongside score, label and timestamps.
3. Compute recency-weighted mean return and weighted worst-20% expected shortfall.
4. Evaluate monetary expectancy using the raw matured-return sample floor before class-balance checks, so rare large losses cannot disappear behind minority-count logic.
5. Persist positive/negative evaluated states and keep insufficient states transient.
6. Bump calibrator keys v4 -> v5.
7. Convert confirmed negative bot-specific state into explicit `no_trade`.
8. Add independent red-to-green regressions and synchronize operator artifacts.

## 16. Actual diff by file group

Production:

- `app/calibration.py`
- `app/recommender.py`
- `app/main.py` (version only)

Tests:

- new `tests/test_iteration228_monetary_expectancy_calibration.py`;
- calibration fixtures updated with explicit `ret` in iteration 85, 189, 191, 211 and `test_logic.py`;
- v5 key assertion in iteration 208;
- current release version assertions updated in iterations 213-226.

Documentation/operator artifacts:

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- regenerated `docs/instrukciya_operatora_bybit_recommender.pdf`;
- regenerated root `how_to_trade.png`;
- this audit report.

Database/migrations/frontend:

- no schema or migration change;
- no frontend source change; expectancy diagnostics are additive backend JSON.

## 17. Red -> green evidence

Red copy command:

```text
python -m pytest -q tests/test_iteration228_monetary_expectancy_calibration.py
```

Essential red evidence on v1.0.39:

```text
assert model.fitted is False
E assert True is False
E LogRegScaler(... fitted=True ... n_samples=200)

AttributeError: 'LogRegScaler' object has no attribute 'expectancy_status'
assert callable(policy)
E assert False

4 failed in 0.47s
```

Green command on v1.0.40:

```text
python -m pytest -q tests/test_iteration228_monetary_expectancy_calibration.py
```

Green result:

```text
8 passed in 0.47s
```

The final test file covers the original asymmetric cohort, 95%-win rare-tail cohort, positive cohort, numeric sanitation, persistence, fresh negative cache, stale-negative refresh and explicit no-trade policy.

## 18. Database/schema compatibility

- No table or column change.
- Expectancy metadata is additive JSON inside existing `app_config.value_json`.
- New cache keys: `logreg_futures_grid_v5`, `logreg_global_v5`.
- Old v4 values may remain inert in `app_config` but are never loaded by v1.0.40.
- Fresh SQLite bootstrap: 18 tables; repeated `init_db()` succeeded.
- v1.0.39 SQLite database upgraded under v1.0.40 with sentinel data preserved.
- PostgreSQL dialect/locking/release subset: 18 passed.
- Live PostgreSQL integration: **SKIPPED**, no clearly disposable DSN supplied.

## 19. API compatibility

No public route or required field changed. `reasons.confidence_model` gains additive diagnostics:

- `expectancy_status`;
- `return_samples`;
- `weighted_mean_return`;
- `weighted_expected_shortfall`.

The existing status vocabulary is unchanged; `no_trade` is used with a new reason code.

## 20. Configuration compatibility

No `.env` action and no new environment variable. Existing `calib_min_samples` is used as the matured-return floor and remains the probability effective-sample threshold after the monetary gate.

## 21. Security boundary

- No order create/amend/cancel method added.
- No private Bybit credential used.
- No production DB used.
- No secret added to release artifacts.
- Recommendation/audit-only boundary retained.

## 22. Post-check commands and exact results

- `python -m pip check` - **FAILED**, pre-existing MoviePy/Pillow environment conflict.
- `python -m compileall -q app tests main.py` - **PASSED**.
- `python -m ruff check .` - **UNAVAILABLE**, module not installed; no network install attempted.
- `node --check app/ui/static/app.js` - **PASSED**.
- `python -m pytest --collect-only -q` - **1016 collected**.
- iteration 228 targeted run - **8 passed**; deterministic repeat also passed.
- relevant calibration/temporal suite - **30 passed**.
- full suite - **1016 passed in 25.93s**.
- PostgreSQL dialect/locking subset - **18 passed**.
- SQLite fresh/re-init - **PASSED**.
- SQLite v1.0.39 -> v1.0.40 existing-schema check - **PASSED**, sentinel preserved.
- DOCX render - 6 pages; all pages visually inspected, no clipping/overlap.
- PDF generated from the verified DOCX and rendered for visual verification.
- root infographic visually inspected at 1344 x 1120.
- private order endpoint scan - none found.
- release secret/junk scan - clean after packaging exclusions.

## 23. Not verified

- Live PostgreSQL transaction behavior against an actual server.
- Real Bybit fill sequence, queue position, partial fills and slippage.
- Account-level cross-margin liquidation behavior.
- Live profitability or stability across future regimes.
- Ruff lint because Ruff is unavailable in the installed environment.

## 24. Residual risks

1. Proxy `ret` remains model-derived, not exchange truth.
2. A positive weighted proxy mean is necessary but not sufficient for alpha.
3. The gate uses an observed mean, not a formal confidence interval; it is intentionally conservative at non-positive values.
4. Before the matured-return floor, raw confidence remains capped and deterministic gates remain active, but profitability is unknown.
5. Exact live evidence can arrive later and must override proxy interpretation operationally through the v1.0.39 stop gate.
6. Regime shifts can invalidate both positive and negative historical proxy estimates; hourly refit and recency weights reduce but do not eliminate this risk.

## 25. Rollback

Code rollback is file-level and requires no DB migration. Restore v1.0.39 files and restart. v5 cache rows can remain unused. Rollback is not recommended because it restores the confirmed hit-rate/monetary-objective mismatch.

## 26. Recommended next work package

Build an evidence-only profitability report over a user-supplied copy of the real runtime DB/export:

- exact net PnL by bot, direction, symbol and model version;
- capital-at-risk and exposure-time normalization;
- maximum drawdown and expected shortfall;
- fee, settled funding and slippage attribution;
- walk-forward intervals with embargo;
- comparison against no-trade, passive hold and simple fixed-grid baselines;
- explicit separation of proxy, shadow and exact execution evidence.

Without that dataset, the correct conclusion is “economic viability not demonstrated,” not “profitable” and not “apriori impossible.”
