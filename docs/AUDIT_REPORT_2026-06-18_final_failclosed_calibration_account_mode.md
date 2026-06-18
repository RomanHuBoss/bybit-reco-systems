# Консолидированный финальный аудит Bybit Linear USDT recommender / fail-closed preflight

**Дата:** 2026-06-18  
**Scope:** regression re-audit после предыдущих итераций; directional semantics, calibration, persistence, Bybit V5 metadata/account-mode, publication lineage, UI/backend parity, static/code-quality gates.  
**Итог:** 3 HIGH и 2 MEDIUM подтверждены и исправлены; 21/21 новый тест доказан как red→green; полный suite зелёный — **800 passed**.

---

## 1. Обязательный pre-read и граница системы

До правок прочитаны:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- пять последних по дате `docs/AUDIT_REPORT_*`.

Подтверждена заявленная архитектурная граница: репозиторий является рекомендателем,
operator UI и fail-closed execution-preflight, но **не содержит полноценного OMS/EMS**.
Поэтому partial fill, live retry/idempotency, exchange reconciliation, real wallet truth,
private account/position mode и lifecycle реальных protective orders не имитировались
фиктивным кодом и остаются требованиями к внешнему executor.

Главный safety-инвариант сохранён: ни один guard не ослаблен, warning не превращён в
разрешение запуска, новые изменения только блокируют неоднозначные/повреждённые состояния
или нейтрализуют недостоверную probability-like confidence.

---

## 2. Исходный baseline до любых production-правок

| Проверка | Исходный результат |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| `pytest -q` | **779 passed, 0 failed, 0 skipped** |

Артефакты:

- `docs/audit_artifacts/2026-06-18_final_reaudit/baseline_compileall.txt`;
- `docs/audit_artifacts/2026-06-18_final_reaudit/baseline_node_check.txt`;
- `docs/audit_artifacts/2026-06-18_final_reaudit/baseline_pytest.txt`.

---

## 3. Зафиксированные математические и торговые конвенции

1. **Directional gross PnL** в `app/trading_semantics.py`:
   - long: `qty × (exit - entry)`;
   - short: `qty × (entry - exit)`.
   Fees и funding туда не включаются; net economics считаются отдельно.
2. **ROI/distance** canonical helper считает относительно notional/entry price, а не
   относительно маржи. Leverage влияет на required margin/liquidation buffer, но не
   меняет gross price return.
3. **Risk:reward** — отношение положительной gross reward distance к gross risk distance;
   при неверной directional geometry возвращается fail-closed `None`/ошибка.
4. **Funding carry** хранится направленно: положительная ставка неблагоприятна long,
   отрицательная — short. Возможный funding receipt не кредитуется как гарантированный alpha.
5. **Neutral grid** не получает одиночные directional TP/SL; отображаются range/grid и
   внешние kill-switch bounds.
6. **Bybit one-way semantics:** `positionIdx=0`; long open=`Buy`, short open=`Sell`;
   закрытие/защита — противоположная side с `reduceOnly=true`; conditional trigger direction
   зависит от движения цены, а не от названия TP/SL.

---

## 4. Карта single source of truth

| Контур | Источник / потребитель | Результат |
|---|---|---|
| Direction normalization | `app/trading_semantics.py:18-20` | canonical |
| Long/short/neutral TP/SL geometry | `app/trading_semantics.py:53-97` | canonical |
| Geometry validation | `app/trading_semantics.py:100-155` | fail-closed |
| Gross PnL, distances, R:R | `app/trading_semantics.py:158-216` | canonical |
| Bybit side / `positionIdx` / reduce-only / triggers | `app/trading_semantics.py:219-298` | canonical one-way mapping |
| Backend operator/API exit payload | `app/main.py:924-971`, materialized at `1620`, `1658` | вызывает canonical helpers |
| Strict execution geometry | `app/main.py:3179+` | вызывает `directional_exit_levels()` |
| UI cards/detail/manual panel/charts | `app/ui/static/app.js:650+`, `737+` | парсит backend `directional_exit_levels`, defensive validation only |
| Saved recommendation state | `app/db.py`, `app/recommender.py` | хранит direction/plan/economics; альтернативной side-модели нет |
| Grid proxy PnL/outcomes | `app/grid_math.py`, `app/outcomes.py` | отдельная grid economics, не дублирует order-side mapping |
| Alerts | `app/alerts.py` | health-only; TP/SL/side не вычисляет |
| Logs/audit reports | диагностические payloads | отображают уже рассчитанную семантику |
| Paper/shadow/live recommendation modes | общий recommender/preflight payload | отдельной directional математики не найдено |
| Live real order lifecycle | отсутствует | обязательство внешнего executor |

