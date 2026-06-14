# Audit Report — Protective TP/SL reference fail-closed and directional PnL quantity, 2026-06-14

## Scope

Аудит выполнен по загруженному архиву `bybit-reco-systems-main(1).zip` как по потенциально боевому контуру рекомендаций для Bybit futures / linear USDT. Проверялись не только линтеры и тесты, а участки, где ошибка направления, TP/SL, triggerDirection, reduceOnly, qty/notional, rounding или UI-отображения может привести к неверному операторскому решению.

Проверенные зоны:

- canonical long/short model: `app/trading_semantics.py`;
- backend operator payload and Bybit preflight: `app/main.py`;
- grid/risk math: `app/grid_math.py`, `app/risk.py`;
- econometric/time-series features: `app/features.py`, `app/direction.py`;
- frontend operator sheet and TP/SL display: `app/ui/static/app.js`, `app/ui/static/index.html`;
- regression tests under `tests/`.

External reference checked: current Bybit V5 documentation for `/v5/order/create`, including `side`, `positionIdx`, `takeProfit`, `stopLoss`, `triggerDirection`, `reduceOnly`, `closeOnTrigger`, and linear/inverse trigger fields. Key relevant Bybit constraints: `positionIdx=0` for one-way mode; hedge mode uses `1` for Buy side and `2` for Sell side; `triggerDirection=1` means trigger on rise and `2` means trigger on fall; `reduceOnly=true` is required when closing/reducing and cannot be combined with TP/SL fields in the same normal order payload.

## Executive summary

The project already had strong protections from prior hardening:

- long TP above entry / SL below entry;
- short TP below entry / SL above entry;
- neutral grids do not receive a single directional TP;
- frontend uses backend `directional_exit_levels` instead of recalculating the truth independently;
- Bybit order-side mapping for one-way linear USDT is centralized.

This pass found and fixed two additional issues:

1. **HIGH** — `bybit_linear_protective_order_plan()` could report a protective TP/SL plan as geometry-valid when `triggerPrice` was positive but `reference_price` was missing. Without reference/entry price, the system cannot prove that TP/SL is on the correct side of the market.
2. **MEDIUM** — directional TP/SL gross PnL in the operator payload used the default quantity of `1.0` even when the recommendation already contained total grid exposure. Risk/reward and distance percentages were correct, but gross USDT profit/loss could be misleading in technical/operator payloads.

All available checks pass after the patch.

## Findings and fixes

### Finding 1 — HIGH — Protective TP/SL plan did not fail closed when reference price was missing

**File:** `app/trading_semantics.py`  
**Function:** `bybit_linear_protective_order_plan`

**Problem:**

The helper created Bybit-style protective order intent with `reduceOnly=True`, `closeOnTrigger=True`, `side` equal to the close side, and `triggerDirection` derived from long/short and TP/SL purpose. However, if `reference_price` was absent and `trigger_price` was positive, `geometry_errors` remained empty and `geometry_valid=True`.

**Why this is wrong:**

A protective TP/SL trigger is directional. For long, TP must be above entry and SL below entry. For short, TP must be below entry and SL above entry. Without entry/reference price, the system cannot prove whether the trigger is profit-taking or loss-stopping. A future execution adapter could incorrectly trust `geometry_valid=True` and submit a reduce-only conditional order whose economic purpose was not verified.

**Trading risk:**

- Wrong-side trigger accepted as apparently valid.
- Short TP/SL inversion may pass through a future integration if reference price is not supplied.
- Operator/API payload can overstate safety because the Bybit side/reduceOnly flags look correct even when TP/SL geometry is unproven.

**Fix:**

`bybit_linear_protective_order_plan()` now fails closed when `reference_price` is missing, non-finite or non-positive:

- adds `PROTECTIVE_REFERENCE_PRICE_INVALID`;
- keeps `geometry_valid=False`;
- also reports `PROTECTIVE_TRIGGER_PRICE_INVALID` if trigger price is invalid.

**Regression test:**

- `tests/test_iteration161_protective_reference_and_qty.py::test_protective_order_plan_fails_closed_without_reference_price`

### Finding 2 — MEDIUM — Directional exit gross PnL used 1 unit instead of known total grid exposure

**File:** `app/main.py`  
**Function:** `_directional_exit_payload_for_reco`

**Problem:**

`directional_trade_math()` supports a `qty` parameter, but `_directional_exit_payload_for_reco()` called it without passing the recommendation's actual or estimated size. Therefore, `gross_profit_usdt` and `gross_loss_usdt` in `directional_exit_levels.trade_math` represented one base unit, not the known grid exposure.

**Why this is wrong:**

