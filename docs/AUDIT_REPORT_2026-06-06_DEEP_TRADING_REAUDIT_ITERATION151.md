# Deep audit report — Bybit Linear USDT futures grid system, iteration 151

Дата: 2026-06-06  
Scope: backend trading semantics, TP/SL long-short mapping, operator UI, Bybit Linear USDT metadata/preflight, risk gates, timeseries/quant logic, static scan and tests.

## Executive summary

Повторный аудит подтвердил, что в архиве уже были сильные защитные слои:

- `app/trading_semantics.py` является единым backend-контрактом для directional exit semantics;
- `app/grid_math.py` корректно различает long/short PnL, liquidation buffer и funding cashflow;
- execution preflight в `app/main.py` проверяет Bybit metadata, tick/qty/minNotional/leverage, диапазон, kill-switch, funding, live price drift и directional TP/SL geometry;
- operator UI предпочитает backend `directional_exit_levels`, а не самостоятельный short/long mapping;
- статические regression-тесты уже покрывали short TP/SL inversion.

Новой критичной инверсии TP/SL для short не найдено. Найдено и исправлено одно риск-значимое UI/backend расхождение: проценты «до нижней границы» и «до нижнего kill-switch» считались с нижней границей в знаменателе, а верхние расстояния — с текущей ценой. Это завышало downside buffer при далёкой нижней границе и могло сделать operator panel визуально более безопасной, чем фактическое расстояние от текущей цены.

## Изменённые файлы

- `app/main.py`
- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `tests/test_iteration151_operator_distance_and_ui_failclosed.py`
- UI cache-key assertions in existing UI regression tests updated from `manual-ui-v27` to `manual-ui-v28`
- `docs/AUDIT_REPORT_2026-06-06_DEEP_TRADING_REAUDIT_ITERATION151.md`

## Findings, severity and fixes

| Severity | Area | File / code area | Problem | Trading / financial risk | Fix | Tests |
|---|---|---|---|---|---|---|
| High | Operator risk display | `app/main.py::_operator_decision_context_for_reco` | `distance_to_lower_pct` and `distance_to_kill_lower_pct` used `(current - lower) / lower`, while upper distances used `(upper - current) / current`. | Lower-bound room could be overstated. Example: current=100, lower=50 was shown as +100%, although the actual current-price downside buffer is +50%. An operator could underestimate downside proximity to range/kill-switch. | Added `_distance_from_current_to_bound_pct(...)`; both lower and upper distances now use current price as denominator. Positive means still inside the relevant bound; negative means breach. | `test_operator_bound_distances_use_current_price_as_symmetric_denominator`; `test_operator_bound_distance_turns_negative_after_bound_breach` |
| Medium | UI fail-closed behavior | `app/ui/static/app.js::operatorExitLevelsFromBackend` | A malformed backend payload with `has_directional_take_profit=true` but non-directional/unknown `direction` could still be displayed as a directional TP/SL. | If future API regression publishes inconsistent payload, UI could render a directional TP/SL for neutral/unknown instead of failing closed to kill-switch display. | UI now treats directional TP/SL as valid only when `has_directional_take_profit === true` and `direction` is explicitly `long` or `short`. | `test_operator_ui_normalizes_direction_labels_and_fails_closed_for_malformed_exit_payload` |
| Low | UI robustness | `app/ui/static/app.js::directionRu` | Direction labels did not normalize casing/spacing. | Low direct trading risk, but inconsistent payload casing could display `Short` as neutral in operator UI. | Direction labels now normalize with `String(...).trim().toLowerCase()`. | Same UI fail-closed test |
| Low | Static asset cache | `app/ui/static/index.html` | JS changed but static cache key needed a bump. | Browser could keep old operator JS after deploy. | Bumped `manual-ui-v27` → `manual-ui-v28`; updated regression assertions. | Existing cache-key tests + `test_static_asset_cache_key_bumped_after_distance_semantics_patch` |

## Directional TP/SL audit result

Checked paths:

- `app/trading_semantics.py`
  - `directional_exit_levels("long")`: TP = upper, SL = lower;
  - `directional_exit_levels("short")`: TP = lower, SL = upper;
  - `directional_exit_levels("neutral")`: no directional TP, only lower/upper kill-switch exits;
  - `validate_directional_exit_geometry(...)`: fail-closed for swapped long/short exits;
  - `bybit_linear_order_semantics(...)`: one-way mapping keeps long open=Buy, long close=Sell, short open=Sell, short close=Buy; close is reduce-only.
