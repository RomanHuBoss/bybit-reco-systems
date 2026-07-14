## v1.0.60 market-data transaction ordering

The `collector` and `backfill` loops remain independently supervised and retain separate runtime leadership locks, but their shared `ohlcv` write contract is now explicit:

1. network workers may complete in any order;
2. results are first accumulated in memory;
3. `db.upsert_ohlcv()` deduplicates and sorts the complete transaction by `(venue, symbol, tf_sec, ts)`;
4. the write is committed through the lock-retry boundary, which rolls back a PostgreSQL deadlock victim before replay;
5. decision-log writes use a separate transaction so a noncritical audit-log lock cannot enlarge the OHLCV lock graph.

The hot collector records which source timeframes actually received rows. Derived 15m/30m/4h maintenance runs only for symbols touched at the corresponding source timeframe. In normal lifespan wiring the hot loop fetches 1m only, therefore 4h maintenance belongs to the backfill loop that fetches 1h. This reduces redundant writes without changing candle geometry or persistence schema.

## v1.0.58 operator evidence boundary

`GET /api/v1/outcomes/stats` accepts `scope=current_policy|current_model|archive` and defaults to `current_policy`. `app/main.py` derives the active fingerprint from current settings plus active risk limits. `app/db.py` filters model lineage, recomputes each persisted policy-contract digest, and aggregates only admitted rows. The frontend requests current-policy and archive payloads independently; only the former drives headline and detailed policy tables. No relational migration is required.

Status now publishes a calibration gate contract with separate monetary and probability floors, sample gaps, OOF requirements, and the observability hard-block. These are diagnostics only; they do not weaken publication gates or create execution authority.

## v1.0.57 policy/evidence architecture

The recommendation cycle canonicalizes normalized settings plus active risk limits into a full SHA-256 policy fingerprint before loading calibration. Each root persists that contract, and both the fit path and outer denominator recompute its digest before use. The calibration path is:

`pre-calibration candidate policy -> exact fingerprint cohort -> outer-join observability denominator -> monetary Student-t gates -> purged walk-forward skill -> untouched terminal holdout -> pre-holdout LogReg + Platt activation`.

`app/policy.py` owns canonical JSON hashing. `app/recommender.py` owns policy construction, verified exact-cohort selection and fail-closed inference; its standalone direction Platt is audit-only. `app/outcomes.py` owns queue rotation and waiting/censored/labeled transitions. `app/db.py` owns the verified independent denominator, immutable reconciliation snapshots and distinct profitability versus loss-conservative risk streams. `app/calibration.py` owns Student-t bounds, aggregate/final log-loss comparisons and persistence validation. `/api/v1/status` and the frontend expose the same policy counts and skill metrics.

The relational change is additive and idempotent in both `init.sql` and `init_postgres.sql`: `reco_outcome_observability` and `execution_reconciliations` plus indexes. Existing databases upgrade through normal `init_db()`; no Alembic/manual data rewrite is used. Execution reconciliation is an ingestion boundary for a trusted external read-only adapter, not private order flow.

## v1.0.56 calibration lineage boundary

`app/recommender.py::calibration_lineage_diagnostics()` is the shared source of truth for archive/current/eligible partitioning. Fit paths and `/api/v1/status` use the same filter. `app/calibration.py` uses v19 cache identities and `app/recommender.py` uses direction key v14, so stale v18/v13 objects cannot be loaded as current. PostgreSQL and SQLite schemas are unchanged.

## v1.0.55 candidate-screen and temporal-thinning flow

`app/settings.py` owns the bounded `MEAN_REVERSION_MIN_SCORE` candidate floor. `app/recommender.py::_mean_reversion_grid_blocks()` distinguishes missing evidence (hard block) from a valid score below that floor (strategy `no_trade`) and deliberately makes no PnL claim. The independent `app/calibration.py` monetary gate remains mandatory for actionability.

`app/calibration.py::_temporal_cluster_return_diagnostics()` now forms one cohort per recommendation timestamp, computes a cross-sectional weighted mean without symbol-count credit, and applies earliest-finish interval scheduling. The selected cohort intervals are pairwise non-overlapping and maximal in count. v17 bot/global keys force recomputation from retained outcomes without deleting rows or changing relational schema.

## v1.0.54 calibration activation flow

`matured historical outcomes` -> `monetary/temporal gates` -> `score-only Platt baseline` -> `feature extraction` -> `purged chronological OOF logits` -> `OOF Platt-on-top` -> `feature LogReg activation`.

`app/calibration.py` owns both fitting and the activation boundary. Full-sample feature coefficients are withheld unless the OOF stage is sufficient; persistence records the OOF diagnostics. `app/recommender.py` may report `bot_logreg` only when non-empty coefficients survived that boundary. Otherwise it reports `bot_platt` or raw confidence. This is code-only calibration-state evolution: no outcome label, relational schema or publication lifecycle change.

