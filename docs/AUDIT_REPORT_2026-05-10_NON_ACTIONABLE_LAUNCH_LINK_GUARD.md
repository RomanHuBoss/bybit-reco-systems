# AUDIT REPORT 2026-05-10 — NON-ACTIONABLE LAUNCH LINK GUARD

## A. Краткое резюме

Проект повторно проверен по продуктовому ограничению: только Bybit Linear USDT Futures / USDT Perpetual `futures_grid`. Базовая торговая математика, funding-aware grid spacing, liquidation buffer, Bybit instrument preflight и strict grid-only execution gates уже покрыты регрессионными тестами и проходят полный suite.

Найдена новая UI/UX проблема: операторская таблица и header карточки показывали ссылку на страницу создания Bybit Futures Grid для любых строк, включая `blocked`, `no_trade` и `pending`. Это противоречило risk report и могло подталкивать оператора к ручному запуску сетки, которую backend уже считает неисполняемой.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Frontend / UI guard | Ссылка на создание Bybit grid-бота рендерилась для non-actionable статусов | Оператор мог открыть Bybit create-flow из карточки `blocked/no_trade/pending` и обойти смысл risk/rejection логики | Добавлен `isLaunchableGridRecommendation()`: ссылка рендерится только для `futures_grid` + `linear` + `recommended/active` + `risk_report.decision=recommended` + без Bybit validation errors | `app/ui/static/app.js`, `tests/test_iteration130_non_actionable_launch_links.py` |
| Frontend / product scope | UI helper строил create-url из произвольного `bot_type` | Лишняя ветвь для неподдерживаемых product modes, риск будущей регрессии single-product UI | Заменено на фиксированный `futuresGridBotCreateUrl()` без параметра `bot_type` | `app/ui/static/app.js`, `tests/test_iteration130_non_actionable_launch_links.py` |
| Frontend / operator fields | В `buildOperatorValues()` оставались legacy-ветки stop/take labels для иных направленных bot-mode UI | Визуальная логика продолжала обслуживать нецелевые варианты, хотя продукт single-mode | Упрощено до Futures Grid semantics: `Стоп-лосс` = нижний kill-switch, `Тейк-профит` = верхний kill-switch для операторской карточки | `app/ui/static/app.js` |

## C. Исправления торговой логики

Новые изменения не меняют формулы PnL/grid/funding/leverage/liquidation. Проверено, что существующая экономика сетки остаётся conservative net-of-fees/funding: funding receipt не повышает canonical edge, adverse funding учитывается в spacing и risk report, liquidation buffer считается по worst boundary. Исправление закрывает UI-обход этих backend gates.

## D. Исправления backend

Backend не менялся в этой итерации. Повторно подтверждено, что execution preflight блокирует неподдерживаемый `bot_type`, не-`linear` venue, non-USDT / non-perpetual metadata, missing exchange filters, off-tick levels, funding deterioration и low liquidation buffer.

## E. Исправления frontend/UI/UX

- Таблица рекомендаций теперь показывает иконку создания Bybit Futures Grid только для launchable rows.
- Header карточки скрывает create-link и удаляет `href`, если рекомендация non-actionable.
- Link guard дополнительно смотрит на `risk_report.decision` и ошибки `bybit_plan_validation` / `bybit_operator_guard`.
- Product URL больше не строится от произвольного `bot_type`.
- Operator field labels упрощены под единственный поддерживаемый Futures Grid product.
- Static asset cache key повышен до `manual-ui-v16`.

## F. Исправления документации и конфигов

Добавлен этот audit report. Конфиги и README не потребовали изменения: продуктовый scope уже описывает только `futures_grid` на Bybit Linear USDT Futures и блокировки для non-actionable статусов.

## G. Тесты

Добавлено:

- `tests/test_iteration130_non_actionable_launch_links.py` — регрессия на скрытие create-link для non-actionable rows и запрет построения Bybit create-url из произвольного `bot_type`.

Обновлено:

- `tests/test_iteration124_prompt_reaudit.py` — ожидание условного create-link вместо безусловного.
- `tests/test_iteration122_ui_detail_badge_fit.py`, `tests/test_iteration129_ui_single_product_simplification.py` — cache key `manual-ui-v16` и single-product helper copy.

Результат:

```bash
python -m pytest -q
# 425 passed in 9.31s
```

## H. Остаточные риски

- Реальные Bybit fees, VIP tier и maker/taker фактического аккаунта требуют live-сверки.
- Актуальные `tickSize`, `qtyStep`, `minOrderQty`, `minNotionalValue`, leverage/risk tiers требуют fresh instruments-info перед исполнением.
- Slippage/fill-efficiency остаются модельными оценками.
- Funding history и будущие funding flips не гарантируются текущей ставкой.
- Exact liquidation price зависит от Bybit risk tier, mark price и wallet/account state.
- Перед production нужен paper trading / dry-run executor с live Bybit preview.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python main.py
```

UI открывается из FastAPI static route после запуска приложения. Production deployment должен использовать реальные env-переменные из `.env.example`, отдельную БД и внешний process supervisor.
