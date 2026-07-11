# Аудит целостности risk/sizing — v1.0.19

## 1. Название итерации

**Risk sizing integrity: согласование shipped risk profile и запрет автоматического повышения qty.**

## 2. Входной ZIP

`bybit-reco-systems-main(1).zip`

## 3. SHA-256 входного ZIP

`09af5891039ba095af99de7d9e0a6ee19e9c548f766bd34c9be925a8ca1e4a37`

Приложенный протокол: `Bybit_Recommender_Iteration_Prompt.pdf`, SHA-256 `1e2d759151c2df3ea6781ddcb9bead7c467d4b8c59a97546550be77a17415647`.

## 4. Исходная версия

`1.0.18`, source of truth: `version=` при создании FastAPI в `app/main.py`.

## 5. Новая версия

`1.0.19` — patch release без изменения публичного API, схемы БД или набора environment variables.

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender:

- присутствуют `README.md`, `CHANGELOG.md`, оба requirements-файла, `main.py`, `app/main.py`, `app/recommender.py`, `app/trading_semantics.py`, `app/grid_math.py`, `app/risk.py`, `app/calibration.py`, `app/outcomes.py`, `app/db.py`, `app/db_backend.py`, `app/bybit_client.py`, frontend, tests, docs и обе SQL-схемы;
- поддерживаемый `bot_type`: `futures_grid`;
- venue scope: Bybit `category=linear`, USDT perpetual;
- сервис остаётся recommendation/audit-only и не содержит private order create/amend/cancel endpoints;
- SQLite и PostgreSQL остаются поддерживаемыми backend;
- один root directory: `bybit-reco-systems-main`;
- ZIP содержит 228 entries, CRC-проверка прошла; absolute paths, `../`, symlinks, duplicate/conflicting paths и вложенные архивы не обнаружены.

## 7. Цель итерации

После этой итерации система должна сохранять заявленный малый risk profile даже без `.env`, не увеличивать provisional размер заявки из-за выдуманного шага и никогда не повышать qty при live Bybit alignment. Недостижимый `minQty`/`minNotional` должен приводить к fail-closed block, а не к росту позиции.

## 8. Критерии приёмки

1. Built-in и runtime fallback limits совпадают с shipped-профилем: 1 bot, daily DD 10 USDT, cooldown 90 min, max notional 500 USDT, max margin 100 USDT, leverage 3–5x.
2. При BTCUSDT price 100000 и target 25 USDT provisional qty равен `0.00025`, notional остаётся 25 USDT.
3. Recommendation-time sizing не содержит фиктивного `qtyStep`.
4. Live metadata alignment округляет qty только вниз по фактическому `qtyStep`.
5. Недостаточный `minQty`/`minNotional` блокируется последующей strict validation и не исправляется повышением размера.
6. Новый независимый regression test падает на pristine-коде и проходит после фикса.
7. Полный test suite, compileall, Node syntax, SQLite bootstrap/re-init и PostgreSQL dialect/locking tests проходят.
8. Публичные routes, schema и env contract не меняются; release не содержит `.env`, runtime DB, cache или credentials.

## 9. Прочитанные источники

Прочитаны/проверены:

- приложенный адаптированный итерационный протокол;
- `README.md`, `CHANGELOG.md`, `requirements.txt`, `requirements-dev.txt`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- пять последних по дате `docs/AUDIT_REPORT_*.md` и существующий audit prompt;
- `app/trading_semantics.py`, `app/grid_math.py`, `app/risk.py`, `app/recommender.py`, `app/calibration.py`, `app/outcomes.py`, `app/features.py`, `app/direction.py`, `app/regime.py`, `app/collector.py`, `app/bybit_client.py`, `app/db_backend.py`, релевантные части `app/db.py`, `app/main.py`, `app/settings.py`, `app/llm_review.py`, `app/security.py`, frontend и релевантные regression tests;
- операторские DOCX/PDF как текст и PNG/Markdown artifact presence.

## 10. Карта затронутого data flow

`environment / built-in defaults` → `load_settings().risk_limits` → `normalize_risk_limits()` → recommendation risk report / publication gate → execution-time risk status.

