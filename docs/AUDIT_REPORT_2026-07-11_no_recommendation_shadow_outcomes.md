# Audit iteration: no-recommendation state and shadow outcomes

## 1. Iteration identity

- Date: 2026-07-11
- Input ZIP: `bybit-reco-systems-1.0.21-outcome-capital-daily-risk.zip`
- Input SHA-256: `2fd33c50ef482a8fd18a8ba5e13e2982028cfc2b2be5ed927b2c4c4654e35a08`
- Source version: `1.0.21`
- Result version: `1.0.22`
- Scope: classification of an unconfirmed mean-reversion thesis, continued counterfactual outcome collection during safe `no_trade`, and truthful operator wording.

## 2. Project fingerprint

Fingerprint matched the Bybit Recommender repository:

- FastAPI app in `app/main.py`;
- recommendation/audit-only boundary, no order placement;
- `futures_grid`, Bybit `category=linear`, USDT perpetual;
- canonical direction helpers in `app/trading_semantics.py`;
- SQLite and PostgreSQL compatibility layer;
- frontend in `app/ui/static/`;
- operator DOCX/PDF/PNG artifacts present.

## 3. User-visible symptom and conclusion

The supplied operator screenshots showed zero effective `recommended/active` rows. Every visible candidate was rejected under `MEAN_REVERSION_EDGE_UNCONFIRMED`; the best displayed mean-reversion score was approximately `0.16` against the required `0.55`. The outcomes journal showed 123 proxy outcomes, 22.0% win rate and -1.49% mean return, with negative averages for long, short and neutral groups.

Therefore, zero actionable recommendations was a safe and expected trading decision. Forcing recommendations would have weakened fail-closed behavior. Two implementation defects nevertheless existed around this state:

1. valid-but-weak strategy evidence was rendered as a hard technical `blocked` state instead of `no_trade`;
2. all `no_trade` candidates were excluded from outcome maturation, so a prolonged no-trade regime could stop creation of new research/calibration observations and preserve selection bias.

The unfitted calibrator was not a blocker: the runtime already falls back to raw confidence until a bot-specific calibrator is fitted. The UI did not explain this clearly.

## 4. Iteration goal

After this iteration the system must:

1. keep hard data/risk/Bybit/preflight failures as `blocked`;
2. classify valid but insufficient mean-reversion edge as `no_trade`;
3. never turn that classification into an actionable recommendation;
4. allow only explicitly opted-in, complete, hard-block-free `no_trade` candidates to mature as `shadow_no_trade` proxy outcomes;
5. keep blocked, pending, malformed and legacy no-trade rows out of that sample;
6. identify shadow and actionable roots separately in outcome statistics;
7. state in the UI that outcomes are OHLCV proxies, not real exchange execution, and that an unfitted calibrator is not itself a publication blocker.

## 5. Baseline

Environment:

- Python `3.13.5`
- Node `v22.16.0`

Baseline commands and results before production changes:

- `python -m compileall -q app tests main.py` — PASSED
- `node --check app/ui/static/app.js` — PASSED
- `python -m pytest -q` — `874 passed in 26.54s`

`python -m pip check` reported an unrelated environment conflict: installed `moviepy 2.2.1` requires Pillow `<12.0`, while the shared environment contains Pillow `12.2.0`. Project dependencies were not changed.

`ruff` was unavailable in the environment.

## 6. Confirmed defects

### NR-210-1 — weak market thesis misclassified as hard block

- Severity: high
- Type: CONFIRMED DEFECT
- Files/functions: `app/recommender.py`, `_mean_reversion_grid_blocks`, recommendation publication flow
- Input: valid mean-reversion evidence on at least three timeframes with score below `0.55`
- Previous behavior: `MEAN_REVERSION_EDGE_UNCONFIRMED` entered `feasibility_blocks`; final status became `blocked`
- Expected behavior: valid but weak evidence is a strategy `no_trade`; missing/invalid evidence remains `blocked`
- Trading impact: no unsafe launch occurred, but the UI falsely described a market-thesis rejection as a technical/risk failure, impairing operator diagnosis and status semantics
- Why old tests missed it: tests asserted the existence of the mean-reversion block but did not test hard-block versus no-trade classification
- Fix: each mean-reversion decision now carries an explicit `decision`; publication separates thesis no-trade reasons from hard feasibility blocks
- Residual risk: other independent blockers may legitimately keep the same row `blocked`

### NR-210-2 — no-trade selection deadlock in outcome collection

- Severity: high
- Type: CONFIRMED DEFECT
- Files/functions: `app/recommender.py`, `app/outcomes.py`, `compute_outcomes_once`
- Input: complete `no_trade` recommendation with valid trade plan, passed deterministic risk checks and no hard blocks
- Previous behavior: SQL excluded every `no_trade` row before maturity processing
- Expected behavior: only an explicitly marked `shadow_no_trade` candidate may mature as a counterfactual research outcome; it must remain non-actionable and must not be represented as an executed trade
- Model/data impact: prolonged no-trade periods could stop new observations and leave calibration based only on previously selected actionable ideas
- Why old tests missed it: the suite tested exclusion of blocked/no-trade rows but had no explicit shadow-sampling contract
- Fix: publisher persists `outcome_policy`; outcome worker requires literal `eligible=true`, exact `sample_role=shadow_no_trade`, passed risk checks and an empty block list
- Residual risk: OHLCV proxy outcomes still cannot reproduce fills, queue position, inventory, partial execution or realised exchange PnL

