# Audit report — collector current ticker gate for Bybit Linear USDT Futures grid-only recommender

Date: 2026-05-10

## A. Краткое резюме

Повторный аудит подтвердил, что проект уже в основном ограничен продуктом `futures_grid` для Bybit Linear USDT Futures / USDT Perpetual: baseline suite перед изменениями проходил полностью (`448 passed`). Основной оставшийся дефект был найден в data-readiness контуре collector: если текущий ticker по символу отсутствовал или был malformed, collector всё равно мог обновить OHLCV/derived таймфреймы в том же цикле.

Это опасно для рекомендательной системы grid-ботов: свежие свечи рядом со stale/отсутствующим last price/funding создают ложное ощущение готовности рынка и могут привести к публикации рекомендации на неполной рыночной картине.

Исправление сделано fail-closed: символ участвует в OHLCV/backfill/derived refresh только если в текущем collect-cycle был получен exact current ticker для Linear USDT symbol. Malformed spot-style symbols дополнительно отфильтровываются до рыночных запросов.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Market-data readiness | Missing/malformed ticker не останавливал OHLCV refresh в том же цикле | Свежие свечи могли смешаться со stale/absent price/funding; recommendation readiness завышалась | Добавлен `active_symbols` gate после `_fetch_ticker_payloads`; OHLCV и derived TF обновляются только для symbols with current ticker | `app/collector.py` |
| Symbol scope defense-in-depth | Direct collector callers могли передать malformed values вроде `BTC/USDT`, `ETH-USDT`, `USDT` | Нецелевые символы могли попадать в market-request/storage слой, если обходили bootstrap/config фильтры | Добавлен `_is_exact_linear_usdt_symbol()` и фильтр в `_normalize_symbols()` | `app/collector.py` |
| Regression coverage | Тесты не проверяли, что отсутствие ticker блокирует свечной контур | Ошибка могла вернуться без падения suite | Добавлены regression tests на missing ticker gate и malformed symbol filter | `tests/test_iteration138_collector_current_ticker_gate.py` |
| Test fixtures | Старые fake tickers не возвращали `symbol`, хотя strict exact-ticker proof требует exact symbol | Regression tests могли маскировать отличие fake payload от Bybit-like payload | Обновлены fixture payloads: `symbol` теперь присутствует | `tests/test_iteration63.py`, `tests/test_logic.py` |

## C. Исправления торговой логики

- Grid formulas, PnL, fee, funding, leverage и liquidation math в `app/grid_math.py` не переписывались в этой итерации: существующие Decimal-based net-of-fees/funding/liquidation gates сохранены.
- Исправлен upstream market-data invariant, от которого зависит вся торговая математика: нельзя считать grid recommendation по символу, для которого в текущем цикле нет exact current ticker.
- Новый invariant: `fresh OHLCV` без `fresh exact ticker` запрещён. Это снижает риск ложного допуска в recommendation/rejection logic.
- Malformed symbols fail-closed ещё до запроса данных, чтобы проект оставался strictly Bybit Linear USDT Futures / USDT Perpetual.

## D. Исправления backend

### `app/collector.py`

- Добавлен `_is_exact_linear_usdt_symbol()`.
- `_normalize_symbols()` теперь удаляет malformed / non-exact USDT symbols до collector loop.
- После `_fetch_ticker_payloads()` формируется `active_symbols`.
- Hot OHLCV, derived bootstrap и local derived TF generation теперь проходят только по `active_symbols`.
- Добавлены stats-поля:
  - `symbols_with_current_ticker`;
  - `symbols_skipped_without_ticker`.

## E. Исправления frontend / UI / UX

UI не менялся: дефект был ниже frontend-слоя. Текущий UI продолжает получать уже отфильтрованные backend recommendations/statuses. Новые collector stats пригодны для последующего отображения в health/status панели, если потребуется вынести operator-facing diagnostics.

## F. Исправления документации и конфигов

- Добавлен этот audit report.
- Конфиги не менялись: исправление находится в runtime collector boundary и покрывает direct callers, tests и future scripts независимо от `.env`.

## G. Тесты

Добавлены:

- `test_collect_once_skips_ohlcv_when_current_ticker_is_missing`
- `test_collect_once_filters_malformed_symbols_before_market_requests`

Обновлены:

- fake ticker payloads в `tests/test_iteration63.py`
- fake ticker payload в `tests/test_logic.py`

Команды и результаты:

```bash
python -m pytest -q
# 450 passed in 12.27s

PYTHONDONTWRITEBYTECODE=1 python -m compileall -q app main.py
# passed

python -m ruff check app tests main.py
# not run: ruff is not installed in the current sandbox environment
```

## H. Остаточные риски

- Реальные Bybit fee tiers оператора и VIP-level требуют проверки на production account.
- Instrument limits, risk tiers, funding interval и статус инструмента должны проверяться live через Bybit metadata перед execution.
- Slippage model остаётся приближением, не заменяет live execution telemetry.
- Funding history и изменения funding-rate по ходу жизни grid-бота могут отличаться от preflight estimate.
- Точная liquidation price на Bybit зависит от risk tier, margin mode, open orders, wallet balance и live maintenance margin.
- Перед реальными средствами нужен paper/staging прогон и сверка с фактическими fills/fees/funding.

## I. Команды запуска

```bash
# install
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# tests
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q

# lint / quality gate after dev dependencies are installed
ruff check app tests main.py
python -m compileall -q app main.py

# local dev run
python main.py

# alternative FastAPI run
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# production-style run
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
