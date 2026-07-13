# Audit iteration 230 - stale positive calibrator fail-closed

## 1. Название итерации

Iteration 230: запрет бессрочного использования положительной калибровки после исчезновения текущей evidence-выборки.

## 2. Входной ZIP

- Файл: `bybit-reco-systems-1.0.41-shadow-outcome-independence.zip`
- SHA-256: `69d03ab02c2d58bc4c6d6b83062d5cad2a68b33656cc385bc26549f0aff29ab3`
- Archive safety: traversal, absolute paths, внешние symlinks, duplicate/conflicting entries и вложенные архивы не обнаружены.
- Фактический root: `bybit-reco-systems-main`.

## 3. Исходная и новая версия

- Исходная FastAPI version: `1.0.41`.
- Новая FastAPI version: `1.0.42`.
- SemVer: patch.
- `OUTCOME_LABEL_VERSION`: без изменения, `grid_label_v18`.
- Signal/model identity: без изменения, `bybit-taxonomy-v4-independent-shadow-roots`.
- Новые cache identities: `logreg_futures_grid_v7`, `logreg_global_v7`, `platt_direction_v6`.

## 4. Project fingerprint

Fingerprint соответствует Bybit Recommender:

- recommendation/audit service, не OMS/EMS;
- Bybit `category=linear`, USDT perpetual;
- поддерживаемый `bot_type=futures_grid`;
- FastAPI в `app/main.py`;
- canonical directional semantics в `app/trading_semantics.py`;
- frontend в `app/ui/static/`;
- dual persistence: SQLite и PostgreSQL compatibility layer;
- private order create/amend/cancel methods не добавлены.

Инвентаризация working tree:

- 23 production Python files в `app/`;
- 174 test files, максимальный iteration number 230;
- 54 документа в `docs/` после добавления текущего отчёта;
- 3 frontend static files;
- 2 migration SQL files;
- 22 FastAPI routes, из них 6 mutating routes.

## 5. Цель итерации

После этой итерации положительный/fitted calibrator не должен влиять на confidence, если после истечения часового cache interval его невозможно воспроизвести из текущей retained outcome-выборки.

Отрицательное monetary expectancy трактуется асимметрично: оно остаётся консервативным `NO_TRADE` veto до появления новой подтверждённой положительной выборки.

## 6. Критерии приемки

1. Stale positive bot-specific LogReg при текущем `insufficient` становится unfitted.
2. Stale positive global LogReg при текущем `insufficient` становится unfitted.
3. Stale fitted direction Platt scaler при текущем `insufficient` становится unfitted.
4. Новое insufficient-состояние сохраняется в `app_config`, чтобы restart не восстановил старые коэффициенты.
5. Fresh cache продолжает использоваться в пределах `CALIB_REFIT_INTERVAL_SEC=3600`.
6. Stale negative monetary expectancy не снимается из-за временно недостаточной выборки.
7. DB schema, API и env остаются совместимыми.
8. Все 1025 собранных test nodes проходят, а итоговый ZIP повторно распаковывается и проверяется.

## 7. Прочитанные источники

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `requirements.txt`, `requirements-dev.txt`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports, включая monetary expectancy и shadow outcome independence;
- `app/calibration.py`, `app/recommender.py`, `app/db.py`, `app/db_backend.py`, `app/main.py`;
- outcome, risk, grid, trading semantics и UI contracts;
- regression tests для iterations 208, 213-230.

## 8. Карта затронутого data flow

`reco_outcomes` + `recommendations` -> `db.get_outcomes_with_recs()` -> current model/version filtering -> monetary and class eligibility -> `_fit_bot_logregs()` / `_fit_global_logreg()` / `_fit_direction_calibrator()` -> serialized cache in `app_config` -> hourly `_load_or_fit_*` -> calibrated confidence / recommendation diagnostics.

Связанный retention flow:

`db.prune_old_data()` удаляет обычные recommendations и outcomes старше 14 дней. Поэтому persisted calibrator не может считаться бессрочным source of truth: после cache expiry он обязан быть воспроизводим из текущих retained rows.

## 9. Baseline environment

- Python: `3.13.5`.
- Node: `v22.16.0`.
- `pip check`: FAILED из-за внешнего конфликта среды: MoviePy 2.2.1 требует Pillow `<12`, установлен Pillow 12.2.0.
- Ruff: UNAVAILABLE (`No module named ruff`).
- Production-like `.env`, private Bybit credentials и live network smoke tests не использовались.

## 10. Baseline commands и результаты

