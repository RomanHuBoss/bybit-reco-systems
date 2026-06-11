# Audit report — Bybit linear USDT TP/SL, risk, UI and test re-audit

Дата: 2026-06-11  
Проект: `bybit-reco-systems-main`  
Контекст: повторная проверка торговой семантики Bybit futures / linear USDT, long/short, TP/SL, kill-switch, UI и тестов.

## 1. Scope

Проведена инженерная проверка проекта как рекомендательной / операторской системы для Bybit linear USDT futures. Особый фокус:

- long/short directional semantics;
- TP/SL mapping;
- Bybit V5 order payload semantics;
- UI consistency with backend trading logic;
- risk guards before execution;
- static scan по trading/risk/Bybit terms;
- regression tests for high-risk sign/direction mistakes.

Репозиторий не содержит полноценного live OMS с реальным приватным исполнением, хранением fills, exchange websocket reconciliation и partial-fill accounting. Поэтому эта проверка закрывает кодовую торговую семантику, preflight/risk gates, UI rendering и тестируемые payload helpers, но не является live-exchange certification.

## 2. External reference points checked

По актуальной Bybit V5 Place Order documentation:

- `triggerDirection=1` означает триггер при росте рынка до `triggerPrice`, `triggerDirection=2` — при падении;
- `triggerDirection` и `triggerBy` валидны для `linear` / `inverse` conditional orders;
- `orderFilter=Order|tpslOrder|StopOrder` отмечен как `spot`-only;
- `reduceOnly=true` означает, что ордер может только уменьшить позицию, и должен использоваться при close/reduce;
- `closeOnTrigger` валиден для `linear` / `inverse` и предназначен для закрывающих conditional orders;
- `positionIdx=0` — one-way mode, `1` — hedge Buy side, `2` — hedge Sell side.

Эти пункты были использованы как источник истины для патча `bybit_linear_protective_order_semantics`.

## 3. Static/project scan

Построена карта проекта и выполнен статический поиск по ключевым trading terms в `app/` и `tests/`:

| Term | Hits |
|---|---:|
| tp | 360 |
| sl | 187 |
| stop | 235 |
| take | 134 |
| upper | 402 |
| lower | 468 |
| short | 312 |
| long | 408 |
| side | 148 |
| Buy | 9 |
| Sell | 9 |
| reduceOnly | 10 |
| kill | 193 |
| leverage | 401 |
| pnl | 135 |
| roi | 0 |
| risk | 560 |
| positionIdx | 5 |
| triggerDirection | 5 |
| orderFilter | 4 |

Вывод: в проекте уже была существенно укреплена directional-модель, но найдены два места, где оставался риск несоответствия Bybit V5 и UI/backend semantics.

## 4. Issues found and fixed

### 4.1 High — Bybit linear protective exits emitted spot-only `orderFilter`

**File:** `app/trading_semantics.py`  
**Function:** `bybit_linear_protective_order_semantics`

**Problem:** helper для защитных TP/SL exits возвращал `orderFilter: "StopOrder"`. По текущей Bybit V5 Place Order schema `orderFilter` valid for `spot` only, а linear/inverse conditional orders должны задаваться через `triggerPrice`, `triggerDirection`, `triggerBy` и закрывающие safety flags.

**Trading/financial risk:**

- live/testnet payload для `category=linear` мог быть отклонен биржей;
- в худшем случае кодовые тесты фиксировали бы неверную модель как «каноническую»;
- это особенно опасно для SL/TP, потому что rejected protective exit оставляет позицию без ожидаемой защиты.

**Fix:**

- удален `orderFilter` из linear protective semantics;
- сохранены `reduceOnly=True` и `closeOnTrigger=True`;
- сохранена строгая directional mapping:
  - long TP: close side `Sell`, `triggerDirection=1`;
  - long SL: close side `Sell`, `triggerDirection=2`;
  - short TP: close side `Buy`, `triggerDirection=2`;
  - short SL: close side `Buy`, `triggerDirection=1`;
- добавлены `triggerBy="LastPrice"` и `orderType="Market"` как явная conditional-market exit семантика;
- добавлен комментарий, чтобы не вернуть spot-only поле в будущем.

**Tests:**

- обновлены существующие Bybit semantics assertions;
- добавлен `tests/test_iteration156_bybit_linear_ui_semantics.py`.

### 4.2 High/Medium — UI trusted backend TP/SL ordering without reference-relative validation

**File:** `app/ui/static/app.js`  
**Functions:** `directionalExitGeometryOk`, `operatorExitLevelsFromBackend`

**Problem:** UI fallback-guard проверял directional consistency backend TP/SL, но ранее был недостаточно строгим: ordering TP vs SL не гарантирует, что уровни находятся по правильные стороны от reference/entry. Например, для short математически правильно только `TP < reference` и `SL > reference`; просто `TP < SL` недостаточно.

**Trading/financial risk:**

