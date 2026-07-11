# Bybit Recommender — Bybit Linear USDT Futures grid-only build

Сервис собирает рыночные данные Bybit Linear USDT Futures / USDT Perpetual, рассчитывает multi-timeframe признаки и строит рекомендации только для `futures_grid`. Любые другие классы ботов и стратегий в этой сборке не поддерживаются. Дополнительно может подключаться локальный LLM-reviewer по свечам; полный журнал решений и состояний хранится в выбранном backend: SQLite или PostgreSQL.

Проект рассчитан прежде всего на **операторский / полуавтоматический контур**: система формирует интерпретируемую рекомендацию, показывает причины, ограничения и риск-контекст, а оператор уже принимает решение о запуске бота на бирже.

## Поддерживаемые bot_type
- `futures_grid` — только Bybit `category=linear`, USDT perpetual, settlement/margin/PnL в USDT.

Неподдерживаемые стратегии не должны появляться в API, UI, тестах или конфигурациях. Если legacy/manual payload содержит иной `bot_type`, несовместимый `venue` или malformed symbol вроде `BTC/USDT`, он фильтруется/блокируется fail-closed.

## Что делает система
- собирает только `linear` тикеры и OHLCV по USDT perpetual символам; public Bybit client отклоняет нецелевой category/symbol до сетевого запроса, фильтрует exact-symbol responses и не пропускает delivery/pre-market ticker rows в recommendation контур;
- принимает Bybit V5 response как успешный только при присутствующем exact-integer `retCode=0`; отсутствующий, boolean, fractional или иной malformed `retCode` считается retryable response-shape error, а не успешными market data;
- перед REST-запросом проверяет exact-integer `limit` и неотрицательные millisecond `start/end` для kline/open-interest; boolean, fractional, negative и инвертированные временные окна отклоняются без сетевого вызова;
- собирает `funding rate`, `fundingIntervalHour` и `open interest` для perpetual linear;
- ведёт эвристический sentiment pipeline (`global`, `symbol`, `topic` scopes);
- определяет direction/regime на нескольких ТФ;
- считает score / confidence / expected RR / risk score;
- UI `Ранг в выборке` показывает не «точный рейтинг», а grouped percentile: близкие raw-score внутри material delta `0.025` объединяются в near-tie band и получают одинаковый averaged percentile/grade, чтобы 0.245/0.242/0.232 не выглядели как 100/50/0;
- применяет risk-gate, publication-gate, market shock guard и symbol fast-veto;
- при необходимости отправляет кандидат в локальный LLM-reviewer;
- перед operator-confirmation повторно проверяет риск-лимиты, свежесть market-data, актуальный market shock / fast-veto, live-price относительно сохранённого диапазона сетки и базовую исполнимость trade plan относительно metadata инструмента Bybit;
- сохраняет рекомендации, решения, outcome-labeling, calibration state, trade history и risk limits в SQLite или PostgreSQL;
- отдаёт REST API и операторский UI.

## Поведение при перезапуске на существующей БД
При штатном рестарте сервис больше не должен выполнять тяжёлый исторический repair всей таблицы рекомендаций только потому, что в БД накопилось несколько дней данных.

На старте теперь делаются только:
- schema/bootstrap операции;
- дешёвые проверки на наличие legacy-строк без materialized publication lineage;
- точечный backfill только если такие строки действительно найдены.

Глубокий исторический retrofit `repair_async_llm_pending_publication_chains()` оставлен как отдельная maintenance-операция и не запускается автоматически при каждом `python main.py`. Это сделано сознательно, чтобы обычный перезапуск не превращался в долгий full-scan/replay на живой БД.

## Архитектурный принцип
Система разделяет несколько слоёв:

1. **Data layer** — сбор и нормализация рыночных данных.
2. **Inference layer** — признаки, direction aggregation, regime, scoring.
3. **Control layer** — risk gate, shock guard, publication gate, LLM reviewer.
4. **Audit layer** — `recommendations`, `decision_log`, `bot_instances`, `trades`, `reco_outcomes`.
5. **Operator layer** — UI и API для ручного исполнения и анализа.

Это **не execution engine биржевого уровня** и не полноценный симулятор исполнения. Сервис оценивает пригодность сетапа и его качество, но не заменяет отдельный production-grade execution layer. Ордеры на Bybit из этого проекта не отправляются: `bot_instances` и `trades` отражают операторский / audit-контур, а не живой OMS/EMS.

## Что входит в проект
- сбор Bybit Linear USDT Futures тикеров и OHLCV;
- сбор funding и open interest для Bybit USDT perpetual;
- sentiment pipeline с global и symbol scopes;
- multi-timeframe direction/regime inference;
- scoring + risk gating + calibration;
- outcome labeling для проверки качества рекомендаций;
- операторский UI;
- REST API для рекомендаций, risk status, sentiment, bot lifecycle и trade ingestion;
- persistence layer с decision log и outcome history (SQLite или PostgreSQL);
- краткая инструкция оператора в `docs/instrukciya_operatora_bybit_recommender.docx` и `docs/instrukciya_operatora_bybit_recommender.pdf`;
- операторская инфографика `how_to_trade.png` и её текстовый source-of-truth `docs/HOW_TO_TRADE_INFOGRAPHIC.md`.

## Ограничения дизайна
- рекомендации не являются финансовым советом и не гарантируют доходность;
- grid опасен на трендовом рынке: система обязана уметь вернуть `blocked`/`no_trade`, если range-edge слабый;
- leverage увеличивает риск ликвидации; estimated liquidation buffer в UI — консервативный preflight-сигнал: используется худшая дистанция из reference price и adverse range/kill-switch boundary, но это всё ещё не точная формула биржи;
- комиссии, spread, slippage и funding могут полностью уничтожить прибыль на сетку;
- sentiment pipeline остаётся **эвристическим**, а не newsroom/LLM/NER-уровня;
- отсутствие sentiment-данных трактуется как неопределённость, а не как «истинный neutral»;
- grid outcomes остаются приближённой path-approximation, а не биржевой truth-моделью исполнения;
- risk limits начинают полноценно отражать реальность только если в `trades` действительно пишутся realized fills / PnL / fee;
- локальный LLM-reviewer — это **консервативный reviewer поверх движка**, а не замена scoring/risk/calibration;
- проект не предназначен для немедленного запуска на полный объём капитала без staging-прогона.

## Операторский профиль 100-500 USDT

`how_to_trade.png` является быстрым регламентом для малого счёта, но не заменяет backend preflight. Текущая синхронизированная модель:

- проект - recommendation/audit service, а не OMS/EMS: он не выставляет реальные ордера на Bybit;
- поддерживается только `futures_grid` для Bybit Linear USDT Perpetual, `account_mode=unified`, `margin_mode=isolated`, `grid_type=arithmetic`;
- shipped risk profile: 1 running bot на счёт и интервал `min_leverage=3`, `max_leverage=5`; 3-5x является базовым actionable-диапазоном этой ревизии;
- если оператор задаёт `max_leverage < 5`, это трактуется как более строгий risk cap внутри или ниже диапазона 3-5x, а не как обещание, что каждая идея станет исполнимой;
- любой `critical`/`blocking` preflight, `INVALID_MARKET_REFERENCE_PRICE`, устаревшая publication-chain, цена вне range/kill-switch, неподтверждённый funding/minNotional/qtyStep или отсутствие OK LLM-gate при включённом reviewer означает `NO TRADE`.

