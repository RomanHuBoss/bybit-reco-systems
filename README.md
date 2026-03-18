# Bybit Recommender — grid-only build

Сервис собирает market data Bybit, считает multi-timeframe признаки, строит рекомендации только для grid-стратегий и сохраняет audit trail в SQLite.

## Поддерживаемые bot_type
- `spot_grid`
- `futures_grid`

## Что входит
- сбор spot/linear тикеров и OHLCV;
- сбор funding и open interest для linear;
- sentiment pipeline с global и symbol scopes;
- операторский UI не пытается выдавать heuristic sentiment за полноценный deep news-анализ: в интерфейсе и README это явно помечено как эвристический фон;
- multi-timeframe direction/regime inference;
- scoring + risk gating + calibration;
- outcome labeling для проверки качества рекомендаций;
- операторский UI с явной маркировкой raw/platt/cal confidence;
- REST API для рекомендаций, risk status, sentiment, bot lifecycle и trade ingestion;
- SQLite persistence с decision log и outcome history.

## Что изменено в этой сборке
- проект переведён на grid-only набор стратегий;
- генерация, API, GUI, calibration и outcome labeling оставлены только для `spot_grid` и `futures_grid`;
- legacy-записи с неподдерживаемыми `bot_type` не попадают в read-only выдачу и статусные агрегаты;
- убраны лишние ветки scoring/trade-plan/outcome logic для неподдерживаемых стратегий;
- execution lifecycle, funding/cost model и калибровка оставлены только для активных grid-ботов;
- добавлен двусторонний `market shock guard` (`amber_down`, `red_down`, `amber_up`, `red_up`, `chaos`) с ручным operator lock/guard режимом;
- добавлен fast-veto на уровне символа по 1m/3m/5m импульсу против направления;
- панель деталей переписана под ручной запуск: JSON убран из основной операторской зоны, вместо него выводятся копируемые поля для Bybit (`range`, `grid_levels`, `leverage`, `kill switch`, `TP/SL`), а также ссылки на график и страницу создания бота;
- значения уровней в панели деталей форматируются в bybit-friendly виде: десятичная точка, без разделителей тысяч, с попыткой подстроиться под `tickSize` инструмента;

## Ограничения дизайна
- текущий sentiment pipeline остаётся эвристическим: RSS/Reddit/market context помогают поймать фон, но это не LLM/NER/newsroom-уровень семантического анализа заголовков и статей;
- это recommendation/evaluation engine, а не exchange-grade execution simulator;
- глобальный calibrator хранится для диагностики/статуса, но inference намеренно не использует cross-bot fallback probability;
- grid outcomes остаются упрощёнными path approximations, но теперь label success требует не только net>0, а подтверждённых oscillation legs, приемлемого time-in-range и отсутствия kill-switch breach; maturity horizon для label/calibration фиксирован по bot_type (сейчас 6h), а не по operator-facing max holding window;
- risk limits начинают работать полноценно только если в `trades` реально пишутся realized fills/PnL;
- при смене версии outcome-labeling сервис автоматически очищает `reco_outcomes` и сохранённые calibrator state, чтобы не смешивать старые мягкие метки с новой логикой.

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
- funding penalty/bonus учитывает direction и реальный funding event horizon;
- raw confidence теперь явно маркируется в UI и режется консервативным cap, чтобы оператор не путал heuristic signal с calibrated probability;
- confidence gate применяется только когда для bot_type реально есть fitted calibrator;
- outcome labeling для grid считается на выделенном label horizon, penalizes unresolved drift / range breach и не ждёт operator max_hours.

## Production notes
- для продакшена используйте внешний process supervisor и backup SQLite;
- background loops используют SQLite runtime lock, поэтому даже при multi-worker запуске активным сборщиком/рекомендером остаётся только один лидер;
- на mutating endpoints задайте `ADMIN_API_KEY`;
- не храните реальные секреты в `.env` внутри репозитория;
- если нужен реальный execution layer, его следует строить отдельно от recommendation engine.

## Что важно после обновления
- при первом запуске этой версии сервис сам сбросит старые `reco_outcomes` и сохранённые calibrator state (`OUTCOME_LABEL_VERSION_RESET` в `decision_log`), потому что логика меток стала строже и старые outcome rows больше нельзя смешивать с новыми;
- после сброса win-rate/калибровка временно будут строиться заново по мере накопления свежих исходов;
- если UI показывает `raw`, это operator-grade heuristic confidence с cap, а не откалиброванная вероятность успеха.
