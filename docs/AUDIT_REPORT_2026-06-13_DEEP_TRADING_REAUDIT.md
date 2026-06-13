# Deep trading semantics audit — Bybit linear USDT futures

Date: 2026-06-13  
Scope: long/short directional semantics, TP/SL mapping, Bybit V5 protective order intent, risk display consistency, UI/API drift, regression tests.

## Executive summary

The project already had a strong centralized direction model in `app/trading_semantics.py` and a broad regression suite. The most material remaining gap was not a direct inversion of short TP/SL in the canonical code, but an API/UI observability gap: the recommendation payload exposed validated prices, while the concrete Bybit protective-order intent (`side`, `triggerDirection`, `reduceOnly`, `closeOnTrigger`, `triggerPrice`) and directional TP/SL risk/reward math were not exported in the same canonical payload. That gap could let a future execution adapter, card, detail view, log, notification, or audit view reconstruct TP/SL independently and drift from backend validation.

The patch keeps architecture intact and hardens the single source of truth:

- added canonical protective trigger geometry validation;
- added executable Bybit linear protective-order plan payloads for TP and SL;
- added fail-closed suppression of protective-order payloads if TP/SL geometry is invalid;
- added backend directional trade math to the recommendation payload;
- surfaced TP/SL distances and risk/reward in the operator UI;
- added regression tests covering short TP/SL, Bybit side/triggerDirection, invalid geometry, and UI exposure of backend math.

## Files changed

| File | Area | Change |
|---|---|---|
| `app/trading_semantics.py` | Backend canonical trading semantics | Added `_normalize_exit_kind`, `_protective_trigger_direction`, `validate_protective_trigger_geometry`, and `bybit_linear_protective_order_plan`. Existing `bybit_linear_protective_order_semantics` now reuses the same normalization/trigger helper. |
| `app/main.py` | API/recommendation payload | `_directional_exit_payload_for_reco` now includes `trade_math`, `take_profit_distance_pct`, `stop_loss_distance_pct`, `risk_reward`, and `bybit_protective_orders` only when directional geometry is valid. |
| `app/ui/static/app.js` | Operator UI | Operator detail fields now show canonical TP/SL distance and `Risk/Reward TP/SL` from backend `directional_exit_levels.trade_math`. |
| `tests/test_iteration158_deep_bybit_directional_audit.py` | Tests | Added regression tests for short protective-order intent, invalid geometry fail-closed behavior, trigger-direction geometry, and UI exposure of backend math. |
| `docs/AUDIT_REPORT_2026-06-13_DEEP_TRADING_REAUDIT.md` | Documentation | This audit report. |

## Findings and fixes

### 1. API/UI drift risk for Bybit protective TP/SL intent

- Severity: **high**
- Files: `app/trading_semantics.py`, `app/main.py`
- Finding: the code had correct centralized TP/SL mapping, but the recommendation payload did not expose the concrete protective-order intent that an execution adapter or UI/audit surface would need: Bybit close side, `triggerDirection`, `reduceOnly`, `closeOnTrigger`, `triggerPrice`, and geometry status.
- Risk: a future UI/detail view/manual control/execution layer could rebuild these fields independently and accidentally invert short TP/SL or create a protective order that visually appears correct but triggers in the wrong direction.
- Fix: added `bybit_linear_protective_order_plan(...)` and `validate_protective_trigger_geometry(...)`; `_directional_exit_payload_for_reco(...)` now publishes TP and SL protective-order plans only after canonical geometry passes.
- Tests: `test_short_directional_exit_payload_exposes_executable_protective_order_intent`, `test_protective_order_plan_geometry_and_trigger_direction_are_canonical`.

### 2. Invalid directional geometry could still leave ambiguous downstream intent

- Severity: **high**
- Files: `app/main.py`, `app/trading_semantics.py`
- Finding: invalid short geometry was flagged, but downstream consumers still had to infer whether an execution payload should be absent or present.
- Risk: if an invalid TP/SL payload is propagated and a future adapter ignores `geometry_valid`, a wrong protective order can be submitted.
- Fix: on invalid geometry, `trade_math` is `None` and `bybit_protective_orders` is `{}`. This makes invalid direction geometry fail closed at payload level.
- Tests: `test_invalid_directional_geometry_does_not_publish_protective_bybit_orders`, `test_protective_trigger_geometry_fails_closed_on_short_take_profit_above_reference`.

### 3. Operator UI did not show canonical directional TP/SL risk/reward

