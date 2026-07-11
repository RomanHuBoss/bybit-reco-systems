# Итерация 201: Bybit response/request integer integrity

## 1. Название итерации

Bybit Recommender v1.0.13 - fail-closed exact-integer semantics для Bybit V5 `retCode` и kline/open-interest request windows.

## 2. Входной ZIP

`bybit-reco-systems-main(1).zip`

## 3. SHA-256 входного ZIP

`252d8d8264cb1425354b22dafbcdda886ca177516436b6a89917ec3e3fcfc7a3`

## 4. Исходная версия

`1.0.12`, source of truth: `FastAPI(..., version="1.0.12")` в `app/main.py`.

## 5. Новая версия

`1.0.13` (patch): исправлены fail-closed defects без изменения публичных routes, response fields, DB schema, config или operator lifecycle.

## 6. Project fingerprint

Fingerprint совпал:

- README описывает Bybit Recommender;
- поддерживается только `futures_grid`;
- scope - Bybit `category=linear`, USDT perpetual;
- проект является recommendation/audit service, не OMS/EMS;
- SQLite и PostgreSQL сохранены;
- FastAPI создаётся в `app/main.py`;
- frontend находится в `app/ui/static/`;
- canonical directional model находится в `app/trading_semantics.py`;
- private order create/amend/cancel endpoints в production code отсутствуют.

## 7. Цель итерации

После этой итерации публичный Bybit-клиент должен принимать HTTP 2xx response как успешный только при присутствующем exact-integer `retCode=0`, а kline/open-interest request boundaries не должны превращать boolean, fractional, negative или инвертированные значения в правдоподобные query parameters через `int()`.

## 8. Критерии приёмки

1. Missing, `null`, boolean, blank, collection и fractional `retCode` не открывают доступ к `result`.
2. Malformed `retCode` использует существующий retryable response-shape path; после исчерпания retry возникает явная ошибка.
3. Kline/open-interest `limit`, `start/end` и `startTime/endTime` принимают только exact integers; `5` и `5.0` совместимы.
4. Отрицательные timestamps и `start > end` блокируются до сетевого вызова.
5. Existing Bybit transport, pagination, funding, symbol-scope и temporal tests остаются зелёными.
6. Schema, migrations, API routes, env, frontend и recommendation/OMS boundary не меняются.

## 9. Прочитанные источники

- `README.md`, `CHANGELOG.md`, requirements, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- iteration reports 195-200 и существующий audit prompt;
- canonical `app/trading_semantics.py`, grid/risk/recommender/calibration/outcomes/features/direction/regime/collector/DB/API/frontend modules по карте вызовов;
- `app/bybit_client.py`, `tests/conftest.py` и Bybit transport/pagination regression suites;
- официальный Bybit V5 Integration Guidance: `retCode` является common response number, success example использует `0`;
- официальный Bybit V5 Get Kline: `limit`, `start`, `end` имеют integer contract, limit range 1-1000;
- официальный Bybit V5 Get Open Interest: `limit`, `startTime`, `endTime` имеют integer contract, limit range 1-200.

Проверенные официальные страницы:

- https://bybit-exchange.github.io/docs/v5/guide
- https://bybit-exchange.github.io/docs/v5/market/kline
- https://bybit-exchange.github.io/docs/v5/market/open-interest

## 10. Карта затронутого data flow

`Bybit HTTP response -> app/bybit_client.py::_get -> ticker/kline/funding/OI/instrument consumers -> collector -> persistence -> features/recommender`.

Request path: `collector backfill/OI pagination -> get_kline/get_open_interest_page -> validated query parameters -> public Bybit REST`.

Directional TP/SL/PnL, grid economics, risk sizing, frontend and operator materialization paths не изменялись.

## 11. Baseline environment

- Python 3.12.13;
- Node v24.14.0;
- отдельный temporary virtualenv вне project root;
- runtime/dev dependencies установлены из pinned requirements;
- ZIP: 213 entries, CRC PASS, один root, без absolute paths, traversal, symlink, duplicate/conflicting paths или nested archives;
- production Python files: 23; test files: 144; docs: 24; frontend files: 3; migration SQL files: 2;
- исходный максимальный iteration number: 200;
- DB backends: SQLite и PostgreSQL/psycopg compatibility layer;
- API: 24 FastAPI routes после импорта приложения.

