# Аудит целостности outcome/funding — v1.0.18

## 1. Название итерации

Outcome/funding integrity: fail-closed funding interval, kill-switch precedence и точная временная семантика proxy outcomes.

## 2. Входной ZIP

`bybit-reco-systems-main(2).zip`

## 3. SHA-256 входного ZIP

`301d24c04b01531a92914ad45a5cdc3e5951e8b5f4b78b68bb1d1f02197d8dc4`

## 4. Исходная версия

`1.0.17`, source of truth: `app/main.py`, параметр `FastAPI(..., version="1.0.17")`.

## 5. Новая версия

`1.0.18` — patch release без изменения публичного API, schema или configuration contract.

## 6. Project fingerprint

Fingerprint совпал. В архиве присутствуют обязательные root-файлы, `app/trading_semantics.py`, `app/grid_math.py`, dual persistence, frontend в `app/ui/static/`, SQLite/PostgreSQL init SQL и operator artifacts. Поддерживаемый scope остаётся `Bybit / category=linear / USDT perpetual / futures_grid`. Статический поиск не обнаружил private order create/amend/cancel endpoints или SDK-вызовов размещения ордеров. Проект остаётся recommendation/audit service, не OMS/EMS.

## 7. Цель итерации

После этой итерации система должна исключать три пути ложного улучшения торговой оценки:

1. malformed funding interval не становится подтверждённым расписанием через округление;
2. kill-switch breach не становится положительным outcome из-за отдельного TP-leg;
3. fractional/missing recommendation timestamps не создают искусственную chronology через `int()`.

Это подтверждается независимыми regression tests, targeted RED → GREEN и полным покрытием post-check collection.

## 8. Критерии приёмки

1. `funding_interval_min=720.5` получает source `fallback_8h_invalid_interval`, uncertain=true и два возможных события за canonical 12h horizon.
2. Любой lower/upper kill-switch breach делает directional proxy outcome неуспешным, даже если в горизонте был TP touch.
3. Fractional/missing `recommendations.ts` или `features_ref_ts` не создают `reco_outcomes`.
4. Invalid temporal row получает audit action `OUTCOME_SKIP_INVALID_TEMPORAL_FIELDS`.
5. Новый test падает на исходной версии и проходит на исправленной.
6. Релевантные старые outcome/funding tests проходят без ослабления expectations.
7. Полная post-check collection из 858 node IDs выполнена без пропусков и дубликатов.
8. API/schema/config/operator execution boundary не изменены.

## 9. Прочитанные источники

- `README.md`, `CHANGELOG.md`, `requirements*.txt`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports, включая signal durability, live spread, execution evidence и live-validation stop gate;
- `app/trading_semantics.py`, `grid_math.py`, `risk.py`, `recommender.py`, `calibration.py`, `outcomes.py`, `features.py`, `direction.py`, `regime.py`, `collector.py`, `bybit_client.py`, `db_backend.py`, релевантные части `db.py` и `main.py`, `settings.py`, `llm_review.py`, `security.py`;
- `app/ui/static/app.js`, `tests/conftest.py` и релевантные regression suites;
- приложенный проектно-специфичный iteration protocol редакции 10 июля 2026 г.

## 10. Карта затронутого data flow

`Bybit ticker/instrument metadata` → `recommender._estimate_cost_model()` → funding event count / expected funding bps → score/economic gate → persisted recommendation cost model.

`recommendation + params + OHLCV horizon` → `outcomes.compute_outcomes_once()` → `_grid_outcome()` → `reco_outcomes(success, ret_proxy, label_available_ts)` → calibration dataset.

Изменения не затрагивают order execution, bot materialization, trade/evidence ingestion, DB schema, frontend API parsing или background topology.

## 11. Baseline environment

- Python: `3.13.5`.
- Node: `v22.16.0`.
- Input root: `bybit-reco-systems-main`.
- Production Python files: 23.
- Test files: 149 до итерации.
- Docs: 29.
- Frontend files: 3.
- Migration SQL files: 2.
- Максимальный существующий iteration: 205; текущий: 206.
- DB backends: SQLite и PostgreSQL/psycopg translation layer.