- Severity: **medium**
- Files: `app/ui/static/app.js`, `app/main.py`
- Finding: the UI showed TP/SL levels and generic range distances, but not the backend-derived directional TP/SL distance or risk/reward ratio.
- Risk: an operator could see a price pair but not immediately see whether short TP is being treated as downward profit and short SL as upward loss. This weakens review and makes hidden sign drift harder to detect.
- Fix: UI now displays `TP/SL дистанция` and `Risk/Reward TP/SL` from backend `directional_exit_levels.trade_math`.
- Tests: `test_operator_ui_surfaces_backend_directional_risk_reward_and_distances`.

### 4. Regression test gap around concrete Bybit trigger geometry

- Severity: **medium**
- Files: `tests/test_iteration158_deep_bybit_directional_audit.py`
- Finding: existing tests covered many semantics, but not an end-to-end payload assertion that short TP below entry becomes `side=Buy`, `triggerDirection=2`, and short SL above entry becomes `side=Buy`, `triggerDirection=1` in the API payload itself.
- Risk: a later refactor could keep price labels correct while breaking actual protective trigger semantics.
- Fix: added dedicated regression tests that pin the API-level protective-order shape and invalid-geometry suppression.

## Directional semantics after patch

| Direction | Profit movement | TP | SL | Close/protective side | TP triggerDirection | SL triggerDirection |
|---|---:|---:|---:|---|---:|---:|
| `long` | price rises | above reference | below reference | `Sell` | `1` upward | `2` downward |
| `short` | price falls | below reference | above reference | `Buy` | `2` downward | `1` upward |
| `neutral` | non-directional | no directional TP | lower/upper kill-switch | not emitted by directional helper | n/a | n/a |

Invalid or missing positive finite reference/trigger prices produce geometry errors and prevent publication of executable protective-order plans.

## Bybit-specific notes checked

- One-way linear order semantics are kept as `category=linear`, `positionIdx=0`.
- Opening side remains `Buy` for long and `Sell` for short.
- Closing/protective side remains `Sell` for long and `Buy` for short.
- Protective TP/SL sets `reduceOnly=True` and `closeOnTrigger=True`, so protective exits cannot intentionally increase exposure.
- `triggerDirection` is derived from direction and exit kind, not from UI labels:
  - long TP and short SL trigger on upward movement (`1`);
  - short TP and long SL trigger on downward movement (`2`).

## Tests added

`tests/test_iteration158_deep_bybit_directional_audit.py`:

1. `test_short_directional_exit_payload_exposes_executable_protective_order_intent`
2. `test_invalid_directional_geometry_does_not_publish_protective_bybit_orders`
3. `test_protective_order_plan_geometry_and_trigger_direction_are_canonical`
4. `test_protective_trigger_geometry_fails_closed_on_short_take_profit_above_reference`
5. `test_operator_ui_surfaces_backend_directional_risk_reward_and_distances`

## Checks executed

| Check | Result |
|---|---:|
| `python3 -m compileall -q app tests` | PASS |
| `pytest -q` | PASS — `551 passed in 15.96s` |
| `node --check app/ui/static/app.js` | PASS |
| Static grep scan over risky tokens (`tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`) | Completed — 1276 matches reviewed by targeted file inspection and regression coverage |

## Checks not executed / limitations

| Check | Status | Reason |
|---|---|---|
| `ruff` | Not executed | `ruff` is listed in `requirements-dev.txt`, but the binary is not installed in the provided environment. |
| `npm/yarn test` | Not executed | No `package.json`, `yarn.lock`, or `pnpm-lock.yaml` was present in the project root. |
| Live Bybit order/reconciliation test | Not executed | No live/testnet credentials or exchange write-access should be used in this offline audit environment. |
| Full market-regime simulation | Not executed as a new long-run backtest | Existing tests were run; this patch targeted deterministic semantic correctness, not new parameter optimization. |

## Residual risks

1. This remains a recommendation/operator project rather than a full exchange OMS; live order lifecycle risks such as partial fills, stale conditional orders, exchange-side cancellation races, and reconciliation must be tested in a credentialed testnet/staging environment before live use.
2. Bybit liquidation estimates in the project are approximate and cannot replace exchange-provided risk-tier/liquidation calculations.
3. UI now displays backend directional math, but any future new card/modal/log/notification must continue to consume `directional_exit_levels` instead of recreating TP/SL from raw lower/upper bounds.
4. `ruff` should be installed in the CI environment so configured linting is actually enforced.

## Result

The patched project passes the available deterministic checks and now has a stronger API contract: the same canonical model determines backend validation, UI display, directional risk/reward, and Bybit protective-order intent.
