# Консолидированный regression audit — remaining numeric fail-closed boundaries

**Дата:** 2026-06-17  
**Scope:** Bybit V5 Linear USDT recommender, quant pipeline, risk/preflight, directional API payload, operator UI  
**Исходный архив:** `bybit-reco-systems-main.zip`  
**SHA-256 исходного архива:** `72b433ef86644b5454f005507a53a83a66eefc3c6360d07cb49b505a4457d5ea`

## 1. Результат

Новых дефектов уровня `CRITICAL` не найдено. Исправлены:

1. **HIGH:** остаточные `bool -> 1/0` преобразования в market-data, OHLCV/direction, sentiment, recommender, LLM-review, calibration/outcomes, persistence, risk/shock, funding timestamps/events и UI formatters.
2. **HIGH:** boolean `score/confidence/expected_rr/coherence/regime_confidence` мог ошибочно признать рекомендацию high-quality и сократить publication confirmation с двух циклов до одного.
3. **MEDIUM:** API directional trade math при отсутствии реального qty использовал unit quantity для R:R, но не маркировал, что gross PnL не является оценкой PnL позиции.

Все production-изменения ужесточают проверку или добавляют provenance. Fail-closed guards, risk thresholds и severity не ослаблялись. Реальный OMS/EMS не добавлялся.

## 2. Обязательный pre-read

До любых production-правок изучены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- пять последних по времени `docs/AUDIT_REPORT_*`.

Последние отчёты показали, что canonical TP/SL/Bybit mapping и часть boolean-boundaries уже были исправлены. Этот аудит поэтому выполнялся как поиск **остаточных обходов** и регрессий, а не как повторный аудит с нуля.

## 3. Граница системы

Репозиторий остаётся рекомендателем и fail-closed operator/preflight-контуром. В нём есть recommendation state, materialized `bot_instance`, operator trade records, validation и preflight, но нет полноценного private Bybit OMS/EMS с реальным order submission, fill lifecycle, cancel/replace, partial fills и exchange reconciliation.

Поэтому:

- найденные проблемы в существующем recommendation/preflight/UI коде исправлены как реальные дефекты;
- partial fill, retry/idempotency реальных ордеров, private rate limits, unknown exchange positions и wallet truth остаются требованиями к внешнему executor;
- фиктивный execution-код и «зелёные» тесты для отсутствующего слоя не создавались.

## 4. Baseline до правок

