# Audit iteration 229 — independent shadow-outcome roots

## 1. Input and release

- Input ZIP: `bybit-reco-systems-1.0.40-monetary-expectancy-gate.zip`
- Input SHA-256: `425c0af3834cf3e03d884f2efdbaa73df18e2a4987ac24ccd76e759233d757d5`
- Baseline version: `1.0.40`
- Release version: `1.0.41`
- Project fingerprint: matched Bybit Recommender, Bybit Linear USDT, `futures_grid`, recommendation/audit-only, SQLite + PostgreSQL.
- `OUTCOME_LABEL_VERSION`: unchanged, `grid_label_v18`.

## 2. Goal and acceptance criteria

After this iteration, repeated counterfactual `shadow_no_trade` publications inside one label horizon must not be counted as independent outcomes.

Acceptance criteria:

1. One open shadow pseudo-position produces one `is_outcome_label_root=1`.
2. Later same-cohort rows remain `no_trade`, link to the root and are non-root.
3. Settled/expired-horizon roots do not block a new independent root.
4. Opposite directions and new model versions remain separate cohorts.
5. Old overlapping sample identity cannot load into current calibration.
6. Full test suite and clean re-extracted ZIP pass.

## 3. Baseline

Environment commands:

- `python -m pytest -q` — `1016 passed in 28.40s`.
- Input archive traversal/duplicate check — 273 entries, no unsafe paths or duplicate names.
- Input ZIP SHA-256 matched the provided v1.0.40 release.

The green baseline did not test independent statistical roots for shadow outcomes.

## 4. Confirmed defect

### SHADOW-229-01 — HIGH — CONFIRMED DEFECT

- Files: `app/recommender.py`, calibration load/fit path in `app/recommender.py` and `app/calibration.py`.
- Affected flow: `no_trade` → `outcome_policy.sample_role=shadow_no_trade` → recommendation persistence → `compute_outcomes_once()` → calibration.
- Actual behavior: `_apply_recent_publication_dedupe()` processed only `recommended`/`active`. An explicit shadow `no_trade` was skipped, retained `is_outcome_label_root=true`, and became independently outcome-eligible each cycle.
- Minimal reproducer: 80 one-minute recommender cycles for the same tuple and 12-hour horizon created 80 roots.
- Expected behavior: one root represents the open counterfactual pseudo-position; 79 later rows are audit children.
- Violated invariant: calibration observations must have defensible statistical identity and must not count overlapping copies of the same market path as independent samples.
- Model impact: `CALIB_MIN_SAMPLES=80` could be reached by 80 minutes of publications, although the labels were based on nearly the same 12-hour path. Recency-weighted mean, class balance, Platt/LogReg sample count and direction calibration were therefore pseudo-replicated.
- Financial/trading impact: inflated confidence and false readiness could make an unproven thesis appear statistically mature. This does not manufacture positive PnL directly, but it can remove the intended uncertainty barrier and influence operator launch decisions.
- Why existing tests missed it: outcome tests verified that an explicit shadow row can mature; publication tests verified actionable chain reuse. No test joined the two contracts.

## 5. Red → green evidence

RED on pristine v1.0.40:

```bash
python -m pytest -q tests/test_iteration229_shadow_outcome_independence.py
```

Essential output:

```text
assert root_count == 1
E assert 80 == 1
4 failed, 2 passed
```

GREEN after fix:

```text
6 passed in 0.42s
```

Relevant publication/outcome/calibration suite:

```text
35 passed
```

## 6. Fix

- Added strict recognition of explicit `shadow_no_trade` outcome candidates.
- Added `_find_open_shadow_outcome_root()` with exact venue/symbol/bot/direction/model identity.
- Shadow locking lasts until pseudo-entry + effective label horizon; recommendation TTL does not falsely create a new statistical sample.
- Repeated rows keep `status=no_trade`, retain immutable audit identity, link to the previous root and set `is_outcome_label_root=false`.
- A new root is allowed after horizon, after a stored outcome, for another direction, or for another model identity.
- Updated model identity to `bybit-taxonomy-v4-independent-shadow-roots`.
- Updated bot/global calibration keys to v6 and direction calibration key to v5. Existing v3 overlapping outcomes remain in audit storage but are excluded from current fit.

## 7. Changed files

Production:

- `app/recommender.py`
- `app/calibration.py`
- `app/main.py`

Tests:

- `tests/test_iteration229_shadow_outcome_independence.py`
- version/model identity assertions in relevant iteration tests.

Documentation:

- `README.md`
- `CHANGELOG.md`
- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- this report.

Database/migrations/frontend:

- No schema or SQL migration changes.
- No public API or frontend code changes.

## 8. Compatibility and operator actions

- Existing SQLite/PostgreSQL schemas are compatible.
- No `.env` changes.
- No data deletion is required.
- Existing v3 recommendations/outcomes remain available for audit but are not used by the v4 model fit.
- Calibrators intentionally restart under new keys. Raw confidence and deterministic gates continue to operate while the independent sample accumulates.

## 9. Post-check

- Full suite: `1022 passed`.
- Targeted iteration229: `6 passed`, repeated deterministically.
- Python compileall: passed.
- JavaScript syntax: passed.
- SQLite fresh init and repeated init: passed.
- PostgreSQL dialect/translation tests: covered by the full offline suite; live integration skipped because no explicitly disposable DSN was supplied.
- Private Bybit order endpoint scan: no execution endpoints introduced.
- Release contains no `.env`, database, WAL/SHM, bytecode or test cache.

## 10. Remaining risks and diagnosis

This fix removes a major source of false statistical confidence. It does not establish positive live edge. Remaining evidence is still OHLCV-based proxy data and cannot prove queue priority, partial fills, actual fee tier, latency or account-level PnL.

The correct current diagnosis is:

- the project was not merely “missing one arithmetic error”;
- its validation stream could substantially overstate independent sample size;
- prior calibrated confidence and readiness based on overlapping shadow roots were not trustworthy;
- after v1.0.41 the project must rebuild an independent sample before any profitability conclusion.

Recommended next work package: add effective-sample diagnostics by symbol/regime and a one-sided uncertainty bound for monetary expectancy; a mean barely above zero should not be treated as established positive edge.

## 11. Rollback

Code-only rollback to v1.0.40 requires no schema action, but reintroduces overlapping shadow roots and permits old v5 calibrators to be used. Rollback is not recommended.
