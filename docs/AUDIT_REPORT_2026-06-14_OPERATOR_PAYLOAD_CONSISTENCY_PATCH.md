# Audit report — Bybit operator payload consistency patch

Дата: 2026-06-14  
Scope: Bybit futures / linear USDT recommendation and operator-preflight layer.

## Контекст проверки

Проверялась зона, где торговая семантика из recommendation payload переходит в операторскую форму запуска futures grid: `params`, `trade_plan`, `operator_sheet`, Bybit instrument metadata, UI-карточка и strict execution-preflight.

Базовые проверки до исправлений уже проходили, поэтому аудит был сфокусирован не на lint-only ошибках, а на потенциальном расхождении между тем, что оператор видит в UI, и тем, что backend фактически валидирует перед переводом рекомендации в `executed`.

## Найденные проблемы

### 1. HIGH — strict Bybit preflight не учитывал все operator-facing источники sizing/leverage

- Файл: `app/main.py`
- Участок: `_validate_trade_plan_against_bybit_meta`
- Проблема: execution/preflight validation читала sizing и economics преимущественно из `trade_plan`, `params.sizing`, `params.economics` и top-level `params`, но не полностью учитывала `params.operator_sheet.sizing`, `params.operator_sheet.economics` и `params.operator_sheet.leverage`.
- Почему это ошибка: `operator_sheet` является операторским переносимым представлением сделки. Если qty/leverage/economics присутствуют там, но отсутствуют в более ранних legacy-полях, backend мог выдать предупреждение `SIZE_INPUT_REQUIRED` вместо проверки фактического qty, либо не использовать operator-sheet leverage/economics в strict execution plan.
- Финансовый/торговый риск: operator UI мог содержать исполнимый размер позиции, но strict-preflight проверял неполный источник истины. Это повышает риск пропуска off-step/minQty/minNotional проблем, ложного допуска/ложной блокировки и расхождения между отображаемой и валидируемой сделкой.
- Исправление: fallback-порядок strict-preflight расширен до `params -> trade_plan -> operator_sheet` для leverage и до `trade_plan.sizing -> params.sizing -> operator_sheet.sizing -> params.economics -> trade_plan.economics -> operator_sheet.economics -> operator_sheet` для sizing/notional.
- Тесты: `test_bybit_preflight_validates_operator_sheet_sizing_not_only_params`, `test_execution_preflight_accepts_operator_sheet_leverage_and_economics`.

### 2. MEDIUM — UI operator field math мог считать размер позиции не из того же источника, что backend

- Файл: `app/ui/static/app.js`
- Участок: `buildOperatorFieldSpecs`
- Проблема: UI-поля «Размер позиции», «Маржа» и derived qty использовали `params.sizing`, `params.economics` и `params`, но не читали `operator_sheet.sizing/economics/leverage` в том же порядке, что execution-preflight.
- Почему это ошибка: операторская карточка могла показывать `—` или derived notional по fallback-значению, хотя в operator sheet уже были явные sizing/economics поля.
- Финансовый/торговый риск: mismatch UI/backend создает риск ручного переноса неверной экспозиции или неверной оценки required margin.
- Исправление: UI теперь читает `operator_sheet`, `operator_sheet.sizing` и `operator_sheet.economics` для расчета маржи, notional, base qty, leverage и reference price.
- Тесты: `test_operator_ui_uses_operator_sheet_sizing_and_leverage_for_position_math`.

## Измененные файлы

- `app/main.py`
  - strict Bybit preflight теперь использует `operator_sheet.leverage` как fallback после `params.leverage` и `trade_plan.leverage`;
  - `account_mode` и `margin_mode` также принимают operator-sheet fallback;
  - sizing/notional validation теперь проверяет `operator_sheet.sizing`, `operator_sheet.economics` и direct fields на `operator_sheet`;
  - strict grid economics validation теперь учитывает `operator_sheet.economics`.
- `app/ui/static/app.js`
  - operator field specs используют единый source order для sizing/economics/leverage;
  - расчет displayed position notional/qty и margin больше не расходится с operator payload.
- `tests/test_iteration165_operator_payload_consistency.py`
  - добавлены регрессионные тесты operator-sheet sizing/leverage/economics и UI source-order.
- `docs/TRADING_LOGIC.md`
  - добавлен инвариант: operator-sheet sizing/economics/leverage должны проверяться strict-preflight и отображаться UI из того же fallback-порядка.
- `docs/STATIC_SCAN_2026-06-14_OPERATOR_PAYLOAD_CONSISTENCY.txt`
  - сохранена сводка static scan по trading-semantics ключам: `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`, `operator_sheet`, `sizing`, `min_notional`, `qty_step`.

## Добавленные тесты

1. `test_bybit_preflight_validates_operator_sheet_sizing_not_only_params`
   - проверяет, что `operator_sheet.sizing.order_qty` проходит реальные Bybit-фильтры;
   - фиксирует ошибки `ORDER_QTY_BELOW_MIN`, `ORDER_QTY_OFF_STEP`, `ORDER_NOTIONAL_BELOW_MIN`;
   - гарантирует отсутствие ложного `SIZE_INPUT_REQUIRED`, когда sizing есть в operator sheet.

2. `test_execution_preflight_accepts_operator_sheet_leverage_and_economics`
   - проверяет, что strict execution-preflight принимает leverage/economics из operator sheet;
   - фиксирует отсутствие ложных `LEVERAGE_MISSING_FOR_EXECUTION`, `GRID_ECONOMICS_MISSING`, `MIN_NOTIONAL_NOT_CHECKED`.

3. `test_operator_ui_uses_operator_sheet_sizing_and_leverage_for_position_math`
   - фиксирует UI-регрессию: карточка оператора обязана читать `operator_sheet.sizing`, `operator_sheet.economics` и `operator_sheet.leverage`.

## Проверки

Выполнено после исправлений:

```text
python3 -m compileall -q app main.py
python3 -m pytest -q
node --check app/ui/static/app.js
```

Результат:

```text
580 passed in 19.69s
```

`node --check` применим к `app/ui/static/app.js`; HTML-файлы не являются JS-модулями и не проверяются этой командой. `npm`/`yarn` test suite в корне проекта не обнаружен.

## Остаточные риски

- Проект остается recommendation/operator-audit layer, а не полноценным exchange order/fill state machine.
- Точная ликвидация требует Bybit risk tier, mark price, wallet margin, текущей позиции и account-state; текущая liquidation model остается conservative approximation для UI/preflight.
- Реальная idempotency ордеров, partial fills, cancel/replace и reconciliation должны обеспечиваться внешним execution layer, если проект подключается к боевому размещению ордеров.
- Live Bybit API side-effects не выполнялись: проверка ограничена unit/integration tests, static scan и локальной синтаксической проверкой.

## Severity summary

| Severity | Count | Summary |
|---|---:|---|
| Critical | 0 | Новых critical дефектов в рамках патча не найдено. |
| High | 1 | Неполная strict-preflight проверка operator-sheet sizing/leverage/economics. |
| Medium | 1 | UI operator card не полностью использовала operator-sheet fallback. |
| Low | 0 | Только документационное уточнение source-order. |
