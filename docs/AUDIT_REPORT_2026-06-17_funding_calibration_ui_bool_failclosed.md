# Консолидированный regression audit — funding / calibration / operator UI boolean fail-closed

**Дата:** 2026-06-17  
**Scope:** Bybit V5 Linear USDT recommender, market-data persistence, funding/OI features, calibration boundary, operator UI numeric rendering/ranking  
**Исходный архив:** `bybit-reco-systems-main(1).zip`  
**SHA-256 исходного архива:** `5f30ae674c78087988e914815d5b75384375db6f4dbb7a3c9b150ef28584af07`

## 1. Итог

Новых дефектов уровня `CRITICAL` не найдено. Найдены и исправлены четыре связанные остаточные регрессии класса `bool -> 1/0`:

1. **HIGH:** обязательные funding/open-interest поля могли принять JSON boolean как реальный rate/OI/timestamp.
2. **HIGH:** calibration boundary могла принять boolean outcome labels/timestamps и построить формально fitted модель на семантически повреждённых данных.
3. **HIGH:** operator UI мог включить boolean score в ranking и показать boolean confidence/correlation/factor weight как достоверное числовое значение.
4. **MEDIUM:** общие UI formatters отображали booleans как цену, процент, bps, USD и probability (`true -> 1/100%`, `false -> 0/$0`).

Все production-изменения ужесточают входные границы. Fail-closed guards, risk limits, publication gates и severity не ослаблялись. Каноническая directional-модель не изменялась. Реальный OMS/EMS не добавлялся.

Полный набор тестов после исправлений: **732 passed / 0 failed / 0 skipped** против baseline **727 / 0 / 0**.

## 2. Обязательный pre-read и контекст прошлых аудитов

До production-правок изучены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- последние аудиты от 2026-06-15—2026-06-17, включая full-system, UI numeric fail-closed, exit-math, boolean/purged OOF и remaining numeric fail-closed.

Прошлые отчёты подтвердили, что canonical long/short TP/SL, gross PnL/R:R, Bybit one-way side mapping, protective trigger semantics и большая часть boolean boundaries уже закрыты. Поэтому текущая работа выполнялась как регрессионный поиск **оставшихся** numeric coercion paths, а не как повторный аудит «с чистого листа».

## 3. Граница системы

По `docs/KNOWN_RISKS.md` и фактическому коду репозиторий является:

- рекомендательным сервисом;
- persistence/operator UI контуром;
- fail-closed risk/Bybit metadata/execution preflight;
- локальной materialization-моделью `bot_instance` и operator trade records.

В репозитории нет полноценного private Bybit OMS/EMS, который отправляет реальные ордера и сопровождает их lifecycle. Поэтому partial fill, cancel/replace, retry/idempotency реальных заявок, private rate limits, insufficient balance, unknown exchange positions и exchange reconciliation не объявлялись дефектами несуществующего кода. Они остаются требованиями к внешнему executor/reconciliation layer.

## 4. Baseline до правок

