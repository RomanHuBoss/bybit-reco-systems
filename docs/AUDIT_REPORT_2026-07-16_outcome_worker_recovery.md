# Итерация 255 — восстановление outcome worker и индексируемая observability

## 1. Название итерации

**Outcome worker recovery, bounded queue processing and materialized liveness — v1.0.67.**

## 2. Входной ZIP

`bybit-reco-systems-main(1).zip`

## 3. SHA-256 входного ZIP

`f1855f4f50a067418ac691586de7e8188343d72d760c323488d0cd3e13cc6878`

## 4. Исходная версия

`1.0.66`, source of truth: `version=` при создании FastAPI в `app/main.py`.

## 5. Новая версия

`1.0.67` (patch, обратно совместимое исправление фонового контура и additive schema extension).

## 6. Project fingerprint

Проверка пройдена. Root содержит обязательные README/CHANGELOG, `main.py`, FastAPI-приложение и модули recommender/trading/risk/calibration/outcomes/DB/Bybit client, frontend `app/ui/static/`, тесты, документацию и два reference SQL. Runtime scope подтверждён как Bybit V5 `category=linear`, USDT perpetual, `futures_grid`, recommendation/audit-only, SQLite + PostgreSQL. Статический поиск private order create/amend/cancel endpoints — 0 совпадений.

Архив: 325 entries; absolute paths, `../` traversal, внешние symlinks, duplicate/conflicting paths и подозрительные вложенные архивы не обнаружены. Созданы независимые pristine/red/working copies; входной ZIP не изменялся.

## 7. Цель итерации

После этой итерации созревшие proxy-outcomes должны обрабатываться независимо от цикла публикации, оставшийся backlog не должен ошибочно обозначаться как остановка при доказанном прогрессе, health/status не должен разбирать тысячи больших `reasons_json`, а необозначенные ветви сеточной реконструкции должны завершаться fail-closed с явной машинной причиной.

## 8. Критерии приёмки

1. В lifespan присутствует отдельный supervised outcomes worker с собственной атомарной runtime lock.
2. Recommender publication loop не вызывает outcome labeling.
3. Состояния очереди различают `processing`, `backlog`, `stalled`, `error`, `ok` по durable heartbeat/progress.
4. Один цикл сохраняет измеримые progress counters и имеет жёсткий предел не более 2000 просматриваемых roots.
5. Liveness eligibility агрегируется по индексируемым колонкам без чтения `reasons_json` в Python.
6. Existing SQLite database обновляется additive/idempotent runtime migration и bounded keyset backfill.
7. Неоднозначный grid outcome возвращает явный diagnostic code и остаётся censored/fail-closed.
8. Новый regression suite красный на pristine и зелёный после исправления; вся коллекция проходит исчерпывающим пакетным запуском.

## 9. Прочитанные источники

Прочитаны/проверены: `README.md`, `CHANGELOG.md`, `requirements*.txt`, `.env.example`, `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`, последние audit reports, `app/main.py`, `app/outcomes.py`, `app/db.py`, `app/db_backend.py`, `app/recommender.py`, `app/calibration.py`, `app/settings.py`, `app/llm_review.py`, frontend, migrations и релевантные regression tests. Дополнительно проанализирован предоставленный эксплуатационный лог: 4775 matured/unattempted roots, oldest due age 2 222 305 sec, `WORKER STALLED`, repeated invalid grid outcomes.

## 10. Карта затронутого data flow

`recommendation persistence` → materialized outcome-policy fields → indexed maturity/eligibility query → dedicated outcomes worker → bounded proxy-outcome reconstruction → `reco_outcomes` / `reco_outcome_observability` → durable cycle snapshot in `app_config` → `/api/v1/status` and `/metrics`.

Publication path теперь идёт независимо: collector/features/regime/recommender/risk/publication не ждут outcome labeling или полной диагностики его очереди.

## 11. Baseline environment

- Python: `3.13.5`
- Node: `v22.16.0`
- Runtime dependencies: FastAPI 0.115.6, Uvicorn 0.34.0, httpx 0.27.2, Pydantic 2.10.6, cryptography 44.0.1, scikit-learn >=1.3, psycopg 3.2.12 и др. согласно `requirements.txt`.
- Dev pins: pytest 9.0.2, pytest-cov 7.0.0, ruff 0.15.9.
- Original inventory: 26 Python files under `app/` + `scripts/`, 199 test files, 77 docs, 3 frontend files, 2 migration SQL.
- DB backends: SQLite and PostgreSQL/psycopg compatibility layer.

