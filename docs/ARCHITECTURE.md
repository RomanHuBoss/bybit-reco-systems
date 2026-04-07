# Архитектура проекта

## 1. Назначение системы
Проект является **рекомендательным и операторским контуром** для grid-стратегий на Bybit.
Он:
- собирает рыночные данные Bybit через публичный REST API;
- рассчитывает признаки, regime/direction и risk-context;
- публикует рекомендации по `spot_grid` и `futures_grid`;
- ведёт audit-историю решений, bot lifecycle и операторских trade-событий в SQLite.

Проект **не содержит полноценного биржевого execution-layer**:
- ордера на Bybit не отправляются;
- websocket-order-stream / private fills / reconciliation с биржей отсутствуют;
- `bot_instances` и `trades` — это операторская модель исполнения и аудита, а не живой OMS/EMS.

## 2. Слои системы
### Data layer
Модули:
- `app/bybit_client.py`
- `app/collector.py`
- `app/sentiment.py`
- `app/sentiment_features.py`
- `app/db.py`

Задачи:
- сбор тикеров, OHLCV, funding rate, open interest;
- нормализация входных payload;
- запись снапшотов в SQLite;
- контроль stale-data и runtime leadership.

### Inference layer
Модули:
- `app/features.py`
- `app/direction.py`
- `app/regime.py`
- `app/recommender.py`
- `app/calibration.py`

Задачи:
- multi-timeframe feature engineering;
- aggregate direction / regime inference;
- scoring, confidence, expected RR;
- persistence-gate, publication dedupe, LLM review.

### Risk / control layer
Модули:
- `app/risk.py`
- `app/shock_guard.py`
- `app/alerts.py`
- `app/security.py`

Задачи:
- лимиты по активным ботам, symbol-cap, daily DD, cooldown;
- market shock guard и symbol fast-veto;
- защита mutating API;
- операторские алерты.

### Operator / API layer
Модуль:
- `app/main.py`

Задачи:
- REST API;
- background loops;
- operator lifecycle (`executed`, `ignored`, stop-bot, trade ingestion);
- execution-time preflight перед подтверждением `executed`;
- status/metrics endpoints.

## 3. Хранилище
Основное состояние хранится в SQLite:
- `ohlcv`, `ticker_snap`, `funding_rate`, `open_interest`, `sentiment`, `features`, `market_regime`;
- `recommendations`, `decision_log`, `bot_instances`, `trades`, `reco_outcomes`, `risk_limits`, `app_config`.

Runtime leadership вынесен в отдельную sidecar-БД (`RUNTIME_LOCK_DB_PATH`), чтобы не смешивать критичные heartbeat-locks с основной write-нагрузкой.

## 4. Runtime-модель
Запускаются фоновые циклы:
- collector hot-pass;
- backfill;
- futures meta;
- sentiment;
- recommender;
- optional LLM-review sweep.

Каждый цикл использует отдельный runtime lock и heartbeat. Потеря leadership считается fail-closed событием и логируется.

## 5. Границы ответственности
### Что проект делает корректно
- оценивает пригодность символа/режима для grid-идеи;
- предотвращает часть operator-side ошибок через status machine, risk gates, execution-time preflight и audit trail;
- позволяет воспроизводимо анализировать качество публикаций и outcomes.

### Что остаётся вне системы
- реальное выставление/отмена/replace ордеров на бирже;
- reconciliation с private execution stream Bybit;
- управление позициями, margin, liquidation и биржевыми reject/error-кодами в execution path;
- точный fill-level PnL/funding truth.

Для production-grade auto-trading нужен отдельный execution-service с идемпотентными order-intents, reconciliation по private WS/REST и независимым risk-kill-switch.
