# Audit report — 2026-06-15 — operator-sheet runtime risk caps

## Scope and starting point

Bounded offline re-audit of the uploaded Bybit Linear USDT futures recommendation/preflight repository. I followed the requested order before patching:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest audit reports dated 2026-06-15 and 2026-06-14.

The system boundary remains unchanged: this repository is a recommendation + operator preflight/fail-closed layer, not a live OMS/EMS. It does not manage live Bybit order lifecycle, fills, partial fills, retries or exchange-side reconciliation. Those remain requirements for an external execution/reconciliation layer.

## Baseline before changes

Commands run from project root before code changes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q --tb=short
pytest --collect-only -q
```

Baseline results:

- `python -m compileall -q app tests main.py`: PASS.
- `node --check app/ui/static/app.js`: PASS.
- `pytest --collect-only -q`: 694 tests collected.
- Monolithic `pytest -q --tb=short`: no failures were observed before the local sandbox timeout; the run did not produce a final summary within the available execution window.

Targeted red evidence for the new regression tests before the fix:

```text
pytest -q tests/test_iteration181_operator_sheet_runtime_size_caps.py --tb=short
FF [100%]

FAILED test_runtime_risk_caps_use_operator_sheet_leverage_notional_and_margin
- only LEVERAGE_MISSING_AT_EXECUTION was returned; operator_sheet leverage/notional/margin were ignored.

FAILED test_runtime_risk_caps_fail_closed_when_operator_sheet_size_context_is_unpriced
- POSITION_SIZE_MISSING_AT_EXECUTION was not returned for operator_sheet.sizing-only context.
```

## Required source-of-truth summary

- `docs/KNOWN_RISKS.md`: the repository is not a live OMS/EMS; external execution must still re-check wallet balance, Bybit metadata, open positions/orders, fills, funding and liquidation truth.
- `docs/TRADING_LOGIC.md`: futures-grid logic must remain Bybit Linear USDT scoped, fail-closed at execution preflight, and not silently downgrade blockers to warnings.
- `docs/ARCHITECTURE.md` / `docs/MODULES.md`: `app/main.py` owns API/operator execution checks, `app/recommender.py` generates recommendation economics, `app/risk.py` normalizes runtime caps, and `app/trading_semantics.py` is the canonical long/short/neutral source of truth.
- `app/trading_semantics.py`: long TP is above entry and SL below; short TP is below entry and SL above; neutral grid has no directional TP; Bybit one-way linear close/protection orders are reduce-only/close-on-trigger and use canonical trigger directions.

## Trading-semantics map reviewed

Single-source directional model and related consumers reviewed:

- `app/trading_semantics.py`: canonical long/short/neutral normalization, directional TP/SL mapping, gross PnL, risk/reward, Bybit one-way side mapping, reduce-only close/protection semantics and trigger-direction geometry.
- `app/main.py`: recommendation payload enrichment, directional exit payload construction, Bybit metadata snapping, execution preflight, runtime size/leverage/notional/margin guards and materialization guard.
- `app/recommender.py`: generated grid economics, leverage profile, estimated notional/margin, funding and liquidation-buffer constraints.
- `app/ui/static/app.js`: operator display for side, TP/SL, position size, margin, worst-case exposure and risk/reward.
- Existing regression tests for directional TP/SL, short UI rendering, Bybit protective order semantics, invalid exit fail-closed handling, worst-case notional/qty and operator-sheet preflight compatibility.

No new TP/SL inversion, Bybit `Buy`/`Sell` inversion, `reduceOnly` weakening or frontend/back-end TP/SL drift was found.

## Finding and fix

### HIGH — execution-time runtime risk caps ignored operator-sheet-only leverage and size fields

- **Files**:
  - `app/main.py`, lines 2348-2371.
  - `tests/test_iteration181_operator_sheet_runtime_size_caps.py`, lines 36-85.
- **Problem**:
  - `_execution_runtime_size_risk_blocks(...)` read leverage only from `params.leverage` and `trade_plan.leverage`.
  - Its sizing/economics source list read `params.sizing`, `trade_plan.sizing`, `params.economics`, `trade_plan.economics`, risk reports and top-level `params`/`trade_plan`, but not `params.operator_sheet.sizing`, `params.operator_sheet.economics`, `params.operator_sheet.risk_report` or direct fields on `params.operator_sheet`.
  - Earlier preflight/UI patches already made `operator_sheet` an accepted operator transfer representation. Runtime caps therefore had a parity gap with strict preflight and UI.
- **Why this is an error**:
  - An operator-sheet-only payload with `leverage=8`, `estimated_max_position_notional_usdt=1200` and `estimated_margin_required_usdt=150` could avoid max leverage/notional/margin cap checks if those same values were absent from legacy `params` / `trade_plan` fields.
  - An operator-sheet-only `qty_per_order` without price/notional context did not trigger the fail-closed `POSITION_SIZE_MISSING_AT_EXECUTION` block even though runtime size caps were active.
- **Financial/trading risk**:
  - This could let an operator materialization path under-enforce tightened runtime caps for launch-sheet payloads whose sizing lived only under `params.operator_sheet`.
  - The issue did not create a live order lifecycle bug because the repository still has no OMS/EMS, but it weakened the fail-closed recommendation/execution guard.
- **Fix**:
  - Added `operator_sheet = params.operator_sheet` extraction in `_execution_runtime_size_risk_blocks(...)`.
  - Added `operator_sheet.sizing`, `operator_sheet.economics`, `operator_sheet.risk_report` and direct `operator_sheet` fields to the runtime sizing/economics source list.
  - Added `operator_sheet.leverage` as a fallback after `params.leverage` and `trade_plan.leverage`.
  - No Bybit side mapping, TP/SL geometry, triggerDirection, reduceOnly/closeOnTrigger logic, execution lifecycle or UI rendering was weakened.

## Tests added and red→green evidence

New test file:

- `tests/test_iteration181_operator_sheet_runtime_size_caps.py`
  - `test_runtime_risk_caps_use_operator_sheet_leverage_notional_and_margin`
  - `test_runtime_risk_caps_fail_closed_when_operator_sheet_size_context_is_unpriced`

Red run before fix:

```text
2 failed in 1.99s
```

Green targeted run after fix:

```text
pytest -q tests/test_iteration181_operator_sheet_runtime_size_caps.py --tb=short
2 passed in 1.86s
```

Related regression run after fix:

```text
pytest -q tests/test_iteration154_execution_runtime_risk_caps.py \
          tests/test_iteration155_deep_directional_risk_patch.py \
          tests/test_iteration165_operator_payload_consistency.py \
          tests/test_iteration169_grid_worst_case_notional.py \
          tests/test_iteration181_operator_sheet_runtime_size_caps.py --tb=short