## 12. Baseline commands и точные результаты

| Проверка | Результат |
|---|---|
| `python --version` | PASSED — Python 3.13.5 |
| `node --version` | PASSED — v22.16.0 |
| `python -m pip check` | FAILED, pre-existing environment conflict: MoviePy 2.2.1 requires Pillow `<12`, installed 12.2.0 |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE — module `ruff` absent in active environment |
| `node --check app/ui/static/app.js` | PASSED |
| monolithic `python -m pytest -q` | TIMED OUT after 20 minutes at 49%; no failure had been printed |
| `python -m pytest --collect-only -q` | PASSED — 1153 collected |
| exhaustive deterministic batches | PASSED — 1153/1153 nodes, 47 disjoint batches, 223.82 s |

## 13. Подтверждённые defects/gaps

### OW-255-01 — HIGH — CONFIRMED DEFECT — outcome processing coupled to publication

- Файлы/функции: исходные `app/main.py::_reco_thread`, `app/outcomes.py::compute_outcomes_once`.
- Фактическое поведение: outcome labeling и дорогая liveness-проверка выполнялись внутри recommender loop; долгий backlog блокировал следующий publication cycle и heartbeat этой роли.
- Ожидаемое: независимый supervised worker с собственной lock/restart/heartbeat границей.
- Нарушенные инварианты: background runtime, liveness, отсутствие duplicate workers.
- Влияние: публикация может задерживаться; калибровка не получает labels; оператор видит устойчивый `calibrator fitted=false`.
- Почему тесты не поймали: проверяли bounded reads, но не владение задачей отдельным runtime contour.
- Reproducer: `python -m pytest -q tests/test_iteration255_outcome_worker_recovery.py::test_outcome_processing_has_its_own_supervised_thread_and_lock`.
- RED: assertion — `_start_background_thread("outcomes"` отсутствовал.
- Fix: отдельные `_outcome_thread()` и `_run_outcome_cycle_once()`, lock `runtime:outcomes`, supervisor registration; вызов из `_reco_thread` удалён.
- Остаточный риск: фактическая скорость на production PostgreSQL зависит от DB I/O и доли сложных grid roots.

### OW-255-02 — HIGH — CONFIRMED DEFECT — дорогая и ложноположительная liveness

- Файл/функция: `app/db.py::get_outcome_worker_liveness`.
- Вход: большой matured backlog; в предоставленном логе — 4775 roots, все unattempted.
- Фактическое поведение: eligibility определялась через Python-разбор `reasons_json`; правило `unattempted == matured` объявляло `stalled` даже после успешной обработки предыдущего пакета. Разница между `checked ts` и записью события в логе — 911 sec, что согласуется с дорогим диагностическим проходом, хотя production query profile не снимался.
- Ожидаемое: индексируемая SQL-агрегация и state по freshness/progress рабочего цикла.
- Влияние: 15-минутный блокирующий участок, ложный аварийный статус, риск истечения role lock.
- RED: `TypeError` на отсутствующий `worker_stale_after_sec`; backlog test не мог выразить новый контракт.
- Fix: materialized eligibility/LLM columns, два индекса, SQL aggregate, max 10 sample IDs, states `processing/backlog/stalled/error/ok`.
- Остаточный риск: до завершения initial legacy backfill NULL-строки обновляются пакетами при startup.

### OW-255-03 — MEDIUM — CONFIRMED GAP — отсутствие durable progress evidence

- Файл: `app/main.py`.
- Фактическое поведение: не сохранялись start/finish/duration, selected/examined/labeled/waiting/censored/failed, backlog before/after и last rec_id.
- Ожидаемое: оператор и health logic различают медленный прогресс и остановку.
- RED: `AttributeError: module 'app.main' has no attribute '_run_outcome_cycle_once'`.
- Fix: snapshot `outcome_worker_cycle` в `app_config`, running/completed/error states, промежуточный progress callback примерно раз в 5 sec.

### OW-255-04 — MEDIUM — CONFIRMED DEFECT — немаркированные grid-outcome exits

- Файл: `app/outcomes.py`, сеточная реконструкция.
- Фактическое поведение: ряд `return None` не заполнял diagnostic reason; caller записывал generic unknown failure.
- Ожидаемое: каждая непроверяемая ветвь имеет стабильный machine-readable code и остаётся censored.
- RED: `KeyError: 'reason'` в explicit ambiguity regression.
- Fix: добавлены отдельные reasons для конфликтующих pending/active orders, gap/kill-switch/path ambiguity, ledger/notional inconsistencies и fallback `grid_outcome_unavailable_without_diagnostic`.
- Торговая семантика не ослаблена; неизвестное не превращается в успешный fill.

