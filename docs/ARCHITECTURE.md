## v1.0.35 cross-margin safety boundary

The recommendation service models Bybit Linear USDT Futures Grid as a unified-account, cross-margin, one-way product. `app/grid_math.py::arithmetic_grid_cross_margin_stress` is the deterministic safety contract shared by leverage selection and execution preflight. It consumes canonical grid commitment and external kill-switch geometry and returns per-unit committed capital, adverse loss, execution/maintenance reserve and remaining equity buffer. It deliberately does not model or expose a standalone isolated liquidation price.

This remains an audit/recommendation boundary, not a private-account liquidation engine. Live wallet equity, other positions/orders, risk tiers and actual mark-price liquidation are external executor responsibilities.

## Neutral opening-order commitment boundary (v1.0.34)

`app/grid_math.py::arithmetic_grid_commitment` is the single source of truth for both reservation and exposure, but these are intentionally distinct. For NEUTRAL it sums every initial Buy/Sell opening-order price into `committed_notional_per_qty` and counts all initial orders in `committed_slot_count`. It separately returns `max_abs_position_slots` as the larger directional stack. Recommender, snap, preflight, runtime risk and outcomes may not replace the commitment sum with a max-side approximation.

The dynamic bridge contract remains: N intervals, N+1 prices, one idle bridge, exactly N initial orders. Therefore neutral committed slots are N, while maximum net position is generally smaller.

## Dynamic bridge topology boundary (v1.0.33)

`app/grid_math.py::arithmetic_grid_commitment` is the single source of truth for initial arithmetic topology. It emits N+1 prices, exactly N initial orders, one `idle_grid_index`, directional initial inventory, one-way committed slots and maximum position slots. Recommender, payload snap, execution preflight, runtime risk, daily-loss guard and outcome ledger consume this contract; no module may reconstruct an N+1 initial-order model independently.

Outcome replacement orders may later occupy the bridge only after the adjacent fill. A bridge fill before that state transition is a fabricated event and must not be labelled.

## Neutral one-way commitment boundary (v1.0.32)

`app/grid_math.py::arithmetic_grid_commitment` is the single topology/commitment source. It returns all resting orders separately from one-way committed slots and maximum directional exposure. HISTORICAL/SUPERSEDED: v1.0.32 used the larger Buy/Sell price sum. v1.0.34 requires the sum of all initial neutral opening orders; for LONG/SHORT it remains initial inventory plus adverse-side openings. Recommender, snap, preflight, runtime risk and outcomes consume this contract and may not reconstruct commitment from `grid_count` or total active orders.

# Архитектура Bybit Recommender

## Quantity-aware ledger and discontinuous-stop boundary (v1.0.31)

`app/outcomes.py` represents each resting level as a signed integer quantity, not a single side flag. Same-side replacement lots are aggregated; opposing quantities at one level invalidate the proxy contract instead of implying self-trading. Cash, inventory, fees, funding exposure and path-equivalence snapshots all include those quantities.

A continuous observed segment may terminate at a kill-switch. A discontinuous close→open or horizon gap that lands beyond the protection cannot be priced at the skipped boundary and is rejected as unavailable. `app/main.py::_execution_daily_loss_budget_guard` reuses `arithmetic_grid_commitment` for its fallback active-order count.

## Exact commitment/path-invariance boundary (v1.0.30)

`app/grid_math.py::arithmetic_grid_commitment` is the single topology/commitment source for `app/recommender.py`, auto-snap and execution validation in `app/main.py`, and proxy normalization in `app/outcomes.py`. It returns arithmetic levels, buy/sell index sets, initial directional slots, active-order count, maximum position slots and committed notional per unit quantity. Callers may not reconstruct `N × reference` independently.

The outcome engine snapshots the full ledger and executes both admissible high/low orderings when a candle has two material excursions. Non-equivalent snapshots are rejected as unavailable; this preserves temporal uncertainty instead of selecting a favorable or unfavorable path.


## Назначение системы

Проект — это **рекомендательный контур** для операторского запуска grid-ботов на Bybit.
Он **не является OMS/EMS**, не создаёт ордера на бирже автоматически и не пытается
эмулировать полный жизненный цикл биржевого execution layer.

Система должна:
- собирать и нормализовать market data Bybit;
- вычислять признаки и multi-timeframe directional/regime контекст;
- публиковать рекомендации для `futures_grid`;
- блокировать очевидно опасные идеи через risk gate / market shock / fast-veto / LLM-review;
- сохранять audit trail, publication lineage, operator actions и proxy-outcomes.

## Outcome path/stop boundary (v1.0.29)

`app/outcomes.py` now treats the persisted arithmetic grid as an explicit protected state machine. Non-grid-line directional entry creates all adjacent close orders and matching initial slots. The worker processes observable endpoint segments, accepts only unambiguous one-sided OHLC excursions, and terminates cash/inventory/funding evolution at the first valid kill-switch boundary. Missing/inside-range protection or dual-boundary intrabar ambiguity produces no label.

