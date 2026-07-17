# Audit report: LLM reviewer shutdown liveness - v1.0.73

## 1. Название итерации

Iteration 260 - корректное завершение LLM reviewer и owner-safe release его runtime-lock.

## 2. Входной ZIP

`bybit-reco-systems-main(4).zip`.

## 3. SHA-256 входного ZIP

`286eb13789f35836a97cd8ee6faa28f9cf7760084e173c3764211ac2e29c6647`.

Архив содержит 337 entries и ровно один root `bybit-reco-systems-main/`. CRC, absolute-path, `../` traversal, symlink, duplicate/conflicting-path и nested-archive проверки пройдены.

## 4. Исходная версия

`1.0.72`; source of truth - `FastAPI(..., version="1.0.72")` в `app/main.py`.

## 5. Новая версия

`1.0.73` (patch, без breaking API/config/schema change).

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender: присутствуют обязательные README/CHANGELOG, FastAPI entrypoint, recommender/trading/grid/risk/calibration/outcome/DB/Bybit modules, frontend `app/ui/static/`, tests, SQLite/PostgreSQL reference SQL и release operator artifacts. Подтверждены `futures_grid`, Bybit `category=linear`, USDT perpetual, dual persistence и recommendation/audit-only boundary. Private order create/amend/cancel endpoints отсутствуют.

Внешний PDF указывает `isolated`, но фактический ZIP, код, документы и regression contract используют Bybit Futures Grid `cross`. По установленному протоколом порядку доверия этот исторический конфликт не изменялся в данной итерации.

## 7. Цель итерации

После этой итерации LLM reviewer должен завершать цикл по общему shutdown-event, не начинать второй sweep после сигнала остановки, возвращаться в supervisor как clean stop и освобождать только принадлежащий текущему owner runtime-lock.

## 8. Критерии приёмки

1. После stop-event reviewer выполняет не более одного уже начатого тестового sweep.
2. Повторный interval wait после stop-event отсутствует.
3. Supervisor сохраняет `state=stopped`, не `error`.
4. `consecutive_failures=0`.
5. Owned `runtime:llm_reviewer` отсутствует после возврата supervisor.
6. LLM verdict/pending semantics, trading/risk/calibration gates, API, schema и `.env` не меняются.
7. Новый test красный на pristine production и зелёный после fix; полный offline suite проходит.

## 9. Прочитанные источники

- пользовательский PDF-протокол от 10 июля 2026 г.;
- диагностический JSON `bybit-recommender-diagnostics-2026-07-17T02-40-30-031Z.json`;
- README, CHANGELOG, requirements, `.env.example`;
- KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS и HOW_TO_TRADE_INFOGRAPHIC;
- пять последних audit reports, особенно iterations 255, 257, 258 и 259;
- `app/main.py`, `app/recommender.py`, `app/db.py`, `app/outcomes.py`, `app/settings.py`, `app/llm_review.py`, runtime-lock helpers и relevant regression tests.

Диагностика 1.0.72 показала здоровые 35/35 symbols, 35 строк последней публикации, 0 actionable, 33 `no_trade`, 2 `blocked`, 29 074 historical outcomes, 112 current-model outcomes и 0 exact-policy eligible outcomes. Это согласованное fail-closed состояние не трактовалось как дефект и пороги не снижались. Дополнительный restart-сигнал: persisted LLM worker snapshot `1784255108` предшествовал текущему process start `1784255217`; это помогло выбрать участок аудита, но само по себе не доказывает причину старого snapshot.

## 10. Карта затронутого data flow

`FastAPI lifespan shutdown` -> `_BACKGROUND_STOP_EVENT.set()` -> `_interval_loop_wait()` -> `_llm_reviewer_thread()` loop guard -> `_run_supervised_background_target()` clean stop -> `_release_component_runtime_lock()` -> owner-qualified `release_runtime_lock()` -> следующий процесс получает lease или fail-closed ждёт TTL.

## 11. Baseline environment

- Python `3.12.13`;
- Node `v24.14.0`;
- отдельный временный venv вне project root;
- runtime/dev requirements установлены без изменения lock-файлов проекта;
- inventory: 25 production Python files, 205 test files, 83 docs, 3 frontend files, 2 migration SQL files;
- 24 API routes, из них 7 mutating;
- максимальный исходный regression number: 259.

## 12. Baseline commands и точные результаты

