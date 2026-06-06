# Deep trading-semantics audit — Bybit Linear USDT Futures Grid

Дата проверки: 2026-06-06  
Scope: backend trading semantics, quant/econometric feature layer, Bybit Linear USDT preflight, risk gates, TP/SL long-short mapping, operator UI, tests and static checks.

## Executive summary

Проект уже содержит сильные guardrails: `app/trading_semantics.py` является единым источником TP/SL mapping для `long`/`short`/`neutral`, `app/grid_math.py` корректно считает PnL для linear USDT long/short, а execution preflight в `app/main.py` проверяет Bybit metadata, tick/qty/minNotional/leverage, kill-switch containment и directional TP/SL geometry.

В ходе повторного deep-аудита исправлены четыре системных расхождения:

1. Feature-layer больше не доверяет порядку свечей от caller'а: OHLCV нормализуются в строго хронологический ряд, duplicated timestamp не дублирует rolling windows.
2. Open-interest trend больше не зависит от входного порядка и не double-count'ит один timestamp.
3. BTC beta/correlation не пропускает `NaN`, `inf`, нулевые и отрицательные цены в лог-доходности.
4. Operator UI теперь читает canonical `trade_plan.levels` / `trade_plan.reference_price` до legacy aliases `params.price_range_*` / `params.price_ref`, чтобы визуальная панель не расходилась с backend preflight.

## Изменённые файлы

- `app/features.py`
- `app/ui/static/app.js`
- `tests/test_iteration150_timeseries_ui_audit.py`
- `docs/AUDIT_REPORT_2026-06-06_DEEP_TRADING_REAUDIT.md`

## Findings and fixes

### HIGH — Feature-layer мог получить newest-first / duplicated OHLCV и исказить rolling indicators

**Файл:** `app/features.py`, `compute_features_from_ohlcv()`

**Проблема:** функция ожидала rows `old -> new`, но это было только комментарием. Если будущий caller передаст `newest-first` или ряд с duplicated timestamp, rolling volatility/ATR/SMA/slope/volume_z будут посчитаны по неправильной временной оси либо с double-count одной свечи. Это эконометрический риск: сигнал может использовать не тот последний бар и фактически получить look-ahead/order-leakage относительно intended chronology.

**Финансовый риск:** неверный regime/range/trend score может изменить recommendation status, direction confidence, grid spacing и risk gates.

**Исправление:** вход OHLCV теперь проходит defensive normalization:

- reject non-dict rows;
- reject non-finite OHLCV и `ts <= 0`;
- deduplicate по `ts` с replacement semantics `last write wins`;
- sort by `ts ASC` before rolling indicators.

**Тесты:** `test_feature_layer_sorts_and_deduplicates_candles_before_rolling_indicators`.

---

### MEDIUM — OI trend зависел от входного порядка и duplicated timestamp

**Файл:** `app/features.py`, `oi_trend()`

**Проблема:** функция документировала `newest-first`, но не нормализовала вход. Неправильный порядок или duplicate timestamp могли сделать `oi_now` не последним значением или исказить 4h/24h changes.

**Финансовый риск:** OI trend влияет на интерпретацию crowded long/short, capitulation/continuation и может менять risk narrative для futures grid.

**Исправление:** OI series теперь нормализуется newest-first внутри функции, duplicate timestamp заменяется последним входным значением, rows без timestamp остаются пригодными для synthetic tests без collision.

**Тесты:** `test_oi_trend_normalizes_order_and_duplicate_timestamps`.

---

### MEDIUM — BTC beta/correlation мог получить `NaN`, `inf`, ноль или отрицательную цену

**Файл:** `app/features.py`, `btc_beta()`

**Проблема:** `_rets()` проверял только previous close `> 0`, но не фильтровал текущий close, `NaN` и `inf`. Это могло привести к `math domain error`, `NaN` beta/correlation или downstream JSON pollution.

**Финансовый риск:** BTC-driven / independent-signal flag мог стать недостоверным, что влияет на scoring и risk explanation.

**Исправление:** перед log-return расчётом оба close series очищаются от non-finite и non-positive значений.

**Тесты:** `test_btc_beta_ignores_non_finite_and_non_positive_prices_without_crashing`.

---

### HIGH — Operator UI мог показывать legacy aliases вместо canonical trade_plan

**Файл:** `app/ui/static/app.js`, `buildOperatorValues()` / `buildOperatorFieldSpecs()`

**Проблема:** UI брал `params.price_range_lower`, `params.price_range_upper`, `params.price_ref` раньше, чем canonical `params.trade_plan.levels.range.lower/upper` и `params.trade_plan.reference_price`. Backend preflight уже использует canonical trade_plan. При stale/missing aliases UI мог показать другой диапазон/entry, чем проверяет backend и чем оператор переносит в Bybit.

**Финансовый риск:** визуальное расхождение UI и execution semantics может привести к ручному созданию grid-бота с неверным диапазоном, TP/SL perception или sizing.