The visible risk/reward and distance percentages were still mathematically correct because they do not depend on absolute quantity. But gross USDT figures in a trading payload should not silently switch to one-unit semantics when total notional or position quantity is present.

**Trading risk:**

- Operator/debug/API users may underestimate absolute loss to SL or profit to TP.
- Downstream reports may mix one-unit PnL and full-exposure PnL.
- Future execution/risk adapters may consume a technically valid but economically incomplete value.

**Fix:**

Added `_directional_exit_qty_for_reco()` in `app/main.py`. It derives conservative directional TP/SL quantity in this order:

1. explicit total position quantity, e.g. `estimated_position_qty`, `position_qty`, `total_qty`;
2. total position/grid notional divided by reference price, e.g. `estimated_total_order_notional_usdt / reference_price`;
3. per-order quantity multiplied by `grid_count`;
4. single-leg quantity or notional fallback.

`_directional_exit_payload_for_reco()` now exposes:

- `qty`;
- `qty_source`;
- `trade_math` gross PnL based on the derived quantity, while preserving risk/reward and percentage distances.

**Regression test:**

- `tests/test_iteration161_protective_reference_and_qty.py::test_directional_exit_payload_uses_total_grid_exposure_for_pnl_math`

## Retained existing protections verified in this pass

| Area | Result |
|---|---|
| Long/short TP/SL mapping | `directional_exit_levels()` still maps long TP=upper/SL=lower and short TP=lower/SL=upper. |
| Directional geometry validation | `validate_directional_exit_geometry()` rejects swapped short TP/SL and long TP/SL. |
| Bybit close/open mapping | `bybit_linear_order_semantics()` keeps one-way `positionIdx=0`; long open=Buy, long close=Sell, short open=Sell, short close=Buy. |
| Protective trigger direction | long TP and short SL trigger on rise; short TP and long SL trigger on fall. |
| UI short TP/SL display | `app/ui/static/app.js` renders backend `directional_exit_levels` and rounds short TP down / short SL up. |
| Neutral grid semantics | neutral recommendations still render no directional TP and expose lower/upper kill-switch only. |
| Tick/qty/minNotional checks | `_validate_trade_plan_against_bybit_meta()` still validates price ticks, qty step, min qty, min notional, grid range and grid count. |
| Risk gates | runtime risk caps, funding stale checks, live-price checks and operator preflight remain in place. |

## Static scan summary

The following high-risk terms were scanned across `app`, frontend JS and tests:

```text
tp: 670
sl: 302
stop: 309
take: 178
upper: 433
lower: 521
short: 351
long: 432
side: 166
Buy: 13
Sell: 10
reduceOnly: 14
kill: 213
leverage: 476
pnl: 147
roi: 0
risk: 605
tick: 582
qty: 416
minNotional: 11
min_notional: 65
positionIdx: 5
triggerDirection: 11
```

No additional long-only TP/SL assumption requiring code change was found in this pass.

## Checks performed

### Passed

```text
python -m compileall -q app tests
OK

node --check app/ui/static/app.js
OK

python -m pytest -q
559 passed in 23.71s
```

### Not configured / not applicable

```text
npm/yarn tests: package.json not found; JS check limited to node --check and Node-executed regression tests inside pytest.
External private Bybit integration tests: not executable from this archive because no API credentials, exchange sandbox state, websocket fills or account-mode fixtures were provided.
```

## Files changed

- `app/trading_semantics.py`
  - fail-closed protective TP/SL validation without `reference_price`.
- `app/main.py`
  - added directional exit quantity derivation and full-exposure gross PnL in `directional_exit_levels.trade_math`.
- `tests/test_iteration161_protective_reference_and_qty.py`
  - new regression tests for missing protective reference price and total-grid-exposure PnL.
- `docs/AUDIT_REPORT_2026-06-14_PROTECTIVE_REFERENCE_AND_QTY.md`
  - this report.

## Residual risks

1. **Private execution lifecycle remains unproven in this archive.** A complete live/testnet proof still needs deterministic fixtures or credentials for rejected orders, partial fills, stale/canceled orders, retries, Bybit rate limits, private position reconciliation and restart recovery.
2. **Liquidation estimates remain approximate.** Existing logic correctly treats them as conservative guidance, but exact Bybit liquidation depends on mark price, margin mode, wallet/account state and risk tiers.
3. **Operator UI is not an OMS.** The project can display and validate a safe plan, but final live safety depends on a private execution adapter preserving the same `trading_semantics.py` model.
4. **ROI field not found.** Static scan found PnL/risk math but no explicit `roi` field/function. This is not a failing test because the project appears to be a recommendation/operator UI rather than a full realized-ROI reporting system.