До production-правок, в pristine copy:

- `python -m pip check` - PASSED: `No broken requirements found.`
- `python -m compileall -q app tests main.py` - PASSED.
- `node --check app/ui/static/app.js` - PASSED.
- `python -m ruff check .` - FAILED: 24 pre-existing findings (2 E741, 2 F401 in production, 6 F841 in `app/main.py`, остальные в historical tests); новая итерация их не создала.
- `python -m pytest --collect-only -q` - 1186 tests collected.
- первый `python -m pytest -q` с proxy-переменными audit harness - 1160 passed, 26 failed: закреплённый HTTPX 0.27.2 отклонил внешний `ALL_PROXY=socks5h://...` до mock transport.
- детерминированный offline baseline с очищенными `ALL_PROXY/HTTP_PROXY/HTTPS_PROXY` - 1186 passed in 45.28s.

Production code до завершения baseline не менялся.

## 13. Подтверждённые defects/gaps

### LSL-260-01 - HIGH - CONFIRMED DEFECT

- Файл: `app/main.py`, исходные строки 6882-6916.
- Функция: `_llm_reviewer_thread()`.
- Вход: reviewer включён; один sweep завершён; `_interval_loop_wait()` получает общий shutdown-event.
- Путь данных: lifespan -> stop-event -> wait -> reviewer loop -> supervisor -> runtime-lock release.
- Фактическое поведение 1.0.72: цикл был `while True`; после stop-event он атомарно получал тот же lock и начинал второй sweep. Динамический reproducer получил `sweep_calls=2`, затем supervisor классифицировал контрольную остановку как crash.
- Ожидаемое поведение: все registered background loops прекращают следующий проход после общего stop-event; supervisor видит clean return и выполняет existing owner-safe release.
- Нарушенный инвариант: background runtime/graceful shutdown/restart recovery.
- Финансовое влияние: прямое изменение PnL отсутствует; возможна потеря торговой возможности из-за задержанного reviewer после restart.
- Trading/risk влияние: fail-open не возникал, но stale lease мог задержать обязательный LLM verdict и привести `pending` к timeout/no-trade.
- Model/data влияние: повторный sweep во время shutdown мог выполнять лишний model call и изменять audit status в уже останавливающемся процессе.
- Operational/security/UX влияние: lock мог пережить обычный restart до TTL; thread state мог получить ложный `error`; чужой owner удалять нельзя.
- Почему tests не поймали: supervisor clean-stop проверялся synthetic target, а LLM loop test завершался искусственным исключением из wait и не проверял общий event.
- Fix: заменить только reviewer guard на `while not _BACKGROUND_STOP_EVENT.is_set()`.
- Остаточный риск: hard kill и уже выполняющийся внешний HTTP-call могут завершить процесс до release; TTL takeover остаётся обязательным.

## 14. Неподтверждённые claims

- Не доказано, что stale LLM snapshot в приложенной диагностике возник именно из-за этого defect: возможен аварийный kill или активный lease другого процесса.
- `0` actionable и `0` exact-policy outcomes не признаны неисправностью: последние строки корректно отклонены действующими mean-reversion, monetary и probability gates.
- Не проверялись и не заявляются profitability, live edge или production auto-execution readiness.
- Один `intrabar_extreme_order_unobservable` в diagnostic остаётся documented fail-closed OHLCV limitation, а не основанием придумывать fill order.

## 15. План исправления

Добавить один независимый dynamic regression; показать red на копии с исходным production; изменить один loop guard; проверить clean supervisor state и lock release; синхронизировать patch version/cache build/docs; повторить полный offline suite и release checks.

## 16. Фактический diff по файлам

Production:

- `app/main.py` - stop-aware reviewer loop и FastAPI `1.0.73`.

Frontend:

- `app/ui/static/index.html` - только cache build `1.0.73`; runtime JS/CSS не менялись.

Tests:

- новый `tests/test_iteration260_llm_shutdown_liveness.py`;
- version consistency обновлена в iterations 213-226, 238, 240 и 256 без изменения их trading expectations.

Database/migrations:

- изменений нет.

Docs:

- `README.md`, `CHANGELOG.md`, `docs/KNOWN_RISKS.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md` и этот report.
- DOCX/PDF/PNG operator artifacts сохранены без изменений: действия оператора, UI, статусы и торговый порядок не менялись.

