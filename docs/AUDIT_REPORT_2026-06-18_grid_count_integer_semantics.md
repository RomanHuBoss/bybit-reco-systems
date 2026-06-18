# Консолидированный deep regression audit — grid-count integer semantics

**Дата:** 2026-06-18  
**Scope:** Bybit Linear USDT Futures recommender, fail-closed execution preflight, grid economics/sizing, proxy outcomes/calibration boundary и operator UI  
**Исходный архив:** `bybit-reco-systems-main.zip`  
**SHA-256 исходного архива:** `a1e41b1a9505590f67433a042128853979a434071abc4cd909d585dfd32a133f`

## 1. Итог

Новых дефектов уровня `CRITICAL` не найдено. Найдены и исправлены две связанные проблемы уровня `HIGH` и одна UI-проблема уровня `MEDIUM`:

1. **HIGH:** `grid_count`/`grid_levels` и связанные alias-поля принимали дробные значения через `int()` и могли маскировать явный `0` или конфликтующее значение через цепочку `or`. Из-за этого strict execution preflight мог принять неоднозначную геометрию как валидную.
2. **HIGH:** proxy outcome для grid читал только legacy `grid_levels` и игнорировал canonical `grid_count`. В результате число зачтённых осцилляций могло не ограничиваться фактическим числом сеток, завышая proxy return и загрязняя калибровочную выборку.
3. **MEDIUM:** operator UI отображал первый nullish alias без проверки точной целочисленности и согласованности. Дробное или конфликтующее значение могло выглядеть как исполнимое, хотя backend должен был его блокировать.

Все production-изменения движут контур только в fail-closed сторону. Risk caps, severity guards, directional TP/SL и Bybit side semantics не ослаблялись. Реальный OMS/EMS не добавлялся.

Финальный полный набор тестов: **741 passed / 0 failed / 0 skipped** против baseline **732 / 0 / 0**.

## 2. Обязательный pre-read и граница системы

До любых production-правок изучены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- последние отчёты `docs/AUDIT_REPORT_*` от 2026-06-15—2026-06-17.

Репозиторий подтверждён как рекомендательный сервис с persistence/operator UI, risk gates, Bybit public metadata и fail-closed preflight. Полноценного private Bybit OMS/EMS, lifecycle реальных ордеров и exchange reconciliation здесь нет. Поэтому partial fill, cancel/replace, real-order retries, insufficient balance, private rate limits и неизвестные биржевые позиции остаются требованиями к внешнему executor, а не объявляются багами отсутствующего слоя.

## 3. Baseline до правок

