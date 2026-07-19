# Audit iteration: operator freshness vs outcome lineage

## 1. Identification

- Input ZIP: `bybit-reco-systems-v1.0.77(1).zip`
- Input SHA-256: `26b80a2d4e0dbb9279c3fa81c964b1aa817eec503cb807ea530f7b747aa6a77e`
- Original version: `1.0.77`
- New version: `1.0.78`
- Date: `2026-07-19`
- Scope: recommendation publication-chain, outcome-root lineage, additive persistence, history API/UI and operator documentation.

## 2. Project fingerprint

The archive contained one project root and the expected Bybit Recommender fingerprint: `README.md`, `CHANGELOG.md`, `main.py`, `app/main.py`, `app/recommender.py`, `app/outcomes.py`, `app/db.py`, SQLite/PostgreSQL migration SQL, frontend files, tests and operator artifacts. The supported scope remains Bybit Linear USDT perpetual, `futures_grid`, recommendation/audit-only. No private order endpoint was added.

Archive safety baseline:

- 340 entries;
- no absolute path;
- no `../` traversal;
- no external symlink;
- no duplicate/conflicting path;
- no suspicious nested archive.

## 3. Objective and acceptance criteria

After this iteration the system must keep operator freshness and statistical independence as two separate contracts, which is confirmed when:

1. A same-direction update inside the live operator TTL reuses both `publication_root_rec_id` and `outcome_root_rec_id` and is `active`.
2. A confirmed same-direction signal after operator TTL receives a fresh actionable `publication_root_rec_id` and `recommended` status.
3. That post-TTL publication reuses the still-open `outcome_root_rec_id` and has `is_outcome_label_root=false` until the full label horizon matures or the root has an outcome.
4. A new independent outcome root is permitted only after the prior outcome window closes.
5. Existing SQLite databases upgrade additively and legacy rows receive deterministic outcome lineage; fresh SQLite and PostgreSQL reference schemas contain the same column/index.
6. History API/UI distinguishes operator publication chains from independent outcome windows.
7. No order submission, risk gate, grid geometry, outcome target or public mutating contract is weakened.
8. The new behavior fails on pristine `1.0.77`, passes on `1.0.78`, and the complete test-node union remains green.

## 4. Sources read

Relevant code and contracts were read in `app/recommender.py`, `app/db.py`, `app/main.py`, `app/outcomes.py`, `app/settings.py`, frontend history rendering, SQLite/PostgreSQL init SQL, README, CHANGELOG, trading/architecture/module/scenario/risk documents, operator DOCX/PDF/PNG and the latest lifecycle/calibration regression tests.

## 5. Data-flow map

`recommender cycle` → candidate recommendation → same-key lineage lookup `(venue, symbol, bot_type, direction)` → operator publication decision → recommendation persistence → operator action/preflight uses publication freshness → outcome worker selects only independent outcome roots → `reco_outcomes` → exact-policy calibration/model evidence → history API/UI displays both lineages.

Two identities now have explicit responsibilities:

- `publication_root_rec_id`: current operator card, TTL, execution idempotency and publication-chain audit;
- `outcome_root_rec_id`: one independent pseudo-position/label window used by outcome/calibration semantics.

## 6. Baseline environment and commands

- Python: `3.13.5`
- Node: `v22.16.0`
- Production Python files: 24
- Test files before change: 210
- Collected test nodes before change: 1201
- Docs: 88
- Frontend files: 3
- Migration SQL files: 2
- Persistence: SQLite and PostgreSQL compatibility layer/reference schema.

Baseline checks:

| Check | Result |
|---|---|
| `python -m pip check` | FAILED: shared environment has `moviepy 2.2.1` requiring `pillow<12`, installed `pillow 12.2.0` |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| monolithic `python -m pytest -q` | TIMED OUT after 15 minutes at approximately 83%; no final summary |
| exhaustive deterministic batches | PASSED: 1201 passed, 0 failed/skipped/errors, 16 non-overlapping batches |

The 16 batch manifests cover 210 unique test files and exactly the 1201 node IDs reported by `pytest --collect-only -q`.

## 7. Confirmed defect

### P2-265-01 — operator TTL released the statistical outcome lock