## 17. Red -> green evidence

RED command на red copy:

`python -m pytest -q tests/test_iteration260_llm_shutdown_liveness.py`

Существенная строка: `E assert 2 == 1`; summary: `1 failed in 1.65s`.

GREEN command на working copy:

`python -m pytest -q tests/test_iteration260_llm_shutdown_liveness.py`

Существенная строка: `1 passed in 1.72s`.

## 18. Database/schema compatibility

Schema/bootstrap/reference migrations не менялись. Fresh/repeated SQLite init: `fresh_tables=20 reinit_tables=20`. Existing SQLite additive-upgrade regression и SQLite/PostgreSQL contract suite прошли. Ручная миграция не требуется.

## 19. API compatibility

24 routes сохранены; field names, request/response schemas и status semantics не менялись. Изменён только FastAPI patch version.

## 20. Config/env compatibility

Новых переменных нет. Reviewer cadence, pending timeout, HTTP timeout, lock TTL и defaults не менялись. `.env.example` не изменён.

## 21. Security boundary

Recommendation/audit-only boundary сохранён; private order endpoints не добавлены. Lock release остаётся owner-qualified. `.env`, credentials, private keys и production DB в release не включаются.

## 22. Post-check commands и точные результаты

- `python -m pip check` - PASSED.
- `python -m compileall -q app tests main.py` - PASSED.
- `node --check app/ui/static/app.js` - PASSED.
- `python -m pytest --collect-only -q` - 1187 collected.
- полный sanitized offline `python -m pytest -q` - 1187 passed in 44.75s.
- relevant shutdown/restart/LLM suite - 17 passed in 3.49s.
- SQLite/PostgreSQL contract suite - 24 passed in 2.84s.
- новый test отдельно повторён дважды - по `1 passed in 1.46s` в каждом независимом запуске; new-file Ruff - PASSED.
- docs/version/release targeted suite после финального отчёта - 19 passed in 1.92s.
- `python -m ruff check .` - те же 24 baseline findings, delta 0.
- private-order endpoint scan - 0 matches.
- version/cache consistency - `1.0.73`/`build=1.0.73`.
- required DOCX/PDF/Markdown/PNG artifacts - present.
- secret and `.env` scans - 0 findings.

Промежуточный полный post-check обнаружил три docs-contract failures из-за ссылки CHANGELOG на audit filename; ссылка удалена, три теста отдельно прошли, затем финальный полный suite завершился зелёным.

## 23. Что не удалось проверить и почему

- Live PostgreSQL integration: disposable DSN не предоставлен.
- Реальный Windows service-manager shutdown/restart и production lock handover.
- Live Bybit и Ollama network calls: offline iteration не использовала внешние credentials/network.
- Завершение процесса посередине реального LLM HTTP-call длительностью до timeout.
- Ruff всего проекта остаётся красным из-за 24 зафиксированных baseline findings; изменённый regression file чист.

## 24. Остаточные риски

- `kill -9`, авария ОС или принудительное завершение во время in-flight model call оставляют lease до TTL.
- Lifespan ждёт каждый daemon thread ограниченное время; эта итерация устраняет бесконечный повторный reviewer loop, но не вводит cancellation transport для уже начатого HTTP-call.
- Диагностика LLM worker пока не публикует такой же подробный lock owner/takeover provenance, как collector.
- Proxy outcomes, calibration и LLM review не доказывают live profitability.

## 25. Rollback procedure

Остановить 1.0.73, восстановить code/docs версии 1.0.72 и перезапустить один application process. DB rollback и ручное удаление таблиц не требуются. Не удалять lock другого owner; при stale lease дождаться TTL либо выполнить штатный owner-safe shutdown процесса-владельца. Откат возвращает дефект повторного reviewer sweep после stop-event.

## 26. Один рекомендуемый следующий work package

Добавить bounded graceful-drain contract для in-flight LLM calls и reviewer-specific provenance: current-process cycle timestamp, lock owner/heartbeat/TTL/takeover и отдельные `handover`/`stalled` состояния. Не менять verdict, confidence или trading gates в этом пакете.

## Готовый commit message

`fix(runtime): stop LLM reviewer cleanly on shutdown`

- stop reviewer loop on the shared shutdown event
- release the owned reviewer lock through the supervisor clean-stop path
- add iteration 260 regression and synchronize version/docs
