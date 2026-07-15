# Итерация 253 — быстрый запуск после длительного простоя и ограничение памяти сборщика

## 1. Название итерации

**Bounded restart backfill and collector memory hardening**: отделение срочного восстановления свежих минутных свечей от исторической догрузки и устранение неограниченного удержания результатов параллельных задач.

## 2. Входной ZIP

`bybit-reco-systems-1.0.64-russian-operator-ui(1).zip`

## 3. SHA-256 входного ZIP

`d4506fa0796efcf80f3a03eefe2348a987fc487383d63da346fa94347aa63a91`

Архивная проверка: 313 записей; один корневой каталог; абсолютных путей, `..`-переходов, внешних символьных ссылок, дублирующихся путей, вложенных архивов и обнаруженных секретов нет.

## 4. Исходная версия

`1.0.64` — фактический источник версии в `app/main.py`.

## 5. Новая версия

`1.0.65` — patch-релиз без изменения существующей схемы БД и без несовместимого API-контракта.

## 6. Project fingerprint

Проект соответствует Bybit Recommender:

- FastAPI-приложение в `app/main.py`;
- Bybit Linear USDT и recommendation/audit lifecycle;
- сбор котировок и свечей в `app/collector.py`;
- SQLite/PostgreSQL compatibility layer;
- статический операторский UI в `app/ui/static/`;
- фоновые циклы collector, backfill, futures metadata, sentiment, recommender и опциональный LLM reviewer;
- сервис не является OMS/EMS и не содержит production-маршрутов создания, изменения или отмены частных ордеров Bybit.

## 7. Цель итерации

1. После простоя в несколько дней или недель сделать свежие минутные данные доступными в течение одного ограниченного запроса на инструмент, не ожидая восстановления всего пропуска.
2. Восстанавливать пропущенный диапазон устойчивыми небольшими порциями между последующими циклами и перезапусками.
3. Сделать верхнюю границу памяти сборщика зависимой от настроенного числа workers и размера порции, а не от количества инструментов, длительности простоя и общего числа завершившихся futures.
4. Добавить операторскую диагностику RSS/пикового RSS Python-процесса и состояния восстановления истории.

## 8. Критерии приёмки

1. При разрыве минутной истории больше 360 баров горячий цикл делает один запрос последних 360 свечей и сразу записывает их.
2. Граница старого пропуска сохраняется в БД и переживает перезапуск процесса.
3. Фоновый backfill обрабатывает не более 360 свечей одного задания за цикл и продвигает курсор только после успешной записи.
4. Одновременно в `ThreadPoolExecutor` существует не более `max_workers` futures; результат выдаётся потоково, а не возвращается общим списком.
5. Без явной настройки прогрев не запускает полный sweep всей вселенной; бюджет по умолчанию ограничен восемью инструментами на временной интервал.
6. Health API и UI показывают текущую/пиковую память процесса и счётчики bounded recovery.
7. Все собранные 1147 тестов проходят; JavaScript и Python компилируются.

## 9. Прочитанные источники

