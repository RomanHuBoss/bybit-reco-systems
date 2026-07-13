# Audit iteration: mean-reversion and temporal-evidence recovery

## 1. Название итерации

Bybit Recommender v1.0.55 — восстановление достижимого mean-reversion candidate screen и независимых temporal cohorts.

## 2. Входной ZIP

`bybit-reco-systems-main(2).zip`.

## 3. SHA-256 входного ZIP

`9ac181964cddec67698994b604047b2c61cd7b48dba5f36760ba1c0e960e9f15`.

Дополнительные runtime-выгрузки:

- `recent_recommendations.csv`: SHA-256 `bbef4968dfa022c74d756f3b9659a6fc6bdf53a20555ec9f0d73cd1d2ed7369d`;
- `mr_ohlcv.csv`: SHA-256 `db623990698213c07124f26d8bb615d2a37152c9d45cbd277a1cfde11724e3d0`.

## 4. Исходная версия

FastAPI `1.0.54`, source of truth: `app/main.py`, параметр `version=`.

Bot/global calibration identity: v16. Outcome contract: `grid_label_v26`. Direction calibration: v12.

## 5. Новая версия

FastAPI `1.0.55`. Bot/global calibration identity: v17. Outcome contract и direction calibration не изменены.

## 6. Project fingerprint

Совпадает с Bybit Recommender:

- `futures_grid`, Bybit `linear` USDT perpetual;
- recommendation/audit-only, без order create/amend/cancel;
- FastAPI в `app/main.py`;
- canonical directional semantics в `app/trading_semantics.py`;
- frontend в `app/ui/static/`;
- SQLite и PostgreSQL persistence;
- обязательные docs, migrations и operator artifacts присутствуют.

Архив: 299 entries, один root, traversal/absolute paths/symlinks/duplicate paths/nested archives не обнаружены.

## 7. Цель итерации

После итерации система должна:

1. не блокировать наблюдаемый верхний хвост mean-reversion score недостижимым фиксированным порогом 0.55;
2. сохранить отдельный fail-closed monetary expectancy gate;
3. не выдавать слабый mean-reversion score за доказанное отрицательное ожидание;
4. извлекать максимально возможное количество попарно неперекрывающихся temporal decision cohorts без транзитивной перколяции;
5. не считать одновременные строки разных символов независимыми временными наблюдениями;
6. быть полностью повторно проверенной и упакованной без runtime DB/секретов.

## 8. Критерии приемки

- Score `0.351` с валидными 5 TF проходит candidate screen по умолчанию, но не обходит downstream gates.
- Score ниже `0.25` остаётся `MEAN_REVERSION_EDGE_UNCONFIRMED/no_trade`.
- Missing/invalid evidence остаётся `MEAN_REVERSION_EVIDENCE_INSUFFICIENT/blocked`.
- Диагностика не утверждает доказанное negative expectancy без matured monetary evidence.
- Цепочка из 42 decision cohorts с 12-часовыми горизонтами и шагом 6 часов даёт 21 попарно неперекрывающийся cohort, а не 1 connected component.
- 80 символов с одним `ts` дают одно temporal observation.
- Новый targeted suite проходит детерминированно дважды.
- Все 1078 test nodes проходят exhaustive batches.

## 9. Прочитанные источники

Прочитаны обязательные README/CHANGELOG/env/docs, последние audit reports, trading/risk/recommender/calibration/outcomes/db/backend/settings/main/frontend modules, связанные tests, приложенный iteration prompt и обе PostgreSQL CSV-выгрузки.

## 10. Карта затронутого data flow

`closed OHLCV -> multi-TF range diagnostics -> mean_reversion_score -> candidate screen -> economics/risk/monetary gates -> recommendation status -> persisted shadow outcome -> maturity/label_available_ts -> calibration rows -> same-ts cohort collapse -> non-overlap thinning -> uncertainty lower bounds -> PROXY_MONETARY_EXPECTANCY_*`.

## 11. Baseline environment

- Python: `3.13.5`.
- Node: `v22.16.0`.
- Runtime executed offline; production credentials and live Bybit calls не использовались.
- Disposable PostgreSQL DSN отсутствовал.

Inventory:

- production Python files: 23;
- test files: 186;
- docs: 65;
- frontend files: 3;
- migration SQL files: 2;
- максимальный существующий iteration до правки: 242.

## 12. Baseline commands и точные результаты

