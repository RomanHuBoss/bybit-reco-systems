# Audit report — 2026-06-15 — UI backend direction parity

## Scope and starting point

Bounded offline re-audit of the uploaded Bybit Linear USDT futures recommendation/preflight repository. I followed the required starting order before changing code:

- reviewed `docs/KNOWN_RISKS.md`;
- reviewed `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`;
- reviewed canonical `app/trading_semantics.py`;
- reviewed the latest 2026-06-14 / 2026-06-15 audit reports, including UI/backend exit payload fail-closed, operator sheet parity, runtime risk caps, worst-case qty and leverage synchronization reports.

The repository boundary remains unchanged: this is a recommendation + operator preflight/fail-closed layer, not a live OMS/EMS. Findings about partial fills, real open orders, rate limits, live order retries and exchange-side reconciliation remain external execution-layer requirements unless such a layer is later added.

## Baseline before changes

Commands run from project root before changes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
pytest --collect-only -q
```

Observed baseline:

- `python -m compileall -q app tests main.py`: passed, exit `0`.
- `node --check app/ui/static/app.js`: passed, exit `0`.
- `pytest -q`: no failure observed before the local command timeout; the run progressed through the existing suite but did not produce a final summary in the tool window.
- `pytest --collect-only -q`: `701 tests collected`.

Red proof for the new regression test was obtained by extracting the uploaded original archive, adding only `tests/test_iteration184_ui_backend_direction_mismatch.py`, and running:

```bash
pytest -q tests/test_iteration184_ui_backend_direction_mismatch.py --tb=short
```

Baseline result on uploaded original:

```text
1 failed in 0.45s
AssertionError: assert '105.0' == '—'
```

This demonstrates the old UI rendered a long-style backend TP (`105.0`) on an item whose top-level direction was `short`.

## Trading semantics map reviewed

Single-source directional model and relevant consumers:

- `app/trading_semantics.py`: canonical long/short/neutral normalization, TP/SL mapping, gross directional PnL, risk/reward, Bybit one-way open/close side mapping, protective TP/SL `triggerDirection`, `reduceOnly` and `closeOnTrigger` semantics.
- `app/main.py`: API/operator payload enrichment through `_directional_exit_payload_for_reco(...)`, execution preflight, Bybit metadata validation, snapped operator payloads and runtime risk guards.
- `app/recommender.py`: recommendation economics, grid sizing, leverage and funding economics.
- `app/grid_math.py`: grid PnL, funding cashflow, grid geometry and liquidation-buffer helper semantics.
- `app/ui/static/app.js`: operator fields, directional TP/SL rendering, backend exit payload consumption, risk/reward display and kill-switch fallback.
- Existing tests: `test_iteration148`, `test_iteration156`, `test_iteration167`, `test_iteration183` and related directional/risk/Bybit preflight regressions.

No new backend long/short TP/SL inversion, Bybit `Buy`/`Sell` inversion, `triggerDirection` inversion, or `reduceOnly` weakening was found. The issue found was a frontend fail-closed parity gap when a present backend payload itself disagreed with the item direction.

## Finding and fix

### HIGH — UI did not fail-closed on `item.direction` ↔ `directional_exit_levels.direction` mismatch

- **Files:**
  - `app/ui/static/app.js`, lines 610-691.
  - `tests/test_iteration184_ui_backend_direction_mismatch.py`, lines 53-95.
  - `tests/test_iteration148_directional_semantics_hardening.py`, lines 187-194.
- **Problem:**
  - The previous UI already required a backend `directional_exit_levels` object for `venue=linear` and top-level `direction in {long, short}`.
  - However, once the payload was present, `operatorExitLevelsFromBackend(...)` trusted `directional_exit_levels.direction` without comparing it to the top-level item `direction`.
  - A stale/corrupted/manual payload could therefore render long TP/SL geometry on a short card, or the reverse, even though the top-level recommendation direction was different.
- **Why this is an error:**
  - Canonical directional semantics require one consistent source of truth for side, TP, SL and risk/reward.
  - A top-level `short` recommendation with a backend exit payload declaring `long` is not a safe state. It must not render a directional TP/SL as if it were valid.
- **Financial/trading risk:**
  - The repository is not a live OMS/EMS, so this did not directly place exchange orders.
  - It could still mislead an operator during manual review by displaying the wrong side's TP/SL levels and risk/reward context. For a short, this could show a higher-price TP and lower-price SL, exactly the dangerous inversion class the canonical model is designed to prevent.
- **Fix:**
  - Extended `operatorExitLevelsFromBackend(...)` with `expectedDirection`.
  - `buildOperatorValues(...)` now passes normalized top-level `dirNorm` into the backend exit renderer.
  - If `expectedDirection` is `long` or `short` and the backend payload direction differs, the UI renders fail-closed:
    - `Take Profit = —`
    - `Stop Loss / Kill-switch = lower / upper`
    - label `Directional TP blocked`
    - geometry explanation identifying the mismatch.
- **Safety direction:**
  - This only blocks unsafe/mismatched display states.
  - It does not weaken scoring, risk gates, execution preflight, Bybit metadata validation, leverage caps, funding checks, `reduceOnly`, `closeOnTrigger`, or canonical backend trade semantics.

## Red→green tests

New test file:

- `tests/test_iteration184_ui_backend_direction_mismatch.py`
  - `test_linear_directional_ui_blocks_backend_exit_payload_direction_mismatch`
  - Scenario: top-level item `direction='short'`, but `directional_exit_levels.direction='long'` with long-valid TP/SL (`105/95`).
  - Red on uploaded original: UI returned `TP='105.0'`.
  - Green after patch: UI returns `TP='—'`, `SL='95.0 / 105.0'`, `Directional TP blocked`, with `direction mismatch`, `item=short`, `payload=long` in the geometry text.

Updated existing test:

- `tests/test_iteration148_directional_semantics_hardening.py`
  - Updated static assertions to verify the new frontend flow passes `dirNorm` into `operatorExitLevelsFromBackend(...)`.

Targeted post-fix run:

```bash
pytest -q \
  tests/test_iteration183_ui_requires_backend_exit_payload.py \
  tests/test_iteration184_ui_backend_direction_mismatch.py \
  tests/test_iteration148_directional_semantics_hardening.py \
  tests/test_iteration167_full_trading_system_audit.py::test_js_backend_short_exit_levels_render_correctly \
  tests/test_iteration167_full_trading_system_audit.py::test_js_invalid_backend_geometry_falls_back \
  --tb=short
