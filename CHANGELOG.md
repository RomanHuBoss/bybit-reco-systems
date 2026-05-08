# CHANGELOG

## 2026-05-09 — funding interval and net grid economics audit hardening

### Исправлено
- Funding cost model больше не считает все Bybit Linear USDT perpetual как 8h funding: collector сохраняет `fundingIntervalHour`, DB хранит `funding_interval_min`, recommender считает funding events по фактическому interval.
- Если funding interval отсутствует и ожидаемый funding impact материален, recommendation получает fail-closed блок `FUNDING_INTERVAL_UNCONFIRMED`.
- `grid_leg_economics()` теперь имеет внутренний round-trip taker fee floor, чтобы net profit per grid не мог случайно игнорировать комиссии.
- Удалены остаточные неподдерживаемые strategy/product термины из комментариев и внутренних labels.
- Обновлены README / trading logic / known risks / scenario docs.

### Добавлено
- `docs/AUDIT_REPORT_2026-05-09.md`.
- Regression tests на fee floor, funding interval event count и сохранение `fundingIntervalHour`.

### Тесты
- Targeted regression suite: `10 passed`.
- `python -m py_compile app/*.py main.py` → passed.

## 2026-05-08 — Bybit Linear USDT Futures grid-only economics and risk hardening

### Исправлено
- Добавлен `app/grid_math.py` с Decimal-based расчётами linear PnL, fees, funding cashflow, margin requirement и conservative liquidation buffer.
- Recommender теперь публикует `params.sizing` / `params.economics` и блокирует grid, если net profit per grid после execution friction/funding неположителен или слишком тонкий.
- Execution preflight больше не запрещает любой leverage > 1; вместо этого проверяет Bybit min/max/leverage_step и liquidation buffer.
- Health/warmup контуры больше не считают `linear` дважды.
- UI деталей рекомендации показывает net/gross per grid, estimated margin, order notional, qty/order, liquidation buffer и risk profile.

### Добавлено
- `docs/AUDIT_REPORT_2026-05-08.md`.
- `tests/test_grid_linear_economics.py`.

