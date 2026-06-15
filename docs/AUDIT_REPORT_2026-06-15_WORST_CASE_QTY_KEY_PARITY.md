# Audit report — 2026-06-15 — worst-case qty key parity

## Scope and starting point

Bounded offline re-audit of the uploaded Bybit Linear USDT futures recommendation/preflight repository. I followed the requested order: reviewed the repository boundary and source-of-truth materials before changing code:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest audit reports dated 2026-06-14 and 2026-06-15.

The existing system boundary remains valid: this repository is a recommendation + operator preflight/fail-closed layer. It is not a live OMS/EMS and does not manage actual Bybit open orders, fills, partial fills or exchange-side reconciliation. Those items remain external execution/reconciliation requirements rather than bugs in nonexistent code.

## Baseline before changes

Commands run from project root before changes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q --tb=short
```

Baseline results:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- `pytest -q --tb=short`: `684 passed in 20.51s`.

## Trading-semantics map reviewed

Single-source directional model and related consumers:

- `app/trading_semantics.py`: canonical long/short/neutral normalization, TP/SL mapping, gross directional PnL, risk/reward, Bybit one-way open/close side mapping and protective TP/SL `reduceOnly` / `closeOnTrigger` semantics.
- `app/main.py`: API/operator payload enrichment, directional exit payload construction, execution preflight and Bybit metadata validation.
- `app/recommender.py`: generated grid economics, sizing, risk caps, leverage and funding economics.
- `app/grid_math.py`: linear-grid PnL, funding cashflow and liquidation-buffer helpers.
- `app/ui/static/app.js`: operator display for side, TP/SL, position size, worst-case notional/margin and risk/reward.
- Existing regression tests: directional TP/SL, Bybit protective order semantics, invalid exit fail-closed behavior, UI short TP/SL rendering, worst-case grid notional and qty handling.

No new long/short TP/SL inversion, Bybit `Buy`/`Sell` inversion, or `reduceOnly` weakening was found.

## Finding and fix

### MEDIUM — backend TP/SL qty math did not treat all UI worst-case/max notional keys as worst-case exposure

- **Files**:
  - `app/main.py`, lines 877-892.
  - `app/ui/static/app.js`, lines 926-941, reviewed as the UI convention being matched.
  - `tests/test_iteration178_worst_case_qty_key_parity.py`, lines 1-64.
- **Problem**:
  - The operator UI already treats four fields as worst-case/max grid exposure: `estimated_worst_case_total_order_notional_usdt`, `worst_case_total_order_notional_usdt`, `estimated_max_position_notional_usdt`, `max_position_notional_usdt`.
  - Backend `_directional_exit_qty_for_reco(...)` used max-grid-price qty derivation only for `estimated_worst_case_total_order_notional_usdt` and `estimated_max_position_notional_usdt`.
  - It ignored `worst_case_total_order_notional_usdt`, and it treated `max_position_notional_usdt` as reference-price notional.
- **Why this is an error**:
  - With `reference_price=100`, `range.upper=150`, and `max_position_notional_usdt=1500`, the correct worst-case base qty is `1500 / 150 = 10`.
  - The old backend path returned `1500 / 100 = 15`, overstating directional TP/SL gross PnL and loss by 50% for that payload shape.
  - For `worst_case_total_order_notional_usdt`, old backend qty could remain unavailable even though the UI can display the same field.
- **Financial/trading risk**:
  - Execution gates remained fail-closed and did not use this bug to place real orders.
  - Operator-facing TP/SL math could still be inconsistent with the UI's own worst-case exposure convention, weakening manual review and backend↔frontend parity.
- **Fix**:
  - Added `worst_case_total_order_notional_usdt` and `max_position_notional_usdt` to the backend worst-case/max notional key set.
  - Removed `max_position_notional_usdt` from the reference-price notional key set.
  - No live execution lifecycle or Bybit side/reduce-only semantics were changed.

## Added tests and red→green evidence

New test file:

- `tests/test_iteration178_worst_case_qty_key_parity.py`
  - `test_directional_exit_qty_treats_all_ui_worst_case_notional_keys_as_max_grid_price[worst_case_total_order_notional_usdt-sizing]`
  - `test_directional_exit_qty_treats_all_ui_worst_case_notional_keys_as_max_grid_price[max_position_notional_usdt-economics]`

Red run before backend fix:

```text
2 failed in 1.07s
- worst_case_total_order_notional_usdt: payload["qty"] was None, expected 10.0
- max_position_notional_usdt: payload["qty"] was 15.0, expected 10.0
```

Green targeted run after fix:

```text
pytest -q tests/test_iteration178_worst_case_qty_key_parity.py --tb=short
2 passed in 0.96s
```

Related regression run after fix:

```text
pytest -q tests/test_iteration170_directional_qty_worst_case.py \
          tests/test_iteration172_ui_worst_case_margin_display.py \
          tests/test_iteration178_worst_case_qty_key_parity.py --tb=short
7 passed in 1.06s
```

## Final verification

Commands run after fixes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q --tb=short
```

Post-fix results:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- `pytest -q --tb=short`: `686 passed in 18.98s`.

`npm` / `yarn` tests were not run because no `package.json` is present in the project root.

## Static scan

A bounded static scan note was saved to:

- `docs/STATIC_SCAN_2026-06-15_WORST_CASE_QTY_KEY_PARITY.txt`

Reviewed changed/new hits were safe: the patch only aligns backend worst-case qty derivation with the already existing UI worst-case key convention and adds regression tests.

## Residual risks and changes relative to `docs/KNOWN_RISKS.md`

- No residual risk in `docs/KNOWN_RISKS.md` was weakened or removed.
- The repository still has no live OMS/EMS, no actual order/fill reconciliation and no exchange-side liquidation truth model.
- External execution/reconciliation must still re-check Bybit metadata, actual account balance, margin, qty step, min notional, live price, open positions and order lifecycle immediately before real execution.
- Proxy outcome labels and advisory calibration remain residual model risks.
- The patch closes a backend↔frontend parity gap for operator TP/SL PnL display; it does not claim to solve external execution-layer risks.

## Files changed

- `app/main.py`
- `tests/test_iteration178_worst_case_qty_key_parity.py`
- `docs/AUDIT_REPORT_2026-06-15_WORST_CASE_QTY_KEY_PARITY.md`
- `docs/STATIC_SCAN_2026-06-15_WORST_CASE_QTY_KEY_PARITY.txt`
