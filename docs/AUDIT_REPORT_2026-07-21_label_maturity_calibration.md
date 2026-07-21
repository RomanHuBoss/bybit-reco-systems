# Audit report: label maturity and calibration learning repair

Date: 2026-07-21  
Release: 1.4.4  
Input archive: `bybit-reco-systems-main(3).zip`  
Input archive SHA-256: `3d0924e33128fdf4d1c05d0ff8b32ab847091bfcb3eaf413b429ab09d90d15c1`  
Applied protocol SHA-256: `cc720113f768ea1f7410fefdc8da7f814ac0737eefafe312fc0d027e99fdb0e4`

## Iteration objective

After this iteration the system must use one provable label-maturity contract from recommendation publication through outcome persistence, calibration fit and operator status; legacy rows created under the split contract must be repaired conservatively; LONG/SHORT/grid/trend payoff semantics must remain unchanged and regression-proven.

Acceptance criteria:

1. no new outcome is persisted with `label_available_ts` earlier than its exact policy due time;
2. publication due time uses the effective outcome horizon actually stored in the outcome;
3. fit lineage rejects missing, premature, malformed or inconsistent maturity metadata;
4. compact status and actual fit consume the same maturity fields and cannot report incompatible eligibility counts;
5. legacy timestamp repair changes metadata only after exact contract verification;
6. LONG/SHORT price, TP/SL, funding and MTF direction mirrors remain green;
7. no calibration/risk/router threshold is weakened to manufacture recommendations.

## Supplied evidence

The attached current-policy statistics contain 155 roots: 43 wins, 112 losses, `win_rate=0.277`, `avg_ret=-0.452`. All 155 are `shadow_no_trade`; actionable and executed cohorts are zero. The exact eligibility report contains 0 calibration-eligible rows and reports `LABEL_AVAILABILITY_PREMATURE` 155 times.

The diagnostic status simultaneously reports `calibrator_fitted=false`, `calibrator_n=0`, `confidence_mode_in_use=raw_only`, 155 current-model outcomes, 79 feature-eligible outcomes and 2 policy-eligible outcomes. Grid has 0 policy rows; trend shows 2 rows, both losses, with an unfitted three-class first-touch model. This status/stats disagreement was reproducible from the source code.

The latest publication has 70 rows, 55 formed strategy recommendations and 15 rejected trend evaluations, but 0 actionable rows. This is evidence of a fail-closed research stream, not performance of permitted live trades.

## What the “model” actually is

The project is not one end-to-end self-learning predictor. It has:

- deterministic feature construction, direction aggregation and strategy-native heuristic scoring;
- bot-specific LogReg/Platt calibration of confidence;
- a separate directional-trend first-touch event model;
- a profitability router that admits a strategy only after exact-policy, OOF, terminal-holdout and monetary lower-bound evidence.

When the bot calibrator is not fitted, runtime explicitly uses `raw_only`: historical outcomes do not alter the heuristic confidence. Therefore the answer to “did it learn from these 155 outcomes?” is no. It recorded them, but the current calibration cohort admitted none of them.

## Confirmed defects and fixes

### HIGH — split label-availability contract blocked learning

Publication persisted policy maturity as recommendation timestamp plus horizon plus 120 seconds. Outcome worker used market-window timing equivalent to horizon plus only 60 seconds after its entry reference. In the supplied deployment, this declared labels approximately 53–60 seconds before policy due. Exact eligibility consequently classified all 155 roots as `LABEL_AVAILABILITY_PREMATURE`.

Fix:

- added canonical `CALIBRATION_LABEL_GRACE_SEC=120` and `policy_label_due_ts()` in `app/policy.py`;
- outcome worker now persists the maximum of market-data readiness and exact policy due;
- strict numeric semantics reject booleans, non-integral floats, non-positive and malformed values.

### HIGH — publication and worker could disagree on effective horizon

Publication previously computed due time from the static bot-family horizon, while outcome labeling could resolve a strategy-specific effective horizon from the persisted trade plan. A valid alternate horizon could therefore make stored due metadata inconsistent with the actual label target.

