# Регрессионный аудит истории, outcome-direction и label horizon — 2026-06-18

**Scope:** Bybit V5 Linear USDT recommender / operator history UI / proxy outcomes / expected R:R / fail-closed regression  
**Репозиторий:** `bybit-reco-systems-main`  
**Исходный ZIP SHA-256:** `6321a2988efad68bab88c528f23f0c2ae0a9bb99cb0d11fc95ee849f320121d2`

## 1. Итог

Исполнено дополнительное требование: в диалоге **«История и динамика»** строки таблицы теперь идут от новых к старым. При одинаковой секунде выше показывается публикация с большим `sequence`; повреждённые даты помещаются в конец. Хронологический API-массив и SVG-график намеренно не разворачивались: временная ось по-прежнему идёт от прошлого к настоящему.

Параллельный регрессионный аудит подтвердил и исправил ещё три дефекта:

1. **HIGH:** legacy-направление `" SHORT "` проходило проверку допустимости, но затем рассчитывалось как long в proxy-outcome return/TP.
2. **HIGH:** `label_horizon_hours=true` интерпретировался как один час и после bounds превращался в 6 часов вместо штатных 12 часов futures-grid.
3. **MEDIUM:** валидный `coherence=0.0` заменялся default `0.5`, завышая expected R:R.

Все изменения минимальны, не добавляют live OMS/EMS и не ослабляют fail-closed guards. Полный suite: **762 → 767 passed**, 0 failed, 0 skipped.

## 2. Обязательный pre-read и граница системы

Перед правками проверены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- последние отчёты `docs/AUDIT_REPORT_2026-06-18_*` и предшествующие regression-аудиты.

Подтверждённая граница: проект является рекомендателем, operator/audit UI и fail-closed preflight-контуром. В репозитории нет реального Bybit OMS/EMS, websocket fill truth, cancel/replace lifecycle, partial-fill recovery и exchange reconciliation. Такие компоненты не выдумывались и не покрывались фиктивными зелёными тестами.

## 3. Зафиксированные конвенции

- Directional gross PnL: long выигрывает при `exit > entry`, short — при `exit < entry`.
- Комиссии и funding учитываются отдельно от gross directional price return.
- Expected R:R в audited helper является recommendation proxy, а не доказанным net-after-fill результатом.
- Положительный funding rate является расходом long и потенциальным поступлением short; receipt не должен автоматически повышать approval edge.
- Neutral/grid не получает одиночный directional TP/SL; используется range/kill-switch geometry.
- One-way Bybit Linear mapping: `positionIdx=0`; close/protective side противоположен open side; protective orders должны быть reduce-only и close-on-trigger; trigger direction зависит от long/short и TP/SL.
- Exact wallet margin, liquidation tier и фактические fills остаются обязанностью внешнего executor/reconciliation слоя.

## 4. Baseline и post-validation

Среда и команды сохранены в `docs/audit_artifacts/2026-06-18_history_horizon_direction/`.

| Проверка | Baseline исходного ZIP | После исправлений |
|---|---:|---:|
| `python -m compileall -q app tests main.py` | PASS | PASS |
| `node --check app/ui/static/app.js` | PASS | PASS |
| `pytest --collect-only -q` | 762 tests | 767 tests |
| `pytest -q` | **762 passed** | **767 passed** |
| failed / skipped | 0 / 0 | 0 / 0 |

Полный baseline и post выполнены едиными запусками, а не выборочными заменами. Время: baseline 36.19 s; post 35.70 s.

## 5. Карта directional single source of truth

| Область | Место | Вывод |
|---|---|---|
| Нормализация execution direction | `app/trading_semantics.py` | Канонический источник long/short/neutral |
| TP/SL geometry, gross PnL, distance, R:R | `app/trading_semantics.py` | Каноническая directional модель |
| Bybit open/close side, `positionIdx`, `reduceOnly`, `closeOnTrigger`, trigger direction | `app/trading_semantics.py` | Канонический one-way mapping |
| Backend payload / preflight validation | `app/main.py` | Использует canonical helpers |
| Grid economics, funding, approximate liquidation | `app/grid_math.py` | Отдельные консервативные numeric primitives; не создают альтернативные TP/SL levels |
| Proxy outcome labeling | `app/outcomes.py` | После исправления нормализует direction через canonical module |
| UI directional display | `app/ui/static/app.js` | Потребляет backend levels и выполняет defensive validation; не создаёт execution orders |
| Реальные ордера/fills/reconciliation | отсутствуют | Требование к внешнему executor |

