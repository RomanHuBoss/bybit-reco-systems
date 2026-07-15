# Итерация 254 — ограничение памяти калибровки, observability и status-диагностики

## 1. Название итерации

**Bounded calibration evidence and diagnostic reads**: устранение неограниченной материализации крупных `reasons_json`, повторных загрузок одного calibration dataset и client-side buffering PostgreSQL в горячих фоновых/API-контурах.

## 2. Входной ZIP

`bybit-reco-systems-main(3).zip`

## 3. SHA-256 входного ZIP

`93239aeef80999ba57e3f79f73c3e0b46360f0d0d72b52496db6a0c59592668d`

Архивная проверка: 323 записи; один фактический корневой каталог `bybit-reco-systems-main`; абсолютных путей, `..`-переходов, конфликтующих/дублирующихся путей и внешних символьных ссылок нет. Входной архив не изменялся; pristine, red и working copies создавались отдельно.

## 4. Исходная версия

`1.0.65` — фактический source of truth в параметре `version=` FastAPI-приложения в `app/main.py`.

## 5. Новая версия

`1.0.66` — patch-релиз. Схема БД, migrations, публичный API, конфигурационные переменные, policy fingerprint, outcome identity и торговая семантика не изменены.

## 6. Project fingerprint

Проект соответствует Bybit Recommender:

- `README.md`, `CHANGELOG.md`, `requirements*.txt`, `main.py` и `app/main.py` присутствуют;
- основной bot type — `futures_grid`;
- биржевой scope — Bybit `linear`, USDT perpetual;
- сервис остаётся recommendation/audit-only, а не OMS/EMS;
- persistence поддерживает SQLite и PostgreSQL через `app/db_backend.py`;
- canonical directional semantics находятся в `app/trading_semantics.py`;
- frontend находится в `app/ui/static/`;
- private Bybit order create/amend/cancel endpoints и эквивалентные методы отсутствуют.

## 7. Цель итерации

После итерации крупные calibration/observability/liveness/status выборки должны иметь ограниченный размер передаваемого пакета, не удерживать полный диагностический JSON для всей истории и не загружать один и тот же exact-policy dataset независимо для global, bot-specific и direction calibrators в одном рекомендательном цикле.

Это подтверждается RED → GREEN тестами, серверным cursor-контрактом PostgreSQL, потоковым API status aggregation, синтетическим memory benchmark и полным post-check из 1153 test nodes.

## 8. Критерии приёмки

1. `get_policy_outcome_observability()` не вызывает `fetchall()` и читает строки порциями не более 256.
2. Bot-specific observability фильтрует `bot_type` в SQL и не сортирует выборку, предназначенную только для подсчётов.
3. PostgreSQL large-read использует именованный server-side cursor с ограниченным `itersize`, а не обычный client cursor.
4. Calibration reader сохраняет только `feature_snapshot`, `outcome_policy` и `direction_agg`, а не полный `reasons_json`.
5. Один `run_recommender_once()` загружает и policy-фильтрует calibration outcomes не более одного раза для global/bot/direction models и затем освобождает ссылки.
6. Outcome-worker liveness и `/api/v1/status` не материализуют полную outcome/recommendation history.
7. Схема БД, routes/fields API, env contract и fail-closed calibration semantics сохраняются.
8. Все 1153 собранных теста проходят исчерпывающими непересекающимися пакетами; targeted regression проходит отдельно.

## 9. Прочитанные источники

- приложенный `Bybit_Recommender_Iteration_Prompt.pdf` и его project-specific protocol;
- `README.md`, `CHANGELOG.md`, `requirements.txt`, `requirements-dev.txt`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports, включая предыдущую итерацию bounded restart/backfill memory;
- `app/db.py`, `app/db_backend.py`, `app/recommender.py`, `app/main.py`, `app/calibration.py`, `app/outcomes.py`, `app/settings.py`;
- тесты calibration, observability, status API, PostgreSQL compatibility/locking и release integrity.

## 10. Карта затронутого data flow

До исправления:

