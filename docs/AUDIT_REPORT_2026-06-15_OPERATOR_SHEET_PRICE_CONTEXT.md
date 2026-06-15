# Audit report — 2026-06-15 — operator-sheet price context parity

## Scope and system boundary

Focused continuation of the requested deep Bybit Linear USDT futures re-audit. The repository boundary remains unchanged: this codebase is a recommendation + operator preflight/fail-closed layer, not a live OMS/EMS. It does not manage exchange-side order lifecycle, fills, partial fills, retries, live wallet balance, exact liquidation truth or Bybit reconciliation. Those remain requirements for an external execution/reconciliation layer.

Before patching I reviewed the required source-of-truth documents and recent audit history:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest 2026-06-15 audit reports, especially operator-sheet runtime risk, directional qty parity, UI/backend direction parity and exit-payload fail-closed patches.

## Baseline before changes

Commands run from project root before changes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest --collect-only -q
```

Baseline results:

- `python -m compileall -q app tests main.py`: PASS.
- `node --check app/ui/static/app.js`: PASS.
- Monolithic `pytest -q`: no failure observed before local sandbox timeout; run reached early progress output and did not produce a final summary within the available execution window.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest --collect-only -q`: 702 tests collected before this patch.
- Partial grouped baseline runs completed before the local command window ended:
  - `tests/test_api.py`: 44 passed.
  - `tests/test_grid_linear_economics.py`: 13 passed.
  - `tests/test_iteration10*.py`: 53 passed.

Targeted red evidence for the new regression tests before the fix:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_iteration183_operator_sheet_price_context.py --tb=short
FFF [100%]

FAILED test_directional_exit_payload_reads_operator_sheet_price_context_fail_closed_display
- payload["reference_price"] was None instead of 100.0.

FAILED test_strict_preflight_still_blocks_operator_sheet_only_payload_without_full_trade_plan
- strict validation emitted missing reference/kill-switch field errors because operator_sheet price context was ignored.

FAILED test_operator_ui_uses_operator_sheet_kill_switch_fallback_for_legacy_payloads
- UI did not have an operatorSheet.kill_switch fallback for operator-sheet-only display.
```

## Source-of-truth summary

- `docs/KNOWN_RISKS.md`: repository is not a live OMS/EMS. External execution must still re-check Bybit metadata, open positions/orders, wallet balance, fills, funding and liquidation truth.
- `docs/TRADING_LOGIC.md`: futures-grid execution remains Bybit Linear USDT scoped and fail-closed. Missing or malformed execution plan fields must block strict preflight.
- `docs/ARCHITECTURE.md` / `docs/MODULES.md`: `app/main.py` owns API/operator execution checks and payload enrichment; `app/recommender.py` generates operator payloads; `app/trading_semantics.py` is the canonical long/short/neutral source of truth.
- `app/trading_semantics.py`: long TP above entry and SL below; short TP below entry and SL above; neutral grid has no single directional TP; Bybit one-way protective exits are reduce-only/close-on-trigger with canonical triggerDirection.

## Trading-semantics map reviewed

Reviewed single-source directional math and related consumers:

- `app/trading_semantics.py`: canonical direction normalization, TP/SL mapping, directional gross PnL, risk/reward, Bybit one-way open/close side mapping, protective order semantics and trigger-direction geometry.
- `app/main.py`: `_trade_plan_price_context`, `_directional_exit_payload_for_reco`, execution preflight, Bybit metadata validation, runtime risk caps and operator payload enrichment.
- `app/ui/static/app.js`: `buildOperatorValues`, frontend fallback display for range/reference/kill-switch, backend directional payload fail-closed rendering.
- Existing tests for short TP/SL UI hardening, invalid exit fail-closed payloads, UI/backend directional parity, operator-sheet runtime risk and operator-sheet directional qty parity.

No new Buy/Sell side mapping, reduceOnly/closeOnTrigger logic, triggerDirection logic or real order lifecycle code was added.

## Finding and fix

### MEDIUM — operator-sheet-only price context was ignored by backend TP/SL display and frontend kill-switch fallback

- **Files**:
  - `app/main.py`, lines 2595-2638.
  - `app/ui/static/app.js`, lines 651-680.
  - `tests/test_iteration183_operator_sheet_price_context.py`, lines 28-115.
- **Problem**:
  - `_trade_plan_price_context(...)` read reference, range, kill-switch and TP-per-leg context only from `params.trade_plan` / legacy top-level params.
  - Recent patches already made `params.operator_sheet` an accepted operator-facing transfer representation for sizing, economics, leverage and UI display parity.
  - For a legacy/operator-sheet-only directional row, backend `_directional_exit_payload_for_reco(...)` returned `reference_price=None`, `take_profit=None`, `stop_loss=None` and `DIRECTIONAL_ENTRY_PRICE_MISSING` even though `operator_sheet.price_ref` and `operator_sheet.kill_switch` carried enough information to render directional TP/SL safely.
  - Frontend `buildOperatorValues(...)` already used `operatorSheet` for range/reference/leverage, but kill-switch display still read only `trade_plan.levels.kill_switch`.
- **Why this is an error**:
  - API and UI could blank or block directional TP/SL display for an operator-sheet-only payload while other operator values came from the sheet. This created backend ↔ frontend source-parity drift around TP/SL/kill-switch display.
  - For shorts specifically, a missing backend payload causes the UI to fall back to kill-switch-only rendering. If kill-switch values are also not read from `operatorSheet.kill_switch`, the display becomes uninformative exactly where fail-closed operator clarity is needed.
- **Financial/trading risk**:
  - Indirect operator-risk: the execution path remains fail-closed for missing full `trade_plan`, but the dashboard/API could hide or blank actionable directional geometry diagnostics. That can mislead review of legacy/manual rows and weaken operator situational awareness.
- **Fix**:
  - Added `params.operator_sheet` as a read-only fallback in `_trade_plan_price_context(...)` for reference price, range lower/upper, kill-switch lower/upper, grid step, grid levels and TP-per-leg context.
  - Explicitly preserved strict preflight behavior: an absent full `params.trade_plan` still emits `TRADE_PLAN_MISSING` and remains blocked at execution time.
  - Added `operatorSheetKillSwitch` fallback in `app/ui/static/app.js::buildOperatorValues(...)` so frontend kill-switch display uses the same operator-sheet source as backend display context.
  - No fail-closed blocker was downgraded. No execution lifecycle code, Bybit order placement, reduceOnly/closeOnTrigger mapping or leverage/risk caps were weakened.

## Tests added and red→green evidence

New test file:

- `tests/test_iteration183_operator_sheet_price_context.py`
  - `test_directional_exit_payload_reads_operator_sheet_price_context_fail_closed_display`
    - verifies backend directional exit payload reads `operator_sheet.price_ref` and `operator_sheet.kill_switch` for display math, including short TP below reference and SL above reference.
  - `test_strict_preflight_still_blocks_operator_sheet_only_payload_without_full_trade_plan`
    - verifies strict preflight still blocks a legacy/operator-sheet-only payload with `TRADE_PLAN_MISSING`, while no longer reporting reference/kill-switch as missing when they exist in the operator sheet.
  - `test_operator_ui_uses_operator_sheet_kill_switch_fallback_for_legacy_payloads`
    - verifies frontend kill-switch fallback includes `operatorSheetKillSwitch`.

Red run before fix:

```text
3 failed in 2.50s
```

Green run after fix:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_iteration183_operator_sheet_price_context.py --tb=short
3 passed in 2.44s
```

