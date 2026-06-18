# Консолидированный аудит: persistence, market-shock, funding и outcome integrity

**Дата:** 2026-06-18  
**Scope:** Bybit Futures / Linear USDT recommender, fail-closed preflight, proxy outcomes, operator UI  
**Отчёт:** `docs/AUDIT_REPORT_2026-06-18_persistence_shock_funding_outcome_integrity.md`

## 1. Граница и исходные конвенции

Репозиторий подтверждён как **рекомендательный/operator-grade слой с fail-closed preflight**, а не OMS/EMS. В нём нет полного live order lifecycle, exchange fills, private websocket reconciliation и истины о фактической позиции. Поэтому требования к partial fill, retry реального ордера, rate limit, insufficient balance и exchange reconciliation остаются требованиями к внешнему executor, а не фиктивно «исправленными» функциями этого репозитория.

Принятые конвенции:

- directional TP/SL, side, PnL и risk:reward — через `app/trading_semantics.py`;
- long: TP выше reference, SL ниже; short: TP ниже, SL выше;
- neutral grid: одиночный directional TP не создаётся;
- `DirectionalTradeMath` — gross price PnL до fees/funding; `risk_reward` — отношение ценовой/PnL-дистанции gross profit к gross loss;
- operator trade ledger: `pnl` — gross realized PnL, `fee` хранится отдельно, net = `pnl - fee`;
- recommendation cost model: execution cost неотрицателен; funding carry хранится signed, но approval использует только adverse carry;
- leverage/liquidation — консервативная рекомендационная оценка, не exchange liquidation truth.

## 2. Обязательный baseline

До изменения production-кода выполнено:

| Проверка | Исходный результат |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| `pytest -q` | **767 passed, 0 failed, 0 skipped** |

После исправлений:

| Проверка | Итоговый результат |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| `pytest -q` | **779 passed, 0 failed, 0 skipped** |

Итог: все 767 исходных зелёных тестов сохранены; добавлено 12 red→green проверок.

## 3. Прочитанные источники проекта

До правок изучены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- последние аудиты от 2026-06-17 и 2026-06-18: boolean/numeric fail-closed, purged OOF, funding/calibration, time-series risk, strict grid count, trade-plan integrity, recommendation freshness и history/horizon/direction.

Новые находки проверялись как регрессии относительно уже закрытых пунктов; прошлый short TP/SL defect повторно не объявлялся новой проблемой.

## 4. Карта single source of truth

| Контур | Реализация / отображение | Статус |
|---|---|---|
| Каноническая direction/TP/SL/PnL/R:R | `app/trading_semantics.py` | Источник истины |
| Backend API/operator payload | `app/main.py::_directional_exit_payload_for_reco`, strict preflight validation | Использует canonical module |
| Bybit side/protection semantics | `bybit_linear_order_semantics`, `bybit_linear_protective_order_plan` | Canonical, test-covered |
| Decimal linear PnL helper | `app/grid_math.py::linear_pnl_usdt` | Теперь нормализует side через canonical `normalize_execution_direction` |
| Recommendation direction/cost/R:R | `app/recommender.py` | Не создаёт независимый live-order lifecycle; directional funding sign явный |
| Proxy outcomes | `app/outcomes.py` | Направление нормализуется canonical helper; labels остаются proxy |
| Persistence/API state | `app/db.py`, recommendation/bot/trade rows | Audit identity `rec_id` теперь immutable |
| Frontend | `app/ui/static/app.js` | Backend exit payload авторитетен; JS geometry — defensive mirror и fail-closed renderer |
| Alerts/logs/reports | `app/alerts.py`, decision log, audit docs | Не обнаружена отдельная исполнимая TP/SL формула |
| Manual controls/preflight | `app/main.py` | Canonical geometry + live metadata guards |
| paper/shadow/live wording | recommendation state + preflight | Реального live executor нет; семантика не подменяется фиктивными order tests |

Статический directional scan дал 120 релевантных попаданий. Они были классифицированы, а не перенесены в отчёт сырым grep-дампом. Нового production-расхождения backend↔frontend TP/SL не обнаружено. UI не принимает локально вычисленный directional TP как разрешение на запуск: отсутствующий, mismatched или невалидный backend payload блокирует directional rendering.

## 5. Исправленные проблемы

### H-01 — market-shock/fast-veto мог использовать второй будущий бар

