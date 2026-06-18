# Известные риски и ограничения

## 1. Нет реального OMS/EMS
Это главный системный риск. Проект не управляет live order lifecycle и не знает реальные open orders/fills.
Следствие: нельзя считать его завершённой автоторговой системой без внешнего execution layer.

## 2. Qty/min-notional validation зависит от фактического размера позиции
Сервис формирует minimum viable `params.sizing` с order notional/qty/margin estimate, округляя provisional qty вверх по conservative fallback step до получения live Bybit metadata, чтобы UI показывал капитал и preflight мог проверить явные значения. Execution-preflight дополнительно проверяет minNotional на нижней цене grid range и согласованность qty/notional, но это всё равно не заменяет live preview фактических ордеров в Bybit. Это не заменяет sizing от баланса аккаунта и не гарантирует совпадение с каждым `qtyStep`: внешний исполнитель обязан повторно сверять `qty_step`, `min_order_qty`, `max_order_qty`, `min_notional`, available balance и фактическую маржу перед созданием Bybit grid bot.

## 3. Outcome labeling остаётся proxy-моделью
Даже усиленная grid-разметка не заменяет реальные fill/funding/liquidation данные.
Использовать её как единственный источник истины для PnL/WR нельзя.

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
