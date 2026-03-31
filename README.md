# Bybit Recommender — grid-only build

Сервис собирает рыночные данные Bybit, рассчитывает multi-timeframe признаки, строит рекомендации только для grid-стратегий, дополнительно может подключать локальный LLM-reviewer по свечам и хранит полный audit trail в SQLite.

Проект рассчитан прежде всего на **операторский / полуавтоматический контур**: система формирует интерпретируемую рекомендацию, показывает причины, ограничения и риск-контекст, а оператор уже принимает решение о запуске бота на бирже.

## Поддерживаемые bot_type
- `spot_grid`
- `futures_grid`

## Что дополнительно усилено в текущей ревизии
- Heartbeat runtime lock теперь работает fail-closed: если отдельное heartbeat-соединение не может обновить лидерский lock, collector считает лидерство потерянным и останавливает текущий проход вместо молчаливого продолжения цикла со стареющим lock.
- `health/symbols` и `/metrics` теперь оценивают свежесть **не только по 1m candles, но и по ticker snapshots**. Состояние `ok` возможно только когда свежи обе плоскости данных; свежие свечи при старом тикере больше не маскируют деградацию execution/cost контекста.
- `run_recommender_once()` теперь тоже требует свежий ticker. Символ пропускается, если 1m candles свежи, но quote-layer отсутствует или устарел; это убирает внутренне противоречивый режим, в котором idea считалась tradeable по chart data, но spread/cost assumptions уже были неактуальны.
- Mutating API-paths (`execute recommendation`, `record trade`, `stop bot`) переведены на цельные транзакции без промежуточных commit. Bot instance, status recommendation, trade row, state patch и audit-log теперь фиксируются как одна операция или целиком откатываются при сбое follow-up шага.
- Добавлен отдельный endpoint `/metrics` с ключевыми gauge-метриками: здоровье символов, число активных рекомендаций, недавние ошибки сбора, длительность последнего collector cycle и настройки worker-параллелизма.
- Внутренние записи в `decision_log` внутри collector больше не коммитят данные посреди случайных веток ошибок. Ошибки API-stage теперь сначала буферизуются в памяти и сбрасываются в БД только на явной stage-boundary, а не через побочный `commit` из произвольной точки цикла.
- Heartbeat runtime lock теперь действительно идёт через отдельное SQLite-соединение, а не через тот же connection, что пишет collector-stage. Это убирает скрытый partial-commit риск и делает stage-boundary commit честным: потеря лидерства больше не может «случайно» зафиксировать чужую незавершённую запись через heartbeat.
- REST-fetch для OHLCV и open interest теперь поддерживает bounded parallelism через `COLLECTOR_MAX_WORKERS` и `FUTURES_COLLECT_MAX_WORKERS`, так что full-universe режим больше не остаётся строго однопоточным.
- Исправлен cold-start разрыв целостности для derived TF: `15m` / `30m` получают одноразовый REST bootstrap, если локально ещё нет достаточной истории для multi-timeframe logic. Для `4h` bootstrap теперь **не выполняется**, если уже есть достаточная `1h` история для локальной сборки. Это убирает лишние REST-вызовы и ложные bootstrap-ошибки при полностью достаточном source TF.
- Сбор `open interest` и OHLCV теперь действительно gap-aware после простоев: длинные разрывы восстанавливаются адаптивными окнами, а open interest дополнительно дочитывается через cursor pagination, если разрыв больше одного API-page. Слой БД жёстче фильтрует невалидные `OI`/`funding` записи и не позволяет «битым» историческим строкам отравлять latest-ts и downstream сигналы.
- `get_latest_ohlcv_ts()` и `get_latest_open_interest_ts()` больше не доверяют сырому `MAX(ts)` безусловно: если в БД попала испорченная строка из старой сборки, ручного импорта или прошлой версии, incremental collector ориентируется на **последнюю валидную** запись, а не на мусорный timestamp. То же касается `health` и ticker-latest путей: future-poisoned market-data строки больше не могут подменить собой «последнюю свежую» запись.
- Добавлена дополнительная защита логики признаков: `funding_signal()` и `oi_trend()` теперь безопасно обрабатывают `NaN/inf` и грязные ряды вместо молчаливого протаскивания нефинитных значений.
- Derived bootstrap stage теперь тоже коммитится на явной stage-boundary. Раньше он мог успешно собрать `15m/30m`, а затем потерять leadership на следующем heartbeat и откатить уже рассчитанный bootstrap, хотя README обещал обратное.
- Если batch `get_tickers(category=...)` временно падает, collector больше не теряет весь venue-цикл целиком: фиксируется один batch-error и включается per-symbol fallback-path.
- Добавлены новые регрессионные тесты на длинный kline catch-up после downtime, open-interest cursor pagination, rollback при потере runtime lock, buffered error handling внутри collector-stage, poisoned historical rows, DB-level валидацию `funding/open interest`, пропуск лишнего `4h` bootstrap и защиту feature-layer от грязных значений.
- Collector thread теперь валидирует heartbeat не только внутри `collect_once()`, но и **после** завершения venue-stage. Потеря лидерства между концом stage и переходом к следующему шагу больше не позволяет тихо продолжить цикл или зайти в futures-meta фазу под протухшим lock.
- Recommender loop теперь тоже использует runtime-lock heartbeat и прекращает follow-up работу (`expire`, `prune`, alerts) при потере лидерства. Это закрывает split-brain сценарий, где основной расчёт уже потерял lock, но поток всё ещё модифицировал housekeeping-состояние.
- `/metrics` и внутренний telegram health-path теперь считают только **активные venues** из текущего `VENUES`, а не все исторически настроенные списки символов. Это убирает расхождение между `/api/v1/health/symbols` и Prometheus/alerting картиной.
- `oi_trend()` больше не изображает полноценный `24h` OI-trend по короткому хвосту после рестарта. Для `24h` классификации теперь требуется реальная глубина истории, иначе сигнал помечается как `unknown` вместо искусственно уверенного `growing/falling/stable`.

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
- Свежие cache-hit ключи больше не съедают весь live candidate budget: sweep теперь сканирует весь pending-срез последнего snapshot и применяет live cap уже после cache-resolution. Это устраняет starvation, при котором часть символов могла висеть `pending` практически бесконечно.
- Нефинитные значения confidence (`NaN`, `inf`) из LLM больше не превращаются в ложный `1.0`; они безопасно нормализуются к `0.0`.