## v1.0.53 boundary-candle liquidity flow

`compute_outcomes_once()` separates the strategy horizon from evidence availability: `horizon_end_ts = entry_ts + horizon_sec`, while `label_available_ts = horizon_end_ts + 60`. It requires the exact boundary 1m candle to be complete before calling `_grid_outcome()`.

`_grid_outcome()` resets `candle_volume_capacity_qty` and `candle_volume_used_qty` when entering the boundary candle. The same budget is consumed by close-to-open gap fills and terminal residual liquidation. Kill-switch liquidation uses the remaining capacity in the breach candle. `ledger_invalid` is checked after all intrabar path simulations so a capacity failure cannot disappear when equivalent path snapshots are restored. No schema change is required; OHLCV volume and JSON diagnostics already exist.

## Historical kill-switch loss bound (v1.0.52)

`app/outcomes.py::_grid_outcome` separates two prices at a protective exit: the grid-processing boundary and the conservative residual-inventory liquidation bound. Resting orders are processed only up to the configured kill-switch. If the observed intrabar continuation is adverse to the residual position, the proxy closes at the corresponding candle extreme; otherwise it retains the boundary price and does not credit favorable slippage. Ledger snapshots include stop boundary and observed extreme so alternative OHLC paths cannot appear equivalent when their terminal loss bounds differ.

## v1.0.51 historical-simulation boundary

The recommendation/outcome path is intentionally independent of runtime exchange executability. `_reco_thread` does not prefetch current Bybit instrument filters for publication, and `run_recommender_once` has no exchange-normalizer callback. `compute_outcomes_once` labels persisted historical geometry without requiring an exchange snapshot.

`reasons.simulation_scope` is the authoritative boundary: `historical_proxy_only`, no order submission, no runtime execution validation, and no exchange fill attestation. Current Bybit snapping/validation helpers remain available only to explicit operator preflight endpoints; their result cannot change recommendation status, persisted geometry, outcome eligibility, or calibration.

The model remains conservative within OHLCV limits: strict trade-through, candle-volume capacity, delayed replacement activation, cost/funding rules, temporal clustering and monetary lower-bound gates. These are simulation assumptions, not claims that an order would have filled in runtime.

## v1.0.50 outcome ledger timing boundary

The proxy ledger now separates `orders` active before the current candle from `pending_orders` created by fills during that candle. Snapshots used for alternative OHLC paths include both maps. Pending replacements activate only at the next candle boundary; crossing one earlier makes the outcome unavailable because order-placement latency is not observable from OHLCV.

## v1.0.49 outcome execution-capacity boundary

`app/outcomes.py` now reads OHLCV `volume` with each one-minute candle and maintains a path-local aggregate fill budget. Recommendation sizing remains immutable input: the persisted `qty_per_order` is multiplied by simulated slot quantity before any ledger mutation. Intrabar high-first/low-first snapshots include consumed volume so path equivalence cannot hide different capacity usage. The change is computation-only and requires no schema migration because `ohlcv.volume` already exists in both SQLite and PostgreSQL schemas.

## v1.0.48 exchange-evidence boundary (historical, removed in v1.0.51)

Versions 1.0.48-v1.0.50 temporarily coupled publication/outcomes to current Bybit filters and an `exchange_execution_snapshot`. Version 1.0.51 removed the metadata prefetch, normalizer callback and mandatory snapshot check. Strict trade-through and other conservative OHLCV rules remain; current exchange filters do not participate in recommendation status or calibration.

## v1.0.46 funding-alpha boundary

- `app/outcomes.py` maintains signed settled funding diagnostics and a separate conservative funding contribution for canonical proxy return. Only negative/adverse cashflows enter `ret`.
- `app/main.py` advances the outcome contract to `grid_label_v19` and deletes current bot/global/direction calibrator cache keys when labels are reset.
- Exact execution evidence remains signed account truth; proxy calibration remains conservative hypothesis evidence. No DB schema or API route changes are required.

## v1.0.45 temporal evidence aggregation

`app/calibration.py` now has two monetary uncertainty layers. `_weighted_return_diagnostics()` describes row-level returns. `_temporal_cluster_return_diagnostics()` builds interval-overlap components from matured `[ts, label_available_ts]` rows, computes one weighted mean per component, and evaluates effective cluster count, dispersion and a one-sided lower bound. `fit_logreg()` is fail-closed unless both layers pass.

The new diagnostics are persisted inside the existing `app_config.value_json`; no table or migration changes are required. `app/recommender.py` exposes `time_clusters=current/min` and `time_cluster_lower_bound` in the monetary-veto diagnostic. v9 cache keys isolate the new contract from v8 models.

## v1.0.44: terminal exact-evidence boundary