- обязательный `Bybit_Recommender_Iteration_Prompt.pdf` внутри входного архива;
- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/AUDIT_REPORT_2026-07-15_postgres_ohlcv_deadlock.md` и последние связанные audit reports;
- `app/collector.py`, `app/main.py`, `app/settings.py`, `app/db.py`, `app/db_backend.py`;
- тесты collector/backfill/runtime locks/PostgreSQL/health API и операторского UI.

## 10. Карта затронутого data flow

До исправления:

`старый last_local_ts` → расчёт всех REST-окон до now → все задачи вселенной сразу submit → каждый future удерживает массив raw kline → общий список результатов удерживает завершившиеся future/result → caller создаёт второй массив нормализованных OHLCV → запись только после окончания группы.

После исправления:

`длинный разрыв 1m` → один явный диапазон последних 360 баров → немедленная нормализация/запись → persisted gap job в `app_config` → backfill берёт один диапазон до 360 баров → commit OHLCV → атомарное продвижение курсора → повтор в следующих циклах.

Параллельный слой:

`iter(tasks)` → submit только `max_workers` задач → `FIRST_COMPLETED` → yield одного результата → submit следующей задачи. Число удерживаемых futures остаётся ограниченным.

## 11. Baseline environment

- Python: `3.13.5`.
- Node: `v22.16.0`.
- Production Python files: 24.
- Test files до итерации: 197.
- Документы до нового отчёта: 75.
- Frontend files: 3.
- Migration SQL files: 2.
- В предоставленном проекте отсутствуют unit-файлы systemd, Docker Compose/Gunicorn-конфигурация и production-журналы Debian OOM killer.

## 12. Baseline commands и точные результаты

| Команда | Результат |
|---|---|
| `python --version` | PASSED: Python 3.13.5 |
| `node --version` | PASSED: v22.16.0 |
| `python -m pip check` | FAILED: внешний конфликт среды — MoviePy 2.2.1 требует Pillow `<12`, установлен Pillow 12.2.0 |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 1143 tests collected |
| `python -m pytest -q` | TIMED OUT после 10 минут примерно на 75%; failure summary отсутствовал, запуск не засчитан как pass |

Baseline выполнялся до изменения production-кода. Реальная Bybit-сеть, production PostgreSQL и Debian VM не использовались.

## 13. Подтверждённые defects/gaps

### MEM-253-01 — HIGH — подтверждённый дефект горячего запуска

При 15-дневном разрыве старый `_kline_fetch_windows()` сформировал **61 последовательное окно Bybit на один минутный ряд одного инструмента**. Свежая свеча не публиковалась до получения и обработки всей последовательности. При десятках инструментов это давало тысячи запросов и продолжительный период, когда тикер уже свежий, а последняя свеча оставалась старой.

### MEM-253-02 — HIGH — подтверждённое неограниченное удержание результатов

Старый `_run_tasks_bounded()` подавал в executor весь список инструментов и возвращал полный `list` результатов только после завершения всех futures. Каждый результат мог содержать много тысяч raw kline rows; затем вызывающий код строил отдельный массив нормализованных словарей. Верхняя граница памяти была пропорциональна `symbols × downtime depth`, а не `workers × bounded page`.

### MEM-253-03 — HIGH — отсутствовал устойчивый bounded gap lifecycle

В проекте не было persisted-задания с `next_start_ts`/`target_end_ts` для отдельной фоновой догрузки длинного минутного пропуска. Поэтому нельзя было сначала вернуть сервис к актуальному состоянию, а затем безопасно восстановить историю малыми транзакциями.

### MEM-253-04 — MEDIUM — опасные значения прогрева по умолчанию

`BACKFILL_FULL_SWEEP_ON_WARMUP=1` и очень большой default budget позволяли backfill одновременно выбирать всю вселенную во время прогрева. Это усиливало запросную, объектную и DB-нагрузку именно в первые минуты после запуска.

### OBS-253-05 — MEDIUM — недостаточная диагностика памяти

Health API не показывал PID, число потоков, VmRSS/VmHWM и максимальный буфер OHLCV. Оператор не мог отличить рост одного процесса от запуска нескольких экземпляров или оценить, уменьшается ли нагрузка после bounded fix.

## 14. Отдельно неподтверждённые claims

- Факт достижения ровно 24 ГБ и конкретная последовательность OOM kill **не воспроизведены**, поскольку архив не содержит `journalctl`, `dmesg`, cgroup/systemd unit, списка процессов, их командных строк и дампов памяти production VM.
- Не подтверждено, что systemd/Gunicorn/Uvicorn действительно запускает несколько workers. Проектный entrypoint `python main.py` запускает один Uvicorn worker, но внешняя конфигурация развёртывания в архиве отсутствует.
- Не установлено, что удалённый Qwen создавал локальную модель или держал её веса: production-код использует сетевой LLM reviewer, а локальный memory spike был найден в collection/backfill path.
- Не измерена абсолютная пиковая RSS на реальной вселенной и двухнедельной истории; regression tests доказывают bounded topology и размер запросов, а не конкретное число мегабайт на Debian.

## 15. План исправления

1. Ввести recent-tail fast path для длинного 1m-разрыва.
2. Сохранять gap job в существующем `app_config`, не добавляя миграцию.
3. Обрабатывать одну bounded page gap job в backfill и продвигать курсор после commit.
4. Преобразовать executor helper в ленивый iterator с не более чем `max_workers` pending futures.
5. Уменьшить безопасные defaults backfill.
6. Экспортировать runtime memory и recovery stats через health API/UI.
7. Зафиксировать single-process deployment requirement и ограничения проверки.
8. Выполнить RED → GREEN и полный post-check.

## 16. Фактический diff по файлам

### Production

- `app/collector.py` — recent-tail window, persisted gap jobs, bounded background page, lazy bounded futures, memory/recovery counters.
- `app/main.py` — bounded backfill wiring, process-memory snapshot, additive health payload, версия 1.0.65.
- `app/settings.py` — безопасные backfill defaults.
- `app/ui/static/app.js` — память Python и восстановление истории в «Здоровье символов».
- `.env.example` — `BACKFILL_FULL_SWEEP_ON_WARMUP=0`, `BACKFILL_PER_TF_BUDGET=8` и пояснения.

### Tests

- добавлен `tests/test_iteration253_bounded_restart_backfill_memory.py` — 4 regression tests;
- скорректирован исторический long-gap regression в `tests/test_iteration66.py` под новый двухэтапный contract;
- синхронизированы статические проверки текущей версии в 16 существующих test files.

### Documentation

- обновлены `README.md`, `CHANGELOG.md`, `docs/KNOWN_RISKS.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- добавлен настоящий audit report.

