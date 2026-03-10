# Bybit Recommender — grid-only build

Сервис собирает market data Bybit, считает multi-timeframe признаки, строит рекомендации только для grid-стратегий и сохраняет audit trail в SQLite.

## Поддерживаемые bot_type
- `spot_grid`
- `futures_grid`

## Что входит
- сбор spot/linear тикеров и OHLCV;
- сбор funding и open interest для linear;
- sentiment pipeline с global и symbol scopes;
- multi-timeframe direction/regime inference;
- scoring + risk gating + calibration;
- outcome labeling для проверки качества рекомендаций;
- REST API для рекомендаций, risk status, sentiment, bot lifecycle и trade ingestion;
- SQLite persistence с decision log и outcome history.

## Что изменено в этой сборке
- проект переведён на grid-only набор стратегий;
- генерация, API, GUI, calibration и outcome labeling оставлены только для `spot_grid` и `futures_grid`;
- legacy-записи с неподдерживаемыми `bot_type` не попадают в read-only выдачу и статусные агрегаты;
- убраны лишние ветки scoring/trade-plan/outcome logic для неподдерживаемых стратегий;
- execution lifecycle, funding/cost model и калибровка оставлены только для активных grid-ботов.

## Что исправлено в этой версии
- восстановлен `app/recommender.py`: возвращены рабочие `_score`, `_params`, `_expected_rr`, `_mode`;
- заново собрана логика executable direction после перехода на grid-only (`spot_grid` = `neutral/long`, `futures_grid` = `neutral/long/short`);
- убраны падения из-за удалённых констант и несуществующих переменных в confidence/scoring pipeline;
- согласованы score → feature_snapshot → calibration → params → outcome labeling;
- исправлены остаточные legacy-сообщения про удалённые bot_type;
- README обновлён под фактическую рабочую grid-only логику.

## Ограничения дизайна
- это recommendation/evaluation engine, а не exchange-grade execution simulator;
- глобальный calibrator хранится для диагностики/статуса, но inference намеренно не использует cross-bot fallback probability;
- grid outcomes остаются упрощёнными path approximations;
- risk limits начинают работать полноценно только если в `trades` реально пишутся realized fills/PnL.

## Быстрый запуск
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

API поднимется на `127.0.0.1:8000`.

После старта сервис сам создаст SQLite schema, начнёт фоновые циклы collect/reco/outcomes и будет публиковать рекомендации в `/api/v1/recommendations`.

Для локальной проверки после запуска удобно открыть:
- `GET /api/v1/status`
- `GET /api/v1/recommendations`
- `GET /api/v1/outcomes/stats`

## Ключевые env
- `DB_PATH` — путь к SQLite;
- `SYMBOLS_SPOT`, `SYMBOLS_LINEAR` — списки символов;
- `MIN_SCORE_TO_RECOMMEND`, `MIN_CONF_TO_RECOMMEND` — пороги публикации;
- `FUTURES_COLLECT_INTERVAL_SEC` — отдельный интервал обновления funding/open-interest;
- `CALIB_MIN_SAMPLES=80` — минимум before calibration;
- `OUTCOME_HORIZON_FALLBACK_SEC` — fallback horizon только для неизвестных/legacy bot_type (legacy `OUTCOME_HORIZON_SEC` тоже принимается);
- `ADMIN_API_KEY` — если задан, обязателен для mutating endpoints;
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional alerts.

## Основные API
### Read-only
- `GET /api/v1/recommendations` (`min_conf` по умолчанию равен publish-threshold `MIN_CONF_TO_RECOMMEND`)
- `GET /api/v1/recommendations/{rec_id}`
- `GET /api/v1/risk/status`
- `GET /api/v1/bots`
- `GET /api/v1/bots/{bot_id}`
- `GET /api/v1/trades`
- `GET /api/v1/outcomes/stats`
- `GET /api/v1/health/symbols`
- `GET /api/v1/decisions`
- `GET /api/v1/sentiment`
- `GET /api/v1/status`

### Mutating (`X-API-Key`, если задан `ADMIN_API_KEY`)
- `POST /api/v1/recommendations/{rec_id}/action` with `{"action":"executed|ignored","operator":"..."}`
- `POST /api/v1/bots/{bot_id}/trades` (`pnl` = gross realized PnL before fee, `fee` deducted отдельно to net)
- `POST /api/v1/bots/{bot_id}/stop`
- `POST /api/v1/risk/limits`
- `POST /api/v1/sentiment`

## Жизненный цикл исполнения
1. recommendation публикуется со статусом `recommended`;
2. оператор вызывает `/recommendations/{rec_id}/action` с `executed`;
3. создаётся `bot_instance`, recommendation переводится в `executed`;
4. realized trades/PnL пишутся через `/bots/{bot_id}/trades`;
5. risk engine использует `bot_instances` + `trades` для cooldown и дневного PnL;
6. бот останавливается через `/bots/{bot_id}/stop` или `stop_bot=true` в trade request.

## Почему confidence теперь честнее
- калибратор получает тот же feature vector, что и inference;
- в inference остались только стратегии с исполнимым outcome model;
- пустой sentiment теперь создаёт неопределённость, а не сильный `neutral`/`risk_on`;
- funding penalty/bonus учитывает direction и реальный funding event horizon.

## Техническая заметка по grid-only логике
- `global_logreg` остаётся в статусе и диагностике, но inference не использует cross-bot probability fallback;
- если bot-specific calibrator ещё не обучен, confidence остаётся эвристической и дополнительно ограничивается сверху;
- `params.cost_model` и `trade_plan.cost_model` хранят один и тот же execution baseline, чтобы scoring и outcome labeling смотрели на одинаковую стоимость исполнения;
- шаг сетки рассчитывается не ниже cost-aware floor, совместимого с `outcomes._grid_outcome()`.

## Production notes
- для продакшена используйте внешний process supervisor и backup SQLite;
- background loops используют SQLite runtime lock, поэтому даже при multi-worker запуске активным сборщиком/рекомендером остаётся только один лидер;
- на mutating endpoints задайте `ADMIN_API_KEY`;
- не храните реальные секреты в `.env` внутри репозитория;
- если нужен реальный execution layer, его следует строить отдельно от recommendation engine.
