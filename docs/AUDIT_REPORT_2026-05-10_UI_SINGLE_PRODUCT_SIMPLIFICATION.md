# Audit report — UI single-product simplification

Дата: 2026-05-10

## A. Краткое резюме

Повторный аудит проведён по приложенному ТЗ: продукт остаётся строго рекомендационной системой только для Futures Grid на Bybit linear USDT perpetual. Backend/риск-логика уже fail-closed проверяют `bot_type=futures_grid`, `venue=linear`, USDT perpetual metadata, tick/lot/min-notional, funding, комиссии, margin/leverage и liquidation buffer.

Найден UI/UX-долг: операторская панель всё ещё показывала единственный продукт как будто это пользовательский выбор или важное табличное измерение: заголовок `Bybit Linear USDT Futures Grid`, колонку `Тип бота`, фильтр `Площадка`, а также `Площадка`/`Бот` в субокнах. При единственном поддерживаемом продукте это создавало шум, занимало место и могло выглядеть как поддержка альтернативных площадок/типов ботов.

Исправление: интерфейс теперь показывает продукт кратко как `Futures Grid`, удаляет выбор площадки и не выводит однотипные product/venue колонки в основном окне, health/outcomes subwindows и hero-блоке деталей. API-запросы при этом остаются явно ограничены `venue=linear`, чтобы backend-контракт и fail-closed scope не изменились.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| UI/UX | В основном окне была колонка `Тип бота` с длинным единственным значением | Шум, плохая читаемость, ложное ощущение выбора типа бота | Колонка удалена; таблица начинается с символа и торговых метрик | `app/ui/static/index.html`, `app/ui/static/app.js` |
| UI/UX | Был combobox `Площадка` с единственным вариантом | Ложное ощущение поддержки/выбора площадок | Combobox удалён; frontend всегда отправляет `venue=linear` | `app/ui/static/index.html`, `app/ui/static/app.js`, `app/ui/static/styles.css` |
| UI/UX | Детали рекомендации показывали product badge рядом с direction/status | Узкая колонка деталей перегружалась повторяющейся информацией | Hero subtitle оставляет только direction/status | `app/ui/static/app.js` |
| UI/UX | Health/outcomes subwindows повторяли `Площадка` и `Бот` | Диагностические таблицы перегружались постоянными измерениями | Redundant columns/section removed | `app/ui/static/app.js` |
| Regression safety | Старые тесты закрепляли длинный label | Будущие правки могли вернуть длинные лейблы | Обновлены regression-тесты и добавлен `iteration129` | `tests/test_iteration122_ui_detail_badge_fit.py`, `tests/test_iteration129_ui_single_product_simplification.py` |

## C. Исправления торговой логики

Торговые формулы не менялись в этой итерации. Аудит подтвердил, что изменение затрагивает только presentation layer: backend scope остаётся `futures_grid` + `linear`, а exchange/risk gates продолжают блокировать неподдерживаемые payload до исполнения.

- Grid logic: без изменений.
- PnL: без изменений.
- Fees/funding: без изменений; UI продолжает показывать conservative net grid edge, funding cost для допуска и excluded funding benefit.
- Leverage/liquidation: без изменений; risk report и execution preflight сохраняются.
- Recommendation/rejection logic: без изменений; `blocked/no_trade/pending` по-прежнему запрещают запуск.

## D. Исправления backend

Backend не менялся. Frontend по-прежнему передаёт `venue=linear` в `/api/v1/recommendations`, даже без combobox в DOM.

## E. Исправления frontend/UI/UX

- Заголовок панели: `Futures Grid — Панель оператора`.
- Удалён select `Площадка`.
- Удалена колонка `Тип бота` в главной таблице рекомендаций.
- Details hero больше не показывает bot-type badge.
- Health modal больше не показывает колонку `Площадка`.
- Outcomes modal больше не показывает секцию `По типу бота`, а также колонки `Бот`/`Площадка` в таблицах.
- Калибровочные тексты заменены с `bot_type`/`bot-specific` на продуктовую формулировку.
- Static asset cache key bumped: `manual-ui-v15`.

## F. Исправления документации и конфигов

- Добавлен этот audit report.
- CHANGELOG обновлён записью об UI single-product simplification.
- Конфиги не менялись.

## G. Тесты

Добавлены/обновлены regression-тесты:

- `tests/test_iteration129_ui_single_product_simplification.py`
- `tests/test_iteration122_ui_detail_badge_fit.py`

Команды проверки:

```bash
node --check app/ui/static/app.js
python -m pytest -q tests/test_iteration122_ui_detail_badge_fit.py tests/test_iteration129_ui_single_product_simplification.py tests/test_iteration128_score_ui_segmentation.py tests/test_iteration124_prompt_reaudit.py
```

## H. Остаточные риски

- Реальные Bybit fee tiers, funding interval и instrument limits всё равно должны проверяться live metadata/preflight на момент исполнения.
- UI-упрощение не заменяет paper/live execution testing.
- Если продуктовая линейка когда-либо расширится, UI нужно будет осознанно вернуть product/venue selectors вместе с backend support matrix.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python -m app.main
```
