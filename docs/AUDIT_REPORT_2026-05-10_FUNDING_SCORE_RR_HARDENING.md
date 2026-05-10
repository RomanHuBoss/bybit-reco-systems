# Audit report — 2026-05-10 — Funding receipt score/RR hardening

## A. Краткое резюме

Повторный аудит проведён по приложенному ТЗ: проект должен оставаться строго grid-only для Bybit Linear USDT Futures / USDT Perpetual, `bot_type=futures_grid`, `venue=linear`, USDT-settled PnL, isolated margin и arithmetic grid.

Основные protective layers уже присутствовали: Bybit instrument metadata validation, exact USDT perpetual symbol scope, tick/qty/minNotional filters, net-profit per grid после fees/spread/slippage/funding, adverse funding spacing floor, liquidation buffer, live-price preflight, risk caps и UI risk report.

Найдена оставшаяся опасная зона: funding receipt уже не попадал в canonical `net_profit_bps`, но всё ещё мог улучшать recommendation `score` и `expected_rr` через signed `net_cost_bps` / `_funding_score_adjustment()`. Это создавало неверное ранжирование: идея могла выглядеть сильнее только из-за текущего funding receipt, хотя funding может сменить знак до накопления inventory.

Исправлено: score/RR теперь используют conservative approval-cost model: `execution_cost_bps + max(expected_funding_bps, 0)`. Signed funding effect оставлен только как diagnostic `signed_net_cost_bps`, а funding receipt больше не даёт положительный score boost.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Recommendation scoring | `net_cost_bps` мог уменьшаться при funding receipt | Кандидат мог получить более высокий score из-за нестабильного funding receipt, а не устойчивой grid-экономики | `net_cost_bps` теперь включает только adverse funding cost; signed carry вынесен в `signed_net_cost_bps` | `app/recommender.py` |
| Expected RR | `_expected_rr()` использовал signed `net_cost_bps`, поэтому received funding повышал RR | UI/API могли ранжировать идею как более привлекательную из-за carry, который может исчезнуть или стать расходом | RR теперь рассчитывается от conservative `net_cost_bps`; funding benefit не повышает RR | `app/recommender.py` |
| Funding score adjustment | `_funding_score_adjustment()` давал положительный boost за receiving funding и funding signal alignment | Funding мог стать псевдо-alpha и повлиять на финальный статус при слабом edge | Adjustment теперь только penalizes paying side; receipt не даёт бонус | `app/recommender.py` |
| UI actions | В `symbolLinksHtml()` был дублирован chart-link | Оператор видел два одинаковых действия вместо чистой пары chart/grid-bot | Оставлен один chart link и один explicit grid-bot creation link | `app/ui/static/app.js` |
| Regression coverage | Не было теста, что funding receipt не повышает score/RR | Повторная регрессия могла вернуть signed funding как approval/ranking edge | Добавлены unit/static regression tests | `tests/test_logic.py`, `tests/test_iteration124_prompt_reaudit.py` |

## C. Исправления торговой логики

- Grid logic: сохраняется только `futures_grid` для Bybit Linear USDT Perpetual; геометрия остаётся `arithmetic`, `grid_count` трактуется как число price intervals.
- PnL: Linear USDT PnL helpers не менялись; long/short расчёт остаётся в USDT.
- Fees: round-trip fee floor остаётся обязательной частью grid economics.
- Funding: canonical `net_profit_bps`, score и expected RR больше не используют funding receipt как edge. Для допуска используется только `max(expected_funding_bps, 0)`; signed funding оставлен диагностикой.
- Leverage/liquidation: existing worst-side/worst-boundary liquidation buffer не менялся; funding score hardening не ослабляет liquidation gates.
- Risk score / recommendation logic: получающий funding short/long больше не получает дополнительный score/RR boost; paying side по-прежнему штрафуется и может блокироваться при extreme funding.
- Recommendation/rejection: система сохраняет возможность сказать `blocked` / `no_trade`; funding benefit не может сделать слабую идею визуально сильнее.

## D. Исправления backend

- `app/recommender.py`:
  - добавлен conservative `funding_cost_bps_for_approval=max(expected_funding_bps, 0)`;
  - `net_cost_bps` теперь равен `execution_cost_bps + funding_cost_bps_for_approval`;
  - добавлен diagnostic `signed_net_cost_bps=execution_cost_bps + expected_funding_bps`;
  - `_funding_score_adjustment()` больше не возвращает положительный boost за funding receipt;
  - `_expected_rr()` документирован и фактически работает от conservative cost basis.

## E. Исправления frontend/UI/UX

- `app/ui/static/app.js`:
  - удалён дублированный link “Открыть график Bybit” в symbol actions;
  - bot action теперь явно называется “Открыть страницу создания grid-бота Bybit”.

UI по-прежнему показывает conservative funding edge labels, signed funding diagnostic и risk report. Исправление не добавляет неподдерживаемых типов ботов и не меняет API выбора стратегии.

## F. Исправления документации и конфигов

- `CHANGELOG.md`: добавлена запись `Funding receipt score/RR hardening`.
- `README.md`: уточнено, что `expected_rr`, score и approval edge не повышаются funding receipt.
- `docs/TRADING_LOGIC.md`: обновлены правила funding для score/RR и diagnostic signed carry.
- `.env.example` и migrations не требовали изменений.

## G. Тесты

Добавлены/обновлены:

- `tests/test_logic.py::test_funding_receipt_does_not_improve_score_cost_or_expected_rr`
- `tests/test_iteration124_prompt_reaudit.py::test_ui_symbol_links_has_single_chart_and_single_grid_bot_link`

Проверки:

```bash
python -m pytest -q tests/test_logic.py tests/test_grid_linear_economics.py tests/test_iteration124_prompt_reaudit.py tests/test_iteration126_funding_interval_and_grid_spacing.py
python -m pytest -q $(ls tests/test_iteration*.py | sed -n '1,35p')
python -m pytest -q $(ls tests/test_iteration*.py | sed -n '36,80p')
python -m pytest -q tests/test_api.py tests/test_sentiment_pipeline.py tests/test_grid_linear_economics.py tests/test_logic.py
python -m py_compile app/*.py tests/*.py main.py
node --check app/ui/static/app.js
```

Результат:

- targeted funding/UI/grid tests: `103 passed`;
- iteration batch 1: `157 passed`;
- iteration batch 2: `105 passed`;
- non-iteration suite: `153 passed`;
- `py_compile`: passed;
- `node --check`: passed.

Примечание: single-process `python -m pytest -q` в этой sandbox-сессии дважды был остановлен внешним timeout после ~69–76% выполнения. Те же 415 уникальных тестов прошли при запуске батчами; это похоже на существующую suite-order/background-state проблему тестового harness, а не на падение добавленных тестов.

## H. Остаточные риски

- Реальные Bybit fees зависят от VIP tier, maker/taker execution и аккаунта.
- Instrument limits, tick/qty/minNotional/funding interval должны сверяться по live Bybit metadata перед созданием бота.
- Funding history и будущие funding flips не гарантируются текущей ставкой.
- Liquidation price остаётся conservative estimate; точная формула зависит от risk tier, mark price, wallet margin и текущей позиции.
- Slippage, partial fills, queue position и live execution требуют paper/live telemetry.
- Production API keys и реальные права аккаунта не проверялись в offline sandbox.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python -m pytest -q
python -m py_compile app/*.py tests/*.py main.py
node --check app/ui/static/app.js
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