### Защита от плохих market-data рядов
- Некорректные OHLCV-бары (нефинитные, нулевые/отрицательные цены, отрицательный объём) отсекаются на чтении из БД, чтобы один испорченный бар не ломал features, direction, shock guard и LLM payload.
- Коллектор дополнительно отбрасывает невалидные `ticker`, `OHLCV`, `funding`, `OI` значения ещё до записи в БД.
- Нефинитный sentiment из внешних источников больше не усиливается clamp-логикой до экстремальных значений.

### Дополнительные усиления в этой ревизии
- `DB_PATH` теперь нормализуется к абсолютному пути относительно корня проекта. Перезапуск из другой shell-директории больше не уводит сервис в случайный `./data/app.db`.
- Панель «Детали» в UI теперь корректно сбрасывает устаревший `rec_id` после `404` и перестаёт бесконечно запрашивать несуществующую запись.
- История OHLCV валидируется строже: отбрасываются не только `NaN/inf`, но и логически невозможные бары (`high < open/close/low`, `low > open/close/high`).
- При чтении OHLCV используется overfetch перед фильтрацией, поэтому пачка битых последних баров не лишает движок достаточной истории для features / direction / LLM payload.
- Crossed quotes (`ask < bid`) теперь санируются и не превращаются в ложный «нулевой спред». Cost-model в таком случае получает безопасный fallback вместо чрезмерно оптимистичной оценки.

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
pytest --cov=app --cov-report=term-missing -q
python -m py_compile app/*.py tests/*.py main.py
```

Текущий проверочный baseline этой ревизии:
- `125 passed`
- покрытие `app/*` — `74%`
- регрессионные тесты покрывают collector / Bybit client / health semantics / stale-ticker semantics / long-gap kline catch-up / open-interest pagination / runtime lock loss rollback / heartbeat fail-closed / poisoned historical rows / DB validation / metrics endpoint / bounded-parallel collector soak / sentiment feature compression / bootstrap stage commit / batch ticker fallback / future-poisoned ticker and health paths / dedicated heartbeat connection wiring / transactional rollback для execute-trade-stop API paths / post-stage heartbeat lock loss / recommender lock-loss housekeeping skip / active-venue metrics semantics / true-lookback OI trend semantics

## Ключевые env
- `DB_PATH` — путь к SQLite. Если указан относительный путь, он автоматически разворачивается относительно корня проекта;
- `SYMBOLS_SPOT`, `SYMBOLS_LINEAR` — списки символов;
- `MIN_SCORE_TO_RECOMMEND`, `MIN_CONF_TO_RECOMMEND` — publish thresholds;
- `FUTURES_COLLECT_INTERVAL_SEC` — интервал обновления funding/open-interest;
- `CALIB_MIN_SAMPLES` — минимум данных для calibration fit;
- `OUTCOME_HORIZON_FALLBACK_SEC` — fallback horizon для legacy/неизвестных bot_type;
- `ADMIN_API_KEY` — ключ для mutating endpoints;
- `COLLECTOR_MAX_WORKERS`, `FUTURES_COLLECT_MAX_WORKERS` — bounded parallelism for collector REST fetches;
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
- `LLM_REVIEWER_MAX_CANDIDATES=24`
- `LLM_REVIEWER_MAX_WORKERS=2`
- `LLM_REVIEWER_MIN_CONFIDENCE=0.65`
- `LLM_REVIEWER_CADENCE_SEC=300`
- `LLM_REVIEWER_KEEP_ALIVE=90s`

Режимы:
- `advisory` — LLM пишет second opinion, но не меняет статус рекомендации;
- `gate` — уверенное расхождение LLM с `execution_direction` может перевести идею в `no_trade`.

Важно:
- LLM-reviewer работает асинхронно и не должен блокировать публикацию core-сигналов.
- В shipped-профиле reviewer настроен консервативно для локальных GPU уровня RTX 3060: короткий keep-alive, сниженный parallelism и ограниченное число live-кандидатов на sweep.
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
- `GET /metrics`

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
- collector работает с явными stage-boundary commit, а не с одной гигантской write-транзакцией через весь цикл: это осознанный компромисс ради корректного heartbeat и отсутствия скрытого split-brain under SQLite;
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
