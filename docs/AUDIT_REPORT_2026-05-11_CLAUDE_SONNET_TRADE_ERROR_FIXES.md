# Audit report 2026-05-11 — Claude Sonnet trade error fixes

## Scope

Re-audit focused on execution-time safety for Bybit Linear USDT Futures grid recommendations. The project already contained the product-scope guards for `venue=linear`, `bot_type=futures_grid`, USDT settlement, isolated margin, tick/qty/min-notional filters, LLM pending guards and live-price preflight. This iteration hardened the remaining gap: a generated or imported recommendation could reach the execution preflight with internally inconsistent economics/sizing fields.

## Critical issues fixed

| Area | Error | Risk | Fix | Files |
|---|---|---|---|---|
| Execution economics | Preflight did not reject explicitly negative/zero `economics.net_profit_bps` when a payload supplied economics | Grid could be launched even though its own model says fees/spread/slippage/funding erase the edge | Added fail-closed net-profit gates for execution-time validation | `app/main.py` |
| Execution economics | Gross edge could barely cover execution costs | A visually profitable grid can become loss-making after one tick of spread/slippage | Added gross-vs-cost coverage gate with a conservative 1.10x multiplier | `app/main.py` |
| Funding carry | Extreme funding cost in economics was not independently blocked at execution-time validation | Carry can dominate small grid profit | Added `GRID_FUNDING_COST_EXTREME` execution guard | `app/main.py` |
| Margin math | `estimated_margin_required_usdt` was not cross-checked against `estimated_total_order_notional_usdt / leverage` | UI/API could understate margin required | Added notional/leverage/margin consistency check | `app/main.py` |
| Position notional | `estimated_total_order_notional_usdt` was not cross-checked against `order_notional * grid_count` | Exposure and risk caps could be calculated for the wrong number of grid orders | Added total-notional/grid-count consistency check | `app/main.py` |
| Grid sizing | `estimated_active_orders` was not cross-checked against Bybit `grid_count` | Margin and active-order estimates could describe a different grid than the operator creates | Added active-orders/grid-count mismatch guard | `app/main.py` |
| Legacy/generated payload shape | Sizing extraction preferred `trade_plan.sizing` and could miss generated `params.sizing` / `params.economics` when plan sizing was absent | False `SIZE_INPUT_REQUIRED` and incomplete min-notional checks for generated payload variants | Added multi-source sizing extraction from plan, params, economics | `app/main.py` |
| Regression coverage | No tests proved execution-time economics fail-closed behavior | Future edits could reintroduce gross-profit-only launchability | Added focused iteration 145 tests | `tests/test_iteration145_execution_economics_fail_closed.py` |
| Backward compatibility | Strict economics requirements could break old lifecycle tests that intentionally use legacy fixtures | Existing manual/legacy tests would fail before reaching the behavior under test | Missing economics is now a warning for legacy payloads; supplied bad economics is a hard error | `app/main.py` |
| Documentation | The current audit trail did not describe this execution-economics hardening layer | Operators could miss which fields are trusted at launch time | Added this audit report | `docs/AUDIT_REPORT_2026-05-11_CLAUDE_SONNET_TRADE_ERROR_FIXES.md` |

## Trading logic changes

- Launch-time validation now refuses supplied `economics.net_profit_bps <= 0`.
- Launch-time validation refuses supplied `economics.net_profit_bps < 2 bps` as too thin for live grid execution.
- Launch-time validation refuses gross edge that is not at least 1.10x execution cost.
- Launch-time validation refuses extreme expected funding cost (`>= 6 bps`) when present in economics.
- Margin/exposure consistency is now checked against leverage and grid count.

## Backend/API changes

- Added execution validation constants:
  - `EXECUTION_MIN_NET_PROFIT_BPS = 2.0`
  - `EXECUTION_GROSS_COST_COVERAGE_MULTIPLIER = 1.10`
- Extended `_validate_trade_plan_against_bybit_meta()` without changing public API schema.
- Preserved legacy behavior: missing economics produces a warning, but explicitly bad economics blocks execution.

## Tests

Added `tests/test_iteration145_execution_economics_fail_closed.py`:

- blocks non-positive net grid edge;
- blocks gross edge that barely covers execution costs;
- blocks notional/margin/leverage mismatch;
- verifies generated `params.sizing` is used when `trade_plan.sizing` is absent.

Result:

```text
473 passed in 18.68s
```

Command used:

```bash
pytest -q
```

## Residual risks

- Exact Bybit liquidation still depends on risk tier, mark price, wallet balance and exchange-side engine.
- Live maker/taker fee tier must be confirmed against the actual account.
- Slippage and partial fills require paper/live execution telemetry.
- Funding history and funding interval should be refreshed from Bybit instrument/ticker metadata before production launch.
