# Audit report — Bybit Linear USDT Futures Grid strategy correction

Date: 2026-05-10
Scope: recommender strategy, risk/economics model, API payload diagnostics, tests and documentation.

## A. Краткое резюме

Проект уже был в основном ограничен единственным поддерживаемым продуктом `futures_grid` для Bybit Linear USDT Futures. Дополнительный аудит выявил три торгово-критичных места, где рекомендация могла быть слишком оптимистичной или неоднозначной для оператора:

1. `grid_spacing_pct` публиковался как экономический минимум шага, хотя фактическая Bybit arithmetic grid исполняется как `(upper - lower) / grid_count`.
2. Если `funding_rate` и funding interval известны, но `next_funding_ts` отсутствует, модель могла недооценить carry-risk на коротком горизонте.
3. Рынки с недостаточным range-edge и уже заметной trendiness могли пройти без отдельного fail-closed veto, если не срабатывал более жёсткий trend-veto.

После исправлений стратегия стала более консервативной: публикуемый шаг сетки теперь соответствует исполнимой геометрии диапазона, funding при неизвестном следующем времени считается с защитным предположением, а слабый range setup блокируется до публикации action-ready рекомендации.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Grid geometry | `grid_spacing_pct` мог отражать не исполнимый Bybit arithmetic step, а минимальный экономический floor | UI/operator payload и экономика шага могли расходиться с реальным `(upper-lower)/grid_count` | `grid_spacing_pct` теперь публикует фактический arithmetic step; economic floor вынесен в `economic_min_grid_spacing_pct`; добавлены `actual_grid_step_abs`, `actual_grid_spacing_pct`, `grid_geometry_model` | `app/recommender.py`, `tests/test_grid_linear_economics.py`, `README.md`, `docs/TRADING_LOGIC.md` |
| Funding | При известном `funding_rate`/interval, но неизвестном `next_funding_ts`, модель могла считать 0 funding events на horizon < interval | Завышение net edge и допуск сетки при неизвестном ближайшем funding boundary | Funding events считаются консервативно через `ceil(horizon/interval)` с минимумом 1; добавлен `funding_event_schedule_assumption` | `app/recommender.py`, `tests/test_logic.py`, `docs/TRADING_LOGIC.md`, `docs/KNOWN_RISKS.md` |
| Recommendation gating | Слабый range-edge при уже повышенной trendiness не имел отдельного veto | Grid мог публиковаться в смешанном/трендовом режиме без достаточного mean-reversion edge | Добавлен fail-closed block `RANGE_EDGE_TOO_WEAK_FOR_GRID` | `app/recommender.py` |
| Docs/tests | Документация не фиксировала различие между economic floor и executable grid step | Оператор мог неверно интерпретировать `grid_spacing_pct` | Обновлены README, trading logic docs, known risks и changelog; тесты закрепляют новую инварианту | `README.md`, `docs/TRADING_LOGIC.md`, `docs/KNOWN_RISKS.md`, `CHANGELOG.md`, tests |

## C. Исправления торговой логики

### Grid logic

- Arithmetic futures grid приведён к явной модели: `grid_count` — число интервалов, а исполнимый шаг — `(price_range_upper - price_range_lower) / grid_count`.
- `grid_spacing_pct` теперь соответствует этому исполнимому шагу, а прежний минимальный safe edge хранится отдельно в `economic_min_grid_spacing_pct`.
- Если defensive clamps могли сделать фактический шаг тоньше экономического минимума, диапазон расширяется, а не публикуется переуплотнённая сетка.

### PnL / fees / net profit

- Существующая логика net-cost floor сохранена: execution cost, spread/slippage и adverse funding не дают сетке считаться прибыльной только по gross step.
- Исправление затрагивает именно входной executable step, который далее используется в trade plan и TP hint.

### Funding

- Для `linear` при известном `funding_rate`, но неизвестном `next_funding_ts`, модель больше не предполагает нулевой carry.
- Добавлен диагностический признак `funding_event_schedule_assumption`: `bybit_next_funding_ts`, `conservative_unknown_next_funding_ts` или `not_applicable`.

### Leverage / liquidation / margin

- Существующая fail-closed логика по leverage, liquidation buffer и Bybit filters сохранена.
- Новые изменения не ослабляют liquidation guard; они снижают вероятность публикации слишком плотной или funding-недооценённой сетки.

### Risk score / recommendation-rejection

- Добавлен отдельный блок отказа `RANGE_EDGE_TOO_WEAK_FOR_GRID` при слабом range score и повышенной trendiness.
- Поведение системы ближе к целевому: лучше вернуть `no_trade/blocked`, чем публиковать grid без достаточного диапазонного edge.

## D. Исправления backend

- `app/recommender.py`
  - Исправлена модель funding events при неизвестном `next_funding_ts`.
  - Разделены economic spacing floor и фактический arithmetic grid step.
  - Добавлены диагностические поля в `params` и `cost_model`.
  - Добавлен fail-closed veto для слабого range edge.

## E. Исправления frontend/UI/UX

Изменений в JS/CSS/HTML не потребовалось: текущий UI уже получает `params`, `cost_model`, `risk_report`, `feasibility_blocks` и отображает обновлённые поля через существующие payload-секции. Live UI не содержит выбора неподдерживаемых типов ботов; поддерживаемый `bot_type` в коде остаётся только `futures_grid`.

## F. Исправления документации и конфигов

- `README.md` — уточнена семантика `grid_count`, `grid_spacing_pct`, `economic_min_grid_spacing_pct` и conservative funding assumption.
- `docs/TRADING_LOGIC.md` — описана новая arithmetic geometry и funding fallback.
- `docs/KNOWN_RISKS.md` — добавлен остаточный риск по неизвестному `next_funding_ts` и способ его fail-closed обработки.
- `CHANGELOG.md` — добавлена запись о strategy grid geometry / funding schedule hardening и очищены дублирующиеся заголовки changelog.

## G. Тесты

Добавлены/изменены регрессии:

- `tests/test_grid_linear_economics.py::test_grid_count_is_used_as_interval_count_for_range_span`
  - проверяет, что published `grid_spacing_pct` равен фактическому arithmetic step `(upper-lower)/grid_count`, а не только economic floor.
- `tests/test_logic.py::test_estimate_cost_model_rolls_stale_funding_forward_and_counts_crossed_events`
  - проверяет conservative funding event count при отсутствующем `next_funding_ts`.

Проверки:

```bash
python -m pytest tests/test_grid_linear_economics.py::test_grid_count_is_used_as_interval_count_for_range_span tests/test_logic.py::test_estimate_cost_model_rolls_stale_funding_forward_and_counts_crossed_events -q
# 2 passed

python -m pytest -q
# 428 passed

python -m compileall -q app
# passed
```

## H. Остаточные риски

Требуют отдельной live/paper проверки:

- реальные account-level maker/taker fees на конкретном Bybit аккаунте;
- актуальные Bybit instrument limits, risk tiers и price-limit risk parameters;
- live execution, partial fills, отмены, очередность лимитных ордеров;
- slippage model на реальном стакане;
- funding history и intra-day funding spikes;
- production API keys, rate limits и permission scope;
- paper trading / dry-run перед любым реальным запуском.

## I. Команды запуска

```bash
# install runtime
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# install dev/test tools
pip install -r requirements.txt -r requirements-dev.txt

# tests
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q

# lint
ruff check app tests main.py

# type/syntax sanity
python -m compileall -q app

# dev / local run
python main.py

# production-style run
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
