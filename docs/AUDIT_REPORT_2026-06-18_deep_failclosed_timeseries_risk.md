# Консолидированный глубокий аудит: fail-closed числовые границы и целостность временных рядов

**Дата:** 2026-06-18  
**Scope:** Bybit Linear USDT recommender, operator API, persistence, risk limits, OHLCV temporal integrity, local resampling, canonical directional semantics, UI/backend parity  
**Репозиторий:** `bybit-reco-systems-main`

## 1. Контекст, порядок работы и граница системы

До изменений изучены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- последние отчёты `AUDIT_REPORT_2026-06-18_*`, `AUDIT_REPORT_2026-06-17_*` и `AUDIT_REPORT_2026-06-16_ui_exit_math_failclosed.md`.

Подтверждена архитектурная граница: репозиторий является рекомендателем и fail-closed preflight-контуром, но не реальным OMS/EMS. Поэтому lifecycle реальных заявок, fills, partial fills, cancel/retry, exchange reconciliation, wallet truth и private-account state не моделировались фиктивным кодом. Они остаются обязательствами внешнего execution-слоя.

Все изменения минимальны и усиливают fail-closed поведение. Severity существующих guard'ов не снижалась, блокировки не удалялись, fail-open ветви не добавлялись.

## 2. Исходный baseline

| Проверка | Результат до изменений |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| `pytest --collect-only -q` | **750 tests collected** |
| Полный набор, шесть непересекающихся batch-прогонов | **750 passed, 0 failed, 0 skipped** |

Один монолитный запуск `pytest -q` не завершился в лимите командного harness. Для исключения пропусков все 750 собранных test nodes были распределены по непересекающимся пакетам; сумма результатов совпала с collection count.

Артефакты: `docs/audit_artifacts/2026-06-18_deep_failclosed_timeseries_risk/`.

## 3. Зафиксированные торговые и числовые конвенции

- `directional_trade_math` считает **gross directional PnL**: long `qty × (exit-entry)`, short `qty × (entry-exit)`. Fees и funding в этот helper не входят.
- В ручном журнале сделок `pnl` — gross realised PnL до комиссии; net risk accounting использует `pnl - fee`.
- `reward_pct` и `risk_pct` canonical helper считаются от notional `entry × qty`, а не от внесённой маржи.
- Canonical `risk_reward` — отношение gross price-distance reward к gross price-distance risk; неверная геометрия не превращается в положительный показатель.
- Positive funding — adverse carry для long и receipt для short; negative funding — adverse carry для short и receipt для long. Потенциальный receipt не должен улучшать approval edge без консервативного подтверждения.
- Leverage, margin и liquidation buffer в репозитории — расчётные рекомендации/preflight-оценки, а не точная истина конкретного Bybit account tier и текущего mark price.
- Отдельного альтернативного canonical ROI-поля не найдено. `expected_rr` является recommendation/economic score, а не подтверждённым exchange ROI.

## 4. Карта single source of truth

| Область | Место | Результат |
|---|---|---|
| Нормализация long/short/neutral и TP/SL mapping | `app/trading_semantics.py:53-129` | Canonical, fail-closed |
| Directional gross PnL, distance, R:R | `app/trading_semantics.py:158-235` | Canonical |
| Bybit one-way protective side, `positionIdx`, `reduceOnly`, `closeOnTrigger`, trigger direction | `app/trading_semantics.py:348-430` | Canonical |
| Backend payload TP/SL/PnL/R:R | `app/main.py:926-987`, `1620`, `1658`, `3153-3154` | Вызывает canonical helpers |
| Saved state / journal economics | `app/db.py`, `app/risk.py`, `app/outcomes.py`, `app/grid_math.py` | Grid/proxy/net-risk accounting; не альтернативный side/TP/SL mapping |
| UI карточки/детали | `app/ui/static/app.js:650-737`, `1059-1061` | Потребляет backend `directional_exit_levels`; локально только проверяет геометрию и форматирует |
| Alerts/logs/preflight | backend payload и canonical validation | Независимой directional формулы не найдено |
| Реальный order lifecycle | отсутствует | Требование к внешнему executor |

Новой исполнимой directional-модели в обход `app/trading_semantics.py` не найдено. В частности, frontend не пересчитывает TP/SL из upper/lower как самостоятельный источник истины: при missing/mismatch/invalid backend geometry он блокирует directional display.

## 5. Находки и исправления

### HIGH-01 — JSON boolean мог стать нулевым PnL/fee или sentiment numeric