Команды выполнены до production-изменений:

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest -vv                                   727 passed / 0 failed / 0 skipped
```

Полный pytest напечатал итог `727 passed in 33.17s`. В данном контейнерном запуске оболочка не завершилась штатно после печати summary и была ограничена внешним timeout; тестовых падений и оставшегося процесса pytest после остановки не было. Это отдельно отмечено как низкоприоритетный инфраструктурный residual, а не скрыто как зелёный exit-code.

Baseline artifacts:

- `audit_artifacts/baseline_compileall.txt`;
- `audit_artifacts/baseline_node_check.txt`;
- `audit_artifacts/baseline_pytest_vv_timeout.txt`.

## 5. Зафиксированные знаки, единицы и конвенции

### 5.1 Directional TP/SL

Источник истины: `app/trading_semantics.py`.

- long: `TP > entry`, `SL < entry`, прибыль при росте цены;
- short: `TP < entry`, `SL > entry`, прибыль при падении цены;
- neutral/grid: одиночные directional TP/SL не используются как у направленной позиции;
- invalid/missing geometry должна стать invalid/hidden/blocked, а не автоматически переставляться.

### 5.2 PnL, ROI и R:R

- `directional_trade_math`: gross linear-USDT price PnL без fees/funding;
- `gross_profit_usdt` и `gross_loss_usdt`: положительные абсолютные величины;
- `risk_reward = gross_profit / gross_loss`;
- reward/risk percent считаются относительно entry notional, а не как ROI на margin;
- leverage не умножает price PnL; он влияет на margin и liquidation exposure;
- net grid economics в `app/grid_math.py` отдельно учитывает fees и adverse funding.

### 5.3 Funding

- положительный funding rate: long платит, short получает;
- отрицательный funding rate: long получает, short платит;
- approval economics не должна считать потенциальный funding receipt гарантированной прибылью;
- malformed/boolean rate, interval или timestamps не должны превращаться в 0/1.

### 5.4 Margin/liquidation

- margin estimate = notional / leverage;
- liquidation price и buffer в этом проекте являются conservative approximation;
- точная liquidation truth требует private account state, mark price и risk tier и относится к внешнему execution layer.

## 6. Карта single source of truth и всех исполнимых/отображающих мест

### 6.1 Каноническая directional-семантика

- `app/trading_semantics.py`
  - `normalize_execution_direction`;
  - `directional_exit_levels`;
  - `validate_directional_exit_geometry`;
  - `directional_trade_math`;
  - `bybit_linear_order_semantics`;
  - `bybit_linear_protective_order_semantics`;
  - `bybit_linear_protective_order_plan`.
- `app/grid_math.py`
  - signed linear PnL;
  - funding cashflow sign;
  - fees/net grid economics;
  - margin and approximate liquidation calculations.

Любой новый расчёт directional side/TP/SL/PnL/R:R вне этих модулей должен рассматриваться как потенциальный обход source of truth.

### 6.2 Backend/API/preflight

- `app/main.py::_trade_plan_price_context` — entry/range/kill-switch/grid context;
- `app/main.py::_directional_exit_qty_for_reco` — qty provenance для exit math;
- `app/main.py::_directional_exit_payload_for_reco` — canonical API/UI exit payload;
- `app/main.py::_validate_trade_plan_against_bybit_meta` — geometry, tick, qty, min-notional, leverage, instrument checks;
- `app/main.py::_signed_funding_bps_for_direction` и `_execution_funding_blocks` — directional funding effect;
- `app/main.py::_execution_runtime_size_risk_blocks` — execution-time size/risk checks;
- `app/main.py::_execution_symbol_direction_conflict_blocks` — one-way same-symbol direction guard;
- `app/recommender.py` — direction aggregation/stabilization, grid geometry, funding adjustment, leverage/liquidation/capital estimates, publication confirmation;
- `app/risk.py`, `app/shock_guard.py` — caps, drawdown/cooldown, market/symbol shock gates;
- `app/bybit_client.py`, `app/collector.py` — public Bybit market/instrument/funding boundary.

### 6.3 Persistence, outcomes и econometrics

- `app/db.py` — OHLCV/ticker/funding/OI/recommendation/bot/trade/outcome/risk state;
- `app/outcomes.py` — proxy label, signed directional return, grid TP hit и adverse funding adjustment;
- `app/calibration.py` — sanitization, chronological/purged OOF, LogReg/Platt;
- `app/direction.py`, `app/features.py`, `app/sentiment.py`, `app/sentiment_features.py` — model features и direction inputs.

### 6.4 Frontend/operator UI

- `app/ui/static/app.js::operatorExitLevels`, `directionalExitGeometryOk`, `directionalExitMathForDisplay`, `operatorExitLevelsFromBackend` — parsing/rendering backend exits;
- `buildRiskEconomicsFields`, `buildPriceFreshnessFields`, `launchDecisionDiagnostics` — risk/economics/operator state;
- `computeUiScoreMetaMap`, `ensureUiScoreMeta` — presentation ranking, не execution permission;
- `confCell`, `dirConfCell`, `btcRelationMetric`, `factorItemHtml` — confidence/relationship/factor display;
- numeric formatters `fmt`, `formatDotNumber`, `formatBybitPrice`, `formatPercentDot`, `formatBps`, `formatUsdValue`, `formatProbability`.

UI не является самостоятельным источником TP/SL geometry. Он должен использовать backend `directional_exit_levels`, проверять direction/geometry и скрывать invalid/malformed значения.

### 6.5 Alerts, logs, audit, manual controls и modes

- `app/alerts.py` и логи отображают advisory state, но не маршрутизируют ордера;
- audit reports/docs документируют semantics, но не исполняют её;
- manual execution подтверждается backend risk/metadata/preflight, а не цветом/числом в UI;
- paper/shadow/live OMS parity не может быть проверена, поскольку live OMS отсутствует; проверена parity существующего canonical backend payload и UI parsing/rendering.

## 7. Findings и исправления

### HIGH-01 — funding/OI persistence и feature layer принимали booleans как рыночные числа

**Файлы и актуальные строки:**

- `app/db.py:2088-2129` — funding normalization;
- `app/db.py:2164-2179` — open-interest normalization;
- `app/features.py:167-185` — liquidity tier;
- `app/features.py:190-240` — funding signal/interval;
- `app/features.py:245-292` — OI trend row sanitization.

**До исправления:**

- `funding_rate=true` становился `1.0`;
- `oi=false` становился `0.0`;
- boolean timestamps могли стать integer `1/0`;
- `turnover24h=true` мог классифицироваться как реальная микроликвидность;
- feature helpers продолжали вычислять signal/trend на семантически нечисловом JSON payload.

**Почему это ошибка:** Python `bool` является подклассом `int`, поэтому обычный `float()`/`int()` не отличает boolean от числа.

**Риск:** ложный funding/OI/liquidity state может исказить score, direction context, crowding assessment, funding carry и операторское решение. Даже без встроенного OMS это safety-critical boundary для recommendation/preflight.

**Исправление:**

- обязательные boolean `ts`, `funding_rate`, `oi` отклоняются целиком;
- optional boolean `next_funding_ts` и `funding_interval_min` становятся unavailable (`None`), а не `1/0`;
- boolean turnover возвращает `unknown`;
- boolean funding rate возвращает unknown funding signal;
- boolean funding interval использует documented neutral fallback 480 minutes;
- OI rows с boolean OI/timestamp пропускаются; при недостатке валидной истории результат `unknown`.

**Безопасность:** только ужесточение; валидные numeric values и numeric strings сохраняют совместимость.

### HIGH-02 — calibration принимала boolean labels/timestamps

**Файл и строки:** `app/calibration.py:563-605`, ключевой guard `590-591`.

**До исправления:** 100 строк с `success=True/False` успешно превращались в labels `1/0`; модель возвращала `fitted=True`, `n_samples=100`. Boolean timestamp аналогично мог стать `1`.

**Почему это ошибка:** boolean outcome не является доказанным numeric class label на schema boundary. Его молчаливое принятие скрывает malformed producer и может загрязнить calibration dataset.

**Эконометрический риск:** модель могла получить ложную fitted-state и probability-like confidence на повреждённых labels/timestamps. Это искажает ranking и diagnostics, даже если downstream risk/preflight guards остаются активны.

**Исправление:** строки с boolean `success` или `ts` исключаются до `int()`/sorting/fit. При отсутствии достаточных валидных строк калибратор остаётся unfitted.

**Безопасность:** fail-closed; thresholds и time-aware/purged OOF не ослаблены.

### HIGH-03 — UI ranking/confidence/correlation/factor weights принимали booleans

**Файл и строки:**

- `app/ui/static/app.js:293-320` — BTC correlation/window;
- `app/ui/static/app.js:336-399` — score ranking/fallback raw score;
- `app/ui/static/app.js:1045-1057` — factor weight;
- `app/ui/static/app.js:1280-1307` — confidence/model metadata;
- `app/ui/static/app.js:1432-1439` — directional confidence.

**До исправления:**

- `score=true` включался в ranking как `1`, мог занять верхнюю позицию;
- `confidence=true` отображался как `1.00`;
- `direction_confidence=false` отображался как `0.00`;
- `correlation=true` отображался как `r=1`;
- `factor.weight=true` отображался как `+1`.

**Риск:** оператор мог получить ложный высокий rank, уверенность, сильную BTC-корреляцию или вес фактора. UI не отправляет ордер сам, но влияет на ручное решение и поэтому не должен маскировать malformed payload как достоверную метрику.

**Исправление:** все перечисленные значения проходят общий `toFiniteNumber`, который отвергает booleans. Invalid metrics отображаются `—`/`-` и исключаются из ranking.

**Безопасность:** execution permission не расширена; отображение стало консервативнее.

### MEDIUM-01 — общие UI formatters преобразовывали booleans в цену/процент/деньги

**Файл и строки:** `app/ui/static/app.js:25-157`.

**До исправления:**

```text
fmt(true)                 -> 1.00
formatBybitPrice(true)    -> 1.00
formatPercentDot(false)   -> 0%
formatBps(true)           -> 1 bps
formatUsdValue(false)     -> $0
formatProbability(true)   -> 100%
```

**Риск:** missing/malformed state выглядел как реальная цена, нулевой процент/стоимость или 100% вероятность. Это могло скрывать проблему data contract.

**Исправление:** форматтеры используют canonical JS numeric boundary `toFiniteNumber`; boolean становится unavailable. Числовые `0/1` остаются валидными и корректно отображаются.

### LOW-01 — stale JS cache после safety patch

**Файл и строки:** `app/ui/static/index.html:7,126`.

**Риск:** браузер мог продолжить использовать старый JS и сохранить boolean coercion после backend update.

**Исправление:** asset cache key повышен `manual-ui-v42 -> manual-ui-v43`. Связанные snapshot/assertion tests обновлены только на новый ключ; торговая логика этих тестов не менялась.

## 8. Red → green доказательство

Новый regression file создан и запущен **до production fixes**:

`tests/test_iteration191_funding_calibration_ui_numeric_failclosed.py`

### Red на исходном коде

```text
5 failed
```

Падали все пять новых тестов:

1. DB funding/OI boundaries не отклоняли booleans;
2. funding/OI/liquidity feature helpers превращали booleans в числа;
3. calibration строила fitted model на boolean labels/timestamps;
4. UI numeric formatters отображали `true/false` как `1/0`;
5. UI ranking/confidence/BTC metrics принимали booleans.

Полный red artifact: `audit_artifacts/iteration191_red_final.txt`.

### Green после исправления

Targeted regression set:

```text
23 passed
```

Artifact: `audit_artifacts/iteration191_green_final_targeted.txt`.

### Независимость expected values

Тесты не сравнивают функцию с её собственным выводом:

- DB/features/calibration expected semantics заданы явно (`None`, `unknown`, `unfitted`, `n_samples=0`);
- JS исполняется через Node на извлечённых production functions;
- ожидаемые строки для booleans и валидных чисел заданы независимо;
- ranking test проверяет, что boolean row отсутствует, а numeric row остаётся.

## 9. Full post-verification

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest -vv                                   732 passed / 0 failed / 0 skipped
```