`reference price + target_notional` → `_fallback_order_qty_for_linear_grid()` → `params.sizing` / `trade_plan` → live Bybit metadata → `_snap_reco_payload_to_bybit_meta()` → strict plan validation (`qtyStep`, `minQty`, `minNotional`) → operator execute gate.

Затронут только риск/sizing boundary. Directional PnL, funding, grid geometry, publication chain, persistence schema и frontend rendering не изменялись.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `v22.16.0`;
- runtime requirements: FastAPI 0.115.6, uvicorn 0.34.0, httpx 0.27.2, Pydantic 2.10.6, python-dotenv 1.0.1, cryptography 44.0.1, scikit-learn >=1.3.0, tzdata >=2024.1, psycopg 3.2.12;
- dev requirements declare pytest 9.0.2, pytest-cov 7.0.0, ruff 0.15.9;
- `ruff` отсутствовал в фактическом interpreter environment;
- `pip check` обнаружил внешний конфликт MoviePy/Pillow, не относящийся к зависимостям проекта.

Инвентаризация:

- production Python files: 23;
- test files до итерации: 150;
- docs до итерации: 30;
- frontend files: 3;
- migration SQL files: 2;
- max существующий iteration: 206; текущий: 207.

## 12. Baseline commands и результаты

| Команда | Результат |
|---|---|
| `python --version` | PASSED — Python 3.13.5 |
| `node --version` | PASSED — v22.16.0 |
| `python -m pip check` | FAILED (environment) — MoviePy 2.2.1 требует Pillow <12, установлен Pillow 12.2.0 |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE — `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | PASSED — 858 tests collected in 2.85s |
| `python -m pytest -q` | PASSED — 858 passed in 23.88s, exit 0 |

## 13. Подтверждённые defects/gaps

### RISK-207-01 — HIGH — CONFIRMED DEFECT

- **Файлы pristine:** `app/settings.py:222-224`, `app/risk.py:22-33`, `app/risk.py:101-103`.
- **Функции:** `load_settings()`, `_normalize_risk_limits()`, `normalize_risk_limits()`.
- **Вход:** запуск без `RISK_LIMITS_JSON`, либо повреждённый/неполный fallback.
- **Фактическое поведение:** код применял 4 concurrent bots, daily DD 200 USDT, cooldown 30 min, max position notional 5000 USDT, max margin 1000 USDT.
- **Ожидаемое поведение:** shipped profile из README/.env/operator guidance: 1 bot, DD 10, cooldown 90, notional 500, margin 100, leverage 3–5x.
- **Нарушенный инвариант:** fail-closed и согласованность code/config/docs.
- **Финансовое влияние:** допустимый notional и margin были в 10 раз выше, daily DD — в 20 раз выше, concurrent bots — в 4 раза больше; cooldown был втрое короче.
- **Trading/risk влияние:** при отсутствии `.env` сервис мог считать существенно более агрессивный runtime-профиль допустимым.
- **Почему старые тесты не поймали:** часть тестов закрепляла старые цифры как expectation, а часть всегда подставляла `.env.example`/explicit limits.
- **Reproducer:** `python -m pytest -q tests/test_iteration207_risk_sizing_integrity.py::test_builtin_risk_defaults_match_the_shipped_small_account_profile tests/test_iteration207_risk_sizing_integrity.py::test_runtime_risk_fallback_uses_the_same_shipped_profile` на red-copy.
- **Regression test:** два теста в `tests/test_iteration207_risk_sizing_integrity.py`.
- **Fix:** built-in defaults, canonical defaults и fallback normalizers синхронизированы с shipped profile; explicit operator override сохранён.
- **Остаточный риск:** оператор всё ещё может явно задать более высокий профиль; это осознанный config override, а не скрытый default.

### SIZE-207-02 — HIGH — CONFIRMED DEFECT

- **Файл pristine:** `app/recommender.py:1570-1594`.
- **Функция:** `_fallback_order_qty_for_linear_grid()`.
- **Вход:** `price=100000`, `target_notional_usdt=25`.
- **Путь данных:** reference price → provisional sizing → `params.sizing` → estimated grid exposure.
- **Фактическое поведение:** использовался выдуманный fallback step `0.001`; qty повышался до `0.001`, notional становился 100 USDT вместо 25 USDT.
- **Ожидаемое поведение:** до live metadata хранить target-notional estimate `25/100000 = 0.00025` без предположения о step.
- **Нарушенный инвариант:** risky qty never rounds up; отсутствующие instrument filters не должны заменяться фиктивными.
- **Финансовое влияние:** 4-кратное увеличение одной заявки в воспроизводимом BTC-примере; total grid exposure далее умножается на число активных интервалов.
- **Почему старые тесты не поймали:** существующий тест прямо ожидал `0.001` и `100.0`, то есть закреплял ошибочную семантику.
- **Reproducer:** `python -m pytest -q tests/test_iteration207_risk_sizing_integrity.py::test_provisional_sizing_keeps_the_target_notional_without_fake_qty_step_upsize` на red-copy.
- **Regression test:** независимая арифметика `25 / 100000`, без использования production result как oracle.
- **Fix:** provisional qty равен `target/price`; provenance сообщает `provisional_target_notional_until_bybit_preflight`; фактические filters обязательны позже.
- **Остаточный риск:** target 25 USDT может быть неисполняемым для конкретного symbol; теперь это безопасно блокируется на preflight.

### PREFLIGHT-207-03 — HIGH — CONFIRMED DEFECT

- **Файл pristine:** `app/main.py:2168-2178`.
- **Функция:** `_snap_reco_payload_to_bybit_meta()`.
- **Вход:** generated qty `0.00025`, live `qtyStep=0.001`, `minOrderQty=0.001`, `minNotionalValue=5`.
- **Путь данных:** generated sizing → live metadata auto-snap → strict validator.
- **Фактическое поведение:** `raw_qty=max(requested,min_required)` и `mode="up"` повышали qty до `0.001`.
- **Ожидаемое поведение:** align down only; если result ниже minQty/minNotional — blocked/no-trade.
- **Нарушенный инвариант:** safe qty ниже exchange minimum не должен повышаться; fail-closed вместо exposure expansion.
- **Финансовое влияние:** размер мог быть увеличен непосредственно на execution boundary без явного решения оператора.
- **Почему старые тесты не поймали:** legacy tests считали upsize желаемым auto-snap поведением.
- **Reproducer:** `python -m pytest -q tests/test_iteration207_risk_sizing_integrity.py::test_exchange_alignment_never_increases_generated_qty_to_satisfy_minimums` на red-copy.
- **Regression test:** проверяет монотонный contract `snapped_qty <= original_qty` и наличие blocking errors `ORDER_QTY_BELOW_MIN` / `ORDER_NOTIONAL_BELOW_MIN`.
- **Fix:** qty quantization `mode="down"`; min values больше не участвуют в выборе raw qty; strict validator остаётся источником блокировки.
- **Остаточный риск:** внешний executor обязан повторно проверять live balance, actual Bybit filters и фактические ордера.

## 14. Неподтверждённые claims и оценка принципиальной состоятельности

Утверждение «проект по природе несостоятелен» **не подтверждено для заявленной роли recommendation/audit service**. Архитектура имеет воспроизводимые deterministic gates, canonical directional math, strict Bybit preflight, audit lifecycle, dual persistence и значительный regression suite. Найденные дефекты локальны и исправимы без смены архитектурной модели.

Одновременно **не подтверждена экономическая состоятельность как прибыльной live-стратегии**. Наличие score, proxy outcomes, calibration и зелёных unit/integration tests не доказывает положительный live expectancy после slippage, funding, regime drift и ошибок внешнего исполнения. Проект нельзя представлять как доказанный alpha engine или production-ready auto-execution. Это DOCUMENTED LIMITATION, а не исправленный bug.

Не подтверждалось, что найдены все ошибки. Текущая итерация закрывает один приоритетный связный HIGH work package.

## 15. План исправления

1. Зафиксировать independent red tests для shipped limits, target-notional arithmetic и down-only qty contract.
2. Синхронизировать built-in/settings/runtime fallback risk limits.
3. Удалить fake quantity step из recommendation-time sizing.
4. Сделать live qty alignment down-only и оставить minimum enforcement strict validator.
5. Минимально обновить старые tests, которые закрепляли unsafe semantics или неявно зависели от старых defaults.
6. Синхронизировать README, trading logic, known risks, operator infographic source и changelog.
7. Выполнить full post-check и release re-extraction checks.

## 16. Фактический diff по файлам

### Production

- `app/settings.py` — safer built-in `RISK_LIMITS_JSON` profile.
- `app/risk.py` — canonical/default/fallback risk limits синхронизированы.
- `app/recommender.py` — provisional target-notional sizing без fake qty step; operator note синхронизирована.
- `app/main.py` — backward-compatible recognition старого/new provenance; live qty alignment down-only; version 1.0.19.

### Tests

- новый `tests/test_iteration207_risk_sizing_integrity.py` — 4 regression tests;
- `tests/test_grid_linear_economics.py` — старый unsafe upsize expectation заменён независимым target-notional contract;
- `tests/test_iteration127_tick_safe_grid_snapping.py` — fixture sizing сделан валидным сам по себе, чтобы price-rounding tests не зависели от qty upsize;
- `tests/test_iteration89_env_and_docs_integrity.py`, `tests/test_iteration94_risk_limits_and_outcome_bounds.py`, `tests/test_api.py`, `tests/test_logic.py` — expectations синхронизированы с canonical shipped defaults;
- `tests/test_iteration92_json_shape_hardening.py` — unrelated fixture получил explicit permissive limits вместо зависимости от global defaults.

### Documentation

- `README.md`;
- `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- этот audit report.