Команды выполнены до production-изменений:

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest -q                                    718 passed / 0 failed / 0 skipped
```

Первый объединённый запуск `pytest` был прерван лимитом оболочки на 60%; отдельный повторный полный запуск до правок завершился `718 passed in 28.90s`, exit code 0. Логи: `audit_artifacts/baseline.txt`, `audit_artifacts/baseline_pytest.txt`.

## 5. Зафиксированные конвенции

### 5.1 Directional TP/SL

Источник истины: `app/trading_semantics.py`.

- long: `TP > entry`, `SL < entry`, прибыль при росте;
- short: `TP < entry`, `SL > entry`, прибыль при падении;
- neutral/grid: одиночные directional TP/SL не публикуются; используются range/kill-switch bounds;
- неверная или неполная geometry должна вернуть invalid/blocked, а не автоматически переставить уровни.

### 5.2 PnL и R:R

- `directional_trade_math`: gross linear-USDT PnL без fees/funding; `gross_profit_usdt` и `gross_loss_usdt` — положительные величины; `risk_reward = gross_profit/gross_loss`.
- `reward_pct/risk_pct`: относительно entry notional, не ROI на маржу.
- leverage не умножает базовый price PnL; leverage влияет на margin/liquidation exposure.
- `grid_math.grid_leg_economics`: отдельно хранит gross edge, execution cost, adverse funding cost и conservative net edge.
- потенциальный funding receipt не используется для approval edge.

### 5.3 Funding

`funding_cashflow_usdt` возвращает положительное значение как cost, отрицательное как receipt. Для положительного funding rate long платит, short получает; при отрицательном — наоборот. Unknown side и malformed event count fail closed.

### 5.4 Margin/liquidation

- margin estimate: notional/leverage;
- liquidation price/buffer — только conservative approximation для UI/preflight;
- точная liquidation truth зависит от private account state, mark price и risk tier и остаётся обязанностью внешнего executor.

### 5.5 Qty provenance

Если position/order qty отсутствует, qty=1 разрешён только как математическая база для dimensionless R:R и price-distance. Такой payload теперь явно маркируется как **не position estimate**.

## 6. Карта single source of truth

### Каноническая directional-семантика

- `app/trading_semantics.py`: direction normalization, TP/SL mapping, geometry validation, gross PnL/R:R, Bybit open/close/protective side, `reduceOnly`, `closeOnTrigger`, `triggerDirection`.
- `app/grid_math.py`: linear PnL, fee/funding sign, net grid economics, margin и approximate liquidation.

### Backend/API/preflight

- `app/main.py::_trade_plan_price_context`: entry/range/kill-switch/grid-level extraction;
- `app/main.py::_directional_exit_payload_for_reco`: canonical exit payload для API/UI;
- `app/main.py::_validate_trade_plan_against_bybit_meta`: direction geometry, tick/qty/notional/leverage/instrument checks;
- `app/main.py`: live-price, funding, same-symbol one-way и risk guards;
- `app/recommender.py`: feature aggregation, direction stabilization, score/confidence, grid geometry, economics, leverage/liquidation/capital estimates, publication confirmation;
- `app/risk.py`, `app/shock_guard.py`: risk limits, drawdown/cooldown и shock gates;
- `app/collector.py`, `app/bybit_client.py`: market data и Bybit instrument metadata boundaries.

### Persistence/quant

- `app/db.py`: ticker/OHLCV/recommendation/bot/trade/outcome/risk state validation;
- `app/outcomes.py`: proxy outcomes и cost/funding adjustments;
- `app/calibration.py`: feature extraction, chronological/purged calibration pipeline;
- `app/direction.py`, `app/features.py`, `app/sentiment.py`, `app/sentiment_features.py`: time-series, direction and sentiment inputs.

### Frontend/manual controls

- `app/ui/static/app.js`: backend exit payload parsing/rendering, operator sheet, price/percent/qty formatting;
- UI не является отдельным источником TP/SL geometry: invalid/mismatched backend payload должен скрывать R:R и distances;
- manual controls проходят backend preflight; UI formatting не должно превращать missing/boolean в числовое значение.

### Alerts/logs/docs/modes

- alerts/logs/audit reports отображают состояние и не маршрутизируют ордера;
- paper/shadow/live execution parity с реальным OMS проверить нельзя, потому что такого OMS в репозитории нет;
- проверена parity canonical backend payload ↔ frontend parsing/rendering и единая one-way semantics внутри существующей границы.

## 7. Findings и исправления

### HIGH-01 — остаточные JSON booleans превращались в торговые числа

**Файлы и актуальные диапазоны:**

- `app/collector.py:87-100,270-305`;
- `app/direction.py:10-17,107-143,220-279`;
- `app/features.py:32-41`;
- `app/sentiment.py:133-154`;
- `app/sentiment_features.py:30-39`;
- `app/llm_review.py:33-42,89-105,296`;
- `app/recommender.py:57-112,1688-1718,1729-1780`;
- `app/grid_math.py:72-91`;
- `app/outcomes.py:141-198`;
- `app/calibration.py:180-191`;
- `app/db.py:610-645,853-885,932-958`;
- `app/main.py:294-300`;
- `app/risk.py:46-99`;
- `app/shock_guard.py:43-51`;
- `app/ui/static/app.js:50-72,936-944`.

**Причина:** Python `bool` — subclass `int`, а JavaScript `Number(true/false)` возвращает `1/0`. Общие `float()`/`int()`/`Number()` без отдельной boolean-проверки могли сделать malformed payload внешне валидным.

**Воспроизведённые последствия до фикса:**

- `lastPrice=true` и OHLCV `volume=true` принимались как реальные market-data числа;
- boolean OHLC/TF score мог повлиять на direction/trend aggregation;
- LLM confidence `true` становился `1.0`, а candidate score попадал в reviewer payload как `1.0`;
- `nextFundingTime=true` становился timestamp `1`, funding event count `true` — одним событием;
- sentiment `true` становился экстремальным `+1`;
- legacy/manual grid/outcome/calibration числовые поля получали `1/0`;
- `cooldown_after_loss_min=false` становился `0` вместо безопасного default `30`;
- UI показывал boolean price/percent/qty как `1`, `+0.00%`, `1 BTC`.

**Финансовый/торговый риск:** ложная цена/объём/score/confidence могли исказить direction, grid geometry, funding horizon, risk limits, operator display и decision ranking. Хотя репозиторий не отправляет live orders, recommendation/preflight/UI boundary safety-critical.

**Исправление:** boolean отклоняется до numeric coercion. Для обязательных numeric payloads значение становится invalid; для advisory/legacy полей применяется нейтральный или documented conservative default. Никакой boolean больше не может отключить cooldown/cap или превратиться в market price/score.

### HIGH-02 — boolean quality metrics обходили two-cycle publication confirmation

**Файл:** `app/recommender.py:2692-2714`; связанная state hardening `2720-2800`, dedupe comparison `2987-3008`.

**До фикса:**

```text
score=true
confidence=true
expected_rr=true
coherence=true
regime_confidence=true
→ (1, "high_quality_signal")
```

То есть malformed recommendation могла получить публикацию после одного цикла вместо `two_cycle_confirmation`.

**Риск:** premature publication и снижение temporal confirmation без реального качества сигнала.

**Исправление:** все quality fields и thresholds проходят finite/boolean-safe parser. Boolean quality fields дают нейтральный default и не сокращают confirmation. Direction persistence state и publication-dedupe comparisons приведены к той же semantics.

**После фикса:** тот же payload возвращает `(2, "two_cycle_confirmation")`.

### MEDIUM-01 — gross PnL в directional API не имел явной qty provenance

**Файл:** `app/main.py:922-965`.

**До фикса:** при отсутствии position/order qty backend передавал `qty=1.0` в canonical math, чтобы получить R:R/distances. Числа были математически согласованы, но API consumer мог принять `gross_profit_usdt/gross_loss_usdt` за оценку всей позиции.

**Риск:** неверная интерпретация displayed gross PnL, особенно для дорогих base assets или фактического qty, отличного от 1.

**Исправление без нарушения совместимости:**

- actual qty отсутствует:
  - `qty=null`;
  - `qty_source="unit_qty_ratio_only"`;
  - `trade_math.qty_basis="one_base_asset_for_ratio_only"`;
  - `trade_math.gross_pnl_is_position_estimate=false`;
- actual qty присутствует:
  - `qty_basis="position_qty"`;
  - `gross_pnl_is_position_estimate=true`.

R:R и percentage distances не изменены.

## 8. Red → green доказательство

Новые regression cases добавлялись до соответствующих production fixes.

### Red evidence

- `audit_artifacts/iteration190_red.txt`: `5 failed` — market data, recommender, DB/outcomes, sentiment, qty provenance;
- `audit_artifacts/iteration190_risk_red.txt`: `1 failed` — `cooldown_after_loss_min=false` давал `0` вместо `30`;
- `audit_artifacts/iteration190_second_wave_red.txt`: `2 failed` — LLM/direction/funding/timestamp и UI formatters;
- `audit_artifacts/iteration190_publication_gate_red.txt`: `1 failed` — boolean quality payload обходил two-cycle gate.

### Green evidence

`tests/test_iteration190_remaining_numeric_failclosed.py` содержит 9 тестов:

1. market-data и feature boundaries;
2. recommender boolean price/grid fail-closed;
3. DB/persistence/proxy-label boundaries;
4. sentiment neutralization;
5. directional API qty provenance;
6. risk limits/cooldown/shock guard;
7. LLM, direction, funding event и timestamp boundaries;
8. frontend price/percent/qty formatters;
9. two-cycle publication gate.

Targeted result:

```text
9 passed in 1.80s
```

Лог: `audit_artifacts/iteration190_green_final_targeted.txt`.

Независимые expected values в тестах заданы вручную; тесты не сравнивают функцию с её собственным выводом.

## 9. Post-verification

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest -q                                    727 passed / 0 failed / 0 skipped
```