- Severity: **HIGH**
- Type: **CONFIRMED DEFECT**
- Primary files: `app/recommender.py`, previous `_find_open_publication_position()` / `_apply_recent_publication_dedupe()` behavior
- Input: an actionable root 20 minutes old, no stored outcome, 12-hour `futures_grid` horizon, default 15-minute operator TTL, persistent same-direction signal.
- Original runtime behavior: the expired publication-chain stopped being considered an open root; the fresh recommendation became both a new operator root and `is_outcome_label_root=true`.
- Expected behavior: the stale operator card must be replaced, but the still-open statistical pseudo-position must remain the only independent label root.
- Violated invariants: publication lifecycle, outcome uniqueness, temporal independence, calibration denominator integrity.
- Model/data impact: a persistent signal could produce up to roughly 48 overlapping roots in a rolling 12-hour interval at a 15-minute TTL. These are not independent experiments and can inflate sample size, narrow uncertainty and contaminate purged validation.
- Trading/operational impact: using a 12-hour card as the only TTL would instead keep stale geometry actionable or hide a fresh signal; therefore suppressing operator refresh is not a safe repair.
- Why prior tests missed it: publication lineage and outcome-label identity were represented by the same root field, so tests verified same-chain reuse but could not express “new operator root, old outcome root”.

### P2-265-02 — one root field could not represent both contracts

- Severity: **MEDIUM**
- Type: **CONFIRMED GAP**
- Files: recommendation schema/read models, history endpoint and frontend history rendering.
- Original behavior: `publication_root_rec_id` and `is_outcome_label_root` jointly represented two different concepts; the history UI could not count fresh operator publications separately from independent outcome windows.
- Expected behavior: materialize and expose a separate immutable outcome lineage without removing existing publication fields.

## 8. Red → green evidence

New regression file: `tests/test_iteration265_operator_outcome_horizon_separation.py`.

Red command on pristine code plus only the new test:

```text
python -m pytest -q tests/test_iteration265_operator_outcome_horizon_separation.py
```

Material red evidence:

```text
assert fresh["outcome_root_rec_id"] == "R-old-outcome"
E AssertionError: assert 'R-fresh-operator' == 'R-old-outcome'

assert update["outcome_root_rec_id"] == "R-live-root"
E AssertionError: assert 'R-live-update' == 'R-live-root'

2 failed in 0.57s
```

Green command after the production fix:

```text
python -m pytest -q tests/test_iteration265_operator_outcome_horizon_separation.py
```

Green evidence, repeated deterministically:

```text
5 passed in 0.39s
5 passed in 0.37s
```

## 9. Design decision: operator TTL remains separate

`RECO_TTL_SEC` remains the operator freshness contract. With the supplied defaults, blank/auto resolves to `max(900, RECO_INTERVAL_SEC × 15)`, which is 900 seconds at a 60-second recommender cadence.

This TTL is necessary because a recommendation contains time-sensitive price, range, economics and risk context. Treating the 12-hour label horizon as operator TTL would conflate “how long this card may be considered now” with “how long the historical experiment is observed”. The former should be short and revalidated; the latter may be long and must not create duplicate labels.

No new environment variable was introduced. Existing deployments keep their current TTL behavior; only the statistical lineage changes.

## 10. Why the label horizon remains 12 hours

The iteration does **not** claim that 12 hours is empirically optimal. It remains the current versioned `grid_label_v26` target because it is embedded consistently in:

- `BOT_HORIZONS` and outcome maturity;
- historical backfill and label availability;
- grid path/funding exposure accounting;
- temporal cohort construction and purged/embargo validation;
- existing model/policy evidence and tests.

Qualitative trade-off:

- **6 hours**: labels mature twice as quickly and may increase cohort turnover, but more often truncate slow range traversal, inventory recycling and funding/path exposure; censoring or low-activity outcomes may rise.
- **12 hours**: current compromise and existing target contract; enough path for intraday grid activity without intentionally spanning a full day.
- **24 hours**: observes longer paths and more funding events, but halves the rate of independent cohort accumulation relative to 12 hours, increases regime mixing and delays detection of policy degradation.

Making the horizon a casual environment toggle would be unsafe: changing it changes the target, label availability, funding-event count, embargo and sample independence. A change must create a new outcome/model lineage and be selected through a separate purged walk-forward study. Therefore this patch preserves 12 hours and explicitly documents that it is a hypothesis to validate, not a proven optimum.

## 11. Implementation

### Production

- `app/recommender.py`
  - added lookup of the latest live operator publication separately from the open outcome root;
  - open outcome lookup ignores operator TTL but requires an unmatured root without a stored outcome;
  - post-TTL recommendations start a new publication chain while sharing the prior outcome root;
  - live updates reuse both roots;
  - model identity changed to `bybit-taxonomy-v11-separated-operator-outcome-lineage` so potentially overlapping legacy samples cannot enter the new exact-policy evidence.
- `app/db.py`
  - additive schema upgrade/backfill/index for `outcome_root_rec_id`;
  - strict normalization and root invariants;
  - read models and calibration rows expose the new lineage;
  - historical/async maintenance repairs statistical lineage without collapsing a fresh post-TTL operator chain.
