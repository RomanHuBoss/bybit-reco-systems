# Audit report: direction-aware learning and strategy viability

Date: 2026-07-21  
Release: 1.4.5  
Input archive: `bybit-reco-systems-main(1).zip`  
Input archive SHA-256: `c989f078ecb150436756404afc96a2fc727295d7568c15f2330a0db2484c9628`  
Applied protocol: `Bybit_Recommender_Iteration_Prompt(2).pdf`  
Applied protocol SHA-256: `cc720113f768ea1f7410fefdc8da7f814ac0737eefafe312fc0d027e99fdb0e4`

## 1. Iteration objective

After this iteration, pooled LONG/SHORT calibration must represent direction-dependent evidence with one consistent meaning. A market signal that supports LONG and the mirrored signal that supports SHORT must enter binary LogReg/Platt and the directional first-touch softmax as the same positive feature, without weakening monetary, temporal, holdout or router gates.

Acceptance criteria:

1. direction is part of the persisted feature snapshot and training row;
2. raw signed sentiment is supplemented by an explicit direction-aligned feature;
3. contradictory persisted direction features are rejected fail-closed;
4. a balanced LONG/SHORT sample whose only real signal is direction alignment beats the null baseline;
5. the same contract is effective in the first-touch three-class model;
6. all affected calibrator/model identities are changed so old coefficients cannot be reused;
7. LONG/SHORT payoff, TP/SL, funding and first-touch invariants remain green;
8. no threshold, risk gate or sample floor is loosened to force a fitted model.

## 2. Executive conclusion

A catastrophic learning-representation defect was confirmed.

The project pooled LONG and SHORT observations into one bot-family model, but the feature vector did not contain direction. `effective_sentiment` retained its raw market sign. Consequently, an equally supportive signal was encoded as `+0.9` for LONG and `-0.9` for SHORT. In a balanced long/short cohort, the same coefficient could not treat both values as supportive. The exact same defect propagated into the three-class trend first-touch model because it reused the same feature extractor.

This is sufficient to make a genuine symmetric directional signal look statistically identical to a coin flip. It does not prove that the real strategy has edge, but it proves that the previous implementation could destroy a real class of edge before validation.

The patch adds canonical `direction_sign` and `sentiment_alignment` features, persists them at recommendation time, rejects contradictory snapshots, and starts new binary and first-touch model lineages.

## 3. Project fingerprint

Confirmed:

- Bybit Linear USDT perpetual recommendation/audit service;
- strategy families `futures_grid` and `directional_trend`;
- FastAPI application in `app/main.py`;
- SQLite and PostgreSQL persistence paths;
- frontend in `app/ui/static/`;
- no private Bybit order creation/amend/cancel implementation;
- canonical directional math in `app/trading_semantics.py`;
- arithmetic grid math in `app/grid_math.py`.

Project fingerprint: **MATCHED**.

## 4. Sources read

The audit used the current ZIP as the primary source of runtime truth. Relevant materials included:

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`;
- the latest five audit reports, especially `AUDIT_REPORT_2026-07-21_label_maturity_calibration.md`;
- `app/features.py`, `direction.py`, `regime.py`, `recommender.py`, `calibration.py`, `trend_events.py`, `strategy_router.py`, `outcomes.py`, `policy.py`, `db.py`, `db_backend.py`, `risk.py`, `trading_semantics.py`;
- relevant regression tests and production frontend helpers;
- the supplied iteration protocol.

## 5. Data-flow map examined

```text
Bybit/sentiment/OI inputs
  -> deterministic feature construction
  -> deterministic multi-timeframe direction vote
  -> fixed strategy-native score
  -> recommendation feature_snapshot
  -> persisted recommendation/outcome lineage
  -> binary bot-family LogReg + Platt
  -> trend first-touch softmax
  -> confidence blend / monetary evidence
  -> strategy-profitability-router-v3
  -> no_trade or operator/audit recommendation