Сравнение:

| Стадия | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Baseline | 718 | 0 | 0 |
| Post | 727 | 0 | 0 |
| Δ | +9 | 0 | 0 |

Полный post-run вывел `727 passed in 30.21s`. Лог: `audit_artifacts/post_pytest_final_clean.txt`. Один запуск оболочки не вернул управление после уже напечатанного pytest summary из-за ограничения/особенности container wrapper; активного pytest-процесса после этого не осталось. Targeted suite, Python compile и Node syntax check завершились обычным exit success.

## 10. Static/code-quality review

Проведён scoped grep/triage по `tp`, `sl`, `take_profit`, `stop_loss`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `closeOnTrigger`, `kill`, `leverage`, `pnl`, `roi`, `risk_reward`, а также по `float/int/Number/parseFloat/parseInt`.

Результат:

- нового обхода canonical TP/SL/side mapping не найдено;
- direct directional math остаётся в `app/trading_semantics.py` и `app/grid_math.py`;
- UI directional exits используют backend payload и не вычисляют независимую short/long geometry;
- оставшиеся direct numeric conversions классифицированы как internal/generated diagnostics/count formatting либо находятся за уже валидированными market-data boundaries;
- предыдущих `docs/STATIC_SCAN_*` в репозитории нет, поэтому формальный diff с таким файлом невозможен;
- сырой grep в отчёт не включён.

