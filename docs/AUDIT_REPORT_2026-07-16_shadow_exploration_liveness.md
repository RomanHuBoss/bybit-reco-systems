# Audit iteration 258 — liveness исследовательских исходов при advisory LLM

## 1. Название итерации

**Разделение наблюдаемости исходов и допуска в exact-policy calibration без ослабления торговых gates.**

## 2. Входной ZIP

`bybit-reco-systems-main(2)(1).zip`

## 3. SHA-256 входного ZIP

`8c4d17429784470d0748f1611f005ecf184c9510621dde9e8de41356cffbac01`

Приложенный протокол: `Bybit_Recommender_Iteration_Prompt(3).pdf`, SHA-256 `1e2d759151c2df3ea6781ddcb9bead7c467d4b8c59a97546550be77a17415647`.

## 4. Исходная версия

`1.0.69`, source of truth: `FastAPI(..., version="1.0.69")` в `app/main.py`.

## 5. Новая версия

`1.0.70`, обратно совместимое patch-исправление.

## 6. Project fingerprint

PASSED:

- Bybit Recommender;
- единственный штатный `bot_type=futures_grid`;
- Bybit `category=linear`, USDT perpetual;
- recommendation/audit service, не OMS/EMS;
- SQLite и PostgreSQL;
- FastAPI в `app/main.py`;
- frontend в `app/ui/static/`;
- каноническая directional-модель в `app/trading_semantics.py`;
- private order create/amend/cancel endpoints отсутствуют.

В приложенном внешнем протоколе упомянут `isolated` margin mode, но фактический ZIP, код, документация и regression contract проекта используют `cross`. По установленному самим протоколом порядку доверия текущий ZIP и исполняемый код выше внешней спецификации. Margin contract не изменялся и не входил в scope.

## 7. Цель итерации

После этой итерации явно разрешённая, риск-чистая исследовательская строка `shadow_no_trade` должна получать proxy-outcome при включённом advisory LLM, даже если `policy_evaluation_eligible=false`; при этом она не должна входить в exact current-policy calibration, увеличивать `calibrator_n` или становиться исполнимой.

## 8. Критерии приёмки

1. `shadow_exploration` с `outcome_eligible=true`, пустыми risk blocks и полным планом созревает без LLM verdict.
2. Liveness считает такой созревший root, а не сообщает `no_matured_pending_roots`.
3. `policy_evaluation_eligible=false` продолжает исключать результат из текущего calibration lineage.
4. Actionable roots без допустимого LLM verdict остаются fail-closed.
5. `MEAN_REVERSION_MIN_SCORE`, monetary/probability floors, risk/economics gates и status semantics не меняются.
6. Схема БД, API и `.env` остаются обратно совместимыми.
7. Полная offline-коллекция проходит без ослабления существующих тестов торговли и риска.

## 9. Прочитанные источники

- пользовательский PDF-протокол от 10 июля 2026 г.;
- диагностический JSON `bybit-recommender-diagnostics-2026-07-16T13-55-05-480Z.json`;
- README, CHANGELOG, requirements, `.env.example`;
- KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC;
- пять последних audit reports, особенно iteration 250/255/256/257;
- `app/recommender.py`, `outcomes.py`, `db.py`, `calibration.py`, `main.py`, `llm_review.py`, `settings.py`;
- frontend и релевантные regression tests.

## 10. Карта затронутого data flow

`recommender status=no_trade` → `outcome_policy.eligible=true` → `sample_role=shadow_no_trade` → `calibration_role=shadow_exploration` → persistence materialized fields → optional LLM gate → outcome-worker SQL selection → proxy label → outcome history → calibration lineage filter.

Ключевое разделение:

- `outcome_eligible` отвечает за возможность наблюдать исследовательский результат;
- `policy_evaluation_eligible` отвечает за допустимость результата в точной когорте текущей policy.

## 11. Baseline environment

- Python `3.13.5`;
- Node `v22.16.0`;
- production Python files: 24;
- test files: 202;
- docs: 80;
- frontend files: 3;
- migration SQL: 2;
- следующий regression number: 258.

ZIP: 331 entries, CRC PASSED, один root, traversal/absolute paths/external symlinks/duplicates/nested archives не обнаружены.

## 12. Baseline commands и точные результаты

- `python -m pip check` — FAILED: внешний конфликт host environment, MoviePy 2.2.1 требует Pillow `<12`, установлен Pillow 12.2.0.
- `python -m compileall -q app tests main.py` — PASSED.
- `python -m ruff check .` — UNAVAILABLE: `No module named ruff`.
- `node --check app/ui/static/app.js` — PASSED.
- `python -m pytest --collect-only -q` — 1175 tests.
- `python -m pytest -q` — **1175 passed in 34.70s**, exit 0.

