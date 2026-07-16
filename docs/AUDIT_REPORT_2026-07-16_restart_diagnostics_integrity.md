# Iteration 257 — restart diagnostics integrity

## 1. Итерация

**v1.0.69 — целостность диагностики после перезапуска и контроль непрерывности БД.**

## 2. Входные материалы

- ZIP: `bybit-reco-systems-1.0.68-operator-readiness-ui.zip`
- SHA-256 ZIP: `1c571b474f0743b27660060a0d0929a5849caff753a76ff8ab2019c0bc8038d8`
- Production diagnostic JSON: `bybit-recommender-diagnostics-2026-07-16T11-27-55-485Z.json`
- SHA-256 diagnostic JSON: `f28edf1654dceabce947872c1b9aef2dcb4b3e7a80f9b79bece9fcc8a64f69ef`
- Исходная версия: `1.0.68`
- Новая версия: `1.0.69`

## 3. Project fingerprint

Подтверждены README, CHANGELOG, requirements, `main.py`, `app/main.py`, recommender/trading/grid/risk/calibration/outcome/database/client модули, frontend в `app/ui/static`, dual SQLite/PostgreSQL migrations, tests и обязательная документация. Scope остаётся recommendation/audit-only, Bybit Linear USDT perpetual, `futures_grid`; private order execution не добавлялся.

## 4. Цель

После этой итерации операторская сводка должна учитывать весь последний publication cycle; после рестарта система не должна объявлять новый процесс готовым по persisted-метрикам прежнего процесса; БД должна иметь безопасный постоянный identity token для проверки непрерывности обновлений.

## 5. Критерии приёмки

1. `latest_snapshot_total` включает outcome-root и non-root audit rows одного `ts`.
2. Статус остаётся `starting`, пока текущий процесс не завершил собственные collector cycle и publication.
3. После boot grace отсутствие собственного прогресса становится `degraded`.
4. `/api/v1/status` содержит `runtime_provenance`.
5. `/api/v1/status` содержит `database_continuity` со стабильным non-secret ID и counts/ranges.
6. Fresh и existing SQLite init проходят идемпотентно; PostgreSQL dialect tests зелёные.
7. Полная тестовая коллекция проходит без ослабления торговых gates.

## 6. Прочитанные источники

Прочитаны релевантные части README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, последние audit reports, `app/main.py`, `app/db.py`, `app/recommender.py`, `app/outcomes.py`, `app/settings.py`, frontend и regression tests iteration255–256. Production diagnostic JSON разобран полностью как JSON.

## 7. Карта затронутого data flow

`run_recommender_once -> recommendations + PUBLISH decision -> /api/v1/status -> recommendation_readiness -> operator_readiness -> health modal/export`.

Restart provenance: `PROCESS_STARTED_TS/RUNTIME_OWNER + collector_last_cycle + latest recommendation ts -> runtime_provenance -> starting/degraded/ready`.

Persistence continuity: `init_db -> app_config.database_instance_id_v1 -> aggregate table counts/ranges -> database_continuity -> health modal/export`.

## 8. Baseline environment

- Python: 3.13.5
- Node: 22.16.0
- `pip check`: FAILED — внешний conflict `moviepy 2.2.1` требует `pillow<12`, установлена Pillow 12.2.0.
- Ruff: UNAVAILABLE (`No module named ruff`).
- compileall: PASSED.
- Node syntax: PASSED.

## 9. Baseline tests

- Collected: 1171.
- Exhaustive batched run: 1171/1171 passed.
- Покрытие: 20 логических batches; один 60-node набор разделён на 6 групп по 10 для гарантированного завершения процесса.
- Фактическое test time по summaries: 54.29 s.
- Targeted existing iteration255+256: 18 passed.

## 10. Подтверждённые дефекты

### RD-257-01 — HIGH — CONFIRMED DEFECT

- Файл: `app/main.py`, `_latest_recommendation_readiness`.
- Production input: latest PUBLISH содержит `count_all=35`, `count_no_trade=34`, `count_blocked=1`.
- Фактическое поведение 1.0.68: readiness возвращает `latest_snapshot_total=1`, `no_trade=0`, `blocked=1`, `dominant_state=all_blocked`.
- Причина: `WHERE ts=? AND is_outcome_label_root=1` исключал 34 текущие строки, повторно использующие прежние shadow outcome roots.
- Нарушение: UI/status и publication lifecycle parity.
- Влияние: оператор не видел реальные причины no-trade и получал ошибочное объяснение «всё заблокировано».
- Исправление: operator snapshot больше не фильтруется по outcome-label identity.

### RD-257-02 — HIGH — CONFIRMED DEFECT

- Файл: `app/main.py`, `api_status` / `_operator_runtime_readiness`.
- Production input: process owner `Ubuntu-RRM:181960`, persisted collector cycle owner `Ubuntu-RRM:135695`; collector cycle and latest publication timestamp предшествуют `process_started_ts`; boot grace active.
- Фактическое поведение 1.0.68: `healthy_not_actionable`, `runtime_healthy=true`.
- Ожидаемое: `starting` до собственного cycle/publication; после grace — `degraded`, если прогресса нет.
- Нарушение: background runtime/restart observability.
- Исправление: additive `runtime_provenance` и readiness state contract.

### RD-257-03 — MEDIUM — CONFIRMED GAP

- Файл: `app/db.py`, status API, frontend.
- Фактическое поведение: diagnostic JSON не позволял доказать, что новая версия использует то же хранилище; path/DSN раскрывать нельзя.
- Исправление: stable 128-bit random database instance ID в `app_config`, safe engine/count/time-range summary без path, DSN и credentials.