Команды выполнены до production-изменений:

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest -q                                    732 passed / 0 failed / 0 skipped
```

Полный baseline: `732 passed in 65.17s`, exit code `0`.

Артефакты:

- `docs/audit_artifacts/2026-06-18/baseline_compileall.txt`;
- `docs/audit_artifacts/2026-06-18/baseline_node_check.txt`;
- `docs/audit_artifacts/2026-06-18/baseline_pytest.txt`.

## 4. Зафиксированные математические конвенции

### 4.1 Directional TP/SL и Bybit one-way

Источник истины — `app/trading_semantics.py`:

- long: `TP > entry`, `SL < entry`, прибыль при росте;
- short: `TP < entry`, `SL > entry`, прибыль при снижении;
- neutral/grid: одиночный directional TP не подменяет grid-геометрию;
- open long=`Buy`, close/protect long=`Sell`;
- open short=`Sell`, close/protect short=`Buy`;
- protective close использует `reduceOnly=true`, `closeOnTrigger=true`, one-way `positionIdx=0`;
- rising trigger: long TP / short SL;
- falling trigger: long SL / short TP.

### 4.2 PnL, ROI и risk:reward

- `directional_trade_math` возвращает **gross** linear-USDT price PnL, без fees/funding;
- gross profit/loss в API — положительные абсолютные величины;
- `risk_reward = gross_profit / gross_loss` и эквивалентно отношению направленных ценовых дистанций;
- процентные distances относятся к entry/notional, а не к margin ROI;
- leverage не умножает price PnL, а влияет на margin и liquidation exposure;
- net grid economics отдельно учитывает execution costs и только adverse funding для approval.

### 4.3 Funding

- положительный funding: long платит, short получает;
- отрицательный funding: long получает, short платит;
- funding receipt не используется как гарантированный approval-edge;
- neutral grid использует adverse-side модель, поскольку inventory может накопиться в обе стороны.

### 4.4 Grid count

- `grid_count` — точное целое число ценовых интервалов;
- Bybit Futures Grid глобально допускает 2–400 grids, но фактический верхний предел может дополнительно снижаться в зависимости от диапазона и экономики сетки;
- `5.0`/`"5.0"` допустимы как точное целое представление;
- `5.9`, boolean, blank, NaN/Infinity и конфликтующие aliases недопустимы;
- при конфликте execution preflight блокирует запуск; exposure estimates используют большую валидную величину, outcome cap — меньшую, чтобы ни risk, ни calibration не становились оптимистичнее.

## 5. Карта single source of truth

### 5.1 Каноническая directional-модель

- `app/trading_semantics.py`: normalization, TP/SL mapping, geometry validation, gross PnL/R:R, Bybit open/close/protective semantics.
- `app/grid_math.py`: signed linear PnL, fee/funding economics, margin, approximate liquidation, tick/qty quantization, теперь также exact-integer semantics для exchange counts.

### 5.2 Backend/API/preflight

- `app/main.py::_directional_exit_payload_for_reco` — canonical exit payload для API/UI;
- `app/main.py::_trade_plan_price_context` — единый context entry/range/kill-switch/grid;
- `app/main.py::_validate_trade_plan_against_bybit_meta` — geometry, tick/qty/min-notional/leverage/instrument/grid/economics checks;
- `app/main.py::_execution_runtime_size_risk_blocks` — worst-price total notional/margin caps;
- `app/main.py::_snap_reco_payload_to_bybit_meta` — консервативное snapping и пересчёт sizing;
- `app/recommender.py::_build_trade_plan` и grid parameter generation — canonical generated payload.

### 5.3 Persistence/outcomes/calibration

- `app/db.py` — market/recommendation/bot/trade/outcome persistence;
- `app/outcomes.py::_grid_outcome` — proxy grid labels и oscillation cap;
- `app/calibration.py` — chronological/purged LogReg/Platt pipeline;
- outcome остаётся proxy, а не фактическим fill/funding/liquidation PnL.

### 5.4 Frontend/operator UI

- `app/ui/static/app.js::operatorExitLevelsFromBackend` — TP/SL только из backend payload с geometry/direction guard;
- `directionalExitMathForDisplay` — distances/R:R только для принятого backend payload;
- `resolveGridCountForDisplay` — exact integer + alias agreement;
- `buildOperatorValues` / `buildOperatorFieldSpecs` — operator rendering.

### 5.5 Alerts/logs/manual controls/modes

Alerts и logs отображают advisory state и не исполняют ордера. Manual materialization проходит backend risk/metadata/preflight. Полную paper/shadow/live OMS parity проверить невозможно, поскольку live OMS отсутствует; проверена parity существующей canonical backend semantics и frontend parsing/rendering.

## 6. Findings и исправления

### HIGH-01 — дробные/нулевые/конфликтующие grid-count aliases проходили как валидное число

**Затронутые места до исправления:**

- `app/main.py::_safe_int_or_none` использовал `int(value)`;
- `_trade_plan_price_context` выбирал aliases через `a or b or c`;
- auto-snap, sizing и runtime risk повторяли собственные цепочки aliases;
- `estimated_active_orders` сравнивался через `int(round(...))`.

**Воспроизведение до фикса:**

- `grid_count=5.9`, `grid_levels=5` → strict preflight возвращал `ok=true`;
- `grid_count=0`, `grid_levels=5` → явный invalid primary маскировался legacy alias и preflight возвращал `ok=true`;
- `params.grid_count=5`, `trade_plan.grid_count=6` → конфликт не блокировался;
- `estimated_active_orders=5.4` → округлялся до `5` и считался согласованным.

**Почему это ошибка:** число сеток определяет price interval, количество активных ордеров, total notional, margin и economic edge. Усечение или выбор другого alias без явного конфликта меняет торговую семантику payload.

**Финансовый риск:** возможны неверная геометрия, недостаточная/избыточная маржа, неправильный min-notional/position cap и operator setup с числом сеток, отличным от того, по которому рассчитана экономика.

**Исправление:**

- `app/grid_math.py:37-88` — добавлены `strict_integer()` и `resolve_integer_aliases()`;
- `app/main.py:309-310` — общий strict parser;
- `app/main.py:2605-2686` — единый resolver всех persisted aliases;
- `app/main.py:3059-3085` — `GRID_COUNT_NOT_INTEGER`, `GRID_COUNT_CONFLICT`, existing min/max gates;
- `app/main.py:3375-3387` — exact integer check для `estimated_active_orders`;
- `app/main.py:1841-1843,1927,2055,2454-2458` — strict count для geometry и conservative maximum для exposure;
- `app/recommender.py` — generated trade plan больше не использует truncating/fallback `or` semantics.

**Safety direction:** неоднозначный payload блокируется. Даже до возврата ошибки risk sizing берёт большую валидную alias-величину и не занижает exposure.

### HIGH-02 — canonical `grid_count` не ограничивал proxy outcome

**Файл:** `app/outcomes.py:336-364,400-417`.

**До исправления:** `_grid_outcome` читал только `params.grid_levels`. Современный payload с одним `grid_count=2` получал `grid_levels=0`, поэтому `completed_steps` не ограничивался числом сеток.

**Контролируемый пример до фикса:**

```text
canonical grid_count=2 -> proxy return 0.0960
legacy grid_levels=2   -> proxy return 0.0096
```

При одинаковых candles и costs canonical payload получил в 10 раз более высокий proxy return.

**Риск:** завышенные outcome returns и win labels могут загрязнять LogReg/Platt calibration, повышать perceived confidence и влиять на последующие рекомендации. Это не фактический exchange PnL, но это high-impact econometric boundary.

**Исправление:** outcome resolver читает canonical, legacy и nested aliases. При конфликте historical label использует `conservative_min`, а invalid-only payload деградирует к cap `0`. Canonical/legacy parity теперь равна `0.0096` в независимом regression fixture.

### MEDIUM-01 — UI отображал неоднозначный grid count как исполнимый

**Файл:** `app/ui/static/app.js:71-110,731,759,1063`.

**До исправления:** operator field использовал `params.grid_count ?? plan.grid_count ?? params.grid_levels`. Он не отличал `5.9`, `true` или конфликт `5` vs `6` от валидного count.

**Риск:** оператор мог скопировать в Bybit значение, которое не соответствует backend geometry/sizing. Backend preflight должен был блокировать исполнение, но UI не должен показывать malformed state как нормальное число.

**Исправление:** frontend exact parser и resolver повторяют backend acceptance boundary: integral JSON number/string принимается, fractional/boolean/conflict возвращает `null` и отображается как `—`. Asset cache key повышен до `manual-ui-v44`.

## 7. Red → green доказательство

Новый файл: `tests/test_iteration192_grid_count_integer_semantics.py`.

### Red до production-fix

```text
9 failed in 5.08s
```

Падали:

1. exact integer parser;
2. fractional/masked primary preflight — 3 cases;
3. conflicting aliases;
4. fractional active orders;
5. canonical-vs-legacy outcome parity;
6. frontend integer/alias resolver;
7. static asset cache key.

Лог: `docs/audit_artifacts/2026-06-18/red_grid_count_tests.txt`.

### Green после fix

```text
pytest -q tests/test_iteration192_grid_count_integer_semantics.py
9 passed in 5.36s
```

Независимое ожидаемое значение outcome `0.0096` выведено из controlled fixture; тест не сравнивает функцию с её собственным результатом.

Дополнительно обновлены две существующие JS harness-обвязки, чтобы они извлекали новый resolver вместе с `buildOperatorValues`. Это изменение тестовой инфраструктуры, а не ослабление production guard.

## 8. Полная post-verification

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest -q                                    741 passed / 0 failed / 0 skipped
```