## 12. Baseline commands и результаты

- `python -m pip check` — **FAILED / environment limitation**: установленный `moviepy 2.2.1` требует `pillow<12`, окружение содержит `pillow 12.2.0`. Конфликт не относится к зависимостям или diff проекта.
- `python -m compileall -q app tests main.py` — **PASSED**.
- `python -m ruff check .` — **UNAVAILABLE**: `No module named ruff`.
- `node --check app/ui/static/app.js` — **PASSED**.
- `python -m pytest --collect-only -q` — **855 collected**.
- `python -m pytest -q` — **855 passed in 24.01s**, exit code 0.

Зелёный baseline не доказывал корректность торговой семантики: прежние tests не проверяли конфликт TP touch ↔ kill-switch breach и прямой fractional persistence input.

## 13. Подтверждённые defects/gaps

### OFI-206-01 — HIGH — CONFIRMED DEFECT

- Файл/строки до исправления: `app/recommender.py`, `_estimate_cost_model()`, funding interval branch.
- Input: `funding_interval_min=720.5`, long, `funding_rate=0.001`, unknown next funding time, 12h horizon.
- Фактическое поведение: `_finite_or_none()` принимал дробь, `int(round(...))` превращал её в 720 минут и объявлял source `ticker_or_instrument_info`.
- Ожидаемое поведение: exchange schedule — exact-integer field; malformed evidence не должно становиться подтверждённым расписанием.
- Нарушенный инвариант: strict numeric semantics и fail-closed funding economics.
- Финансовое влияние: в reproducer количество возможных funding events уменьшалось с 2 до 1, adverse funding с 20 bps до 10 bps; net edge/score могли быть завышены.
- Почему tests не поймали: exact-integer tests покрывали collector/execution boundaries, но не этот внутренний cost-model branch.
- Fix: `strict_integer()`, раздельная provenance missing/invalid, conservative 8h fallback.
- Остаточный риск: при отсутствующем `nextFundingTime` event count остаётся консервативной оценкой, а не фактом.

### OFI-206-02 — HIGH — CONFIRMED DEFECT

- Файл/строки: `app/outcomes.py:499-525`, `_grid_outcome()`.
- Input: directional long grid; ранний TP touch; затем lower kill-switch breach в том же horizon.
- Фактическое поведение: `tp_success` входил в success через OR и мог вернуть `success=1`; `net_proxy=max(..., tp_realized_net)` стирал отрицательное значение.
- Ожидаемое поведение: kill-switch — terminal risk failure; остановленный grid не является успешным whole-bot outcome из-за одной прибыльной ноги.
- Нарушенный инвариант: directional/grid economics, outcome integrity и calibration label correctness.
- Trading/model impact: ложноположительная метка могла повышать calibrated confidence и укреплять ошибочную рекомендационную логику.
- Почему tests не поймали: существующие tests отдельно проверяли TP economics и kill-switch penalties, но не их конфликт в одном horizon.
- Fix: horizon-wide `kill_switch_intact`; TP success разрешён только без breach; ambiguous same-candle path разрешается fail-closed в пользу kill-switch.
- Остаточный риск: OHLC bars не определяют intrabar fill sequence; исправление сознательно консервативно.

### OFI-206-03 — MEDIUM — CONFIRMED DEFECT

- Файл/строки: `app/outcomes.py:565-583`, `compute_outcomes_once()`.
- Input: legacy/direct SQLite row с fractional `recommendations.ts` и `features_ref_ts`.
- Фактическое поведение: `int()` усекал значения, создавая синтетическую chronology и допуская row в labeling.
- Ожидаемое поведение: exact integer timestamps; malformed/missing chronology исключается fail-closed.
- Нарушенный инвариант: temporal correctness и calibration data integrity.
- Model impact: неверный entry candle и maturity/order chronology могли попасть в proxy labels.
- Почему tests не поймали: upstream writers уже валидируют значения; не был покрыт прямой/legacy poisoned persistence row.
- Fix: `strict_integer()` для обоих полей; skip + audit diagnostic.
- Остаточный риск: существующие уже записанные outcomes автоматически не пересчитываются этой patch-итерацией.