### Frontend / database / migrations

Не изменялись.

## 17. Red → green evidence

### RED

Команда:

```bash
cd red
python -m pytest -q tests/test_iteration207_risk_sizing_integrity.py
```

Существенные строки:

```text
FAILED ...test_builtin_risk_defaults_match_the_shipped_small_account_profile
Obtained risk defaults: bots=4, DD=200, cooldown=30, notional=5000, margin=1000
FAILED ...test_provisional_sizing_keeps_the_target_notional_without_fake_qty_step_upsize
Obtained: 0.001; Expected: 0.00025
FAILED ...test_exchange_alignment_never_increases_generated_qty_to_satisfy_minimums
assert 0.001 <= 0.00025
FAILED ...test_runtime_risk_fallback_uses_the_same_shipped_profile
4 failed in 0.91s
```

### GREEN

Команда:

```bash
cd working
python -m pytest -q tests/test_iteration207_risk_sizing_integrity.py
```

Существенная строка:

```text
4 passed in 1.04s
```

Повторные deterministic runs: `4 passed in 0.84s`, затем `4 passed in 0.85s`.

## 18. Database/schema compatibility

Schema и migrations не изменялись.

Проверено:

- fresh SQLite bootstrap: PASS, 17 tables;
- повторный `init_db()` на fresh DB: PASS;
- `init_db()` и повторная инициализация на временной копии существующей SQLite DB: PASS;
- `PRAGMA integrity_check`: `ok`;
- PostgreSQL translation/locking/database retry suite: 20 passed.