Полный pytest summary: `732 passed in 32.79s`.

| Стадия | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Baseline | 727 | 0 | 0 |
| Post | 732 | 0 | 0 |
| Δ | +5 | 0 | 0 |

Ни один ранее зелёный тест не упал.

Post artifacts:

- `audit_artifacts/post_compileall.txt`;
- `audit_artifacts/post_node_check.txt`;
- `audit_artifacts/post_pytest_vv_timeout.txt`.

Как и baseline, контейнерная оболочка потребовала timeout после уже напечатанного полного pytest summary. Это не изменяет тестовые counts, но должно быть отдельно воспроизведено в CI с thread/process diagnostics.

## 10. Static/code-quality review

Выполнен scoped scan по:

`tp`, `sl`, `take_profit`, `stop_loss`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `closeOnTrigger`, `triggerDirection`, `kill`, `leverage`, `pnl`, `roi`, `risk_reward`, funding, OI и JavaScript `Number(...)`.

Artifacts:

- `audit_artifacts/static_semantics_scan.txt`;
- `audit_artifacts/js_numeric_coercions.txt`.

Результат triage:

- новых обходов canonical directional TP/SL/side/PnL geometry не найдено;
- UI directional exits продолжают использовать backend `directional_exit_levels` и fail-closed geometry checks;
- найденные новые hits относились к remaining boolean numeric boundaries и исправлены;
- сырой grep не объявлялся списком багов: каждый изменённый path был воспроизведён тестом;
- реального Bybit order submission кода нет.