`run_recommender_once` → global load/refit → полный JOIN outcomes/recommendations до 200 000 строк → decode полного `reasons_json`; затем bot load/refit → повтор той же загрузки; затем direction load/refit → третья загрузка. До каждого решения о свежести calibrator выполнялись отдельные observability scans с `fetchall()` и полным JSON decode.

Отдельно:

`/metrics` или health → `get_outcome_worker_liveness()` → `fetchall()` pending roots;

`/api/v1/status` → полный historical outcome JOIN → Python list всех decoded rows → несколько производных списков и статистик.

После исправления:

`run_recommender_once` → cycle-local `_CalibrationEvidenceContext` → memoized observability по scope → при реальном refit один bounded compact dataset → один exact-policy filter → общие ссылки для global/bot/direction calibrators → `release_rows()`.

Large reads:

SQLite cursor / PostgreSQL named server cursor → `fetchmany(256)` → обработка пакета → следующий пакет → `close()` в `finally`.

Status:

streaming compact lineage rows → однопроходные counters/per-bot stats → API payload без хранения всей истории.

## 11. Baseline environment

- Python: `3.13.5`.
- Node: `v22.16.0`.
- Production Python files: 24.
- Test files до итерации: 198.
- Docs до нового audit report: 76.
- Frontend files: 3.
- Migration SQL files: 2.
- DB backends: SQLite и PostgreSQL/psycopg.
- Максимальный существующий номер regression iteration: 253; текущий номер: 254.
- Реальная 24-ГБ VM, production DB, systemd/cgroup telemetry и disposable PostgreSQL DSN в среде аудита отсутствовали.

## 12. Baseline commands и точные результаты

| Команда | Результат |
|---|---|
| `python --version` | PASSED: Python 3.13.5 |
| `node --version` | PASSED: v22.16.0 |
| `python -m pip check` | FAILED: pre-existing environment conflict — MoviePy 2.2.1 требует Pillow `<12`, установлен Pillow 12.2.0 |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 1147 tests collected |
| `python -m pytest -q` | TIMED OUT внешним 120-секундным лимитом примерно на 37%; failure summary отсутствовал и запуск не засчитан как pass |

Baseline выполнялся до изменения production-кода. Сеть Bybit, production credentials и production database не использовались.

## 13. Подтверждённые defects/gaps

### MEM-254-01 — HIGH — CONFIRMED DEFECT — неограниченная observability materialization

- **Файл исходной версии:** `app/db.py:2980–3093`, критическая операция `cur.fetchall()` на строке 3037.
- **Функция:** `get_policy_outcome_observability()`.
- **Вход:** текущая model version/policy fingerprint; потенциально вся retained recommendation history соответствующей версии.
- **Фактическое поведение:** SQL без `LIMIT` возвращал корневые рекомендации с крупным `reasons_json`; весь result set одновременно материализовался, затем JSON разбирался в Python.
- **Ожидаемое поведение:** bounded streaming с теми же fail-closed проверками и счётчиками.
- **Нарушенный инвариант:** background/API large reads не должны создавать memory footprint, пропорциональный полной таблице.
- **Влияние:** многогигабайтный пик RSS и allocator retention; latency/CPU amplification; торговый gate мог задерживаться, но не становился fail-open.
- **Почему тесты не поймали:** проверяли observability semantics, но не запрещали `fetchall()` и не моделировали batch-only cursor.
- **Regression:** `test_policy_observability_uses_bounded_batches_and_sql_bot_filter`.
- **Исправление:** `execute_stream()` + `fetchmany(256)`, SQL bot filter, удаление ненужного `ORDER BY`, обязательное закрытие cursor.

### MEM-254-02 — HIGH — CONFIRMED DEFECT — повторные полные calibration datasets