- **Severity:** HIGH
- **Файл до/после:** `app/shock_guard.py`, строки 18–43
- **Ошибка:** `_drop_open_candle()` проверял только первый элемент и удалял не более одной незакрытой свечи. При нескольких будущих/открытых строках второй бар оставался в массиве и попадал в market-shock/fast-veto расчёт. `int(True)` и усечение fractional timestamp дополнительно превращали malformed time в допустимый.
- **Риск:** look-ahead/data leakage в защитном market regime; ложный veto либо, опаснее, ложное отсутствие veto на доступных только в будущем данных.
- **Исправление:** каждый бар независимо проходит `strict_integer`; invalid, boolean, fractional, future и still-open rows отбрасываются. Невалидные `tf_sec`/`ts_now` возвращают пустой набор — fail-closed.
- **Red→green:** `test_market_shock_filters_every_open_future_and_malformed_candle`.

### H-02 — `INSERT OR REPLACE` позволял переписывать recommendation audit row

- **Severity:** HIGH
- **Файл:** `app/db.py`, строки 801–941
- **Ошибка:** повторная запись того же `rec_id` могла без ошибки заменить direction, score, confidence, status, params и lineage metadata.
- **Риск:** исторический сигнал и его экономическое обоснование могли быть ретроспективно изменены; расследование, publication-chain, calibration lineage и operator history переставали быть доказуемыми.
- **Исправление:** `rec_id` теперь immutable audit identity. Exact canonical payload retry является idempotent no-op. Конфликтующий payload вызывает `ValueError`; batch защищён savepoint и не оставляет частично записанные строки. JSON сравнивается канонически, а не по случайному порядку ключей.
- **Red→green:** `test_recommendation_insert_is_idempotent_but_cannot_overwrite_audit_row`.

### H-03 — Python/JSON booleans превращались в реальные numeric recommendation fields

- **Severity:** HIGH
- **Файл:** `app/db.py`, строки 809–899
- **Ошибка:** SQLite мог сохранить `True` как `1/1.0`, а `bool("false")` превращал строку в root flag `1`. Это затрагивало `ts`, score/confidence/R:R/risk, TTL, feature timestamp и outcome-root semantics.
- **Риск:** malformed payload выглядел валидной рекомендацией, менял freshness, ranking, expiry, calibration lineage и UI.
- **Исправление:** boolean values блокируются до numeric coercion; root flag принимает только реальный bool либо exact `0/1`; finite numeric values нормализуются. Намеренно сохранены старые SQLite resilience fixtures, которые записывают явно испорченный TEXT (`"broken-ts"`, `"bad-score"`) и проверяют downstream fail-closed sanitization — это не producer contract и не ослабляет boolean guard.
- **Red→green:** 8 параметризованных кейсов `test_recommendation_persistence_rejects_boolean_or_ambiguous_numeric_fields`.

### H-04 — отрицательная execution cost превращала friction в alpha

- **Severity:** HIGH
- **Файл:** `app/outcomes.py`, строки 200–252
- **Ошибка:** explicit `execution_cost_bps=-12` или отрицательный `total/net_cost_bps` принимался как валидный finite float. В `_grid_outcome` отрицательный cost floor мог превратить тонкое движение в успешный label и положительный proxy return.
- **Риск:** оптимистичная разметка, загрязнение calibration и завышенная probability-like confidence.
- **Исправление:** execution/net friction не может быть отрицательной. Отрицательный или poisoned explicit value заменяется консервативным fallback (15 bps по умолчанию), а signed funding остаётся отдельной величиной.
- **Red→green:** `test_negative_execution_cost_cannot_create_optimistic_outcome_label`.

### H-05 — boolean `next_funding_ts` имитировал реальное расписание

- **Severity:** HIGH
- **Файл:** `app/recommender.py`, строки 1376–1443
- **Ошибка:** `int(True)==1`; timestamp `1` затем прокручивался вперёд по funding interval и мог дать один ожидаемый event вместо двух при неизвестном расписании на 12-часовом горизонте.
- **Риск:** недооценка adverse carry и завышение net edge/R:R для Linear USDT grid.
- **Исправление:** `ts_now` и `next_funding_ts` проходят strict safe integer parsing; boolean/non-positive schedule считается неизвестным. Unknown next event использует консервативный event count; payload явно маркируется `conservative_unknown_next_funding_ts`.
- **Red→green:** `test_boolean_funding_schedule_is_treated_as_unknown_and_charged_conservatively`.

