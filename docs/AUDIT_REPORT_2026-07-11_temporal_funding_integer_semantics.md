# Итерация 200: exact temporal/funding integer semantics

## 1. Название итерации

Bybit Recommender v1.0.12 - fail-closed exact-integer semantics для market time, funding schedule и purged calibration.

## 2. Входной ZIP

`bybit-reco-systems-main(2).zip`

## 3. SHA-256 входного ZIP

`2146d457d91375200537db354f218fe6cc73a9f0012c537f86f9fdb082ccaa13`

## 4. Исходная версия

`1.0.11`, source of truth: `FastAPI(..., version="1.0.11")` в `app/main.py`.

## 5. Новая версия

`1.0.12` (patch): исправлены дефекты без изменения публичного API, schema, config или operator lifecycle.

## 6. Project fingerprint

Fingerprint совпал:

- README описывает Bybit Recommender;
- единственный штатный `bot_type` - `futures_grid`;
- scope - Bybit `category=linear`, USDT perpetual;
- recommendation/audit-only, не OMS/EMS;
- SQLite и PostgreSQL сохранены;
- FastAPI создаётся в `app/main.py`;
- frontend находится в `app/ui/static/`;
- canonical directional model находится в `app/trading_semantics.py`;
- private order create/amend/cancel endpoints в production code отсутствуют.

## 7. Цель итерации

После этой итерации система должна отклонять fractional/boolean/non-finite значения в целочисленных market-time, funding/OI, label-horizon и event-count полях, не превращать их через `int()`/rounding в исполнимые данные и сохранять консервативную funding/calibration семантику.

## 8. Критерии приёмки

1. Fractional Bybit OHLCV/ticker/funding/OI timestamps не становятся валидными integer timestamps.
2. Fractional funding/OI row не может перезаписать валидный persistence key после truncation.
3. `fundingIntervalHour` принимает только whole hours, instruments-info `fundingInterval` - exact integer minutes.
4. Fractional decision/label-availability timestamps не допускаются в purged OOF training.
5. Fractional label horizon использует canonical 12h grid horizon; malformed next-funding schedule получает conservative unknown count.
6. Funding cashflow принимает только exact integer event count.
7. Все исходные 800 тестов остаются зелёными; новые regression tests доказаны red -> green.
8. Схема, migrations, API, env и recommendation/OMS boundary не меняются.

## 9. Прочитанные источники

- `README.md`, `CHANGELOG.md`, requirements, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- пять последних iteration reports: 195-199;
- существующий `docs/AUDIT_PROMPT_2026-06-18_UPDATED.md`;
- canonical `app/trading_semantics.py` и релевантные части grid/risk/recommender/calibration/outcomes/features/direction/regime/collector/Bybit/DB/API/frontend модулей;
- `tests/conftest.py` и связанные regression suites;
- официальный Bybit V5: `market/tickers`, `market/instrument`, `market/open-interest`.

Официальный контракт подтверждает: `fundingIntervalHour` поддерживает whole hours; `fundingInterval` задаётся integer minutes; `nextFundingTime` и open-interest `timestamp` передаются как millisecond timestamps.

## 10. Карта затронутого data flow

`Bybit response -> app/bybit_client.py -> app/collector.py -> app/db.py -> recommender execution funding / calibration OOF`.

Дополнительный чистый helper path: `app/grid_math.py::funding_cashflow_usdt`.

Directional TP/SL/PnL mapping, frontend rendering и operator action lifecycle не затрагивались.

## 11. Baseline environment

- Python 3.12.13;
- Node v24.14.0;
- отдельный временный virtualenv вне project root;
- runtime/dev dependencies установлены строго из `requirements.txt` и `requirements-dev.txt`;
- dependency check: compatible;
- входной ZIP: 210 entries, CRC PASS, без absolute path, traversal, symlink, duplicate/conflict или nested archive.

Harness экспортировал `ALL_PROXY=socks5h://...`, который pinned `httpx==0.27.2` не умеет разбирать при создании mock-клиента. Первый pytest дал 22 environment-induced failures и 778 passes. Для канонического offline suite proxy variables были удалены только из окружения test command; сетевые вызовы не выполнялись, production code не менялся ради harness.

## 12. Baseline commands и точные результаты

| Команда | Результат |
|---|---|
| `python --version` | Python 3.12.13 |
| `node --version` | v24.14.0 |
| dependency check | PASS, 41 packages compatible |
| `python -m compileall -q app tests main.py` | PASS |
| `python -m ruff check .` | FAILED: 9 pre-existing findings |
| `node --check app/ui/static/app.js` | PASS |
| sanitized offline `python -m pytest --collect-only -q` | 800 collected |
| sanitized offline `python -m pytest -q` | 800 passed, 0 failed, 0 skipped, exit 0, 15.37s |