Новой исполнимой directional-математики в обход `app/trading_semantics.py` не обнаружено.
Backend↔frontend parity и short TP/SL regression уже покрыты существующими тестами и
повторно прошли в полном suite.

---

## 5. Подтверждённые находки и исправления

### HIGH-01 — `NaN`/`Infinity` превращались в экстремальную confidence

**До исправления:** `app/calibration.py`, `PlattScaler.predict`,
`LogRegScaler.predict`, `LogRegScaler.predict_score_only`.  
**После исправления:** `app/calibration.py:74-87`, `342-379`.

**Ошибка:** Python `min/max` над `NaN` и арифметика с infinite values не обеспечивали
нейтральную деградацию. Повреждённая feature/model value могла дать probability около
`1.0` или почти `0.0`, хотя наблюдение математически невалидно.

**Финансовый риск:** искусственно высокая confidence могла повлиять на ranking/gating и
визуально представить повреждённый сигнал как статистически сильный.

**Исправление:** вход, коэффициенты, intercept и итоговый logit проходят finite-проверку.
При любом non-finite значении возвращается нейтральное `0.5`; модель не получает
оптимистического или пессимистического directional bias.

**Red→green тесты:** 11 параметризованных случаев:

- non-finite Platt input: `NaN`, `+Inf`, `-Inf`;
- non-finite LogReg feature: `NaN`, `+Inf`, `-Inf`;
- non-finite score-only input: `NaN`, `+Inf`, `-Inf`;
- non-finite coefficient и intercept.

### HIGH-02 — malformed persisted calibrator активировался через truthiness

**До исправления:** `app/calibration.py`, loaders около прежних строк 716-799.  
**После исправления:** `app/calibration.py:746-834`.

**Ошибка:** `bool("false") == True`; loaders также не проверяли точный discriminator
`type`. Поэтому payload с `"fitted":"false"` или model-type mismatch мог быть загружен
как активный LogReg/Platt. Аналогичная проблема была в nested Platt layer.

**Финансовый риск:** повреждённая/чужая schema могла считаться fitted model, менять
confidence и подавлять своевременный refit/fallback.

**Исправление:** loaders требуют:

- точный `type="logreg"` или `type="platt"`;
- настоящий JSON boolean для top-level `fitted`;
- настоящий JSON boolean для nested Platt `fitted`;
- finite coefficients/intercept/a/b.

При нарушении schema loader возвращает `None`; система переходит к штатному безопасному
fallback/refit, а не активирует неоднозначный объект.

**Red→green тесты:** 5 случаев — top-level string boolean, wrong model type, nested string
boolean и два malformed Platt payload.

### HIGH-03 — strict preflight не доказывал account-mode совместимость

**До исправления:** `app/main.py`, account-mode ветка около прежних строк 2971-2977;
`unifiedMarginTrade` кэшировался, но не валидировался.  
**После исправления:** `app/main.py:2281-2295`, `2924`, `2992-3013`.

**Ошибка:**

- explicit unsupported mode (`hedge`, `demo`, и т.п.) был только warning;
- отсутствующий account mode не блокировался;
- `instruments-info.unifiedMarginTrade=false` игнорировался при требуемом
  `account_mode=unified`.

**Финансовый риск:** оператор мог материализовать рекомендацию, доменная execution-модель
которой не соответствует заявленному режиму счёта/инструмента. Для one-way/hedge это
особенно опасно из-за различного `positionIdx` и возможности встретить существующую
позицию неверной стороной.

**Исправление:**

- strict execution path блокирует missing account mode (`ACCOUNT_MODE_MISSING`);
- любой explicit mode кроме `unified` и исторического alias `one_way` блокируется
  (`ACCOUNT_MODE_UNSUPPORTED`);
