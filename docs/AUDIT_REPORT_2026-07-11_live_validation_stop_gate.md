# Аудит замкнутого контура live-validation и stop gate — v1.0.17

## 1. Название итерации

Exact-evidence negative-expectancy stop gate.

## 2. Входной ZIP

`bybit-reco-systems-main(1)(2).zip`

## 3. SHA-256 входного ZIP

`40dd09c02142f9f2764f47176a15fe608ec4f8d582e01ac09a0add34f39602d5`

## 4. Исходная версия

`1.0.16`, source of truth: `FastAPI(..., version="1.0.16")` в `app/main.py`.

## 5. Новая версия

`1.0.17` — patch: additive API diagnostics и fail-closed execution-time control без изменения DB schema.

## 6. Project fingerprint

PASS. Найдены все обязательные файлы и устойчивые признаки Bybit Recommender:

- recommendation/audit service, не OMS/EMS;
- `futures_grid`;
- Bybit `category=linear`, USDT perpetual;
- SQLite + PostgreSQL persistence;
- FastAPI в `app/main.py`;
- frontend в `app/ui/static/`;
- canonical directional semantics в `app/trading_semantics.py`.

Статический поиск не нашёл private order endpoints `/v5/order/create|amend|cancel` и batch-вариантов.

## 7. Цель итерации

После итерации operator execution lifecycle не должен продолжаться механически, если exact execution evidence уже показывает устойчивую отрицательную realised-экономику для той же версии модели, символа и направления. Это подтверждается red→green regression tests и полным suite.

Исправление не утверждает прибыльность и не превращает proxy outcomes в live edge.

## 8. Критерии приёмки

1. Пять последних независимых stopped bots одного `(model_version, symbol, direction)` с `realized_pnl_net < 0` блокируют новый `executed`.
2. Direction cohort из минимум 8 независимых bots блокируется, если total и median net PnL отрицательны и positive-bot rate < 50%.
3. Аналогичные symbol/portfolio gates требуют 12/20 независимых observations.
4. Один `publication_root_rec_id` учитывается один раз.
5. Long losses не блокируют short до symbol-level threshold.
6. Explicit новая `model_version` не наследует отрицательный cohort старой версии.
7. В расчёт входят только stopped bots с exact execution events; malformed/non-finite и legacy `/trades` не используются.
8. Full regression suite проходит, release ZIP повторно распаковывается и проверяется.

## 9. Прочитанные источники

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports, включая execution-evidence и signal-durability итерации;
- `app/main.py`, `db.py`, `risk.py`, `recommender.py`, `trading_semantics.py`, `grid_math.py`, `outcomes.py`, `calibration.py`, `features.py`, `direction.py`, `regime.py`, `bybit_client.py`, `db_backend.py`, `settings.py`;
- `app/ui/static/app.js`;
- релевантные regression/API/PostgreSQL tests;
- приложенный адаптированный итерационный протокол от 10 июля 2026 г.

## 10. Карта затронутого data flow

`external read-only execution adapter -> execution_evidence -> get_bot_execution_summary -> list_live_validation_records -> model-version/direction/symbol/portfolio cohorts -> _execution_preflight -> operator action executed / 409 block -> decision_log`

Recommendation publication не блокируется: идея остаётся доступна для аудита. Блок применяется непосредственно перед materialization нового `bot_instance`.

## 11. Baseline environment

- Python `3.13.5`;
- Node `v22.16.0`;
- input archive: 224 entries, один root `bybit-reco-systems-main`;
- production Python files: 24;
- test files: 148;
- docs files: 28;
- frontend files: 3;
- migration SQL files: 2;
- максимальный существующий iteration: 204;
- DB backends: SQLite и PostgreSQL compatibility layer.

Archive traversal/absolute path/symlink/duplicate/nested-archive checks: PASS. `unzip -t`: PASS.

## 12. Baseline commands и результаты

