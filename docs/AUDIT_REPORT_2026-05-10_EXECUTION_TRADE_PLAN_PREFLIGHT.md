# Audit report — execution trade_plan preflight fail-closed

Date: 2026-05-10
Scope: Bybit Linear USDT Futures / USDT Perpetual arithmetic grid recommendations only.

## Summary

The project already had strong product-scope restrictions: `futures_grid` only, `venue=linear`, USDT symbols, Bybit instrument metadata checks, decimal-based linear PnL helpers, maker/taker/funding economics, live-price/range guards, and operator UI fields for recommendation status, risk and rejection reasons.

The remaining critical gap found in this pass was execution-time validation of incomplete legacy/manual recommendation payloads. The UI/operator guard exposed missing `trade_plan` as a warning, but execution preflight reused the same validation mode. As a result, a row that had `params.grid_levels` but lacked the full executable grid geometry could reach later lifecycle paths without proving reference price, grid range, kill-switch and grid-step consistency.

## Critical finding

| Area | Error | Risk | Fix | Files |
|---|---|---|---|---|
| Execution preflight | Missing or incomplete `params.trade_plan` was not a hard execution error. | A legacy/manual recommendation could be materialized without proving the actual Bybit Linear USDT futures grid geometry, live-price range, kill-switch and grid-step safety. | Added explicit `require_execution_plan=True` mode. Mutating execution checks now fail closed when `trade_plan`, `reference_price`, range, kill-switch or `grid_step.step_abs` is absent/non-finite. | `app/main.py` |
| API test fixtures | Lifecycle/rollback tests used incomplete historical params while asserting execution success. | Tests could keep a false contract where execution is allowed without an executable plan. | Added safe Bybit Linear grid fixture payloads with full `trade_plan`, tick-aligned levels, min-notional-safe sizing and risk economics. | `tests/conftest.py`, `tests/test_api.py`, `tests/test_iteration68.py`, `tests/test_iteration92_json_shape_hardening.py`, `tests/test_iteration96_runtime_and_payload_hardening.py`, `tests/test_iteration101_resilience_hardening.py` |
| Regression coverage | No direct test proved that execution mode blocks absent/incomplete `trade_plan`. | Future refactors could restore fail-open behavior. | Added strict preflight tests for missing plan and incomplete grid geometry. | `tests/test_iteration117_grid_only_strict_preflight.py` |

## Implementation notes

- UI/list/detail validation remains non-destructive for malformed historical JSON and missing payloads, so operator pages can still display audit rows.
- Execution validation is now stricter than display validation. It requires both current Bybit metadata and a complete execution plan.
- The required executable plan fields are:
  - `params.trade_plan.reference_price`
  - `params.trade_plan.levels.range.lower`
  - `params.trade_plan.levels.range.upper`
  - `params.trade_plan.levels.kill_switch.lower`
  - `params.trade_plan.levels.kill_switch.upper`
  - `params.trade_plan.levels.grid_step.step_abs`
- Existing Bybit checks continue to enforce `LinearPerpetual`, `quoteCoin=USDT`, `settleCoin=USDT`, `status=Trading`, tick size, quantity step, min/max quantity, min notional and leverage filters.

## Test result

Command:

```bash
python -m pytest -q
```

Result:

```text
401 passed in 8.61s
```

## Residual risks

- Live execution still requires validation against the actual account fee tier, risk tier and balance/margin state.
- Funding impact is only as accurate as current and historical funding inputs.
- Slippage and partial-fill behavior still need paper/live execution validation.
- Bybit instrument filters can change; runtime metadata checks remain mandatory.
