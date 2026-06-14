# Audit report: Bybit futures grid trading semantics and runtime risk re-audit

Date: 2026-06-14  
Scope: Bybit V5 linear USDT futures/grid recommendation system; backend trading semantics, execution preflight, runtime risk caps, grid sizing economics, UI-facing payload consistency, regression tests.

## Executive summary

The project already contained a substantial directional model for long/short TP/SL, Bybit order side semantics, protective order planning, execution preflight, and UI safety guards. Baseline checks on the received archive passed before modification:

- `python3 -m compileall -q app main.py`: passed
- `pytest -q`: 654 passed
- `node --check app/ui/static/app.js`: passed

A deeper semantic review found one high-severity trading-risk issue in grid notional/margin accounting. For fixed-qty futures grids, parts of the recommender/runtime pipeline used `qty * reference_price * grid_count` as the total grid notional. That is not conservative when the executable grid range includes prices above the reference price. The corrected model now uses the highest positive executable grid price across `lower/reference/upper` for worst-case notional and margin caps while preserving the lowest positive grid price for min-notional validation.

After the fix and new regression tests:

- `python3 -m compileall -q app main.py`: passed
- `pytest -q`: 658 passed
- `node --check app/ui/static/app.js`: passed
- npm/yarn tests: not applicable; no `package.json` exists in the project root.

## Project map reviewed

Key areas inspected:

- `app/trading_semantics.py`: canonical directional TP/SL, long/short PnL and Bybit side/protective-order semantics.
- `app/main.py`: execution preflight, Bybit metadata snapping, risk cap checks, operator execution safety gates, route-level payload handling.
- `app/recommender.py`: grid construction, sizing/economics payloads, leverage/margin estimates, risk-gated recommendation generation.
- `app/outcomes.py`: outcome labelling and grid result scoring.
- `app/ui/static/app.js`: UI formatting and backend-driven directional TP/SL display.
- `tests/test_iteration127_tick_safe_grid_snapping.py`, `tests/test_iteration154_execution_runtime_risk_caps.py`, `tests/test_iteration155_deep_directional_risk_patch.py`, `tests/test_iteration158_deep_bybit_directional_audit.py`, `tests/test_iteration167_full_trading_system_audit.py`, `tests/test_iteration168_execution_direction_conflict_guard.py`: existing semantic and regression coverage.

Static keyword scan summary used during review:

| Term | Hits |
|---|---:|
| tp | 1219 |
| sl | 1044 |
| stop | 398 |
| take | 207 |
| upper | 1163 |
| lower | 1486 |
| short | 1594 |
| long | 1829 |
| side | 1016 |
| Buy | 463 |
| Sell | 437 |
| reduceOnly | 532 |
| kill | 517 |
| leverage | 974 |
| pnl | 663 |
| roi | 163 |
| risk | 1434 |
| notional | 263 |
| margin | 337 |

## Findings and fixes

### HIGH: Grid notional and margin could be understated by reference-price accounting

- Severity: high
- Files:
  - `app/recommender.py`, grid economics construction around lines 2287-2391
  - `app/main.py`, Bybit auto-snap/economics normalization around lines 1719-1748
  - `app/main.py`, runtime risk cap re-check around lines 2078-2184
  - `app/main.py`, worst-case notional helper around lines 1802-1817
- Problem:
  - The system had several payloads where total notional and margin were derived from `order_qty * reference_price * grid_count`.
  - In a fixed-qty linear futures grid, orders near the upper boundary have larger notional than orders at reference price.
  - Example: `qty=1`, `reference=100`, `upper=150`, `grid_count=10`. Reference-based estimate is `1000 USDT`, but worst executable grid notional is `1500 USDT`.
- Trading/financial risk:
  - `max_position_notional_usdt` and `max_margin_per_bot_usdt` could appear satisfied at recommendation time or execution time while the grid actually reserved/created more exposure at upper grid levels.
  - This is especially dangerous in volatile regimes, squeeze/short-squeeze moves, and after runtime risk limits are tightened between publication and operator execution.