### M-01 — Decimal PnL helper имел локальную side normalization

- **Severity:** MEDIUM (архитектурный drift risk; текущая формула была математически верной)
- **Файл:** `app/grid_math.py`, импорт canonical helper и `linear_pnl_usdt`
- **Ошибка:** helper самостоятельно lower-case нормализовал `long/short`, то есть имел отдельную точку directional interpretation вне canonical module.
- **Риск:** будущий набор поддерживаемых направлений или malformed-side policy мог разойтись с `app/trading_semantics.py`.
- **Исправление:** side теперь нормализуется через `normalize_execution_direction`; Decimal-арифметика остаётся в economics module. Поведение подтверждается существующими PnL long/short/fail-closed tests.

## 6. Red→green доказательство

Новый файл: `tests/test_iteration198_deep_audit_persistence_shock_funding.py`.

До исправлений:

- **12 failed**;
- failure set включал future candle leakage, recommendation overwrite, 8 boolean-coercion кейсов, negative execution cost и boolean funding schedule.

После исправлений:

- **12 passed**;
- полный suite: **779 passed**.

Независимые expected values в тестах не вычисляются проверяемыми production-функциями: timestamp filtering, immutable stored row, cost fallback, label sign и funding event count заданы явно.

## 7. Bybit V5 cross-check на 2026-06-18

Проверены официальные страницы Bybit V5 API Documentation:

- **Place Order**: `positionIdx=0` соответствует one-way; reduce/close order требует `reduceOnly=true`; `closeOnTrigger` не должен увеличивать позицию; acknowledgement асинхронен и требует websocket confirmation.
- **Get Instruments Info**: Linear metadata содержит `tickSize`, `qtyStep`, `minOrderQty`, `minNotionalValue`, max quantities, leverage limits и `fundingInterval`.
- **Set Trading Stop**: position TP/SL создаёт внутренние conditional orders и требует корректного `positionIdx`; full/partial semantics различаются.

Текущая canonical model не противоречит этим требованиям. Поскольку OMS/EMS отсутствует, websocket confirmation, actual fill quantity, partial fills, live position reconciliation и повторная проверка динамических instrument/risk limits остаются внешним execution-layer requirement. Фиктивный live executor и «зелёные» тесты к несуществующему коду не добавлялись.

## 8. Проверки, которые не выполнялись

- private Bybit testnet/live API: отсутствуют и не должны использоваться пользовательские ключи в offline-аудите;
- реальные order submit/fill/cancel/reconciliation: соответствующего OMS/EMS нет;
- PostgreSQL integration server: SQL path сохранён переносимым (`ON CONFLICT`, savepoints), но живой PostgreSQL instance не был предоставлен;
- npm/yarn/lint/typecheck: в репозитории нет `package.json`, `pyproject.toml`, lint/mypy config;
- реальная slippage/funding/fill validation: proxy-модель не может это доказать.

## 9. Остаточные риски

1. **External OMS/EMS — HIGH/system boundary.** Перед реальным ордером обязательны live wallet/margin, price band, instrument filters, idempotent orderLinkId, websocket confirmation, partial-fill handling и reconciliation.
2. **Proxy outcomes — MEDIUM.** После исправления negative cost labels остаются приближением, а не fill-level PnL.
3. **Legacy malformed TEXT — LOW/MEDIUM compatibility.** SQLite test fixtures могут хранить явный poisoned TEXT для проверки downstream sanitization. Production producer обязан передавать typed finite payload; PostgreSQL naturally жёстче.
4. **Frontend mirror — LOW.** JavaScript содержит defensive geometry mirror, но execution permission формируется backend. Его parity уже покрыта long/short/neutral/invalid/mismatch tests; при любом изменении canonical payload эти tests обязательны.
5. **Resource cleanup — LOW.** Coverage-прогон выявлял отдельные `ResourceWarning` об unclosed SQLite connections в test paths. Они не проявились как функциональные failures, но дальнейшая уборка fixtures желательна.
6. **Dynamic Bybit limits — HIGH для будущего executor.** Instrument/risk metadata должна перечитываться непосредственно перед submit; cached/public REST не является execution truth.

## 10. Итог

Critical defects не обнаружены. Исправлено пять HIGH-дефектов и один MEDIUM architectural drift без перехода fail-closed → fail-open. Исходный зелёный baseline сохранён, новый suite полностью зелёный, а граница между recommender/preflight и отсутствующим OMS/EMS не размыта.
