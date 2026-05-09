# Финальный аудит и исправление: Bybit Linear USDT Futures Grid Recommender

Дата: 2026-05-09  
Область: только Bybit Linear USDT Futures / USDT Perpetual grid-боты (`futures_grid`, `venue=linear`)  
Результат проверки: `370 passed`, `ruff check` passed, `py_compile` passed, `node --check` passed.

## A. Краткое резюме

Проект уже имел сильную базовую защиту: `SUPPORTED_BOT_TYPES=("futures_grid",)`, сбор `linear` market data, Decimal-математику для grid economics, execution-preflight против Bybit instrument metadata и набор регрессионных тестов. Базовый прогон до правок: `369 passed`.

Опасные места, найденные во время аудита:

1. Некоторые helper-функции linear economics были fail-open: неизвестная сторона позиции могла неявно попасть в ветку long/short-логики.
2. Рекомендатель мог продолжать оценивать Linear USDT perpetual setup без актуального funding rate, то есть показывать net-profit без ключевого futures-компонента.
3. Трендовый рынок и экстремальная волатильность штрафовались, но требовали более явного grid-veto, потому что безопасное поведение системы — не рекомендовать сетку при слабом range-edge.
4. UI показывал много деталей execution/economics, но не имел отдельного нормализованного `risk_report`, который одинаково читается frontend/backend/API-клиентами.
5. В документации/тестах оставались примеры с названиями неподдерживаемых режимов; они заменены нейтральными invalid placeholders.

Итог: проект стал более fail-closed. Теперь рекомендация должна не только иметь положительный net/grid, но и проходить funding/range/volatility/liquidation/exchange-constraint gates. Лучшее допустимое поведение — `not_recommended`/`blocked`, если данных или условий недостаточно.

## Карта проекта

| Зона | Файлы/модули | Ответственность |
|---|---|---|
| Backend entrypoint/API | `main.py`, `app/main.py` | FastAPI, REST API, UI mount, execution preflight, Bybit metadata validation, operator lifecycle |
| Market data / Bybit | `app/bybit_client.py`, `app/collector.py` | Public REST client, retries, ticker/OHLCV/funding/OI collection, exact-symbol instrument metadata |
| Shared product scope | `app/bot_types.py` | Единственный поддерживаемый тип `futures_grid`, фильтры bot_type/direction/venue |
| Grid economics | `app/grid_math.py` | Decimal PnL, fees, funding cashflow, rounding, margin, liquidation estimate, grid leg economics |
| Recommendation engine | `app/recommender.py` | Feature aggregation, grid generation, risk gating, recommendation/rejection logic, risk report |
| Features/regime/stats | `app/features.py`, `app/direction.py`, `app/regime.py`, `app/calibration.py`, `app/outcomes.py` | Индикаторы, direction aggregation, regime, proxy-outcomes, calibration |
| Risk controls | `app/risk.py`, `app/shock_guard.py`, parts of `app/main.py` | Risk limits, shock state, fast-veto, execution-time recheck |
| Persistence | `app/db.py`, `app/db_backend.py`, `migrations/*.sql` | SQLite/Postgres schema, recommendations, decision log, bot instances, trades, runtime locks |
| Frontend | `app/ui/static/index.html`, `app/ui/static/app.js`, `app/ui/static/styles.css` | Operator UI, recommendation cards, execution/risk details, error/loading states |
| Config/env | `.env.example`, `requirements*.txt` | Runtime/dev deps, environment defaults, quality gates |
| Tests | `tests/*.py` | Unit/integration/scenario/regression tests for Bybit, grid, risk, API, DB, UI contracts |
| Docs | `README.md`, `docs/*.md` | Architecture, modules, trading logic, scenarios, known risks, audit history |

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Linear PnL/funding | Неизвестный `side` не блокировался явно в helper-функциях | Silent mispricing: typo мог превратиться в валидную ветку расчёта | Unknown side теперь возвращает `0` и не предполагает long/short | `app/grid_math.py`, `tests/test_grid_linear_economics.py` |
| Funding | Отсутствие актуального funding rate не было hard block для Linear USDT perpetual | UI/API могли показать net profit без funding component | Добавлен `FUNDING_RATE_UNKNOWN` в feasibility blocks | `app/recommender.py` |
| Market regime | Сильный тренд с низким range-score мог быть лишь penalty, а не veto | Grid мог рекомендоваться в режиме, где он накапливает направленную позицию против тренда | Добавлен `MARKET_TOO_TRENDY_FOR_GRID` | `app/recommender.py` |
| Волатильность | Экстремальный ATR не имел отдельного grid-specific rejection | Пробой диапазона/ликвидация/fees/slippage могли доминировать expected edge | Добавлен `VOLATILITY_TOO_HIGH_FOR_GRID` | `app/recommender.py` |
| API/UI risk contract | Риск был размазан по `reasons`, `economics`, `cost_model`, `sizing` | Frontend/оператор могли пропустить ключевую причину отказа | Добавлен нормализованный `params.risk_report` | `app/recommender.py`, `app/ui/static/app.js`, `README.md`, `docs/TRADING_LOGIC.md` |
| Product scope hygiene | В тестах/документации оставались явные примеры неподдерживаемых семейств | Нарушение grid-only product boundary | Заменены на нейтральные `unsupported_venue`/`unsupported_spacing`, README очищен | `tests/test_iteration117_grid_only_strict_preflight.py`, `README.md` |
| Quality gate | Несколько lint-дефектов после аудита и один duplicate-key тестовый fixture | Риск скрытых ошибок и non-deterministic fixture intent | Исправлены imports, unused var, duplicate key | `app/grid_math.py`, `app/recommender.py`, `tests/test_logic.py`, `tests/test_iteration112_redteam_integrity_and_bybit_meta.py` |

