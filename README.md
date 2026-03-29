# Bybit Recommender — grid-only build

Сервис собирает рыночные данные Bybit, рассчитывает multi-timeframe признаки, строит рекомендации только для grid-стратегий, дополнительно может подключать локальный LLM-reviewer по свечам и хранит полный audit trail в SQLite.

Проект рассчитан прежде всего на **операторский / полуавтоматический контур**: система формирует интерпретируемую рекомендацию, показывает причины, ограничения и риск-контекст, а оператор уже принимает решение о запуске бота на бирже.

## Поддерживаемые bot_type
- `spot_grid`
- `futures_grid`

## Что делает система
- собирает `spot` / `linear` тикеры и OHLCV по нескольким таймфреймам;
- собирает `funding rate` и `open interest` для perpetual linear;
- ведёт эвристический sentiment pipeline (`global`, `symbol`, `topic` scopes);
- определяет direction/regime на нескольких ТФ;
- считает score / confidence / expected RR / risk score;
- применяет risk-gate, publication-gate, market shock guard и symbol fast-veto;
- при необходимости отправляет кандидат в локальный LLM-reviewer;
- сохраняет рекомендации, решения, outcome-labeling, calibration state, trade history и risk limits в SQLite;
- отдаёт REST API и операторский UI.

## Архитектурный принцип
Система разделяет несколько слоёв:

1. **Data layer** — сбор и нормализация рыночных данных.
2. **Inference layer** — признаки, direction aggregation, regime, scoring.
3. **Control layer** — risk gate, shock guard, publication gate, LLM reviewer.
4. **Audit layer** — `recommendations`, `decision_log`, `bot_instances`, `trades`, `reco_outcomes`.
5. **Operator layer** — UI и API для ручного исполнения и анализа.

Это **не execution engine биржевого уровня** и не полноценный симулятор исполнения. Сервис оценивает пригодность сетапа и его качество, но не заменяет отдельный production-grade execution layer.

## Что важно в текущей версии
### Логика LLM-reviewer
- LLM-review теперь не живёт в ритме каждого нового `rec_id`.
- Свежий review переиспользуется между соседними рекомендациями одного `(venue, symbol, bot_type, direction-signature)`.
- Кэш LLM теперь инвалидируется не только по `model/provider/prompt_version`, но и по **контексту ревью**: набору ТФ и числу свечей на ТФ. Это исключает тихое наследование старого review после изменения входного LLM-контекста.
- Нефинитные значения confidence (`NaN`, `inf`) из LLM больше не превращаются в ложный `1.0`; они безопасно нормализуются к `0.0`.

### Защита от плохих market-data рядов
- Некорректные OHLCV-бары (нефинитные, нулевые/отрицательные цены, отрицательный объём) отсекаются на чтении из БД, чтобы один испорченный бар не ломал features, direction, shock guard и LLM payload.
- Коллектор дополнительно отбрасывает невалидные `ticker`, `OHLCV`, `funding`, `OI` значения ещё до записи в БД.
- Нефинитный sentiment из внешних источников больше не усиливается clamp-логикой до экстремальных значений.

### Интерпретируемость
- В `reasons_json` сохраняются факторы, контекст сигнала, execution-constraints, funding/OI/liquidity, market shock и LLM review.
- UI и API различают raw / calibrated confidence и показывают оператору итоговый статус вместе с блоками решения.
- Для grid-стратегий outcome-labeling и calibration живут отдельно от операторского max holding window.

## Что входит в проект
- сбор spot/linear тикеров и OHLCV;
- сбор funding и open interest для linear;
- sentiment pipeline с global и symbol scopes;
- multi-timeframe direction/regime inference;
- scoring + risk gating + calibration;
- outcome labeling для проверки качества рекомендаций;
- операторский UI;
- REST API для рекомендаций, risk status, sentiment, bot lifecycle и trade ingestion;
- SQLite persistence с decision log и outcome history;
- краткая инструкция оператора в `docs/instrukciya_operatora_bybit_recommender.docx` и `docs/instrukciya_operatora_bybit_recommender.pdf`.

## Ограничения дизайна
- sentiment pipeline остаётся **эвристическим**, а не newsroom/LLM/NER-уровня;
- отсутствие sentiment-данных трактуется как неопределённость, а не как «истинный neutral»;
- grid outcomes остаются приближённой path-approximation, а не биржевой truth-моделью исполнения;
- risk limits начинают полноценно отражать реальность только если в `trades` действительно пишутся realized fills / PnL / fee;
- локальный LLM-reviewer — это **консервативный reviewer поверх движка**, а не замена scoring/risk/calibration;
- проект не предназначен для немедленного запуска на полный объём капитала без staging-прогона.

## Как читать ключевые поля
- `status` — итоговый допуск идеи к рассмотрению.
- `direction` — исполнимое направление для текущего bot_type.
- `confidence` — степень уверенности системы; не читать изолированно от score, RR и risk context.
- `expected_rr` — экономический смысл идеи после учёта friction/funding.
- `risk_score` — грубая оценка рыночной/исполнительной сложности.
- `reasons.direction_agg` — агрегированное направление и структура голосов по ТФ.
- `reasons.execution_constraints` — что можно, а что нельзя исполнить на выбранном bot_type.
- `reasons.llm_review` — second opinion LLM, включая источник (`live`, `cache`, `cache_inherited`, `async_live`, `async_inherited`).

