# Audit iteration 242 - purged OOF feature-calibration activation gate

## 1. Iteration name

Purged OOF feature-calibration activation gate.

## 2. Input ZIP

`bybit-reco-systems-1.0.53-horizon-boundary-liquidity.zip`

## 3. Input SHA-256

`a763b02f29d9af29258ed667ab04ad9d4fe271679153eadb210d1f506f0994ab`

## 4. Initial version

FastAPI `1.0.53`; outcome contract `grid_label_v26`; bot/global calibrators v15; direction calibrator v12.

## 5. New version

FastAPI `1.0.54`; outcome contract remains `grid_label_v26`; bot/global calibrators v16; direction calibrator remains v12.

## 6. Project fingerprint

PASSED. The archive contains one expected Bybit Recommender root, historical futures-grid recommendation/outcome code, SQLite/PostgreSQL persistence, frontend, migrations, tests and operator artifacts. Input archive: 298 unique entries, no traversal, duplicate paths or symlinks. No private order create/amend/cancel implementation was added.

## 7. Iteration goal

After this iteration, full feature LogReg coefficients must influence recommendation confidence only when the existing chronological validation path produces enough genuinely out-of-fold predictions after label-availability purging and Platt-on-top fits successfully. A full-sample fit alone must not be presented as calibrated feature confidence.

## 8. Acceptance criteria

1. A temporally concentrated dataset with positive monetary gates but zero purged OOF predictions does not expose feature coefficients.
2. Insufficient OOF degrades to score-only Platt, or raw capped confidence if that fallback is also unavailable.
3. A distributed dataset with at least `CALIB_MIN_SAMPLES` purged OOF predictions activates full feature LogReg.
4. OOF status, actual count and required count persist and appear in recommendation diagnostics.
5. Monetary expectancy and temporal-independence gates remain unchanged.
6. Existing `grid_label_v26` outcomes are retained; only bot/global calibrator identities change.
7. Full regression, SQLite, PostgreSQL-offline, documentation and repacked-ZIP checks pass.

## 9. Sources read

