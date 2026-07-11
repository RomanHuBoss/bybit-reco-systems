# Архитектура Bybit Recommender

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
8. `outcomes.py` размечает outcome-root записи по proxy-логике grid-outcome.
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

Private exchange reconciliation, raw payload archival, account inventory and unrealised PnL remain outside this repository.