| Проверка | Результат |
|---|---|
| `python --version` | PASSED — Python 3.13.5 |
| `node --version` | PASSED — v22.16.0 |
| `python -m pip check` | FAILED — внешний конфликт: MoviePy 2.2.1 требует Pillow `<12`, окружение содержит Pillow 12.2.0 |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE — модуль ruff отсутствует |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest -q` | TIMED OUT на 73%, итоговый summary отсутствовал и не засчитан |
| `python -m pytest --collect-only -q` | 1072 collected |
| exhaustive six batches | 173 + 167 + 156 + 188 + 233 + 155 = 1072 passed; failed 0, errors 0 |

## 13. Подтверждённые defects/gaps

### MR-243-01 — fixed cutoff caused strategic shutdown

- Severity: **high**.
- Тип: **CONFIRMED DEFECT**.
- Файл: `app/recommender.py`, `_mean_reversion_grid_blocks`.
- Вход: 10,000 рекомендаций за 2026-07-13 15:47:44–20:38:14 UTC.
- Фактическое распределение: min `0.02064`, median `0.18198`, p90 `0.26620`, p95 `0.29263`, p99 `0.34175`, max `0.35102`; `score >= 0.55`: 0.
- Статусы: 9,632 `no_trade`, 368 `blocked`; `MEAN_REVERSION_EDGE_UNCONFIRMED` присутствовал во всех 10,000 строках.
- Фактическое поведение: фиксированный `0.55` исключал весь наблюдаемый runtime support.
- Ожидаемое поведение: score является candidate-quality feature, а не доказательством PnL; candidate floor должен быть явным и достижимым, а profitability — отдельным gate.
- Финансовое/trading влияние: полный стратегический shutdown и невозможность сформировать достаточный поток кандидатов для последующей проверки.
- Почему tests не поймали: прежние тесты доказывали прохождение искусственно сильного процесса, но не сопоставляли cutoff с runtime distribution.
- Fix: `MEAN_REVERSION_MIN_SCORE`, default `0.25`; strict range validation `[0,1]`; отсутствующий env получает backward-compatible default.
- Остаточный риск: 0.25 не доказан как оптимальный или прибыльный; требуется более длинная chronological validation.

### MR-243-02 — false negative-expectancy wording

- Severity: **medium**.
- Тип: **CONFIRMED DEFECT**.
- Файл: `app/recommender.py`, `_mean_reversion_grid_blocks`.
- Фактическое поведение: сообщение утверждало, что комиссии «дают отрицательное ожидание» только из-за score ниже 0.55.
- Ожидаемое поведение: score ниже floor означает лишь недостаточную anti-persistence; monetary expectancy определяется matured returns и uncertainty bounds.
- Fix: сообщение теперь говорит, что положительное expectancy не доказано до отдельной outcome-проверки.

### MR-243-03 — transitive temporal-cluster percolation

- Severity: **high**.
- Тип: **CONFIRMED DEFECT**.
- Файл: `app/calibration.py`, `_temporal_cluster_return_diagnostics`.
- Минимальный reproducer: 42 cohorts, шаг 6 часов, horizon 12 часов; соседние интервалы пересекаются, но существует 21 попарно неперекрывающийся интервал.
- Фактическое поведение: connected-component merge по транзитивному overlap возвращал `temporal_cluster_count=1`.
- Ожидаемое поведение: количество временных наблюдений не должно увеличиваться от cross-sectional symbol count, но непрерывная overlap chain не должна навсегда замораживать count на 1.
- Fix: одинаковый `ts` объединяется в один weighted cohort; затем earliest-finish greedy выбирает maximum-cardinality set pairwise non-overlapping intervals.
- Model/data влияние: штатный минимум 20 effective temporal cohorts становится достижимым при накоплении фактически независимых по горизонту решений.
- Остаточный риск: неперекрывающиеся интервалы всё ещё могут зависеть от длительного market regime.

### MR-243-04 — runtime DB files packaged in input release

- Severity: **low**.
- Тип: **CONFIRMED GAP**.
- Вход содержал `data/app.db` и `data/app.runtime_locks.sqlite`. Торговых строк в них не было, но release contract запрещает runtime DB artifacts.
- Fix: файлы удалены из release; SQLite runtime/bootstrap support не удалён.

## 14. Неподтверждённые claims

- Прибыльность стратегии после изменения cutoff **не подтверждена**.
- Live fill/execution edge **не подтверждён**; проект не является OMS/EMS.
- Порог 0.25 **не объявляется оптимальным**.
- `n=0` в конкретной 10,000-row выгрузке не доказывал отсутствие всех outcomes в PostgreSQL; подтверждён именно алгоритмический механизм, способный удерживать temporal count на единице.

## 15. План исправления

1. Добавить independent regression tests в pristine/red copy.
2. Воспроизвести fixed cutoff, false wording, overlap percolation и отсутствие env contract.
3. Ввести configurable candidate floor без обхода downstream gates.
4. Заменить connected components на same-ts cohort collapse + earliest-finish thinning.
5. Bump cache identity до v17.
6. Синхронизировать operator/docs/version artifacts.
7. Выполнить exhaustive checks и собрать чистый ZIP.

## 16. Фактический diff по файлам

Production:

- `app/recommender.py`;
- `app/calibration.py`;
- `app/settings.py`;
- `app/main.py`.

Tests:

- новый `tests/test_iteration243_mean_reversion_temporal_recovery.py`;
- `tests/test_iteration189_purged_calibration_oof.py` — fixture сделан временно независимым, чтобы тест продолжал изолировать OOF forwarding;
- version/calibrator identity assertions синхронизированы в существующих tests.

Config/docs/artifacts:

- `.env.example`, `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX/PDF и `how_to_trade.png`;
- этот audit report.