Fix: publication resolves the same effective horizon before storing `outcome_policy.label_due_ts`; outcome and lineage reuse the stored outcome horizon.

### HIGH — operator status did not project maturity fields used by fit

`iter_calibration_lineage_rows()` omitted `horizon_sec` and `label_available_ts`. The status endpoint therefore could not prove the same eligibility contract as model fitting. Before the strict fix it reported two policy-eligible rows while the stats endpoint correctly reported zero; after adding strict validation without fixing the iterator it would have falsely reported zero for every row.

Fix: compact rows now include recommendation timestamp, exact horizon and availability timestamp. `calibration_lineage_diagnostics()` recomputes due, checks stored due equality, checks availability and current maturity, and uses the same logic for retained fit rows and bounded status counters.

### HIGH — legacy evidence needed conservative recovery

Without repair, valid market outcomes already calculated under the old worker would remain permanently outside calibration solely because their metadata timestamp was early.

Fix: `init_db()` runs a bounded candidate scan. A row is updated only when its stored policy contract is parseable, grace is a positive exact integer, stored due equals recomputed due, due is already mature and current availability is earlier. Only `label_available_ts` moves forward; `success`, `ret`, event type, prices and model/policy identity remain unchanged.

## Sign and payoff audit

No LONG/SHORT sign inversion was confirmed. Regression fixtures prove:

- mirrored favourable LONG-up and SHORT-down moves have the same positive sign; mirrored adverse moves have the same negative sign;
- LONG TP/SL and SHORT TP/SL geometry are canonical;
- Bybit open/close side semantics are symmetric and closes are reduce-only;
- positive funding is a cost to LONG and receipt to SHORT, with the inverse for negative funding;
- monotonically rising/falling synthetic paths resolve to LONG/SHORT respectively.

The poor short cohorts in the attached snapshot are therefore not explained by the canonical sign helpers. They may reflect a bad static thesis in that market sample, regime/selection bias, or insufficient evidence. The patch does not disguise this by flipping labels or loosening gates.

## Expected behavior after deployment

On first startup, exact legacy rows whose only defect is premature availability metadata are repaired. Health and stats should then agree on calibration eligibility. However the model will not immediately become fitted: current floors are 80 monetary samples and 300 probability samples, followed by class-balance, purged OOF/null baseline, whole-timestamp terminal holdout and positive selected-policy expectancy. Based on the supplied snapshot, only a very small exact-policy subset can pass all non-maturity gates.

Thus v1.4.4 fixes evidence accumulation and truthful readiness; it does not claim profitability or synthesize training samples.

## Files changed

- `app/policy.py`
- `app/outcomes.py`
- `app/recommender.py`
- `app/db.py`
- `app/main.py`
- `app/ui/static/index.html`
- `tests/test_iteration274_label_maturity_learning.py`
- maturity-aware historical fixtures in iterations 216, 241, 244 and 254
- current release assertions
- `README.md`, `CHANGELOG.md`
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`
- this report

## Verification

- Python: 3.13.5.
- Node: 22.16.0.
- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- focused calibration/router/sign regression: 100 passed.
- explicit mirror/payoff regression: 6 passed.
- `pytest --collect-only`: 1304 tests.
- exhaustive non-overlapping batches: 450 passed + 455 passed + 399 passed = 1304 passed.
- monolithic suite exceeded the harness timeout and is not counted as a pass.
- `python -m pip check`: environment conflict `moviepy 2.2.1` requires Pillow <12 while host has Pillow 12.2.0; project requirements do not add moviepy and no dependency was changed.

## Residual limitations

- The supplied sample is entirely shadow/no-trade and cannot estimate executable policy performance.
- The base scoring formulas remain static; v1.4.4 repairs calibration data flow rather than replacing the recommender with a new learning architecture.
- Proxy OHLCV outcomes cannot prove live fills, queue position, partial fills or real exchange fee/slippage truth.
- No live Bybit private order endpoint was added.

## Suggested commit message

`fix(calibration): unify outcome label maturity and repair learning lineage`
