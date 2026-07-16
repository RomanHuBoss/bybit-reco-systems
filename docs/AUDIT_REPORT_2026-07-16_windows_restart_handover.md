# Аудит-итерация: Windows restart handover, shadow outcome liveness и журнал диагностики

## 1. Название итерации

Bybit Recommender v1.0.71 — безопасная передача runtime-lock при перезапуске, восстановление исследовательских исходов и целостная локализация журнала.

## 2. Входной ZIP

`bybit-reco-systems-main(2)(1).zip`

## 3. SHA-256 входного ZIP

`8c4d17429784470d0748f1611f005ecf184c9510621dde9e8de41356cffbac01`

## 4. Исходная версия

`1.0.69`, source of truth: `version=` при создании FastAPI в `app/main.py`.

## 5. Новая версия

`1.0.71` (patch). Итог включает подтверждённое исправление shadow-outcome, фактически проверенное в промежуточной Windows-диагностике 1.0.70, и новый restart-handover пакет.

## 6. Project fingerprint

Проверен и совпадает: README/CHANGELOG/requirements/main.py; FastAPI в `app/main.py`; `futures_grid`; Bybit `category=linear`, USDT perpetual; recommendation/audit-only; SQLite + PostgreSQL; frontend в `app/ui/static`; canonical direction в `app/trading_semantics.py`; обе reference migrations присутствуют. Private order create/amend/cancel endpoints не обнаружены.

## 7. Цель итерации

После этой итерации штатно завершающийся Windows/Linux процесс должен освобождать принадлежащие ему runtime-lock, новый процесс должен показывать `handover`, а не ложный `stalled`, пока безопасно ждёт аварийный lease, риск-чистые `shadow_exploration` должны размечаться без невозможного LLM verdict, а журнал должен сохранять машинные коды и идентификаторы без разрушительного пословного перевода.

## 8. Критерии приёмки

1. `shadow_no_trade` с `policy_evaluation_eligible=false` виден outcome-worker при advisory LLM.
2. Supervised shutdown удаляет lock только при совпадении owner.
3. Restart в пределах collector lease возвращает `starting`/`handover`, не `COLLECTOR_STALLED`.
4. Status публикует lock owner, heartbeat, TTL и секунды до takeover.
5. Stale market data не маскируются как торгово свежие.
6. Action/reason codes и `rec_id` сохраняются в журнале дословно; рядом есть русское объяснение.
7. Exact-policy calibration и торговые gates не ослаблены.
8. Полная коллекция regression tests проходит.

## 9. Прочитанные источники

Прочитаны protocol PDF, README, CHANGELOG, `.env.example`, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, последние audit reports, `app/main.py`, `app/db.py`, `app/outcomes.py`, settings, collector, calibration, risk, recommender, frontend и релевантные tests. Учтены диагностические JSON 1.0.69/1.0.70 и предоставленный оператором Windows-журнал outcome-censor событий.

## 10. Карта затронутого data flow

`FastAPI lifespan -> supervised background target -> runtime lock claim/heartbeat/release -> collector_last_cycle -> runtime_provenance -> operator_readiness -> health UI`.

`recommendation outcome_policy -> materialized eligibility -> outcome SQL/liveness -> proxy label or censored observability -> calibration lineage`.

`decision_log API payload -> localizeObjectForDisplay -> exact action/reason mapping -> operator JSON/modal`.

## 11. Baseline environment

- Python 3.13.5
- Node 22.16.0
- Input root: `bybit-reco-systems-main`
- Production Python files: 24
- Test files before итерации: 202
- Docs: 79
- Frontend files: 3
- Migration SQL: 2
- API routes: 24; mutating routes: 7
- Следующий regression number: 258

## 12. Baseline commands и результаты

- `python -m pip check`: FAILED — внешний конфликт `moviepy 2.2.1` требует `pillow<12`, установлен `pillow 12.2.0`.
- `python -m compileall -q app tests main.py`: PASSED.
- `python -m ruff check .`: UNAVAILABLE — модуль ruff не установлен.
- `node --check app/ui/static/app.js`: PASSED.
- `pytest --collect-only -q`: 1175 tests.
- Монолитный baseline pytest не был принят как итоговый из-за ограничения execution harness.
- Исчерпывающий baseline: 8 непересекающихся file batches, union = collected set, 1175 passed, 0 failed.

## 13. Подтверждённые defects/gaps

### WRH-001 — HIGH, CONFIRMED DEFECT

- Файлы: `app/main.py`, background supervisor/lifespan.
- Фактическое поведение: при штатном shutdown lock-row сохранялся до TTL. На Windows новый PID видел старого owner; collector lease 400 сек превышал stale threshold 300 сек.
- Влияние: все 35 инструментов переходили в stale и UI показывал `COLLECTOR_STALLED`, хотя ошибок Bybit не было.
- Исправление: lifecycle release в `finally`; delete только `WHERE owner=current_owner`.

### WRH-002 — MEDIUM, CONFIRMED DEFECT

- Файл: `app/main.py`, runtime provenance/readiness.
- Фактическое поведение: old-cycle age оценивался как stall до завершения безопасного takeover; boot grace был короче collector lease.
- Исправление: отдельный handover grace = collector TTL + interval, состояние `handover`, lock diagnostics. Symbol freshness не подменяется.

### WRH-003 — HIGH, CONFIRMED DEFECT

- Файлы: `app/outcomes.py`, `app/db.py`.
- Фактическое поведение: LLM SQL/liveness требовали `policy_evaluation_eligible=true` даже для риск-чистого research shadow, хотя LLM reviewer не рассматривает `no_trade`.
- Исправление: LLM bypass зависит от explicit outcome eligibility, shadow role и чистых risk checks; exact-policy eligibility остаётся downstream calibration gate.

