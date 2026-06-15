# Audit report — 2026-06-15 — UI backend exit payload fail-closed

## Scope and intake

Offline regression re-audit of the uploaded Bybit futures / Linear USDT recommender archive. Work order followed the attached deep-audit prompt: review repository boundaries and canonical semantics first, establish baseline, then make only fail-closed corrections with regression coverage.

Read before changes:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest audit reports:
  - `docs/AUDIT_REPORT_2026-06-15_3X_5X_LEVERAGE_SYNC.md`
  - `docs/AUDIT_REPORT_2026-06-15_DEEP_BYBIT_REAUDIT_PATCH.md`
  - `docs/AUDIT_REPORT_2026-06-15_ESTIMATED_MAX_QTY_UI_PARITY.md`
  - `docs/AUDIT_REPORT_2026-06-15_OPERATOR_MINIMUM_FLOOR.md`
  - `docs/AUDIT_REPORT_2026-06-15_OPERATOR_SHEET_DIRECTIONAL_QTY_PARITY.md`
  - `docs/AUDIT_REPORT_2026-06-15_OPERATOR_SHEET_RUNTIME_RISK.md`
  - `docs/AUDIT_REPORT_2026-06-15_RECO_FILTER_PERSISTENCE.md`
  - `docs/AUDIT_REPORT_2026-06-15_WORST_CASE_QTY_KEY_PARITY.md`

Confirmed system boundary from `KNOWN_RISKS.md`: this repository is a recommendation + operator preflight/fail-closed service, not a live OMS/EMS. Real open-order lifecycle, partial fills, exchange-side reconciliation, live private liquidation truth, idempotent order management and private Bybit account state remain external execution/reconciliation-layer requirements.

## Baseline before changes

Commands run from project root before edits:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q <suite split by test files>
```

Baseline results:

- `python -m compileall -q app tests main.py`: passed, exit `0`.
- `node --check app/ui/static/app.js`: passed, exit `0`.
- Plain `pytest -q`: printed progress through the suite but did not produce a final summary before the container timeout/teardown issue.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` chunked baseline against the uploaded original archive:
  - chunk 1: `147 passed`
  - chunk 2: `62 passed`
  - chunk 3: `101 passed`
  - chunk 4: `128 passed`
  - chunk 5: `79 passed`
  - chunk 6: `76 passed`
  - chunk 7: `106 passed`
  - total baseline: `699 passed`.

The chunked run covers the same collected original test files and avoids unrelated environment/plugin teardown behavior.

## Trading semantics / risk map reviewed

Single source of truth and main consumers:

- `app/trading_semantics.py`: canonical long/short/neutral normalization, TP/SL mapping, directional gross PnL and risk/reward, Bybit one-way open/close side mapping, protective TP/SL `reduceOnly`, `closeOnTrigger`, `triggerDirection` and trigger geometry validation.
- `app/main.py`: UI/API augmentation, `_directional_exit_payload_for_reco`, `_directional_exit_qty_for_reco`, execution preflight, runtime risk limits, Bybit metadata validation, publication-chain freshness and operator next-actions.
- `app/recommender.py`: scoring, signal/range quality, futures-grid economics, cost/funding model, leverage interval selection, grid spacing, sizing, liquidation-buffer checks and no-trade/recommendation state.
- `app/grid_math.py`: linear USDT PnL, funding cashflow sign convention, margin and approximate liquidation helpers.
- `app/ui/static/app.js`: operator details panel for direction, TP/SL, risk/reward, worst-case notional/qty/margin, filters and launchability.
- `app/outcomes.py` and `app/calibration.py`: proxy outcomes and advisory calibration remain residual model-risk areas, not execution truth.

No new backend long/short TP/SL inversion, Bybit `Buy`/`Sell` inversion, `reduceOnly` weakening, `closeOnTrigger` weakening or triggerDirection inversion was found in this bounded pass.

## Finding and fix

### HIGH — UI could render local directional TP/SL for linear long/short when backend canonical exit payload was absent

- **Files:**
  - `app/ui/static/app.js`, lines 667-681.
  - `tests/test_iteration148_directional_semantics_hardening.py`, lines 187-193.
  - `tests/test_iteration183_ui_requires_backend_exit_payload.py`, lines 53-116.
- **Problem:**
  - `buildOperatorValues()` created a local `operatorExitLevels(direction, killLower, killUpper)` mapping and passed it as fallback into `operatorExitLevelsFromBackend((it || {}).directional_exit_levels, exits, meta)`.
  - If `directional_exit_levels` was missing, `operatorExitLevelsFromBackend()` returned the fallback.
  - For a linear short with `kill_switch.lower=95`, `kill_switch.upper=105`, and no backend canonical payload, the old UI still displayed directional `TP=95.0`, `SL=105.0`.
- **Why this is an error:**
  - Directional TP/SL rendering for linear long/short must be grounded in backend `app.trading_semantics` output, including geometry validation and reference-price checks.
  - The local JS fallback bypassed the backend payload contract when that payload was absent. That creates a backend↔frontend parity gap exactly in the fail-closed path.
  - Legacy fallback mapping is acceptable as a helper, but linear directional operator UI must not present it as a proven TP/SL contract without `directional_exit_levels`.