## 11. Неподтверждённые claims и ограничения

- Production diagnostic не доказывает смену БД. В 1.0.68 отсутствовал continuity token; нулевые current-policy outcomes также совместимы с 12-часовым horizon и новым exact-policy cohort.
- Outcome worker в diagnostic был `ok`, очередь matured roots равна нулю; его остановка не подтверждена.
- 3 исторических Bybit `10006` rate-limit events присутствуют в 200 decisions, но `errors_10m=0`, данные всех 35 symbols свежие; устойчивый collector defect не подтверждён.
- Наличие `no_trade` не доказывает неисправность стратегии или инфраструктуры. Точные dominant no-trade codes в 1.0.68 были скрыты RD-257-01 и станут доступны после обновления.

## 12. План и фактический diff

### Production

- `app/db.py`: stable DB identity и continuity summary.
- `app/main.py`: полный publication snapshot, runtime provenance, readiness transition, API fields, version 1.0.69.
- `app/ui/static/app.js`: отображение DB identity/counts и restart provenance.
- `app/ui/static/index.html`: cache bust 1.0.69.

### Tests

- Новый `tests/test_iteration257_restart_diagnostics_integrity.py` — 4 regression tests.
- Version assertions синхронизированы с 1.0.69.

### Database/migrations

Reference SQL не изменён: новая информация хранится в существующей `app_config`. `init_db()` добавляет ID идемпотентно. Breaking schema migration отсутствует.

### Docs

README, CHANGELOG, ARCHITECTURE, KNOWN_RISKS, MODULES, SCENARIOS, TRADING_LOGIC, HOW_TO_TRADE_INFOGRAPHIC, operator DOCX/PDF и этот audit report.

## 13. RED -> GREEN

RED command:

```bash
python -m pytest -q tests/test_iteration257_restart_diagnostics_integrity.py
```

Существенный RED result на 1.0.68:

```text
4 failed
assert latest_snapshot_total == 3  # actual 1
assert operator_readiness.state == "starting"  # actual healthy_not_actionable
AttributeError: app.db has no attribute get_database_continuity_status
assert "runtime_provenance" in app.js
```

GREEN command:

```bash
python -m pytest -q tests/test_iteration257_restart_diagnostics_integrity.py
```

GREEN result:

```text
4 passed in 1.54s
```

Связанный suite iteration255–257: 22 passed.

## 14. Database/schema compatibility

- Fresh SQLite: integrity `ok`, 20 tables, DB ID length 32 hex chars.
- Existing SQLite simulation without new config key: `init_db()` создаёт ID, integrity `ok`.
- PostgreSQL offline/dialect/locking suite: 20 passed.
- Live PostgreSQL: SKIPPED — verified disposable DSN не предоставлен.
- Manual SQL actions: не требуются.

## 15. API/config compatibility

- API additive: `runtime_provenance`, `database_continuity`.
- Existing fields/status semantics сохранены.
- `.env` variables не добавлены и не изменены.
- Stable DB ID не является credential и не раскрывает target path/DSN.

## 16. Security boundary

Production source scan не обнаружил private Bybit order create/amend/cancel/batch endpoints. Recommendation/audit-only boundary сохранён. В diagnostic continuity нет DB path, host, username, password или DSN.

## 17. Post-check

- Collected: 1175.
- Exhaustive batched run: 1175/1175 passed.
- 25 непересекающихся groups; test time по summaries 55.83 s.
- compileall: PASSED.
- Node syntax: PASSED.
- Targeted iteration257 repeated: PASSED.
- Related iteration255–257: 22 passed.
- PostgreSQL offline suite: 20 passed.
- Fresh/existing SQLite: PASSED.
- Operator DOCX/PDF: 13 pages rendered and visually inspected; clipping/overlap не обнаружены.
- Ruff: UNAVAILABLE.
- pip check: FAILED только из-за pre-existing MoviePy/Pillow environment conflict.

## 18. Что не проверено

- Live Bybit network и credentials.
- Live PostgreSQL disposable integration.
- Фактическая DB identity до установки 1.0.69: token ранее отсутствовал.
- Production behavior после первого собственного cycle 1.0.69; для этого нужен новый exported diagnostic JSON.
- Profitability/live edge не проверялись и не заявляются.

## 19. Остаточные риски

- При нескольких реально запущенных OS-процессах runtime locks должны предотвратить duplicate workers, но provenance после grace покажет degraded только по отсутствию собственного progress; он не заменяет process supervisor audit.
- Stable ID помогает сравнивать будущие обновления, но не восстанавливает прошлую identity.
- 35-row publication может оставаться полностью no-trade длительное время из-за exact-policy calibration/economics; thresholds не ослаблены.

## 20. Rollback

1. Остановить сервис.
2. Вернуть код 1.0.68.
3. Запустить один процесс.
4. Откат БД не требуется: дополнительный `app_config` key безопасно игнорируется 1.0.68.
5. Выполнить hard refresh браузера.

## 21. Следующий work package

После 30–60 минут работы 1.0.69 выгрузить новый diagnostic JSON. Проверить: оба current-process flags равны `true`, DB ID стабилен между рестартами, `latest_snapshot_total=35`, сумма status counts равна 35, и появились структурированные dominant no-trade reason codes. Только после этого выбирать data-driven work package по calibration/economics; thresholds заранее не снижать.