### WRH-004 — MEDIUM, CONFIRMED DEFECT

- Файл: `app/ui/static/app.js`.
- Фактическое поведение: общий regex-перевод превращал `OUTCOME_SKIP_INVALID_GRID_CONTRACT` в смешанный текст, а `futures_grid` внутри `rec_id` — в русские слова, разрушая audit identity.
- Исправление: exact action/reason dictionaries; technical identifier fields выводятся дословно; русское описание содержит исходный code.

## 14. Неподтверждённые claims

Не подтверждено, что высокая доля `censored` является ошибкой алгоритма. Предоставленные причины (`intrabar_extreme_order_unobservable`, неизвестный порядок replacement/kill-switch, недостаточный candle volume) соответствуют documented fail-closed observability boundary. Они не ослаблялись.

## 15. План исправления

Минимальный пакет: синхронизировать SQL/liveness shadow contract; добавить owner-safe release; разделить handover/stall; опубликовать lock diagnostics; исправить только отображение журнала; не менять schema, thresholds, grid math или calibration identity.

## 16. Фактический diff по файлам

Production: `app/main.py`, `app/db.py`, `app/outcomes.py`.
Frontend: `app/ui/static/app.js`, `app/ui/static/index.html`.
Tests: новый `tests/test_iteration258_windows_restart_handover.py`; release-version assertions синхронизированы с 1.0.71.
Docs: README, CHANGELOG, ARCHITECTURE, MODULES, SCENARIOS, TRADING_LOGIC, KNOWN_RISKS, HOW_TO_TRADE_INFOGRAPHIC, operator DOCX/PDF, этот report.
Database/migrations: без изменений.

## 17. RED -> GREEN evidence

RED command:

`python -m pytest -q tests/test_iteration258_windows_restart_handover.py`

Существенный результат на pristine + test:

`4 failed`:
- `matured_pending_total` был 0 вместо 1;
- collector lock оставался в таблице;
- `boot_grace_active` был false и статус становился stalled;
- журнал выдавал `наблюдения SKIP INVALID сетка CONTRACT` и изменял `rec_id`.

GREEN command: тот же.

Результат: `4 passed` (повторная проверка также passed).

## 18. Database/schema compatibility

Schema не изменена. `get_runtime_lock_snapshot` — read-only helper над существующей таблицей. Fresh SQLite init: 20 tables; repeated init: 20; `PRAGMA integrity_check=ok`; шесть materialized outcome columns присутствуют. Existing-schema migration regression входит в полный suite. PostgreSQL translation/locking/schema subset: 16 passed, 10 deselected. Live PostgreSQL integration SKIPPED: disposable DSN не предоставлен.

## 19. API compatibility

Поля не удалялись. В `runtime_provenance` добавлены nullable diagnostics: collector lock owner, heartbeat, TTL, takeover seconds, owned-by-current-process. Collector state получил дополнительное значение `handover`. Existing `CURRENT_PROCESS_CYCLE_PENDING` сохранён; добавлено объяснение `RUNTIME_LOCK_HANDOVER`.

## 20. Config/env compatibility

Новые переменные окружения не добавлены. Ручных изменений `.env` не требуется. Collector TTL формируется прежней формулой; изменён lifecycle release и диагностическая интерпретация.

## 21. Security boundary

Реальные credentials, production DB и Bybit private order methods не использовались. Release не содержит `.env`, runtime DB и cache artifacts. Read-only lock snapshot не участвует в claim; atomic PostgreSQL UPSERT сохраняется.

## 22. Post-check commands и результаты

- `python -m pip check`: FAILED, тот же внешний MoviePy/Pillow conflict.
- `python -m compileall -q app tests main.py`: PASSED.
- `python -m ruff check .`: UNAVAILABLE.
- `node --check app/ui/static/app.js`: PASSED.
- Collection: 1179 tests.
- Exhaustive post-check: 1179/1179 passed в непересекающихся file batches (batch 5 дополнительно разделён на 3 deterministic sub-batches из-за межфайлового завершения процесса в harness).
- Targeted iteration258: 4 passed дважды.
- Relevant runtime/outcome/UI/PostgreSQL suite: 38 passed.
- Version/document suite: 127 passed.
- DOCX rendered: 14 pages, visual review passed; PDF rendered: 14 pages.

## 23. Что не удалось проверить

- Реальный disposable PostgreSQL integration.
- Реальную Bybit сеть.
- Поведение на production Windows service manager при жёстком завершении питания/ОС.
- Ruff из-за отсутствующего dev tool.
- Монолитный pytest как один процесс: harness не завершал/ограничивал длинный процесс; вместо этого выполнен полный доказанный union batches.

## 24. Остаточные риски

При аварийном kill lock живёт до TTL; `handover` не означает свежесть данных. Большой outcome backlog может быть вычислительно тяжёлым. Высокая цензура уменьшает эффективную выборку и не должна обходиться предположениями. Shadow exploration не доказывает live edge.

## 25. Rollback procedure

Остановить 1.0.71, вернуть 1.0.69/1.0.70 файлы приложения и запустить с прежней `.env`. Откат схемы/данных не нужен. Не удалять outcomes, observability ledger или runtime lock table вручную; owner-safe release и TTL обеспечивают восстановление.

## 26. Следующий рекомендуемый work package

После разгрузки очереди измерить censor-reason distribution и effective temporal clusters. Отдельно исследовать, можно ли повысить наблюдаемость через более детальные публичные данные без реконструкции несуществующей очереди заявок и без ослабления fail-closed.
