# Deep Bybit futures / linear USDT audit — 2026-06-14

## Scope

Audit requested as a full re-check of the Bybit futures / linear USDT trading system with emphasis on directional long/short semantics, TP/SL, kill-switch ranges, grid geometry, risk gates, Bybit V5 API semantics, UI/API consistency and regression testing.

Reviewed high-risk areas:

- `app/trading_semantics.py` — canonical long/short TP/SL, PnL, risk/reward, Bybit order intent.
- `app/main.py` — API/UI augmentation, Bybit metadata preflight, grid/risk validation.
- `app/recommender.py` — grid generation, costs/funding, sizing, leverage and trade plan construction.
- `app/grid_math.py` — tick/qty quantization, notional/margin/grid economics.
- `app/bybit_client.py` — Bybit public metadata access and linear USDT scope.
- `app/features.py`, `app/direction.py`, `app/outcomes.py` — time-series normalization, signal inputs and outcome semantics.
- `app/ui/static/app.js` — operator-facing TP/SL, long/short, grid and numeric display.
- `tests/` — existing and new regression tests for directional semantics and fail-closed behavior.

## External Bybit V5 cross-check

The code was cross-checked against official Bybit V5 API semantics:

- Linear/perpetual order `side`, `positionIdx`, `triggerDirection`, `reduceOnly`, `closeOnTrigger`, `triggerPrice` and TP/SL parameters.
- Linear instrument filters: `tickSize`, `minOrderQty`, `qtyStep`, `minNotionalValue`, leverage limits.
- One-way vs hedge position-mode semantics.

## Findings and fixes

### HIGH — invalid market price was converted into an actionable-looking synthetic price

- **File:** `app/recommender.py`
- **Area:** `_params(...)`, grid/trade-plan construction.
- **Problem:** invalid market price input (`0`, negative, `NaN`, non-finite, missing) was previously normalized by a positive fallback (`1.0`). In a trading system this is unsafe because missing or corrupt market data can produce plausible-looking grid ranges, TP/SL hints, sizing, margin and liquidation estimates.
- **Trading/financial risk:** an operator or downstream automation could see a fabricated executable plan derived from a fake reference price instead of a fail-closed non-actionable plan.
- **Fix:** invalid price is now detected with `_finite_or_none(...)`; the recommender no longer synthesizes a positive reference price. It emits finite but non-executable values (`price_ref=0.0`, zero range, zero notional), adds `price_input_valid=false` and `invalid_price_fail_closed=true`, and lets strict Bybit preflight reject the plan via existing missing/invalid executable-level checks.
- **Code changed:** `app/recommender.py:2013-2024`, `app/recommender.py:2121-2123`.
- **Tests added:** `tests/test_iteration161_invalid_price_fail_closed.py`.

### MEDIUM — non-finite ATR input could contaminate grid geometry before sanitization

- **File:** `app/recommender.py`
- **Area:** `_params(...)`, volatility/grid spacing.
- **Problem:** `atr_pct_for_grid` and `f["atr_pct"]` were cast directly with `float(...)`. A non-finite ATR value could propagate into range/spacing math before later JSON sanitization.
- **Trading/financial risk:** NaN geometry can cause inconsistent UI/API payloads or hide that a grid cannot be proven executable.
- **Fix:** ATR now uses `_finite_or_none(...)` and falls back to the conservative minimum ATR floor if the input is invalid.
- **Code changed:** `app/recommender.py:2021-2024`.
- **Tests added:** `test_recommender_params_sanitize_nonfinite_atr_before_grid_geometry`.

### Verified — canonical long/short TP/SL semantics remain consistent

- **Files:** `app/trading_semantics.py`, `app/main.py`, `app/ui/static/app.js`.
- **Result:** no new TP/SL inversion was found in the canonical path.
- **Verified semantics:**
  - Long: TP above entry, SL below entry, profit on price rise, loss on price fall.
  - Short: TP below entry, SL above entry, profit on price fall, loss on price rise.
  - Neutral/grid mode does not expose directional TP/SL as if it were a single directional position.
  - Backend directional payloads include geometry validation and do not publish Bybit protective orders if geometry is invalid.
  - UI uses backend directional TP/SL when valid and fails closed on invalid backend geometry.

### Verified — Bybit protective order intent is directionally safe

- **Files:** `app/trading_semantics.py`, `app/main.py`.
- **Result:** protective order semantics are aligned with a one-way linear USDT model:
  - Open long: `Buy`; close/protect long: `Sell`, `reduceOnly=true`, `closeOnTrigger=true`.
  - Open short: `Sell`; close/protect short: `Buy`, `reduceOnly=true`, `closeOnTrigger=true`.
  - Long TP and short SL use rising trigger direction.
  - Long SL and short TP use falling trigger direction.
  - Invalid protective trigger geometry fails closed.

### Verified — Bybit metadata/risk preflight gates are present

- **Files:** `app/main.py`, `app/grid_math.py`, `app/bybit_client.py`.
- **Result:** preflight validates tick size, price bounds, grid range, grid count, min/max leverage, qty step, min qty, min notional, margin sizing, executable trade plan fields and directional exit geometry.
- **Residual limitation:** without authenticated live exchange access in the audit environment, private Bybit position state, order lifecycle, partial fills, rejects and reconciliation can only be reviewed statically and through local tests.

## Tests added

`tests/test_iteration161_invalid_price_fail_closed.py`:

1. `test_recommender_params_do_not_synthesize_fake_reference_price_for_invalid_market_data`
   - Covers `0`, negative, string `NaN`, float `NaN`, and missing price.
   - Asserts fail-closed flags, zero non-executable price/range/notional and no non-finite JSON numbers.
2. `test_trade_plan_for_invalid_price_has_no_actionable_grid_or_directional_tp_sl_levels`
   - Asserts no actionable reference price, range, kill-switch, step or TP-per-leg levels are published.
3. `test_recommender_params_sanitize_nonfinite_atr_before_grid_geometry`
   - Asserts NaN ATR cannot contaminate generated grid geometry.

## Checks run

```text
python -m compileall -q app tests main.py
PASS

node --check app/ui/static/app.js
PASS

python - <<'PY'
import os
import pytest
code = pytest.main(['-q'])
print(f'PYTEST_MAIN_EXIT_CODE={code}', flush=True)
os._exit(int(code))
PY
564 passed in 24.30s
PYTEST_MAIN_EXIT_CODE=0
```

## Files changed

- `app/recommender.py`
- `tests/test_iteration161_invalid_price_fail_closed.py`
- `docs/AUDIT_REPORT_2026-06-14_PROMPT_DEEP_BYBIT_REAUDIT.md`

## Residual risks

- Private Bybit V5 order placement, authenticated position reconciliation, partial fills, rejected orders, live rate limits, insufficient balance and exchange-side idempotency cannot be fully proven in this offline audit environment.
- Liquidation estimates remain approximate because exact Bybit liquidation depends on risk tier, mark price, maintenance margin and wallet/account state.
- UI/backend static consistency was checked, but browser cache behavior and production deployment cache invalidation need operational validation after release.
- Any future strategy module that bypasses `app/trading_semantics.py` would reintroduce directional risk; all future TP/SL and protective order generation should continue to use the canonical helpers.