## Как читать ключевые поля
- `status` — итоговый допуск идеи к рассмотрению.
- `direction` — исполнимое направление для текущего bot_type.
- `confidence` — степень уверенности системы; не читать изолированно от score, RR и risk context.
- `expected_rr` — консервативный экономический смысл идеи после учёта execution friction и только неблагоприятного funding carry; потенциальное получение funding не повышает RR.
- `score` / `reasons.score_components.economic_cost_bps` — ранжирование также штрафует adverse funding carry; signed funding receipt не превращается в положительный edge и не снижает cost-feature.
- `risk_score` — грубая оценка рыночной/исполнительной сложности.
- `reasons.direction_agg` — агрегированное направление и структура голосов по ТФ.
- `reasons.execution_constraints` — что можно, а что нельзя исполнить на выбранном bot_type.
- `bybit_meta` — metadata инструмента Bybit, доступная UI для операторской сверки диапазона, leverage и шагов.
- `params.grid_type/grid_count` — Bybit Futures Grid Bot geometry: `grid_count` означает число price intervals (“Number of Grids”), а текущая генерация и execution-preflight допускают только `grid_type=arithmetic`; `geometric` блокируется до реализации отдельной геометрической математики. Для arithmetic grid опубликованный `params.grid_spacing_pct` теперь соответствует исполнимой геометрии `(price_range_upper - price_range_lower) / grid_count`; минимальный экономический пол хранится отдельно как `economic_min_grid_spacing_pct`, а `grid_geometry_model` явно фиксирует `bybit_arithmetic_range_width_div_grid_count`. `params.economics` / `reasons.grid_economics` — net-of-fees экономика одной сетки: gross/net bps, estimated execution cost, signed funding impact, funding cost used for approval, excluded funding benefit, estimated order notional, margin required и worst-boundary liquidation buffer. Получение funding не улучшает canonical approval-edge, score, expected RR или outcome labels: оно показывается отдельно как signed diagnostic, потому что funding может измениться или стать расходом при накоплении inventory. Минимальный шаг сетки и плотность grid строятся от execution-cost плюс adverse expected funding carry; получение funding не уменьшает spacing, не увеличивает score/RR и не используется как «бесплатный edge». Если net profit per grid не положителен или слишком тонкий, рекомендация блокируется. `reasons.funding.funding_interval_source` показывает, был ли funding interval получен из Bybit ticker/instrument metadata; если `next_funding_ts` недоступен, recommendation и execution-preflight консервативно считают возможные funding events по горизонту, а не предполагают нулевой или single-event carry. Public collector при отсутствии `fundingIntervalHour` в ticker дополнительно берёт interval из instruments-info, а при материальном funding и неизвестном interval рекомендация блокируется fail-closed.
- Временные поля market-data/funding/OI, label horizon и число funding events имеют exact-integer семантику. Значения `5` и `5.0` допустимы как точное целое; boolean, дробные и non-finite значения не усекаются и не округляются. Malformed funding schedule остаётся unknown: при материальном carry рекомендация/execute-preflight блокируется либо используется документированный консервативный unknown-schedule count, но не оптимистический single-event fallback.
- `bybit_plan_validation` — результат execution-time валидации trade plan: ошибки блокируют подтверждение, предупреждения напоминают о неполной проверке qty/min_notional без фактического размера позиции; если `trade_plan.sizing` или `params` уже содержит явный `order_qty`/`qty_per_leg`/`base_qty` либо `order_notional`, эти значения проверяются против Bybit `qty_step`, `min_order_qty`, `max_order_qty` и `min_notional`; для base-qty minNotional проверяется по минимальной цене основного grid range, а не только по reference price, и payload блокируется при существенном расхождении `qty * reference_price` с заявленным `order_notional`. Дополнительно блокируются рекомендации с любым `bot_type` кроме `futures_grid`, любым `venue` кроме `linear`, `reference_price` вне диапазона, внутренним `kill_switch`, схлопыванием сетки после округления по `tick_size`, отсутствующим или неподдерживаемым `margin_mode`, metadata Bybit от другого `symbol` или другого `category/venue`, instrument `status` отличным от `Trading`, несогласованным `grid_count`/`grid_step`, `grid_count > 400`, неподдержанным `grid_type`, off-tick ценами/шагом/`tp_per_leg` в строгом execution-mode, некорректным `leverage` относительно `min/max/leverage_step` Bybit, отсутствующими обязательными Bybit filters (`tickSize`, `qtyStep`, `min/max qty`, `minNotionalValue`, `leverageFilter`), delivery-контрактом вместо perpetual, а также слишком малым worst-side/worst-boundary estimated liquidation buffer при leverage > 1. Legacy/manual payload без `leverage` получает предупреждение и preflight рассматривает его только как 1x; новые рекомендации обязаны хранить явное leverage. Execute-path дополнительно блокирует подтверждение, если текущий ticker уже вышел за сохранённый диапазон сетки или `kill_switch`, либо если свежий ticker не содержит пригодной `last`/`bid`/`ask` live price (`LIVE_PRICE_UNAVAILABLE`), даже при свежих candles/ticker. Для полноценных costed-рекомендаций с `cost_model` execute-preflight также повторно проверяет свежий `funding_rate`, `funding_interval_min` и блокирует запуск, если funding стал stale/недоступен, экстремален или ухудшился настолько, что net edge сетки становится неположительным (`FUNDING_RATE_UNAVAILABLE_AT_EXECUTION`, `STALE_FUNDING_RATE`, `FUNDING_EXTREME_AT_EXECUTION`, `FUNDING_EDGE_TURNED_NEGATIVE`). Metadata инструмента теперь берётся только при точном совпадении `symbol` и сохраняет `result.category`, чтобы preflight не валидировал payload ограничениями чужого или нецелевого инструмента. Auto-snap для сгенерированных operator payload расширяет range/kill-switch наружу по `tick_size` и округляет `grid_step`/`tp_per_leg` вверх, чтобы UI/preflight не показывали более узкую и более прибыльную сетку, чем допускает exchange-aligned geometry. Public Bybit client дополнительно блокирует не-`linear` category и non-USDT symbols до REST-запроса, а ticker collector отбрасывает non-perpetual/pre-market rows и не переименовывает чужой `symbol` в запрошенный.
- `bybit_operator_guard` — строгий operator-facing слой поверх `bybit_plan_validation`: если свежая Bybit metadata недоступна или `require_meta=True` выявляет ошибку exchange constraints, API/UI переводят actionable `recommended`/`pending`/`active` в `blocked`, добавляют причины в `blocks`, меняют `params.risk_report.decision` на `not_recommended` и показывают rejection reasons до попытки исполнения.
- `params.risk_report` — операторский риск-отчёт: итоговое решение, conservative/moderate/aggressive profile, net/grid после издержек, funding impact, execution cost, funding interval, required capital, liquidation buffer, adverse scenario, rejection reasons, warnings и approval factors. UI показывает этот блок явно; при `not_recommended`/blocking reasons запуск запрещён до пересчёта.
- `reasons.llm_review` — second opinion LLM, включая источник (`live`, `cache`, `cache_inherited`, `async_live`, `async_inherited`).