Scoped static scan не выявил нового исполнимого side/TP/SL mapping в обход `app/trading_semantics.py`. Найденный residual drift был именно в proxy-outcome арифметике и устранён.

## 6. Findings и исправления

### HIGH-01 — legacy/casing direction мог инвертировать proxy-outcome

**Файл до/после:** `app/outcomes.py:7`, `261-270`, `317-328`, `558-561`.  
**Тест:** `tests/test_iteration197_history_horizon_rr_regression.py:56-60`.

**До исправления:** `_is_supported_direction()` нормализовал строку через `strip().lower()`, поэтому `" SHORT "` считался допустимым. Но `_signed_return()` и `_grid_tp_hit()` сравнивали исходную строку с точным `"short"`; всё остальное неявно становилось long.

**Риск:** falling market для legacy short мог получить отрицательный signed return, а верхний барьер мог быть принят за short TP. Это искажало proxy-return/success и последующую диагностику/калибровку. Дефект не создавал реальный ордер, но портил label semantics.

**Исправление:** worker и оба helper используют `normalize_execution_direction()` из canonical module. Long и short обрабатываются явно; neutral/unknown не получают directional return или односторонний TP success. Invalid direction остаётся fail-closed и пропускается worker-ом.

### HIGH-02 — boolean label horizon преждевременно созревал через 6 часов

**Файлы:** `app/outcomes.py:22-34`, `app/db.py:337-349`.  
**Тест:** `tests/test_iteration197_history_horizon_rr_regression.py:12-26`.

**До исправления:** в Python `bool` является подклассом `int`; `float(True)` давал `1.0`. Для futures-grid значение затем ограничивалось нижней границей 6 часов. Canonical built-in horizon 12 часов не применялся.

**Риск:** malformed manual/legacy row мог быть размечен раньше доступности полного будущего окна. Та же ошибка была продублирована в DB lineage/backfill resolver, создавая runtime ↔ repair divergence и потенциальное загрязнение временной калибровки.

**Исправление:** boolean отклоняется до numeric coercion в обоих resolver-ах. После этого используется штатный `BOT_HORIZONS[futures_grid] = 12h`. Guard не ослаблен; malformed input не получает более короткое окно.

### MEDIUM-01 — `coherence=0.0` завышал expected R:R

**Файл:** `app/recommender.py:1917-1928`.  
**Тест:** `tests/test_iteration197_history_horizon_rr_regression.py:29-47`.

**До исправления:** `agg.get("coherence") or 0.5` превращал математически валидный ноль в 0.5. Аналогичный truthiness pattern затрагивал fallback trendiness/ATR selection.

**Риск:** gross capture proxy увеличивался на ненаблюдаемую когерентность и мог сделать слабую рекомендацию более привлекательной в operator UI/filters.

**Исправление:** fallback применяется только к `None`/invalid/non-finite. Ноль сохраняется. Новый тест независимо выводит ожидаемое значение: `0.00734 / 0.03 = 0.244666…`, а не сравнивает функцию с её же выводом.

### MEDIUM-02 — таблица «История и динамика» показывала старые строки сверху

**Файлы:** `app/ui/static/app.js:1915-1935`, `1938`, `1967`; `app/ui/static/index.html:126`.  
**Тест:** `tests/test_iteration195_recommendation_history_ui.py:210-247`.

**До исправления:** API корректно возвращал хронологический массив, и тот же порядок без преобразования использовался таблицей. Оператору приходилось прокручивать вниз к актуальным событиям.

**Исправление:** добавлен pure helper, который сортирует копию массива:

1. `ts DESC`;
2. при одинаковом `ts` — `sequence DESC`;
3. затем deterministic `rec_id DESC`;
4. invalid timestamp — в конец.

Только таблица получает reversed copy. Summary/latest и SVG timeline продолжают использовать исходный chronological array. Тест также доказывает, что исходный массив не мутируется. Cache key изменён на `manual-ui-v45-history-desc-1`, чтобы браузер не продолжал исполнять старый JS.

## 7. Red → green доказательство

Новые тесты были скопированы в нетронутую baseline-распаковку и выполнены до production fixes:

