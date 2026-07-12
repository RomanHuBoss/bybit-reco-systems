# Known risks

## 2026-07-12 temporal data-lineage audit (v1.0.23)

### RESOLVED/HIGH: local receipt time could hide stale Bybit ticker data
Bybit V5 top-level response time is now propagated to ticker rows before collector freshness checks. Missing event time remains unknown; it is not manufactured from local receipt time when the exchange supplied authoritative envelope time.

### RESOLVED/HIGH: shifted candles and sparse horizons could manufacture chronology
OHLCV starts must be exact integer milliseconds aligned to both one second and the requested timeframe. Outcome entry is the exact next minute, the complete horizon must be contiguous, and exit is the exact boundary candle. Missing minutes now produce no label rather than a later substitute price.

### RESOLVED/HIGH: unavailable labels could enter calibration
Calibration now requires a valid `label_available_ts <= fit_ts` and `label_available_ts >= recommendation.ts`. Rows with missing, malformed or future maturity are excluded. Dirty persisted mandatory numeric fields are skipped; malformed optional label availability decodes to unknown and therefore remains ineligible for fit.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v4`
The startup label-version guard clears incompatible `reco_outcomes` and calibrator keys once. Recommendations, bot instances, trades, risk limits and exact execution evidence are preserved. New calibration remains unfitted until enough v4 labels mature.

### Remaining limitation
A complete OHLCV horizon is still a proxy: it does not prove queue priority, individual fills, partial inventory, live fees or liquidation behavior. Profitability remains unverified.


### RESOLVED/HIGH: built-in risk defaults diverged from shipped small-account profile

До v1.0.19 запуск без пользовательского `RISK_LIMITS_JSON` получал 4 concurrent bots, daily DD 200 USDT, notional 5000 USDT и margin 1000 USDT, хотя README/.env/operator instruction описывали 1 bot, DD 10, notional 500 и margin 100. Встроенные defaults и fallback normalization синхронизированы с shipped-профилем; явный операторский override по-прежнему поддерживается.

### RESOLVED/HIGH: generated qty could be silently increased

До v1.0.19 provisional sizing округлялся вверх по фиктивному `0.001`, а live auto-snap повышал qty до minQty/minNotional. Для дорогого BTCUSDT target 25 USDT превращался в 100 USDT на одну grid-заявку до учета количества интервалов. Теперь provisional qty сохраняет target notional, live alignment выполняется только вниз, а недостаточный minQty/minNotional блокируется fail-closed.
# Известные риски и ограничения

## 2026-07-11 no-recommendation state audit (v1.0.22)

### RESOLVED/HIGH: weak edge was misreported as a hard technical block

`MEAN_REVERSION_EDGE_UNCONFIRMED` означает, что валидный торговый evidence не прошёл порог стратегии. Это `no_trade`, а не ошибка Bybit, malformed payload или risk/preflight failure. Отсутствующее обязательное evidence по-прежнему остаётся hard `blocked` через `MEAN_REVERSION_EVIDENCE_INSUFFICIENT`.

### RESOLVED/HIGH: no-trade regime stopped outcome accumulation

Outcome worker исключал все `no_trade`, поэтому при полном отсутствии actionable рекомендаций новая calibration sample переставала расти. Добавлен explicit opt-in `outcome_policy.sample_role=shadow_no_trade` только для полных кандидатов без hard blocks. Worker повторно валидирует literal boolean, risk pass и пустой block list.

### RESOLVED/MEDIUM: proxy outcome UI implied real execution

Журнал использовал формулировки «что реально торговалось», хотя сервис не является OMS и outcome строится по OHLCV. UI теперь явно говорит о proxy-кандидатах и показывает shadow/non-shadow roots отдельно.

### RESIDUAL/HIGH: отсутствие рекомендаций может быть экономически правильным

Система не обязана постоянно выдавать сделки. Низкий win-rate, отрицательный avg return или отсутствие подтверждённого range edge должны приводить к `no_trade`. Shadow labels позволяют проверять, не стал ли gate чрезмерно строгим, но не дают права ослаблять его без walk-forward/exact-evidence анализа.

## 2026-07-11 independent range-edge audit

### RESOLVED/HIGH: отсутствие тренда ошибочно считалось положительным grid edge

Legacy `range_score` почти полностью равнялся `1 - trend_strength`, поэтому low-drift random walk мог получить высокий range score, положительный вклад в launch-score и эвристический `expected_rr`. Это не доказывало возвратность и создавало систематический false-positive path после комиссий. Исправлено независимым anti-persistence diagnostic, multi-TF coverage gate и hard blockers `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` / `MEAN_REVERSION_EDGE_UNCONFIRMED`.

### RESOLVED/HIGH: старая калибровка могла пережить изменение feature semantics

Сохранённые v3 coefficients и legacy outcome snapshots были обучены на прежнем диапазонном proxy. Простая замена inference feature создала бы train/inference skew. Текущая модель получила identity `bybit-taxonomy-v3-mean-reversion`, calibrator keys v4, а fit фильтруется по model version и явному независимому evidence snapshot.

### RESOLVED/MEDIUM: `expected_rr` выглядел как фактический reward:risk

Поле является эвристическим capture/volatility proxy и не моделирует полную monetary loss distribution, inventory path и liquidation tail. API field сохранён, но UI и reasons теперь явно маркируют его как proxy, не доказательство прибыли.

### HIGH/RESIDUAL: положительное mean-reversion evidence не доказывает net alpha

Negative autocorrelation может отражать bid/ask bounce, stale marks или краткоживущий microstructure effect, который нельзя исполнить после spread, fees, slippage, funding и latency. Порог 0.55 является conservative false-positive filter, а не оценкой expected net PnL. До достаточной независимой walk-forward/shadow выборки по exact fills проект остаётся генератором гипотез.

### MEDIUM/RESIDUAL: новые calibrators временно будут unfitted

Legacy outcomes намеренно исключены из v4 training. Confidence будет использовать raw/fallback semantics до накопления достаточного числа matured outcomes модели v3. Это безопаснее, чем переносить коэффициенты с несовместимой feature semantics, но уменьшает статистическую информативность в переходный период.

## 1. Нет реального OMS/EMS
Это главный системный риск. Проект не управляет live order lifecycle и не знает реальные open orders/fills.
Следствие: нельзя считать его завершённой автоторговой системой без внешнего execution layer.

## 2. Qty/min-notional validation зависит от фактического размера позиции
Сервис формирует provisional `params.sizing` от целевого order notional без выдуманного `qtyStep`. После получения live Bybit metadata quantity может только округляться вниз по фактическому `qty_step`; если safe qty ниже `min_order_qty`/`min_notional`, preflight блокирует recommendation и не повышает размер автоматически. Это всё равно не заменяет sizing от баланса аккаунта и live preview фактических ордеров в Bybit: внешний исполнитель обязан повторно сверять `qty_step`, `min_order_qty`, `max_order_qty`, `min_notional`, available balance и фактическую маржу перед созданием Bybit grid bot.

## 3. Outcome labeling остаётся proxy-моделью
Даже усиленная grid-разметка не заменяет реальные fill/funding/liquidation данные.
Использовать её как единственный источник истины для PnL/WR нельзя.


## 3A. Live edge технологии не доказан

Launch-score классифицирует пригодность текущего режима для futures grid, но не является прямой оценкой математического ожидания фактической сделки. В raw-режиме `confidence` детерминированно зависит от эвристического score и контекстных penalties; это не независимая вероятность прибыли. Даже fitted calibration обучается на proxy outcomes без queue priority, partial fills, live fee tier, latency и полной траектории inventory.

Следствие: наличие `recommended`, высокий score/confidence или зелёный proxy-backtest не подтверждает прибыльность технологии. До статистически устойчивого положительного net expectancy в chronological walk-forward/shadow данных по фактическим fills система должна использоваться как recommendation/audit и hypothesis-generation layer. Продолжающаяся отрицательная expectancy после устранения execution/data defects является основанием остановить live использование и пересмотреть саму модель признаков/target, а не поднимать пороги постфактум.

## 3C. RESOLVED/HIGH: отрицательная exact-evidence expectancy не останавливала новые запуски

До v1.0.17 сервис уже сохранял immutable execution evidence и строил descriptive live-validation export, но operator execution preflight не использовал накопленный realised net PnL. Поэтому система могла продолжать materialize новые `bot_instance`, даже когда несколько независимых остановленных ботов по тому же символу и направлению подряд дали отрицательный результат. Зелёный unit-test suite этого не выявлял, потому что проверял корректность отдельных формул и persistence, а не замкнутый feedback loop «фактический результат → разрешение следующего запуска».

Исправление: preflight применяет fail-closed stop gate только к stopped bots с exact execution evidence, дедуплицирует наблюдения по immutable publication root, ограничивает cohort текущим explicit `model_version` и отдельно считает direction/symbol/portfolio cohorts. Directional gate срабатывает после пяти последовательных убытков либо после восьми независимых наблюдений с отрицательными total и median net PnL и positive rate ниже 50%; symbol и portfolio gates требуют соответственно 12 и 20 наблюдений. Это заранее определённый operational stop criterion, а не статистический тест alpha.

Остаточный риск: отсутствие exact evidence означает отсутствие данных для этого gate. Внешний adapter обязан передавать все fills, fee и funding; до накопления достаточной независимой выборки технология остаётся непроверенной.

## 3B. RESOLVED/HIGH: одноцикловый signal spike мог стать actionable

Сильный эвристический score ранее обходил persistence gate с `required_hits=1`, а повторные циклы могли считаться подтверждением без проверки нового `features_ref_ts`. Теперь любой `futures_grid` требует двух разных последовательно закрытых evidence snapshots; повтор одной свечи, stale/out-of-order evidence и legacy state не продвигают gate.

## 3C. RESOLVED/HIGH: UI подменял immutable recommendation при обновлении

Кнопка обновления карточки ранее искала `latest_rec_id` только по `(venue, symbol, bot_type)` и могла заменить выбранный `recommended` новым `no_trade`/другим направлением. Теперь перечитывается exact selected `rec_id`; новые публикации остаются отдельными событиями истории.

## 4. SQLite — практичный, но ограниченный backend
Для operator-grade single-node контура это допустимо. Для multi-node/multi-writer production
нужна более сильная persistence model.

## 5. Публичный Bybit REST не гарантирует полную временную согласованность
Сервис теперь fail-closed отвергает market-data/metadata responses без точного совпадения `symbol`, блокирует нецелевой `category`/non-USDT symbol ещё до REST-запроса и блокирует instrument `status != Trading`, что снижает риск валидации чужими/неактивными лимитами, но не отменяет фундаментальное ограничение публичного REST как источника execution truth.
Сервис делает защитные retry/backoff, transport/decode retry и stale checks, но не получает execution truth.
Если metadata Bybit временно недоступна на execution-path, подтверждение fail-closed блокируется, а не превращается в warning-only запуск. В recommendation-path funding interval берётся из Bybit ticker/instrument metadata; если interval отсутствует и ожидаемый funding impact материален, рекомендация получает блок `FUNDING_INTERVAL_UNCONFIRMED`. Если известен funding rate/interval, но нет `next_funding_ts`, approval-модель считает funding events консервативно по горизонту вместо нулевого carry. На execution-path полноформатные рекомендации с `cost_model` повторно сверяются со свежим funding snapshot: stale/missing interval/rate и ухудшение carry, уничтожающее net edge, блокируют запуск. Explicit sizing validation остаётся возможной только при наличии metadata с lot/notional фильтрами.

## 6. LLM reviewer может быть полезен только как вторичный фильтр
LLM не должен принимать финальное торговое решение вместо scoring/risk/shock логики.

## 7. Cross margin / hedge mode / exact live liquidation modeling не поддержаны
В этой ревизии проект исходит из `futures_grid + isolated` как из безопасного operational minimum. Leverage поддерживается только как Bybit Linear USDT Futures leverage с проверкой `leverageFilter` и conservative worst-boundary liquidation buffer. Точный liquidation price должен подтверждаться внешним execution/reconciliation контуром или Bybit calculator/API account data.

## 8. Telegram alerts best-effort
Оповещения не гарантируют доставку и не заменяют внешний мониторинг / process supervisor.

## 9. Raw publication history по-прежнему хранится полностью
UI/operator-list теперь по умолчанию схлопывает repeated rows одной publication-chain и адаптивно добирает raw-кандидаты,
если одна длинная chain доминирует в snapshot. Audit-след в БД при этом сознательно не удаляется.
Это правильно для расследований и калибровки, однако raw SQL-выгрузки без учёта `publication_root_rec_id`
всё ещё могут визуально выглядеть как поток похожих сигналов.

## 10. Legacy/manual payload compatibility остаётся частично семантической
Execution-time validation теперь fail-closed блокирует futures/linear recommendations без явного `margin_mode`,
а также рекомендации, для которых Bybit metadata относится к другому `symbol` или другой `category/venue`.
Это безопаснее, но означает, что старые вручную заведённые записи могут перестать быть исполнимыми без миграции payload'а.

`account_mode=one_way` сохраняется как legacy-совместимость старых тестовых/исторических rows, однако
это не полноценная модель account-mode текущей ревизии и не должно использоваться как основание для
расширения execution-логики на hedge/cross сценарии.

## 11. Рекомендательный сервис по-прежнему не заменяет внешний reconciliation с биржей
Даже после усиления row-level locking в PostgreSQL, DB-level инвариантов publication-chain и canonical directional semantics (`app.trading_semantics`) проект видит только операторские `trades`, а не реальный поток ордеров/исполнений Bybit. Поэтому окончательная truth-модель позиции, funding и liquidation всё ещё должна жить во внешнем execution/reconciliation контуре. При добавлении live executor его side/reduceOnly mapping должен быть привязан к `bybit_linear_order_semantics()` и покрыт testnet/private API tests.

## 12. Глубокие исторические retrofit-операции больше не выполняются автоматически на каждом старте
Это сознательное решение на безопасность эксплуатации. Иначе штатный restart на БД с накопленной историей может превращаться в тяжёлый full-scan recommendations/ohlcv и визуально выглядеть как зависание сервиса.

Следствие: если нужно ретро-исправить очень старые `pending`/LLM publication chains исторической БД, это следует делать как отдельную maintenance-процедуру, а не ожидать от обычного `python main.py`.


## 13. Live-price guard защищает от устаревшей рекомендации, но не заменяет real execution precheck
Execute-path теперь блокирует подтверждение, если текущий ticker вышел за рекомендованный диапазон или `kill_switch`.
Это снижает риск запуска старой сетки после резкого движения, но внешний execution layer всё равно обязан перед реальным созданием бота заново сверять цену, spread, margin, available balance и фактические лимиты аккаунта.

## Tick-size snapping and operator UI

Auto-generated operator payloads are now snapped conservatively against Bybit metadata: lower boundaries expand downward, upper boundaries expand upward, and step/TP hints round upward. This avoids a UI-only range shrink or thinner per-grid edge after tick alignment. Manual/legacy payloads remain strict: off-tick values are warnings in UI validation and blocking errors on execution preflight.

## 14. Операторская инфографика не является исполнимым контрактом
`how_to_trade.png` и `docs/HOW_TO_TRADE_INFOGRAPHIC.md` описывают quick-reference для оператора. Исполнимость всегда определяется runtime guards: risk status, Bybit metadata, live ticker, funding snapshot, publication-chain TTL, minNotional/qtyStep/minQty и LLM gate, если он включён.

Текущий shipped-профиль использует интервал `min_leverage=3` и `max_leverage=5`. Это сохраняет повышенную чувствительность к ликвидации на верхней границе 4-5x и допустимо только при fail-closed liquidation-buffer проверках, малой марже на bot и явном резерве капитала вне позиции. Если оператор хочет lower-risk профиль, он должен явно снизить `max_leverage` или `min_leverage` в `RISK_LIMITS_JSON` и принять, что часть идей останется `no_trade`/`blocked`, а не станет автоматически исполнимой.

---

## 2026-06-14 Independent full re-audit additions

### RESOLVED/HIGH: one-way same-symbol direction conflict at execution materialization
- **Files**: `app/main.py`, `tests/test_iteration168_execution_direction_conflict_guard.py`
- **Risk**: when `max_symbol_bots` is deliberately raised above 1, the numeric risk gate alone is not enough to prove that a Bybit Linear USDT one-way symbol cannot get incompatible local bot directions. A running long/short/neutral grid on the same symbol must remain the single directional source of truth unless hedge-mode is implemented explicitly.
- **Mitigation added**: execution materialization now checks running bots on the same `(venue, symbol)` inside the serialized write transaction and fail-closed blocks different or unknown directions, while still allowing idempotent re-attach to the same publication root.

### LOW/RESIDUAL: calibration fallback remains advisory and proxy-based
- **File**: `app/calibration.py`
- **Clarification**: the full LogReg + Platt path already uses chronological out-of-fold logits for the Platt-on-top stage, so the issue is not a blanket absence of time-aware validation. The score-only fallback still fits Platt on available historical proxy outcomes when the dataset is below `logreg_min_samples`.
- **Risk**: calibrated confidence can remain over-optimistic on small/non-stationary samples, especially because labels are proxy outcomes rather than real fill/funding/liquidation truth.
- **Mitigation**: effective-sample and class-balance gates remain in place; confidence must still pass risk, shock, freshness, funding, Bybit metadata and execution-preflight gates.

## 2026-06-14 fixed-leverage no-trade clarification

The shipped leverage profile is now an adaptive interval (`min_leverage=3`, `max_leverage=5`). The recommender treats ideas that cannot justify that active interval as `no_trade` / `not_actionable` instead of emitting a synthetic `1x` recommendation and letting it appear as a runtime leverage block. This does not weaken execution safety: legacy/manual `1x` rows remain blocked by execution-time leverage guards, and `no_trade` rows are not executable.

Residual limitation: the service still does not know the operator's actual wallet balance or live liquidation state; external execution/reconciliation must re-check leverage, margin, available balance and Bybit account state immediately before creating any real bot.

## 2026-06-15 execution-preflight liquidation boundary hardening

Execution preflight now recomputes the leverage liquidation-buffer gate against the adverse grid/kill-switch boundary when `leverage > 1`, and takes the minimum of this recomputed value and any supplied `params.economics.liquidation_buffer_pct`. This prevents a manually edited or legacy payload from passing only because the reference-price buffer looks safe while the boundary-side buffer is already below the operator floor. The exact liquidation price still remains an approximation and must be rechecked by an external execution/reconciliation layer with live account data.

## 2026-06-15 UI numeric parsing fail-closed hardening

Resolved: the operator UI numeric helper no longer treats `null`, `undefined`, empty strings or whitespace-only strings as numeric zero. This prevents missing backend/API fields from being rendered or propagated as zero prices, zero risk distances or zero sizing context in frontend-only diagnostics. Literal numeric zero (`0` / `"0"`) remains accepted where a caller explicitly passes it, and downstream guards still reject non-positive prices where prices are required.

## 2026-06-17 deep regression audit additions

### RESOLVED/HIGH: JSON booleans could cross numeric price/qty/UI boundaries
- **Files**: `app/trading_semantics.py`, `app/main.py`, `app/bybit_client.py`, `app/calibration.py`, `app/ui/static/app.js`.
- **Risk**: Python treats `bool` as an `int` subclass and JavaScript `Number(true/false)` yields `1/0`. A malformed manual/legacy JSON field could therefore be interpreted as a real price, qty, leverage, grid count or UI level instead of becoming invalid.
- **Mitigation**: canonical directional math, execution-price extraction, Bybit metadata parsing, calibration numeric parsing and the shared UI parser now reject booleans before numeric coercion. The operator asset cache key was bumped to `manual-ui-v42`.

### RESOLVED/HIGH: chronological OOF did not prove that train labels were observable
- **Files**: `app/outcomes.py`, `app/db.py`, `app/calibration.py`, `migrations/init.sql`, `migrations/init_postgres.sql`.
- **Risk**: an outcome horizon begins at the first tradeable candle, which can be later than recommendation time. Row-order chronology alone can place a label in the train fold even though its future window had not ended when validation decisions began.
- **Mitigation**: newly computed outcomes persist exact `label_available_ts = entry_ts + effective_horizon`; OOF fitting admits a train row only when its recommendation and exact label availability are both strictly earlier than validation time. Equal timestamps and malformed availability are purged.

### LOW/RESIDUAL: legacy outcome rows lack exact label availability
Existing `reco_outcomes` rows receive a nullable schema column but are not assigned an optimistic synthetic timestamp. They remain usable for the final model fitted at the current time, because those labels are already present, but are excluded from historical OOF train folds until enough newly timestamped outcomes accumulate. This can temporarily reduce OOF/Platt coverage; it is an intentional fail-closed trade-off against leakage.


## 2026-06-18 grid-count exact integer semantics

### RESOLVED/HIGH: fractional and conflicting grid-count aliases

`grid_count`, `grid_levels` and persisted nested aliases now use an exact-integer parser and a shared alias resolver. Fractional values, booleans, non-finite values and conflicting aliases are no longer truncated or masked by truthy/falsy fallback chains. Strict execution preflight emits `GRID_COUNT_NOT_INTEGER` / `GRID_COUNT_CONFLICT`; exposure calculations use the larger valid alias while the payload remains blocked.

### RESOLVED/HIGH: canonical grid count in proxy outcomes

Grid outcome labeling now recognises canonical `grid_count` as well as legacy/nested aliases. Historical conflicting payloads use the lower valid count as an oscillation cap, preventing optimistic proxy-return inflation. This does not make proxy labels equivalent to real fills or exchange PnL.

### RESIDUAL: exchange-specific dynamic grid limit and active-order count

Bybit documents a global Futures Grid range of 2–400 grids but may lower the actual maximum for a chosen price range/economic configuration. A running bot can also have fewer active orders than its initial grid count under dynamic-order/trailing mechanics. The recommender/preflight validates the global limit, executable geometry and economic edge, but an external executor/reconciliation layer must confirm the exact Bybit UI/API constraints and live active-order state immediately before and after bot creation.

## 2026-06-18 strict trade-plan integrity and calibration zero semantics

### RESOLVED/HIGH: partial or arbitrary `trade_plan` could satisfy strict execution validation through legacy aliases
- **Files**: `app/main.py`, `tests/test_iteration193_strict_trade_plan_integrity.py`
- **Risk**: a non-empty object such as `{"marker": ...}` or a partial canonical plan could be treated as present while reference/range/kill-switch/grid-step values were silently sourced from legacy/operator aliases. This weakened the proof that the canonical execution contract itself was complete.
- **Mitigation**: strict execution now requires positive finite canonical nested values for `trade_plan.reference_price`, range lower/upper, kill-switch lower/upper and absolute grid step. Aliases remain read-only/UI compatibility data and cannot upgrade an arbitrary object into an executable plan.

### RESOLVED/HIGH: observed zero-valued calibration features were replaced by neutral defaults
- **Files**: `app/calibration.py`, `tests/test_iteration194_calibration_zero_semantics.py`
- **Risk**: Python truthiness fallbacks changed valid `0.0` observations into `0.5`, `0.67` or `0.8` for range score, directional confidence, coherence, normalized spread, liquidity tier and regime confidence. This distorted training/inference parity and could inflate probability-like confidence on weak or absent signals.
- **Mitigation**: defaults are now applied only by `_safe_float` for missing/invalid/non-finite input; valid numerical zero is preserved in both feature snapshots and legacy reconstruction.

### MEDIUM/RESIDUAL: legacy/manual grid step versus level-count mismatch remains warning-only outside generated strict geometry
A global conversion of `GRID_STEP_LEVELS_MISMATCH` to an execution error was tested but caused 31 regressions in the repository's documented legacy/manual compatibility paths and was therefore not retained. Generated strict-geometry payloads remain fail-closed. A future migration should version the execution payload schema, recompute legacy grid geometry into a canonical plan, and only then remove the compatibility warning path. The external executor must independently recompute exact order levels and active-order count before creating a real Bybit grid.

## 2026-06-18 recommendation freshness and timeline audit

### RESOLVED/HIGH: `latest_operator` could resurrect an older LLM-ready snapshot
The operator list now always uses the actual newest recommendation cycle. Status and LLM filters are applied inside that cycle and can no longer search backward and present an older row as current. Explicit historical snapshot modes remain available for diagnostics.

### RESOLVED/HIGH: invalid/future recommendation timestamps looked age-zero
Persisted recommendations with missing, non-positive, malformed or more than 300 seconds future-skewed timestamps are now fail-closed. Age is not reported as zero; execution and operator guard emit timestamp-integrity blocks and the UI identifies the clock error explicitly.

### RESOLVED/MEDIUM: pair history was not observable
The details card now opens a chronological recommendation timeline for the selected `(venue, symbol, bot_type)`, including root/update publications, direction changes, persisted statuses and LLM state. Historical runtime Bybit guards are intentionally not reconstructed with current market data.

### RESIDUAL: history window and historical execution truth
The operator dialog returns at most 2000 recent publication rows and represents recommendation decisions, not exchange fills. Deep forensic export and real order/fill/PnL truth remain responsibilities of DB export and the external execution/reconciliation layer.

## 2026-06-18 history ordering, outcome direction and horizon hardening

### RESOLVED/HIGH: legacy direction casing could invert proxy-outcome economics
- **Files**: `app/outcomes.py`, `tests/test_iteration197_history_horizon_rr_regression.py`.
- **Risk**: support validation lower-cased a value such as `" SHORT "`, but the subsequent return and TP calculations compared the original string. The row therefore passed as a supported short and was then evaluated with long arithmetic, potentially inverting proxy return and contaminating calibration diagnostics.
- **Mitigation**: the outcome worker and its directional helpers now use `app/trading_semantics.py::normalize_execution_direction`; invalid directions fail closed and cannot default to long.

### RESOLVED/HIGH: JSON boolean could shorten the proxy-label horizon
- **Files**: `app/outcomes.py`, `app/db.py`, `tests/test_iteration197_history_horizon_rr_regression.py`.
- **Risk**: Python converted `label_horizon_hours=true` to `1.0`; the futures-grid lower bound then silently converted it to 6 hours instead of the canonical 12-hour label horizon. Runtime labeling and historical lineage repair could therefore treat a malformed row as mature too early.
- **Mitigation**: boolean horizon values are rejected before numeric coercion in both runtime and DB-backfill horizon resolvers, which then use the canonical bot horizon.

### RESOLVED/MEDIUM: zero coherence inflated recommendation expected R:R
- **Files**: `app/recommender.py`, `tests/test_iteration197_history_horizon_rr_regression.py`.
- **Risk**: a valid observed `coherence=0.0` was replaced by the neutral default `0.5` through a truthiness fallback. This increased the expected capture component and overstated recommendation R:R.
- **Mitigation**: defaults are now selected only for missing/invalid values; finite zero is preserved for coherence, trendiness and ATR inputs.

### RESOLVED/MEDIUM: history table order was opposite to operator workflow
- **Files**: `app/ui/static/app.js`, `app/ui/static/index.html`, `tests/test_iteration195_recommendation_history_ui.py`.
- **Mitigation**: the table in **«История и динамика»** is sorted by `ts DESC`, then `sequence DESC`, with invalid timestamps last. The API and SVG timeline remain chronological so graph semantics are unchanged. The helper sorts a copy rather than mutating the source array, and the frontend cache key was bumped.

## 2026-06-18 persistence, shock, funding and outcome integrity audit

### RESOLVED/HIGH: market-shock used at most one-row open-candle removal
`app/shock_guard.py` now validates every candle timestamp with exact-integer semantics and keeps only fully closed rows. Multiple future/open rows, booleans and fractional timestamps can no longer leak into market-shock or fast-veto calculations.

### RESOLVED/HIGH: recommendation rows were mutable through `INSERT OR REPLACE`
`rec_id` is now an immutable audit identity. Exact canonical retries are idempotent; conflicting payloads fail closed and the batch is rolled back to a savepoint. This protects direction, score, status, params and publication lineage from retrospective overwrite.

### RESOLVED/HIGH: recommendation numeric booleans crossed the persistence boundary
Boolean `ts`, score/confidence/R:R/risk, TTL and feature timestamps are rejected before SQLite coercion; `is_outcome_label_root` accepts only boolean or exact 0/1. Legacy poisoned TEXT fixtures remain supported solely to exercise downstream fail-closed readers.

### RESOLVED/HIGH: negative execution friction could create optimistic proxy outcomes
Outcome cost extraction now replaces negative execution/total/net cost with the conservative fallback. Signed funding remains separate; execution friction cannot become alpha.

### RESOLVED/HIGH: boolean funding timestamp undercounted expected events
Malformed/boolean `next_funding_ts` is treated as unknown. Funding event count therefore follows the conservative unknown-schedule path instead of rolling timestamp `1` into a seemingly valid future event.

### RESIDUAL: immutable recommendation identity changes retry semantics
Callers may retry an identical recommendation payload safely, but may no longer reuse a `rec_id` to mutate status or economics. Lifecycle changes must use dedicated state transitions/new publication rows rather than audit-row replacement.

## 2026-06-18 final fail-closed calibration/account-mode re-audit

### RESOLVED/HIGH: non-finite calibration data could become extreme confidence
`PlattScaler` and `LogRegScaler` now return neutral probability `0.5` when an input,
coefficient, intercept or derived logit is `NaN`/`Infinity`. This prevents poisoned
feature snapshots or malformed in-memory models from becoming artificial confidence
near `0` or `1`.

### RESOLVED/HIGH: malformed persisted calibrators could be activated by truthiness
Calibration loaders now require the exact model `type` and real JSON booleans for
`fitted` flags, including the nested Platt layer. Strings such as `"false"` no longer
activate a model. Invalid persisted payloads are rejected and the normal fallback/refit
path is used.

### RESOLVED/HIGH: execution preflight did not strictly prove account-mode compatibility
Strict preflight now blocks a missing account mode, any explicit unsupported mode, and
an instrument whose current Bybit `instruments-info` metadata explicitly reports
`unifiedMarginTrade=false` while the recommendation requires `account_mode=unified`.
Legacy `account_mode=one_way` remains warning-only solely for historical compatibility;
it is not interpreted as support for hedge mode.

### RESOLVED/MEDIUM: string `"false"` could corrupt publication lineage backfill
Historical dedupe backfill now treats only explicit true values as `active_reuse`.
An ambiguous or false string no longer links an independent recommendation to an older
publication root.

### RESIDUAL: public instrument capability is not authenticated account truth
`unifiedMarginTrade` proves instrument capability, not the operator account's current
UTA generation, position mode, wallet state or permissions. Because this repository has
no OMS/EMS/private-account execution layer, the external executor must still verify the
authenticated account mode, `positionIdx=0` one-way configuration, permissions, balance,
open positions and reconciliation immediately before real order creation.

### RESOLVED/MEDIUM: malformed Telegram `ok` value could suppress later alerts
Telegram transport success now requires the literal JSON boolean `true`. A malformed
HTTP 200 payload such as `{"ok":"false"}` no longer counts as delivery success and
therefore cannot start the alert cooldown after a failed notification.

## 2026-07-11 exact temporal/funding integer semantics

### RESOLVED/HIGH: fractional market timestamps could overwrite valid funding/OI keys

Bybit/collector/persistence boundaries previously used `int()` on some OHLCV, ticker, funding and open-interest timestamps. A malformed fractional value such as `1700000000.75` became `1700000000` and could replace a valid row at that logical key. These paths now use the shared exact-integer parser; malformed rows are discarded before persistence.

### RESOLVED/HIGH: malformed funding schedules could become executable assumptions

Fractional `fundingIntervalHour`, instruments-info `fundingInterval`, label horizons and next-funding timestamps were rounded or truncated into plausible schedules. They now remain unknown. Costed execution blocks an invalid interval, and an invalid next-event timestamp uses the conservative unknown-schedule event count rather than an optimistic single-event assumption.

### RESOLVED/MEDIUM: purged calibration accepted fractional temporal boundaries

Recommendation and `label_available_ts` values entering chronological OOF are now exact integers. Fractional values are excluded, preserving the proof that every training label was fully observable before its validation decision.

### RESIDUAL: legacy malformed rows are ignored, not deleted

This patch is schema-free and non-destructive. Existing manually inserted fractional funding/OI rows may remain physically present in SQLite; current readers skip them. If a forensic cleanup is required, back up the database and perform it as a separate maintenance operation. Live PostgreSQL behavior was covered by shared normalization/dialect tests but not by a disposable server integration run.

## 2026-07-11 Bybit response/request integer integrity

### RESOLVED/HIGH: zero-like malformed `retCode` could pass as success

The public client previously used `int(retCode or 0)`, so a missing value, `null`, JSON boolean `false`, an empty value, or a fractional code such as `0.5` became success code `0`. A malformed HTTP 2xx payload could therefore expose its `result` to market-data consumers. The client now requires a present exact integer; invalid shapes follow the existing retryable response-shape path and fail closed after retries.

### RESOLVED/MEDIUM: request windows silently truncated malformed integers

Kline and open-interest `limit`, `start/end` and `startTime/endTime` previously used direct `int()` conversion. Boolean and fractional values could become valid-looking query parameters, while negative or inverted windows were sent upstream. These fields now use exact-integer parsing, non-negative timestamp validation and ordered-window validation before any network request. Exact integral values such as `5.0` remain compatible.

### RESIDUAL: public REST remains snapshot data, not execution truth

Strict response and request controls prevent malformed payload coercion but do not make public REST atomic or authenticated. A future external executor must still re-check current instrument metadata, account state, wallet, positions and order constraints immediately before any real Bybit action.

## 2026-07-11 live execution spread/economics revalidation

### RESOLVED/HIGH: publication-time spread could outlive its economic validity

Execution preflight refreshed price and funding but did not reprice transaction costs from the current best bid/ask. A still-fresh recommendation could therefore pass range/kill-switch checks after spread widened enough to make its per-grid edge negative. Costed Linear USDT futures-grid recommendations now require valid live bid/ask and recompute spread, slippage, fee floor and net edge immediately before operator materialization. The same absolute spread, minimum net-edge and gross/cost-coverage gates used at publication are applied fail-closed.

### RESIDUAL: public top-of-book is still not fill truth

Best bid/ask is a stronger execution-cost input than `lastPrice`, but it does not model queue position, depth, market impact, partial fills or latency between preflight and a real external Bybit action. The repository remains a recommendation/audit service, not OMS/EMS. An external executor must refresh the order book, authenticated fee tier, account state and exact order preview immediately before creation, and must reconcile actual fills afterwards.


## 2026-07-11 execution evidence and realised PnL integrity

### RESOLVED/HIGH: aggregate trade rows could not prove what was actually executed

Legacy rows had no immutable direct `rec_id`, Bybit `execId`/transaction id, order id, fill price/qty or separate funding events. They could not distinguish model loss from execution loss and were insufficient for partial-fill-safe live validation. A new additive `execution_evidence` ledger now records each execution and funding event against immutable `bot_id -> origin_rec_id`, with unique source/external id and conflict-aware retries.

### RESOLVED/HIGH: funding was absent from realised risk accounting

The legacy net result and daily risk stream used gross `pnl - fee`. Signed funding is now stored and included as `gross_pnl + funding - fee` in both compatibility and exact evidence paths.

### RESOLVED/HIGH: slippage could be double-counted or measured against the wrong price

Bybit fill-based gross PnL already reflects actual execution prices. Slippage is therefore not subtracted again. `orderPrice` is retained as exchange evidence but is not treated as a slippage benchmark. An execution event requires a separately timestamped pre-submit/decision benchmark and computes only adverse benchmark-to-fill deviation as a diagnostic.

### RESOLVED/HIGH: mixed ledgers could double-count one bot

Supported writes fail closed if a bot already has rows in the other ledger. Defensive risk aggregation also gives exact execution events precedence over legacy aggregates in a pre-existing mixed database.

### RESOLVED/MEDIUM: exact execution identifiers were exposed by unauthenticated reads

The evidence and live-validation read endpoints require the same admin-key/loopback authorization model as mutating operator routes.

### RESOLVED/MEDIUM: runtime lock SQLite file could enter a release archive

The release builder excludes SQLite/DB/WAL/SHM artifacts, including `data/app.runtime_locks.sqlite`, and is covered by a regression test.

### RESIDUAL/HIGH: no authenticated Bybit reconciliation adapter is shipped

The repository still does not fetch private executions, transaction logs, wallet/position state or unrealised PnL. An external read-only adapter must normalize Bybit millisecond timestamps to UTC seconds, preserve raw exchange payloads, attribute funding to the correct bot, include all fee components/rebates and submit idempotently. No private order-create/amend/cancel code was added.

### RESIDUAL/HIGH: realised-only risk does not capture open inventory

The unified stream improves realised drawdown/cooldown but cannot see mark-to-market loss, liquidation proximity, unfilled grid orders or account-level cross-effects. Those remain external executor/reconciler responsibilities.

### RESIDUAL/MEDIUM: normalized monetary fields use floating persistence

Canonical ids and raw metadata can preserve source evidence, but summaries use SQLite REAL/PostgreSQL DOUBLE PRECISION. For forensic accounting at very large event counts, an external archive should retain exact source decimal strings; migration to fixed-scale decimal accounting is a separate work package.

### RESIDUAL: live edge remains unproven

The new dataset makes validation possible but contains no user fills by itself. Positive expectancy still requires sufficient independent stopped bots, chronological walk-forward evaluation, no-trade/comparator baselines, regime cohorts, calibration reliability and a predefined stop criterion.

## 2026-07-11 outcome capital and daily-risk correction (v1.0.21)

### RESOLVED/HIGH: one directional TP leg could label a losing whole grid as successful

The previous kill-switch precedence fix still allowed `tp_success` to replace a negative whole-grid proxy whenever the price touched `tp_per_leg` and then finished with a large unresolved adverse inventory loss without crossing the outer kill-switch. OHLCV cannot prove that the remaining grid inventory was closed. A per-leg TP is now diagnostic only; `success=1` requires matched oscillation cycles, intact kill-switch geometry and a positive whole-grid net proxy.

### RESOLVED/HIGH: grid proxy return used one-order percentage as full-grid return

Each completed step previously added `step_pct × fill_efficiency` directly to `ret_proxy`, regardless of whether the grid committed 2 or 20 capital slices. The same path therefore produced the same percentage return for different grid sizes and overstated strategy outcomes approximately in proportion to `grid_count`. Gross leg return and per-leg execution cost are now normalized by committed grid capital.

### RESOLVED/HIGH: new grid could exceed the remaining daily drawdown budget

The existing gate blocked only after realised `daily_dd` reached `max_daily_dd_usdt`. It did not ask whether the next grid's conservative loss to kill-switch could fit inside the remaining budget. Execution preflight now estimates adverse kill-switch loss from the largest available/derived position notional and explicit execution friction, and blocks `DAILY_LOSS_BUDGET_EXCEEDED` when the prospective loss is larger than `max_daily_dd_usdt - daily_dd`.

### DATA ACTION: proxy outcomes and calibrators reset to `grid_label_v3`

The return denominator and success semantics changed. On first startup, v1.0.21 deletes legacy `reco_outcomes` and the associated calibrator keys through the existing version-reset path. This does not delete recommendations, bot instances, trades, exact execution evidence or schema data.

### RESIDUAL/HIGH: kill-switch loss remains a conservative model, not live inventory truth

The guard assumes the persisted/derived maximum position notional can be exposed across the adverse reference-to-kill distance. It cannot observe open orders, partial fills, average inventory price, exchange liquidation mechanics or account-level cross-effects. The external read-only executor/reconciler remains required.

## 2026-07-11 outcome/funding integrity correction

### RESOLVED/HIGH: directional TP touch could override a kill-switch breach

The proxy grid label previously allowed `tp_success` to win over a lower/upper kill-switch breach in the same evaluation horizon. A stopped directional grid could therefore be stored as `success=1`, and that false positive could enter bot-specific calibration. Kill-switch breach now has fail-closed precedence, including ambiguous same-candle TP/kill touches; the isolated TP leg no longer replaces the loss/breach proxy.

### RESOLVED/HIGH: recommender rounded malformed funding intervals

Collector/execution boundaries already required exact integer minutes, but `_estimate_cost_model()` still accepted a fractional interval and rounded it into a confirmed schedule. A malformed `720.5` minute value could reduce the canonical 12-hour horizon from two conservative 8-hour events to one apparent event. The cost model now distinguishes missing from invalid intervals and keeps malformed evidence uncertain.

### RESOLVED/MEDIUM: outcome worker truncated recommendation chronology

Legacy/direct SQLite rows with fractional `recommendations.ts` or `features_ref_ts` were converted through `int()`, creating a synthetic decision/entry chronology. Such rows are now skipped with `OUTCOME_SKIP_INVALID_TEMPORAL_FIELDS` and cannot enter `reco_outcomes` or calibration.

### RESIDUAL: proxy outcomes remain non-execution evidence

The correction removes known false-positive paths but does not make OHLC-based grid labels equivalent to actual fills, queue priority, partial fills, open inventory, Bybit fee tier, liquidation waterfall or realised account PnL. Exact execution evidence and chronological comparator validation remain required before any profitability claim.
