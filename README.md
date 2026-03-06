# Bybit Recommender — finished audited build

Сервис собирает market data Bybit, считает multi-timeframe признаки, строит рекомендации по типам ботов Bybit и сохраняет полный audit trail в SQLite.

## Что входит
- сбор spot/linear тикеров и OHLCV;
- сбор funding и open interest для linear;
- sentiment pipeline с global и symbol scopes;
- multi-timeframe direction/regime inference;
- scoring + risk gating + calibration;
- outcome labeling для проверки качества рекомендаций;
- REST API для рекомендаций, risk status, sentiment, bot lifecycle и trade ingestion;
- SQLite persistence с decision log и outcome history.

## Что исправлено в этой сборке
- убран train/inference skew в calibration через явный `feature_snapshot`;
- funding/cost model сделан direction-aware и event-aware (`next_funding_ts`);
- outcome labeling для `futures_martingale` переведён на path-based simulation;
- убран скрытый bullish/risk_on default при полном отсутствии sentiment data;
- `futures_combo` перестал публиковаться как action-ready recommendation без two-leg PnL model;
- добавлен execution lifecycle: `recommendation -> executed -> bot_instance -> trades -> stopped`;
- mutating endpoints можно защитить через `ADMIN_API_KEY`;
- вычищены секреты из поставляемого `.env`.

## Ограничения дизайна
- это recommendation/evaluation engine, а не полноценный exchange-grade execution simulator;
- `futures_combo` остаётся эвристическим режимом и блокируется на публикации;
- grid/DCA outcomes остаются упрощёнными path approximations;
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

## Ключевые env
- `DB_PATH` — путь к SQLite;
- `SYMBOLS_SPOT`, `SYMBOLS_LINEAR` — списки символов;
- `MIN_SCORE_TO_RECOMMEND`, `MIN_CONF_TO_RECOMMEND` — пороги публикации;
- `CALIB_MIN_SAMPLES=60` — минимум before calibration;
- `ADMIN_API_KEY` — если задан, обязателен для mutating endpoints;
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional alerts.

## Основные API
### Read-only
- `GET /api/v1/recommendations`
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
- `POST /api/v1/bots/{bot_id}/trades`
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
- confidence для `futures_combo` не притворяется статистически надёжным;
- пустой sentiment теперь создаёт неопределённость, а не сильный `neutral`/`risk_on`;
- funding penalty/bonus учитывает direction и реальный funding event horizon.

## Production notes
- для продакшена используйте внешний процесс supervisor и backup SQLite;
- на mutating endpoints задайте `ADMIN_API_KEY`;
- не храните реальные секреты в `.env` внутри репозитория;
- если нужен реальный execution layer, его следует строить отдельно от recommendation engine.