### OFI-206-04 — LOW — CONFIRMED GAP / RELEASE HYGIENE

Runtime tests/import создают `data/app.db` и `data/app.runtime_locks.sqlite` в working tree. Release builder уже исключает `.db`/`.sqlite`; финальный ZIP дополнительно проверяется на отсутствие runtime DB. Это не production-code defect и не требовало изменения builder.

## 14. Неподтверждённые claims

- Не подтверждено, что проект «математически невозможен» или обязан иметь отрицательное ожидание при любых market regimes.
- Не подтверждена прибыльность. В ZIP нет достаточного независимого live dataset с фактическими fills, fees, funding, stopped-bot cohorts и comparator/no-trade baseline.
- Proxy outcomes и calibrated confidence не являются доказательством live edge.
- Нельзя доказать, что исправлены все торговые ошибки; итерация закрывает только воспроизведённый связный work package.

## 15. План исправления

1. Зафиксировать независимые математические expectations.
2. Добавить один iteration-206 regression file к pristine/red copy.
3. Получить targeted RED на исходном production code.
4. Локально исправить funding interval parsing и outcome semantics.
5. Получить targeted GREEN и повторяемость.
6. Прогнать релевантную suite и полную collection.
7. Синхронизировать version/docs/changelog.
8. Собрать и повторно проверить clean release ZIP.

## 16. Фактический diff по файлам

### Production

- `app/recommender.py` — exact-integer funding interval и invalid/missing provenance.
- `app/outcomes.py` — kill-switch precedence; exact recommendation temporal fields.
- `app/main.py` — version `1.0.18`.

### Tests

- `tests/test_iteration206_outcome_funding_integrity.py` — 3 independent regression tests.

### Frontend

Нет изменений.

### Database/migrations

Нет изменений schema/runtime bootstrap/init SQL.

### Docs

