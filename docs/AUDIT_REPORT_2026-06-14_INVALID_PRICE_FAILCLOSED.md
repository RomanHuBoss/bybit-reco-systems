# Deep Bybit Linear Futures Audit Patch — Invalid Price Fail-Closed

Date: 2026-06-14
Scope: Bybit linear USDT futures/grid recommendation semantics, long/short TP/SL display consistency, Bybit order/filter assumptions, risk gates, static/test checks.

## Executive summary

The audit focused on dangerous classes of trading-system failure: direction/sign inversion, TP/SL inversion, false exchange geometry, rounding/min-notional assumptions, and risk checks that could allow an apparently actionable futures-grid recommendation to be published when its market reference price is unavailable.

The existing directional model for long/short TP/SL, Bybit one-way side mapping, reduce-only protective order intent, UI TP/SL display, tick rounding, liquidation-buffer checks, funding-cost exclusion, and risk-limit gates already had substantial regression coverage. The confirmed remaining critical issue was in `app/recommender.py::_params()`: invalid market price input was silently coerced to `1.0`. This could synthesize plausible grid/risk/exchange values from missing or poisoned market data. The patch makes this path fail closed and adds regression coverage.

## External Bybit API checks used for this patch

Official references checked during the audit:

- Bybit V5 Place Order: `category=linear`, `side=Buy/Sell`, positive `qty`, `triggerDirection` for conditional orders, `positionIdx`, `reduceOnly`, `closeOnTrigger`, and `orderFilter` scope.
- Bybit V5 Instruments Info: `priceFilter.tickSize`, `lotSizeFilter.qtyStep`, `lotSizeFilter.minOrderQty`, `lotSizeFilter.minNotionalValue`, `leverageFilter`, `contractType=LinearPerpetual`, `settleCoin=USDT`, and `unifiedMarginTrade`.
- Bybit FAQ: `reduceOnly=true` is the key close-position flag; `closeOnTrigger` may also be used for trigger exits but must not conflict with non-reduce-only orders.

## Findings and fixes

| ID | Severity | File / area | Problem | Trading / financial risk | Fix | Tests |
|---|---:|---|---|---|---|---|
| F-001 | critical | `app/recommender.py::_params()` | Invalid `f["price"]` (`0`, negative, `NaN`, `None`) was converted to synthetic `1.0`. | False Bybit grid levels, TP/SL/kill-switch geometry, liquidation buffers, min-notional sizing and leverage policy could be shown as if market data existed. This is especially dangerous for linear derivatives because a fake reference price contaminates notional and margin estimates. | Replaced synthetic price fallback with explicit `price_input_valid` / `invalid_price_fail_closed`; invalid price now returns zero range, zero grid spacing, zero sizing, zero economics, blocked leverage policy, and `exchange_filter_assumption.mode="invalid_price"`. | `tests/test_iteration161_invalid_price_fail_closed.py` |
| F-002 | high | `app/recommender.py` recommendation publication pipeline | Invalid price path was not exposed as a dedicated execution block before risk-report generation. | A recommendation could be blocked by downstream zero economics, but operator/audit diagnostics would not identify the root cause as bad market reference price. | Added `INVALID_MARKET_REFERENCE_PRICE` block after `_params()` when `price_input_valid is False`; risk report now includes the root blocking reason. | Covered by invalid-price regression suite and full test run. |
| F-003 | medium | `app/recommender.py::_params()` numeric sanitization | `global_sent` and `direction_bias_strength` used direct float conversions in the same hot path. | Non-finite or malformed scoring inputs can poison payload values or crash recommendation generation. | Reused `_finite_float()` and `_finite_or_none()` for sentiment, direction strength and ATR selection in `_params()`. | Full regression suite. |
| F-004 | low | `tests/test_iteration161_invalid_price_fail_closed.py` | Existing invalid-price tests did not assert that leverage/economics stay non-actionable. | Future changes could preserve zero price flags while accidentally publishing non-zero economics or leverage policy. | Added regression test asserting invalid-price params have no actionable economics, no position notional, no qty, and `grid_geometry_model="invalid_price_fail_closed"`. | New test added. |

## Directional TP/SL audit result

Reviewed canonical files and tests:

- `app/trading_semantics.py`
  - `directional_exit_levels()` maps long TP=upper / SL=lower and short TP=lower / SL=upper.
  - `validate_directional_exit_geometry()` fails closed when long TP is not above entry, long SL is not below entry, short TP is not below entry, or short SL is not above entry.
  - `directional_trade_math()` computes long profit on price rise and short profit on price fall.
  - `bybit_linear_order_semantics()` maps one-way open/close side and `reduceOnly`.
  - `bybit_linear_protective_order_semantics()` and `bybit_linear_protective_order_plan()` set close-side, `reduceOnly=true`, `closeOnTrigger=true`, `positionIdx=0`, and direction-specific `triggerDirection`.