Полный post-run: `741 passed in 73.16s`, exit code `0`.

| Стадия | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Baseline | 732 | 0 | 0 |
| Post | 741 | 0 | 0 |
| Δ | +9 | 0 | 0 |

Targeted regression around grid/preflight/outcomes/sizing: `71 passed`; final JS dependency regression: `12 passed`.

## 9. Static/code-quality review

Проведён scoped scan по `tp`, `sl`, `take_profit`, `stop_loss`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `closeOnTrigger`, `kill`, `leverage`, `pnl`, `roi`, `risk_reward`, `grid_count`, `grid_levels`, `int`, `round`, `Number`.

Результат:

- нового обхода canonical directional TP/SL/side/PnL/R:R не найдено;
- Bybit protective side/trigger semantics остаётся только в `app/trading_semantics.py`;
- frontend directional exits используют backend payload и fail-closed geometry checks;
- все executable/display grid-count paths, найденные в текущем scope, переведены на exact integer/alias resolver или generated exact integer;
- предыдущих `docs/STATIC_SCAN_*` в архиве нет, поэтому формальный diff с таким baseline невозможен;
- raw grep в отчёт не включён; triaged hits сохранены в audit artifacts.

Недоступно:

- `ruff`: указан в `requirements-dev.txt`, но модуль/исполняемый файл не установлен в текущей среде;
- `mypy`/`eslint`: не установлены;
- npm/yarn tests/lint: `package.json` отсутствует;
- private Bybit testnet/live API checks: credentials отсутствуют и OMS/EMS слоя нет.