Harness экспортировал `ALL_PROXY=socks5h://...`, который pinned `httpx==0.27.2` не разбирает при создании mock-клиента. Первый environment-contaminated запуск дал 23 failures и 787 passes. Канонический offline suite выполнен с удалёнными только для test command proxy variables; network calls не выполнялись, production code ради harness не менялся.

## 12. Baseline commands и точные результаты

| Команда | Результат |
|---|---|
| `python --version` | Python 3.12.13 |
| `node --version` | v24.14.0 |
| `python -m pip check` | PASS |
| `python -m compileall -q app tests main.py` | PASS |
| `python -m ruff check .` | FAILED: 9 pre-existing findings |
| `node --check app/ui/static/app.js` | PASS |
| sanitized `python -m pytest --collect-only -q` | 810 collected |
| sanitized `python -m pytest -q` | 810 passed, 0 failed, 0 skipped, exit 0, 13.45s |

Baseline Ruff: one E741 in `app/direction.py`, one F841 in `app/main.py`, six E402 in historical tests and one F401 in a historical test. Они не связаны с scope итерации.

## 13. Подтверждённые defects/gaps

### BR-201-01 - HIGH - CONFIRMED DEFECT

- File/function before fix: `app/bybit_client.py::_get`, прежний блок `ret_code_raw = data.get("retCode", 0)` / `int(ret_code_raw or 0)`.
- Input: HTTP 200 payload с отсутствующим `retCode`, `null`, `false`, `0.5`, `""` или `[]`, содержащий внешне правдоподобный `result.list`.
- Data path: HTTP response -> `_get` -> public market method -> collector/recommendation data.
- Actual: zero-like malformed value заменялся/усекался в `0`; client возвращал `result` как successful response.
- Expected: common response control field должен присутствовать и быть exact integer; только `0` является success.
- Broken invariants: fail-closed, strict numeric semantics, malformed upstream response handling.
- Financial/trading impact: неподтверждённый result мог попасть в market-data контур и повлиять на freshness/features/recommendation; реальный order автоматически не создавался.
- Why tests missed it: прежние tests проверяли non-numeric string `"oops"`, но не missing/falsy/fractional zero-like shapes.
- Reproducer: iteration-201 targeted test на untouched production code.
- Fix: `strict_integer` + обязательное присутствие `retCode`; invalid shape повторяется через существующий response-shape retry.
- Residual: strict control validation не делает public REST атомарной execution truth.

### BR-201-02 - MEDIUM - CONFIRMED DEFECT

- Files/functions before fix: `app/bybit_client.py::get_kline`, `get_open_interest_page`.
- Input: `limit=True`, `limit=5.7`, boolean/fractional/negative timestamps или `start > end`.
- Actual: прямой `int()` создавал `1`, `5` или усечённый millisecond timestamp; отрицательные и inverted windows отправлялись upstream.
- Expected: exact-integer fields only; timestamps non-negative; time window ordered; invalid request rejected before network.
- Broken invariants: strict integer semantics, temporal correctness, fail-closed request boundary.
- Operational/model impact: collector мог получить неполное/другое окно истории, лишний empty response или неправильную pagination boundary, влияя на warm-up/freshness/data coverage.
- Why tests missed it: existing pagination tests проверяли forwarding корректных integer values, не malformed numeric types.
- Fix: shared request integer/window validators; limit range clamp сохранён только после exact parsing; exact integral floats remain compatible.
- Residual: interval and cursor shape hardening остаются отдельным ограниченным work package.

## 14. Неподтверждённые claims

- Новая directional TP/SL inversion не обнаружена.
- Private Bybit order submission отсутствует; OMS/EMS остаётся external executor requirement, не defect этого service.
- Прибыльность, live edge и production-ready auto-execution не заявляются.
- Реальная PostgreSQL integration не выполнялась без verified disposable DSN.

## 15. План исправления

1. Добавить один iteration-201 regression file в pristine/red и working copies.
2. Доказать red на untouched production code.
3. Применить canonical exact-integer parser к response control и request boundaries.
4. Сохранить retry/backoff и valid integer compatibility.
5. Проверить related Bybit/collector suites, полный suite, dual-DB compatibility, docs и release ZIP.

## 16. Фактический diff по файлам

Production:

- `app/bybit_client.py`;
- `app/main.py` - patch version only.

Tests:

- `tests/test_iteration201_bybit_response_request_integers.py`.

Docs:

- `README.md`;
- `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/MODULES.md`;
- `docs/SCENARIOS.md`;
- этот report.

Frontend, database and migrations: no changes.

## 17. Red -> green evidence

Red command:

`python -m pytest -q tests/test_iteration201_bybit_response_request_integers.py`

