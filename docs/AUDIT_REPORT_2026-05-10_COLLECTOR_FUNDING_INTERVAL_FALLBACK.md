# Audit report — Collector funding interval fallback hardening

Date: 2026-05-10

Scope: Bybit Linear USDT Futures grid recommendation collector, funding metadata, tests and documentation.

## A. Краткое резюме

Повторный аудит подтвердил, что проект уже ограничен одним продуктовым режимом: `futures_grid` на Bybit `linear` USDT perpetual. Основная найденная оставшаяся ошибка была в сборщике funding metadata: если ticker payload не содержал `fundingIntervalHour`, collector записывал `funding_interval_min=None`, хотя Bybit V5 instruments-info публикует `fundingInterval` в минутах для linear/inverse instruments.

Это не ломало execution fail-closed полностью, но создавало ложную деградацию качества данных: рекомендация могла блокироваться из-за отсутствующего interval или использовать неполный funding context, хотя точный interval был доступен через instrument metadata.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Funding metadata | Collector не добирал `fundingInterval` из instruments-info, когда ticker не содержал `fundingIntervalHour`. | Funding events до горизонта grid могли считаться от неполного interval context; рекомендация/execute-preflight могли деградировать к missing interval block или менее информативному risk report. | Добавлен fallback к `get_instrument_info(category='linear', symbol=...)` с product-scope validation перед принятием interval. | `app/collector.py` |
| Product scope | Instrument fallback мог бы быть опасен без повторной проверки contract/quote/settle/status. | Можно было принять interval от не-USDT или не-perpetual инструмента. | Fallback принимает interval только для exact symbol, `LinearPerpetual`, `USDT` quote/settle, `Trading`, без delivery/pre-listing. | `app/collector.py` |
| Regression coverage | Не было теста, что collector, а не только standalone client helper, использует instruments-info fallback. | README/TRADING_LOGIC могли обещать поведение, которое collector фактически не выполнял. | Добавлены 3 регрессии: fallback при missing ticker interval, отсутствие лишнего instrument call при ticker interval, отказ от USDC instrument fallback. | `tests/test_iteration136_collector_funding_interval_fallback.py` |

## C. Исправления торговой логики

- Grid logic: без изменения геометрии; остаётся arithmetic-only, `grid_count` — число Bybit price intervals.
- PnL/fees: без изменения формул; `net_profit_bps` остаётся net-of-fees/spread/slippage/adverse funding.
- Funding: collector теперь повышает качество funding context — при отсутствии `fundingIntervalHour` в ticker пытается взять `fundingInterval` из instruments-info.
- Leverage/liquidation: без изменения; existing fail-closed checks сохранены.
- Recommendation/rejection: рекомендации получают более точный `funding_interval_min`; missing interval blocks остаются fail-closed, если metadata не подтверждена.

## D. Исправления backend

- `app/collector.py`
  - добавлен `_funding_interval_min_from_instrument_info()`;
  - добавлен `_ensure_funding_interval_from_instrument_info()`;
  - `_fetch_ticker_payloads()` теперь применяет fallback как для batch ticker path, так и для symbol-specific ticker fallback;
  - fallback не принимает metadata, если exact symbol/product boundary не доказаны.

## E. Исправления frontend/UI/UX

Frontend не менялся: UI уже отображает funding/risk report из backend payload. Исправление backend делает эти поля точнее без изменения контракта.

## F. Исправления документации и конфигов

- `CHANGELOG.md` обновлён новой записью о collector funding interval fallback.
- Существующие README/TRADING_LOGIC утверждения о fallback теперь соответствуют фактическому поведению collector.

## G. Тесты

Добавлены:

- `tests/test_iteration136_collector_funding_interval_fallback.py::test_collector_fills_missing_funding_interval_from_instrument_info`
- `tests/test_iteration136_collector_funding_interval_fallback.py::test_collector_keeps_ticker_funding_interval_without_extra_instrument_call`
- `tests/test_iteration136_collector_funding_interval_fallback.py::test_collector_rejects_instrument_interval_when_product_scope_not_usdt_perpetual`

Результаты:

```bash
python -m pytest -q
# 444 passed in 10.83s

python -m py_compile app/*.py tests/*.py main.py
# passed

node --check app/ui/static/app.js
# passed
```

## H. Остаточные риски

- Реальные Bybit fees, VIP-tier и maker/taker split требуют live account metadata.
- Instrument limits и funding interval могут меняться; collector теперь обновляет interval из public metadata, но production всё равно должен мониторить upstream drift.
- Slippage/fill efficiency остаются модельными оценками, не гарантией исполнения.
- Точная liquidation price зависит от risk tier, wallet margin, mark price и account state.
- Live execution/paper trading требует отдельной сверки с Bybit bot creation flow и account permissions.

## I. Команды запуска

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python -m pytest -q
python -m py_compile app/*.py tests/*.py main.py
node --check app/ui/static/app.js
uvicorn app.main:app --reload
```