**До исправления:** `app/main.py` и `app/db.py`.  
**После исправления:** `app/main.py:4000-4028`; `app/db.py:637-650`, `1270-1289`, `1746-1797`.

Pydantic permissive numeric coercion принимал JSON `false` как `0`/`0.0`. Дополнительно persistence-path использовал truthiness fallback: `trade.get("pnl") or 0.0`, `fee`, `velocity`, `volume`. Поэтому булевы значения могли сохраниться как математически правдоподобные нули.

**Финансовый риск:** ложный нулевой loss/fee искажает net PnL, daily drawdown, cooldown, outcome evidence и операторский audit trail. Булевый sentiment payload мог выглядеть как реальная количественная точка.

**Исправление:** operator API использует строгие `StrictInt`/`StrictFloat`; timestamps обязаны быть положительными. Persistence различает `None` и явное значение, а boolean/fractional integer отвергается вместо coercion.

### HIGH-02 — нулевой emergency cap ослаблялся дефолтом; дробный integer limit усекался

**До исправления:** `app/risk.py`.  
**После исправления:** `app/risk.py:47-180`.

Fallback-выражения `value or shipped_default` превращали намеренный нулевой cap, например `max_daily_dd_usdt=0` или `max_position_notional_usdt=0`, в более мягкий ненулевой лимит. Значения integer-политик преобразовывались через `int()`, поэтому `cooldown_after_loss_min=0.9` становился `0`, а другие дробные настройки молча меняли смысл.

**Финансовый риск:** операторский hard stop мог незаметно ослабнуть; некорректная настройка могла снизить cooldown или изменить leverage/bot limits.

**Исправление:** zero сохраняется как валидный строгий cap; integer settings принимаются только при точной целочисленности через общий `strict_integer`. Malformed override использует уже нормализованный fallback, а не более мягкий hard-coded default.

### HIGH-03 — удалялась только одна незакрытая свеча

**До исправления:** `app/recommender.py`, прежняя `_drop_open_candle`.  
**После исправления:** `app/recommender.py:1286-1311`.

Старая функция проверяла только первый newest-first row. При нескольких still-forming/future rows, clock skew, retry overlap или malformed timestamp второй незакрытый бар оставался в feature window.

**Quant-риск:** look-ahead/data-availability leakage, нестабильные ATR/volatility/trend/acceleration и расхождение paper/shadow/live semantics.

**Исправление:** каждая строка обязана доказать `timestamp + timeframe <= decision_time`; invalid, boolean, fractional, future и still-open timestamps исключаются. Порядок входных закрытых баров сохраняется.

### HIGH-04 — неполный higher-timeframe bucket выглядел как полноценная свеча

**До исправления:** `app/collector.py`, прежняя `_resample_rows`.  
**После исправления:** `app/collector.py:690-777`.

Старая агрегация выпускала локальную 15m/30m/4h свечу даже при пропущенном исходном баре, частичном bucket или duplicate timestamp. Также отсутствовала строгая проверка единства venue/symbol/source timeframe и OHLCV geometry.

**Quant-риск:** ложная временная полнота, смещённые rolling windows, ATR, volatility, trend strength и calibration features. Исторический пробел скрывался вместо fail-closed отказа от derived candle.

**Исправление:** bucket публикуется только при точном полном наборе contiguous source timestamps. Duplicate, mixed stream, malformed OHLCV, non-finite values, fractional/bool timestamps/timeframes приводят к безопасному отказу; incomplete buckets пропускаются.

### MEDIUM-01 — persistence принимал дробные timestamps/timeframes через `int()`

**До исправления:** `app/db.py`.  
**После исправления:** `app/db.py:637-650`, `858-970`, `1270-1289`, `1746-1797`.

Ticker/OHLCV/sentiment/trade boundaries местами молча усекали `1700000000.9` или принимали boolean как integer-like value.

**Риск:** коллизии временных ключей, неверная candle alignment/freshness и неоднозначный audit trail.

**Исправление:** общий exact-integral guard применяется к `ts`, `tf_sec`, volume и другим integer-полям; positive timestamps проверяются до записи.

## 6. Red→green доказательство

Файл `tests/test_iteration196_deep_failclosed_regression.py` содержит **10 test functions / 12 pytest items**. Те же тесты были скопированы в свежую распаковку исходного ZIP без production changes.

| Состояние | Результат |
|---|---:|
| Исходный код + новые тесты | **12 failed** |
| Исправленный код + те же тесты | **12 passed** |

Покрыты:

1. boolean PnL/fee на DB boundary;
2. boolean velocity/volume в batch sentiment insert;
3. сохранение строгих нулевых risk caps;
4. запрет silent truncation дробных integer limits;
5. фильтрация всех open/future/malformed candles;
6. complete contiguous resampling и отказ при gap/duplicate;
7. строгие operator API models;
8. persistence integer guards;
9. OHLCV exact timeframe/timestamp;
10. ticker exact timestamp.

Ожидаемые значения независимы от функций под тестом: тесты задают явные математические/временные инварианты, а не сравнивают helper с его собственным результатом.

Артефакты: `red_on_original.txt`, `green_static_and_collection.txt`, `production_changes.diff`.

## 7. Post-validation

| Проверка | После исправлений |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| `pytest --collect-only -q` | **762 tests collected** |
| Новые тесты | **12 passed** |
| Полный набор, шесть непересекающихся batch-прогонов | **762 passed, 0 failed, 0 skipped** |

Итого: **750 → 762 passed**. Ни один из исходных 750 зелёных тестов не потерян.

## 8. Static / code-quality и directional regression checks

- Выполнен scoped scan по `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`.
- `docs/STATIC_SCAN_*` отсутствуют, поэтому дифф с прошлым static scan невозможен. Сырые grep-результаты в отчёт не копировались.
- Изменённые unsafe hits классифицированы и закрыты в разделах HIGH-01…HIGH-04/MEDIUM-01.
- Canonical long/short TP/SL, PnL, R:R и Bybit protective mapping не изменялись; существующие backend↔frontend parity/directional suites остались зелёными.
- `ruff` указан в `requirements-dev.txt`, но executable в audit environment отсутствовал; lint не выполнен.
- `package.json` и JS test/lint config отсутствуют; доступная JS-проверка — `node --check`, она прошла.

## 9. Сверка с Bybit V5

Проверены официальные V5-разделы Kline, WebSocket Kline, Place Order, Instruments Info и FAQ:

- REST kline для незакрытого интервала содержит текущий `closePrice`, поэтому такой бар нельзя считать закрытым decision-time observation;
- в WebSocket kline закрытие обозначается `confirm=true`;
- one-way protective close использует противоположный side; `reduceOnly`/`closeOnTrigger` предотвращают увеличение позиции защитным ордером;
- tick/qty/min-notional constraints должны браться из актуального Instruments Info и повторно проверяться внешним executor перед отправкой.

В этом аудите private API/testnet order placement не запускался: безопасные credentials не предоставлены, а реальный OMS/EMS отсутствует в границе репозитория.

## 10. Остаточные риски относительно `KNOWN_RISKS.md`

**Закрыто в этой итерации:**

1. boolean-to-zero coercion на operator API и persistence boundaries;
2. ослабление intentional zero risk caps;
3. silent truncation audited integer limits/timestamps;
4. использование нескольких still-open/future candles в recommendation features;
5. публикация incomplete local higher-timeframe buckets.

**Остаётся:**

1. Нет реального OMS/EMS, fill truth, idempotent order lifecycle и exchange reconciliation.
2. Proxy outcomes не равны фактическому net PnL с fills, slippage, funding и liquidation effects.
3. Calibration на малых/нестационарных выборках остаётся advisory; purged/walk-forward защита не отменяет model risk.
4. Хранение текущей source candle в market-data storage само по себе разрешено; scoring теперь использует только доказанно закрытые бары.
5. Exact account-mode, wallet, maintenance margin tier, liquidation price, current instrument filters и order acceptance должны быть повторно подтверждены внешним executor непосредственно перед торговым действием.
6. Partial fill, cancel/retry, rate limit, timeout, invalid key, insufficient balance и local↔exchange divergence остаются требованиями внешнего execution-слоя, а не исправленными функциями этого репозитория.
7. `ruff`/полный configured static type/lint gate не был доступен в текущем окружении.

## 11. Изменённые файлы

- `app/db.py`;
- `app/main.py`;
- `app/risk.py`;
- `app/recommender.py`;
- `app/collector.py`;
- `tests/test_iteration196_deep_failclosed_regression.py`;
- `docs/AUDIT_REPORT_2026-06-18_deep_failclosed_timeseries_risk.md`;
- `docs/audit_artifacts/2026-06-18_deep_failclosed_timeseries_risk/*`.

## 12. Итог

Исправлены четыре HIGH и одна MEDIUM проблема без изменения canonical directional-модели и без добавления несуществующего OMS-кода. Числовые границы стали строгими, нулевые emergency caps сохраняются, recommendation features не используют недоказанно закрытые свечи, а local resampling больше не скрывает gaps. Все новые тесты доказаны как red→green; полный regression suite после изменений зелёный.