- **Financial/trading risk:**
  - The bug did not create real orders because this repo has no live OMS/EMS and execution preflight remains backend fail-closed.
  - It could mislead the operator details panel by showing a plausible short TP/SL pair even when the backend failed to provide or could not prove canonical directional exits.
  - The risk is operator situational-awareness and manual execution risk.
- **Fix:**
  - Added `rawBackendExits`, normalized `dirNorm` and `venueNorm` in `buildOperatorValues()`.
  - For `venue=linear` and `direction in {long, short}`, the UI now requires a backend `directional_exit_levels` object before rendering directional TP/SL.
  - If the backend payload is missing, the UI renders:
    - `Take Profit = —`
    - `Stop Loss / Kill-switch = lower / upper`
    - `Directional TP blocked`
    - explanatory geometry text: backend directional TP/SL payload missing; rendering kill-switch only.
  - Existing behavior for present valid backend payload remains unchanged.
  - Existing behavior for present invalid backend payload remains fail-closed and still renders kill-switch-only.
- **Safety direction:**
  - This moves the UI in the safer direction only. It does not weaken scoring, risk gates, execution preflight, Bybit metadata guards, side mapping, triggerDirection, `reduceOnly`, `closeOnTrigger`, leverage caps or funding guards.

## Red→green tests

New file: `tests/test_iteration183_ui_requires_backend_exit_payload.py`

1. `test_linear_directional_ui_blocks_local_tp_sl_when_backend_exit_payload_missing`
   - Red on uploaded original archive:
     ```text
     FAILED ... AssertionError: assert '95.0' == '—'
     1 failed, 1 passed in 0.35s
     ```
   - The old UI rendered local short TP `95.0` without a backend `directional_exit_levels` payload.
   - Green after patch: missing backend payload renders `TP=—`, kill-switch-only SL field and `Directional TP blocked`.

2. `test_linear_directional_ui_still_uses_backend_exit_payload_when_present`
   - Guards against overblocking.
   - Confirms valid backend short payload still renders `TP=95.0`, `SL=105.0`, label `Take Profit`.

Updated existing test: `tests/test_iteration148_directional_semantics_hardening.py`

- Changed the static assertion to match the new `rawBackendExits` flow while preserving the requirement that the operator UI consumes `directional_exit_levels` and spreads `canonicalExits` into the displayed operator values.

Targeted post-fix run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q \
  tests/test_iteration148_directional_semantics_hardening.py \
  tests/test_iteration167_full_trading_system_audit.py \
  tests/test_iteration183_ui_requires_backend_exit_payload.py --tb=short
```

Result:

```text
74 passed in 2.42s
```

## Post-change verification

Commands run after patch:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q <suite split by test files> --tb=short
```

Results:

- `python -m compileall -q app tests main.py`: passed, exit `0`.
- `node --check app/ui/static/app.js`: passed, exit `0`.
- Targeted UI/directional suite: `74 passed in 2.42s`.
- Full repository suite, split by test files:
  - chunk 1: `147 passed`
  - chunk 2: `62 passed`
  - chunk 3: `101 passed`
  - chunk 4: `128 passed`
  - chunk 5: `77 passed`
  - chunk 6: `75 passed`
  - chunk 7: `111 passed`
  - total post-change: `701 passed`.

The test-count increase from `699` to `701` is the two added regression tests.

`npm` / `yarn` tests were not run because the project root does not contain `package.json`, `package-lock.json`, `yarn.lock` or `pnpm-lock.yaml`.

## Static scan

Saved to:

- `docs/STATIC_SCAN_2026-06-15_UI_BACKEND_EXIT_PAYLOAD_FAILCLOSED.txt`

Reviewed changed/new TP/SL hits only. No changed hit weakens backend geometry validation, execution preflight, runtime risk caps, Bybit side mapping, `reduceOnly`, `closeOnTrigger`, triggerDirection, funding checks or leverage caps.

## Residual risks relative to `docs/KNOWN_RISKS.md`

Unchanged residual risks:

- No real OMS/EMS in this repository.
- No actual open-order/fill/partial-fill reconciliation.
- No private exchange-side liquidation truth model.
- Proxy outcome labeling remains a model-risk limitation.
- Calibration remains advisory and bounded by proxy labels / regime drift.
- SQLite remains acceptable for single-node operator use but not multi-node production truth.
- External execution layer must still re-check Bybit metadata, live price, available balance, account mode, position state, qty step, min notional, margin and order lifecycle immediately before real execution.

Closed/mitigated in this patch:

- UI no longer displays directional TP/SL for linear long/short rows unless backend canonical `directional_exit_levels` exists.
- The operator details panel fails closed to kill-switch-only presentation when backend directional exit proof is absent.

## Files changed

- `app/ui/static/app.js`
- `tests/test_iteration148_directional_semantics_hardening.py`
- `tests/test_iteration183_ui_requires_backend_exit_payload.py`
- `docs/AUDIT_REPORT_2026-06-15_UI_BACKEND_EXIT_PAYLOAD_FAILCLOSED.md`
- `docs/STATIC_SCAN_2026-06-15_UI_BACKEND_EXIT_PAYLOAD_FAILCLOSED.txt`