Live PostgreSQL integration: SKIPPED — безопасный disposable DSN не предоставлен.

Действия пользователя по БД: отсутствуют.

## 19. API compatibility

- public route names и JSON field names не изменялись;
- FastAPI version: 1.0.19;
- 27 route objects, 26 unique paths, 30 method bindings;
- private order create/amend/cancel patterns в production code не обнаружены;
- recommendation/audit-only boundary сохранён.

## 20. Config/env compatibility

- новых environment variables нет;
- существующий explicit `RISK_LIMITS_JSON` override поддерживается;
- изменение касается только безопасного поведения при отсутствующем/неполном config;
- `.env.example` уже описывал shipped small-account profile и не требовал изменения.

Действия пользователя по `.env`: обязательных нет. При наличии явного `RISK_LIMITS_JSON` следует отдельно проверить, что его значения намеренно отличаются от новых defaults.

## 21. Security boundary

- `.env` и реальные credentials не использовались;
- production Bybit private API и order endpoints не добавлялись;
- credential-like assignment scan не выявил секретов в production files;
- release excludes runtime DB/cache/log/build artifacts;
- input ZIP не изменялся.

## 22. Post-check commands и результаты

| Команда | Результат |
|---|---|
| `python -m pytest --collect-only -q` | PASSED — 862 tests collected in 1.05s |
| `python -m pytest -q` | PASSED — 862 passed in 25.15s |
| `python -m pytest -q tests/test_iteration207_risk_sizing_integrity.py` | PASSED — 4 passed; repeated twice |
| relevant risk/sizing suite | PASSED — 41 passed |
| final focused risk/economics suite after operator note sync | PASSED — 17 passed |
| PostgreSQL dialect/locking/DB retry suite | PASSED — 20 passed |
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pip check` | FAILED (pre-existing environment conflict) — MoviePy/Pillow |
| `python -m ruff check .` | UNAVAILABLE — module not installed |
| SQLite fresh/repeat/existing-copy init | PASSED; integrity `ok` |
| private order endpoint scan | PASSED — no matches |
| version/API route inspection | PASSED — 1.0.19; 27/26/30 |
| operator artifacts presence | PASSED — DOCX/PDF/Markdown/PNG present |
| clean ZIP re-extraction | PASSED — one root, fingerprint, no junk, compileall, Node syntax, targeted 4/4 |
| clean-copy full coverage | Monolithic run TIMED OUT at 75% without failure summary; exact collected set of 862 nodes then executed in disjoint deterministic groups `144+144+144+126+10+8+144+142`, all 862 passed |

## 23. Что не удалось проверить и почему

- live PostgreSQL integration — отсутствует проверенный disposable DSN;
- live Bybit private account / actual order preview — выходит за recommendation/audit boundary и не использовались credentials;
- `ruff` — отсутствует в фактическом Python environment, несмотря на pin в `requirements-dev.txt`;
- clean `pip check` — среда содержит внешний MoviePy/Pillow conflict, которого нет в project requirements;
- единый монолитный pytest из повторно распакованного clean ZIP не завершился в harness и был честно заменён exhaustive batched run по точному collected set; функциональных failures в 862 nodes не обнаружено;
- прибыльность/positive live edge — не может быть доказана unit tests, proxy outcomes или статическим аудитом.

## 24. Остаточные риски

1. Стратегический edge остаётся непроверенным на независимом walk-forward и live shadow dataset с фактическими costs/fills.
2. Provisional target 25 USDT часто будет ниже exchange minimum для дорогих contracts; теперь это приводит к safe block, но повышает долю no-trade.
3. Сервис не знает wallet balance, current private position, risk tier, actual grid bot constraints и внешний executor state.
4. Explicit operator override может сделать профиль агрессивнее; UI/audit должны показывать effective limits перед запуском.
5. Большой исторический test suite снижает regression risk, но не исключает скрытые дефекты и тесты, закрепляющие неверную экономическую гипотезу.

## 25. Rollback procedure

1. Остановить service/background workers.
2. Вернуть код/документацию к release v1.0.18 или предыдущему ZIP.
3. Schema rollback не требуется: схема БД не менялась.
4. Если использовался explicit `RISK_LIMITS_JSON`, сохранить его отдельно и сверить после rollback.
5. Повторно запустить compileall, Node syntax и full pytest перед эксплуатацией.

Rollback не рекомендуется для production-like использования, поскольку он возвращает подтверждённое автоматическое повышение qty и агрессивные скрытые defaults.

## 26. Рекомендуемый следующий work package

**Independent economic-validity audit:** построить хронологический walk-forward / purged validation по immutable recommendation snapshots с фактическими bid/ask, taker/maker fees, funding availability, blocked/no-trade selection и conservative fill assumptions; отдельно оценить expectancy, drawdown, calibration reliability и stability по regimes. До такого исследования проект считать безопасным recommendation/audit framework, но не доказанной прибыльной торговой системой.