- malformed/stale backend response мог быть визуально принят UI как корректный;
- оператор мог увидеть TP/SL, которые выглядят валидно по порядку, но фактически находятся не по ту сторону от entry/reference;
- риск особенно критичен для short-ботов, где визуальная инверсия TP/SL ранее уже была отдельной проблемой.

**Fix:**

- `directionalExitGeometryOk(direction, takeProfit, stopLoss, referencePrice = null)` теперь требует finite positive `referencePrice`;
- для `long` проверяется `TP > reference` и `SL < reference`;
- для `short` проверяется `TP < reference` и `SL > reference`;
- `operatorExitLevelsFromBackend` передает `exitLevels.reference_price` в guard;
- при провале проверки UI падает обратно на локальную kill-switch mapping и явно показывает diagnostic text;
- static asset version в `index.html` поднят до `manual-ui-v29`, чтобы браузер не держал старый JS после патча.

**Tests:**

- добавлен static regression test, фиксирующий reference-relative guard в JS.

### 4.3 Low — duplicate local assignments in execution preflight code

**File:** `app/main.py`  
**Functions:** `_execution_market_data_blocks`, `_execution_live_price_blocks`

**Problem:** в двух местах были дублированные локальные присваивания (`blocks`, `lower_ks`). Это не меняло runtime-семантику, но ухудшало читаемость и повышало риск будущего drift при правках execution preflight.

**Fix:** удалены дубли.

## 5. Validated existing safeguards

В существующем коде уже присутствуют и после патча остаются ключевые protective layers:

- единые функции `normalize_direction`, `directional_pnl`, `directional_exit_levels`, `validate_directional_exit_geometry`;
- тесты для long/short PnL и exit geometry;
- risk validation before execution через `_validate_trade_plan_against_bybit_meta`;
- checks для tick size, qty step, min qty, min notional, max leverage, liquidation buffer, kill-switch bounds, gross/net edge;
- fail-closed checks для stale candles/tickers;
- execution-time price guard: recommendation блокируется, если актуальная цена уже ушла за range/kill-switch context;
- operator workflow gates для demo/testnet/live mode и preflight checks;
- UI warnings/fallbacks для backend mismatch.

## 6. Tests added/updated

### Added

- `tests/test_iteration156_bybit_linear_ui_semantics.py`
  - `test_linear_protective_order_semantics_do_not_emit_spot_only_order_filter`
  - `test_operator_ui_validates_backend_tp_sl_against_reference_price`

### Updated

- existing tests that previously expected `orderFilter="StopOrder"` for linear protective exits now assert that `orderFilter` is absent and `triggerBy="LastPrice"`, `orderType="Market"` are present;
- UI cache-bust tests updated from `manual-ui-v28` to `manual-ui-v29`.

## 7. Checks performed

| Check | Result |
|---|---|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| targeted regression pytest | PASS — `40 passed in 2.03s` |
| full pytest with plugin autoload disabled | PASS — `541 passed in 23.97s` |
| static grep scan | Completed |

Targeted regression command:

```bash
pytest -q \
  tests/test_iteration147_short_tp_sl_ui_hardening.py \
  tests/test_iteration148_directional_semantics_hardening.py \
  tests/test_iteration153_prompt_directional_risk_reaudit.py \
  tests/test_iteration155_deep_directional_risk_patch.py \
  tests/test_iteration156_bybit_linear_ui_semantics.py
```

Full suite command that completed:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
```

## 8. Checks not available / not fully completed

- `ruff` was not installed in the container (`ruff: command not found`), so ruff lint could not be executed.
- No `package.json` was present, so npm/yarn tests were not applicable.
- A deterministic full-suite run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` completed successfully. An earlier default `pytest -q` attempt was interrupted by the execution timeout in this container, so the plugin-isolated run is the recorded full-suite result.
- No live Bybit private API, testnet account, or websocket execution reconciliation was exercised.

## 9. Residual risks

1. **No full live OMS in repo.** The repository primarily implements recommendation/operator workflow and preflight/risk logic. It does not fully model partial fills, live exchange position state machines, websocket order lifecycle or post-restart reconciliation.
2. **Bybit payload helper is semantic, not actual order submitter.** The protective helper now matches current V5 semantics, but live submission code should still pre-check `/v5/order/pre-check` or testnet order placement before production.
3. **Liquidation math remains approximate unless exchange margin engine is queried.** Current checks are useful fail-closed guards, not a substitute for exchange liquidation/margin model.
4. **No formal proof of no look-ahead bias.** Code-level guards and tests exist, but a full econometric validation requires dataset-level replay and independent backtest audit.
5. **Race conditions require live/event-driven verification.** The static/unit suite cannot prove behavior under network timeouts, concurrent retries, partial fills or exchange-side order amendments.

## 10. Summary of implemented patch

The patch removes a Bybit V5 linear-vs-spot semantic mismatch, hardens UI/backend TP/SL consistency against reference price, adds regression tests that fix correct long/short semantics, and cleans minor duplicate execution-preflight locals. All available deterministic checks passed after the changes.