Relevant sources included `README.md`, `CHANGELOG.md`, `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, recent audit reports, `app/calibration.py`, `app/recommender.py`, `app/main.py`, database bootstrap/persistence code, and calibration/temporal regression tests.

## 10. Affected data flow

`matured proxy outcomes` -> `monetary and temporal gates` -> `score-only Platt baseline` -> `feature matrix` -> `chronological splits` -> `label-availability purge` -> `OOF logits` -> `Platt-on-top` -> `feature LogReg activation` -> recommendation `confidence_model` diagnostics.

## 11. Baseline environment

- Python: `3.13.5`
- Node: `22.16.0`
- `pip check`: pre-existing MoviePy/Pillow conflict only
- Ruff: unavailable in the environment
- input archive safety: PASSED

## 12. Baseline results

- compileall: PASSED
- JavaScript syntax: PASSED
- collection: `1069` unique test nodes
- monolithic run: did not return a final summary in the harness and was not counted
- exhaustive execution: all `1069/1069` nodes passed; large groups that did not terminate reliably as one process were split into deterministic non-overlapping subgroups/individual nodes

## 13. Confirmed defect

### DEF-242-01 - in-sample feature coefficients exposed without purged OOF validation

- Severity: HIGH
- Type: CONFIRMED DEFECT
- File/function: `app/calibration.py::fit_logreg`
- Consumer: `app/recommender.py::run_recommender_once`
- Reproducer: 320 valid rows, of which 280 share one early temporal cluster and 40 occupy 20 later clusters. Monetary row/cluster lower bounds are positive and class balance is acceptable.
- Chronological behavior: every fixed validation boundary falls inside the first cluster; label-availability purging leaves zero valid OOF predictions.
- Old result: `fitted=true`, 13 non-empty feature coefficients, `platt.fitted=false`; recommender could identify the source as `bot_logreg` and use full-sample logits.
- Expected: feature coefficients withheld because no out-of-fold probability evidence exists.
- Financial/model impact: in-sample separation could overstate confidence and model readiness, especially for concentrated histories, while appearing to be calibrated.
- Why old tests missed it: OOF construction and final model fitting were tested separately; one prior test explicitly mocked an empty OOF set while still asserting that full feature coefficients remained active.

## 14. Unconfirmed claims

This iteration does not establish live profitability, exact execution quality, regime stability or the optimality of score-only Platt. It only closes the incorrect feature-model activation boundary.

## 15. Fix plan

- Add explicit OOF activation diagnostics to `LogRegScaler`.
- Require at least `min_samples` purged OOF logits and fitted Platt-on-top before retaining feature coefficients.
- Degrade insufficient/error paths to the existing score-only Platt baseline or raw confidence.
- Persist/validate OOF diagnostics.
- Expose diagnostics and honest source notes in recommendation output.
- Bump bot/global calibrator identities without changing outcome labels.

## 16. Actual diff by file group

### Production

- `app/calibration.py`
- `app/recommender.py`
- `app/main.py`

### Tests

- added `tests/test_iteration242_purged_oof_activation_gate.py`
- minimally updated prior sanitization/OOF fixtures that encoded feature activation without sufficient OOF
- synchronized version and calibrator-key assertions

### Documentation and operator artifacts

- README, CHANGELOG and relevant docs
- operator DOCX/PDF
- root `how_to_trade.png`
- this audit report

### Database/migrations/frontend

No relational schema, migration, public API route, environment-variable or frontend source change.

## 17. Red -> green evidence

Red command:

`python -m pytest -q tests/test_iteration242_purged_oof_activation_gate.py`

Essential red result:

`3 failed in 1.23s` with `AttributeError: 'LogRegScaler' object has no attribute 'oof_status'`. Independent inspection of the concentrated sample showed `fitted=True`, 13 coefficients and zero usable OOF predictions.

Green command: same command on the working copy.

Green result: `3 passed`; repeated deterministic run: `3 passed`.

## 18. Database/schema compatibility

- No relational schema change.
- SQLite fresh initialization and repeated initialization: PASSED; sentinel configuration preserved.
- Calibrator JSON gains an additive `oof_validation` object.
- Old v15 bot/global models are not selected because keys move to v16; retained outcomes remain valid because the target contract is unchanged.
- PostgreSQL offline translation/locking subset: `18 passed`.
- live PostgreSQL: skipped without an explicitly disposable DSN.

## 19. API compatibility

No route or request field was removed. Recommendation diagnostics add `purged_oof_status`, `purged_oof_samples` and `purged_oof_required_samples` inside the existing confidence-model object.

## 20. Configuration compatibility

No environment variable was added, removed or reinterpreted.

## 21. Security and execution boundary

The project remains historical recommendation/audit-only. The fix does not submit, amend or cancel orders and does not add private Bybit execution dependencies.

## 22. Post-check results

- collection: `1072` unique test nodes
- exhaustive deterministic batches: `90 + 90 + 90 + 90 + 89 + 89 + 89 + 89 + 89 + 89 + 89 + 89 = 1072/1072 passed`
- targeted iteration 242: `3 passed`, repeated
- calibration/temporal targeted checks: PASSED
- compileall and JavaScript syntax: PASSED
- SQLite fresh/repeat: PASSED
- PostgreSQL offline subset: `18 passed`
- DOCX and PDF: 9 pages, rendered and visually inspected
- PNG infographic: visually inspected
- repacked ZIP integrity: PASSED (`unzip -t`, one root, no release junk)
- tests from re-extracted archive: `10 passed`

## 23. Not verified

- live PostgreSQL without a safe disposable DSN
- live market profitability and exact-fill performance
- Ruff lint because Ruff is unavailable

## 24. Residual risks

- Score-only Platt remains a simpler full-sample calibration fallback; it is not proof of feature generalisation.
- Fixed chronological folds may still be statistically inefficient under highly uneven temporal density.
- Purged OOF reduces leakage but does not replace regime-aware walk-forward, block bootstrap or independent exact-fill validation.
- Positive proxy expectancy and calibrated confidence do not prove live edge.

## 25. Rollback

No schema or outcome-label rollback is required. Restore v1.0.53 code and restart. Do not treat v16 calibrator payloads as v15 models.

## 26. Recommended next work package

Replace fixed row-count fold boundaries with temporal-cluster-aware walk-forward folds and report fold coverage, embargo duration and cluster distribution. Compare feature LogReg against score-only Platt on strictly held-out temporal blocks before retaining the more complex model.