## C. Исправления торговой логики

### Grid logic

- Добавлен explicit market-regime veto: если режим `trend`, `trendiness >= 0.80`, а `range_score < 0.35`, рекомендация получает `MARKET_TOO_TRENDY_FOR_GRID`.
- Добавлен explicit high-volatility veto: `ATR >= 10%` блокирует grid-рекомендацию как чрезмерно опасную для диапазонной стратегии.
- Сохранены существующие проверки net/grid, gross edge vs execution cost, grid count/step, tick-size snapping, kill-switch/range boundaries и Bybit execution preflight.

### PnL

- Linear long: `qty * (exit_price - entry_price)`.
- Linear short: `qty * (entry_price - exit_price)`.
- Unknown side теперь fail-closed: helper возвращает `0`, а не молча использует long/short semantics.

### Fees

- Round-trip fees остаются частью grid-leg economics.
- `GRID_NET_PROFIT_NON_POSITIVE`, `GRID_NET_PROFIT_TOO_THIN` и `GRID_GROSS_EDGE_BELOW_COSTS` блокируют рекомендации, где gross/net edge не покрывает издержки.

### Funding

- Для `venue=linear` актуальный funding rate теперь обязателен.
- Если funding rate отсутствует или stale — рекомендация блокируется `FUNDING_RATE_UNKNOWN`.
- Если funding interval не подтверждён и funding material — сохраняется fail-closed `FUNDING_INTERVAL_UNCONFIRMED`.

### Leverage / liquidation

- Существующая логика проверяет leverage относительно Bybit `leverageFilter` и worst-boundary estimated liquidation buffer.
- `params.risk_report.liquidation_buffer_pct` выводится в API/UI.
- Модель ликвидации остаётся conservative approximation, а не точной risk-tier/account-margin моделью Bybit.

### Risk score / recommendation-rejection logic

- `params.risk_report` добавлен как единый риск-контракт:
  - `decision`;
  - `risk_profile`;
  - `expected_net_profit_per_grid_bps/usdt`;
  - execution cost;
  - funding impact / interval;
  - liquidation buffer;
  - capital required;
  - adverse scenario;
  - rejection reasons / warnings / approval factors.

## D. Исправления backend

| Файл | Изменения |
|---|---|
| `app/grid_math.py` | Исправлен порядок module docstring/future import; unknown side в `linear_pnl_usdt()` и `funding_cashflow_usdt()` теперь fail-closed. |
| `app/recommender.py` | Удалён неиспользуемый `futures_neutral`; добавлены `MARKET_TOO_TRENDY_FOR_GRID`, `VOLATILITY_TOO_HIGH_FOR_GRID`, `FUNDING_RATE_UNKNOWN`; добавлен `params.risk_report`. |
| `tests/test_logic.py` | Убран duplicate dict key в LLM-review fixture. |
| `tests/test_iteration112_redteam_integrity_and_bybit_meta.py` | Убран unused import. |