### OW-255-05 — MEDIUM — CONFIRMED GAP — policy eligibility только внутри JSON

- Файлы: `app/db.py`, `migrations/init.sql`, `migrations/init_postgres.sql`.
- Фактическое поведение: `outcome_eligible`, policy eligibility, sample role, risk pass/blocks и LLM status нельзя было эффективно фильтровать SQL.
- RED: schema subset assertion failed; required columns отсутствовали.
- Fix: шесть nullable materialized columns, strict literal-boolean extraction, sync при LLM review, idempotent bounded backfill, SQLite/PostgreSQL reference schema parity.

### OW-255-06 — MEDIUM — CONFIRMED GAP — неограниченное время одного catch-up pass

- Файл: `app/outcomes.py::compute_outcomes_cycle`.
- Фактическое поведение: memory batching не гарантировал верхнюю границу просматриваемых roots.
- Fix: hard scan cap 2000 roots/cycle; при малом process limit сохраняется rotation window `min(2000, limit*12)`, чтобы waiting rows не блокировали зрелые rows дальше очереди. Ускоренный повтор разрешён только после терминального прогресса.

## 14. Неподтверждённые claims

- Не доказано, что вся 911-секундная задержка production-лога была вызвана только `get_outcome_worker_liveness`; это сильная причинная гипотеза по call path и прежней реализации, но production DB profiling отсутствует.
- Не доказана прибыльность или live edge; proxy-outcomes и calibration остаются исследовательским evidence layer.
- Не выполнялась live Bybit network проверка и не использовались private credentials.
- Live PostgreSQL integration SKIPPED: безопасный disposable DSN не предоставлен. Выполнены offline dialect/translation/schema tests.

## 15. План исправления

1. Отделить outcome role и lock от recommender.
2. Добавить durable cycle snapshot и progress callback.
3. Материализовать SQL-filterable policy fields с additive migration/backfill.
4. Переписать liveness на SQL aggregate и progress-state machine.
5. Ограничить scan/catch-up работу одного цикла.
6. Сделать сеточные censorship reasons явными.
7. Синхронизировать tests/version/docs и проверить оба DB contracts.

## 16. Фактический diff по файлам

### Production
- `app/main.py`
- `app/outcomes.py`
- `app/db.py`

### Database/migrations
- `migrations/init.sql`
- `migrations/init_postgres.sql`

### Tests
- новый `tests/test_iteration255_outcome_worker_recovery.py` — 11 tests;
- `tests/test_iteration254_bounded_calibration_memory.py` — liveness expectation синхронизирован с SQL aggregate contract;
- iteration 213–226, 238, 240 static version assertions синхронизированы с v1.0.67.