- **Файлы исходной версии:** `app/db.py:2853–2913`; `app/recommender.py:3936–3974`, `4011–4237`.
- **Путь данных:** global, bot-specific и direction load/refit paths независимо вызывали `get_outcomes_with_recs(limit=200_000)` и декодировали полный `reasons_json`.
- **Фактическое поведение:** в одном рекомендательном цикле один логический cohort мог загружаться до трёх раз; каждый row удерживал все diagnostic sections, хотя calibration использует ограниченный набор.
- **Ожидаемое поведение:** один cycle-local exact-policy evidence set; compact reasons; явное освобождение после разрешения calibrators.
- **Влияние:** пик памяти умножался на число calibration consumers; CPython allocator мог не вернуть RSS ОС после освобождения.
- **Regression:** `test_calibration_outcome_reader_streams_and_compacts_reasons`, `test_shared_calibration_evidence_loads_policy_rows_once`.
- **Исправление:** `calibration_compact=True`, `_CalibrationEvidenceContext`, lazy one-time load/filter, reuse и `release_rows()`.

### MEM-254-03 — HIGH — CONFIRMED GAP — PostgreSQL `fetchmany()` без server cursor не гарантирует bounded transfer

- **Файл исходной версии:** `app/db_backend.py`, обычный unnamed psycopg cursor.
- **Фактическое поведение:** добавление `fetchmany()` только на consumer level не доказывало, что psycopg не буферизовал весь result set на клиенте.
- **Ожидаемое поведение:** named server-side cursor для large read paths с ограниченным `itersize`.
- **Влияние:** SQLite мог вести себя bounded, а production PostgreSQL сохранял риск полной client-side materialization.
- **Regression:** `test_postgres_large_read_uses_named_server_cursor`.
- **Исправление:** `PostgresConnection.execute_stream()` создаёт уникальный именованный cursor, применяет SQL translation, задаёт `itersize` и возвращает wrapper с `fetchmany()`.

### MEM-254-04 — MEDIUM — CONFIRMED DEFECT — liveness/status diagnostics материализовали историю

- **Файлы исходной версии:** `app/db.py:3180–3257`, `fetchall()` на строке 3208; `app/main.py:6870–6923`.
- **Фактическое поведение:** metrics/health удерживали все pending roots; status API строил полный `historical_rows`, затем несколько derived lists/statistics.
- **Ожидаемое поведение:** bounded scan и one-pass aggregation без row retention.
- **Влияние:** операторский refresh или metrics scrape мог создавать второй крупный memory spike одновременно с recommender refit.
- **Regression:** `test_outcome_worker_liveness_uses_bounded_batches`, `test_lineage_status_mode_aggregates_without_retaining_rows`.
- **Исправление:** streaming liveness; `iter_calibration_lineage_rows()`; `calibration_lineage_diagnostics(retain_rows=False)` и stage statistics.

## 14. Отдельно неподтверждённые claims

- Достижение именно 24 ГБ RSS за 5–10 минут не воспроизведено: нет production DB cardinality/JSON distribution, process timeline, `smaps_rollup`, systemd unit, cgroup metrics и OOM journal.
- Не доказано, что после этого work package отсутствуют другие источники роста памяти. Особенно остаются возможными несколько application workers, сторонний supervisor restart storm, большие DB-driver buffers в иных paths и долгоживущие сторонние native allocations.
- Не измерена production latency полного observability scan. Память ограничена размером batch, но CPU/IO по-прежнему пропорциональны cohort size.
- Live PostgreSQL server cursor не проверялся на disposable сервере из-за отсутствия явно тестового DSN; проверен wrapper/translation contract и существующие offline PostgreSQL tests.

## 15. План исправления

1. Добавить единый backend contract для bounded large reads.
2. На PostgreSQL использовать named server-side cursor, на SQLite — native lazy cursor.
3. Перевести подтверждённые `fetchall()` hot paths на `fetchmany()` с `finally: close()`.
4. Ввести compact calibration reasons contract.
5. Ввести один cycle-local evidence context для calibrators.
6. Перевести status lineage на streaming aggregation без row retention.
7. Сохранить defaults, fail-closed gates, DB schema и API contract.
8. Выполнить RED → GREEN, benchmark, полный suite и clean-release verification.

## 16. Фактический diff по файлам

### Production