Related regression run after fix:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q \
  tests/test_iteration151_operator_distance_and_ui_failclosed.py \
  tests/test_iteration165_operator_payload_consistency.py \
  tests/test_iteration181_operator_sheet_runtime_size_caps.py \
  tests/test_iteration182_operator_sheet_directional_qty_parity.py \
  tests/test_iteration183_operator_sheet_price_context.py --tb=short
15 passed in 3.23s
```

Iteration-18x focused run after fix:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_iteration18*.py --tb=short
13 passed in 3.29s
```

## Final verification

Post-fix checks:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest --collect-only -q
```

Results:

- `python -m compileall -q app tests main.py`: PASS.
- `node --check app/ui/static/app.js`: PASS.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest --collect-only -q`: 705 tests collected after adding 3 tests.
- New targeted tests: 3 passed.
- Related regression tests: 15 passed.
- Iteration-18x focused group: 13 passed.
- Monolithic full-suite `pytest -q` could not be completed in the local sandbox within the available execution window; no failure was observed before timeout. This is an environment/runtime limitation, not a known test failure.
- `npm` / `yarn` tests were not run because there is no `package.json` in the project root.

## Static scan

Focused static scan note saved to:

- `docs/STATIC_SCAN_2026-06-15_OPERATOR_SHEET_PRICE_CONTEXT.txt`

Reviewed changed/new hits were safe hardening only:

- `app/main.py::_trade_plan_price_context`: read-only operator-sheet fallback for display/context parity; strict preflight still blocks missing full `trade_plan`.
- `app/ui/static/app.js::buildOperatorValues`: frontend kill-switch fallback aligned with backend context.
- `tests/test_iteration183_operator_sheet_price_context.py`: regression coverage for backend display parity, frontend fallback and strict preflight remaining fail-closed.

## Residual risks and changes relative to `docs/KNOWN_RISKS.md`

- No residual risk in `docs/KNOWN_RISKS.md` was weakened or removed.
- The repository still has no live OMS/EMS and no exchange-side order/fill/reconciliation truth.
- External execution/reconciliation must still re-check Bybit metadata, account mode, wallet balance, available margin, qty step, min notional, live price, open positions, open orders, fills, funding and liquidation truth immediately before real execution.
- Proxy outcome labels and advisory calibration remain residual model risks.
- This patch closes a display/context parity gap for operator-sheet price data. It does not claim to solve external execution-layer risks.

## Files changed

- `app/main.py`
- `app/ui/static/app.js`
- `tests/test_iteration183_operator_sheet_price_context.py`
- `docs/AUDIT_REPORT_2026-06-15_OPERATOR_SHEET_PRICE_CONTEXT.md`
- `docs/STATIC_SCAN_2026-06-15_OPERATOR_SHEET_PRICE_CONTEXT.txt`