- Fix:
  - Added `_grid_max_notional_price(reference_price, lower, upper)` in `app/main.py`.
  - Kept `_grid_min_notional_price(...)` for min-notional checks because min-notional must be conservative at the lowest positive executable price.
  - Added worst-case fields to recommender and snapped execution payloads:
    - `estimated_worst_case_order_notional_usdt`
    - `estimated_worst_case_total_order_notional_usdt`
    - `estimated_worst_case_margin_required_usdt`
  - Runtime risk checks now prefer `estimated_worst_case_total_order_notional_usdt` and `estimated_worst_case_margin_required_usdt` over legacy reference-price estimates.
  - Runtime risk checks also derive `order_qty * max(range/reference price) * grid_count` if a legacy payload lacks explicit worst-case fields.
  - If a legacy estimate is materially understated and the derived worst-case notional breaches the current cap, execution emits `POSITION_NOTIONAL_UNDERSTATED_BY_GRID_PRICE` together with the relevant cap block.
- Tests added:
  - `tests/test_iteration169_grid_worst_case_notional.py::test_runtime_caps_use_worst_executable_grid_price_not_reference_price`
  - `tests/test_iteration169_grid_worst_case_notional.py::test_understated_legacy_estimate_does_not_block_when_worst_case_is_within_cap`
  - `tests/test_iteration169_grid_worst_case_notional.py::test_worst_case_total_notional_field_takes_precedence_over_legacy_reference_estimate`
  - `tests/test_iteration169_grid_worst_case_notional.py::test_auto_snap_publishes_worst_case_grid_notional_and_margin`

### LOW: Explicit exposure fields were missing from operator-facing snapped payloads

- Severity: low, because execution could still infer from existing fields in many generated payloads, but the operator-facing explanation was incomplete.
- Files:
  - `app/main.py`, snapped payload normalization around lines 1719-1748
  - `app/recommender.py`, sizing/economics payload around lines 2336-2391
- Problem:
  - Existing UI/operator economics exposed `estimated_total_order_notional_usdt` and margin at reference-price semantics, not the worst-case upper-bound grid exposure.
  - This made the displayed capital requirement less conservative than the actual executable grid envelope.
- Fix:
  - Added explicit worst-case notional/margin fields to `params.sizing`, `params.economics`, `trade_plan.sizing`, `trade_plan.economics`, `operator_sheet.economics` where those mappings exist.
  - `operator_sheet.economics.capital_required_usdt` is now bumped to the larger of existing capital requirement and worst-case margin during Bybit auto-snap.
- Tests added:
  - Covered by `test_auto_snap_publishes_worst_case_grid_notional_and_margin`.

## Directional TP/SL and Bybit semantics review notes

No new TP/SL inversion was found in the audited paths. The current codebase already contains explicit direction-aware functions and tests for:

- long TP above entry and SL below entry;
- short TP below entry and SL above entry;
- long profit on price increase and short profit on price decrease;
- Bybit linear Buy/Sell interpretation;
- `reduceOnly`/protective order semantics;
- backend-to-frontend directional exit payload validation;
- UI rejection of invalid backend exit payloads before rendering short TP/SL.

The new patch did not change directional TP/SL semantics. It addresses a separate but related futures-risk issue: exposure/margin accounting for a fixed-qty grid across its full executable price range.

## Added regression test file

`tests/test_iteration169_grid_worst_case_notional.py`

Coverage:

1. Runtime caps block a grid whose worst-case upper-bound notional breaches `max_position_notional_usdt` even when the legacy reference-price estimate appears safe.
2. Legacy understated estimates do not generate an execution block when the derived worst-case notional remains inside the active cap.
3. Explicit worst-case notional/margin fields take precedence over legacy fields.
4. Bybit auto-snap publishes worst-case order notional, total notional, and margin to sizing/economics/operator payloads.

## Verification results

Commands executed from project root:

```bash
python3 -m compileall -q app main.py
pytest -q
node --check app/ui/static/app.js
```

Results:

```text
python compileall: passed
pytest: 658 passed in 18.15s
node --check app/ui/static/app.js: passed
npm/yarn tests: not run; no package.json in project root
```

## Residual risks

- This audit was performed as a bounded source-level and regression-test pass in the current environment. It does not prove live exchange behavior against a real Bybit account.
- Exact liquidation and margin behavior still depends on live Bybit risk tiers, account state, mark price, wallet margin, fee tier, and exchange-side changes.
- The project should still run testnet/live preflight with real Bybit instrument metadata before any production execution.
- Backtest/paper/live semantic equivalence remains covered by existing tests and static review, but should be periodically revalidated after any strategy or UI changes.

## Changed files

- `app/main.py`
- `app/recommender.py`
- `tests/test_iteration169_grid_worst_case_notional.py`
- `docs/AUDIT_REPORT_2026-06-14_WORST_CASE_NOTIONAL_RISK_REAUDIT.md`