## Outcome temporal/contract boundary (v1.0.28)

`app/outcomes.py` treats publication time as an availability boundary. A proxy position may start only at the open of the first exact 1m candle strictly after both the signal reference bar and the persisted recommendation publication. Missing exact candles remain unavailable.

Duplicated persisted grid/funding fields are one contract. Valid duplicates must agree; explicit malformed or conflicting aliases do not receive a first-wins or conservative fallback. An invalid contract is skipped before `reco_outcomes` insertion, preserving calibration integrity.


## Границы ответственности

### Что система делает
- читает публичные REST-данные Bybit;
- формирует trade idea и operator guidance;
- проверяет исполнимость trade plan по известным ограничениям инструмента;
- ведёт журнал рекомендаций, операторских действий и агрегированных trade rows;
- даёт API/UI для ручного подтверждения и анализа качества рекомендаций.

### Что система сознательно не делает
- не выставляет и не сопровождает реальные ордера на Bybit;
- не хранит живую книгу ордеров и не восстанавливает её после рестарта;
- не моделирует websocket-order stream / execution report stream;
- не знает фактический размер leg, если его не сообщает внешний исполнитель;
- не гарантирует корректность live PnL/fee/funding без внешнего источника фактических trade/fill данных.

## Слои архитектуры

### 1. Data layer
Модули: `collector.py`, `bybit_client.py`, `sentiment.py`, `sentiment_features.py`

Функции:
- сбор тикеров и OHLCV;
- сбор funding/open interest для linear;
- backfill исторических окон;
- нормализация битых payload'ов;
- ограничение параллелизма и heartbeat runtime-lock'ов.

### 2. Inference layer
Модули: `features.py`, `direction.py`, `regime.py`, `recommender.py`, `calibration.py`

Функции:
- вычисление market features;
- multi-timeframe vote и directional aggregation;
- определение regime;
- построение grid-параметров и trade plan;
- calibration / logreg / Platt scaling для quality-контуров.

### 3. Control layer
Модули: `risk.py`, `shock_guard.py`, `llm_review.py`, части `main.py`

Функции:
- risk limits и execution-time recheck;
- market shock state machine;
- symbol fast-veto;
- LLM second opinion;
- preflight перед operator execution.

### 4. Persistence and audit layer
Модуль: `db.py`

Функции:
- SQLite schema и миграции bootstrap;
- recommendations / decision log / bots / trades / outcomes;
- publication lineage;
- runtime locks;
- app_config и техническое состояние фона.

### 5. Operator/API layer
Модуль: `main.py`, `app/ui/static/*`

Функции:
- REST API;
- UI для просмотра рекомендаций и деталей;
- ручное подтверждение `executed|ignored`;
- запись агрегированных trade rows;
- остановка bot instance в audit-контуре.

## Поток данных

1. `collector` собирает тикеры и свечи, пишет их в SQLite.
2. `recommender` читает последние признаки и формирует recommendation snapshot.
3. recommendation проходит control layer: risk gate, shock guard, dedupe, publication lineage, опционально LLM review.
4. published recommendation сохраняется в `recommendations` и `decision_log`.
5. оператор вручную подтверждает `executed`.
6. сервис повторно делает execution preflight и только после этого materialize'ит `bot_instance`.
7. внешний исполнитель или оператор пишет агрегированные `trades`.
8. `outcomes.py` размечает outcome-root записи через close-to-close arithmetic-grid order/inventory ledger по persisted range/count, применяет execution cost к каждой inferred leg и terminal close, adverse funding - только к фактическому inventory на точных событиях внутри horizon, а success - по знаку total net PnL с kill-switch precedence; результат остаётся OHLCV proxy, а не execution truth.
9. calibration использует outcome history для quality-моделей.

## Потоки и конкуренция

Фоновые контуры:
- `collector`
- `backfill`
- `futures_meta`
- `sentiment`
- `reco`
- `llm_reviewer` (если включён)

Защиты:
- отдельная SQLite sidecar-БД для runtime-lock'ов;
- heartbeat leader-lock'а;
- supervised background wrapper с явным thread state;
- bounded parallelism для REST-fetch задач;
- WAL + busy_timeout для основной SQLite.

## Модель согласованности

### Что считается источником истины
- market data snapshot — SQLite tables `ohlcv`, `ticker_snap`, `features`;
- publication-chain — `recommendations.publication_root_rec_id`;
- operator-facing recommendation list делает adaptive raw-scan перед collapse, чтобы длинная одна chain не вытесняла остальные уникальные идеи из `top_n`;
- operator execution state — `bot_instances`;
- realised operator/audit events — `trades`, `decision_log`.

