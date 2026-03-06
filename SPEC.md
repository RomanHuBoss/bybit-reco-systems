# SPEC — audited release

## Назначение
Система формирует рекомендации по типам Bybit Bot на основе market data, multi-timeframe direction inference, sentiment, risk gates и пост-фактум outcome labeling.

## Архитектура
- `collector.py` — сбор spot/linear tickers и klines;
- `features.py` — базовые признаки из OHLCV;
- `direction.py` — multi-TF vote и regime-related direction aggregation;
- `regime.py` — market-wide regime snapshot;
- `sentiment.py` / `sentiment_features.py` — sentiment ingestion и aggregation;
- `recommender.py` — сквозной inference, feasibility, scoring, calibration, publication;
- `outcomes.py` — outcome labeling для back-checking качества рекомендаций;
- `risk.py` — concurrency / cooldown / daily dd controls;
- `db.py` — SQLite contracts и storage helpers;
- `main.py` — API и background threads.

## Важные инварианты
1. `db.get_latest_ohlcv()` возвращает newest->oldest; callers обязаны делать reverse для indicator math.
2. calibration обучается и применяется на одном и том же смысловом feature vector через `feature_snapshot`.
3. recommendation может стать `executed` только через операторское действие; после этого создаётся `bot_instance`.
4. realized PnL учитывается только через таблицу `trades`; без этого risk logic неполна.
5. `futures_combo` не публикуется как action-ready signal без полноценной two-leg PnL/execution model.

## Исполненный scope доработок
- funding model: sign-aware + event-aware;
- martingale outcome: path simulation with averaging ladder;
- admin-protected mutating endpoints;
- bot/trade lifecycle endpoints;
- cleanup shipped env secrets;
- schema hardening: unique index on `origin_rec_id`, index on `trades(bot_id, ts)`.

## Непокрытый scope
- нет exchange-grade fill simulator;
- нет полноценного two-leg hedge PnL engine;
- нет отдельного auth/user model beyond shared admin API key;
- нет внешнего task queue и HA storage.

## Контракты API
### Execute recommendation
`POST /api/v1/recommendations/{rec_id}/action`
```json
{"action":"executed","operator":"alice"}
```
Результат: `recommendation.status=executed`, создаётся `bot_instance`.

### Record trade
`POST /api/v1/bots/{bot_id}/trades`
```json
{"pnl": 12.5, "fee": 0.7, "operator": "alice", "meta": {"exchange_order_id": "..."}}
```
Результат: обновляется `trades`, bot state, дневной PnL для risk engine.

### Stop bot
`POST /api/v1/bots/{bot_id}/stop`
```json
{"operator": "alice", "reason": "manual close"}
```

## Residual risks
- outcome labels всё ещё approximations, а не true execution replay;
- short horizon и sparse data могут делать calibration unstable even with fixed contracts;
- SQLite подходит для single-node service, но не для high-write distributed deployment.
