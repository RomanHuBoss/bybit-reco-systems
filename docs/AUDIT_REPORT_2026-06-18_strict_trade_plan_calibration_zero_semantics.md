# Консолидированный аудит: strict trade-plan integrity и calibration zero semantics

**Дата:** 2026-06-18  
**Scope:** fail-closed execution preflight, canonical trade-plan contract, calibration feature extraction, regression/static checks  
**Репозиторий:** `bybit-reco-systems-main`

## 1. Граница системы и прочитанный контекст

До изменений были изучены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- последние отчёты `AUDIT_REPORT_2026-06-18_grid_count_integer_semantics.md`, `AUDIT_REPORT_2026-06-17_remaining_numeric_failclosed.md`, `AUDIT_REPORT_2026-06-17_funding_calibration_ui_bool_failclosed.md`, `AUDIT_REPORT_2026-06-17_deep_regression_bool_purged_oof.md` и `AUDIT_REPORT_2026-06-16_ui_exit_math_failclosed.md`.

Подтверждённая системная граница: репозиторий является рекомендателем и fail-closed preflight-контуром, но не реальным OMS/EMS. Поэтому lifecycle реальных order/fill/cancel/retry/reconciliation, wallet truth и private-account state не моделировались фиктивным кодом и оставлены требованиями к внешнему execution-слою.

## 2. Исходный baseline

| Проверка | Исходный результат |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| `pytest -q` | **741 passed, 0 failed, 0 skipped** |

Артефакты: `docs/audit_artifacts/2026-06-18_strict_plan_calibration/baseline_*`.

## 3. Зафиксированные конвенции

- Directional gross PnL в canonical helper: long `qty × (exit-entry)`, short `qty × (entry-exit)`; комиссии и funding не входят в `directional_trade_math`, а учитываются отдельно в grid economics/cost model.
- `reward_pct` и `risk_pct` в helper считаются от notional `entry × qty`, а не от маржи.
- `risk_reward` — отношение положительной ценовой gross-profit magnitude к gross-loss magnitude; неверная геометрия возвращает `None`.
- Положительный funding — adverse carry для long; отрицательный funding — adverse carry для short. Потенциальное получение funding не кредитуется как устойчивый alpha в approval edge.
- Neutral/grid не получает одиночный directional TP; UI показывает outer kill-switch geometry.
- Exact liquidation/wallet-margin truth отсутствует и должен подтверждаться внешним execution/reconciliation layer.

## 4. Карта single source of truth

| Область | Реализация / отображение | Результат проверки |
|---|---|---|
| Нормализация direction, TP/SL geometry | `app/trading_semantics.py:22-129` | Canonical, fail-closed |
| Long/short PnL, distance, R:R | `app/trading_semantics.py:132-216` | Canonical gross directional math |
| Bybit side / `positionIdx` / `reduceOnly` / `closeOnTrigger` | `app/trading_semantics.py:219-298` | Canonical one-way mapping |
| Backend API payload | `app/main.py:925-977`, `1514`, `1551` | Использует canonical helper |
| Execution geometry validation | `app/main.py:3020-3060` | Использует canonical exit mapping |
| UI TP/SL/R:R rendering | `app/ui/static/app.js:634-760`, `1020-1060` | Потребляет backend `directional_exit_levels`; при missing/mismatch/invalid geometry блокирует directional display |
| Persisted grid/outcome PnL | `app/grid_math.py`, `app/outcomes.py`, `app/db.py`, `app/risk.py` | Grid/proxy economics, не альтернативная side/TP/SL модель |
| Live real orders | отсутствуют | Требование к внешнему executor; фиктивные тесты не добавлялись |

В обход `app/trading_semantics.py` новой исполнимой directional side/TP/SL модели не найдено. UI содержит только defensive geometry verification и formatting, а не независимый источник ценовых уровней.

## 5. Находки и исправления

### HIGH-01 — неполный или произвольный `trade_plan` мог пройти strict execution guard

**Файл до исправления:** `app/main.py`, прежний блок около строк 2818-2826.  
**Файл после исправления:** `app/main.py:2818-2845`.  
**Риск:** проверка считала любой непустой dict достаточным признаком наличия плана. После этого reference/range/kill-switch/grid-step могли быть взяты из legacy/operator aliases. В результате объект `trade_plan={"marker":"..."}` или частичный nested plan не доказывал наличие канонического execution-контракта, но strict preflight мог вернуть `ok=true`.

**Исправление:**

- strict guard извлекает только канонические nested-поля самого `trade_plan`;
- обязательны finite values: `reference_price`, range lower/upper, kill-switch lower/upper, `grid_step.step_abs`;
- legacy/operator aliases остаются допустимыми для read-only UI/diagnostics, но не могут сделать произвольный/частичный объект исполнимым;
- `TRADE_PLAN_MISSING` остаётся fail-closed error при `require_execution_plan=true`.

**Тесты red→green:**

- `test_strict_execution_rejects_noncanonical_nonempty_trade_plan_even_with_complete_aliases`;
- `test_strict_execution_rejects_partial_trade_plan_when_operator_sheet_fills_missing_geometry`.

### HIGH-02 — валидные нулевые quant-признаки заменялись нейтральными defaults

**Файл до исправления:** `app/calibration.py`, прежние строки около 221-313.  
**Файл после исправления:** `app/calibration.py:218-313`.  
**Риск:** выражения вида `_safe_float(value, 0.5) or 0.5` трактовали корректный `0.0` как отсутствие значения. В feature snapshot и legacy reconstruction это заменяло наблюдения:

- `range_score: 0.0 → 0.5`;
- `dir_conf: 0.0 → 0.5`;
- `coherence: 0.0 → 0.5`;
- `spread_bps_norm: 0.0 → 0.8`;
- `liq_tier_num: 0.0 → 0.67`;
- `regime_conf: 0.0 → 0.5`.

Это нарушало training/inference semantic parity, смещало калибровочную матрицу и могло повышать probability-like confidence при реально нулевой уверенности/когерентности.

**Исправление:** truthiness fallback удалён. `_safe_float` по-прежнему даёт консервативный default для missing, invalid и non-finite значений, но числовой ноль сохраняется.

**Тесты red→green:**

- `test_feature_snapshot_preserves_valid_zero_values_instead_of_neutral_defaults`;
- `test_legacy_feature_reconstruction_preserves_observed_zero_confidence_and_spread`.

### MEDIUM-01 — legacy/manual grid-count ↔ step mismatch остаётся compatibility warning

**Файл:** `app/main.py`, `GRID_STEP_LEVELS_MISMATCH` validation path.  
**Статус:** документирован, не изменён в финальном diff.

Была проверена гипотеза о глобальном переводе mismatch в strict execution error. Изменение прошло целевой новый тест, но полный suite дал **31 regression failure** в уже задокументированных legacy/manual compatibility flows. Ослабление существующих тестов или guard'ов ради зелёного результата запрещено, поэтому эксперимент полностью откатан.

Generated strict-geometry payloads продолжают блокироваться fail-closed. Для полного закрытия остаточного риска нужна versioned migration старых payloads в canonical geometry, а не локальная смена warning→error. До такой миграции внешний executor обязан независимо пересчитать exact levels/order count.

## 6. Red→green доказательство

Финальные четыре теста были скопированы в нетронутую исходную распаковку архива и выполнены до production fixes:

- **RED:** `4 failed`, exit code 1;
- **GREEN после fixes:** `4 passed`.

Артефакты:

- `docs/audit_artifacts/2026-06-18_strict_plan_calibration/red_final_tests.txt`;
- `docs/audit_artifacts/2026-06-18_strict_plan_calibration/green_final_tests.txt`.

Ожидаемые значения в тестах выведены независимо: canonical plan completeness проверяется по явно заданному набору обязательных nested-полей; calibration tests ожидают сохранения математически валидного `0.0`, а не сравнивают функцию с её собственным выводом.

## 7. Post-validation

| Проверка | Результат после исправлений |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| Целевые новые тесты | **4 passed** |
| Полный `pytest -q` | **745 passed, 0 failed, 0 skipped** |

Итого: baseline **741 → 745 passed**; ни один ранее зелёный тест не упал.

## 8. Static / code-quality проверки

- Выполнен scoped scan canonical semantics и мест TP/SL/PnL/R:R/Bybit side.
- После исправления в `app/calibration.py` не осталось `_safe_float(...) or default` в audited feature path.
- Единственный похожий scan hit — `_safe_int(ticker.get("nextFundingTime") or 0)` в `app/bybit_client.py`; там `or 0` нормализует отсутствующий timestamp, а `_safe_int`/последующая freshness логика не превращают его в исполнимое подтверждение. Новая находка не открыта.
- `ruff` не установлен; команда не выполнена. В репозитории также не обнаружены `package.json`, `pyproject.toml`, ESLint/mypy/ruff config на проверенной глубине, поэтому настроенных npm/yarn/lint/type-check команд нет.

Артефакты scan/diff находятся в `docs/audit_artifacts/2026-06-18_strict_plan_calibration/`.

## 9. Bybit V5 сверка

Canonical mapping проверен против официальной документации Bybit V5 Place Order:

- one-way `positionIdx=0`;
- close-side противоположен open-side;
- protective exit использует `reduceOnly=true` и `closeOnTrigger=true`;
- `triggerDirection=1` — рост, `2` — падение;
- подтверждение REST create-order асинхронно и не является fill truth.

Источник: `https://bybit-exchange.github.io/docs/v5/order/create-order` (проверено 2026-06-18).

Реальный private API/testnet order placement не запускался: в репозитории отсутствует OMS/EMS, а audit environment не содержит безопасно предоставленных private credentials/account sandbox.

## 10. Остаточные риски относительно `KNOWN_RISKS.md`

**Закрыто:**

1. Canonical execution contract больше нельзя подменить непустым/частичным `trade_plan` плюс aliases.
2. Calibration extraction сохраняет observed zeros и не подменяет их neutral defaults.

**Остаётся:**

1. Нет реального OMS/EMS, fill truth и reconciliation.
2. Proxy outcomes не равны фактическому net PnL с fills/funding/liquidation.
3. Small-sample score-only calibration fallback остаётся advisory и потенциально переоптимистичным.
4. Legacy/manual grid-count/step mismatch требует versioned payload migration.
5. Exact qty/minNotional, wallet balance, liquidation tier и account state должны повторно проверяться непосредственно перед live order creation.
6. Public REST snapshots не гарантируют единую временную консистентность.

## 11. Изменённые файлы

- `app/main.py`;
- `app/calibration.py`;
- `tests/test_iteration193_strict_trade_plan_integrity.py`;
- `tests/test_iteration194_calibration_zero_semantics.py`;
- `docs/KNOWN_RISKS.md`;
- `docs/AUDIT_REPORT_2026-06-18_strict_trade_plan_calibration_zero_semantics.md`;
- `docs/audit_artifacts/2026-06-18_strict_plan_calibration/*`.

## 12. Вывод

Финальный diff минимален и не добавляет execution-код вне существующей границы системы. Два подтверждённых HIGH-дефекта исправлены fail-closed способом и покрыты независимыми red→green тестами. Полный исходный regression suite сохранён, итоговый результат — 745/745 passed.