## Быстрый запуск
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

API поднимется на `127.0.0.1:8000`.

## Минимальная проверка после установки
```bash
pytest -q
python -m py_compile app/*.py tests/*.py main.py
```

## Ключевые env
- `DB_PATH` — путь к SQLite;
- `SYMBOLS_SPOT`, `SYMBOLS_LINEAR` — списки символов;
- `MIN_SCORE_TO_RECOMMEND`, `MIN_CONF_TO_RECOMMEND` — publish thresholds;
- `FUTURES_COLLECT_INTERVAL_SEC` — интервал обновления funding/open-interest;
- `CALIB_MIN_SAMPLES` — минимум данных для calibration fit;
- `OUTCOME_HORIZON_FALLBACK_SEC` — fallback horizon для legacy/неизвестных bot_type;
- `ADMIN_API_KEY` — ключ для mutating endpoints;
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional alerts.

### Опциональный локальный LLM-reviewer
Основные настройки:
- `LLM_REVIEWER_ENABLED=1`
- `LLM_REVIEWER_MODE=advisory` или `gate`
- `LLM_REVIEWER_PROVIDER=ollama`
- `LLM_REVIEWER_URL=http://127.0.0.1:11434`
- `LLM_REVIEWER_MODEL=qwen3:8b`
- `LLM_REVIEWER_TFS=15m,1h,4h`
- `LLM_REVIEWER_CANDLES_PER_TF=32`
- `LLM_REVIEWER_MAX_CANDIDATES=60`
- `LLM_REVIEWER_MAX_WORKERS=8`
- `LLM_REVIEWER_MIN_CONFIDENCE=0.65`
- `LLM_REVIEWER_CADENCE_SEC=300`

Режимы:
- `advisory` — LLM пишет second opinion, но не меняет статус рекомендации;
- `gate` — уверенное расхождение LLM с `execution_direction` может перевести идею в `no_trade`.

Важно:
- LLM-reviewer работает асинхронно и не должен блокировать публикацию core-сигналов.
- UI и API умеют показывать `pending`, `ok`, `error`, `cache_inherited`, `async_live` и другие состояния reviewer.
- После изменения `LLM_REVIEWER_TFS` или `LLM_REVIEWER_CANDLES_PER_TF` старый кэш reviewer больше не переиспользуется автоматически.

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
- `POST /api/v1/recommendations/{rec_id}/action` с `{"action":"executed|ignored","operator":"..."}`
- `POST /api/v1/bots/{bot_id}/trades`
- `POST /api/v1/bots/{bot_id}/stop`
- `POST /api/v1/risk/limits`
- `POST /api/v1/sentiment`

## Жизненный цикл исполнения
1. recommendation публикуется со статусом `recommended`;
2. оператор вызывает `/recommendations/{rec_id}/action` с `executed`;
3. создаётся `bot_instance`, recommendation переводится в `executed`;
4. realized trades/PnL пишутся через `/bots/{bot_id}/trades`;
5. risk engine использует `bot_instances` + `trades` для cooldown и дневного PnL / DD;
6. бот останавливается через `/bots/{bot_id}/stop` или `stop_bot=true` в trade request.

## Stability notes
- background loops используют SQLite runtime lock, поэтому активным сборщиком/рекомендером остаётся только один лидер;
- SQLite работает в `WAL`-режиме с увеличенным `busy_timeout`;
- ошибки одного символа не должны ронять весь collect/recommend loop;
- corrupted JSON в критичных местах читается через safe fallback;
- риск-лимиты и многие env-параметры нормализуются и зажимаются в разумные пределы;
- исторические trade rows с невалидными значениями не должны отравлять daily PnL / drawdown summaries.

## Рекомендации перед live-запуском
Не начинать сразу с полного размера.

Рекомендуемая последовательность:
1. Прогонить сервис на реальном market data без исполнения.
2. Проверить `decision_log`, `risk/status`, `health/symbols`, `outcomes/stats`.
3. Убедиться, что `trades` и `bot_instances` пишутся корректно на тестовом сценарии.
4. Запустить минимальный размер / paper-like режим / ручное подтверждение.
5. Только после этого переходить к рабочему объёму.

## Что особенно проверить оператору
- нет ли частых `COLLECT_ERROR`, `LLM_REVIEW_ERROR`, `STALE_DATA_SKIP`, `SYMBOL_DISABLED`;
- не «залипает» ли `pending` у LLM-reviewer;
- совпадает ли Bybit-форма бота с тем, что показывает панель деталей;
- не деградирует ли quality score / confidence после накопления новых outcome labels;
- корректно ли отрабатывают risk limits после записи реальных trade rows.

## Production notes
- используйте внешний process supervisor;
- делайте резервные копии SQLite;
- не храните реальные секреты в `.env` внутри репозитория;
- если нужен полноценный execution layer, его нужно строить отдельно от recommendation engine.