- `app/main.py`
  - API/operator augmentation publishes `directional_exit_levels`;
  - execution preflight validates reference/range/kill-switch geometry and directional exits;
  - operator context now computes price-to-bound buffers symmetrically from current price.
- `app/ui/static/app.js`
  - UI prefers backend `directional_exit_levels`;
  - short TP/SL mapping remains TP below / SL above;
  - malformed non-directional backend exit payload now fails closed to non-directional display.

No TP/SL inversion remained after this patch.

## Risk-management and Bybit-specific audit result

Observed existing protections:

- Linear USDT perpetual-only metadata checks: category, symbol, contract type, quote/settle coin, delivery/prelisting status;
- Bybit filters: tick size, price range, qty step, min/max order qty, min notional, min/max leverage and leverage step;
- Grid-specific geometry: range order, kill-switch containment, grid count, grid step, tick rounding and minNotional at conservative grid price;
- Execution-time guards: stale candle/ticker blocks, live price outside range/kill-switch, excessive reference drift, funding unavailable/stale/extreme/edge-turned-negative;
- Runtime risk gates: active bot caps, symbol caps, daily drawdown/loss cooldown;
- UI launch gating: launch link hidden unless status, risk report, LLM state and Bybit operator guard are all compatible with execution.

## Quant/econometric and timeseries audit result

Previously hardened components remain present:

- OHLCV feature layer sorts candles chronologically and deduplicates timestamps before rolling indicators;
- invalid OHLCV rows, non-finite values and impossible high/low/close relationships are rejected;
- OI trend normalizes order and duplicated timestamps;
- BTC beta/correlation rejects non-finite and non-positive prices;
- outcome labeling uses grid oscillation/proxy economics, cost floor and kill-switch penalties rather than a one-sided touch for neutral grids.

No new look-ahead/data-leakage issue was found in this pass. Residual limitation remains: outcome labels are proxy labels, not exchange fill-level PnL with order queue, partial fills and exact funding events.

## Static scan focus

Static scan was run across `app/` and `tests/` for tokens:

```text
tp, sl, stop, take, upper, lower, short, long, side, Buy, Sell, reduceOnly,
positionIdx, kill, leverage, pnl, roi, risk, minNotional, qtyStep, tickSize,
funding, look-ahead, lookahead, rolling
```

Scan returned 3092 matches and was reviewed around the trading-semantics, preflight, risk, feature/outcome and UI surfaces. No additional short TP/SL inversion or long-only PnL assumption was found.

## Checks executed

Passed:

```text
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
python -m pytest -q
502 passed in 16.82s
```

Not executed / unavailable:

```text
python -m ruff check app tests main.py
# /opt/pyvenv/bin/python: No module named ruff

ruff check app tests main.py
# ruff: command not found

npm/yarn tests
# package.json is absent; no Node test runner configured in this archive.
```

## Added tests

New file: `tests/test_iteration151_operator_distance_and_ui_failclosed.py`

- `test_operator_bound_distances_use_current_price_as_symmetric_denominator`
- `test_operator_bound_distance_turns_negative_after_bound_breach`
- `test_operator_ui_normalizes_direction_labels_and_fails_closed_for_malformed_exit_payload`
- `test_static_asset_cache_key_bumped_after_distance_semantics_patch`

Total test count after patch: 502 passed.

## Residual risks

1. This repository remains an operator/recommendation layer, not a full private live OMS/EMS. It does not place real Bybit private orders, track fills from private streams, reconcile open orders/positions continuously, or maintain exchange-side reduce-only protective orders.
2. Liquidation price remains a conservative estimate. Exact liquidation depends on Bybit risk tiers, wallet/account margin, mark price and open-order state.
3. Browser/UI tests are source-level static checks. A Playwright/Selenium E2E suite should be added if the project adopts a browser test stack.
4. `ruff` could not be run in the current environment despite being listed in dev requirements; install dev dependencies before CI linting.
5. Live/testnet private API behavior, API-key permissions and rate-limit behavior require connected Bybit testnet/live credentials and were not executed here.

## Final verdict

After iteration 151, available compile/static/runtime tests pass. The long/short TP/SL model remains consistent across backend semantic helper, execution preflight, API augmentation and UI rendering. The operator risk panel now displays range/kill-switch distances with a symmetric current-price denominator, removing a downside-buffer overstatement that could affect manual launch decisions.