```

The generator is not trained end-to-end. The learned components calibrate or route candidates produced by fixed rules.

## 6. Baseline environment and inventory

- Python: 3.13.5.
- Node: 22.16.0.
- Source application version: 1.4.4.
- Baseline collected tests: 1304.
- Baseline full suite: 1304 passed in 44.01 seconds.
- Production Python files under `app/`: 26.
- Test files: 220 before this iteration.
- Existing `test_iteration<N>` maximum: 274.
- ZIP entries: 371, one project root.
- Unsafe archive paths, duplicate paths, symlinks, nested archives, credentials and packaged runtime databases: not found.

`python -m pip check` reported a host-environment conflict: `moviepy 2.2.1` requires Pillow below 12 while the shared environment contains Pillow 12.2.0. The project does not declare moviepy and this iteration changed no dependencies.

`ruff` was unavailable in the execution environment.

## 7. Confirmed defects and gaps

### D275-01 — HIGH — pooled LONG/SHORT calibration erased symmetric directional signal

**Type:** CONFIRMED DEFECT.  
**Source:** `app/calibration.py`, `app/recommender.py`.  
**Affected path:** persisted feature snapshot -> `extract_features()` -> bot-family LogReg/Platt.

Before the patch:

- `FEATURE_NAMES` had 13 fields and no direction field;
- bot models were keyed only by `futures_grid` or `directional_trend`;
- `effective_sentiment` was stored in raw market polarity;
- LONG-supportive sentiment was positive, SHORT-supportive sentiment negative;
- the fixed scorer itself correctly multiplied sentiment by direction, but the learner did not.

Independent reproducer:

- 400 observations;
- 50 non-overlapping 12-hour decision cohorts;
- balanced LONG/SHORT and balanced success/failure;
- all features and score constant except raw sentiment;
- success perfectly determined by whether sentiment supports the chosen side.

Pristine result:

- feature OOF log-loss: `0.6931471805599453`;
- null log-loss: `0.6931471805599453`;
- score-only log-loss: `0.6931475537569459`;
- `oof_skill_status=rejected`;
- `fitted=false`.

The learner therefore reduced a perfectly predictive mirrored signal to null performance.

**Fix:** add `direction_sign` and `sentiment_alignment`; persist them; pass recommendation direction into the calibration row; validate persisted values against the row direction.

Patched result on the identical sample:

- feature OOF log-loss: `0.016522855492156034`;
- null log-loss: `0.6931471805599453`;
- `oof_skill_status=accepted`;
- selected-policy and terminal monetary status remain positive;
- `fitted=true`.

### D275-02 — HIGH — first-touch softmax inherited the same representation defect

**Type:** CONFIRMED DEFECT.  
**Source:** `app/trend_events.py` through shared `extract_features()`.

A three-class sample was built with 300 rows and exactly balanced `TP_FIRST`, `SL_FIRST` and `HORIZON_EXIT` classes. Raw sentiment was balanced by LONG/SHORT; only direction alignment predicted event class.

Pristine result:

- feature holdout log-loss: `1.0986122886681087`;
- null-frequency log-loss: `1.0986122886681087`;
- `holdout_status=rejected`;
- `fitted=false`.

Patched result:

- feature holdout log-loss: `0.31700849352082366`;
- null-frequency log-loss: `1.0986122886681087`;
- `holdout_status=accepted`;
- `fitted=true`.

**Fix:** the shared direction-aware feature schema is used by the first-touch model; its model key/version moved to `trend-first-touch-softmax-v2` / `trend_event_softmax_v2`.

### D275-03 — HIGH — old coefficients could otherwise survive a semantic feature change

**Type:** CONFIRMED DEFECT PREVENTED BY RELEASE CHANGE.  
**Affected path:** persisted `app_config` model artifacts.

A feature semantic/schema change must not load coefficients trained on the previous 13-dimensional direction-blind representation.

**Fix:** new identities:

- application model: `bybit-taxonomy-v12-direction-aware-calibration`;
- grid calibrator: `logreg_futures_grid_v22`;
- trend binary calibrator: `logreg_directional_trend_v3`;
- global calibrator: `logreg_global_v22`;
- trend recommender suffix: `directional-trend-v5`;
- first-touch softmax: `trend-first-touch-softmax-v2`.

The router now compares event model version against the imported canonical constant instead of a duplicated literal.

### G275-04 — HIGH — the system is not an end-to-end learning strategy

**Type:** CONFIRMED GAP / ARCHITECTURAL LIMITATION.  
**Not changed in this work package.**

The current system learns neither the feature representation, direction rule, entry, exit geometry nor strategy generator. Those remain deterministic formulas. The learned binary layer reweights 15 snapshot fields, including the already hand-composed `score`. The first-touch model uses the same compact representation.

Even when a calibrator is active, runtime confidence uses a fixed blend whose calibration weight is capped at 50%. Therefore at least half of the final confidence remains the original heuristic confidence. This design can improve calibration and reject bad candidates, but it cannot reliably discover a materially different trading policy.

This is not automatically non-viable. It is viable as a conservative rule-based research system with learned validation. It is not equivalent to a self-learning trading model.

### G275-05 — HIGH — rapid lineage churn makes statistical activation operationally unlikely

**Type:** CONFIRMED OPERATIONAL RISK.  
**Not changed in this work package.**

The changelog contains 74 versioned July 2026 release headings and 24 distinct `grid_label_vN` identifiers. Many changes were justified correctness repairs, but each model/label/policy identity change can split or invalidate the exact-policy evidence cohort.

Current activation also requires:

- at least 80 monetary observations;
- at least 300 observations before probability inference;
- class balance;
- purged whole-timestamp OOF skill over null/score baselines;
- untouched terminal holdout;
- positive selected-policy and terminal monetary lower bounds.

With a 12-hour horizon, correlated symbols and repeated lineage resets, calendar time and effective independent sample count can be far worse than the raw row count suggests. The system needs a frozen experimental lineage, not another daily semantic revision.

### L275-06 — evidence supplied with the project does not prove a live edge

**Type:** DOCUMENTED LIMITATION.

The latest embedded audit report describes 155 current-policy roots, 43 wins, 112 losses, `win_rate=0.277` and `avg_ret=-0.452`; all are `shadow_no_trade`, with zero actionable or executed rows. That previous release also found all 155 excluded by premature label availability and repaired that maturity contract.

No populated runtime database, exchange fills or private read-only reconciliation dataset was included in the current ZIP. Therefore this audit cannot honestly conclude that the patched strategy is profitable. It can conclude that the previous learner was incapable of representing an important mirrored signal and that this specific failure is fixed.

## 8. Claims not confirmed

- No canonical LONG/SHORT P&L sign inversion was found.
- No TP/SL mirror inversion was found.
- No funding payer/receiver inversion was found.
- No evidence was found that simply expanding the symbol universe creates independent edge; correlated cross-sectional rows do not substitute for temporal cohorts.
- No evidence was found that lowering sample, confidence, OOF, monetary or router gates would be statistically justified.
- No evidence was found that the LLM reviewer can create trading alpha; it remains advisory/control logic.

## 9. Implementation

### Production

- `app/calibration.py`
  - adds canonical direction helpers;
  - adds `direction_sign` and `sentiment_alignment` to the feature schema;
  - rejects contradictory persisted direction-aware fields;
  - changes binary model storage keys.
- `app/recommender.py`
  - persists direction-aware fields in `feature_snapshot`;
  - passes direction to training/inference rows;
  - changes model identities.
- `app/trend_events.py`
  - starts first-touch softmax v2 lineage.
- `app/strategy_router.py`
  - removes duplicated first-touch model-version literal.
- `app/main.py`
  - application version 1.4.5.
- `app/ui/static/index.html`
  - cache-busting build version 1.4.5.

### Tests

- new `tests/test_iteration275_direction_aware_learning.py` with six independent contract tests;
- current lineage/version assertions updated where the old identifiers were intentionally part of the contract.

### Database and configuration

- no schema migration;
- no environment-variable change;
- no risk-limit or threshold change;
- existing persisted outcomes are retained;
- old model artifacts are ignored by new keys and will be refitted only from eligible current-lineage evidence.

## 10. RED -> GREEN evidence

Red command:

```bash
pytest -q tests/test_iteration275_direction_aware_learning.py
```

Pristine/red result: **6 failed**. Essential failures:

- `sentiment_alignment` absent from the feature schema;
- symmetric binary sample returned `oof_skill_status=rejected`;
- first-touch model returned `fitted=false` and null log-loss;
- contradictory persisted direction fields were accepted;
- `_build_feature_snapshot()` did not accept direction;
- model lineage identifiers remained old.

Green command:

```bash
pytest -q tests/test_iteration275_direction_aware_learning.py
```

Patched result: **6 passed in 3.04 seconds**.

## 11. Post-checks

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- direction/payoff/funding/first-touch focused suite: 122 passed in 5.89 seconds.
- `pytest --collect-only -q`: 1310 collected.
- monolithic full suite: 1310 passed in 46.36 seconds.
- `python -m pip check`: host moviepy/Pillow conflict, unrelated to project dependencies.
- `ruff`: UNAVAILABLE.
- PostgreSQL live integration: SKIPPED because no explicitly disposable test DSN was supplied; dialect/unit paths remain in the full suite.

## 12. API, security and compatibility

- public API field names are unchanged;
- DB schema is unchanged;
- recommendation/audit-only boundary is unchanged;
- no private order endpoint was added;
- fail-closed behavior is strengthened for contradictory feature snapshots;
- old fitted artifacts are not silently reinterpreted;
- existing outcomes remain audit records but only exact current lineage can train current models.

## 13. What this means for viability

The answer is neither “everything was fine” nor “the entire approach is certainly dead.”

1. The month of coin-flip-like calibration was partly explained by real implementation defects: first label maturity excluded the accumulated cohort, and direction-blind feature encoding then made a mirrored signal unlearnable.
2. The corrected system can now learn this class of signal, but it is still fundamentally a fixed heuristic recommender with a validation/calibration layer.
3. If a frozen, correctly labeled, exact-policy lineage still fails null/score baselines and monetary holdouts after enough independent cohorts, the rational conclusion will be that the current rule-based thesis has no demonstrated edge. More bug fixing or more correlated symbols should not be used to postpone that conclusion.

## 14. Required deployment actions

1. Replace the application with release 1.4.5.
2. Do not copy cached calibrator rows under the old model keys into the new keys.
3. Keep the policy, outcome label, feature schema and risk settings frozen during the next evidence window unless a safety defect is proven.
4. After startup, verify that new recommendation snapshots contain `direction_sign` and `sentiment_alignment` and that Health reports the new model identities.
5. Do not enable execution based solely on a fitted flag; retain exact-policy monetary, terminal-holdout and router requirements.

## 15. Residual risks

- Real market edge remains unproven.
- Proxy OHLCV outcomes do not reproduce queue priority, partial fills, market impact or actual account-level costs.
- Features are highly dependent on the same hand-built score components; incremental model capacity is limited.
- A 50% cap on calibrated-confidence influence leaves the heuristic score structurally dominant.
- Cross-symbol rows at one timestamp are not independent observations.
- Frequent model/policy changes can again starve the current lineage.
- No populated runtime database was available for direct post-repair re-fit on the user's actual history.

## 16. Recommended next work package

Freeze one immutable experimental lineage and run a paired walk-forward research protocol for at least 30–45 calendar days:

- compare current heuristic score, direction-aware LogReg, and a deliberately simple direction-aware baseline on exactly the same candidates;
- evaluate by whole decision timestamp, not row-random split;
- report grid and trend separately, then LONG and SHORT diagnostics separately;
- measure net return, expected shortfall, calibration, first-touch log-loss and effective temporal cohorts;
- keep a truly untouched final period;
- decide in advance that failure to beat null and score-only baselines terminates the current strategy thesis.

Only after that should the project consider a new learned candidate generator rather than another calibration patch.

## 17. Rollback

Restore the 1.4.4 production files and frontend build token. The new v1.4.5 model artifacts use separate storage keys, so rollback does not require deleting old 1.4.4 artifacts. Do not rename v1.4.5 artifacts to old keys.

## 18. Suggested commit message

`fix(calibration): make pooled long-short learning direction-aware`
