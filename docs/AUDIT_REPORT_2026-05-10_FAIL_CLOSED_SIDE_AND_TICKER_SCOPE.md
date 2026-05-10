# Audit report — fail-closed liquidation side and exact ticker scope hardening (2026-05-10)

## A. Краткое резюме

Повторный аудит подтвердил, что проект уже ограничен продуктовым контуром `futures_grid` для Bybit Linear USDT perpetual, использует arithmetic grid, проверяет Bybit metadata, tick/qty/minNotional constraints, funding, fees, liquidation buffer и execution-time preflight. Найденные проблемы были не в основной формуле grid, а в fail-closed краевых случаях:

1. helper оценки liquidation price трактовал любой неизвестный `side` как long;
2. collector мог принять symbol-specific ticker payload без поля `symbol`, если тестовый/upstream adapter вернул такую форму;
3. `.env.example` содержал неточный комментарий по `REQUIRE_CONF_GATE`.

Исправления усиливают безопасное поведение: неизвестная сторона теперь не получает liquidation estimate, а ticker без exact echoed symbol не записывается как рыночные данные целевого USDT perpetual.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Liquidation/risk math | `estimate_linear_liq_price()` для неизвестной стороны попадал в long-ветку | Legacy/manual payload с некорректным side мог получить ложный liquidation buffer вместо fail-closed неопределённости | Unknown side теперь возвращает `None`; `liquidation_buffer_pct()` также возвращает `None` для unknown side | `app/grid_math.py`, `tests/test_grid_linear_economics.py` |
| Market data scope | Symbol-specific ticker fallback мог принять payload без `symbol` | Битый adapter/stub мог записать цену/funding как BTCUSDT, хотя upstream не подтвердил exact symbol | `_select_exact_ticker()` теперь требует echoed exact symbol even in per-symbol fallback | `app/collector.py`, `tests/test_iteration119_linear_perpetual_scope.py`, `tests/test_logic.py` |
| Config docs | Комментарий `REQUIRE_CONF_GATE` содержал двусмысленное `(recommended)` | Оператор мог неверно понять, что low-confidence превращается в recommended | Комментарий исправлен: low-confidence становится `no_trade` | `.env.example` |

## C. Исправления торговой логики

- Grid logic: без изменений в базовой arithmetic grid geometry; существующая логика остаётся grid-only и arithmetic-only.
- PnL/fees/funding: без изменения формул; существующий net-profit per grid остаётся net-of-fees/spread/slippage/adverse funding.
- Leverage/liquidation: добавлен fail-closed guard для неизвестного side; система больше не превращает malformed `side` в long liquidation estimate.
- Recommendation/rejection logic: execution preflight продолжает блокировать unsupported venue/bot type/grid type, non-Trading instrument status, missing Bybit filters, stale funding/market data и плохой net edge.

## D. Исправления backend

- `app/grid_math.py`: unknown side в liquidation helpers больше не получает расчёт по long-формуле.
- `app/collector.py`: per-symbol ticker fallback требует exact `symbol` в payload.
- `tests/test_logic.py`: legacy collector stubs обновлены так, чтобы явно возвращать `symbol`, как это должен делать корректный Bybit-like payload.

## E. Исправления frontend/UI/UX

Frontend-код не менялся: текущий UI уже показывает `recommended/not recommended`, confidence, market regime, grid range/count, net profit per grid, funding warning, liquidation buffer, required margin, risk profile, reasons/warnings и Bybit validation panel.

## F. Исправления документации и конфигов

- `.env.example`: уточнён комментарий `REQUIRE_CONF_GATE`.
- `docs/TRADING_LOGIC.md`: добавлено уточнение, что unknown liquidation side не оценивается и считается непроверенным состоянием.
- `CHANGELOG.md`: добавлена запись по этой ревизии.

## G. Тесты

Добавлены/обновлены тесты:

- `tests/test_grid_linear_economics.py::test_liquidation_helpers_fail_closed_on_unknown_side`
- `tests/test_iteration119_linear_perpetual_scope.py::test_collect_once_rejects_symbol_specific_ticker_without_echoed_symbol`
- обновлены collector stubs в `tests/test_logic.py` под строгий exact-symbol contract.

Результат:

```bash
python -m pytest -q
# 413 passed
```

## H. Остаточные риски

Остаются внешние production-риски, которые нельзя полностью закрыть offline-аудитом архива:

- актуальные Bybit fees для конкретного аккаунта/VIP tier;
- live `instruments-info` limits, risk tiers и maintenance margin;
- фактическая ликвидационная цена по аккаунту, mark price и wallet margin;
- live execution, partial fills и slippage;
- funding history/future funding flips;
- доступная маржа и реальные API keys;
- paper/live trading smoke-test перед production.

## I. Команды запуска

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python main.py
```

Для production с PostgreSQL:

```bash
export DB_ENGINE=postgresql
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/bybit_reco
export RUNTIME_LOCK_DATABASE_URL=$DATABASE_URL
export BYBIT_BASE_URL=https://api.bybit.com
export ADMIN_API_KEY=<strong-random-key>
python main.py
```