- `app/db_backend.py` — `PostgresCursor.fetchmany()`, named `PostgresConnection.execute_stream()`, backend-neutral `execute_stream()`.
- `app/db.py` — batch size/compact keys; bounded calibration reader; compact lineage generator; bounded observability и outcome-worker liveness.
- `app/recommender.py` — streaming lineage mode/stats; `_CalibrationEvidenceContext`; reuse compact policy rows/observability; явное освобождение evidence.
- `app/main.py` — потоковая status lineage aggregation; версия `1.0.66`.

### Tests

- добавлен `tests/test_iteration254_bounded_calibration_memory.py` с 6 regression tests;
- в 16 существующих static release tests синхронизирована ожидаемая версия `1.0.66`; иная семантика этих тестов не менялась.

### Frontend

- изменений нет.

### Database/migrations

- изменений schema/bootstrap/migrations нет.

### Documentation

- обновлены `README.md`, `CHANGELOG.md`, `docs/KNOWN_RISKS.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`;
- добавлен данный audit report.

Runtime `data/*.db`, runtime-lock DB, Python/Pytest caches и локальные benchmark/log artifacts исключаются из release ZIP.

## 17. Red → green evidence

RED выполнялся на отдельной копии исходного v1.0.65 с добавленным только новым regression-файлом:

```bash
python -m pytest -q tests/test_iteration254_bounded_calibration_memory.py
```

RED: **6 failed in 1.33s**.

Существенные строки:

- `AssertionError: hot-path query must not materialize the full result with fetchall()`;
- `TypeError: get_outcomes_with_recs() got an unexpected keyword argument 'calibration_compact'`;
- `AttributeError: module 'app.recommender' has no attribute '_CalibrationEvidenceContext'`;
- `AttributeError: 'PostgresConnection' object has no attribute 'execute_stream'`;
- `TypeError: calibration_lineage_diagnostics() got an unexpected keyword argument 'retain_rows'`.

После production fix:

```bash
python -m pytest -q tests/test_iteration254_bounded_calibration_memory.py
```

GREEN: **6 passed in 0.83s**.

Synthetic topology benchmark, 4 000 rows с неиспользуемым diagnostic block около 25 КБ:

- исходный full reader: `tracemalloc` current/peak **104.85 MB**, max RSS **436.59 MB**;
- новый compact reader: current **8.44 MB**, peak **8.47 MB**, max RSS **327.99 MB**;
- retained Python allocation уменьшилась примерно в **12.4 раза** для этой синтетической формы данных.

Benchmark не является обещанием конкретного production RSS и не заменяет soak test на VM.

## 18. Database/schema compatibility

- SQLite и PostgreSQL schema не менялись.
- `migrations/init.sql` и `migrations/init_postgres.sql` не менялись.
- Fresh SQLite init: 20 tables; повторный `init_db()` — те же 20 tables; `PRAGMA integrity_check = ok`.
- Existing database migration не требуется: добавленных columns/indexes/tables нет.
- PostgreSQL SQL translation сохраняется перед созданием named cursor.
- Server cursor требует существующей transaction scope; штатный psycopg connection использует `autocommit=False`, cursor всегда закрывается consumer-ом.

## 19. API compatibility

- Routes не добавлены, не удалены и не переименованы.
- `/api/v1/status` возвращает прежние поля и значения; изменён только внутренний способ формирования статистики.
- `calibration_lineage_diagnostics()` сохраняет default `retain_rows=True`, поэтому существующие internal callers получают прежние row lists.
- `get_outcomes_with_recs()` по умолчанию сохраняет полный reasons contract; compact mode включается явно только calibration consumers.
- Recommendation statuses, direction/grid/risk/outcome semantics не менялись.

## 20. Config/env compatibility

- Новых обязательных или опциональных env variables нет.
- `.env.example` не менялся.
- Batch size — внутренний bounded constant 256; публичной настройки не добавлено, чтобы не позволить случайно вернуть неограниченную память.
- Пользовательских действий с конфигурацией не требуется.

## 21. Security boundary

