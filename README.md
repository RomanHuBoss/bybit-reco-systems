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
- Telegram alerting теперь считает доставку успешной только при `HTTP 200` **и** `{"ok": true}` в ответе Telegram API. Ложный `200 OK` с `{"ok": false}` больше не ставит cooldown и не приводит к тихой потере следующего реального алерта.
- Public Bybit client больше не доверяет форме upstream JSON вслепую: response обязан быть объектом с валидным `retCode`, а `result.list` санируется от недиктовых элементов. Это устраняет неявные `AttributeError`/`ValueError` на сломанном proxy/mock payload и делает причину сбоя диагностируемой.
- Убраны мёртвые переменные и неоднозначные ветки в runtime/status-коде; статический smoke-check по `ruff` теперь проходит на всём `app/` слое без предупреждений.

- Risk limits теперь приводятся к каноническим *effective limits* ещё в mutating API и bootstrap-конфигурации: в БД и decision log больше не сохраняется сырой операторский payload, который runtime потом тихо clamp'ит по-другому. Это устраняет расхождение между `/api/v1/risk/status`, активной записью `risk_limits` и audit trail.
- Outcome-labeling теперь игнорирует poisoned `price_range_lower/upper` и kill-switch bounds (`"NaN"`, `"Infinity"`, отрицательные/нулевые значения) и корректно откатывается к валидным границам из `trade_plan.levels.*`. Битый top-level JSON больше не отключает range/kill-switch penalties и не искажает историческую разметку grid-рекомендаций.
- Ручной `POST /api/v1/sentiment` теперь нормализует операторский `key` и список `tags`: пробелы по краям убираются, пустые/дублирующиеся теги не пишутся в БД и decision log. Пустой `key` отвергается с `422`, чтобы не плодить бессмысленные sentiment-series.
- Outcome-labeling для legacy/manual рекомендаций теперь корректно разлагает `net_cost_bps` на execution-cost и funding-carry, если явный `execution_cost_bps` отсутствует. Это устраняет двойной штраф funding в исторической разметке linear grid-сетапов и выравнивает outcome/calibration с economics recommender-слоя.
- Release smoke-tests теперь проверяют поставочный пакет как единый артефакт: README, `.env.example`, `requirements-dev.txt` и операторские `docx/pdf` не должны расходиться между собой.
- Sentiment ingestion дополнительно hardened against poisoned upstream payloads: невалидный `NaN/inf` из внешних источников больше не может тихо превратиться в фиктивный extreme fear/risk-off.
- Global sentiment combine теперь пропускает не только non-finite source rows, но и недиктовые/poisoned payload-блоки вместо падения всего sentiment-цикла.
- Per-symbol blended sentiment игнорирует испорченные source blocks и считает только валидные momentum / reddit / rss / trending компоненты, даже если соседний source вернул строку/список вместо dict.
- Reddit sentiment больше не теряет весь символ из-за одного битого post payload: испорченная запись пропускается локально, валидные соседние посты продолжают участвовать в оценке.
- `collect_sentiment_once()` дополнительно нормализует типы return payload'ов от source-adapter'ов и fail-open переживает неожиданный мусорный ответ отдельного адаптера.
- `DB_PATH` теперь нормализуется к абсолютному пути относительно корня проекта. Перезапуск из другой shell-директории больше не уводит сервис в случайный `./data/app.db`.
- `RUNTIME_LOCK_DB_PATH` по умолчанию разворачивается в sidecar-файл `*.runtime_locks.sqlite`; блокировки лидерства больше не делят файл с основными write-paths.
- Панель «Детали» в UI теперь корректно сбрасывает устаревший `rec_id` после `404` и перестаёт бесконечно запрашивать несуществующую запись.
- История OHLCV валидируется строже: отбрасываются не только `NaN/inf`, но и логически невозможные бары (`high < open/close/low`, `low > open/close/high`).
- При чтении OHLCV используется overfetch перед фильтрацией, поэтому пачка битых последних баров не лишает движок достаточной истории для features / direction / LLM payload.
- Crossed quotes (`ask < bid`) теперь санируются и не превращаются в ложный «нулевой спред». Cost-model в таком случае получает безопасный fallback вместо чрезмерно оптимистичной оценки.
- Outcome-labeling больше не падает на частично испорченном `params_json`: невалидные `grid_spacing_pct/grid_levels` из legacy/manual payload'ов безопасно нормализуются к conservative default вместо аварии цикла разметки.
- `compute_features_from_ohlcv()` теперь сам отбрасывает логически невозможные бары (`close` вне диапазона `[low, high]`) даже при прямом вызове в обход DB-sanitization.
- `liquidity_tier()` больше не трактует poisoned `turnover24h` (`NaN/inf` или отрицательное значение) как реальную ликвидность; такой payload считается `unknown`, а не `micro/high`.
- `trade_plan` и `cost_model` дополнительно санируются от `NaN/Infinity`: один poisoned numeric больше не превращает рекомендацию в несериализуемый JSON и не рисует оператору фиктивные уровни вида `nan/inf`.
- LLM review payload теперь fail-safe переживает malformed legacy/manual recommendation numerics (`score/confidence/expected_rr/risk_score`): мусор в исторической строке не роняет весь reviewer sweep ещё до общей JSON-санации.
- `MASTER_KEY` теперь валидируется на bootstrap как корректный Fernet key. Ошибка конфигурации выявляется при старте сервиса, а не в момент первой live-операции шифрования/дешифрования.
- `RUNTIME_LOCK_DB_PATH` теперь обязан отличаться от `DB_PATH`; accidental конфиг на один и тот же SQLite-файл блокируется fail-fast, чтобы не возвращать runtime lock в тот же файл, где идут основные write-paths.
- `_estimate_cost_model()` теперь fail-safe обрабатывает невалидный `spread` / `funding_rate`: для битого funding payload не начисляется фиктивный carry, а спред откатывается к консервативному fallback вместо загрязнения `net_cost_bps`.
- Идемпотентные execute/stop/trade-paths явнее закрывают SQLite write-транзакции на раннем возврате; повторный запрос больше не держит лишний `BEGIN IMMEDIATE` до закрытия соединения.
- Sentiment API теперь отвергает `NUL` не только в `key`, но и в `tags`, а GET-фильтры `scope/key` перестали тихо превращаться в пустой ответ при poisoned query-string.
- Чтение legacy/manual JSON из SQLite и status/UI helper-слоёв теперь нейтрализует `NaN/Infinity` в `None` вместо того, чтобы позволять poisoned payload тихо менять ветвление бизнес-логики.

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
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
pytest --cov=app --cov-report=term-missing -q
python -m py_compile app/*.py tests/*.py main.py
ruff check app tests main.py
```

Эта проверка сознательно разделяет runtime- и dev-зависимости: prod-установка может ограничиться `requirements.txt`, а релизная/аудиторская проверка использует дополнительный `requirements-dev.txt`.

Текущий проверочный baseline этой ревизии:
- `268 passed`
- `python -m py_compile app/*.py tests/*.py main.py` — passed without errors
- `pytest --cov=app --cov-report=term-missing` — total coverage `81%`
- `requirements-dev.txt` входит в поставку и фиксирует quality-gate (`pytest`, `pytest-cov`, `ruff`) как часть репозитория, а не как неявную зависимость локального окружения
- регрессионные тесты покрывают collector / hot-vs-backfill separation / Bybit client / health semantics / stale-ticker semantics / long-gap kline catch-up / open-interest pagination / runtime lock loss rollback / heartbeat fail-closed / poisoned historical rows / DB validation / metrics endpoint / bounded-parallel collector soak / sentiment feature compression / bootstrap stage commit / batch ticker fallback / future-poisoned ticker and health paths / dedicated heartbeat connection wiring / transactional rollback для execute-trade-stop API paths / atomic recommender publish rollback / duplicate-trade no-op semantics / latest-operator snapshot selection for non-actionable views / execute-idempotency across one publication-chain / idempotent stop retries without duplicate audit events / rollback on silent-false execute-status transition / rollback on failed stop_bot trade finalization / boot-grace honesty for inherited stale rows / malformed sentiment adapter payloads / poisoned Reddit posts / safe fail-open of `collect_sentiment_once()` / malformed legacy JSON-shapes in recommendation-bot-trade-sentiment APIs / malformed app_config payloads in status and metrics / rejection of blank audit keys for `risk limits version` and explicit `trade_id` / persistence of normalized effective risk limits in bootstrap and mutating API / fail-open fallback from poisoned top-level grid range bounds to valid `trade_plan.levels.range` and `trade_plan.levels.kill_switch` / rejection of `NUL` in sentiment tags and GET-filters / explicit transaction cleanup on idempotent execution paths / sanitization of non-finite `trade_plan` and `cost_model` payloads / correct decomposition of legacy `net_cost_bps` into execution-cost plus funding-carry for outcome-labeling.

## Ключевые env
- `DB_PATH` — путь к основной SQLite БД. Если указан относительный путь, он автоматически разворачивается относительно корня проекта;
- `RUNTIME_LOCK_DB_PATH` — путь к отдельной sidecar-БД runtime lock; по умолчанию это `*.runtime_locks.sqlite` рядом с основной БД. Значение обязано отличаться от `DB_PATH`, иначе bootstrap завершится ошибкой конфигурации;
- `SYMBOLS_SPOT`, `SYMBOLS_LINEAR` — списки символов;
- `MIN_SCORE_TO_RECOMMEND`, `MIN_CONF_TO_RECOMMEND` — publish thresholds;
- `FUTURES_COLLECT_INTERVAL_SEC` — интервал обновления funding/open-interest;
- `CALIB_MIN_SAMPLES` — минимум данных для calibration fit;
- `RECO_REPUBLISH_COOLDOWN_SEC` — cooldown для подавления почти идентичных повторных публикаций одной и той же идеи;
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
- `LLM_REVIEWER_KEEP_ALIVE=90s`

Режимы:
- `advisory` — LLM пишет second opinion, но не меняет статус рекомендации;
- `gate` — уверенное расхождение LLM с `execution_direction` может перевести идею в `no_trade`.

Важно:
- LLM-reviewer работает асинхронно и не должен блокировать публикацию core-сигналов. Reviewer применяется к новым `recommended` и к повторно актуальным `active`, но не тратит циклы на `pending`/`suppressed`.
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
2. если сигнал повторился в окне republish-cooldown без material upgrade, он сохраняется как `active` в той же publication-chain: запись остаётся исполнимой для оператора, но её lineage указывает на прежний `publication_root_rec_id`, поэтому outcome/calibration считают только корневую публикацию; если предыдущий bot этой chain уже остановлен, новый `execute` обязан создать новый running-бот, а не вернуть старый stopped-instance;
3. если сигнал для persistence-ботов требует подтверждения ещё одним циклом, он получает статус `pending`;
4. проигравшие альтернативы по тому же `(venue, symbol)` уходят в `suppressed` с явной причиной в `reasons.suppression`;
5. оператор вызывает `/recommendations/{rec_id}/action` с `executed` для `recommended` или `active`;
6. создаётся `bot_instance`, recommendation переводится в `executed`;
7. realized trades/PnL пишутся через `/bots/{bot_id}/trades`;
8. risk engine использует `bot_instances` + `trades` для cooldown и дневного PnL / DD;
9. бот останавливается через `/bots/{bot_id}/stop` или `stop_bot=true` в trade request.

### Семантика статусов recommendation
- `recommended` — новый actionable сигнал, готовый к исполнению;
- `active` — повторно актуальный сигнал внутри cooldown без material upgrade; исполним, но не считается новым выпуском и не создаёт отдельный outcome-root;
- `pending` — кандидат ждёт подтверждения persistence-gate и ещё не исполним;
- `suppressed` — скрытая альтернатива, проигравшая dedupe/selector и сохранённая только для аудита.

## Stability notes
- background loops используют SQLite runtime lock, поэтому активным сборщиком/рекомендером остаётся только один лидер;
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

## Production notes
- используйте внешний process supervisor;
- делайте резервные копии SQLite;
- не храните реальные секреты в `.env` внутри репозитория;
- если нужен полноценный execution layer, его нужно строить отдельно от recommendation engine.


## Runtime lock storage

Runtime leadership locks are stored in a separate SQLite sidecar database. By default the path is derived from `DB_PATH` (for example `app.db` -> `app.runtime_locks.sqlite`). You can override it with `RUNTIME_LOCK_DB_PATH`. This isolates heartbeat writes from long-running market-data transactions in the main database and avoids false leadership loss caused by `database is locked` on the primary DB file.


## Runtime loops

Background work is split into `collector`, `backfill`, `futures_meta`, `sentiment`, `reco` and `llm_reviewer`. During warm-up the backfill loop can sweep the full active symbol set per timeframe, while futures metadata stays on its own cadence so open-interest collection cannot stall readiness progress.
