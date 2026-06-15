# Audit report — 2026-06-15 — operator-sheet directional qty parity

## Scope and starting point

Bounded offline re-audit of the uploaded Bybit Linear USDT futures recommendation / preflight repository. I followed the requested order before changing code:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest audit reports dated 2026-06-15 and 2026-06-14

The repository boundary remains unchanged and important: this project is a recommendation + operator UI + fail-closed preflight layer. It is not a live OMS/EMS and does not manage real Bybit open orders, fills, partial fills, cancellations, private-account reconciliation or exact liquidation truth. Those remain external execution/reconciliation requirements, not bugs in absent code.

## Baseline before changes

Commands run from project root before patching:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q --tb=short
pytest --collect-only -q
```

Baseline results:

- `python -m compileall -q app tests main.py`: PASS
- `node --check app/ui/static/app.js`: PASS
- `pytest --collect-only -q`: 696 tests collected
- `pytest -q --tb=short`: `696 passed in 22.48s`

## Source-of-truth summary

- `docs/KNOWN_RISKS.md`: external OMS/EMS, exchange-side open-order/fill reconciliation, live account balance, private account state and exact liquidation remain out of scope for this repository.
- `docs/TRADING_LOGIC.md`: Linear USDT futures grid recommendations must stay fail-closed at operator/execution preflight and must not silently downgrade blockers to warnings.
- `docs/ARCHITECTURE.md` / `docs/MODULES.md`: `app/main.py` owns API/operator enrichment and execution-preflight checks; `app/recommender.py` generates recommendation economics; `app/risk.py` owns runtime caps; `app/trading_semantics.py` is the canonical long/short/neutral source of truth.
- `app/trading_semantics.py`: long TP is above entry and SL below; short TP is below entry and SL above; neutral grids do not expose a single directional TP. Bybit one-way linear close/protective semantics use the canonical close side with `reduceOnly=true` and `closeOnTrigger=true`.

## Trading-semantics map reviewed

Single-source directional model and related consumers reviewed:

- `app/trading_semantics.py`: canonical direction normalization, TP/SL mapping, gross directional PnL, risk/reward, Bybit one-way open/close side mapping, protective order `triggerDirection`, `reduceOnly` and `closeOnTrigger` semantics.
- `app/main.py`: recommendation UI enrichment, `_directional_exit_payload_for_reco(...)`, `_directional_exit_qty_for_reco(...)`, Bybit metadata validation, runtime risk caps and operator decision context.
- `app/ui/static/app.js`: operator panel display for side, position size, TP/SL labels, TP/SL distances, risk/reward and worst-case exposure fields.
- `app/grid_math.py`: linear PnL, funding cashflow sign, margin and liquidation-buffer estimates.
- `app/recommender.py`: generated grid economics, sizing, funding carry, leverage and risk-report fields.
- Tests around directional semantics, UI short TP/SL, worst-case notional, runtime operator-sheet caps and estimated max qty parity.

No long/short TP/SL inversion, Bybit `Buy`/`Sell` inversion, protective `reduceOnly` weakening, or fail-open change was made.

## Finding and fix

### MEDIUM — backend TP/SL PnL math ignored top-level `operator_sheet` sizing fields used by the UI

- **Files**:
  - `app/main.py`, lines 814-829 after patch.
  - `app/ui/static/app.js`, `buildOperatorFieldSpecs(...)`, reviewed as the UI convention being matched.
  - `tests/test_iteration182_operator_sheet_directional_qty_parity.py`, lines 28-99.
- **Problem**:
  - The operator UI already searches the top-level `operatorSheet` object when deriving displayed position notional / position qty.
  - Backend `_directional_exit_qty_for_reco(...)` searched `operator_sheet.sizing` and `operator_sheet.economics`, but skipped the top-level `operator_sheet` object itself.
- **Why this is an error**:
  - A generated or legacy operator payload may expose `estimated_position_qty`, `max_position_notional_usdt` or equivalent fields directly on `params.operator_sheet`.
  - Before this patch the UI could show full operator-sheet exposure while the backend directional TP/SL payload returned `qty=None` and omitted full-size gross TP/SL PnL context.
  - For a short with `reference_price=100`, grid upper `150`, and top-level `operator_sheet.max_position_notional_usdt=1500`, the correct worst-case qty is `1500 / 150 = 10`. The old backend path returned no qty at all.
- **Financial/trading risk**:
  - Execution guards remained fail-closed and already included top-level `operator_sheet` in runtime risk-cap validation.
  - Operator-facing risk/PnL review could still be internally inconsistent: UI position-size display could imply a full bot exposure while backend TP/SL math did not use that same exposure.
- **Fix**:
  - Added the top-level `operator_sheet` mapping to `_directional_exit_qty_for_reco(...)` `sizing_maps` after canonical/nested generated maps and after `params` / `plan`.
  - This preserves existing precedence and only fills the parity gap for legacy/generated operator-sheet fields.
  - No execution lifecycle, Bybit side mapping, protective order mapping, leverage caps or fail-closed blockers were weakened.

## Added tests and red→green evidence

New test file:

- `tests/test_iteration182_operator_sheet_directional_qty_parity.py`
  - `test_backend_directional_exit_qty_reads_operator_sheet_top_level_like_ui[...]`
    - verifies top-level `operator_sheet.estimated_position_qty` is used as full-position qty.
    - verifies top-level `operator_sheet.max_position_notional_usdt` is treated as a worst-case/max exposure field and divided by max grid price, not reference price.
    - verifies independent expected gross profit/loss values for short TP/SL math.
  - `test_operator_ui_position_notional_lookup_includes_top_level_operator_sheet`
    - verifies the operator UI still includes top-level `operatorSheet` as a notional source and carries the relevant max-position key.

Red run before backend fix:

```text
pytest -q tests/test_iteration182_operator_sheet_directional_qty_parity.py --tb=short
FF. [100%]