- `app/main.py`
  - version `1.0.78`;
  - operator decision context includes outcome lineage;
  - history response adds `outcome_kind`, separate root-change indicators and separate publication/outcome counts.
- `app/ui/static/app.js`, `index.html`
  - separate “Публикация” and “Разметка” semantics;
  - separate summary counters;
  - cache build synchronized to `1.0.78`.

### Database/migrations

- `migrations/init.sql`
- `migrations/init_postgres.sql`

Both contain `outcome_root_rec_id TEXT` and `idx_reco_outcome_lineage_ts`.

### Tests

- new iteration 265: five lifecycle/schema/maintenance tests;
- adjusted persistence/history/PostgreSQL fake-row fixtures for the additive field;
- updated model/version assertions that intentionally define the current lineage.

### Documentation

Updated README, CHANGELOG, `.env.example`, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, KNOWN_RISKS, HOW_TO_TRADE_INFOGRAPHIC, operator DOCX/PDF and `how_to_trade.png`.

## 12. Database and compatibility assessment

- Migration type: additive and idempotent.
- Existing SQLite: `init_db()` adds the column before creating the new index, then backfills from the existing publication root or `rec_id`.
- Fresh SQLite: covered by the new regression.
- Existing SQLite upgrade: covered by the new regression.
- PostgreSQL: reference DDL and dialect/static tests passed as part of the full suite.
- Live PostgreSQL integration: SKIPPED because no explicitly disposable test DSN was provided.
- No manual SQL action is required for normal application startup.
- Public API compatibility: additive fields only; existing field names/statuses remain.
- Config compatibility: no new variable; `.env.example` only clarifies semantics.

## 13. Post-check results

| Check | Result |
|---|---|
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| `pytest --collect-only -q` | 1206 nodes |
| exhaustive deterministic pytest batches | 1206 passed, 0 failed/skipped/errors |
| final large batch subdivision | 44 passed + 30 passed after harness timeout on combined package |
| new regression, repeat 1 | 5 passed |
| new regression, repeat 2 | 5 passed |
| relevant lifecycle/DB/history/calibration suite | 132 passed |
| operator DOCX render | PASSED: 15 pages |
| visual document QA | PASSED: all pages inspected; second render changed only pages 3 and 12 and both were re-inspected |
| infographic visual QA | PASSED |
| private order endpoint scan | PASSED: none found |
| secret/private-key scan | PASSED; only intentional placeholder assertion in tests |
| `python -m pip check` | FAILED due shared MoviePy/Pillow conflict, unchanged from baseline |
| `python -m ruff check .` | UNAVAILABLE: ruff is not installed |
| live Bybit/network smoke | NOT RUN; not necessary for this offline lifecycle fix |

The monolithic suite is not reported as green: the baseline run did not produce a final summary and the final combined batch 16 hit the harness timeout. The exhaustive union is instead proven by collect manifests and non-overlapping deterministic batches.

## 14. Security boundary

The project remains recommendation/audit-only. Static scan found no `/v5/order/create`, amend/cancel or equivalent private order submission path in production code. No credentials, `.env` or production database will be included in the release ZIP.

## 15. Residual risks

1. The 12-hour horizon is not empirically selected against 6/24 hours in this iteration.
2. Same-symbol opposite directions remain separate exact keys and can have overlapping windows. Purged timestamp cohorts and horizon embargo remain necessary to control cross-sectional/temporal dependence.
3. Legacy rows receive deterministic lineage backfill, but the new model version intentionally excludes old model evidence from the current exact-policy cohort.
4. A very long unresolved/censored outcome can still reduce evidence liveness; bounded-censor handling remains a documented calibration risk.
5. Live PostgreSQL behavior was not exercised without a disposable DSN.

## 16. Rollback

Deploy the previous `1.0.77` archive and restart the service. The additive `outcome_root_rec_id` column/index may remain in SQLite/PostgreSQL; `1.0.77` ignores it, so destructive downgrade SQL is neither required nor recommended. Preserve the database for audit continuity. If rolling back after accumulating `v11` evidence, treat it as a separate model lineage and do not merge it manually with the older model.

## 17. Recommended next work package

Run a frozen, offline, purged walk-forward comparison of 6/12/24-hour targets using the same universe and decision timestamps. Compare independent temporal cohorts, censor/unresolved rate, grid activity, net proxy return lower bounds, expected shortfall/tail loss, funding-event exposure and terminal holdout skill. Any selected change should create a new target version (for example `grid_label_v27`), corresponding embargo and a new model/calibrator lineage.

## 18. Commit message

```text
fix(outcome-lineage): separate operator TTL from 12h label windows

- add independent outcome_root_rec_id lineage and additive DB migration
- refresh operator publications without duplicating overlapping outcomes
- update history UI, model identity, regressions and operator documentation
```