Недоступно в среде:

- `ruff`, `mypy`, `eslint` — executables не установлены;
- npm/yarn tests/lint — `package.json` отсутствует;
- private Bybit API, wallet, positions и testnet order lifecycle — нет credentials и отсутствует OMS/EMS layer.

## 11. Bybit V5 cross-check

Сверены официальные разделы:

- Place Order: `category=linear`, `side=Buy|Sell`, `positionIdx`, `triggerDirection`, `reduceOnly`, `closeOnTrigger`;
- Instruments Info: `tickSize`, `qtyStep`, `minOrderQty`, `minNotionalValue`, leverage/funding metadata;
- Position Mode: one-way/hedge semantics;
- Trading Stop: TP/SL conditional order semantics.

Canonical model остаётся согласованной:

- open long=`Buy`, close/protect long=`Sell`;
- open short=`Sell`, close/protect short=`Buy`;
- close/protection `reduceOnly=true`, `closeOnTrigger=true`, one-way `positionIdx=0`;
- upward triggers: long TP / short SL;
- downward triggers: long SL / short TP.

Официальные URL сохранены в audit notes:

- `https://bybit-exchange.github.io/docs/v5/order/create-order`
- `https://bybit-exchange.github.io/docs/v5/market/instrument`
- `https://bybit-exchange.github.io/docs/v5/position/position-mode`
- `https://bybit-exchange.github.io/docs/v5/position/trading-stop`

## 12. Остаточные риски относительно `KNOWN_RISKS.md`

Не закрыты и не должны считаться закрытыми этим аудитом:

1. Нет real OMS/EMS, private order lifecycle и exchange reconciliation.
2. Proxy outcomes не равны реальным fills, fees, funding и liquidation/account truth.
3. Exact liquidation зависит от private account/risk-tier state.
4. Public REST data может быть stale/missing; fail-closed снижает, но не устраняет инфраструктурный риск.
5. Legacy rows без некоторых новых provenance/availability полей могут иметь меньшую calibration coverage.
6. LLM reviewer остаётся advisory/gated secondary layer, а не источник execution truth.
7. Реальный executor обязан повторно проверить market/mark price, balance, position mode, instrument filters, qty/notional, leverage, reduce-only geometry и idempotency непосредственно перед order submission.

`docs/TRADING_LOGIC.md` дополнен обязательным правилом numeric boolean rejection и описанием unit-qty PnL provenance.

## 13. Изменённые файлы

Production:

- `app/calibration.py`
- `app/collector.py`
- `app/db.py`
- `app/direction.py`
- `app/features.py`
- `app/grid_math.py`
- `app/llm_review.py`
- `app/main.py`
- `app/outcomes.py`
- `app/recommender.py`
- `app/risk.py`
- `app/sentiment.py`
- `app/sentiment_features.py`
- `app/shock_guard.py`
- `app/ui/static/app.js`

Tests/docs:

- `tests/test_iteration190_remaining_numeric_failclosed.py`
- `docs/TRADING_LOGIC.md`
- `docs/AUDIT_REPORT_2026-06-17_remaining_numeric_failclosed.md`
- `audit_artifacts/*` — baseline/red/green/post evidence.

## 14. Вывод

Репозиторий после исправлений сохраняет fail-closed architecture и canonical long/short semantics. Boolean JSON больше не может тихо стать торговым числом на проверенных safety-critical boundaries, отключить cooldown, создать ложный funding/timestamp/price/score или сократить publication confirmation. Directional API теперь явно различает real position PnL estimate и unit-qty arithmetic basis. Полный regression suite увеличен с 718 до 727 зелёных тестов.