## 10. Bybit V5 / Futures Grid cross-check

По официальной документации, актуальной на 2026-06-18, подтверждено:

- Futures Grid использует целое `Number of Grids`; общий диапазон — 2–400;
- фактический максимальный count может снижаться в зависимости от установленного price range и необходимости сохранить grid profit выше fees;
- arithmetic interval определяется как `(upper - lower) / number_of_grids`;
- Bybit V5 instrument metadata содержит `tickSize`, `qtyStep`, `minOrderQty`, `minNotionalValue`, leverage filters;
- conditional protective order semantics требуют согласованных `side`, `triggerDirection`, `reduceOnly` и `closeOnTrigger`.

Текущий код согласован с этими правилами в пределах рекомендательного/preflight слоя. Точный динамический maximum grids и minimum investment, показываемые интерфейсом Bybit для конкретного диапазона/аккаунта, должны повторно подтверждаться внешним executor непосредственно перед созданием бота.

## 11. Остаточные риски относительно `KNOWN_RISKS.md`

Не закрыты и не должны считаться закрытыми этим аудитом:

1. Нет private OMS/EMS и exchange reconciliation.
2. Proxy outcomes не равны фактическим fills, fees, funding, liquidation и account PnL.
3. Exact liquidation зависит от mark price, wallet margin и risk tier.
4. Bybit может динамически уменьшать допустимый grid count для конкретного range; public instrument metadata не даёт полной bot-UI validation truth.
5. В running Futures Grid число реально активных ордеров может стать меньше initial grid count из-за dynamic order/trailing mechanics; это должен отслеживать внешний reconciliation layer.
6. Aliases сохраняются ради backward compatibility. Resolver блокирует несогласованность, но историческая БД не мигрирована к одному физическому полю.
7. Public REST market data может быть stale/missing; текущие freshness/fail-closed guards уменьшают, но не устраняют инфраструктурный риск.
8. LLM reviewer остаётся advisory secondary layer.

## 12. Изменённые файлы

Production:

- `app/grid_math.py`;
- `app/main.py`;
- `app/outcomes.py`;
- `app/recommender.py`;
- `app/ui/static/app.js`;
- `app/ui/static/index.html`.

Tests:

- `tests/test_iteration192_grid_count_integer_semantics.py` — новый red→green suite;
- `tests/test_iteration183_ui_requires_backend_exit_payload.py` — JS harness dependency;
- `tests/test_iteration184_ui_backend_direction_mismatch.py` — JS harness dependency;
- существующие cache-key assertions обновлены `manual-ui-v43 → manual-ui-v44`.

Docs:

- `docs/AUDIT_REPORT_2026-06-18_grid_count_integer_semantics.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/audit_artifacts/2026-06-18/*`.

## 13. Вывод

Baseline был полностью зелёным, но exact-integer semantics числа сеток не была закреплена единым источником истины. Это позволяло truncation, alias masking и econometric divergence между canonical и legacy payload. Исправление вводит единый strict parser/resolver, блокирует конфликт в execution preflight, не занижает risk exposure, консервативно ограничивает historical outcome и синхронизирует UI.

Финальный offline quality gate зелёный: **741 passed**, `compileall` PASS, `node --check` PASS. Ни один ранее зелёный тест не потерян.