## E. Исправления frontend/UI/UX

| Файл | Изменения |
|---|---|
| `app/ui/static/app.js` | Recommendation detail теперь показывает отдельный блок `Риск-отчёт`: решение, профиль, net/grid, funding impact, execution cost, required capital, liquidation buffer, funding interval, adverse scenario, warnings, rejection reasons и approval factors. |
| `app/ui/static/index.html` | Проверено: selector ограничен `linear`; дополнительных неподдерживаемых bot selectors нет. |

UI теперь явно сообщает, что решение относится только к Bybit Linear USDT Perpetual futures grid, а при `not_recommended`/blocking reasons запуск запрещён до пересчёта.

## F. Исправления документации и конфигов

| Файл | Изменения |
|---|---|
| `README.md` | Обновлён baseline тестов до `370 passed`; добавлено описание `params.risk_report`; удалены остаточные ссылки на неподдерживаемые режимы в пользовательском описании. |
| `docs/TRADING_LOGIC.md` | Добавлены правила `FUNDING_RATE_UNKNOWN` и `params.risk_report`. |
| `CHANGELOG.md` | Добавлена запись аудита 2026-05-09 с итогами и командами проверки. |
| `.env.example` | Проверено: не добавляет неподдерживаемые strategy/bot defaults. |

## G. Тесты

### Добавлено/исправлено

- `tests/test_grid_linear_economics.py`:
  - `test_linear_helpers_fail_closed_on_unknown_side()` — unknown side не даёт ложный PnL/funding.
- `tests/test_iteration117_grid_only_strict_preflight.py`:
  - unsupported payload examples больше не называют запрещённые стратегии; проверка остаётся строгой.
- `tests/test_logic.py`:
  - устранён duplicate-key fixture.

### Команды и результат

```bash
python -m py_compile app/*.py main.py
node --check app/ui/static/app.js
ruff check app tests main.py
python -m pytest -q
```

Результат:

```text
ruff: All checks passed!
pytest: 370 passed
py_compile: passed
node --check: passed
```

## H. Остаточные риски

1. **Real Bybit fees** — фактические maker/taker fees зависят от аккаунта/VIP/промо и должны подтягиваться в production execution layer.
2. **Instrument limits** — live `tickSize`, `qtyStep`, `minOrderQty`, `minNotionalValue`, leverage/risk limits нужно сверять непосредственно перед созданием бота.
3. **Live execution** — проект не OMS/EMS; реальные ордера, fills, cancel/replace, websocket execution reports и reconciliation должны жить во внешнем контуре.
4. **Slippage model** — текущая оценка conservative, но не заменяет order book simulation и реальные fill distributions.
5. **Funding history** — текущий funding rate и interval проверяются, но будущий funding path неизвестен.
6. **Liquidation model** — используется приближение; точная liquidation price зависит от risk tier, mark price, wallet margin, позиции, account mode и биржевого движка.
7. **Paper trading/staging** — перед production нужен staged/paper режим с live instrument metadata, account-specific fees и реальным ingestion fills.
8. **Binary docs** — текстовая документация обновлена; DOCX/PDF operator instruction не пересобирались, так как правки вносились в Markdown/кодовый контур.

## I. Команды запуска

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp .env.example .env
```

### Lint / type-ish syntax / tests

```bash
ruff check app tests main.py
python -m py_compile app/*.py main.py
node --check app/ui/static/app.js
python -m pytest -q
```

### Dev run

```bash
python main.py
```

API/UI по умолчанию: `http://127.0.0.1:8000`.

### Production run baseline

```bash
export APP_ENV=production
export DB_BACKEND=postgres
export DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME'
python main.py
```

Для production также нужен внешний контур execution/reconciliation, который перед запуском grid-бота повторно проверяет live Bybit instrument metadata, account-specific fees, available balance, position mode, margin mode, risk tier, current mark price и фактические order/fill states.