- `README.md`;
- `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- этот audit report.

Binary operator DOCX/PDF/PNG сохранены. Их существующий contract уже говорит, что funding unavailable блокирует запуск, kill-switch является внешним exit и live edge не доказан; последовательность operator actions не изменилась.

## 17. Red → green evidence

RED command:

```bash
python -m pytest -q tests/test_iteration206_outcome_funding_integrity.py
```

RED result на pristine production code + только новый test:

```text
FFF                                                                      [100%]
3 failed in 0.68s
```

Существенные differences:

- expected `fallback_8h_invalid_interval`, got `ticker_or_instrument_info`;
- expected `success == 0`, got `1`;
- expected `processed == 0`, got `1`.

GREEN command — тот же:

```bash
python -m pytest -q tests/test_iteration206_outcome_funding_integrity.py
```

GREEN result:

```text
...                                                                      [100%]
3 passed in 0.48s
```

Детерминированный повтор: `3 passed` и `3 passed`.

Relevant suite:

```text
117 passed in 2.87s
```

## 18. Database/schema compatibility

Schema change отсутствует. `app/db.py`, `migrations/init.sql`, `migrations/init_postgres.sql` не изменены. Fresh SQLite bootstrap создал 17 tables, включая `recommendations`, `reco_outcomes`, `execution_evidence`. PostgreSQL translation/locking/red-team suite: `21 passed in 1.70s`. Live PostgreSQL integration не запускался без явно disposable DSN.

Invalid legacy recommendation rows остаются в audit storage, но не получают новые outcomes. Уже созданные ранее proxy outcomes не удаляются и не переписываются автоматически, чтобы не нарушать immutable audit history.

## 19. API compatibility

Маршруты и JSON field names не изменены. Import check: version `1.0.18`, 27 route objects, 26 unique paths, 30 method bindings. Private order endpoints отсутствуют.

## 20. Config/env compatibility

Новых или изменённых environment variables нет. `.env.example` не изменён. Пользовательских config actions не требуется.

## 21. Security boundary

- Не добавлены Bybit private/order methods.
- Не использовались credentials или network smoke tests.
- Recommendation/audit-only boundary сохранён.
- Runtime DB, caches и bytecode исключаются release builder.
- Admin/security model и sensitive execution-evidence API не изменены.

## 22. Post-check commands и результаты

- `python -m compileall -q app tests main.py` — **PASSED**.
- `node --check app/ui/static/app.js` — **PASSED**.
- `python -m ruff check .` — **UNAVAILABLE**, модуль не установлен.
- `python -m pip check` — **FAILED**, тот же внешний MoviePy/Pillow conflict.
- Targeted iteration-206 — **3 passed**, повторён детерминированно.
- Relevant outcome/funding suite — **117 passed**.
- PostgreSQL dialect/locking suite — **21 passed**.
- `pytest --collect-only -q` — **858 collected**.
- Монолитный `pytest -q` — **TIMED OUT/STALL** после 75% без failure summary; result не засчитан как success.
- Exhaustive deterministic batches — **6 × 143 passed = 858 passed**. Проверка множества: collected=858, unique=858, batched=858, unique=858, missing=0, extra=0.
- FastAPI import/version/routes — **PASSED**.
- Fresh SQLite schema — **PASSED**, 17 tables.
- Private order endpoint scan — **PASSED**, 0 hits.
- Required operator artifact presence — **PASSED**.

## 23. Что не удалось проверить и почему

- Live PostgreSQL: отсутствует доказанно disposable test DSN.
- Ruff: пакет недоступен в окружении; зависимости не изменялись и сеть не использовалась.
- Единый монолитный post-suite не выдал summary из-за stall/timeout. Полнота компенсирована protocol-compliant exhaustive batches с доказанным exact union.
- Live profitability/negative expectancy: отсутствует необходимый execution-grade dataset и заранее определённый validation protocol.
- Точная intrabar последовательность TP/kill: OHLC proxy её не содержит; выбран conservative kill-switch precedence.

## 24. Остаточные риски

1. Основной риск проекта — не доказанная live edge, а не обнаруженная математическая невозможность.
2. Heuristic score и proxy calibration могут оставаться regime-dependent и не переноситься на live fills.
3. Grid mechanics несут tail/trend risk; kill-switch ограничивает, но не устраняет gap/slippage/liquidation risk.
4. Старые ошибочные proxy labels не пересчитаны автоматически. Для model validation требуется versioned cohort/filter или отдельный controlled relabel procedure.
5. Exact execution evidence остаётся необходимым для claims о PnL, expectancy и calibration reliability.

## 25. Rollback procedure

Вернуть `app/recommender.py`, `app/outcomes.py`, `app/main.py`, новый iteration test и перечисленные Markdown docs к v1.0.17. DB rollback не требуется. Не удалять существующие audit/evidence/outcome rows. После rollback повторить compileall, Node syntax и test suite.

## 26. Рекомендуемый следующий work package

Evidence-grade strategy validation, не очередной локальный formula patch:

- immutable dataset только из stopped bots с exact fills/fees/funding;
- chronological walk-forward по explicit `model_version` и regime cohorts;
- no-trade и simple comparator baselines;
- realised PnL/expectancy, drawdown, tail loss, turnover и calibration reliability;
- block bootstrap confidence intervals и predefined stop/go criteria;
- отдельная проверка neutral/long/short grid inventory paths;
- исключение или controlled relabel старых outcomes, созданных до v1.0.18.

Только этот пакет способен подтвердить или опровергнуть экономическую состоятельность стратегии. Текущая итерация подтверждает, что архитектура recommendation/audit сервиса не содержит найденного логического противоречия, но не подтверждает прибыльность.