Execution-evidence persistence remains append-only. `db.get_bot_execution_summary()` now adds a deterministic signed-quantity reconciliation layer over immutable execution rows. `db.list_live_validation_records()` exposes both complete and incomplete records for audit, but marks a record eligible only when the bot is stopped and the execution ledger is terminally flat. `main._live_validation_scope_summary()` independently rechecks `total_pnl_finalized=True` before accepting a row, so a malformed caller or stale payload cannot inject partial PnL into the stop gate.

No new table or column is required: finalization is recomputed from existing `execution_evidence.side`, `qty`, `bot_instances.status`, and `stopped_ts`.

## v1.0.43: uncertainty-bounded calibration boundary

`app/calibration.py` now owns monetary uncertainty diagnostics in addition to probability fitting. The persisted LogReg payload carries weighted dispersion, Kish effective sample size, one-sided lower bound and confidence level. `app/recommender.py` treats bot-specific monetary evidence as a prerequisite publication layer: non-positive or unproven evidence creates a shadow `no_trade` before operator action, while preserving the row for independent future outcome accumulation.

The v8 bot/global cache identities prevent v7 positive-mean models from being reloaded under the stricter contract. No relational schema migration is needed because calibration state remains versioned JSON in `app_config`. Direction Platt calibration is diagnostic and remains v6; it cannot override the bot-specific monetary gate.

## v1.0.42: calibration cache lifecycle

Calibration persistence in `app_config` is now a bounded cache, not an independent source of model truth. `app/recommender.py` revalidates stale positive bot/global/direction models against the retained joined outcome dataset; sparse current evidence produces a persisted unfitted state. Negative monetary expectancy remains an asymmetric safety veto. New cache keys force this lifecycle on first startup without deleting outcomes or changing schema.

## v1.0.41: shadow publication lineage

Publication-chain теперь имеет отдельный horizon-aware путь для counterfactual `shadow_no_trade`. Он не меняет operator status (`no_trade` остаётся `no_trade`) и не превращает shadow row в active recommendation. Путь отвечает только за statistical identity: один открытый pseudo-position соответствует одному outcome root, а повторные UI/audit publications становятся children. Это устраняет псевдорепликацию без удаления истории.

## v1.0.40 monetary-expectancy calibration flow

`db.get_outcomes_with_recs()` supplies matured proxy rows including `ret`. `calibration.fit_logreg()` sanitizes timestamps, binary labels and returns, computes recency weights, weighted mean return and 20% lower-tail expected shortfall, then either fits the probability model or returns a persisted negative expectancy state.

`_load_or_fit_bot_logregs()` treats both a fitted positive model and an unfitted negative expectancy state as persistable cache states. In the recommendation loop, `_calibration_expectancy_no_trade_reason()` converts the latter into an explicit strategy `no_trade` before publication. Confidence falls back to capped raw heuristic and cannot use the rejected model. Hard feasibility/risk blocks retain precedence over `no_trade`.

The change is additive JSON inside `app_config`; SQLite/PostgreSQL schemas and public API fields are unchanged. Cache keys move to v5 so v4 coefficients cannot cross the new eligibility boundary.

## v1.0.39 exact-evidence tail-loss control flow

`_execution_preflight()` calls `_compute_live_validation_strategy_health()` before bot audit materialization. The latter reads immutable stopped-bot execution evidence, filters by venue/bot/model version, deduplicates publication roots, builds direction/symbol/portfolio summaries, and applies the sample floors. In v1.0.39 a negative cumulative exact net PnL is the sample-based stop predicate; median and win rate are emitted only as distribution diagnostics. Any resulting `LIVE_VALIDATION_*` block prevents `executed` and bot-instance creation.

## v1.0.38 unavailable-outcome state flow

`recommendation + OHLCV + settled funding` -> outcome ledger. A missing settlement is a retryable dependency state, not a malformed recommendation. The worker writes a rate-limited `OUTCOME_WAIT_FUNDING_SETTLEMENT` event and leaves the recommendation unlabeled. Permanent persisted-contract failures use structured `OUTCOME_SKIP_INVALID_GRID_CONTRACT` reasons. No new database table or API contract is introduced.

## v1.0.37 settled-funding data flow

`BybitPublicClient.get_funding_rate_history` -> collector 35-day paginated backfill -> `funding_settlement(symbol, ts, funding_rate)` -> outcome inventory ledger. Forecast snapshots remain in `funding_rate` for recommendation-time risk; immutable settlements are a separate source of truth for historical labels.

## v1.0.36 cost-layer ownership

- `app/recommender.py` публикует recurring grid fee, one-time market friction и funding как разные поля/слои.
- `app/grid_math.py` считает Grid Profit пары только после двух fill fees и публикует отдельный Total-P&L funding stress.
- `app/outcomes.py` применяет market friction к initial directional entry/terminal residual exit, grid fee к resting fills и funding к фактическому inventory во времени.
- `app/main.py` оставляет spread отдельным live-liquidity gate, recurring fee - per-grid edge gate, funding - отдельным schedule/inventory gate.

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