## Документация в репозитории
- `docs/ARCHITECTURE.md` — фактическая архитектура, потоки данных и границы ответственности.
- `docs/MODULES.md` — назначение ключевых модулей и их контракты.
- `docs/TRADING_LOGIC.md` — торгово-логические правила, ограничения и жизненный цикл recommendation/publication-chain.
- `docs/SCENARIOS.md` — ключевые эксплуатационные сценарии и expected behavior.
- `docs/KNOWN_RISKS.md` — оставшиеся риски и осознанные ограничения.
- `CHANGELOG.md` — журнал существенных исправлений этой ревизии.

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
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
PYTHONDONTWRITEBYTECODE=1 python -m py_compile app/*.py main.py
ruff check app tests main.py
```

Эта проверка сознательно разделяет runtime- и dev-зависимости: prod-установка может ограничиться `requirements.txt`, а релизная/аудиторская проверка использует дополнительный `requirements-dev.txt`.

Текущий проверочный baseline этой ревизии:
- full test suite: `409 passed`;
- `PYTHONDONTWRITEBYTECODE=1 python -m py_compile app/*.py main.py` — passed;
- `ruff check app tests main.py` — not run in this container because `ruff` is not installed; install `requirements-dev.txt` first.;
- `pytest --cov=app --cov-report=term-missing` — запускать в release/dev-контуре при изменениях покрытия;
- `requirements-dev.txt` входит в поставку и фиксирует quality-gate (`pytest`, `pytest-cov`, `ruff`) как часть репозитория, а не как неявную зависимость локального окружения
- регрессионные тесты покрывают collector / hot-vs-backfill separation / Bybit client / health semantics / stale-ticker semantics / long-gap kline catch-up / open-interest pagination / runtime lock loss rollback / heartbeat fail-closed / poisoned historical rows / DB validation / metrics endpoint / bounded-parallel collector soak / sentiment feature compression / bootstrap stage commit / batch ticker fallback / future-poisoned ticker and health paths / dedicated heartbeat connection wiring / transactional rollback для execute-trade-stop API paths / atomic recommender publish rollback / duplicate-trade no-op semantics / latest-operator snapshot selection for non-actionable views / execute-idempotency across one publication-chain / idempotent stop retries without duplicate audit events / rollback on silent-false execute-status transition / rollback on failed stop_bot trade finalization / boot-grace honesty for inherited stale rows / malformed sentiment adapter payloads / poisoned Reddit posts / safe fail-open of `collect_sentiment_once()` / malformed legacy JSON-shapes in recommendation-bot-trade-sentiment APIs / malformed app_config payloads in status and metrics / rejection of blank audit keys for `risk limits version` and explicit `trade_id` / persistence of normalized effective risk limits in bootstrap and mutating API / fail-open fallback from poisoned top-level grid range bounds to valid `trade_plan.levels.range` and `trade_plan.levels.kill_switch` / rejection of `NUL` in sentiment tags and GET-filters / explicit transaction cleanup on idempotent execution paths / sanitization of non-finite `trade_plan` and `cost_model` payloads / correct decomposition of legacy `net_cost_bps` into execution-cost plus funding-carry for outcome-labeling / execution-time preflight по свежести market-data / live-price drift относительно диапазона и kill-switch / market shock / fast-veto / базовой Bybit-валидации сетки / adaptive publication-chain collapse under large duplicate bursts / retry of transient Bybit decode- and protocol-level failures / strict grid-only execution preflight for unsupported bot_type, non-linear venue and off-tick prices/steps/TP / exact-symbol funding ticker / execution-time funding carry preflight / malformed symbol and pre-listing metadata hardening / worst-boundary liquidation buffer / tick-safe operator snapping for range, kill-switch, grid step and TP / запрет funding receipt повышать score, expected RR и outcome labels / запрет net-negative TP-touch повышать outcome success / strict generated-grid geometry mismatch preflight.

## Ключевые env
- `DB_ENGINE` — backend persistence: `sqlite` или `postgresql`;
- `DB_PATH` — путь к основной SQLite БД. Если указан относительный путь, он автоматически разворачивается относительно корня проекта;
- `RUNTIME_LOCK_DB_PATH` — путь к отдельной sidecar-БД runtime lock для SQLite; по умолчанию это `*.runtime_locks.sqlite` рядом с основной БД. Значение обязано отличаться от `DB_PATH`, иначе bootstrap завершится ошибкой конфигурации;
- `DATABASE_URL` — обязательный DSN основной PostgreSQL БД в режиме `DB_ENGINE=postgresql`; теперь он должен быть задан явно, чтобы сервис не пытался молча подключаться к локальному `postgresql://127.0.0.1/...` по unsafe-default;
- `RUNTIME_LOCK_DATABASE_URL` — опциональный отдельный DSN для runtime lock в PostgreSQL-режиме; если не задан, используется `DATABASE_URL`;
- `SYMBOLS_LINEAR` — список только USDT perpetual symbols для `venue=linear`; дубли удаляются, а не-USDT symbols fail-closed отфильтровываются на bootstrap, чтобы нецелевой Bybit payload не попал в сбор и scoring;
- `MIN_SCORE_TO_RECOMMEND`, `MIN_CONF_TO_RECOMMEND` — publish thresholds;
- `FUTURES_COLLECT_INTERVAL_SEC` — интервал обновления funding/open-interest;
- `RISK_LIMITS_JSON` — runtime risk caps; `max_concurrent_bots` и `max_symbol_bots` дополнительно clamp-ятся к product cap 50 Futures Grid Bots, даже если оператор передал большее значение; `min_leverage` задаёт минимальное операторское плечо для actionable futures-grid идей, `max_leverage`, `max_position_notional_usdt` и `max_margin_per_bot_usdt` блокируют публикацию/запуск grid-рекомендаций, если расчётный leverage/notional/margin превышает операторский лимит; shipped-профиль использует интервал `min_leverage=3` и `max_leverage=5`: 3x является базовым actionable минимумом, 4-5x выбираются адаптивно только при более сильной экономике/качестве сигнала; значения ниже 3x должны быть осознанным safety-cap, при котором идеи не становятся actionable автоматически;
- `CALIB_MIN_SAMPLES` — минимум данных для calibration fit;
- `RECO_REPUBLISH_COOLDOWN_SEC` — cooldown для подавления почти идентичных повторных публикаций одной и той же идеи; после этого окна same-direction сигнал всё равно не откроет новый outcome-root, пока предыдущая псевдо-сделка той же chain не доживёт до своего horizon или не получит outcome;
- `OUTCOME_HORIZON_FALLBACK_SEC` — fallback horizon для legacy/неизвестных bot_type;
- `ADMIN_API_KEY` — ключ для mutating endpoints; если ключ пуст, mutating API разрешён только с loopback (`127.0.0.1` / `::1` / `localhost`). Для любого удалённо доступного стенда ключ обязателен;
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
- `.env.example` синхронизирован с этими runtime-дефолтами; drift между шаблоном env, README и `settings.py` теперь считается регрессией и проверяется тестами.
- `LLM_REVIEWER_MIN_CONFIDENCE=0.65`
- `LLM_REVIEWER_CADENCE_SEC=300`
- `LLM_REVIEWER_PENDING_TIMEOUT_SEC=900` — максимальное время операторского `pending`: если включён LLM-reviewer, `recommended/active` допустимы только после `llm_review.status=ok`; при таймауте pending переводится в `no_trade` fail-closed
- `LLM_REVIEWER_TTL_SEC=` — отдельный TTL валидности LLM-review для повторного использования по тому же `(venue, symbol, bot_type, direction)`; оставьте пустым для auto-режима: по умолчанию не короче TTL самой рекомендации
- `LLM_REVIEWER_KEEP_ALIVE=90s`

Режимы:
- `advisory` — LLM пишет second opinion; пока нет OK-вердикта, actionable-идея удерживается в `pending`, а после OK возвращается к целевому `recommended/active`;
- `gate` — LLM также удерживает идею в `pending`, а уверенное расхождение с `execution_direction` или timeout переводит идею в `no_trade` fail-closed.

Важно:
- При `LLM_REVIEWER_ENABLED=1` операторский запуск запрещён без `llm_review.status=ok`. Новые и legacy-строки со stored `recommended/active`, но без OK-вердикта, API/UI показывают как effective `pending`. Этот hold ограничен `LLM_REVIEWER_PENDING_TIMEOUT_SEC`: затем рекомендация переводится в `no_trade` fail-closed. Для same-direction reuse используется отдельный TTL валидности reviewer-кэша, поэтому `active` остаётся actionable только при свежем OK-cache/review.
- В shipped-профиле reviewer настроен консервативно для локальных GPU уровня RTX 3060: короткий keep-alive, сниженный parallelism и ограниченное число live-кандидатов на sweep.
- UI и API умеют показывать `pending`, `ok`, `error`, `cache_inherited`, `async_live` и другие состояния reviewer.
- После изменения `LLM_REVIEWER_TFS` или `LLM_REVIEWER_CANDLES_PER_TF` старый кэш reviewer больше не переиспользуется автоматически.

## Основные API
### Read-only
- `GET /api/v1/recommendations`
  - по умолчанию схлопывает repeated rows одной `publication_chain` и возвращает только один operator-facing сигнал на `publication_root_rec_id`; при длинной chain API теперь адаптивно расширяет budget сырой выборки, чтобы `top_n` не схлопывался до 1–2 уникальных идей только из-за доминирующего потока `active` updates. Для raw-аудита можно передать `collapse_chains=false`.
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
  - для `executed` endpoint теперь делает execution-time preflight и может вернуть `409`, если recommendation устарела по market-data, блокируется текущим market shock / fast-veto или её trade plan не проходит базовую Bybit-валидацию.
- `POST /api/v1/bots/{bot_id}/trades`
- `POST /api/v1/bots/{bot_id}/stop`
- `POST /api/v1/risk/limits`
- `POST /api/v1/sentiment`

## Жизненный цикл исполнения
1. recommendation публикуется со статусом `recommended`, если это новый actionable выпуск;
2. если same-direction сигнал пришёл, пока предыдущая корневая идея по этому `(venue, symbol, bot_type, direction)` ещё находится внутри своего outcome-horizon, новый `publication_root` не создаётся даже при material-upgrade: запись принудительно сохраняется как `active` в существующей publication-chain, чтобы outcome-labeling имитировал одну открытую псевдо-сделку, а не серию повторных входов;
3. если сигнал повторился уже после закрытия псевдо-сделки, но внутри republish-cooldown и без material upgrade, он тоже сохраняется как `active` в той же publication-chain: запись остаётся исполнимой для оператора, но её lineage указывает на прежний `publication_root_rec_id`, поэтому outcome/calibration считают только корневую публикацию; если предыдущий bot этой chain уже остановлен, новый `execute` обязан создать новый running-бот, а не вернуть старый stopped-instance;
4. если включён `LLM_REVIEWER_ENABLED=1`, actionable grid-рекомендация получает `pending` до `llm_review.status=ok`; без OK-вердикта `recommended/active` в UI/API не показываются как запускаемые, а hold ограничен `LLM_REVIEWER_PENDING_TIMEOUT_SEC`;
5. проигравшие альтернативы по тому же `(venue, symbol)` уходят в `suppressed` с явной причиной в `reasons.suppression`;
6. оператор вызывает `/recommendations/{rec_id}/action` с `executed` для `recommended` или `active`;
7. перед созданием `bot_instance` сервис повторно проверяет текущие риск-лимиты, свежесть candles/ticker, live-price относительно диапазона/kill-switch, актуальный market shock / fast-veto и базовую Bybit-валидность сетки; instrument metadata Bybit подгружается заранее, вне SQLite write-lock, чтобы медленный upstream не блокировал collector/recommender; при ошибке возвращается `409`, а в `decision_log` пишется `EXECUTION_BLOCKED` или `EXECUTION_PRECHECK_BLOCKED`;
8. если preflight пройден, создаётся `bot_instance`, recommendation переводится в `executed`;
9. realized trades/PnL пишутся через `/bots/{bot_id}/trades`;
10. risk engine использует `bot_instances` + `trades` для cooldown и дневного PnL / DD;
11. бот останавливается через `/bots/{bot_id}/stop` или `stop_bot=true` в trade request.

### Семантика статусов recommendation
- `recommended` — новый actionable сигнал, готовый к исполнению;
- `active` — повторно актуальный signal-update внутри уже открытой publication-chain; возникает либо при обычном cooldown-reuse, либо при жёстком same-direction pseudo-position lock до завершения horizon. Исполним, но не считается новым выпуском и не создаёт отдельный outcome-root;
- `pending` — временный gate-hold перед исполнением; не является `no_trade`, но не исполним до финального `recommended`/`active` либо fail-closed отказа;
- `suppressed` — скрытая альтернатива, проигравшая dedupe/selector и сохранённая только для аудита.

### Инварианты исполнения publication-chain
- в одной `publication_chain` допускается не более одного `running` bot_instance одновременно; этот инвариант теперь удерживается не только логикой API, но и индексом/проверкой на уровне БД;
- в PostgreSQL mutating API дополнительно берут `FOR UPDATE` на целевую recommendation/bot row; это снижает риск потерянного `state_json` при одновременных `trade`/`stop` запросах и гонок статуса между `executed`/`ignored`;
- при гонке двух `execute` для разных членов одной chain второй запрос должен идемпотентно переиспользовать уже созданный running-бот, а не создавать дублирующую позицию;
- если в существующей БД уже обнаружены два `running` bot_instance для одной chain, bootstrap завершится fail-closed с явной ошибкой конфигурационной/исторической целостности.

## Stability notes
- background loops используют runtime lock в выбранном backend; в SQLite это отдельная sidecar-БД, в PostgreSQL — тот же DSN либо отдельный `RUNTIME_LOCK_DATABASE_URL`. Для PostgreSQL захват лидерства теперь выполняется одной atomic UPSERT-операцией, а не парой `SELECT`→`UPDATE`, чтобы исключить split-brain при одновременном старте двух инстансов; operator execute-path больше не держит этот же write-контур на внешнем Bybit fetch, что снижает риск каскадных `database is locked` при деградации сети;
- публичный Bybit REST-клиент ретраит не только обычные timeout/network ошибки и HTTP 429/5xx, но и transient transport/protocol сбои уровня `RemoteProtocolError`, `408` и битые 2xx-ответы с невалидным JSON, которые периодически встречаются за CDN/WAF;
- background loops завершаются по lifespan stop-event и не должны переживать штатный stop/restart процесса как «ложно упавшие» daemon-потоки;
- collector работает с явными stage-boundary commit, а не с одной гигантской write-транзакцией через весь цикл: это осознанный компромисс ради корректного heartbeat и отсутствия скрытого split-brain;
- в SQLite включён `WAL` и увеличенный `busy_timeout`; для PostgreSQL проект использует `psycopg` и совместимый migration bootstrap;
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
- нет ли в деталях `bybit_plan_validation.errors` или предупреждений о том, что диапазон/шаг сетки не выровнен по ограничениям Bybit;
- не деградирует ли quality score / confidence после накопления новых outcome labels;
- корректно ли отрабатывают risk limits после записи реальных trade rows.

## Инженерные заметки
- используйте внешний process supervisor;
- делайте резервные копии используемого backend: SQLite-файлов или PostgreSQL БД;
- не храните реальные секреты в `.env` внутри репозитория;
- если нужен полноценный execution layer, его нужно строить отдельно от recommendation engine.


## Troubleshooting PostgreSQL bootstrap
- Ошибка `DATABASE_URL is required when DB_ENGINE=postgresql` означает, что выбран PostgreSQL-режим без явного DSN. Задайте `DATABASE_URL=postgresql://...` в `.env`.
- Ошибка `PostgreSQL mode requires installed package 'psycopg[binary]'` означает, что окружение собрано без runtime-зависимостей PostgreSQL. Исправление: `pip install -r requirements.txt`. Если PostgreSQL не нужен, переключите `DB_ENGINE=sqlite`.