### Тесты
- segmented `pytest` suite → `353 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `node --check app/ui/static/app.js` → passed.

## 2026-04-24 — live-price execution guard, Bybit status hardening and explicit sizing validation

### Исправлено
- execute-path теперь блокирует operator confirmation, если свежий ticker уже вышел за сохранённый `trade_plan.levels.range` или `kill_switch`; старая grid-рекомендация больше не может быть подтверждена как будто рынок остался в исходном диапазоне;
- Bybit instrument metadata теперь включает `status`, `baseCoin`, `quoteCoin`, `settleCoin`, `unifiedMarginTrade`; `status != Trading` блокирует подтверждение fail-closed;
- добавлена проверка `tp_per_leg` на положительность, схлопывание и выравнивание по `tick_size`;
- добавлен warning при несогласованности `params.grid_levels` и `trade_plan.levels.grid_step.step_abs`;
- удалён лишний дублирующий assignment PostgreSQL `DATABASE_URL` в `settings.py`;
- execution-time validation теперь проверяет явный `order_qty` / `qty_per_leg` / `base_qty` и notional-алиасы из `trade_plan.sizing` или `params` против `qty_step`, `min_order_qty`, `max_order_qty` и `min_notional` Bybit; если размер уже задан и проверен, ложные предупреждения `SIZE_INPUT_REQUIRED` / `MIN_NOTIONAL_NOT_CHECKED` не выводятся.

### Добавлено
- единый helper ценового контекста `trade_plan`, чтобы Bybit validation и live-price guard не расходились в парсинге payload;
- `tests/test_iteration114_live_price_and_status_guards.py`;
- `tests/test_iteration115_order_sizing_validation.py`;
- `docs/AUDIT_REPORT_2026-04-24.md`;
- документация по live-price guard, instrument status guard и остаточным рискам.

### Тесты
- `pytest -q` → `348 passed, 1 warning`;
- targeted docs/integrity + sizing regression → `19 passed, 1 warning`;
- `python -m py_compile main.py app/*.py tests/*.py` → passed;
- smoke import `app.main` → passed.

## 2026-04-23 — startup bootstrap scalability on existing history

### Исправлено
- `db.init_db()` больше не запускает тяжёлый historical publication-lineage backfill на каждом старте. Теперь полный Python backfill выполняется только если в `recommendations` реально найдены legacy-строки без materialized `publication_root_rec_id` / `is_outcome_label_root`;
- bootstrap `bot_instances` больше не пересканирует всю таблицу на каждом рестарте: backfill `publication_root_rec_id` выполняется только если обнаружены пустые legacy-значения;
- глубокий retrofit `repair_async_llm_pending_publication_chains()` больше не вызывается автоматически на старте процесса. Это отдельная maintenance-операция для исторической БД, а не обязательный шаг штатного перезапуска.

### Добавлено
- регрессионные тесты на то, что `init_db()` не делает полный rescanning already-materialized recommendations и bot_instances при обычном рестарте.

### Практический эффект
- повторный `python main.py` на БД с накопленной историей больше не должен зависать из-за безусловного startup-repair старых рекомендаций.

## 2026-04-22 — red-team hardening: exact Bybit instrument match, savepoint-safe idempotency and release-audit repair

### Исправлено
- `BybitPublicClient.get_instrument_info()` теперь принимает metadata только при точном совпадении `symbol`; если upstream/stub вернул список без целевого инструмента, клиент fail-closed возвращает `None`, а не берёт первый попавшийся элемент;
- `_fetch_bybit_instrument_meta()` теперь сохраняет в кэш реальные `symbol/category`, пришедшие от upstream, а не безусловно повторяет запрошенные значения. Это возвращает смысл проверкам `BYBIT_META_SYMBOL_MISMATCH` / `BYBIT_META_CATEGORY_MISMATCH`;
- `db.insert_bot_instance()` и `db.insert_trade()` переведены на `SAVEPOINT`-обёртку вокруг INSERT, чтобы после `IntegrityError` корректно классифицировать дубликаты и не ронять всю внешнюю транзакцию в PostgreSQL aborted-state.

### Добавлено
- `docs/AUDIT_REPORT_2026-04-22.md` с итогами текущего deep audit;
- missing historical audit artifacts `docs/AUDIT_REPORT_2026-04-15.md`, `docs/AUDIT_REPORT_2026-04-10.md`, `docs/AUDIT_REPORT_2026-04-08.md` возвращены в release, чтобы README / changelog / тесты не ссылались на отсутствующие документы;
- регрессионные тесты на exact-symbol instrument metadata, на сохранение фактического upstream symbol в prefetch cache и на savepoint-safe duplicate classification для bot/trade inserts.

### Тесты
- `pytest -q` → `342 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed.

## 2026-04-15 — outcome backlog hardening under LLM mode and audit artifact reconciliation

### Исправлено
- `compute_outcomes_once()` теперь фильтрует LLM-eligible рекомендации в SQL **до** `ORDER BY ... LIMIT`, поэтому oldest-first окно больше не засоряется legacy/root rows без финального `llm_review.status=ok` и outcome-worker снова доходит до реально созревших рекомендаций;
- release-документация приведена к фактическому составу поставки: восстановлены audit-report artifacts, README больше не ссылается на отсутствующий документ, baseline тестов обновлён.

### Добавлено
- `docs/AUDIT_REPORT_2026-04-15.md` с итогами deep audit этой ревизии;
- row-level locking (`FOR UPDATE`) для mutating API-путей в PostgreSQL, чтобы concurrent `execute` / `trade` / `stop` не теряли согласованность состояния;
- выравнивание standalone migration-файлов `init.sql` / `init_postgres.sql` с runtime-bootstrap: добавлены индексы и уникальный инвариант по `publication_root_rec_id` для running-ботов;
- архивные `docs/AUDIT_REPORT_2026-04-10.md` и `docs/AUDIT_REPORT_2026-04-08.md`, чтобы historical changelog / README не ссылались на отсутствующие файлы;
- регрессионные тесты на LLM outcome backlog starvation и на целостность release-doc артефактов.

### Тесты
- `pytest -q` → `322 passed`;
- `pytest --cov=app --cov-report=term-missing -q` → passed;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `ruff check app tests main.py` → passed.

## 2026-04-10 — execution-path lock ordering, stricter Bybit metadata validation and operator UI tables

### Исправлено
- `POST /api/v1/recommendations/{rec_id}/action` больше не захватывает SQLite `BEGIN IMMEDIATE` до execution-time prefetch metadata Bybit: сетевой preflight снова выполняется вне write-lock, как и задумано архитектурой, поэтому медленный upstream не должен блокировать collector/recommender/operator writer-контур;
- execution-time Bybit validation теперь fail-closed блокирует несоответствие `metadata.category` и `recommendation.venue`, а не оставляет это предупреждением;
- модальное окно UI расширено, таблицы внутри модалок получили sticky header + независимую прокрутку тела, а самый широкий журнал исходов переведён в более компактную плотность строк.

### Добавлено
- регрессионный API-тест на порядок `Bybit prefetch -> BEGIN IMMEDIATE` в execution-path;
- регрессионный тест на fail-closed блокировку `BYBIT_META_CATEGORY_MISMATCH`;
- `docs/AUDIT_REPORT_2026-04-10.md` с итогами аудита этой ревизии, списком дефектов и остаточных рисков.

### Тесты
- `pytest -q` → `318 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `ruff check app tests main.py` → passed.

## 2026-04-09 — fail-closed execution validation and upstream shape hardening

### Исправлено
- execution-time Bybit validation теперь блокирует legacy/manual recommendations без явного `margin_mode` вместо молчаливого допуска исполнения в неявном режиме;
- validation теперь fail-closed отклоняет рекомендации, если полученная metadata Bybit относится к другому `symbol`, а не к целевому инструменту recommendation;
- публичный Bybit REST-клиент теперь ретраит не только decode-failures, но и transient `response shape error` сценарии: не-объектный JSON и битый `retCode`.

### Добавлено
- warning `ACCOUNT_MODE_LEGACY_ALIAS` для исторических futures rows с `account_mode=one_way`, чтобы отделить legacy-совместимость от штатной модели `account_mode=unified`;
- регрессионные тесты на блокировку missing-`margin_mode`, symbol-mismatch Bybit metadata и retry битых shape-ответов публичного клиента.

### Тесты
- `pytest -q` → `316 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `ruff check app tests main.py` → passed.

## 2026-04-08 — red-team reliability and operator-signal hygiene

### Исправлено
- execute-path больше не держит SQLite `BEGIN IMMEDIATE` во время внешнего запроса за instrument metadata Bybit: metadata теперь подгружается заранее, вне write-lock, а внутри критической секции используется уже готовый snapshot проверки;
- operator-facing `GET /api/v1/recommendations` теперь по умолчанию скрывает дубли одной `publication_chain`, оставляя в списке один лучший элемент на `publication_root_rec_id`, чтобы repeated `active` updates не выглядели как поток одинаковых идей;
- operator-facing collapse больше не зависит от жёсткого лимита `top_n * 4`: API адаптивно расширяет сырой scan-budget, если одна publication-chain доминирует длинной серией `active` updates и вытесняет другие уникальные идеи из snapshot;
- публичный Bybit REST-клиент теперь ретраит transient transport/protocol ошибки и битые 2xx decode-failures, а также считает HTTP 408 retryable upstream-сценарием.

### Добавлено
- ответ `GET /api/v1/recommendations` дополнен блоком `publication_chain_dedupe` с числом скрытых дублей и возможностью отключить collapse через `collapse_chains=false`;
- регрессионные тесты на отсутствие внешнего Bybit fetch под SQLite write-lock, на adaptive collapse больших duplicate-chain bursts и на retry transient Bybit transport/decode failures.

### Тесты
- добавлен сценарий, который проверяет порядок `Bybit metadata fetch -> BEGIN IMMEDIATE`, чтобы execute-flow не блокировал остальные writer-контуры на сетевых задержках;
- добавлен API-тест на collapse raw-дублей `recommended/active` внутри одной publication-chain;
- добавлены transport-тесты на `RemoteProtocolError` и malformed-JSON retry-path в публичном Bybit-клиенте.

## 2026-04-08 — release consistency and stop-state determinism

### Исправлено
- `.env.example` синхронизирован с фактическими runtime-дефолтами LLM-reviewer (`LLM_REVIEWER_MAX_CANDIDATES=24`, `LLM_REVIEWER_MAX_WORKERS=2`);
- остановка бота теперь использует единый `stopped_ts` для строки `bot_instances` и `state_json`, чтобы audit/state reconciliation был детерминированным.

### Добавлено
- `docs/AUDIT_REPORT_2026-04-08.md` с итогами red-team-аудита;
- API-регрессии на синхронность `stopped_ts` для manual stop и `stop_bot=true` при записи trade.

### Тесты
- расширен регрессионный набор на stop-state timestamp consistency;
- подтверждена согласованность `.env.example` с runtime/default docs.

## 2026-04-07 — audit hardening revision

### Исправлено
- усилена execution-time Bybit validation:
  - добавлена проверка внутренних инвариантов `bot_type ↔ venue ↔ direction`;
  - добавлена проверка `account_mode` / `margin_mode` против фактической модели проекта;
  - добавлена проверка `min_leverage`, `max_leverage`, `leverage_step`;
  - validation теперь явно показывает `snapped` leverage при off-step значении;
- шаблон `.env.example` теперь явно содержит `SYMBOLS_LINEAR`, а не только закомментированный пример.

### Добавлено
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/TRADING_LOGIC.md`
- `docs/SCENARIOS.md`
- `docs/KNOWN_RISKS.md`

### Тесты
- добавлены регрессионные тесты на mode/leverage validation;
- добавлены тесты release-integrity для новых docs/env cross-reference.

## 2026-04-15 — hardening after deep audit
- PostgreSQL mode теперь требует явно заданный `DATABASE_URL`; unsafe-default на localhost удалён.
- Сообщение о старте в PostgreSQL-режиме без установленного `psycopg[binary]` сделано явным и операционно полезным.
- Захват `runtime_locks` в PostgreSQL переведён на atomic UPSERT, чтобы исключить split-brain при конкурентном старте нескольких процессов.
- Для `bot_instances` введён materialized `publication_root_rec_id` и жёсткий инвариант: не более одного `running`-бота на одну publication-chain.
- Bootstrap теперь fail-closed обнаруживает исторически повреждённые БД с дублирующими running-ботами в одной chain.
- Добавлены регрессионные тесты для PostgreSQL bootstrap, runtime-lock safety и publication-chain execution safety.
