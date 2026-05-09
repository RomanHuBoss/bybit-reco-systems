# Audit report — Linear USDT perpetual ticker scope hardening — 2026-05-09

## A. Краткое резюме

Повторный аудит подтвердил, что проект уже ограничен `futures_grid` для Bybit `category=linear` и содержит развитые проверки fees/funding/leverage/liquidation/risk report. Основная найденная новая проблема находилась в слое public ticker collection: batch/symbol-specific ticker rows могли быть приняты как USDT perpetual только по suffix `USDT`, без явного отбрасывания delivery/pre-market rows; fallback collector при кастомном client/mock мог взять первый вернувшийся ticker и записать его под запрошенным symbol.

Это опасно для futures grid recommender: чужой или непереставочный ticker искажает price/spread/liquidity/funding context, а значит может создать ложную рекомендацию или неверный preflight status.

Исправления выполнены в product-boundary/data-ingestion/UI слоях, добавлены регрессионные тесты. Полный suite: `383 passed`.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Bybit ticker scope | `linear` ticker filtering принимал любой `*USDT` ticker и не отбрасывал `deliveryTime != 0` / pre-market phase | Delivery/pre-market контракт мог попасть в market-data контур USDT perpetual grid | Добавлен fail-closed фильтр linear USDT perpetual ticker rows: exact USDT, perpetual deliveryTime, отсутствие pre-listing phase | `app/bybit_client.py`, `tests/test_iteration119_linear_perpetual_scope.py` |
| Collector fallback | При per-symbol fallback кастомный client мог вернуть ticker другого symbol; collector записывал его как запрошенный symbol | Чужой price/spread/liquidity/funding context для рекомендации | Добавлен exact-symbol selector; mismatch трактуется как missing ticker, не пишется в БД | `app/collector.py`, `tests/test_iteration119_linear_perpetual_scope.py` |
| UI/UX product boundary | Venue selector показывал `all`, хотя продукт поддерживает только один scope | UI создавал ощущение будущей мульти-venue/мульти-product поддержки | Selector заблокирован на “Bybit Linear USDT Perpetual only”, title уточнён | `app/ui/static/index.html` |
| Документация | README не описывал новый ticker-scope fail-closed guard | Оператор мог недооценить, что ticker rows тоже проходят product validation | README/CHANGELOG обновлены | `README.md`, `CHANGELOG.md` |

## C. Исправления торговой логики

- Grid logic: не менялась геометрия сетки; предыдущая логика `grid_type=arithmetic`, `grid_count` как число intervals сохранена.
- PnL/fees/funding: существующие net-of-fees/funding guards сохранены; новая правка защищает входные ticker/funding данные от чужого контракта.
- Leverage/liquidation: существующие worst-boundary liquidation checks сохранены; новая правка снижает риск, что эти checks будут рассчитаны по неправильному symbol/contract.
- Recommendation/rejection logic: если exact ticker не подтверждён, collector не пишет строку; downstream stale/missing ticker gates блокируют recommendation fail-closed.

## D. Исправления backend

- `app/bybit_client.py`
  - добавлен `_delivery_time_is_perpetual()`;
  - добавлен `_is_linear_usdt_perpetual_ticker()`;
  - `_filter_exact_symbol()` теперь сначала ограничивает ticker rows только Bybit Linear USDT perpetual scope, затем применяет exact-symbol filtering.

- `app/collector.py`
  - добавлены `_ticker_delivery_time_is_perpetual()`, `_is_exact_linear_usdt_perpetual_ticker()`, `_select_exact_ticker()`;
  - batch ticker path отбрасывает delivery/pre-market rows;
  - per-symbol fallback не берёт `lst[0]` без проверки symbol;
  - удалён дублирующий ключ `symbol` в funding payload.

## E. Исправления frontend/UI/UX

- `app/ui/static/index.html`
  - title/header теперь явно: “Bybit Linear USDT Futures Grid”;
  - venue selector больше не показывает `all`; единственный вариант — “Bybit Linear USDT Perpetual only”.

## F. Исправления документации и конфигов

- `README.md`
  - добавлено описание ticker scope guard: exact-symbol responses, no delivery/pre-market ticker rows, no relabeling чужого symbol.
- `CHANGELOG.md`
  - добавлен блок про Linear perpetual ticker scope hardening и результат тестов.

## G. Тесты

Добавлен файл:

- `tests/test_iteration119_linear_perpetual_scope.py`
  - `test_bybit_ticker_filter_excludes_delivery_and_premarket_contracts`;
  - `test_collect_once_does_not_relabel_wrong_symbol_ticker`.

Команды и результат:

```bash
pytest -q tests/test_iteration119_linear_perpetual_scope.py
# 2 passed

pytest -q
# 383 passed
```

## H. Остаточные риски

- Реальные Bybit fees и VIP fee tier должны подтягиваться из production/account context либо явно задаваться оператором.
- Актуальные instrument limits меняются; execution preflight обязан получать live instrument metadata перед подтверждением запуска.
- Live execution, partial fills, order amend/cancel и фактический PnL/funding требуют отдельного execution layer или paper-trading контура.
- Slippage model остаётся эвристикой; для low-liquidity symbols нужен live order book / depth-aware simulation.
- Funding history и funding interval должны мониториться на stale/missing values; при отсутствии данных recommendation должна оставаться blocked.
- Production API keys, permissions, account margin mode и available balance не проверяются public-data recommender напрямую.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python main.py
```

Проверки:

```bash
pytest -q
python -m py_compile app/*.py main.py
ruff check app tests main.py
```

Dev:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Production-style run:

```bash
python main.py
```