**Исправление:** UI теперь читает значения в порядке:

1. canonical nested `trade_plan` / `trade_plan.levels`,
2. legacy `params.*`,
3. `operator_sheet` aliases.

Также `grid_count` теперь проверяется через `params.grid_count ?? plan.grid_count ?? params.grid_levels`, а `referencePrice` для position qty берётся из `plan.reference_price` до `params.price_ref`.

**Тесты:** `test_operator_ui_reads_canonical_trade_plan_before_legacy_param_aliases`.

## Directional TP/SL audit result

Проверены ключевые участки:

- `app/trading_semantics.py`:
  - `directional_exit_levels()` maps `long`: TP = upper, SL = lower;
  - `directional_exit_levels()` maps `short`: TP = lower, SL = upper;
  - `neutral`: no directional TP, only lower/upper kill-switch exits;
  - `validate_directional_exit_geometry()` blocks invalid long/short geometry;
  - `bybit_linear_order_semantics()` maps one-way Bybit Linear order side and reduceOnly for open/close.
- `app/grid_math.py`:
  - `linear_pnl_usdt()` signs PnL correctly for long and short;
  - unknown side returns zero instead of silently becoming long;
  - liquidation buffer is side-aware.
- `app/main.py`:
  - execution preflight validates range, kill-switch containment, directional exit geometry, tick/qty/minNotional/leverage, funding edge and live price drift;
  - operator payload is augmented with canonical `directional_exit_levels`.
- `app/ui/static/app.js`:
  - short TP/SL is no longer derived only in UI; backend `directional_exit_levels` is preferred;
  - UI cache headers already use `no-store` for `/` and `/static/*`.

No new TP/SL inversion was found after the patch. Existing regression tests include `test_iteration147_short_tp_sl_ui_hardening.py` and `test_iteration148_directional_semantics_hardening.py`.

## Risk-management and Bybit-specific checks observed

Existing code already contains checks for:

- max concurrent bots, symbol bot cap, daily drawdown and cooldown after losses;
- Bybit Linear USDT symbol scope;
- LinearPerpetual / quoteCoin=USDT / settleCoin=USDT / status=Trading;
- tickSize, qtyStep, minQty, maxQty, minNotionalValue;
- leverageFilter min/max/step;
- grid count Bybit range 2..400;
- execution-time funding stale/extreme/edge-turned-negative gates;
- live price outside range / kill-switch / excessive reference drift;
- no actionable launch link when recommendation is blocked/pending/no_trade.

## Checks executed

### Passed

```text
python3 -m compileall -q app main.py
node --check app/ui/static/app.js
pytest -q
498 passed in 18.58s
```

### Not executed / unavailable

```text
ruff check app tests main.py
# ruff: command not found
python3 -m ruff check app tests main.py
# No module named ruff
```

`requirements-dev.txt` pins `ruff==0.15.9`, but it is not installed in the current execution environment. No `package.json` was present, so `npm/yarn` tests are not applicable for this archive.

## Static scan focus

Manual/static review focused on these tokens and modules:

- `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `positionIdx`, `kill`, `leverage`, `pnl`, `roi`, `risk`;
- `app/trading_semantics.py`, `app/grid_math.py`, `app/main.py`, `app/recommender.py`, `app/features.py`, `app/bybit_client.py`, `app/ui/static/app.js`, tests under `tests/`.

## Residual risks

1. This repository is primarily recommendation/operator tooling. It does not implement a full live OMS with exchange-side order placement, clientOrderId idempotency, partial-fill state machine, TP/SL order reconciliation and reduceOnly protective-order maintenance. If live execution is added, these must be implemented before production trading.
2. Liquidation price remains a conservative approximation, not Bybit's exact risk-tier and wallet-balance liquidation engine.
3. UI regression coverage is static source-level. A browser/E2E test should be added if a Playwright/Selenium stack is introduced.
4. RUFF could not be executed in this environment because the dev dependency is not installed.

## Added tests

- `tests/test_iteration150_timeseries_ui_audit.py::test_feature_layer_sorts_and_deduplicates_candles_before_rolling_indicators`
- `tests/test_iteration150_timeseries_ui_audit.py::test_oi_trend_normalizes_order_and_duplicate_timestamps`
- `tests/test_iteration150_timeseries_ui_audit.py::test_btc_beta_ignores_non_finite_and_non_positive_prices_without_crashing`
- `tests/test_iteration150_timeseries_ui_audit.py::test_operator_ui_reads_canonical_trade_plan_before_legacy_param_aliases`

## Final verdict

After the fixes, all available executable checks pass. The project's long/short TP/SL semantics are consistent across backend helper, API payload augmentation, execution preflight and operator UI. The remaining high-impact gap is outside the current repository scope: a true live OMS/reconciliation layer would need additional exchange-order lifecycle controls before any autonomous production execution.