- `app/main.py`
  - `_directional_exit_payload_for_reco()` uses backend geometry validation before publishing protective-order plans.
  - It derives quantity context from total grid exposure before computing TP/SL PnL math.
  - It avoids publishing protective orders if backend geometry is invalid.

- `app/ui/static/app.js`
  - UI TP/SL mapping delegates to backend `directional_exit_levels` when available.
  - Short TP display is rounded down; short SL display is rounded up, avoiding visual risk-bound shrinkage.
  - Fallback UI geometry blocks directional TP/SL rendering when backend geometry is invalid.

Regression tests covering these areas passed:

- `tests/test_iteration147_short_tp_sl_ui_hardening.py`
- `tests/test_iteration148_directional_semantics_hardening.py`
- `tests/test_iteration155_deep_directional_risk_patch.py`
- `tests/test_iteration156_bybit_linear_ui_semantics.py`
- `tests/test_iteration157_ui_invalid_exit_failclosed.py`
- `tests/test_iteration158_deep_bybit_directional_audit.py`
- `tests/test_iteration160_frontend_tick_directional_rounding.py`
- `tests/test_iteration161_protective_reference_and_qty.py`
- `tests/test_iteration161_invalid_price_fail_closed.py`

## Risk-management audit result

Reviewed areas:

- `app/risk.py`: normalized runtime limits, max concurrent bots, max daily drawdown, cooldown after realized losses, max symbol bots, leverage bounds, max notional and max margin limits.
- `app/recommender.py`: grid economics, funding cost exclusion from approval edge, liquidation-buffer gating, leverage selection policy, recommendation publication blocks, persistence gate, LLM review hold, market-shock gates.
- `app/main.py`: Bybit operator guard, current metadata validation, UI/API blocking merge.

Confirmed behaviour after patch:

- Invalid market data fails closed before sizing/economics can become actionable.
- Risk report gets a specific invalid-reference-price rejection reason.
- The existing Bybit metadata guard still validates live/current instrument constraints separately.
- Full test suite passed after changes.

## Bybit-specific audit result

Reviewed areas:

- `app/bybit_client.py`: linear category normalization, exact symbol filtering, instruments-info metadata extraction, funding interval fallback.
- `app/trading_semantics.py`: one-way `positionIdx=0`, side mapping, `reduceOnly`, `closeOnTrigger`, `triggerDirection`.
- `app/main.py`: Bybit metadata guard and operator payload.
- `app/ui/static/app.js`: Bybit chart URL and tick-aware display rounding.

No additional code change was required in Bybit side/reduce-only mapping during this patch. Existing tests assert:

- Long open = `Buy`, long close = `Sell` reduce-only.
- Short open = `Sell`, short close = `Buy` reduce-only.
- Short TP trigger is downward (`triggerDirection=2`), short SL trigger is upward (`triggerDirection=1`).
- `orderFilter` is not used as a linear-perp protective-order discriminator.

## Checks performed

| Check | Result |
|---|---:|
| `python3 -m compileall -q app tests` | PASS |
| `pytest -q --disable-warnings --maxfail=1` | PASS — 567 passed |
| `node --check app/ui/static/app.js` | PASS |
| `npm test` | SKIPPED — no `package.json` in project root |
| Static grep scan over `tp/sl/stop/take/upper/lower/short/long/side/Buy/Sell/reduceOnly/kill/leverage/pnl/roi/risk` | Completed; patch focused on confirmed invalid market-reference-price issue |

## Files changed

- `app/recommender.py`
  - Removed fake `price=1.0` fallback.
  - Added invalid-price fail-closed payload.
  - Added `price_input_valid` and `invalid_price_fail_closed` flags to valid and invalid paths.
  - Added publication block `INVALID_MARKET_REFERENCE_PRICE`.
  - Hardened local numeric conversion for sentiment, ATR and direction-strength inputs.

- `tests/test_iteration161_invalid_price_fail_closed.py`
  - Added regression coverage to prove invalid-price params cannot publish actionable economics, position notional, qty or leverage policy.

## Residual risks

1. This project is still a recommendation/operator layer, not a full live OMS. Exchange execution, partial fills, order replacement, idempotency and reconciliation must remain guarded by live preflight and any future execution adapter.
2. Exact Bybit liquidation values depend on live account state, risk tier, mark price, wallet margin and exchange-side risk formulas. Existing liquidation figures are conservative estimates, not an exact exchange liquidation engine.
3. `npm test` could not be run because no Node package/test configuration exists.
4. Static grep confirms many trading-semantics terms are present; the audit concentrated on confirmed executable risk paths and regression-covered semantics rather than rewriting the architecture.

## Final status

Patched archive status: PASS for available local checks.