Baseline ruff: one E741 in `app/direction.py`, one F841 in `app/main.py`, six E402 in historical tests and one F401 in a historical test. Они не связаны с выбранным scope.

## 13. Подтверждённые defects/gaps

### BR-200-01 - HIGH - CONFIRMED DEFECT

- Files/functions: `app/bybit_client.py:get_funding_rate/get_open_interest_page`; `app/collector.py:_sanitize_ohlcv_row/_remote_ticker_ts/_extract_funding_row`; `app/db.py:_normalize_funding_row/_normalize_open_interest_row`.
- Input: numeric fractional timestamp `1700000000.75` или millisecond analogue.
- Actual: `int()` делал `1700000000`; malformed row получал valid key и через upsert заменял correct funding `0.0001` значением `0.0099`.
- Expected: invalid exact-integer input is dropped; valid row remains immutable at its logical key.
- Broken invariants: strict numeric semantics, temporal correctness, audit/data integrity, fail-closed.
- Impact: funding/OI freshness, OI signal, cost model и operator decision могли использовать malformed replacement row.
- Existing tests covered booleans/non-finite values but not fractional numeric timestamps.
- Fix: shared `strict_integer` applied before client normalization, collector materialization and persistence upsert.
- Residual: legacy physical rows are skipped, not automatically deleted.

### BR-200-02 - HIGH - CONFIRMED DEFECT

- Files/functions: `app/bybit_client.py:get_funding_rate`; `app/collector.py:_extract_funding_row/_funding_interval_min_from_instrument_info`; `app/main.py:_execution_label_horizon_sec/_funding_events_until_horizon/_execution_funding_blocks`; `app/outcomes.py:_resolve_effective_horizon`; `app/db.py:_backfill_effective_horizon_sec`.
- Input: `fundingIntervalHour=8.5`, `fundingInterval=480.5`, `horizon_sec=21600.5`, `next_funding_ts=2000.5`.
- Actual: rounding/truncation manufactured a plausible schedule, shortened malformed grid horizon to 21600 seconds and proved one funding event where unknown-schedule conservative logic requires two in the reproducer.
- Expected: exact integers only; invalid schedule remains unknown, 12h canonical grid horizon applies, conservative unknown event count is used or execution blocks for missing interval.
- Impact: adverse carry could be understated and a costed grid could look safer than the malformed evidence supports.
- Fix: exact whole-hour/integer-minute/second parsing and conservative unknown-schedule behavior.

### BR-200-03 - MEDIUM - CONFIRMED DEFECT

- File/function: `app/calibration.py:_purged_train_indices/fit_logreg`.
- Input: fractional recommendation or `label_available_ts`.
- Actual: `int()` manufactured an integer chronology and admitted the row into temporal processing.
- Expected: malformed temporal boundary excluded; no historical OOF claim without exact observability time.
- Impact: model/data integrity and auditability of purged validation; not direct order execution.
- Fix: exact-integer validation for decision, success and label availability fields.

### BR-200-04 - MEDIUM - CONFIRMED GAP

- File/function: `app/grid_math.py:funding_cashflow_usdt`.
- Input: `events=1.9`.
- Actual: helper charged exactly one event through truncation.
- Expected: fractional event count is invalid and produces zero/unknown helper output.
- Impact: future consumers could silently understate or alter funding cashflow.
- Fix: event count uses `strict_integer`; exact `2.0` remains compatible.

## 14. Неподтверждённые claims

- Новая directional TP/SL inversion не обнаружена.
- Private Bybit order submission в проекте отсутствует; отсутствие OMS/EMS остаётся documented boundary, не defect текущего service.
- Прибыльность, live edge и production-ready auto-execution не заявляются.
- Реальная PostgreSQL integration не выполнялась без verified disposable DSN.

## 15. План исправления

1. Добавить один iteration-200 regression file в pristine/red и working copies.
2. Доказать red на исходном production code.
3. Применить shared exact-integer parser на внешних и persistence boundaries.
4. Сохранить conservative fallback для malformed schedule/horizon.
5. Проверить related suite, полный suite, dual-DB translation, docs и release archive.

## 16. Фактический diff по файлам

Production:

- `app/bybit_client.py`;
- `app/collector.py`;
- `app/db.py`;
- `app/calibration.py`;
- `app/outcomes.py`;
- `app/grid_math.py`;
- `app/main.py` (fix + version).

Tests: `tests/test_iteration200_temporal_funding_integer_semantics.py`.

Docs: `README.md`, `CHANGELOG.md`, `docs/TRADING_LOGIC.md`, `docs/KNOWN_RISKS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`, этот report.

Frontend and migrations: no changes.

## 17. Red -> green evidence

Red command:

`python -m pytest -q tests/test_iteration200_temporal_funding_integer_semantics.py`

Red result on untouched production code plus the new test: `8 failed, 2 passed, exit 1`.

Essential red lines:

- persisted funding `0.0099 != 0.0001`;
- purged indices `[0] != []`;
- execution horizon `21600 != 43200`;
- funding events `1 != 2`;
- fractional funding cashflow `1.000 != 0`.

Green command: same command. Green result: `10 passed, exit 0, 0.54s`; two final repeat runs were deterministic at `10 passed, 0.37s` each.

Related suite: `75 passed, exit 0, 0.86s`.

## 18. Database/schema compatibility

- Schema change: none.
- `migrations/init.sql` and `migrations/init_postgres.sql`: unchanged.
- Fresh SQLite bootstrap created 16 tables and a second `init_db` call completed successfully.
- A database created by pristine `1.0.11` was opened by `1.0.12`; two `init_db` calls completed successfully with the same 16-table schema. No upgrade is required.
- Normalization is shared before SQLite/PostgreSQL writes; 18 PostgreSQL translation/locking/dialect tests remain green.
- Live disposable PostgreSQL: SKIPPED (no safely verified DSN).

## 19. API compatibility

No route, request/response field, status or operator action changed. Malformed fields that never satisfied the documented integer contract now remain unavailable/fail-closed.

## 20. Config/env compatibility

No environment variable or default changed. No user config action is required.

## 21. Security boundary

- No secret/credential added or printed.
- No `.env`, database, log or model artifact is shipped.
- No private order create/amend/cancel method added.
- Recommendation/audit-only boundary preserved.
- The injected harness proxy issue is documented as environment behavior, not hidden as a product regression.

## 22. Post-check commands и точные результаты

| Команда | Результат |
|---|---|
| dependency check | PASS |
| `python -m compileall -q app tests main.py` | PASS |
| `python -m ruff check .` | FAILED: same 9 pre-existing findings; delta 0 |
| `node --check app/ui/static/app.js` | PASS |
| fresh SQLite + repeated bootstrap | PASS, 16 tables |
| pristine-1.0.11 SQLite opened twice by 1.0.12 | PASS, 16 tables |
| PostgreSQL translation/locking/dialect tests | 18 passed, 0.35s |
| generated OpenAPI/version check | PASS, version 1.0.12, 24 routes |
| iteration-200 test repeated | 10 passed |
| related suite | 75 passed |
| `python -m pytest --collect-only -q` | 810 collected |
| `python -m pytest -q` | 810 passed, 0 failed, 0 skipped, exit 0, 14.29s |

Final release-repack checks are recorded in the delivery response with the archive SHA-256.

## 23. Что не удалось проверить и почему

- Live PostgreSQL integration: no verified disposable DSN.
- Private Bybit/testnet account behavior: outside repository boundary; no credentials used.
- Real fill/funding/liquidation truth: no OMS/EMS/reconciliation layer.
- npm/yarn/ESLint/mypy: no project manifest/config.
- Ruff overall green: blocked by exactly 9 historical findings already present at baseline; no new finding was introduced.

## 24. Остаточные риски

1. Legacy malformed temporal rows are ignored, not deleted.
2. Public REST snapshot is not authenticated account/execution truth.
3. Proxy outcomes/calibration remain approximations and do not prove live edge.
4. Exact wallet, position mode, risk tier, filters, fills and funding must be rechecked by an external executor.
5. Other non-trading telemetry/direct-adapter integer conversions should be audited as a separate bounded package; this report does not claim every repository integer conversion was eliminated.

## 25. Rollback procedure

Stop the service, restore the previous `1.0.11` code/ZIP and restart. No DB rollback or down-migration is required because schema and stored rows were not rewritten. Keep a database backup before any independent forensic cleanup.

## 26. Рекомендуемый следующий work package

Audit remaining direct-adapter integer boundaries outside this funding/OI package: feature/sentiment timestamps, pagination/request limits and Bybit response control integers. Require exact-integer semantics, red -> green tests and no weakening of current fail-closed gates.
