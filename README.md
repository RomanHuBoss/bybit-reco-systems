# Bybit Recommender — grid-only build

Сервис собирает рыночные данные Bybit, рассчитывает multi-timeframe признаки, строит рекомендации только для grid-стратегий, дополнительно может подключать локальный LLM-reviewer по свечам и хранит полный журнал решений и состояний в SQLite.

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
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
pytest --cov=app --cov-report=term-missing -q
python -m py_compile app/*.py tests/*.py main.py
ruff check app tests main.py
```

Эта проверка сознательно разделяет runtime- и dev-зависимости: prod-установка может ограничиться `requirements.txt`, а релизная/аудиторская проверка использует дополнительный `requirements-dev.txt`.

Текущий проверочный baseline этой ревизии:
- `283 passed`
- `python -m py_compile app/*.py tests/*.py main.py` — passed without errors
- `pytest --cov=app --cov-report=term-missing` — запускать в release/dev-контуре; ожидается стабильный coverage baseline не ниже ранее зафиксированного уровня
- `requirements-dev.txt` входит в поставку и фиксирует quality-gate (`pytest`, `pytest-cov`, `ruff`) как часть репозитория, а не как неявную зависимость локального окружения
- регрессионные тесты покрывают collector / hot-vs-backfill separation / Bybit client / health semantics / stale-ticker semantics / long-gap kline catch-up / open-interest pagination / runtime lock loss rollback / heartbeat fail-closed / poisoned historical rows / DB validation / metrics endpoint / bounded-parallel collector soak / sentiment feature compression / bootstrap stage commit / batch ticker fallback / future-poisoned ticker and health paths / dedicated heartbeat connection wiring / transactional rollback для execute-trade-stop API paths / atomic recommender publish rollback / duplicate-trade no-op semantics / latest-operator snapshot selection for non-actionable views / execute-idempotency across one publication-chain / idempotent stop retries without duplicate audit events / rollback on silent-false execute-status transition / rollback on failed stop_bot trade finalization / boot-grace honesty for inherited stale rows / malformed sentiment adapter payloads / poisoned Reddit posts / safe fail-open of `collect_sentiment_once()` / malformed legacy JSON-shapes in recommendation-bot-trade-sentiment APIs / malformed app_config payloads in status and metrics / rejection of blank audit keys for `risk limits version` and explicit `trade_id` / persistence of normalized effective risk limits in bootstrap and mutating API / fail-open fallback from poisoned top-level grid range bounds to valid `trade_plan.levels.range` and `trade_plan.levels.kill_switch` / rejection of `NUL` in sentiment tags and GET-filters / explicit transaction cleanup on idempotent execution paths / sanitization of non-finite `trade_plan` and `cost_model` payloads / correct decomposition of legacy `net_cost_bps` into execution-cost plus funding-carry for outcome-labeling.

## Ключевые env
- `DB_PATH` — путь к основной SQLite БД. Если указан относительный путь, он автоматически разворачивается относительно корня проекта;
- `RUNTIME_LOCK_DB_PATH` — путь к отдельной sidecar-БД runtime lock; по умолчанию это `*.runtime_locks.sqlite` рядом с основной БД. Значение обязано отличаться от `DB_PATH`, иначе bootstrap завершится ошибкой конфигурации;
- `SYMBOLS_SPOT`, `SYMBOLS_LINEAR` — списки символов; дубли теперь автоматически удаляются на bootstrap с сохранением порядка, чтобы один и тот же инструмент не собирался и не скорился несколько раз в рамках одного venue;
- `MIN_SCORE_TO_RECOMMEND`, `MIN_CONF_TO_RECOMMEND` — publish thresholds;
- `FUTURES_COLLECT_INTERVAL_SEC` — интервал обновления funding/open-interest;
- `CALIB_MIN_SAMPLES` — минимум данных для calibration fit;
- `RECO_REPUBLISH_COOLDOWN_SEC` — cooldown для подавления почти идентичных повторных публикаций одной и той же идеи; после этого окна same-direction сигнал всё равно не откроет новый outcome-root, пока предыдущая псевдо-сделка той же chain не доживёт до своего horizon или не получит outcome;
- `OUTCOME_HORIZON_FALLBACK_SEC` — fallback horizon для legacy/неизвестных bot_type;
- `ADMIN_API_KEY` — ключ для mutating endpoints; настоятельно рекомендуется задать перед любым запуском вне localhost/стенда, иначе mutating API остаётся открытым по design для локальной разработки;
- `MASTER_KEY` — Fernet-ключ для шифрования секретов. Теперь валидируется fail-fast на старте: битое значение больше не принимается молча;
- `COLLECTOR_MAX_WORKERS`, `FUTURES_COLLECT_MAX_WORKERS` — bounded parallelism for collector REST fetches;
- `RISK_DAY_TZ` — часовой пояс дневной отсечки для daily PnL / drawdown limits (по умолчанию `UTC`);
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
- `LLM_REVIEWER_TTL_SEC=` — отдельный TTL валидности LLM-review для повторного использования по тому же `(venue, symbol, bot_type, direction)`; по умолчанию не короче TTL самой рекомендации
- `LLM_REVIEWER_KEEP_ALIVE=90s`

Режимы:
- `advisory` — LLM пишет second opinion, но не меняет статус рекомендации;
- `gate` — уверенное расхождение LLM с `execution_direction` может перевести идею в `no_trade`.

Важно:
- Если LLM-reviewer включён, actionable-статусы (`recommended`/`active`) без свежего LLM-вердикта временно переводятся в `pending` и возвращаются обратно после review. Для same-direction reuse теперь используется отдельный TTL валидности reviewer-кэша, поэтому `active` не должен откатываться в `pending` только из-за короткого sweep cadence.
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
  - теперь показывает не только `collector`, но и отдельный `backfill`-контур с его last-cycle/thread state.
- `GET /metrics`

### Mutating (`X-API-Key`, если задан `ADMIN_API_KEY`)
> Для любого окружения с сетевым доступом следует считать `ADMIN_API_KEY` обязательным operational minimum. Если ключ не задан, проект сознательно оставляет mutating endpoints открытыми ради локального/dev-режима.

- `POST /api/v1/recommendations/{rec_id}/action` с `{"action":"executed|ignored","operator":"..."}`
- `POST /api/v1/bots/{bot_id}/trades`
- `POST /api/v1/bots/{bot_id}/stop`
- `POST /api/v1/risk/limits`
- `POST /api/v1/sentiment`

## Жизненный цикл исполнения
1. recommendation публикуется со статусом `recommended`, если это новый actionable выпуск;
2. если same-direction сигнал пришёл, пока предыдущая корневая идея по этому `(venue, symbol, bot_type, direction)` ещё находится внутри своего outcome-horizon, новый `publication_root` не создаётся даже при material-upgrade: запись принудительно сохраняется как `active` в существующей publication-chain, чтобы outcome-labeling имитировал одну открытую псевдо-сделку, а не серию повторных входов;
3. если сигнал повторился уже после закрытия псевдо-сделки, но внутри republish-cooldown и без material upgrade, он тоже сохраняется как `active` в той же publication-chain: запись остаётся исполнимой для оператора, но её lineage указывает на прежний `publication_root_rec_id`, поэтому outcome/calibration считают только корневую публикацию; если предыдущий bot этой chain уже остановлен, новый `execute` обязан создать новый running-бот, а не вернуть старый stopped-instance;
4. если сигнал для persistence-ботов требует подтверждения ещё одним циклом, он получает статус `pending`;
5. проигравшие альтернативы по тому же `(venue, symbol)` уходят в `suppressed` с явной причиной в `reasons.suppression`;
6. оператор вызывает `/recommendations/{rec_id}/action` с `executed` для `recommended` или `active`;
7. создаётся `bot_instance`, recommendation переводится в `executed`;
8. realized trades/PnL пишутся через `/bots/{bot_id}/trades`;
9. risk engine использует `bot_instances` + `trades` для cooldown и дневного PnL / DD;
10. бот останавливается через `/bots/{bot_id}/stop` или `stop_bot=true` в trade request.

### Семантика статусов recommendation
- `recommended` — новый actionable сигнал, готовый к исполнению;
- `active` — повторно актуальный signal-update внутри уже открытой publication-chain; возникает либо при обычном cooldown-reuse, либо при жёстком same-direction pseudo-position lock до завершения horizon. Исполним, но не считается новым выпуском и не создаёт отдельный outcome-root;
- `pending` — кандидат ждёт подтверждения persistence-gate и ещё не исполним;
- `suppressed` — скрытая альтернатива, проигравшая dedupe/selector и сохранённая только для аудита.

## Stability notes
- background loops используют SQLite runtime lock, поэтому активным сборщиком/рекомендером остаётся только один лидер;
- background loops завершаются по lifespan stop-event и не должны переживать штатный stop/restart процесса как «ложно упавшие» daemon-потоки;
- collector работает с явными stage-boundary commit, а не с одной гигантской write-транзакцией через весь цикл: это осознанный компромисс ради корректного heartbeat и отсутствия скрытого split-brain under SQLite;
- SQLite работает в `WAL`-режиме с увеличенным `busy_timeout`;
- ошибки одного символа не должны ронять весь collect/recommend loop;
- corrupted JSON в критичных местах читается через safe fallback, а operator/UI-facing payloads дополнительно нормализуются по ожидаемой форме (`dict`/`list`) вместо прокидывания строк/массивов не того типа наружу;
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

## Инженерные заметки
- используйте внешний process supervisor;
- делайте резервные копии SQLite;
- не храните реальные секреты в `.env` внутри репозитория;
- если нужен полноценный execution layer, его нужно строить отдельно от recommendation engine.

