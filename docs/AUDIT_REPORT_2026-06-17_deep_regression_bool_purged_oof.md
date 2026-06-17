# Консолидированный deep regression audit — 2026-06-17

**Scope:** Bybit V5 Linear USDT recommender / operator UI / fail-closed preflight / directional semantics / proxy-outcome calibration  
**Репозиторий:** `bybit-reco-systems-main`  
**Исходный ZIP SHA-256:** `19261c65f9884151f21a2d5d26ae6b0f05f55c7816b9811a75d0f455611a3ded`

## 1. Итог

Новых `CRITICAL` дефектов не найдено. Подтверждены и исправлены две независимые проблемы уровня `HIGH`:

1. JSON `true/false` могли превращаться в числа `1/0` в канонической directional-математике, execution-path, Bybit metadata parser и UI.
2. Chronological OOF для LogReg→Platt не доказывал, что train-label уже был доступен к началу validation-window. Поскольку outcome horizon отсчитывается от первой торгуемой свечи, а не обязательно от `recommendation.ts`, row-order split мог содержать скрытое пересечение будущих label windows.

Оба исправления fail-closed. Защиты не ослаблялись, severity существующих guard'ов не снижалась, отсутствующий OMS/EMS не выдумывался.

## 2. Обязательный pre-read и граница системы

До правок изучены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- `docs/AUDIT_REPORT_2026-06-16_ui_exit_math_failclosed.md`;
- `docs/AUDIT_REPORT_2026-06-15_ui_numeric_failclosed_reaudit.md`;
- `docs/AUDIT_REPORT_2026-06-15_full_system_regression.md`;
- `docs/AUDIT_REPORT_2026-06-15_execution_liq_boundary_reaudit.md`.

Подтверждённая граница: репозиторий является recommendation + audit + fail-closed operator-preflight сервисом. Реального order/fill lifecycle, websocket reconciliation и recovery по биржевой книге ордеров здесь нет. Поэтому partial fill, retry order routing, actual reduce-only order state, insufficient balance и exchange reconciliation остаются требованиями к внешнему executor, а не дефектами несуществующего кода.

## 3. Baseline до правок