- `python -m compileall -q app tests main.py` - PASSED.
- `node --check app/ui/static/app.js` - PASSED.
- `python -m pytest --collect-only -q` - 1022 test nodes.
- Монолитный baseline `pytest -q` не завершил итоговый summary в пределах harness; он не засчитан как pass.
- Выполнен exhaustive deterministic non-overlapping batched run: 170 + 170 + 170 + 170 + 170 + 170 + 2 = 1022 nodes; 1022 passed.

Baseline был зелёным, поскольку существующие tests проверяли сохранение и загрузку calibrator, но не контракт после исчезновения поддерживающей retained sample.

## 11. Подтверждённый defect

### CAL-230-01

- Severity: **HIGH**.
- Тип: **CONFIRMED DEFECT**, model/risk fail-open.
- Основные файлы: `app/recommender.py`, функции `_load_or_fit_bot_logregs`, `_load_or_fit_global_logreg`, `_load_or_fit_direction_calibrator`.
- Связанный retention: `app/db.py::prune_old_data`.
- Входной payload/state: stale persisted calibrator с `fitted=true`, `expectancy_status=positive`, sample count 320; current refit возвращает `fitted=false`, `expectancy_status=insufficient`.
- Фактическое поведение v1.0.41: loader возвращал прежний fitted calibrator, когда новый fit был insufficient.
- Ожидаемое поведение: stale positive evidence должна быть деактивирована; current insufficient state должен быть сохранён.
- Нарушенный инвариант: положительный probability-like output обязан иметь текущую воспроизводимую evidence lineage; unknown/insufficient нельзя трактовать как безопасное продолжение старой модели.
- Финансовое влияние: оператор мог видеть calibrated confidence и принимать решение о запуске на коэффициентах, поддерживающие строки которых уже удалены или перестали удовлетворять sample contract.
- Model/data влияние: effective evidence lifetime становился не 14 дней, а неограниченным; hourly refit не выполнял функцию invalidation.
- Operational влияние: restart вновь загружал старый положительный payload из `app_config`.
- Почему tests не поймали: тесты покрывали fresh cache и fit success, но не stale-positive -> current-insufficient transition одновременно для bot/global/direction.

Минимальный reproducer:

1. Сохранить fitted positive scaler со `saved_ts < now - 3600`.
2. Подменить current fit на insufficient scaler.
3. Вызвать соответствующий `_load_or_fit_*`.
4. В v1.0.41 результат остаётся `fitted=true`.

## 12. Отдельно неподтверждённые claims

- Не доказано, что стратегия априори убыточна.
- Не доказано наличие положительного live edge.
- Без runtime database с exact fills нельзя определить фактический net expectancy, drawdown и expected shortfall.
- Текущий defect способен завышать доверие, но сам по себе не доказывает знак доходности стратегии.

## 13. План исправления

1. Добавить независимый regression test к pristine source.
2. Подтвердить red для bot, global и direction calibrators.
3. Сделать cache semantics асимметричной: positive evidence требует воспроизводимости; negative expectancy сохраняется fail-closed.
4. Сохранять current insufficient state, чтобы исключить resurrection после restart.
5. Обновить cache keys для немедленного refit при upgrade.
6. Не менять schema/API/env/outcome labels.
7. Синхронизировать документы и операторские DOCX/PDF.

## 14. Фактический diff

Production:

- `app/recommender.py` - исправлена stale cache/refit semantics для bot/global/direction.
- `app/calibration.py` - bot/global keys v7.
- `app/main.py` - version 1.0.42.

Tests:

- новый `tests/test_iteration230_stale_calibrator_fail_closed.py`;
- синхронизированы version/key assertions в iterations 208, 213-226 и 229.

Documentation:

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- `docs/instrukciya_operatora_bybit_recommender.pdf`;
- текущий audit report.

Frontend/database/migrations:

- frontend production files не изменялись;
- schema и migration SQL не изменялись.

## 15. Red -> green evidence

RED на pristine v1.0.41 с добавленным только regression test:

```bash
python -m pytest -q tests/test_iteration230_stale_calibrator_fail_closed.py
```

Существенный output:

```text
assert loaded.fitted is False
E AssertionError: assert True is False
3 failed in 0.47s
```

GREEN после production fix:

```bash
python -m pytest -q tests/test_iteration230_stale_calibrator_fail_closed.py
```

```text
3 passed in 0.37s
```

Совместный calibration/regression run iterations 228-230:

```text
17 passed in 0.62s
```

## 16. Реализация и обоснование