| Проверка | Результат |
|---|---|
| `python --version` | Python 3.13.5 |
| `node --version` | v22.16.0 |
| `python -m pip check` | FAILED: environment conflict `moviepy 2.2.1` requires `pillow<12`, installed `12.2.0` |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: module `ruff` not installed |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest -vv` | 849 passed in 34.70 s, exit summary green |
| `pytest --collect-only -q` | 849 collected |

Baseline pytest summary was complete; an outer harness process required cleanup after the summary was emitted. No test failure was hidden.

## 13. Подтверждённые defects/gaps

### BR-205-01 — HIGH — CONFIRMED DEFECT

- **Файлы до исправления:** `app/main.py::_execution_preflight`, `_materialize_bot_from_rec`; `app/db.py::list_live_validation_records`.
- **Фактический input:** несколько independent stopped bots с immutable Bybit execution evidence и отрицательным `gross_pnl + funding - fee`, затем operator action `executed` для новой рекомендации того же signal cohort.
- **Фактическое поведение:** `/api/v1/validation/live-evidence` показывал отрицательные total/mean/positive rate только descriptive-only; execution preflight не читал эти данные и мог создать следующий audit `bot_instance`.
- **Ожидаемое поведение:** persistent negative exact evidence должно быть fail-closed stop condition до следующего запуска, не post-hoc информацией.
- **Нарушенный инвариант:** fail-closed risk/control loop; documented requirement прекратить live use при продолжающейся отрицательной expectancy.
- **Финансовое/trading влияние:** система могла повторять заведомо убыточный режим после появления достаточного фактического evidence.
- **Почему tests не поймали:** предыдущие tests проверяли ledger, net-PnL formula, idempotency, cooldown и descriptive export по отдельности, но не проверяли feedback path `realised evidence -> next execution permission`.
- **Команда воспроизведения:** `python -m pytest -q tests/test_iteration205_live_validation_stop_gate.py` на исходном коде.
- **RED:** 4 failed / 2 passed; существенные строки: `LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY ... not in set()`, `LIVE_VALIDATION_DIRECTION_LOSS_STREAK ... not in set()`, отсутствует `strategy_health`.
- **Исправление:** model-version-scoped independent cohorts, directional separation, loss-streak and negative-cohort blockers, preflight/decision-log/API integration.
- **GREEN:** 6 passed.
- **Остаточный риск:** gate не может работать без полного exact evidence; пороги являются operational stop rules, а не статистическим доказательством alpha.

## 14. Неподтверждённые claims

- Не подтверждено, что стратегия прибыльна или принципиально неприбыльна: архив не содержит пользовательской исторической БД/fills для независимой оценки edge.
- Не подтверждено, что все прошлые убытки вызваны кодовыми ошибками. Возможна фундаментально отрицательная стратегия после fees/spread/funding.
- Proxy outcomes, score и calibrated confidence не считаются доказательством live profitability.
- Не заявляется, что найдены все дефекты.

## 15. План исправления

1. Добавить regression tests на отсутствующий feedback loop.
2. Доказать red на pristine code.
3. Расширить validation rows полями venue/bot_type/model_version.
4. Сформировать newest-first exact-evidence cohorts с publication-root deduplication.
5. Разделить direction, symbol и portfolio stop levels.
6. Включить gate в execution preflight до bot materialization.
7. Экспортировать те же policy/metrics через admin validation API.
8. Синхронизировать operator docs и release artifacts.

## 16. Фактический diff по файлам

### Production

- `app/main.py` — strategy-health aggregation/gates, preflight and audit-state integration, version `1.0.17`, additive validation query fields.
- `app/db.py` — validation records now expose venue, bot_type and model_version.

### Tests

- `tests/test_iteration205_live_validation_stop_gate.py` — 6 deterministic tests.

### Frontend

- Production JS/HTML/CSS не изменялись; existing API error rendering получает новые block codes.

### Database/migrations

- Schema не изменена; `migrations/init.sql` и `init_postgres.sql` не изменялись.

### Docs/operator artifacts

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- `docs/instrukciya_operatora_bybit_recommender.pdf`;
- `how_to_trade.png`.

## 17. Red → green evidence

### RED

```bash
python -m pytest -q tests/test_iteration205_live_validation_stop_gate.py
```

Result on source code plus new tests: `4 failed, 2 passed`.

### GREEN

```bash
python -m pytest -q tests/test_iteration205_live_validation_stop_gate.py
```

Result after production fix: `6 passed in 1.88s`.

Independent oracles use explicit realised PnL arrays and expected block codes; production output is not reused as the expected value.

## 18. Database/schema compatibility

- No schema change.
- Fresh SQLite bootstrap: PASS, 17 tables, `execution_evidence` present.
- Existing execution-evidence schema tests: PASS.
- PostgreSQL translation/locking/release subset: 18 passed.
- Live PostgreSQL integration: SKIPPED — no explicitly disposable test DSN was provided.
- No user migration action required.

## 19. API compatibility

Backward-compatible additive change to `GET /api/v1/validation/live-evidence`:

- optional query filters `symbol`, `direction`, `model_version`, `venue`, `bot_type`;
- additive `strategy_health` response object.

Existing fields remain. Mutating route set is unchanged. Operator `executed` may now return 409 with `LIVE_VALIDATION_*` where old code incorrectly allowed continuation.

## 20. Config/env compatibility

No new environment variables. No `.env` action required. Stop thresholds are code-defined fail-closed policy to avoid silent weakening through malformed/operator config.

## 21. Security boundary

- No private Bybit order endpoints added.
- Evidence read remains protected by `ADMIN_API_KEY`/loopback security model.
- No credentials, `.env`, runtime DB or model artifacts are included in the release.
- Project remains recommendation/audit-only.

## 22. Post-check commands и результаты

| Проверка | Результат |
|---|---|
| targeted iteration 205 | 6 passed |
| execution-evidence + iteration 205 | 15 passed |
| PostgreSQL dialect/locking subset | 18 passed |
| full `pytest -q` | 855 passed in final run (see release verification log) |
| `compileall` | PASSED |
| Node syntax | PASSED |
| `pip check` | same environment conflict: MoviePy/Pillow |
| Ruff | UNAVAILABLE |
| operator DOCX render | 4 pages, visually checked |
| operator PDF preflight | 4 pages, openable, non-encrypted, non-scanned |
| private order endpoint scan | 0 hits |

The exact full-suite count is updated at release time after the final test collection. If this report is read from an intermediate working tree, the authoritative release verification section and ZIP re-extraction checks prevail.

## 23. Что не удалось проверить

- Live profitability/edge: no user execution dataset was included in the ZIP.
- Live Bybit private account/order state: intentionally outside project boundary.
- Live PostgreSQL integration: no verified disposable DSN.
- Ruff: tool unavailable in the environment.
- Environment-wide `pip check`: pre-existing unrelated MoviePy/Pillow conflict.

## 24. Остаточные риски

1. **Live edge remains unproven.** Gate stops persistent losses; it does not prove non-blocked cohorts profitable.
2. **Evidence completeness.** Missing fills, fees or funding can make the gate blind or biased.
3. **Small-sample policy.** 5/8/12/20 are conservative operational thresholds, not a p-value or confidence guarantee.
4. **Model version discipline.** Any material signal/target/economics change must bump `model_version`; otherwise old and new results remain in one cohort.
5. **External executor risk.** Unrealised inventory, account liquidation, open orders and exchange reconciliation remain outside this repository.
6. **Hard stop recovery.** A blocked cohort requires model/evidence investigation and an explicit new model version where the strategy truly changed; merely hiding or deleting losing rows would corrupt the audit trail.

## 25. Rollback procedure

No DB rollback is required. Replace v1.0.17 code/artifacts with the original v1.0.16 release or revert the listed production/docs/test files. Existing evidence rows remain compatible. Do not delete execution evidence to bypass a blocker.

## 26. Рекомендуемый следующий work package

Build an evidence-grade chronological validation layer grouped by `model_version`, direction and market regime, with:

- no-trade and simple comparator baselines;
- return-on-margin/notional normalization;
- walk-forward cohort reporting;
- bootstrap/confidence intervals reported descriptively;
- predefined promotion, pause and retirement rules;
- explicit distinction between exact fills and proxy outcomes.

Until that package receives sufficient independent evidence, the project should be treated as a controlled hypothesis-generation and audit tool, not as a proven profitable trading system.