`data/app.db`, кэши Python/Pytest и локальные test artifacts не являются частью изменения и перед упаковкой восстановлены/удалены.

## 17. Red → green evidence

RED выполнялся на отдельной копии исходного v1.0.64 с добавленным regression test:

```bash
python -m pytest -q tests/test_iteration253_bounded_restart_backfill_memory.py
```

RED: `3 failed in 0.66s`.

Существенные результаты:

- ожидался один recent-tail запрос, фактически получено `61`;
- отсутствовал `_gap_backfill_config_key`;
- `_run_tasks_bounded()` возвращал materialized `list`, а не bounded iterator.

После исправления и добавления release-contract проверки:

```bash
python -m pytest -q tests/test_iteration253_bounded_restart_backfill_memory.py
```

GREEN: `4 passed`.

Дополнительная совместимость с изменённой семантикой long gap:

```bash
python -m pytest -q \
  tests/test_iteration253_bounded_restart_backfill_memory.py \
  tests/test_iteration66.py::test_collect_once_loads_recent_tail_and_schedules_long_gap_without_losing_history
```

GREEN: `5 passed in 0.42s`.

## 18. Database/schema compatibility

- Схема SQLite/PostgreSQL не менялась.
- SQL migrations не добавлялись и не редактировались.
- Gap job хранится в существующем key/value JSON `app_config`.
- OHLCV primary key остаётся `(venue, symbol, tf_sec, ts)`.
- Fresh SQLite init: 20 tables; повторный init: те же 20 tables; PK неизменен.
- Старые БД совместимы: отсутствующий gap key означает отсутствие активного задания.

## 19. API compatibility

- Существующие routes и существующие поля не удалены и не переименованы.
- `/api/v1/health/symbols` получил только additive sections/fields: `runtime`, расширенный `collector`, расширенный `backfill`.
- Торговые статусы, recommendation payload, risk/grid/outcome semantics не менялись.
- UI допускает отсутствие новых полей и показывает прочерк/нулевую диагностику, поэтому старый сохранённый payload не ломает окно.

## 20. Config/env compatibility

- Новых обязательных переменных нет.
- Имена существующих `BACKFILL_FULL_SWEEP_ON_WARMUP` и `BACKFILL_PER_TF_BUDGET` сохранены.
- Изменены только безопасные defaults: full sweep выключен, budget равен 8.
- Явно заданные пользователем env-значения продолжают применяться.
- `per_tf_budget` дополнительно ограничивается фактическим количеством символов.

## 21. Security boundary

- Private Bybit order create/amend/cancel capability не добавлялась.
- LLM остаётся внешним advisory/review transport и не получает локального execution authority.
- Секреты, `.env`, runtime lock DB и test DB не включаются в release ZIP.
- Memory endpoint раскрывает только локальные PID/RSS/HWM/thread count внутри уже существующего health API; пути, env и секреты не публикуются.

## 22. Post-check commands и точные результаты