Недоступные проверки:

- `ruff check app tests main.py`: executable `ruff` отсутствует в текущей среде, несмотря на запись в `requirements-dev.txt`; artifact `audit_artifacts/post_ruff_check.txt`;
- `mypy`: не настроен/не установлен как обязательный project command;
- npm/yarn tests/lint: `package.json` отсутствует;
- private Bybit wallet/positions/order lifecycle/testnet order placement: нет credentials и нет OMS/EMS слоя;
- exact liquidation/account risk tier: невозможно без private account state.

## 11. Bybit V5 correctness cross-check

На дату аудита повторно проверены официальные Bybit V5 документы:

- Place Order: `https://bybit-exchange.github.io/docs/v5/order/create-order`;
- Get Instruments Info: `https://bybit-exchange.github.io/docs/v5/market/instrument`;
- Switch Position Mode: `https://bybit-exchange.github.io/docs/v5/position/position-mode`;
- Set Trading Stop: `https://bybit-exchange.github.io/docs/v5/position/trading-stop`.

Подтверждено:

- linear order side остаётся `Buy`/`Sell`;
- one-way mode использует `positionIdx=0` и не допускает одновременный long+short одного символа;
- instrument metadata содержит `tickSize`, `qtyStep`, `minOrderQty`, `minNotionalValue`, leverage limits;
- reduce-only/close-on-trigger semantics должны не увеличивать позицию;
- репозиторий корректно трактует эти поля как preflight inputs, но не как замену exchange execution truth.