Команды выполнены до изменения исходников:

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest -q                                    712 passed / 0 failed / 0 skipped
```

Среда:

```text
Python 3.13.5
Node v22.16.0
```

## 4. Зафиксированные математические конвенции

### Directional trade math

Источник истины: `app/trading_semantics.py`.

- `gross_profit_usdt` / `gross_loss_usdt` — **gross**, без fee/funding;
- `reward_pct` / `risk_pct` — процент от entry notional `entry * qty`, не ROI на маржу;
- `risk_reward` — отношение gross price/PnL distances, до costs;
- long: `TP > entry > SL`, PnL `qty * (exit-entry)`;
- short: `TP < entry < SL`, PnL `qty * (entry-exit)`;
- neutral-grid не получает одиночные directional TP/SL.

### Grid economics

Источник: `app/grid_math.py` и `app/recommender.py`.

- `margin_required_usdt = notional / leverage`;
- canonical net grid edge вычитает execution cost и только adverse positive funding cost;
- отрицательный signed funding (потенциальное получение) остаётся diagnostic и не кредитуется как durable executable edge;
- approximate liquidation применяется только как conservative isolated-linear buffer, не как точное значение Bybit account engine.

### Bybit one-way mapping

Источник истины: `app/trading_semantics.py`.

- open long=`Buy`; close/protect long=`Sell`;
- open short=`Sell`; close/protect short=`Buy`;
- protective exits: `reduceOnly=true`, `closeOnTrigger=true`, `positionIdx=0`;
- upward trigger: long TP / short SL;
- downward trigger: long SL / short TP;
- неверная geometry блокируется, а не нормализуется в «похожий» ордер.

## 5. Карта single source of truth

### Канонический слой

- `app/trading_semantics.py`: direction normalization, TP/SL mapping, geometry, gross PnL, R:R, Bybit side/reduceOnly/trigger semantics.
- `app/grid_math.py`: linear PnL, funding sign, fee/cost, margin, approximate liquidation, grid net economics.

### Backend/API/preflight

- `app/main.py::_directional_exit_payload_for_reco`: API payload для UI из canonical helpers;
- `app/main.py::_trade_plan_price_context`: единый lookup entry/range/kill-switch/step/TP hints;
- `app/main.py::_validate_trade_plan_against_bybit_meta`: tick/qty/notional/leverage/directional geometry;
- `app/main.py` live-price, funding, risk и same-symbol one-way guards;
- `app/recommender.py`: direction aggregation, grid construction, funding cost, leverage/liquidation/capital estimates;
- `app/risk.py`: recommendation-time и execution-time caps;
- `app/bybit_client.py`: instrument/ticker metadata normalization.

### Persistence / labels / calibration

- `app/db.py`: recommendation, bot, trade, outcome, risk/audit state;
- `app/outcomes.py`: proxy grid outcome, signed direction and adverse funding adjustment;
- `app/calibration.py`: feature extraction, weighting, LogReg, chronological OOF and Platt.

### Frontend/alerts/docs

- `app/ui/static/app.js`: отображает backend `directional_exit_levels`; самостоятельная directional geometry не считается источником истины;
- `app/alerts.py`: advisory alert text, без order routing;
- `docs/TRADING_LOGIC.md`, `docs/KNOWN_RISKS.md`, audit reports: документируют, но не исполняют торговую семантику.

### Режимы

Paper/shadow/live OMS в репозитории отсутствуют. Имеются recommendation state, operator materialization `bot_instance` и preflight. Поэтому parity проверена между canonical backend payload и UI parsing/rendering, а не с фиктивным live executor.

## 6. Findings и исправления

### HIGH-01 — Boolean → numeric coercion нарушал fail-closed семантику

**Затронутые места до исправления:**

- `app/trading_semantics.py::_finite_float`;
- `app/main.py::_safe_int_or_none`, `_finite_float_or_none`, nested directional qty parser;
- `app/bybit_client.py::_safe_float`, `_safe_int`;
- `app/calibration.py` numeric parsers;
- `app/ui/static/app.js::toFiniteNumber`.

**Причина:**

- в Python `bool` является подклассом `int`: `float(True)==1.0`, `int(False)==0`;
- в JavaScript `Number(true)==1`, `Number(false)==0`.

**Воспроизводимый риск:**

До фикса `directional_trade_math("long", True, 2, 0.5, 1)` возвращал валидную сделку с entry `1.0`; execution price context принимал boolean как `reference_price=1.0`; UI показывал `true/false` как реальный numeric level. Это позволяло malformed manual/legacy JSON пересечь границу «невалидно» → «число».

**Финансовый риск:**

Неверные entry/range/kill-switch/qty значения могли создать ложную геометрию, некорректный displayed PnL/R:R или ошибочную instrument validation. Внешний executor отсутствует, поэтому прямой order placement из этого репозитория невозможен, но operator decision/preflight boundary является safety-critical.

**Исправление:**

- `app/trading_semantics.py:23-34` — booleans отклоняются до `float()`;
- `app/main.py:301-307`, `783-787`, `1730-1741` — integer/float execution parsing fail-closed;
- `app/bybit_client.py:13-31` — boolean metadata не становится tick/qty/interval;
- `app/calibration.py:39-57` — boolean confidence/timestamp не становится числом;
- `app/ui/static/app.js:66-71` — shared parser возвращает `null` для boolean;
- `app/ui/static/index.html:7,126` — cache key `manual-ui-v42`, чтобы старый JS не «залип» у оператора.

**Безопасность изменения:** только ужесточение. Числовые `0/1` и строковые числа остаются совместимыми; boolean больше не маскируется под число.

### HIGH-02 — OOF split не учитывал фактический момент доступности label

**Затронутые места до исправления:**

- `app/calibration.py::_time_series_oof_logits`;
- `app/outcomes.py` / `app/db.py`: не хранился exact label availability timestamp.

**Причина:**

OOF был chronological по row index. Но proxy outcome вычисляется на окне от `entry_ts`, где `entry_ts` — первая tradeable candle после signal reference. При пропущенных свечах `entry_ts > recommendation.ts`. Следовательно, даже purge по `recommendation.ts + horizon` был бы приблизительным и мог считать label доступным раньше фактического `entry_ts + horizon`.

**Финансовый/econometric риск:**

Train fold мог использовать информацию из future window, недоступную к началу validation decisions. Это делает Platt-on-top переоптимистичным и искажает probability-like confidence. Confidence остаётся под risk/preflight gates, но leakage снижает доверие к ranking и model diagnostics.

**Исправление:**

- `migrations/init.sql:151-164`, `migrations/init_postgres.sql:147-160` — nullable `reco_outcomes.label_available_ts`;
- `app/db.py` — idempotent runtime migration для legacy DB, persistence/read-through exact timestamp;
- `app/outcomes.py:587-603` — новая label сохраняет `label_available_ts = ts_exit = entry_ts + effective_horizon`;
- `app/calibration.py:460-496` — purged train indices допускают строку только если:
  - `train_recommendation_ts < validation_ts`;
  - `train_recommendation_ts <= label_available_ts < validation_ts`;
  - timestamp известен, finite/integer и не boolean;
- `app/calibration.py::_time_series_oof_logits` — purged indices используются для каждого fold;
- `fit_logreg` сортирует timestamps и передаёт exact availability в OOF.

**Legacy policy:** существующим rows не присваивается синтетический optimistic timestamp. `NULL` labels исключаются из OOF train folds. Они всё ещё могут участвовать в финальном fit на текущий момент, поскольку факт наличия строки означает, что label уже рассчитан. Цена безопасности — временно меньшая OOF/Platt выборка.

**Безопасность изменения:** только ужесточение. При недостатке purged samples OOF не подменяется in-sample logits.

## 7. Red → green доказательство

Финальные regression tests были наложены на pristine исходный код.

### До исправления

```text
7 failed / 2 passed
```

Подтверждённые red cases:

- canonical long math принимал boolean entry;
- execution context принимал boolean price/grid count;
- frontend parser выдавал `1/0`;
- UI cache key оставался v41;
- purged helper отсутствовал;
- `fit_logreg` не передавал timestamps/availability в OOF;
- schema не имела `label_available_ts`.

### После исправления — targeted

```text
9 passed
```

### Новые тесты

`tests/test_iteration188_boolean_numeric_failclosed.py` — 4 теста:

1. canonical TP/SL/PnL/protective geometry отклоняет booleans;
2. execution price/qty/grid context и Bybit parsers отклоняют booleans;
3. JS shared numeric parser отклоняет booleans, сохраняя numeric `0/1`;
4. static asset cache key обновлён.

`tests/test_iteration189_purged_calibration_oof.py` — 2 теста:

1. purged split удаляет unfinished/equal-time/missing-availability labels;
2. `fit_logreg` передаёт exact `label_available_ts` в walk-forward OOF.

Расширен `tests/test_iteration93_outcomes_and_feature_hardening.py`:

- проверяет exact `label_available_ts = first_tradeable_candle_ts + effective_horizon`;
- проверяет прохождение поля через DB join в calibration rows.

## 8. Full post-verification

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest -q                                    718 passed / 0 failed / 0 skipped
```