| Команда | Результат |
|---|---|
| `python -m pytest --collect-only -q` | 1147 tests collected in 1.39s |
| `python -m pytest -q` | **1147 passed in 35.26s** |
| 8 deterministic non-overlapping batches | 144+144+144+143+143+143+143+143 = **1147 passed** |
| новый test отдельно | 4 passed |
| targeted health/backfill/deadlock/API | 12 passed, 38 deselected in 2.34s |
| PostgreSQL offline support/locks/deadlock subset | 17 passed in 0.82s |
| fresh/repeated SQLite init | 20/20 tables, identical; OHLCV PK unchanged |
| `python -m compileall -q app scripts` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pip check` | FAILED только на том же baseline-конфликте MoviePy 2.2.1 / Pillow 12.2.0 |
| `python -m ruff check app tests` | UNAVAILABLE: `No module named ruff` |
| версия в `app/main.py` и static assertions | PASSED: 1.0.65 |
| clean release ZIP inventory | 315 entries, один root, 0 cache/runtime DB/secret-env entries; обязательные PDF/report/test присутствуют |
| повторная распаковка: compileall / JavaScript | PASSED / PASSED |
| повторная распаковка: monolithic `pytest -q` | TIMED OUT внешним 180-секундным лимитом примерно на 69%, без failure summary; не засчитан как pass |
| повторная распаковка: 8 непересекающихся пакетов | **1147/1147 passed** |

## 23. Что не удалось проверить и почему

- Реальный 24-ГБ Debian OOM сценарий: нет доступа к VM, production process list, cgroup limits, `journalctl` и `dmesg`.
- Live Bybit catch-up: внешняя сеть не использовалась; проверка выполнена детерминированными fake clients.
- Disposable PostgreSQL concurrency integration: DSN не предоставлен; выполнены offline dialect/locking/deadlock tests.
- Ruff: модуль отсутствует в среде.
- Исправление внешнего MoviePy/Pillow dependency conflict: конфликт не относится к проектному requirements contract и уже существовал в baseline.
- Один monolithic запуск из повторно распакованного ZIP не завершился за 180 секунд, хотя тот же 1147-node suite ранее прошёл монолитно за 35.26 секунды и затем полностью прошёл восемью независимыми пакетами из ZIP. Failure summary отсутствовал; это классифицировано как непредсказуемая order/process liveness особенность test harness, а не подтверждённое падение production-кода.

## 24. Остаточные риски

1. Внешний supervisor может запускать несколько Python application workers. Runtime locks предотвращают одновременную работу одноимённых циклов, но каждый worker всё равно импортирует Python/ML runtime и создаёт свои supervisor threads. Для этой архитектуры требуется один application process.
2. Bounded recovery снижает пик памяти, но точное production значение зависит от размера вселенной, `COLLECTOR_MAX_WORKERS`, ответов Bybit, драйвера БД и количества внешних процессов.
3. При очень длинном пропуске полное восстановление займёт несколько циклов. Свежий слой доступен сразу, но исторические аналитические окна могут оставаться неполными до завершения gap jobs.
4. Если оператор принудительно вернёт full sweep и большой budget, нагрузка возрастёт; health UI теперь позволяет это увидеть.
5. Изменение не доказывает экономическую эффективность стратегии и не меняет fail-closed торговые ворота.
6. Monolithic pytest демонстрирует нестабильную длительность в зависимости от окружения/порядка; deterministic non-overlapping batches являются подтверждённым полным покрытием текущего набора, но отдельный аудит test-harness liveness остаётся полезным.

## 25. Rollback procedure

1. Остановить сервис.
2. Восстановить ZIP/файлы v1.0.64.
3. Перезапустить с существующей БД; миграционный rollback не нужен.
4. При необходимости удалить только ключи `collector_gap_backfill:*` из `app_config`; v1.0.64 их игнорирует, поэтому удаление необязательно.
5. Учесть, что rollback возвращает исходный риск многократного catch-up, неограниченного materialized future list и OOM при длинном простое.

## 26. Один рекомендуемый следующий work package

Провести контролируемый production-like soak test на отдельной Debian VM: один application process, реальная рабочая вселенная, искусственный 15–30-дневный разрыв, PostgreSQL/Bybit test data source; каждую минуту сохранять PID, RSS/HWM, thread count, число pending gap jobs, скорость восстановления и cgroup metrics. Одновременно зафиксировать фактический systemd unit и запретить запуск более одного Uvicorn/Gunicorn worker. Критерий выпуска: отсутствие монотонного роста RSS, отсутствие OOM kill и публикация свежего 1m-tail в первом hot cycle.