FAILED ... operator_sheet.estimated_position_qty: payload["qty"] was None, expected 12.5
FAILED ... operator_sheet.max_position_notional_usdt: payload["qty"] was None, expected 10.0
2 failed, 1 passed in 1.40s
```

Green run after backend fix:

```text
pytest -q tests/test_iteration182_operator_sheet_directional_qty_parity.py --tb=short
3 passed in 1.18s
```

Related regression run after fix:

```text
pytest -q \
  tests/test_iteration178_worst_case_qty_key_parity.py \
  tests/test_iteration179_estimated_max_qty_ui_parity.py \
  tests/test_iteration181_operator_sheet_runtime_size_caps.py \
  tests/test_iteration182_operator_sheet_directional_qty_parity.py \
  --tb=short
10 passed in 1.80s
```

## Final verification

Commands run after the patch:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q --tb=short
```

Post-fix results:

- `python -m compileall -q app tests main.py`: PASS
- `node --check app/ui/static/app.js`: PASS
- `pytest -q --tb=short`: `699 passed in 21.73s`

No `npm`, `yarn`, frontend lint or frontend typecheck was run because there is no `package.json`, `package-lock.json` or `yarn.lock` in the project root.

## Static scan

A bounded scan note was saved to:

- `docs/STATIC_SCAN_2026-06-15_OPERATOR_SHEET_DIRECTIONAL_QTY_PARITY.txt`

Key classification:

- unsafe before patch / safe after patch: `_directional_exit_qty_for_reco(...)` skipped top-level `operator_sheet` although UI used it.
- safe / unchanged: `app/ui/static/app.js` top-level `operatorSheet` lookup convention.
- safe / unchanged: `app/trading_semantics.py` directional TP/SL, PnL, risk/reward and Bybit protective order semantics.
- safe / unchanged: execution-time runtime risk caps already included top-level `operator_sheet`; this patch aligns UI/API display math with that execution guard coverage.

## Checks not performed

- No live Bybit private/testnet order lifecycle checks were performed.
- No real order placement, partial fill handling, order cancellation, open-order reconciliation or private wallet/liquidation verification was performed.
- No npm/yarn frontend toolchain checks were available.
- No external OMS/EMS code was invented or tested.

## Residual risks relative to `docs/KNOWN_RISKS.md`

No residual-risk item was weakened or removed. The following remain unchanged:

- The repository is not a live OMS/EMS.
- Exchange-side order/fill/cancel/reconciliation truth remains external.
- Exact liquidation, available balance, margin tier, fee tier and private-account state remain external execution/reconciliation responsibilities.
- Outcome labeling remains proxy-based.
- Public Bybit REST metadata/ticker remains insufficient as execution truth.
- Any future live executor must bind order side / reduce-only / protective TP/SL semantics to `app.trading_semantics` and validate with private testnet/live checks.

## Files changed

- `app/main.py`
- `tests/test_iteration182_operator_sheet_directional_qty_parity.py`
- `docs/AUDIT_REPORT_2026-06-15_OPERATOR_SHEET_DIRECTIONAL_QTY_PARITY.md`
- `docs/STATIC_SCAN_2026-06-15_OPERATOR_SHEET_DIRECTIONAL_QTY_PARITY.txt`