Текущий patch не меняет Bybit order mapping. Он предотвращает попадание boolean values в market/funding/OI/calibration/UI numeric contracts.

## 12. Изменённые файлы

Production:

- `app/db.py`;
- `app/features.py`;
- `app/calibration.py`;
- `app/ui/static/app.js`;
- `app/ui/static/index.html`.

Tests:

- новый `tests/test_iteration191_funding_calibration_ui_numeric_failclosed.py`;
- существующие UI tests обновлены только с cache key `manual-ui-v42` на `manual-ui-v43`.

Audit artifacts/report:

- `audit_artifacts/*`;
- данный отчёт.

## 13. Остаточные риски относительно `docs/KNOWN_RISKS.md`

Ни один заявленный системный residual не был ошибочно объявлен закрытым:

1. **Нет OMS/EMS** — остаётся главным ограничением.
2. **Live qty/min-notional/account balance truth** — остаётся обязанностью executor.
3. **Outcome labels proxy-based** — остаются непригодными как единственный источник реального PnL/WR.
4. **SQLite single-node limitation** — без изменений.
5. **Public REST temporal consistency** — без изменений; malformed booleans теперь блокируются раньше.
6. **Exact liquidation/cross/hedge mode** — не поддерживаются.
7. **External reconciliation** — обязателен.
8. **UI infographic/display is not executable contract** — без изменений.
9. **Calibration small/non-stationary sample risk** — остаётся; текущий patch лишь исключает явно невалидные labels/timestamps.

Новый residual:

- **LOW:** full pytest печатает успешный summary, но текущая container shell invocation не завершается без внешнего timeout. Следует проверить shutdown фоновых scheduler/worker threads в отдельном CI job с thread dump. Это не повлияло на test counts и не оправдывает отключение тестов.

## 14. Финальный вывод

После исправления boolean больше не маскируется под funding rate, OI, liquidity, calibration label/timestamp, UI score/confidence/correlation/factor weight, price, percent, bps, USD или probability. Numeric `0/1` и numeric strings остаются совместимыми. Canonical long/short semantics и fail-closed preflight не ослаблены. Полный набор тестов расширен на пять доказанных red→green regressions и остаётся зелёным.