- Fresh persisted state любого статуса используется только до конца часового cache interval.
- После cache expiry выполняется current-evidence refit.
- Current positive или negative persistable state сохраняется и используется.
- Если current state insufficient, stale positive/fitted payload заменяется на current insufficient и сохраняется.
- Stale `expectancy_status=negative` сохраняется как консервативный veto, если current data временно insufficient. Это предотвращает fail-open снятие ранее подтверждённого отрицательного monetary gate.
- Direction scaler не имеет monetary-negative state, поэтому stale fitted Platt всегда заменяется current unfitted state при insufficient refit.

## 17. Database/schema compatibility

- Schema change: нет.
- `migrations/init.sql` и `migrations/init_postgres.sql`: без изменений.
- SQLite fresh init + повторный init: PASSED, 18 tables.
- SQLite upgrade smoke test v1.0.41 -> v1.0.42: PASSED; контрольная строка `app_config` сохранена.
- Старые cache rows остаются в DB, но новые key identities v7/v6 исключают их автоматическое использование.
- PostgreSQL live integration: SKIPPED, поскольку явно disposable DSN не предоставлен.
- Offline PostgreSQL dialect/locking subset: 24 passed.

## 18. API compatibility

- Routes и JSON field names не менялись.
- Status semantics не менялись.
- Изменяется только истинность `fitted/source/n_samples` после stale insufficient refit: API больше не должен показывать unsupported calibrated confidence.

## 19. Config/env compatibility

- Новых env variables нет.
- `.env.example` не изменён.
- Действия пользователя с `.env` не требуются.

## 20. Security boundary

- Order create/amend/cancel endpoints не добавлены.
- Recommendation/audit-only boundary сохранена.
- Production credentials не использовались и не включаются в release.
- ADMIN/security model mutating routes не ослаблялась.

## 21. Post-check commands и результаты

- `python -m pytest --collect-only -q` - 1025 tests collected.
- Exhaustive non-overlapping post-fix batches: 5 interleaved groups по 205 nodes; один group технически выполнен подгруппами 40+40+40+40+40+5. Union = 1025, duplicates = 0, missing = 0, 1025 passed.
- `python -m compileall -q app tests main.py` - PASSED.
- `node --check app/ui/static/app.js` - PASSED.
- iteration230 targeted - 3 passed, повторяемо.
- PostgreSQL offline subset - 24 passed.
- SQLite fresh/upgrade checks - PASSED.
- DOCX render - 6 pages, все страницы визуально проверены.
- PDF render - 6 pages, все страницы визуально проверены.
- `pip check` - FAILED только из-за внешнего MoviePy/Pillow conflict.
- Ruff - UNAVAILABLE.

## 22. Что не удалось проверить

- Реальный PostgreSQL transaction behavior на live server: нет disposable DSN.
- Реальные Bybit fills, fees, funding, latency и account equity: release не содержит runtime evidence DB и production credentials намеренно не использовались.
- Live profitability и production readiness не подтверждались.
- Ruff full-project baseline недоступен в среде.

## 23. Остаточные риски

1. Unfitted current calibrator по существующему documented contract не является самостоятельным hard block: система может использовать ограниченный raw confidence, если deterministic gates проходят. Это не должно интерпретироваться как calibrated probability.
2. OHLCV proxy outcomes не воспроизводят queue priority, partial fills, latency и фактический fee tier.
3. Положительный weighted mean proxy return не является статистическим доказательством alpha без uncertainty bound и independent walk-forward.
4. Предыдущие calibrated confidence до v1.0.42 могли опираться на уже исчезнувшую evidence-выборку и не должны использоваться как историческое доказательство.

## 24. Диагноз проекта

За четыре последовательные итерации подтверждены разные способы ложного положительного заключения:

- отрицательный cumulative exact PnL маскировался median/win rate;
- бинарный success игнорировал величину хвостовых убытков;
- перекрывающиеся shadow outcomes считались независимыми;
- положительный calibrator переживал исчезновение supporting evidence.

Это доказывает, что прежняя доказательная цепочка проекта была существенно скомпрометирована. Это не доказывает априорную убыточность самой торговой идеи. После v1.0.42 код лучше ограничивает ложную уверенность, но стратегия должна заново доказать edge на независимых exact-execution данных.

## 25. Rollback procedure

- Остановить сервис.
- Восстановить release v1.0.41.
- Перезапустить сервис.
- Schema rollback не требуется.

Rollback не рекомендуется: он возвращает бессрочное использование stale positive calibrators после insufficient refit.

## 26. Рекомендуемый следующий work package

Добавить fail-closed uncertainty gate для monetary expectancy:

- effective independent sample size;
- one-sided lower confidence bound;
- block/no-trade, пока нижняя граница `<= 0`;
- temporal walk-forward по exact fills;
- expected shortfall и drawdown по капиталу под риском;
- сравнение с no-trade и простой baseline grid.
