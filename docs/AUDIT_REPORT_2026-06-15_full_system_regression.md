# AUDIT REPORT 2026-06-15 - Bybit Linear USDT Futures recommender regression audit

## Scope

Repository: Bybit Linear USDT futures / grid-only recommendation and operator-preflight service.

Requested scope: strict regression audit against the canonical directional model, Bybit Linear USDT semantics, risk management, UI/backend parity, static scan of TP/SL/side/risk logic, baseline/post quality gates, and corrected archive delivery.

System boundary confirmed from `docs/KNOWN_RISKS.md`: this repository is a recommendation/audit service and fail-closed operator preflight, not a live OMS/EMS. Live order lifecycle, fills, partial fills, exchange reconciliation, balance truth and exact liquidation truth remain requirements for an external execution/reconciliation layer.

## Initial documents reviewed

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- Latest `docs/AUDIT_REPORT_*`: none were present in the supplied ZIP before this report. `docs/KNOWN_RISKS.md` did include 2026-06-14 audit additions and residual-risk notes.

## Baseline before changes

Commands executed from repository root:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
```

Baseline results:

| Check | Result |
|---|---:|
| `python -m compileall -q app tests main.py` | pass, exit 0 |
| `node --check app/ui/static/app.js` | pass, exit 0 |
| `pytest -q` | 697 passed / 8 failed |

The 8 baseline failures were all release-artifact consistency failures:

- missing `CHANGELOG.md`;
- missing `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- missing `how_to_trade.png`;
- missing `docs/instrukciya_operatora_bybit_recommender.docx`;
- missing `docs/instrukciya_operatora_bybit_recommender.pdf`.

The failed tests were:

- `tests/test_iteration108_outcome_queue_and_docs_audit.py::test_release_docs_do_not_reference_external_audit_report_artifacts`
- `tests/test_iteration111_postgres_row_locking_and_release_artifacts.py::test_release_history_does_not_require_audit_report_artifacts`
- `tests/test_iteration112_redteam_integrity_and_bybit_meta.py::test_release_docs_omit_audit_report_artifact_references`
- `tests/test_iteration162_docs_and_infographic_sync.py::test_how_to_trade_source_documents_non_oms_scope_and_3x_5x_gate`
- `tests/test_iteration162_docs_and_infographic_sync.py::test_root_how_to_trade_png_exists_and_is_nonempty_png`
- `tests/test_iteration163_payload_guard_status.py::test_how_to_trade_source_mentions_complete_trade_plan_payload`
- `tests/test_iteration91_sentiment_release_hardening.py::test_release_artifacts_are_present_and_cross_referenced`
- `tests/test_iteration98_release_artifact_integrity.py::test_readme_release_checks_reference_existing_artifacts`

## Canonical directional model audit

### Single source of truth

The canonical long/short/neutral source is `app/trading_semantics.py`:

- `normalize_execution_direction()` and supported direction sets: lines 12-27.
- `directional_exit_levels()` maps long TP=upper/SL=lower, short TP=lower/SL=upper, neutral=no directional TP: lines 49-94.
- `validate_directional_exit_geometry()` fail-closed geometry checks: lines 96-135.
- `directional_trade_math()` gross PnL, reward/risk percent and risk/reward, with invalid geometry returning `None`: lines 154-208.
- `bybit_linear_order_semantics()` one-way open/close side mapping: lines 215-262.
- `bybit_linear_protective_order_semantics()` and trigger direction mapping for TP/SL: lines 271-293.
- `validate_protective_trigger_geometry()` and `bybit_linear_protective_order_plan()`: lines 297-378.

### Backend usage

Backend/API rendering and validation route through the canonical module:

- `_directional_exit_payload_for_reco()` builds `directional_exit_levels`, geometry validation, trade math and protective-order plans: `app/main.py` lines 922-975.
- `_augment_reco_for_ui()` exposes `directional_exit_levels` to UI/API responses: `app/main.py` lines 1505 and 1542.
- `_validate_trade_plan_against_bybit_meta()` validates directional TP/SL geometry during strict preflight: `app/main.py` lines 2978-2984.

No new bypass of `app.trading_semantics` was found in backend TP/SL mapping.

### Frontend usage

UI rendering uses backend payload as authoritative for linear directional recommendations:

- local fallback `operatorExitLevels()` exists for non-authoritative fallback labels: `app/ui/static/app.js` lines 570-605;
- `operatorExitLevelsFromBackend()` blocks missing/invalid/mismatched backend payload for linear directional ideas: `app/ui/static/app.js` lines 610-648;
- `buildOperatorValues()` requires backend `directional_exit_levels` for `venue=linear` and `direction=long|short`: `app/ui/static/app.js` lines 681-699;
- detail panel displays risk/reward and distances from backend `trade_math`: `app/ui/static/app.js` lines 994-1021.

