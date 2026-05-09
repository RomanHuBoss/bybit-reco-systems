# Audit report 2026-05-10 — Conservative funding approval edge

## A. Краткое резюме

Проект уже был существенно зачищен под единственный продуктовый режим: Bybit Linear USDT Perpetual `futures_grid`, FastAPI backend, SQLite/PostgreSQL persistence, простая operator UI в `app/ui/static`, большой набор регрессионных тестов.

Повторный аудит подтвердил, что основные fail-closed слои для Bybit metadata, tick/qty filters, grid count, isolated margin, live-price preflight, risk caps и UI risk-report присутствуют. Критичная найденная проблема была в экономике funding: отрицательный signed funding, то есть ожидаемое получение funding, мог повышать canonical `net_profit_bps` сетки. Это опасно, потому что funding может измениться на следующем событии или стать расходом при накоплении противоположной inventory внутри futures grid.

Исправление: canonical approval edge больше не засчитывает funding receipt. `net_profit_bps` считается только с adverse funding cost, а signed funding оставлен отдельным diagnostic-полем для UI и risk report.

## Карта проекта

| Зона | Файлы |
|---|---|
| Market data / Bybit public API | `app/bybit_client.py`, `app/collector.py` |
| Feature engineering / market regime | `app/features.py`, `app/direction.py`, `app/regime.py`, `app/shock_guard.py`, `app/sentiment_features.py` |
| Grid math / PnL / funding / liquidation approximations | `app/grid_math.py` |
| Recommendation / scoring / rejection logic | `app/recommender.py` |
| Runtime risk caps | `app/risk.py` |
| API / execution preflight / UI payload augmentation | `app/main.py` |
| Persistence | `app/db.py`, `app/db_backend.py`, `migrations/*.sql` |
| UI | `app/ui/static/index.html`, `app/ui/static/app.js`, `app/ui/static/styles.css` |
| Config / docs | `.env.example`, `README.md`, `docs/*.md` |
| Tests | `tests/*.py` |

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Funding / grid economics | `expected_funding_bps < 0` мог увеличивать canonical `net_profit_bps` и делать thin grid выглядящим прибыльным. | Рекомендация могла пройти только из-за текущего funding receipt, хотя после смены ставки или накопления opposite-side inventory edge исчезает. | Canonical `net_profit_bps` теперь считает `funding_cost_bps=max(expected_funding_bps, 0)`. Funding receipt вынесен в `funding_benefit_excluded_bps` и signed diagnostic net. | `app/grid_math.py` |
| Risk report | Risk report не объяснял, что funding receipt не должен быть основой допуска. | Оператор мог принять signed funding benefit за устойчивую прибыль сетки. | Добавлены `funding_cost_bps_for_approval`, `funding_benefit_excluded_bps`, `net_profit_with_signed_funding_bps` и warning при исключённом funding benefit. | `app/recommender.py` |
| UI/UX | UI показывал один `Net/сетка`, без явного разделения conservative net и signed funding diagnostic. | Ложное ощущение, что благоприятный funding является частью устойчивой grid edge. | UI теперь показывает `Net/сетка conservative`, `Net signed funding`, `Funding cost для допуска`, `Funding benefit исключён`. | `app/ui/static/app.js` |
| Tests | Не было regression-теста на funding receipt windfall. | Будущая правка могла снова начать засчитывать funding receipt в approval-edge. | Добавлен unit regression на отрицательный funding и статический UI regression. | `tests/test_grid_linear_economics.py`, `tests/test_iteration124_prompt_reaudit.py` |

## C. Исправления торговой логики

### Grid logic

Генерация остаётся arithmetic-only. `grid_count` трактуется как Bybit Number of Grids / число price intervals. Geometric grid остаётся заблокированным fail-closed до отдельной реализации ratio geometry, net-profit и tick rounding.

### PnL

Формулы linear PnL не менялись: long `qty * (exit - entry)`, short `qty * (entry - exit)`, расчёт в USDT. Unknown side остаётся fail-closed.

### Fees / spread / slippage

`grid_leg_economics()` по-прежнему использует execution cost floor: минимум double taker fee, даже если caller передал нулевой/слишком низкий execution cost.

### Funding

Изменён canonical funding treatment:

- signed `expected_funding_bps` сохраняется как market diagnostic;
- `funding_cost_bps=max(expected_funding_bps, 0)` используется в canonical `net_profit_bps`;
- negative funding receipt не улучшает approval-edge;
- добавлены `funding_benefit_excluded_bps`, `net_profit_with_signed_funding_bps`, USDT-аналоги.

### Leverage / liquidation

Логика leverage и approximate liquidation buffer не менялась: risk gate использует worst boundary / kill-switch buffer, а exact liquidation зависит от risk tier, mark price и account margin.

### Risk score / recommendation-rejection

Existing rejection gates сохраняются: insufficient MTF history, strong trend, high ATR, spread, funding unknown/extreme, thin grid net, liquidation buffer, runtime leverage/notional/margin caps, Bybit preflight constraints. Новая funding-правка делает `GRID_NET_PROFIT_*` консервативнее, потому что receipt больше не может спасти fee-dominated grid.

## D. Исправления backend

| Файл | Изменение |
|---|---|
| `app/grid_math.py` | Переписана funding часть `grid_leg_economics()`: separate signed funding, adverse cost for approval, excluded benefit diagnostics. |
| `app/recommender.py` | Risk report расширен новыми funding fields и предупреждением, если receipt исключён из approval edge. |

## E. Исправления frontend/UI/UX

| Файл | Изменение |
|---|---|
| `app/ui/static/app.js` | В блоке исполнения и risk-report добавлены conservative/signed net labels и funding-cost/benefit labels. |

## F. Исправления документации и конфигов

| Файл | Изменение |
|---|---|
| `README.md` | Документирован запрет засчитывать funding receipt как основание для approval edge. |
| `docs/TRADING_LOGIC.md` | Описаны `funding_cost_bps`, `funding_benefit_excluded_bps`, `net_profit_with_signed_funding_bps`. |
| `CHANGELOG.md` | Добавлен changelog entry по funding approval edge. |

Конфиги и migrations не менялись.

## G. Тесты

Добавлены/изменены:

- `tests/test_grid_linear_economics.py::test_grid_leg_economics_does_not_approve_from_funding_receipt_windfall`
- `tests/test_iteration124_prompt_reaudit.py::test_ui_exposes_conservative_funding_edge_labels`

Результат:

```bash
python -m pytest -q
# 399 passed in 8.84s

python -m py_compile app/*.py main.py
# ok

node --check app/ui/static/app.js
# ok
```

## H. Остаточные риски

Остались внешние и production-зависимые риски:

- реальные Bybit fee tiers maker/taker должны подставляться из актуального аккаунта;
- live `instruments-info` limits могут меняться;
- точный liquidation engine зависит от risk tier, mark price, wallet balance, maintenance margin и режима аккаунта;
- slippage model остаётся оценочным;
- funding history и будущие funding flips не гарантируются текущей ставкой;
- production API keys, Bybit permissions, hedge/one-way mode и paper/live execution требуют отдельного end-to-end прогона;
- backtest/proxy outcomes не гарантируют будущую доходность.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest -q
python -m py_compile app/*.py main.py
node --check app/ui/static/app.js

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Для production перед запуском заполнить `.env` / environment variables, проверить DB target, Bybit base URL, risk limits и отключить любые legacy payloads, которые не проходят operator guard.