Production code до завершения baseline не изменялся.

## 13. Подтверждённые defects/gaps

### I258-01 — HIGH — CONFIRMED DEFECT

**Файлы/функции:**

- `app/outcomes.py:1771-1789`, `compute_outcomes_cycle()`;
- `app/db.py:4448-4477`, `_is_explicit_safe_shadow_no_trade()`.

**Вход:** `status=no_trade`, `outcome_policy.eligible=true`, `sample_role=shadow_no_trade`, risk checks passed, blocks empty, `policy_evaluation_eligible=false`, LLM reviewer enabled.

**Фактическое поведение исходного кода:** SQL и повторный Python predicate требовали `policy_evaluation_eligible=true` для обхода LLM verdict. Но LLM reviewer намеренно не рецензирует non-actionable `no_trade`. Исследовательский root не выбирался и не мог получить outcome.

**Ожидаемое поведение:** explicit safe shadow root должен быть наблюдаемым независимо от advisory-LLM. Флаг policy evaluation должен применяться позже, при формировании calibration cohort.

**Нарушенный инвариант:** advisory LLM не должен изменять доступность исследовательских данных, если он принципиально не обрабатывает данный класс строк; fail-closed должен сохраняться только для actionable roots.

**Влияние:**

- financial/trading: прямого разрешения сделки не происходило; риск fail-open отсутствовал;
- model/data: постоянное голодание `shadow_exploration`, невозможность проверить гипотезы вне текущего candidate screen;
- operational: здоровая система могла бесконечно сохранять `outcomes_total=0` при длительном low-edge рынке.

**Почему тесты не поймали:** iteration 250 проверял только `policy_evaluation_eligible=true` exact-policy shadow roots. Отдельный контракт exploration был создан рекомендатором, но его прохождение через LLM selection не тестировалось.

**Fix:** LLM bypass теперь основан на полном explicit safe outcome contract — `outcome_eligible`, `sample_role`, risk checks и отсутствие blocks — без требования policy calibration eligibility.

**Остаточный риск:** proxy-outcome остаётся исторической моделью без exchange fills и не доказывает live edge.

### I258-02 — MEDIUM — CONFIRMED DEFECT

**Файл/функция:** `app/db.py:3570-3594`, `get_outcome_worker_liveness()`.

Liveness использовал более узкий LLM predicate и поэтому скрывал созревшие exploration roots, возвращая нулевую очередь. Worker и diagnostics должны использовать один eligibility contract.

**Fix:** `llm_shadow_expr` унифицирован с `safe_shadow_expr`.

## 14. Неподтверждённые и скорректированные claims

1. **«В проекте отсутствует отдельное разделение исполнения и накопления outcomes» — НЕ ПОДТВЕРЖДЕНО.** Уже существуют `outcome_eligible`, `policy_evaluation_eligible`, `sample_role` и `calibration_role`.
2. **«Нужно снизить `MEAN_REVERSION_MIN_SCORE` до 0.10–0.15» — ОТКЛОНЕНО.** Это смешало бы слабые гипотезы с текущей policy и нарушило evidence contract. Порог 0.25 сохранён.
3. **«Нужно добавить новую схему БД/новый shadow status» — НЕ ТРЕБУЕТСЯ.** Схема и роли уже поддерживают исследовательские наблюдения.
4. **«Нулевые outcomes вызваны неработающим worker» — НЕ ПОДТВЕРЖДЕНО.** Диагностика показывает живой worker; подтверждён именно eligibility mismatch.

## 15. План исправления

1. Зафиксировать exploration contract независимым regression test.
2. Воспроизвести RED на pristine copy.
3. Удалить лишнее calibration-условие только из outcome observability/LLM bypass.
4. Оставить calibration lineage неизменным и проверить его отдельным assertion.
5. Синхронизировать liveness, код, документацию и операторские артефакты.
6. Выполнить полный post-check и собрать чистый ZIP.

## 16. Фактический diff по файлам

### Production

- `app/outcomes.py` — согласован LLM bypass для всех explicit safe shadow roots.
- `app/db.py` — согласованы runtime predicate и liveness SQL.
- `app/main.py` — версия 1.0.70.

### Tests

- добавлен `tests/test_iteration258_shadow_exploration_liveness.py`;
- исторические статические version assertions синхронизированы с 1.0.70; торговые expectations не менялись.

### Frontend

- `app/ui/static/index.html` — cache build 1.0.70; JS/CSS logic не менялась.

### Database/migrations

- изменений схемы и SQL migrations нет.

### Docs

README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, DOCX/PDF operator instruction и `how_to_trade.png`.

## 17. RED → GREEN evidence

Новый файл: `tests/test_iteration258_shadow_exploration_liveness.py`.