- explicit `unifiedMarginTrade=false` блокируется
  (`BYBIT_UNIFIED_MARGIN_UNSUPPORTED`);
- legacy `one_way` оставлен warning-only для чтения старых rows и не означает поддержку
  hedge-mode.

**Red→green тесты:** unsupported mode, missing mode, explicit incompatible instrument.

**Важно:** public instrument capability не подтверждает private account truth. Внешний
executor всё ещё обязан проверить authenticated UTA/position mode и `positionIdx=0`.

### MEDIUM-01 — строка `"false"` портила publication lineage backfill

**До исправления:** `app/db.py`, прежняя строка около 276.  
**После исправления:** `app/db.py:35-44`, `284`.

**Ошибка:** `bool(dedupe["active_reuse"])` трактовал строку `"false"` как true и мог
связать независимую рекомендацию с предыдущим publication root.

**Риск:** искажались dedupe/history/outcome-label roots, что способно загрязнять
калибровочную выборку и операторскую историю, хотя реальный ордер напрямую не создавался.

**Исправление:** reuse признаётся только для явного boolean true или строго распознанного
true-string; ambiguous/false values fail-closed трактуются как отсутствие reuse.

**Red→green тест:** независимая публикация со строковым `"false"` остаётся собственным
label root.

### MEDIUM-02 — malformed Telegram `ok` считался успешной доставкой

**До исправления:** `app/alerts.py:45`.  
**После исправления:** `app/alerts.py:45`.

**Ошибка:** `bool(payload.get("ok"))` принимал строку `"false"` за true. При HTTP 200 с
повреждённым/non-canonical JSON alert считался доставленным, а вызывающий код мог поставить
10-минутный cooldown.

**Риск:** оператор не получал повторный alert после фактической ошибки доставки. Это не
меняет торговую математику, но снижает наблюдаемость risk/data outage событий.

**Исправление:** success допускается только при literal JSON boolean `ok is True`.

**Red→green тест:** HTTP 200 + `{"ok":"false"}` обязан вернуть `False`.

---

## 6. Red→green доказательство

Финальный файл `tests/test_iteration199_final_reaudit.py` был скопирован в **нетронутую
повторную распаковку исходного ZIP** и запущен без production fixes:

- **RED:** `21 failed`, exit code 1;
- **GREEN:** `21 passed` после исправлений.

Артефакты:

- `docs/audit_artifacts/2026-06-18_final_reaudit/red_final_untouched_original.txt`;
- `docs/audit_artifacts/2026-06-18_final_reaudit/green_iteration199.txt`.

Ожидаемые значения выведены независимо от тестируемых функций: non-finite probability
должна нейтрально деградировать в `0.5`; persisted schema должна принимать только реальный
boolean/type discriminator; lineage string false не является reuse; unsupported/missing
account mode и explicit incompatible instrument обязаны давать named blocking error.

---

## 7. Econometric / look-ahead re-check

Повторно проверены `app/calibration.py`, `app/outcomes.py` и тесты purged OOF:

- recommendation feature timestamp отделён от будущего outcome availability;
- `_purged_train_indices()` допускает в training только labels, доступные строго до
  validation decision timestamp;
- duplicate timestamp не разрывается между train/validation;
- `fit_logreg()` передаёт `label_available_ts` в chronological OOF;
- outcome entry использует open первой 1m свечи **после** reference candle, а не close той
  свечи, на которой построены features;
- time-series rows сортируются хронологически;
- proxy-label nature остаётся документированным residual risk.

Новой подтверждённой look-ahead утечки не найдено. Small-sample score-only Platt fallback
остаётся advisory и потенциально переоптимистичным на нестационарном рынке; risk/preflight
не должен трактовать его как самостоятельное разрешение сделки.

---

## 8. Bybit V5 verification

Сверка выполнена 2026-06-18 по официальной Bybit V5 documentation:

- `Get Instruments Info` публикует `tickSize`, `qtyStep`, min/max qty,
  `minNotionalValue`, leverage filter и boolean `unifiedMarginTrade`;