### Что считается приближением
- `trade_plan` и `expected_rr`;
- `risk_score`;
- `reco_outcomes.ret` и `success`;
- daily PnL / DD при неполных trade rows;
- LLM review.

## Execution-time preflight

Перед переводом recommendation в `executed` система перепроверяет:
- freshness candles/tickers;
- active symbol disable state;
- market shock blocks;
- symbol fast-veto;
- геометрию trade plan относительно Bybit metadata;
- внутреннюю согласованность bot_type / venue / direction / mode;
- отсутствие обязательного `margin_mode` для supported execution paths (fail-closed для legacy/manual rows);
- символическую согласованность Bybit metadata (`symbol/category` не должны относиться к другому инструменту);
- leverage bounds и alignment по `leverage_step`, если биржа их предоставляет.
- exact-evidence strategy health по direction, symbol и portfolio: persistent realised losses блокируют новые operator executions до ревизии модели.

## Ключевой архитектурный вывод

Проект можно считать **production-ready только как recommendation + audit service**.
Для production-grade auto-execution нужен отдельный OMS/EMS-контур с order/fill state machine,
идемпотентным order routing, websocket reconciliation и recovery по реальным ордерам.


## Дополнительные инварианты этой ревизии
- `runtime_locks` в PostgreSQL захватываются атомарно через одну UPSERT-операцию; схема `SELECT`→`UPDATE` для leader-election признана небезопасной из-за риска split-brain.
- `bot_instances.publication_root_rec_id` materialized и используется как DB-level инвариант для запрета двух одновременных `running`-ботов в одной publication-chain.
- mutating API-пути в PostgreSQL теперь дополнительно берут row-level lock (`FOR UPDATE`) на целевую `recommendations`/`bot_instances` строку, чтобы concurrent `execute` / `trade` / `stop` не принимали решения по устаревшему snapshot и не теряли агрегаты состояния.


## Дополнительный execution-time guard текущей ревизии

Operator execution path теперь содержит отдельный live-price guard между freshness-check и materialization `bot_instance`.
Он использует последний валидный ticker из persistence-слоя и сохранённый `trade_plan`, чтобы не позволить оператору подтвердить grid-рекомендацию, рассчитанную для уже неактуального диапазона.
Guard не отправляет и не отменяет ордера; он только блокирует операторское подтверждение в audit/recommendation контуре.


## Execution evidence validation contour

`external read-only Bybit adapter -> authenticated evidence API -> execution_evidence -> unified realised event stream -> risk/drawdown/cooldown + descriptive validation export`

`execution_evidence` is additive to dual persistence and never performs order operations. It stores immutable linkage to `bot_instances.origin_rec_id`, exact external identities, exchange fill fields, a separate benchmark snapshot and separate execution/funding event types. SQLite and PostgreSQL use the same logical contract. Legacy `trades` remains for compatibility but is mutually exclusive per bot.

The architecture deliberately separates:

- execution truth: actual fill/funding events;
- execution-quality diagnostic: adverse benchmark-to-fill deviation;
- validation claim: not produced automatically.

The realised stream also feeds a conservative preflight stop gate. It deduplicates publication roots, scopes evidence to the explicit recommendation `model_version`, and blocks continued operator execution after predefined negative direction/symbol/portfolio evidence. The gate is a safety response to losses, not an automatic claim that non-blocked cohorts have positive expectancy.

Private exchange reconciliation, raw payload archival, account inventory and unrealised PnL remain outside this repository.

## Independent range-edge validation (v1.0.20)

Inference layer теперь разделяет два разных понятия: отсутствие направленного тренда и подтверждённую anti-persistence. `app.direction` вычисляет mean-reversion diagnostics на каждом закрытом TF; `aggregate_direction` формирует weighted evidence; `app.recommender` применяет hard publication gate. Это separation-of-concerns не позволяет score/LLM/risk слоям трактовать low trend как достаточный alpha signal.

Calibration contour версионирован отдельно: recommendation identity `bybit-taxonomy-v3-mean-reversion`, LogReg/Platt keys v4 и фильтрация training rows по model version + evidence snapshot. Старые DB rows сохраняются как audit history, но не участвуют в новой калибровке. Схема БД не меняется.
## Shadow outcome branch

`candidate -> deterministic gates -> no_trade` не должен становиться тупиком обучения. Если payload полный и hard blocks отсутствуют, recommender добавляет explicit `outcome_policy(sample_role=shadow_no_trade, eligible=true)`. Outcome worker принимает только этот literal opt-in, повторно проверяет `risk_checks.passed` и после horizon создаёт counterfactual proxy label. Hard-blocked/pending/malformed/legacy rows остаются вне sample. Calibration и UI получают sample-role diagnostics; реальное исполнение по-прежнему подтверждается только external execution evidence.