### NR-210-3 — misleading outcome and calibrator wording

- Severity: medium
- Type: CONFIRMED DEFECT
- File: `app/ui/static/app.js`
- Previous behavior: modal section was titled `Что реально торговалось`, although the service stores recommendation proxy outcomes; the untrained-calibrator banner could be read as the reason no recommendations existed
- Expected behavior: distinguish proxy candidates, shadow sample and actionable roots; state explicitly that raw confidence is used before fit and the calibrator alone does not block publication
- Operational impact: reduced risk of treating proxy statistics as realised trading evidence or trying to bypass gates to “train” the system
- Fix: wording and outcome summary cards updated

## 7. Red → green evidence

New regression file: `tests/test_iteration210_no_recommendation_state.py`

Red command on the pristine source plus only the new test:

```text
python -m pytest -q tests/test_iteration210_no_recommendation_state.py
```

Material red results:

```text
KeyError: 'decision'
assert 0 == 1
assert 'Калибратор сам по себе не блокирует публикацию' in js
3 failed
```

Green command after production changes:

```text
python -m pytest -q tests/test_iteration210_no_recommendation_state.py
```

Green result:

```text
3 passed in 0.34s
```

The tests independently verify:

- weak valid mean reversion -> `no_trade`, missing evidence -> `blocked`;
- explicitly eligible shadow no-trade matures while an excluded no-trade row does not;
- outcome statistics identify the shadow sample;
- UI no longer calls proxy outcomes real trading and explains calibrator behavior.

## 8. Implementation summary

Production:

- `app/recommender.py`
  - separated hard evidence failures from weak-thesis no-trade decisions;
  - persisted explicit `outcome_policy` metadata.
- `app/outcomes.py`
  - added strict shadow-no-trade eligibility validation;
  - retained hard exclusion of blocked/suppressed/pending rows and legacy no-trade rows.
- `app/db.py`
  - added `shadow_no_trade_total`, `actionable_total` and `executed_audit_total` outcome counters.
- `app/ui/static/app.js`
  - corrected status, calibrator and proxy-outcome explanations.
- `app/main.py`
  - version `1.0.22`.

Tests:

- `tests/test_iteration210_no_recommendation_state.py`

Documentation/artifacts:

- `README.md`
- `CHANGELOG.md`
- `docs/TRADING_LOGIC.md`
- `docs/KNOWN_RISKS.md`
- `docs/SCENARIOS.md`
- `docs/MODULES.md`
- `docs/ARCHITECTURE.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- `docs/instrukciya_operatora_bybit_recommender.docx`
- `docs/instrukciya_operatora_bybit_recommender.pdf`
- `how_to_trade.png`

## 9. Compatibility

- Database schema: unchanged
- Runtime migration: not required
- SQLite support: preserved
- PostgreSQL support: preserved; JSON path expression is handled by the existing SQL translation layer
- Public API field names: unchanged
- Existing recommendation statuses: unchanged; classification is corrected within the existing `blocked`/`no_trade` contract
- Environment variables: unchanged
- Order execution boundary: unchanged; no private Bybit order method added

Old no-trade records are not retrospectively promoted into the shadow sample. This is deliberate: legacy rows lack the explicit eligibility contract and may be incomplete or blocked for reasons not represented consistently.

## 10. Post-check

- `python -m compileall -q app tests main.py` — PASSED
- `node --check app/ui/static/app.js` — PASSED
- targeted regression — `3 passed in 0.34s`
- relevant mean-reversion/outcome/UI suite — `18 passed in 1.14s`
- PostgreSQL translation/locking suite — `18 passed in 0.57s`
- collection — `877 tests collected`
- full suite — `877 passed in 24.16s`
- fresh SQLite bootstrap — PASSED, 17 tables
- idempotent SQLite re-init — PASSED
- DOCX render — 5 pages, visually inspected
- PDF render — 5 pages, visually inspected

Unavailable/limited:

- `ruff` — module not installed
- live PostgreSQL integration — no explicitly disposable test DSN supplied
- live Bybit/account/fill validation — not performed and not required for this recommendation/audit-only fix
- the user runtime database was not supplied, so the exact 123 rows shown in the screenshots were not replayed; the status and outcome paths were reproduced with deterministic fixtures

## 11. Interpretation after deployment

Version 1.0.22 may still show zero `recommended/active` rows. With weak mean-reversion evidence and negative proxy statistics this is the correct safe result. The visible difference is that valid but weak thesis rows should be `no_trade`, while genuine missing data/risk/Bybit/preflight failures remain `blocked`.

New eligible no-trade candidates can later mature as `shadow_no_trade` proxy observations. They do not become recommendations, are not bot executions and must not be used as evidence of realised profitability.

## 12. Rollback

1. Stop the application.
2. Restore the prior `1.0.21` application directory.
3. Restart using the same configuration and database.

No schema rollback is required. Outcomes created under the explicit shadow policy may remain in `reco_outcomes`; version 1.0.21 will not interpret the new summary counters, but the stored rows remain ordinary linked proxy outcomes. For a strict analytical rollback, restore a pre-1.0.22 database backup rather than selectively deleting audit records.

## 13. Recommended next work package

Run a chronological, publication-time evaluation that compares actionable roots, shadow no-trade roots and a no-trade benchmark under the same availability timestamps. Report coverage, calibration, net proxy return after costs and regime-conditioned performance. This can determine whether the system has any stable candidate edge; it must not be represented as proof of live profitability.