Existing regression coverage for this path includes `test_iteration147`, `148`, `153`, `156`, `157`, `158`, `161`, `167`, `183`, and `184`.

### Bybit Linear USDT semantics

Current canonical mapping matches the requested model:

| Direction/action | Expected | Current source |
|---|---|---|
| Open long | `Buy`, `reduceOnly=false`, `positionIdx=0` | `bybit_linear_order_semantics()` |
| Close/protect long | `Sell`, `reduceOnly=true`, `closeOnTrigger=true` for protective plan | `bybit_linear_order_semantics()` / `bybit_linear_protective_order_semantics()` |
| Open short | `Sell`, `reduceOnly=false`, `positionIdx=0` | `bybit_linear_order_semantics()` |
| Close/protect short | `Buy`, `reduceOnly=true`, `closeOnTrigger=true` for protective plan | `bybit_linear_order_semantics()` / `bybit_linear_protective_order_semantics()` |
| Long TP / short SL trigger | rising, `triggerDirection=1` | `_protective_trigger_direction()` |
| Long SL / short TP trigger | falling, `triggerDirection=2` | `_protective_trigger_direction()` |

No fail-open change was made.

## Math, economics, risk and time-series checks

### Gross/net and sign conventions observed

- `directional_trade_math()` returns gross directional PnL and distance metrics; fees/funding are not included there by design.
- Grid economics in `app/grid_math.py` separates gross bps, execution cost, signed funding diagnostics, adverse funding cost and net bps: lines 144-197.
- `funding_cashflow_usdt()` uses positive value as cost paid by the side; positive funding is adverse to long and beneficial to short, negative funding is the opposite: `app/grid_math.py` lines 72-89.
- `linear_pnl_usdt()` handles long/short sign symmetrically and returns zero for unknown side: `app/grid_math.py` lines 52-66.
- Approximate liquidation helpers are explicitly not exchange truth and are used only for conservative buffer gates: `app/grid_math.py` lines 98-142.

### Risk / preflight observations

Strict Bybit plan validation remains fail-closed for:

- unsupported bot type / venue / symbol and non-linear/non-USDT scope;
- missing or mismatched Bybit metadata;
- missing `trade_plan` fields when execution plan is required;
- off-tick price/step/TP and invalid range/kill-switch geometry;
- invalid directional TP/SL geometry;
- unsupported grid type and grid count above Bybit futures grid cap;
- missing/invalid leverage, leverage outside Bybit filter or off leverage step;
- liquidation buffer too low for leveraged linear recommendations;
- qty/min-notional/min-order/max-order validation and qty/notional mismatch;
- non-positive or too-thin grid economics.

Relevant validation source: `app/main.py` lines 2711-3425.

### Econometric / time-series audit

- Feature layer normalizes OHLCV rows chronologically, deduplicates timestamps and rejects non-finite or impossible candles before indicator calculation: `app/features.py` lines 37-84.
- Outcome labeling enters from the first tradeable candle strictly after the signal reference candle, reducing same-bar look-ahead optimism: `app/outcomes.py` `_get_first_tradeable_candle_after()`.
- Calibration remains proxy-based. The residual risk is already acknowledged in `docs/KNOWN_RISKS.md`; no live fill/funding/liquidation truth exists in this repository.

No new leakage fix was made in this pass because the offline regression suite already covers the known guardrails and no concrete new failing leakage case was found.

## Static scan summary

A targeted static scan was run over `app`, `tests`, `docs`, `README.md`, `.env.example`, and `CHANGELOG.md` for:

`tp`, `sl`, `take_profit`, `stop_loss`, `kill_switch`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `closeOnTrigger`, `triggerDirection`, `pnl`, `roi`, `risk_reward`, `leverage`, `notional`, `qty_step`, `min_notional`, `upper`, `lower`.

No previous `docs/STATIC_SCAN_*` file was present, so there was no historical scan to diff against. High-risk hits were manually mapped to:

- canonical semantics: `app/trading_semantics.py`;
- risk/execution preflight: `app/main.py`;
- UI parity/fail-closed rendering: `app/ui/static/app.js`;
- grid PnL/funding/liquidation helpers: `app/grid_math.py`;
- proxy outcome labeling: `app/outcomes.py`;
- tests documenting the semantics: `tests/test_iteration147` through `tests/test_iteration184` plus older risk/preflight suites.

Result: no new unsafe implementation of directional TP/SL or Bybit side mapping outside the canonical module was found. Local UI fallback is guarded by backend payload availability and direction/geometry checks for linear directional ideas.

## Findings and fixes

### Finding 1 - Release package referenced artifacts that were not shipped