Database/migrations:

- schema/runtime bootstrap/migrations не изменялись;
- packaged DB artifacts удалены.

## 17. Red → green evidence

RED command:

```bash
python -m pytest -q tests/test_iteration243_mean_reversion_temporal_recovery.py
```

RED result on pristine production code plus new tests:

- score 0.351 returned `MEAN_REVERSION_EDGE_UNCONFIRMED ... < 0.55`;
- false message contained `комиссии дают отрицательное ожидание`;
- `temporal_cluster_count` was `1`, expected `21`;
- `Settings` had no `mean_reversion_min_score`;
- summary: `4 failed, 2 passed in 0.46s`.

GREEN command: same.

GREEN result: `6 passed in 0.35s`; repeated: `6 passed in 0.35s`.

Relevant compatibility suite: `130 passed` before final exhaustive run. OOF fixture correction plus iteration243: `8 passed`.

## 18. Database/schema compatibility

Relational schema unchanged. `migrations/init.sql`, `migrations/init_postgres.sql` and runtime migration path were not modified.

SQLite fresh schema + idempotent re-init: PASSED, sentinel preserved, 18 tables.

PostgreSQL offline translation/locking subset: 24/24 passed. Live integration: SKIPPED, disposable test DSN not supplied.

Existing matured outcomes remain valid because the label target is unchanged. Bot/global cache keys move v16→v17, causing safe refit under the corrected temporal selection contract.

## 19. API compatibility

Public routes and JSON field names unchanged. Status semantics remain:

- missing evidence: `blocked`;
- weak but valid candidate evidence: `no_trade`;
- monetary evidence remains an independent `no_trade` veto until positive.

## 20. Config/env compatibility

New optional variable:

```env
MEAN_REVERSION_MIN_SCORE=0.25
```

Omission is backward compatible and uses 0.25. Values outside `[0,1]`, boolean-like/non-numeric malformed values are rejected by existing numeric env validation.

## 21. Security boundary

- No private Bybit order create/amend/cancel endpoints found.
- No credentials used or emitted.
- No `.env` packaged.
- Project remains recommendation/audit-only.
- Runtime DB files excluded from release.

## 22. Post-check commands и точные результаты

| Проверка | Результат |
|---|---|
| targeted iteration243 | 6/6 PASSED twice |
| collect-only | 1078 collected |
| exhaustive batches | 183 + 159 + 163 + 196 + 146 + 231 = 1078 PASSED |
| failed/skipped/errors | 0 / 0 / 0 |
| `compileall` | PASSED |
| Node syntax | PASSED |
| PostgreSQL offline subset | 24/24 PASSED |
| SQLite fresh/re-init | PASSED |
| private order endpoint scan | PASSED — none found |
| DOCX render | PASSED — 9 pages, visual inspection |
| PDF preflight/render | PASSED — openable, unencrypted, 9 pages |
| `pip check` | same pre-existing MoviePy/Pillow environment conflict |
| ruff | UNAVAILABLE |

## 23. Что не удалось проверить и почему

- Live PostgreSQL integration: нет явно disposable DSN; production DB не подключалась.
- Live Bybit execution/fills: вне архитектурной границы проекта.
- Ruff: пакет отсутствует в доступном Python environment.
- Абсолютная полнота всех дефектов не заявляется.

## 24. Остаточные риски

1. Порог 0.25 выбран как селективный восстановительный candidate floor по фактическому runtime support, но требует long-horizon walk-forward/outcome calibration.
2. Monetary gate может продолжать удерживать кандидаты в shadow `no_trade`, пока не накопится минимум независимых matured cohorts с положительными lower bounds; это ожидаемое fail-closed поведение.
3. Earliest-finish устраняет overlap, но не всю regime dependence.
4. OHLCV proxy не моделирует queue priority, точные partial fills и exchange liquidation mechanics.
5. Внешний dependency environment имеет MoviePy/Pillow conflict, не связанный с проектными requirements этой итерации.

## 25. Rollback procedure

1. Остановить приложение.
2. Развернуть v1.0.54 code/archive.
3. Удалить `MEAN_REVERSION_MIN_SCORE` из env, если он был добавлен.
4. Запустить приложение; schema rollback не требуется.
5. v17 calibrator payloads будут игнорироваться v16 keys; retained outcomes не удалять.

Rollback вернёт fixed 0.55 cutoff и transitive-cluster defect, поэтому использовать только как аварийную меру.

## 26. Следующий рекомендуемый work package

После накопления достаточного числа v17 matured cohorts выполнить заранее зафиксированную chronological threshold study: сравнить candidate floors 0.20–0.35 только на out-of-sample monetary outcomes с purging/embargo, costs/funding и фиксированными selection rules. До этого не объявлять live edge и не повышать систему до auto-execution.