- Private order create/amend/cancel endpoints и SDK-equivalent methods не добавлены; статический поиск по `app/` дал `none`.
- LLM, execution preflight, operator lifecycle и recommendation/audit-only boundary не изменены.
- Секреты, `.env`, private keys, runtime DB и runtime lock DB отсутствуют в release inventory.
- Named cursor получает только translated SQL/params и не выводит DSN/credentials.

## 22. Post-check commands и точные результаты

| Проверка | Результат |
|---|---|
| `python -m pytest --collect-only -q` | **1153 tests collected** |
| monolithic `python -m pytest -q` | TIMED OUT внешним 180-секундным лимитом примерно на 37%; failure summary отсутствовал |
| 8 deterministic non-overlapping batches | `144 + 144 + 144 + 144 + 144 + 144 + 144 + 145` = **1153/1153 passed** |
| batch durations | 12.67s, 12.68s, 5.35s, 6.54s, 7.29s, 6.84s, 9.35s, 6.37s |
| новый regression отдельно | **6 passed in 0.83s** |
| calibration/observability/status/API/PostgreSQL targeted selection | **52 passed, 44 deselected in 4.80s** |
| PostgreSQL support/locks/new cursor subset | **21 passed in 1.14s** |
| fresh/repeated SQLite init | 20/20 tables; `integrity_check=ok` |
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pip check` | FAILED только на том же baseline MoviePy/Pillow conflict |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| forbidden private order search | PASSED: none |
| version consistency | PASSED: `app/main.py` и 16 static assertions = 1.0.66 |
| whitespace checks для изменённого production code | PASSED |
| clean release ZIP inventory | 317 entries; один root; 0 cache/runtime DB/secret entries; operator artifacts, новый test и audit report присутствуют |

## 23. Что не удалось проверить и почему

- Реальный 24-ГБ OOM и стабилизацию RSS на production VM: среда/журналы/БД не предоставлены.
- Live PostgreSQL server-side cursor против disposable instance: отсутствует явно тестовый DSN; production DSN использовать запрещено.
- Live Bybit/network smoke: не нужен для данного DB/memory scope и не выполнялся.
- Ruff: package отсутствует в текущей среде.
- `pip check`: общий Python environment имеет pre-existing MoviePy/Pillow conflict вне project requirements scope.
- Один монолитный pytest не завершился в harness limit; полный union test nodes независимо подтверждён восемью непересекающимися пакетами.

## 24. Остаточные риски

1. Observability теперь memory-bounded, но всё ещё сканирует cohort и разбирает необходимые policy JSON. При миллионах rows это может быть CPU/IO heavy; следующий архитектурный шаг — нормализованные индексируемые policy fields/compact evidence table.
2. Calibration refit всё ещё сознательно удерживает до 200 000 **compact** row dictionaries, поскольку model fitting требует dataset. Память теперь ограничена и существенно меньше, но не константна относительно configured limit.
3. Несколько Uvicorn/Gunicorn application workers умножат baseline Python/ML memory и могут выполнять независимые diagnostic reads. Для текущей фоновой архитектуры требуется один application process.
4. Native allocations драйверов/NumPy/scikit-learn не полностью отражаются `tracemalloc`; production RSS soak остаётся обязательным.
5. Исправление не доказывает прибыльность, live edge или production-readiness auto-execution и не изменяет торговые решения.

## 25. Rollback procedure

1. Остановить сервис.
2. Восстановить файлы/ZIP версии 1.0.65.
3. Перезапустить с той же БД и `.env`; rollback schema/config не требуется.
4. Проверить health и recommender startup.
5. Учесть, что rollback возвращает подтверждённые неограниченные `fetchall()` и повторные full calibration loads.

## 26. Один рекомендуемый следующий work package

Выполнить production-like memory soak и затем, только при подтверждённой необходимости, нормализовать calibration policy evidence: вынести `policy_fingerprint`, eligibility, label due time и минимальный feature/direction vector из `reasons_json` в компактную индексируемую структуру. Критерии soak: один application PID, RSS/HWM каждые 15 секунд, отдельные отметки recommender refit/status scrape, размер cohort/JSON, отсутствие монотонного роста после первого refit и отсутствие OOM kill в течение не менее двух часов.