- Severity: HIGH for release integrity and reproducibility; LOW direct trading risk.
- Files/tests showing the defect:
  - `README.md` lines 56-57 and 73-79 reference operator DOCX/PDF and infographic artifacts.
  - `tests/test_iteration91_sentiment_release_hardening.py` lines 170-190 expects those release artifacts and `CHANGELOG.md`.
  - `tests/test_iteration98_release_artifact_integrity.py` lines 24-32 expects DOCX/PDF artifacts.
  - `tests/test_iteration162_docs_and_infographic_sync.py` lines 37-61 expects infographic source and PNG.
  - `tests/test_iteration163_payload_guard_status.py` lines 90-93 expects the complete trade-plan statement in the infographic source.
- Why it is an error: the supplied ZIP could not pass its own release-integrity tests from a clean checkout; operator documentation referenced in README was missing. This breaks reproducibility of the audited delivery and can leave the operator without the required NO TRADE / leverage / OMS-boundary quick reference.
- Financial/trading risk: indirect. The missing docs do not execute trades, but they remove an operator-facing guardrail explaining that the system is not OMS/EMS, that 3-5x is the shipped actionable leverage interval, and that invalid market/reference/payload states are NO TRADE.
- Fix implemented:
  - Added `CHANGELOG.md` lines 1-14.
  - Added `docs/HOW_TO_TRADE_INFOGRAPHIC.md` lines 1-54 with OMS/EMS boundary, 3-5x profile, short TP/SL, `INVALID_MARKET_REFERENCE_PRICE`, and complete `params.trade_plan` guard text.
  - Added `how_to_trade.png` as a quick-reference infographic.
  - Added `docs/instrukciya_operatora_bybit_recommender.docx` and `docs/instrukciya_operatora_bybit_recommender.pdf`.
  - DOCX rendered to PNG and visually checked. PDF rendered to PNG and checked. No clipping/overlap detected after the second render.
- Red -> green evidence:
  - The 8 baseline release-artifact tests listed above failed before the fix and passed after adding the artifacts.

### Finding 2 - No new critical/high directional or Bybit-side code bug found

- Severity: informational.
- Files inspected: `app/trading_semantics.py`, `app/main.py`, `app/ui/static/app.js`, `app/grid_math.py`, `app/outcomes.py`, `app/calibration.py`, `app/recommender.py`, `app/risk.py`, documentation and regression tests.
- Result: no code changes were made to directional math or execution guards because existing canonical semantics and tests already match the requested model.
- Rationale: making unnecessary changes in `app/main.py` or `app/recommender.py` would increase regression risk without improving safety.

## Tests and verification after changes

Commands executed from repository root:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
python -m ruff check app tests main.py
```

Post-change results:

| Check | Result |
|---|---:|
| `python -m compileall -q app tests main.py` | pass, exit 0 |
| `node --check app/ui/static/app.js` | pass, exit 0 |
| `pytest -q` | 705 passed / 0 failed |
| `python -m ruff check app tests main.py` | not executed successfully: `ruff` is listed in `requirements-dev.txt` but is not installed in this container (`No module named ruff`) |

No `package.json`, `yarn.lock`, or `package-lock.json` was present, so no npm/yarn test or lint command was available beyond `node --check app/ui/static/app.js`.

## Baseline vs post counts

| Suite | Baseline | Post |
|---|---:|---:|
| pytest | 697 passed / 8 failed | 705 passed / 0 failed |
| compileall | pass | pass |
| node syntax check | pass | pass |

No previously green test regressed.

## Residual risks relative to `docs/KNOWN_RISKS.md`

Still open and not solved by this repository:

1. No real OMS/EMS and no live exchange truth for orders/fills/reconciliation.
2. Outcome labels remain proxy labels, not exchange PnL/funding/liquidation truth.
3. Exact liquidation price and wallet-balance checks require external executor/account data.
4. Public Bybit REST can be stale/incomplete; repository mitigates with fail-closed guards but cannot prove live execution truth.
5. LLM review remains secondary and must not replace risk/scoring/preflight.
6. Telegram alerts remain best-effort.
7. SQLite remains single-node practical storage, not multi-node production truth.
8. `ruff` quality gate could not be run in this offline container because the tool is not installed.

Closed in this pass:

- Release artifact mismatch in the supplied ZIP: the repository now ships the documentation/infographic files already referenced by README and required by tests.

## Files added/changed in this pass

Added:

- `CHANGELOG.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- `docs/instrukciya_operatora_bybit_recommender.docx`
- `docs/instrukciya_operatora_bybit_recommender.pdf`
- `how_to_trade.png`
- `docs/AUDIT_REPORT_2026-06-15_full_system_regression.md`

No application source-code files were changed.

## Final conclusion

The supplied ZIP was not release-test clean at baseline because required operator documentation artifacts were missing. After restoring those artifacts and adding this report, the full offline quality gate available in the container is green: `705 passed`, `compileall` passed, and `node --check` passed.

Directional TP/SL, PnL, Bybit one-way side mapping, protective trigger direction, UI/backend parity, leverage/min-notional/range/kill-switch validation and fail-closed operator guards were audited against the canonical model. No new critical/high trading-code defect was found, and no fail-closed guard was weakened.