```text
RED: 5 failed, exit code 1
```

Зафиксированные падения:

- boolean horizon: `21600` вместо `43200`;
- expected R:R: `0.294666…` вместо independently-derived `0.244666…`;
- отсутствующий новый frontend cache key;
- legacy `" SHORT "`: `-0.1` вместо `+0.1`;
- отсутствующий newest-first UI helper.

После исправлений:

```text
GREEN: 5 passed
FULL POST: 767 passed
```

Артефакты:

- `red_new_tests.txt`;
- `green_new_tests.txt`;
- `baseline_pytest.txt`;
- `post_pytest.txt`.

## 8. Static / code-quality проверки

Выполнено:

- Python compileall;
- JavaScript syntax check через Node;
- полный pytest;
- focused scan по canonical semantics, TP/SL, PnL, trigger direction, `reduceOnly`, `closeOnTrigger`, `positionIdx`;
- diff против исходной распаковки;
- отдельный red→green запуск.

Не выполнено:

- npm/yarn tests, ESLint, mypy, ruff — соответствующие project configs/commands отсутствуют; `ruff` и `mypy` не установлены;
- private Bybit/testnet order placement — нет безопасно предоставленных credentials и реального OMS/EMS;
- partial fill/retry/reconciliation integration — соответствующий execution слой отсутствует и не должен имитироваться внутри recommender.

## 9. Bybit V5 сверка

Проверены официальные V5-контракты:

- one-way mode использует `positionIdx=0`;
- `Buy`/`Sell` задают сторону order, а close order должен уменьшать существующую противоположную позицию;
- `reduceOnly=true` запрещает защитному/закрывающему ордеру увеличивать позицию;
- conditional order использует `triggerDirection=1` для роста и `2` для падения;
- подтверждение REST create-order не является доказательством fill и требует внешнего websocket/reconciliation контроля;
- instrument filters (`tickSize`, qty step/min/max, minimum notional) должны повторно проверяться непосредственно перед реальным order submit.

Официальные источники, проверенные 2026-06-18:

- `https://bybit-exchange.github.io/docs/v5/order/create-order`;
- `https://bybit-exchange.github.io/docs/v5/market/instrument`;
- `https://bybit-exchange.github.io/docs/v5/position/position-mode`;
- `https://bybit-exchange.github.io/docs/v5/position/trading-stop`.

## 10. Остаточные риски относительно `KNOWN_RISKS.md`

**Закрыто в этой ревизии:**

1. Proxy-outcome direction не расходится с canonical normalizer на casing/whitespace legacy values.
2. Boolean label horizon не сокращает 12-часовое окно.
3. Expected R:R сохраняет observed zero coherence.
4. History table соответствует operator workflow: latest-first без нарушения graph chronology.

**Остаётся:**

1. Proxy outcomes не равны фактическому net PnL: отсутствуют fills, slippage truth, exact fees/funding и liquidation.
2. Исторический dialog ограничен последними 2000 строками и показывает publications, а не exchange lifecycle.
3. Exact account balance, position state, risk tier, min notional и order limits должны проверяться внешним executor непосредственно перед order submit.
4. Public REST snapshots не гарантируют атомарную временную согласованность.
5. SQLite остаётся ограничением при высоком write concurrency.
6. Calibration при малой/нестационарной выборке остаётся advisory; исправленные labels уменьшают semantic drift, но не доказывают устойчивую predictive validity.

## 11. Изменённые файлы

```text
app/outcomes.py
app/db.py
app/recommender.py
app/ui/static/app.js
app/ui/static/index.html
tests/test_iteration195_recommendation_history_ui.py
tests/test_iteration197_history_horizon_rr_regression.py
docs/KNOWN_RISKS.md
docs/AUDIT_REPORT_2026-06-18_history_horizon_direction.md
docs/AUDIT_PROMPT_2026-06-18_UPDATED.md
docs/audit_artifacts/2026-06-18_history_horizon_direction/*
CHANGELOG.md
```

## 12. Вывод

Дополнительное требование по убыванию даты реализовано без изменения backend chronology и без регрессии графика. Одновременно закрыты два HIGH-дефекта proxy-label semantics и один MEDIUM-дефект expected R:R. Все новые проверки доказаны red→green; полный исходный regression suite сохранён. Итог: **767/767 passed**.
