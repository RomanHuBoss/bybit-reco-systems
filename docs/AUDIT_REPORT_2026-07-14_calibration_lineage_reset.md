# Audit iteration: calibration lineage reset and dataset transparency

## 1. Input and versions

- Input ZIP: `bybit-reco-systems-1.0.55-mean-reversion-temporal-recovery.zip`
- Input SHA-256: `4aa29cba54c9d36ffa0f89e53e1ae5608acb99ae78d075e1f6bf98e305695336`
- Original application version: `1.0.55`
- New application version: `1.0.56`
- Original recommendation lineage: `bybit-taxonomy-v6-historical-proxy-shadow-roots`
- New recommendation lineage: `bybit-taxonomy-v7-mr-floor-temporal-cohorts`
- Outcome target remains `grid_label_v26` because outcome mathematics was not changed.
- Bot/global calibration keys: v17 -> v18.
- Direction calibration key: v12 -> v13.

## 2. Project fingerprint

Fingerprint matched Bybit Recommender: FastAPI recommendation/audit service, `futures_grid`, Bybit Linear USDT perpetual scope, SQLite/PostgreSQL dual persistence, canonical directional semantics, static frontend and required operator artifacts. No order-create/amend/cancel capability was added.

## 3. Goal and acceptance criteria

After this iteration the project must preserve old outcomes as immutable audit history while starting the changed model policy with zero compatible calibration evidence. Acceptance criteria:

1. Old v6 outcomes are not eligible for v7 calibration.
2. New cache identities cannot load v17/v12 calibrators as current.
3. API distinguishes historical, current-model and feature-eligible counts.
4. Bot progress/gating uses eligible rows, not the historical archive.
5. UI explicitly shows archive/current/eligible and calibrator identity.
6. PostgreSQL schema remains unchanged and no destructive data action is required.
7. Full offline test set passes.

## 4. Sources reviewed

Reviewed the project prompt, README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, operator infographic/documentation, current audit report, and relevant code in `app/recommender.py`, `app/calibration.py`, `app/main.py`, `app/db.py`, `app/outcomes.py`, settings and frontend.

## 5. Affected data flow

`recommendation.model_version` -> joined outcome archive -> lineage partition -> feature-evidence partition -> fit sanitization -> temporal cohort selection -> v18/v13 calibrator persistence -> `/api/v1/status` -> calibration UI.

## 6. Baseline environment and results

- Python: 3.13.5
- Node: 22.16.0
- `compileall`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- Ruff: UNAVAILABLE (`No module named ruff`)
- `pip check`: FAILED due to external environment conflict: MoviePy 2.2.1 requires Pillow <12 while Pillow 12.2.0 is installed. Project dependency pins were not changed.
- Collected: 1078 test nodes.
- Exhaustive deterministic batches: 180 + 180 + 180 + 180 + 179 + 179 = 1078.
- Baseline result: 1078 passed, 0 failed, 0 errors.

## 7. Confirmed defects

### CL-01 - HIGH - CONFIRMED DEFECT

Changed model-policy rules retained recommendation lineage v6. As a result, old outcomes could continue to satisfy the current-model filter even though the operator expected a fresh evidence lifecycle.

Impact: training lineage contamination and false continuity of calibration status after a material policy change.

### CL-02 - HIGH - CONFIRMED DEFECT

`/api/v1/status` used all retained outcome roots for bot calibration progress and `eligible_for_fit`, while actual fit code applied model and feature filters.

Impact: the UI could show hundreds of outcomes while the real calibrator had `n=0`.

### CL-03 - MEDIUM - CONFIRMED GAP

The UI did not display model lineage, calibrator key, archive/current/eligible counts or temporal cohort counts. A non-empty archive therefore looked like unchanged current evidence.

## 8. RED -> GREEN evidence

RED command:

```bash
python -m pytest -q tests/test_iteration244_calibration_lineage_reset.py
```

RED result on pristine production code: `4 failed`.

Substantial failures:

- v6 model identity instead of v7;
- no `calibration_lineage_diagnostics` source of truth;
- no `historical_outcome_count` / `current_model_outcome_count` / `calibration_eligible_outcome_count` API contract;
- frontend text omitted archive/current/eligible and v18 identity.

GREEN command:

```bash
python -m pytest -q tests/test_iteration244_calibration_lineage_reset.py
```

GREEN result: `4 passed`; deterministic repeat: `4 passed`.

## 9. Implementation

- Introduced v7 recommendation model identity.
- Introduced v18 bot/global and v13 direction cache identities.
- Added shared `calibration_lineage_diagnostics()` used by fit paths and status diagnostics.
- Preserved all historical rows but rejected non-current model versions before feature eligibility.
- Changed bot status progress and fit eligibility to use feature-eligible current-lineage rows.
- Added archive/current/eligible, fit-row, expectancy and temporal-cluster diagnostics.
- Updated frontend wording and cache-busted `app.js`.
- Updated application version, documentation, DOCX, PDF and PNG artifacts.

## 10. Database and compatibility

No schema changes. `reco_outcomes`, recommendations, bot lifecycle, trades and audit logs are preserved. Existing v17/v12 app_config objects remain historical cache records but are not read by v18/v13 keys. SQLite fresh init and repeated init passed. PostgreSQL translation/locking offline suite passed 24/24. Live PostgreSQL integration was skipped because no explicitly disposable test DSN was supplied.

## 11. Post-check

- Collected: 1082 test nodes.
- Exhaustive deterministic batches: 181 + 181 + 180 + 180 + 180 + 180 = 1082.
- Final result: 1082 passed, 0 failed, 0 errors.
- Targeted regression twice: 4/4 passed each run.
- PostgreSQL offline suite: 24/24 passed.
- SQLite fresh/re-init: passed; 18 tables; empty outcome table.
- `compileall`: passed.
- Node syntax: passed.
- DOCX: rendered and visually inspected, 10 pages.
- PDF: independently rendered and visually inspected, 10 pages.
- Ruff unavailable; external pip conflict unchanged.

A transient post-check failure was an obsolete v17 assertion in an existing identity test. Its expectation was updated to v18; no production behavior was weakened.

## 12. Security and release boundary

No credentials or production DSN were used. No private order endpoints were added. The project remains recommendation/audit-only. Release excludes `.env`, databases, caches, bytecode and local test artifacts.

## 13. Unverified items

- Live PostgreSQL integration: skipped without disposable DSN.
- Live Bybit execution and actual fills: outside project boundary.
- Profitability and statistical optimality of v7: not claimed.

## 14. Residual risks

The v7 dataset begins empty. The strategy will remain shadow `NO_TRADE` until sufficient matured, feature-eligible and non-overlapping v7 cohorts exist. Historical outcomes are still visible in audit endpoints, so external consumers must use the new lineage-specific fields rather than treating archive count as training evidence.

## 15. Rollback

Stop v1.0.56 and redeploy v1.0.55. No database rollback is required. v1.0.55 will again use v6/v17/v12 identities, which reintroduces the lineage-contamination and misleading-count defects.

## 16. Recommended next work package

Add a dedicated calibration-evidence history endpoint/chart showing daily current-lineage rows, sanitization drops, matured rows and selected temporal cohorts, without changing actionability gates.