Red on untouched production code plus new tests: `21 failed, 2 passed, exit 1`.

Essential red evidence:

- `Failed: DID NOT RAISE RuntimeError` для missing/null/false/fractional `retCode`;
- malformed `retCode=false` вернул `lastPrice=1` вместо retry response `lastPrice=100`;
- `Failed: DID NOT RAISE ValueError` для boolean/fractional request values и inverted windows.

Green command: тот же targeted command.

Green: `23 passed, exit 0, 0.08s`; deterministic repeats: `23 passed` за 0.06s и 0.07s.

Related Bybit/collector suite: `84 passed, exit 0, 1.08s`.

## 18. Database/schema compatibility

- Schema change: none.
- `migrations/init.sql` and `migrations/init_postgres.sql`: unchanged.
- Fresh SQLite repeated bootstrap: PASS, 15 application tables (plus SQLite internal metadata).
- SQLite created by pristine 1.0.12 opened and initialized twice by 1.0.13: PASS, same 15 application tables.
- PostgreSQL translation/locking/dialect regression files: 21 passed, 0.43s.
- Live disposable PostgreSQL: SKIPPED, no safely verified DSN.
- User database action: none; standard backup-before-upgrade practice remains recommended.

## 19. API compatibility

No route, request/response field, recommendation status or operator action changed. OpenAPI reports version 1.0.13, 24 routes and 18 path templates. The Python public client now rejects inputs that violate its documented integer contract.

## 20. Config/env compatibility

No environment variable or default changed. No `.env` action is required.

## 21. Security boundary

- No secret or credential added or printed.
- No private Bybit order create/amend/cancel method added.
- Recommendation/audit-only boundary preserved.
- Invalid upstream control fields can no longer masquerade as success.
- Generated `.env`, DB, cache and test artifacts are excluded from release.

## 22. Post-check commands и точные результаты

| Команда | Результат |
|---|---|
| dependency check | PASS |
| `python -m compileall -q app tests main.py` | PASS |
| `python -m ruff check .` | FAILED: same 9 pre-existing findings; delta 0 |
| Ruff on changed client/new test | PASS |
| `node --check app/ui/static/app.js` | PASS |
| iteration-201 test, repeated | 23 passed on each run |
| related Bybit/collector suite | 84 passed |
| fresh SQLite + repeated bootstrap | PASS, 15 application tables |
| pristine-1.0.12 SQLite opened by 1.0.13 | PASS, 15 application tables |
| PostgreSQL translation/locking/dialect files | 21 passed |
| generated OpenAPI/version | PASS, 1.0.13, 24 routes, 18 paths |
| private-order static scan | 0 production hits |
| `python -m pytest --collect-only -q` | 833 collected |
| `python -m pytest -q` | 833 passed, 0 failed, 0 skipped, exit 0, 14.25s |
| release ZIP CRC/root/junk validation | PASS, one root, no traversal/symlink/duplicate/junk |
| re-extracted `compileall` / Node syntax / iteration-201 test | PASS / PASS / 23 passed |

## 23. Что не удалось проверить и почему

- Live PostgreSQL integration: no verified disposable DSN.
- Private Bybit/testnet account behavior: outside repository boundary; credentials не использовались.
- Real fill/funding/liquidation truth: no OMS/EMS/reconciliation layer.
- npm/yarn/ESLint/mypy: no project manifest/config.
- Ruff overall green: blocked by exactly the same 9 historical baseline findings; changed client and new test are clean.

## 24. Остаточные риски

1. Public REST responses remain non-atomic market snapshots, not authenticated account/execution truth.
2. Request `interval` and pagination cursor shape deserve a separate bounded audit; this iteration does not claim all Bybit query fields are fully validated.
3. Remaining direct-adapter exact-integer boundaries in feature/sentiment timestamps should be audited independently.
4. Proxy outcomes/calibration remain approximations and do not prove live edge.
5. External executor must re-check live wallet, account/position mode, current instrument filters, risk tier and fills.

## 25. Rollback procedure

Stop the service, restore the previous `1.0.12` code/ZIP and restart. No DB rollback or down-migration is required because schema and stored rows were not changed. Preserve the database backup for normal operational rollback discipline.

## 26. Рекомендуемый следующий work package

Audit feature/sentiment direct-adapter integer boundaries and remaining Bybit interval/cursor contracts. Require exact timestamps/counts, no fractional collision or boolean coercion, red -> green tests and unchanged fail-closed behavior.