Сравнение:

| Стадия | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Baseline | 712 | 0 | 0 |
| Post | 718 | 0 | 0 |
| Δ | +6 | 0 | 0 |

Ни один ранее зелёный тест не упал.

## 9. Static/code-quality review

Проведён grep/scoped review по `tp`, `sl`, `take_profit`, `stop_loss`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk_reward`.

Крупнейшие production-hit зоны:

- `app/main.py` — API/preflight/UI payload;
- `app/recommender.py` — recommendation math;
- `app/trading_semantics.py` — canonical directional source;
- `app/ui/static/app.js` — presentation/parsing;
- `app/outcomes.py`, `app/grid_math.py`, `app/db.py` — labels/economics/persistence.

Результат triage:

- новых обходов canonical TP/SL/side geometry не найдено;
- frontend directional exits получает backend payload и fail-closed скрывает invalid/missing geometry;
- remaining direct numeric formatting calls относятся преимущественно к diagnostics/counts и не формируют защитные ордера;
- реального Bybit order submission кода нет;
- прошлые static scan artifacts `docs/STATIC_SCAN_*` отсутствуют, поэтому формальный diff такого файла невозможен; сырой grep в отчёт не дублировался.

Недоступные проверки:

- `ruff`: модуль не установлен;
- `mypy`: модуль не установлен;
- npm/yarn tests/lint: `package.json` отсутствует;
- private Bybit API / wallet / positions / testnet order lifecycle: нет credentials и нет OMS/EMS слоя;
- exact liquidation/account risk tier: невозможно без private account state.

## 10. Bybit V5 cross-check

На дату аудита сверены официальные V5 разделы Place Order и Get Instruments Info:

- Linear order payload использует `category=linear`, `side=Buy|Sell`, `positionIdx=0` для one-way;
- protective close semantics требуют не увеличивать позицию; canonical model сохраняет `reduceOnly/closeOnTrigger`;
- price/qty/notional/leverage filters должны подтверждаться свежими instrument metadata;
- репозиторий корректно остаётся preflight/recommendation boundary, а фактическая повторная проверка account balance, current position и order state принадлежит внешнему executor.

## 11. Остаточные риски относительно `KNOWN_RISKS.md`

Остаются открытыми и не маскируются зелёными тестами:

1. нет реального OMS/EMS, fills и exchange reconciliation;
2. proxy outcomes не равны realised fills/fees/funding/liquidation truth;
3. legacy outcomes без `label_available_ts` временно уменьшают OOF coverage;
4. score-only Platt fallback на малых/non-stationary samples остаётся advisory и proxy-based;
5. public REST не является execution truth;
6. exact liquidation, cross margin и hedge mode не поддержаны;
7. SQLite подходит для single-node, но не заменяет production multi-writer persistence;
8. Telegram alerts best-effort;
9. фактический minNotional/qtyStep/balance должен повторно проверяться внешним executor непосредственно перед созданием ордеров.

`docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md` и `docs/MODULES.md` обновлены с новой numeric и label-availability семантикой.

## 12. Изменённые production-файлы

- `app/trading_semantics.py`
- `app/main.py`
- `app/bybit_client.py`
- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `app/outcomes.py`
- `app/db.py`
- `app/calibration.py`
- `migrations/init.sql`
- `migrations/init_postgres.sql`

Механически обновлены существующие UI cache-key assertions с v41 на v42. Архитектурный рефакторинг `app/main.py` / `app/recommender.py` не выполнялся.

## 13. Вывод

После исправлений repository boundary стала строже в двух местах, где прежний зелёный baseline не покрывал реальный класс регрессий:

- malformed boolean больше не превращается в торговое число;
- historical OOF больше не получает train-label до момента его фактической доступности.

Финальный full suite зелёный: **718 passed**. Изменения не добавляют live execution и не создают ложных тестов для отсутствующего OMS/EMS.