RED на pristine code + только новый test:

```bash
python -m pytest -q tests/test_iteration258_shadow_exploration_liveness.py
```

Результат:

```text
2 failed, 1 passed in 0.62s
E assert 0 == 1
E assert status["matured_pending_total"] == 1
```

GREEN после production fix:

```bash
python -m pytest -q tests/test_iteration258_shadow_exploration_liveness.py
```

```text
3 passed in 0.55s
```

Повторный targeted post-check: `3 passed in 0.39s` два раза.

## 18. Database/schema compatibility

Схема не изменялась. Fresh/repeated SQLite init:

```text
20 tables -> 20 tables
required materialized columns present: true
PRAGMA integrity_check: ok -> ok
```

Reference `init.sql`/`init_postgres.sql` не изменялись. Existing-schema upgrade не требуется для этой итерации. PostgreSQL offline translation/locking subset: **22 passed**. Live PostgreSQL integration — SKIPPED: явно disposable DSN не предоставлен.

## 19. API compatibility

Публичные routes и JSON fields не менялись. OpenAPI version и FastAPI version: `1.0.70`. Status semantics не менялись.

## 20. Config/env compatibility

Новых переменных нет. `.env.example` не изменён. Пользовательские действия с конфигурацией не требуются.

## 21. Security boundary

- private Bybit order create/amend/cancel не добавлены;
- recommendation/audit-only boundary сохранена;
- `.env`, credentials и production DB не включаются в release;
- actionable roots остаются LLM-gated при включённом reviewer;
- hard blocks и risk checks не ослаблены.

## 22. Post-check commands и точные результаты

- `python -m pip check` — FAILED, тот же внешний MoviePy/Pillow conflict, delta отсутствует.
- `python -m compileall -q app tests main.py` — PASSED.
- `python -m ruff check .` — UNAVAILABLE, Ruff отсутствует.
- `node --check app/ui/static/app.js` — PASSED.
- collection — **1178 tests**.
- `pytest.main(['-q'])` — **1178 passed in 33.67s**, framework return code **0**.
- обычный foreground `python -m pytest -q` также напечатал `1178 passed in 34.80s`, но execution channel не завершился после summary из-за оставшегося shutdown/background handle в текущем harness. Это не скрыто; test framework return code отдельно зафиксирован до принудительной остановки процесса.
- targeted regression — 3 passed, повторно 3 passed.
- relevant outcome/readiness/restart suite — **31 passed**.
- PostgreSQL offline subset — **22 passed**.
- SQLite fresh/repeated init — PASSED.
- OpenAPI/version consistency — PASSED.
- private order endpoint scan — PASSED, 0 matches.
- operator artifacts — PRESENT; DOCX/PDF render проверен, 13 страниц без overflow; PNG проверен визуально.

## 23. Что не удалось проверить и почему

- live PostgreSQL: отсутствует явно disposable test DSN;
- реальная Bybit network/account behavior: не требовалось и не запускалось;
- production database reprocessing: production DB не использовалась;
- Ruff: пакет отсутствует;
- чистое завершение монолитного pytest-процесса в foreground harness: summary и return code тестового фреймворка получены, но внешний process shutdown удерживался фоновым handle.

## 24. Остаточные риски

- Первые новые outcomes появятся только после label horizon и при наличии полного допустимого OHLCV пути.
- Общий `outcomes_total` может расти при `calibrator_n=0`; это ожидаемо для exploration, а не ошибка.
- Proxy-outcomes не моделируют queue priority, точные fills, partial fills и exchange liquidation waterfall.
- Положительная monetary expectancy и live edge этой итерацией не доказаны.
- Причину фонового handle после полного pytest summary следует локализовать отдельной QA-итерацией, если она воспроизводится вне текущего harness.

## 25. Rollback procedure

1. Остановить сервис.
2. Вернуть архив версии 1.0.69.
3. Сохранить текущую БД: schema rollback не требуется и выполнять его нельзя.
4. Запустить сервис с прежней конфигурацией.
5. Проверить неизменность `database_instance_id`, собственный collector cycle и publication текущего процесса.

Rollback вернёт прежнее голодание `shadow_exploration` при включённом LLM, но не повредит существующие данные.

## 26. Рекомендуемый следующий work package

После развёртывания дождаться одного полного label horizon и проверить по диагностике:

- рост `outcomes_total`/исследовательской истории;
- отсутствие роста `calibrator_n` за счёт `policy_evaluation_eligible=false`;
- сохранение `recommended/active=0`, пока exact-policy evidence не выполнит действующие условия;
- отсутствие censored roots по malformed plan/market-data.

Следующая code-итерация нужна только при фактическом отсутствии прогресса после horizon; пороги заранее не снижать.