22 passed in 2.80s
```

## Final verification

Post-fix checks:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest --collect-only -q
```

Results:

- `python -m compileall -q app tests main.py`: PASS.
- `node --check app/ui/static/app.js`: PASS.
- `pytest --collect-only -q`: 696 tests collected.
- Targeted and related regression tests: 22 passed.
- Grouped post-fix test runs with plugin autoload disabled covered all 696 collected tests by file-pattern groups: 696 passed, 0 failed. The monolithic full-suite run in one pytest process still did not produce a final summary before sandbox timeout; no failure was observed before timeout. Grouped runs were used to avoid the local long-running monolithic process limitation.

Grouped post-fix counts:

- `tests/test_api.py`: 44 passed.
- `tests/test_grid_linear_economics.py`: 13 passed.
- `tests/test_iteration10*.py`: 53 passed.
- `tests/test_iteration11*.py`: 45 passed.
- `tests/test_iteration12*.py`: 30 passed.
- `tests/test_iteration13*.py`: 27 passed.
- `tests/test_iteration14*.py`: 41 passed.
- `tests/test_iteration15*.py`: 60 passed.
- `tests/test_iteration16*.py`: 103 passed.
- `tests/test_iteration17*.py`: 34 passed.
- `tests/test_iteration18*.py`: 4 passed.
- `tests/test_iteration6*.py`: 33 passed.
- `tests/test_iteration7[0-2]*.py`: 10 passed.
- `tests/test_iteration7[3-9]*.py`: 26 passed.
- `tests/test_iteration8*.py`: 37 passed.
- `tests/test_iteration9*.py`: 39 passed.
- `tests/test_logic.py`: 83 passed.
- `tests/test_sentiment_pipeline.py`: 14 passed.

`npm` / `yarn` tests were not run because no `package.json` is present in the project root.

## Static scan

A bounded static scan note was saved to:

- `docs/STATIC_SCAN_2026-06-15_OPERATOR_SHEET_RUNTIME_RISK.txt`

Reviewed changed/new hits were safe hardening: the patch only extends execution-time runtime cap inputs to the already accepted operator-sheet representation.

## Residual risks and changes relative to `docs/KNOWN_RISKS.md`

- No residual risk in `docs/KNOWN_RISKS.md` was weakened or removed.
- The repository still has no live OMS/EMS and no exchange-side order/fill/reconciliation truth.
- External execution/reconciliation must still re-check Bybit metadata, account mode, available balance, margin, qty step, min notional, live price, open positions and order lifecycle immediately before real execution.
- Proxy outcome labels and advisory calibration remain residual model risks.
- This patch closes an execution-time runtime cap parity gap for operator-sheet payloads; it does not claim to solve external execution-layer risks.

## Files changed

- `app/main.py`
- `tests/test_iteration181_operator_sheet_runtime_size_caps.py`
- `docs/AUDIT_REPORT_2026-06-15_OPERATOR_SHEET_RUNTIME_RISK.md`
- `docs/STATIC_SCAN_2026-06-15_OPERATOR_SHEET_RUNTIME_RISK.txt`