- `Place Order`: `side` — `Buy`/`Sell`; `triggerDirection=1` при росте и `2` при падении;
  `positionIdx=0` — one-way; hedge uses `1`/`2`;
- `reduceOnly=true` требуется при закрытии/уменьшении позиции;
- `closeOnTrigger` предназначен для closing conditional order и не должен увеличивать
  позицию;
- order acknowledgement не является fill/reconciliation truth.

Проверенные источники:

- `https://bybit-exchange.github.io/docs/v5/market/instrument`;
- `https://bybit-exchange.github.io/docs/v5/order/create-order`;
- `https://bybit-exchange.github.io/docs/v5/position`;
- `https://bybit-exchange.github.io/docs/v5/position/trading-stop`;
- `https://bybit-exchange.github.io/docs/faq`.

Private API/testnet order placement не выполнялся: проект не содержит OMS/EMS, а аудит не
получал специально выданных безопасных credentials/sandbox account.

---

## 9. Финальные проверки

| Проверка | Результат после исправлений |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| Новые regression tests | **21 passed** |
| Связанный calibration/preflight/directional subset | **112 passed** до финального расширения test-файла |
| Полный `pytest -q` | **800 passed, 0 failed, 0 skipped** |

Baseline → post: **779 → 800 passed**. Ни один из 779 ранее зелёных тестов не упал.

### Static/code-quality

- Выполнен scoped scan по `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`,
  `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`;
- отдельно проверены truthiness/bool coercion и numeric fallback hits;
- production diff сохранён в
  `docs/audit_artifacts/2026-06-18_final_reaudit/production_diff.patch`;
- `ruff==0.15.9` указан в `requirements-dev.txt`, но отсутствовал и не был доступен в
  offline pip cache; попытка зафиксирована в `ruff_install.txt`;
- `package.json`, npm/yarn tests, ESLint, mypy config в репозитории отсутствуют.

---

## 10. Остаточные риски относительно `KNOWN_RISKS.md`

### Закрыто этой итерацией

1. Non-finite confidence inputs/parameters больше не дают extreme probabilities.
2. Persisted calibration schema больше не активируется string truthiness/type mismatch.
3. Strict execution больше не принимает missing/unsupported account mode.
4. Explicit `unifiedMarginTrade=false` блокируется.
5. Publication lineage не считает строку `"false"` активным reuse.
6. Telegram delivery success требует literal boolean `ok=true`.

### Остаётся

1. Нет реального OMS/EMS, fill truth, cancel/retry/idempotency и exchange reconciliation.
2. Public `unifiedMarginTrade` — capability инструмента, а не доказательство private UTA,
   position mode, permissions, balance или existing positions.
3. Exact wallet-based sizing, maintenance tier и liquidation price должны проверяться
   непосредственно перед реальным order creation.
4. Outcome labels остаются proxy, а не фактическим net PnL с реальными fills/fees/funding.
5. Small-sample/non-stationary calibration может быть переоптимистична даже при purged OOF.
6. Public REST snapshots не гарантируют единую временную согласованность.
7. Legacy `account_mode=one_way` сохраняется только как совместимость исторических rows;
   новые executable recommendations должны хранить `account_mode=unified`.

---

## 11. Изменённые файлы

- `app/alerts.py`;
- `app/calibration.py`;
- `app/db.py`;
- `app/main.py`;
- `tests/test_iteration199_final_reaudit.py`;
- `docs/KNOWN_RISKS.md`;
- `docs/AUDIT_REPORT_2026-06-18_final_failclosed_calibration_account_mode.md`;
- `docs/audit_artifacts/2026-06-18_final_reaudit/*`.

Крупный рефакторинг `app/main.py`/`app/recommender.py` не выполнялся. Production diff
ограничен подтверждёнными safety defects.

---

## 12. Итог

Проверка не обнаружила новой инверсии short TP/SL или расхождения backend↔frontend:
canonical directional model остаётся единым источником истины. Найдены и закрыты пять
новых дефектов на trust boundaries — calibration numeric input, persisted model schema,
Bybit account/instrument compatibility и publication lineage. Все исправления fail-closed,
доказаны на нетронутом исходном коде как red→green и не внесли регрессий в полный suite.
