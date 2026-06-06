# Audit report — Bybit futures directional semantics hardening

Дата: 2026-06-06

Область проверки: Bybit Linear USDT Futures / `futures_grid`, long/short/neutral semantics, TP/SL отображение, execution preflight, Bybit side/reduceOnly mapping, UI/API consistency, tests and static checks.

## Executive summary

Проект уже имел сильные защитные слои: Linear USDT scope, USDT perpetual metadata validation, tick/qty/minNotional checks, conservative grid economics after fees/funding, execution-time funding and live-price preflight, risk caps, LLM pending guards, stale-market-data guards and UI short TP/SL regression coverage.

Основной остаточный риск был не в конкретной формуле short TP/SL в UI, а в архитектуре: directional mapping жил в UI как самостоятельная логика. Это означает, что будущая правка backend/API или внешний execution adapter могли снова разойтись с отображением оператора. Исправление: добавлен единый backend-модуль `app/trading_semantics.py`, API теперь публикует canonical `directional_exit_levels`, UI использует backend payload с fallback на старую локальную функцию, а execution preflight явно проверяет directional TP/SL geometry.

## Найденные проблемы и исправления

| Severity | Область | Файл/участок | Проблема | Финансовый/торговый риск | Исправление |
|---|---|---|---|---|---|
| High | Directional TP/SL source of truth | `app/ui/static/app.js` | UI сам вычислял operator TP/SL из kill-switch bounds; backend не публиковал canonical mapping. Даже при правильной текущей формуле это оставляло риск будущего расхождения API/UI/execution. | Оператор мог увидеть TP/SL, отличающиеся от backend/execution semantics после следующей доработки, особенно для short. | Добавлен `app/trading_semantics.py`; `_augment_reco_for_ui()` добавляет `directional_exit_levels`; UI использует `operatorExitLevelsFromBackend(...)` и оставляет локальный fallback только для legacy payload. |
| High | Execution preflight | `app/main.py::_validate_trade_plan_against_bybit_meta` | Preflight валидировал диапазон и kill-switch, но не фиксировал отдельный инвариант: long TP выше entry/SL ниже entry; short TP ниже entry/SL выше entry. | Malformed/manual payload мог выглядеть формально диапазонным, но содержать перепутанные directional exit levels. | Добавлена проверка `validate_directional_exit_geometry()` для `long`/`short`; ошибка блокирует execution preflight fail-closed. |
| Medium | Future Bybit execution adapter | `app/trading_semantics.py` | В проекте не было минимальной формализованной таблицы Bybit V5 side/reduceOnly semantics. | При добавлении live executor можно ошибиться в `Buy/Sell`, `reduceOnly`, `closeOnTrigger`, особенно при закрытии short. | Добавлен `bybit_linear_order_semantics()` для one-way directional orders: long open=Buy/close=Sell, short open=Sell/close=Buy, close uses `reduceOnly=True`, `closeOnTrigger=True`, `positionIdx=0`; neutral fail-closed. |
| Medium | Regression coverage | `tests/` | Тесты проверяли UI short TP/SL строками, но не фиксировали единый backend directional contract и Bybit side mapping. | Повторная регрессия могла пройти UI-static тесты, но сломать API/execution semantics. | Добавлен `tests/test_iteration148_directional_semantics_hardening.py` с 9 новыми проверками. |
| Low | API response shape | `app/main.py::api_recommendations` | В response dict был дублирующий ключ `regime`. | Не торговый риск, но снижает качество API и усложняет audit/readability. | Дубликат удалён. |
| Low | UI cache key | `app/ui/static/index.html` | После изменения JS требовался новый cache-busting key. | Браузер оператора мог оставить старую JS-логику после деплоя. | Версия static asset bump: `manual-ui-v26`; связанные тесты обновлены. |

## Добавленные/изменённые тесты

- `tests/test_iteration148_directional_semantics_hardening.py`
  - canonical long/short/neutral exit level mapping;
  - strict validation of long/short TP/SL geometry;
  - regression for swapped short TP/SL;
  - Bybit one-way `side/reduceOnly/closeOnTrigger/positionIdx` mapping;
  - neutral grid cannot be silently mapped to a single directional order;
  - API augmentation publishes `directional_exit_levels`;
  - execution preflight rejects invalid short exit geometry;
  - UI consumes backend `directional_exit_levels`.
- Updated existing UI cache-key assertions from `manual-ui-v25` to `manual-ui-v26`.

## Проверки

Passed:

```text
python3 -m compileall -q app tests
node --check app/ui/static/app.js
python3 -m pytest -q
490 passed in 11.74s
```

Not run / not applicable:

```text
npm/yarn tests: package.json отсутствует.
lint/type checks: конфигурация ruff/mypy/eslint/tsc отсутствует в архиве.
Live Bybit private execution tests: в проекте нет private OMS/EMS adapter и тестовых ключей; система остаётся recommendation/operator layer.
```

## Остаточные риски

1. Проект всё ещё не является полноценным live OMS/EMS: нет real order placement, fills stream, open orders reconciliation, external position truth и account-aware liquidation/margin model.
2. Exact liquidation price на Bybit зависит от risk tier, wallet margin, mark price, open orders and account state. Текущая модель остаётся conservative estimate для UI/preflight.
3. Grid outcome labeling остаётся proxy-моделью без реальных fills/funding/queue priority.
4. При добавлении live executor необходимо использовать `app.trading_semantics.bybit_linear_order_semantics()` как обязательный contract и добавить integration tests against testnet/private Bybit API.

## Изменённые файлы

- `app/trading_semantics.py` — новый canonical semantic module.
- `app/main.py` — API augmentation, directional exit validation, response cleanup.
- `app/ui/static/app.js` — UI consumes backend `directional_exit_levels` with legacy fallback.
- `app/ui/static/index.html` — static cache key bumped to `manual-ui-v26`.
- `tests/test_iteration148_directional_semantics_hardening.py` — new regression suite.
- Existing UI tests — cache key assertions updated to `manual-ui-v26`.