### Docs
- `README.md`
- `CHANGELOG.md`
- `docs/ARCHITECTURE.md`
- `docs/KNOWN_RISKS.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- `docs/TRADING_LOGIC.md`
- текущий отчёт.

Frontend и operator DOCX/PDF/PNG не изменялись: operator action sequence, trading geometry/status semantics и UI contract не менялись.

## 17. Red → green evidence

Первый RED:

```bash
python -m pytest -q tests/test_iteration255_outcome_worker_recovery.py
```

Существенный итог: `5 failed in 1.29s`; отсутствовали outcomes thread, stale/progress contract, cycle function и точная diagnostic reason.

Расширенный RED после добавления materialized schema assertions: `6 failed in 1.12s`; существенная строка — required materialized columns `issubset(columns)` returned `False`.

GREEN:

```bash
python -m pytest -q tests/test_iteration255_outcome_worker_recovery.py
```

Результат повторён детерминированно дважды: `11 passed in 1.04s` и `11 passed in 1.12s`.

Релевантный suite: `113 passed in 5.29s`.

## 18. Database/schema compatibility

Изменение additive и idempotent. `init_db()`:

- добавляет nullable columns при отсутствии;
- создаёт `idx_reco_outcome_liveness` и `idx_reco_llm_outcome_liveness`;
- выполняет bounded keyset backfill только для legacy rows с NULL materialized fields;
- сохраняет исходные `reasons_json`, audit identity и outcomes;
- использует commit boundaries, совместимые с SQLite/PostgreSQL layer.

Fresh SQLite: integrity `ok`, 20 tables, все 6 columns и оба indexes присутствуют. Existing-SQLite upgrade/backfill/idempotency покрыты regression test. PostgreSQL reference SQL и offline dialect suites прошли. Ручной SQL не требуется; перед обновлением рабочей БД рекомендуется обычная резервная копия.

## 19. API compatibility

OpenAPI после изменения: version 1.0.67, 23 paths, 7 mutating routes. Новые route names отсутствуют; private order routes — 0. Existing status/metrics расширены диагностикой обратно совместимо. Recommendation lifecycle, action endpoints и bot audit semantics не менялись.

## 20. Config/env compatibility

Новых переменных окружения нет; `.env.example` не изменён. Runtime interval использует существующий `OUTCOMES_INTERVAL_SEC`; hard safety cap является внутренним invariant. Пользовательских действий с `.env` не требуется.

## 21. Security boundary

Recommendation/audit-only boundary сохранён. Static scan не обнаружил Bybit order create/amend/cancel/batch endpoints или SDK-equivalent methods. `.env`, private keys и production DB не включаются в release. Реальные credentials и внешние сервисы не использовались.

## 22. Post-check commands и точные результаты

| Проверка | Результат |
|---|---|
| `python -m pip check` | FAILED — тот же pre-existing MoviePy/Pillow conflict |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE — `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | PASSED — 1164 collected |
| iteration255 targeted, run 1 | PASSED — 11 in 1.04 s |
| iteration255 targeted, run 2 | PASSED — 11 in 1.12 s |
| relevant outcome/liveness/DB/API suite | PASSED — 113 in 5.29 s |
| exhaustive deterministic batches | PASSED — 1164/1164, 47 disjoint batches, 221.67 s |
| monolithic pytest | Printed `1164 passed in 34.26s`, but process did not exit before 300-s external timeout; not used as the authoritative clean-exit result |
| fresh SQLite schema/integrity | PASSED — 20 tables, required columns/indexes, integrity ok |
| PostgreSQL offline support/locking/schema | PASSED within relevant suite; live integration SKIPPED |
| OpenAPI/static execution boundary | PASSED — 23 paths, 7 mutating audit/config routes, 0 private order routes |
| release operator artifacts | PASSED — DOCX, PDF, infographic markdown and PNG retained |

Synthetic liveness check over 4775 eligible roots with padded diagnostics returned only 10 sample IDs and completed in approximately 151 ms on local SQLite; это performance smoke test, не production PostgreSQL benchmark.

## 23. Что не удалось проверить и почему

- Ruff не установлен в активном environment; dependency installation не выполнялась через сеть.
- `pip check` не зелёный из-за внешнего pre-existing MoviePy/Pillow conflict, не связанного с проектными requirements.
- Live PostgreSQL integration отсутствует без явно disposable DSN.
- Production 4775-row database и её actual query plan не предоставлены; проверены synthetic SQLite и offline PostgreSQL contracts.
- Live Bybit, Ollama throughput и production VM resource profile не запускались.
- Monolithic pytest оставил процесс живым после полного summary; exhaustive batch union имеет нормальный exit code и покрывает все collected nodes.

## 24. Остаточные риски

- Первое обновление большой legacy БД выполнит bounded backfill; общее время зависит от объёма history и DB storage.
- Значительная доля historical grid roots может правомерно остаться censored из-за недостаточного candle volume или intrabar ambiguity; исправление улучшает liveness/диагностику, но не выдумывает fills.
- Hard cap ограничивает latency одного прохода, однако большой backlog потребует нескольких cycles.
- Реальная PostgreSQL latency/lock contention требует наблюдения после развёртывания по новым progress metrics.
- Profitability/calibration readiness не гарантированы исправлением runtime worker.

## 25. Rollback procedure

1. Остановить сервис.
2. Восстановить предыдущий v1.0.66 code/archive.
3. При необходимости восстановить резервную копию БД, сделанную до первого запуска v1.0.67.
4. Добавленные nullable columns/indexes можно оставить: v1.0.66 их игнорирует. Их удаление не требуется и не рекомендуется без отдельной migration plan.
5. Запустить v1.0.66 и проверить status/locks. Учесть: старый coupling и liveness defect вернутся.

## 26. Рекомендуемый следующий work package

Провести production-observability validation после развёртывания: зафиксировать 24–48 часов `rows_examined/labeled/censored`, backlog slope, cycle duration p50/p95, SQL query plans PostgreSQL и распределение explicit censorship reasons по symbol. На основании этих данных отдельно решить, нужны ли instrument liquidity admission rules или finer-grained outcome reconstruction — без ослабления fail-closed.