```

Result:

```text
14 passed in 2.44s
```

Direct new-test post-fix run:

```text
1 passed in 0.29s
```

## Post-change verification

Commands run after the patch:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest --collect-only -q
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q --tb=short <split-by-file/chunk suite>
```

Results:

- `python -m compileall -q app tests main.py`: passed, exit `0`.
- `node --check app/ui/static/app.js`: passed, exit `0`.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest --collect-only -q`: `702 tests collected`.
- Split post-change suite aggregate: `702 passed`.

Split/chunk evidence recorded during verification:

- chunk 1: `97 passed`
- chunk 2: `86 passed`
- chunk 3: `58 passed`
- chunk 4, split by file: `70 passed`
- chunk 5: `51 passed`
- chunk 6: `206 passed`
- chunk 7, split by file: `91 passed`
- chunk 8: `43 passed`

A full unsplit `pytest -q` run again progressed deeply into the suite but did not produce a final summary before the local command timeout, so the final pass count above is based on split execution. No split test file failed.

`npm`/`yarn` tests were not run because the repository root has no `package.json`.

## Static scan

Static scan note saved to:

- `docs/STATIC_SCAN_2026-06-15_UI_BACKEND_DIRECTION_PARITY.txt`

Changed hits were reviewed as safe: they either add the direction-mismatch fail-closed branch, pass top-level direction into the renderer, or assert the new regression behavior.

## Residual risks and changes relative to `docs/KNOWN_RISKS.md`

- No known risk was weakened or deleted.
- The repository still has no live OMS/EMS, exchange-side fill/order lifecycle model, partial-fill reconciliation, account-balance truth, or exact liquidation truth model.
- External execution/reconciliation must still re-check Bybit metadata, qty step, min notional, live price, account mode, available balance, open positions and open orders immediately before real execution.
- Proxy outcome labels and calibration remain advisory model risks.
- This patch closes one UI/backend parity gap for operator-facing TP/SL rendering; it does not claim to solve external execution-layer risks.

## Files changed

- `app/ui/static/app.js`
- `tests/test_iteration148_directional_semantics_hardening.py`
- `tests/test_iteration184_ui_backend_direction_mismatch.py`
- `docs/AUDIT_REPORT_2026-06-15_UI_BACKEND_DIRECTION_PARITY.md`
- `docs/STATIC_SCAN_2026-06-15_UI_BACKEND_DIRECTION_PARITY.txt`
