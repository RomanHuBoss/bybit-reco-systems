# Audit report: nested grid-count TP/SL PnL context hardening

Date: 2026-06-14  
Scope: Bybit futures / linear USDT recommendation layer; canonical directional TP/SL payload, operator UI/API PnL context, grid sizing metadata, regression tests.

## Baseline before changes

Commands executed from project root before any code changes:

```text
python -m compileall -q app tests main.py: passed
node --check app/ui/static/app.js: passed
pytest -q: 660 passed in 28.80s
```

The project remains a recommender + fail-closed preflight/operator layer, not a live OMS/EMS. No live exchange execution, fills, partial-fill reconciliation, account-balance or real order lifecycle code was invented or tested.

## Documents and canonical model reviewed

Read/reviewed before patching:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest 2026-06-14 audit reports, including `AUDIT_REPORT_2026-06-14_DIRECTIONAL_QTY_WORST_PRICE.md`, `AUDIT_REPORT_2026-06-14_WORST_CASE_NOTIONAL_RISK_REAUDIT.md`, `AUDIT_REPORT_2026-06-14_INDEPENDENT_FULL_REAUDIT.md`, `AUDIT_REPORT_2026-06-14_OPERATOR_PAYLOAD_CONSISTENCY_PATCH.md` and adjacent same-day reports.

Canonical semantics remained unchanged:

- Long: TP above reference, SL below reference.
- Short: TP below reference, SL above reference.
- Neutral/grid: no single directional TP.
- Bybit Linear one-way: open long `Buy`, close/protect long `Sell`; open short `Sell`, close/protect short `Buy`; protective exits are reduce-only and close-on-trigger.

A spot-check against current official Bybit V5 documentation confirmed the relevant assumptions: perps/futures order `qty` is contract quantity, `positionIdx=0` is one-way mode, `triggerDirection=1` means rise and `2` means fall, and close/reduce orders should use `reduceOnly=true`; `closeOnTrigger` is a closing-order safety flag for linear/inverse.

## Static scan summary

A bounded static scan was recorded in:

- `docs/STATIC_SCAN_2026-06-14_NESTED_GRID_QTY_PNL.txt`

Changed-file hot spots were limited to:

- `app/main.py`: `_directional_exit_qty_for_reco(...)`
- `tests/test_iteration171_nested_grid_count.py`: new regression coverage

No new bypass of `app/trading_semantics.py` was added. The patch only improves quantity context used when `_directional_exit_payload_for_reco(...)` calls canonical `directional_trade_math(...)`.

## Finding and fix

### MEDIUM: Nested grid order count was ignored for directional TP/SL PnL display

- **Severity:** medium
- **Files:**
  - `app/main.py:801-811`
  - `app/main.py:849-868`
  - `tests/test_iteration171_nested_grid_count.py:23-79`
- **Problem:** `_directional_exit_qty_for_reco(...)` correctly preferred explicit `qty_per_order`, but multiplied it only by top-level `params.grid_count`, top-level `trade_plan.grid_count`, or top-level `params.grid_levels`. Generated/current payloads can place `grid_count` or `estimated_active_orders` inside nested `trade_plan.sizing`, `trade_plan.economics`, `params.sizing`, `params.economics`, or operator-sheet mappings. When that nested count existed but no top-level count was present, the API/UI directional-exit payload used one grid order as total quantity.
- **Example:** short grid, reference `100`, kill-switch lower/upper `70/160`, `qty_per_order=1`, nested `trade_plan.economics.grid_count=10`. Before the fix, displayed math used `qty=1`: gross TP `30 USDT`, gross SL `60 USDT`. Correct full-grid context is `qty=10`: gross TP `300 USDT`, gross SL `600 USDT`.
- **Trading/operator risk:** not an order-placement bug and not a TP/SL inversion, but an operator-facing understatement of the full-position PnL/risk magnitude. It could make directional exit consequences appear smaller than the actual grid exposure carried by the recommendation payload.
- **Fix:** added `find_first_positive_int(...)` to scan the same sizing/economics/operator mappings already used for `qty_per_order`; accepted count keys now include `grid_count`, `estimated_active_orders`, `active_grid_intervals`, `grid_levels`, `levels_count`, and `orders_count`. When a nested count is found, `qty_per_order` is multiplied by that count and `qty_source` records the exact source, e.g. `qty_per_order*estimated_active_orders`.
- **Safety direction:** fail-closed/conservative. The change increases displayed/derived TP/SL exposure context when nested grid counts are available; it does not reduce risk guards or relax execution preflight.

## Red → green tests added

New file:

- `tests/test_iteration171_nested_grid_count.py`

Tests:

1. `test_directional_exit_qty_uses_nested_trade_plan_economics_grid_count`
   - Red before fix: `payload["qty"] == 1.0` instead of `10.0`.
   - Green after fix: nested `trade_plan.economics.grid_count=10` produces `qty=10.0`, gross TP `300.0`, gross SL `600.0`.
2. `test_directional_exit_qty_uses_nested_sizing_estimated_active_orders`
   - Red before fix: `payload["qty"] == 0.25` instead of `2.0`.
   - Green after fix: nested `trade_plan.sizing.estimated_active_orders=8` produces `qty=2.0`, gross TP `80.0`, gross SL `60.0`.

Red run before patch:

```text
pytest -q tests/test_iteration171_nested_grid_count.py
2 failed
```

Green targeted run after patch:

```text
pytest -q tests/test_iteration171_nested_grid_count.py tests/test_iteration170_directional_qty_worst_case.py tests/test_iteration161_protective_reference_and_qty.py
6 passed in 2.07s
```

## Post-change verification

Commands executed from project root after changes:

```text
python -m compileall -q app tests main.py: passed
node --check app/ui/static/app.js: passed
pytest -q -p no:ddtrace: 662 passed in 26.29s
```

Additional chunked verification was run because the harness intermittently timed out while waiting for the shell/tool wrapper even after the pytest output file contained a complete green summary. The collected 662 tests were split and all chunks passed:

```text
chunk1: 220 passed
chunk2 split: 220 passed
chunk3: 220 passed
chunk4: 2 passed
```

No npm/yarn tests were run because there is no `package.json` in the project root.

## Baseline vs post counts

| Stage | Result |
|---|---:|
| Baseline pytest | 660 passed |
| Post pytest | 662 passed |
| New tests | +2 passed |
| Failed/skipped after patch | 0 failed / 0 skipped |

## Residual risks

- Exact live Bybit execution behavior is still out of scope for this repository and remains an external execution/reconciliation-layer responsibility.
- The fix corrects operator-facing gross PnL context. It does not convert proxy outcomes into real fill/funding/liquidation truth.
- Exact liquidation, wallet-margin and risk-tier behavior remain approximate and must be validated by live/testnet execution preflight plus external OMS/EMS logic before production use.
- Static scan remains bounded; it did not re-audit every historical documentation hit manually, only changed/high-risk semantic paths and adjacent recent reports.

## Changed files

- `app/main.py`
- `tests/test_iteration171_nested_grid_count.py`
- `docs/AUDIT_REPORT_2026-06-14_NESTED_GRID_QTY_PNL.md`
- `docs/STATIC_SCAN_2026-06-14_NESTED_GRID_QTY_PNL.txt`
