## Observation evidence grading - v1.5.1

The market-trade journal now separates transport coverage from replay authority. REST overlap spans are diagnostic/bootstrap artifacts. Only session-isolated WebSocket spans are eligible for exact grid replay. The outcome layer records ignored non-exact coverage IDs and detailed WebSocket/OHLC deltas without changing strategy generation, risk gates or persistence schema.

## Data-efficient dual-strategy architecture - v1.5.0

Persistence разделён на два слоя:

1. `recommendation_latest` — mutable operator snapshot с одной строкой на `(venue, symbol, bot_type)`; здесь одновременно присутствуют и `futures_grid`, и `directional_trend`.
2. `recommendations` — immutable material-event ledger: первые состояния, существенные transitions, actionable/pending publications и outcome roots. Exact rec identity и operator lifecycle сохраняются.

Latest API читает mutable snapshot и накладывает более поздние audited LLM/operator mutations для того же `rec_id`. Historical API продолжает читать immutable ledger. Existing DB upgrade additive/idempotent для SQLite и PostgreSQL.

Market-data write path:

- OHLCV UPSERT обновляет conflict-row только при реальном изменении OHLCV;
- steady-state derived TF читает два последних target buckets, а cold bootstrap bounded до 96 buckets;
- backfill отделён от 20-секундного hot collector;
- ticker/funding forecast bucket сохраняет первый фактический exchange timestamp и обновляет значения внутри bucket; settled funding остаётся отдельной immutable history;
- WebSocket/REST public trades подписываются только на symbols с открытым waiting `futures_grid` outcome root. `directional_trend` не запускает raw-trade capture.

Retention разделяет high-volume refresh data и scarce evidence. Non-root audit refreshes имеют короткое окно; outcomes/observability и связанные roots сохраняются дольше, особенно для current/exact policy lineage.

## Trade-ingest concurrency and restart-handover architecture - v1.4.13

PostgreSQL market-trade writers share `pg_advisory_xact_lock(4259842013)`. The lock is acquired before any `market_trade` or `market_trade_coverage` access and is released automatically by commit/rollback. REST fallback commits per symbol, preventing an advisory lock from being held across HTTP calls. Hourly retention runs in a separate transaction after ordinary table pruning.

At FastAPI lifespan startup, local runtime-lock owners are parsed as `hostname:pid`. A lease is reclaimed only when the hostname equals the current machine and OS-level PID inspection proves the process is no longer active. Remote hosts, malformed owners, access-denied checks and uncertain states remain untouched.

## Heartbeat and transaction-isolation architecture - v1.4.12

`app/trade_stream.py` отключает RFC-level auto-ping библиотеки и использует единственный source of truth для liveness: Bybit JSON heartbeat плюс receive watchdog. Normal disconnects возвращаются в `_market_trade_stream_thread()`, который закрывает session coverage и применяет bounded reconnect backoff.

`app/db.py::record_market_trade_poll()` является atomic per-symbol unit внутри caller transaction. Функция открывает savepoint, пишет trade rows и coverage, затем release; при исключении выполняет rollback-to-savepoint/release и re-raise. Collector может безопасно продолжить и записать `COLLECT_ERROR` на той же PostgreSQL connection. DB schema не изменяется.

## Stream transport and fallback architecture - v1.4.11

`market_trade_stream` теперь владеет reconnect lifecycle внутри supervised worker. Expected `ConnectionClosed`/transport timeout закрывает session coverage, сохраняет session stats и запускает новую connection после bounded backoff. Только malformed payload, DB/invariant error или иная неожиданная ошибка выходит в outer supervisor как crash.

Network receive и persistence разделены bounded buffering semantics: `websockets` queue допускает краткий consumer lag, а DB commits объединяют до 32 сообщений или 0.5 секунды. Protocol Ping/Pong дополняется Bybit JSON heartbeat `{"op":"ping"}`.

Модуль хранит process-local runtime state `active/session_id/last_message/disconnect_reason`. Hot collector использует его как переключатель источника: WebSocket active → REST trade polling disabled; stream inactive/disabled → REST fallback enabled. Ticker, funding и OHLCV collection не зависят от trade-journal transaction load.

Warm-up decision logging является state machine: `not ready` transition создаёт один compact `RECO_WARMUP_SKIP`; material readiness signature change может создать новый event после cooldown; переход в ready создаёт `RECO_WARMUP_RECOVERED`. Health status остаётся полным source of truth.

## WebSocket coverage boundary persistence - v1.4.10

`record_market_trade_stream_batch()` сохраняет trades и per-symbol session coverage в одной транзакции. Для первого сообщения start остаётся exclusive-границей `oldest_trade_ts_ms + 1`. Если она на 1 ms позже envelope timestamp, end поднимается до start и образует zero-width open span без ложного покрытия первого millisecond. Последующие сообщения расширяют только `coverage_end_ms` через monotonic max, а disconnect закрывает span без bridging следующей session.

Изменение локально для persistence boundary. Таблицы, индексы, API, background topology и dual SQLite/PostgreSQL contract не изменены.

## WebSocket delivery-order persistence - v1.4.9

`app/trade_stream.py` назначает monotonically increasing message index внутри connection session и сохраняет исходный row index каждого `publicTrade` payload. `app/db.py` materializes эти значения в `market_trade`; existing SQLite/PostgreSQL schemas получают nullable columns через idempotent runtime upgrade до выполнения индекса нового порядка.

Coverage details содержат `session_id`, `last_message_index` и `ordering_basis=websocket_delivery_order_v1`. При path lookup WebSocket span имеет приоритет перед REST span, а trades фильтруются по exact session. Delivery index является локальным source of truth для порядка сообщений; exchange timestamps остаются event-time evidence и не используются как недокументированная межсообщенческая sequence guarantee.

## Outcome data recovery architecture - v1.4.8

- `collector.py` ведёт process-local funding refresh state с отдельными `last_success`, failure count и `next_retry`; durable repair state хранится в БД.
- `funding_settlement_repair` является идемпотентной адресной очередью missing settlement ranges. Outcome worker создаёт request, collector исполняет, а последующий outcome cycle завершает label.
- `market_trade` хранит дедуплицированные public trades; `market_trade_coverage` хранит доказанные непрерывные spans и явные gaps. `app/trade_stream.py` ведёт основной read-only `publicTrade.{symbol}` WebSocket: каждая connection/session создаёт отдельный span и никогда не мостит disconnect. REST recent-trade создаёт отдельные fallback spans и расширяет их только при trade-ID overlap.
- `outcomes.py` использует trade path как дополнительный observation source, не как новую strategy/model lineage. OHLC mismatch или неполное coverage блокируют replay.
- `/api/v1/status` публикует repair queue, WebSocket/fallback transport и journal health. UI показывает их в единой readiness table и advanced diagnostics.
- Таблицы добавлены одинаково в runtime bootstrap, SQLite init SQL и PostgreSQL init SQL; migration additive/idempotent.

## Direction representation and operator observability - v1.4.7

`app/direction.py` является source of truth для raw multi-timeframe vote и теперь работает в `log_price_v1`. Log levels обеспечивают математическую антисимметрию зеркальных return paths; score thresholds, risk gates и canonical payoff остаются вне этого изменения. Поскольку feature meaning изменился, recommender, binary calibrators и trend first-touch model используют новые immutable lineages.

`app/db.py` расширяет outcome projection двумя аддитивными контрактами. `sample_observability` вычисляется на корневых observation windows и передаётся на summary/cohort/group levels. `by_bot_cohort` разделяет main aggregation по mutually exclusive eligibility cohort; legacy `by_bot` сохранён как audit/backward-compatible projection.

Frontend остаётся server-rendered static JS без framework. `app/ui/static/app.js` использует одну каноническую cohort-aware Results table и отдельный master-detail renderer для decision journal. `styles.css` задаёт responsive card grid; full details остаются в native `<details>`, а modal wide/height/keyboard lifecycle не меняется. Backend payload не humanize-ится повторно и HTML escaping применяется к каждому operator-controlled leaf.

Схема SQLite/PostgreSQL не изменена. Изменения не добавляют private Bybit endpoints, OMS/EMS или order submission.

## Exchange sizing boundary - v1.4.6

`app/main.py` now separates immutable operator intent from provisional generator output. Exchange normalization may never enlarge explicit/manual sizing. For generated provisional plans it may materialize the minimum executable quantity, marks the transformation as risk-increasing and immediately routes the normalized payload through the existing full-grid runtime risk boundary. `app/outcomes.py` treats an exit candle as partially observable: only the gap open or terminal trigger is available after exit; full OHLC extrema are accepted only for non-terminal candles.

## Direction-aware learning boundary - v1.4.5

The calibration boundary now treats recommendation direction as part of the immutable feature contract. `app/recommender.py` persists `direction_sign` and `sentiment_alignment`; `app/calibration.py` validates and extracts them; `app/trend_events.py` reuses the same 15-feature schema. Model storage keys and recommender identities were bumped so 13-feature coefficients cannot load under the new contract.

The broader architecture remains hybrid rather than end-to-end learned: deterministic market features and scoring create candidates; learned components calibrate confidence and first-touch probabilities; monetary/temporal gates and the profitability router decide admissibility. No DB schema, private execution path or risk boundary changed.

## Shared label-maturity source of truth — v1.4.4

`app/policy.py` владеет `CALIBRATION_LABEL_GRACE_SEC` и `policy_label_due_ts`. `app/recommender.py` использует helper при materialization policy contract и при fit-lineage validation; `app/outcomes.py` использует его перед сохранением outcome; `app/db.py` применяет bounded startup repair и передаёт maturity fields в compact status iterator. Это устраняет прежний split-brain между JSON policy, worker schedule и model-readiness observability.

Startup repair не меняет immutable outcome target. Он выбирает только строки, чья availability раньше default due, затем проверяет точный persisted policy contract и зрелость, после чего сдвигает metadata timestamp вперёд. Malformed или неоднозначные строки остаются исключёнными.

## Operator observability composition — v1.4.3

Frontend observability теперь имеет два уровня:

- primary decision layer: summary cards, one operator-status table, one readiness/evidence table, one canonical strategy table;
- audit layer: `<details class="modal-disclosure">` с причиной допуска, LLM matrix, symbol breakdown, full current rows, archive, runtime/collector/backfill и DB semantic details.

`renderModalDisclosure()` является общим renderer для audit layer. `closeAllDialogs()` централизует Escape/backdrop/close-button lifecycle и закрывает все элементы `.modal`, а не один hard-coded dialog. Wide layout применяется только через `configureModalLayout({wide:true})` и ограничен 1600 px / 88vh / 900 px.

Data API и persistence contract не изменены. Компоновка не удаляет audit fields и не пересчитывает strategy statistics на frontend.

## Rejected trend evaluation boundary — v1.4.2

`directional_trend` is now a formed strategy only when direction is LONG or SHORT. The durable discriminator is `recommendations.candidate_kind`:

- `strategy_recommendation` — a strategy-native candidate that may own geometry, an outcome root, history and an execution audit lifecycle;
- `trend_evaluation_rejected` — a preliminary trend assessment with no position semantics.

The rejected branch is terminal and fail-closed: no trade plan, no TP/SL, no `is_outcome_label_root`, no `reco_outcome_observability`, no position-history point, no training eligibility and no router/execution participation. It carries only `TREND_DIRECTION_UNCONFIRMED` plus bounded diagnostics stored outside the operator-block list. `strategy-profitability-router-v3` rejects every candidate whose kind is not `strategy_recommendation`.

At database bootstrap, legacy `directional_trend/neutral` rows are classified as rejected, their eligibility/root flags are cleared and waiting schedules without a terminal outcome are removed. Existing immutable outcomes are retained for audit, while health reports `rejected_trend_outcome_total` as semantic-integrity failure.

## Strategy-native operator projection — v1.4.1

`bot_type` is the discriminator for every operator-facing direction label and remediation path. `neutral` is not a global product name: under `futures_grid` it is a valid neutral inventory geometry; under `directional_trend` it means the mandatory LONG/SHORT direction was not established and the candidate is structurally invalid. API rows remain independent; frontend rendering must never infer strategy from `direction` alone.

The remediation layer follows the same boundary. Grid guards may produce range/grid actions; trend guards may produce only single-position direction/entry/TP/SL/first-touch actions. Legacy rows without `bot_type` are interpreted as `futures_grid`, matching the historical schema contract. Concrete blocker codes are deduplicated across stored and live-validation sources without merging different strategies or generic warnings.

## Strategy observability data flow — v1.4.0

`recommendation persistence → observability schedule → outcome worker → canonical event → enriched API → Details/History/Outcomes/Health` is one auditable chain. The persistence transaction immediately records the due timestamp for each supported strategy root. Read APIs join by `outcome_root_rec_id`, batch tracking for list views, and preserve `bot_type` in journal and aggregate projections. Health verifies cross-table identity and event-type invariants.

Historical price geometry is read solely from immutable `params_json`. Current Bybit metadata may validate the latest recommendation but cannot rewrite older points. Frontend charts are strategy-specific and fail visibly; missing geometry starts a new SVG segment.

## Trend first-touch event architecture — v1.3.0

Data flow для `directional_trend`:

1. `app/outcomes.py` создаёт immutable `event_type` и net-return diagnostics из непрерывного 1m path.
2. `app/db.py` сохраняет `reco_outcomes.event_type`; SQLite bootstrap и PostgreSQL reference schema используют additive default `LEGACY_BINARY` для старых строк.
3. `app/trend_events.py` формирует exact-policy трёхклассовую выборку, выполняет whole-timestamp chronological terminal holdout, purging по exact label availability и softmax fit только на pre-holdout train.
4. Модель сохраняется в существующем calibrator store под отдельным bot/policy key и не смешивается с grid LogReg.
5. `app/recommender.py` строит plan-specific `trend_event_assessment` с вероятностями, payoffs и консервативной EV.
6. `app/strategy_router.py` сравнивает grid и trend только после strategy-specific evidence gates; непроверенный trend исключается fail-closed.
7. API status и frontend показывают readiness, class probabilities и first-touch EV отдельно от heuristic confidence.

Версии контракта: `directional_trend_v2`, `directional_trend_label_v2`, `logreg_directional_trend_v3`, `trend-first-touch-softmax-v2`, `strategy-profitability-router-v3`. Изменение label/contract исключает legacy v1 trend outcomes из нового fit без удаления исторического аудита.

## Strategy profitability router - v1.2.0

`app/strategy_router.py` is the strategy-family selection boundary. The recommender constructs independent grid and trend candidates from one feature snapshot, annotates each with its bot-specific calibration evidence and calls the router before persistence/publication selection. The router never reads raw strategy score. It returns a selected winner, `no_eligible_strategy`, or `no_clear_winner`; non-winners remain paired outcome samples.

Execution remains recommendation/audit-only. Grid and trend use distinct plan validators and audit instance kinds. Trend materialization stores an external single-order package but no private order client or endpoint is present. One-way symbol locking prevents simultaneous grid/trend instances on the same symbol. SQLite/PostgreSQL schema is unchanged.

## Historical v1.1.0 architecture: separate shadow strategy family

The recommendation loop now emits two mechanics-specific candidates per supported Linear USDT symbol:

1. `futures_grid`: existing arithmetic grid, range/mean-reversion gates, neutral/long/short inventory bias and existing operator lifecycle.
2. `directional_trend`: research-only single-position long/short policy with independent score, plan, outcome and calibration lineage.

In v1.1.0 the separation was structural, not cosmetic. `GRID_BOT_TYPES` contains only `futures_grid`; `DIRECTIONAL_BOT_TYPES` and `SHADOW_ONLY_BOT_TYPES` contain `directional_trend`. The trend plan has no grid levels or replacement-order topology. The execution boundary rejects it before Bybit metadata can make it appear launchable. The outcome dispatcher routes it to a separate exact-1m TP/SL label and the calibrator registry uses `logreg_directional_trend_v1`.

No schema migration is required: existing `bot_type`, JSON contract and outcome tables already represent the new family. Publication/outcome lineage remains partitioned by `bot_type`, so grid and trend roots cannot share an independent outcome window. SQLite and PostgreSQL paths remain supported.

## Dual lineage: operator publication и statistical outcome - v1.0.78

Recommendation persistence materializes two orthogonal roots. `publication_root_rec_id` governs operator freshness, list collapse, execution idempotency and one-running-bot constraints. `outcome_root_rec_id` governs statistical label identity; only `is_outcome_label_root=true` rows are independently labeled.

Data flow: candidate -> locate open same-direction outcome root by label horizon -> locate live operator publication by TTL -> either reuse both roots, create a fresh publication root sharing the old outcome root, or create both new roots after maturity. SQLite/PostgreSQL bootstrap adds and backfills the outcome lineage before creating its index, so an existing database upgrades additively.

The canonical `futures_grid` horizon remains 12 hours. It is part of the versioned label target and calibration embargo, not an operator freshness value.

## Outcome eligibility read model - v1.0.77

Outcome scope and calibration eligibility are orthogonal. `current_policy`
filters by current model plus verified canonical policy fingerprint. Each retained
root is then assigned to exactly one eligibility cohort from immutable
`feature_snapshot` and `outcome_policy`: calibration eligible, policy-evaluation
candidate, shadow exploration, outcome-only, other policy or excluded. API gate
diagnostics never mutate the original recommendation.

The hot evidence store has two bounded lanes: ordinary outcome evidence for 14
days and materialized policy-evaluation roots for 90 days. The longer selector is
intentionally conservative; training still verifies contract hash, active
fingerprint, score/MR floors and label maturity.

## Outcome audit data flow - v1.0.76

Завершённый proxy outcome проходит единый доказуемый путь: `_grid_outcome` формирует `(success, net_proxy_return)` и terminal diagnostics -> `compute_outcomes_cycle` передаёт их в `db.insert_outcome` -> существующий `reco_outcome_observability.details_json` сохраняет diagnostics атомарно с outcome lifecycle -> `get_outcomes_recent_enriched` присоединяет observability -> frontend показывает отдельные поля исхода, P&L и причины. Схема SQLite/PostgreSQL не изменена; используется уже существующий additive observability-контракт.

## Terminal selected-policy activation boundary - v1.0.75

`app/calibration.py` повторно применяет shared confidence transform к финальному whole-timestamp fold и строит для выбранных terminal rows отдельные row/temporal monetary diagnostics. Persistence сохраняет этот блок отдельно от aggregate `selected_policy`; loader не принимает fitted payload без положительного terminal contract. `app/recommender.py` использует model только когда оба monetary слоя положительны, а `app/main.py` и UI публикуют фактические/требуемые terminal-selected counts.

Поток: exact-policy outcomes -> purged folds -> terminal candidate -> binary skill -> aggregate selected money -> terminal selected money -> strict cache -> publication confidence gate. Любое неизвестное или неположительное terminal evidence разрывает поток до inference; deterministic risk/economics gates и recommendation/audit-only boundary не меняются.

## Calibration activation boundary - v1.0.74

`fit_logreg()` теперь имеет две независимые OOF границы допуска. Первая сравнивает feature model со score-only/null по aggregate и terminal log-loss. Вторая применяет общий runtime confidence transform к тем же purged OOF predictions и рассчитывает monetary diagnostics только для строк, которые реально прошли бы publication threshold. Активный pipeline — candidate, обученный строго до terminal block; metadata `n_samples` соответствует его train prefix.

`_chronological_validation_blocks()` формирует blocks на границах одинаковых timestamps и резервирует terminal block не менее 80 строк/5 timestamps при default contract. Persistence хранит обе границы; loader отклоняет fitted payload без положительного selected-policy status, минимального terminal block или полных lower bounds. Model/policy/calibrator identity bump не требует DB migration: старые rows остаются immutable archive и не смешиваются с v2 evidence.

## LLM reviewer shutdown lifecycle - v1.0.73

`_llm_reviewer_thread()` использует тот же общий `_BACKGROUND_STOP_EVENT`, что collector, backfill, futures metadata, sentiment, recommender и outcomes. После сигнала shutdown новый reviewer cycle не начинается; target возвращается в `_run_supervised_background_target()`, который сохраняет clean `stopped` state и в `finally` вызывает owner-safe release для `runtime:llm_reviewer`.

Runtime-lock остаётся single-owner lease. Исправление не удаляет чужой lock, не ослабляет atomic acquisition SQLite/PostgreSQL и не меняет LLM review/pending contract. При аварийном kill восстановление по-прежнему происходит только после TTL.

## Bounded operator diagnostics — v1.0.72

Тяжёлые operator reads разделены по назначению. Историческая lineage-сводка строится агрегатами SQL; JSON-контракты проверяются только для outcomes текущей модели. Полное окно исходов получает детальные матрицы текущей policy-когорты, а архив — отдельный summary endpoint с ограниченным recent-list. Это сохраняет immutable audit history, но исключает O(N) JSON-декодирование всего архива при каждом клике.

## Runtime lock handover и shadow exploration - v1.0.71

Supervised wrapper владеет единым lifecycle release для блокировок фоновых компонентов. `release_runtime_lock` удаляет строку только при совпадении owner, поэтому завершившийся процесс не может удалить lease нового лидера. При аварийном завершении сохраняется TTL takeover. Status API читает lock-row только как диагностику и не использует read-before-write для claim; PostgreSQL atomic UPSERT остаётся source of truth.

Runtime provenance теперь содержит collector lock owner/heartbeat/TTL/takeover, а collector state различает `handover`, `starting`, `ok`, `stalled`, `error`. Handover grace отделён от symbol freshness: диагностическая передача может быть штатной, но stale symbols не маскируются как торгово свежие. Outcome SQL и liveness используют одинаковый риск-чистый shadow contract; `policy_evaluation_eligible` остаётся downstream calibration gate, а не prerequisite для исследовательской разметки.

## Restart provenance and persistence continuity — v1.0.69

`/api/v1/status` различает heartbeat supervised thread и завершённый цикл именно текущего процесса. `runtime_provenance.current_process_ready` требует одновременно собственного collector cycle и собственной recommendation publication. Старые persisted cycle metrics допустимы только во время boot grace. `database_continuity.database_instance_id` хранится в `app_config` и не содержит путь, DSN или credentials.

## Operator readiness observability — v1.0.68

`GET /api/v1/status` теперь объединяет четыре независимых слоя наблюдаемости:

1. `database_schema` — наличие materialized outcome/LLM колонок и число legacy-строк, ещё ожидающих backfill;
2. `background_threads` и `outcome_worker` — фактическая жизнеспособность supervised runtime;
3. `recommendation_readiness` — bounded-агрегация последней публикации, включая actionable/status counts и ранжированные `no_trade`/`blocked` причины;
4. `operator_readiness` — производное состояние `ready`, `healthy_not_actionable`, `starting` или `degraded`, которое не смешивает техническую готовность с наличием торгового кандидата.

Frontend health flow параллельно получает `/api/v1/health/symbols`, `/api/v1/status` и `/api/v1/decisions?limit=200`. Отрисовка выполняется только после проверки обоих обязательных HTTP-ответов. Экспорт формируется в браузере из уже полученных payload; чтения файлов сервера, `.env` или credential storage нет.

Latest-publication aggregation ограничена 1000 корневыми рекомендациями и не сканирует исторический outcome backlog. Это сохраняет bounded request cost и не возвращает тяжёлую проверку очереди в HTTP hot path.

## Outcome maintenance runtime — v1.0.67

Outcome/calibration maintenance является самостоятельным фоновым контуром. `outcomes` запускается через общий supervisor, получает собственную атомарную runtime-блокировку `runtime:outcomes` и не выполняется внутри рекомендательного цикла. Потеря блокировки проверяется heartbeat перед циклом, по строкам и после цикла; ошибка сохраняется в `app_config[outcome_worker_cycle]` и приводит к безопасному рестарту supervisor.

`outcome_worker_cycle` — устойчивый runtime contract со состояниями `running`, `completed`, `error` и показателями прогресса. Liveness сопоставляет его с фактическим SQL-агрегатом созревшей очереди и публикует операторские состояния `ok`, `processing`, `backlog`, `stalled`, `error`. SQL читает агрегаты и до десяти идентификаторов, а не полный JSON-набор. Runtime snapshot хранится в существующем `app_config`. Таблица `recommendations` получает шесть индексируемых materialized-полей outcome policy/LLM. Новые публикации заполняют их на persistence boundary, LLM-review синхронизирует их с `reasons_json`, а legacy-строки проходят одноразовый bounded keyset backfill при обнаружении NULL.

## Bounded calibration evidence pipeline (v1.0.66)

Крупные read-only выборки проходят через `app.db_backend.execute_stream()`. Для SQLite используется естественный ленивый cursor; для PostgreSQL `PostgresConnection.execute_stream()` создаёт именованный server-side cursor и задаёт `itersize`. Consumers обязаны читать результат через `fetchmany()` и закрывать cursor в `finally`.

`app.recommender._CalibrationEvidenceContext` существует только в пределах одного `run_recommender_once()`: memoize-ит observability по scope и лениво загружает один compact exact-policy outcome dataset. После разрешения global/bot/direction calibrators набор явно освобождается. `app.main.api_status()` использует streaming lineage rows и агрегирует счётчики без materialized history. Транзакционная, policy и fail-closed семантика не менялась.

## Ограниченный restart recovery market-data (v1.0.65)

Поток минутных данных разделён на две независимые обязанности:

1. `collector` проверяет текущий тикер и при длинном разрыве получает только свежий хвост из 360 минутных свечей. После committed upsert он сохраняет задание `collector_gap_backfill:<venue>:<symbol>:60` в `app_config`.
2. `backfill` читает устойчивый курсор `next_start_ts/target_end_ts`, получает одну страницу не более 360 свечей, атомарно пишет её и только затем продвигает курсор. При перезапуске процесс продолжает с последней подтверждённой границы.
3. REST executor поддерживает не более `max_workers` одновременно отправленных futures. Завершённый результат отдаётся потребителю до отправки следующей задачи, поэтому память ограничена числом workers и размером одной страницы.

Существующий ключ OHLCV `(venue, symbol, tf_sec, ts)`, SQLite/PostgreSQL dialect и fail-closed recommendation warm-up не изменены.

## Слой локализации операторского UI (v1.0.64)

Машинные контракты API, БД и внутренние коды остаются стабильными. Frontend содержит явные функции преобразования статусов, направлений, режимов, ролей выборки, временных интервалов и диагностического текста в русские операторские формулировки. Backend также формирует русские безопасные действия оператора. Неизвестные значения показываются как «не определено» или общая безопасная причина, а исходный код сохраняется в подробной диагностике. Локализация не участвует в расчётах, risk gate, publication lifecycle или outcome labeling.

## v1.0.63 operator-summary presentation boundary

`app/main.py` converts the selected primary gate code into a bounded operator hint while retaining the original diagnostic detail. `app/ui/static/app.js` renders that hint only as the title/accessible label of the final decision badge. The table does not parse or display raw reason payloads. Full diagnostics continue through the existing Details contract.

## v1.0.62 runtime liveness and operator-summary flow

`app/outcomes.py` selects actionable LLM-ready roots plus explicit safe shadow roots. `app/db.py` owns the shared LLM-outcome eligibility predicate and the read-only outcome-worker liveness calculation. `app/main.py` exposes the liveness payload and an additive `operator_summary`; the frontend renders only the six-field decision table and keeps full diagnostics in Details. Collector retries use Bybit reset timing and confirm instrument absence before temporary disablement.

## v1.0.61 operator-metrics data flow

`app/recommender.py` keeps legacy `_expected_rr()` as a compatibility/internal heuristic, then builds two separate immutable publication diagnostics. `_plan_rr_metrics()` consumes generated `params.economics`, full `cross_margin_stress` and the cost model. `_empirical_expectancy_metrics()` consumes the fitted exact-policy calibrator diagnostics and never reads plan geometry. Both are stored under `reasons.operator_metrics`; plan/empirical summaries are also copied into `params.operator_metrics` and `params.risk_report`.

`app/main.py::_operator_decision_context_for_reco()` exposes only Plan RR and empirical statistics to the operator context. `app/db.py::get_recommendation_history()` extracts those stored fields for history rows. The frontend renders Plan RR, empirical expectancy and cross-margin risk buffer in the main table and detailed economics card; raw rank/confidence diagnostics are removed from the primary table/history; the heuristic proxy remains in backend storage/API compatibility only and is not copied into the frontend technical payload. Existing rows without the additive JSON fields remain readable and show unavailable. There is no relational schema or migration change.

## v1.0.60 market-data transaction ordering

The `collector` and `backfill` loops remain independently supervised and retain separate runtime leadership locks, but their shared `ohlcv` write contract is now explicit:

1. network workers may complete in any order;
2. results are first accumulated in memory;
3. `db.upsert_ohlcv()` deduplicates and sorts the complete transaction by `(venue, symbol, tf_sec, ts)`;
4. the write is committed through the lock-retry boundary, which rolls back a PostgreSQL deadlock victim before replay;
5. decision-log writes use a separate transaction so a noncritical audit-log lock cannot enlarge the OHLCV lock graph.

The hot collector records which source timeframes actually received rows. Derived 15m/30m/4h maintenance runs only for symbols touched at the corresponding source timeframe. In normal lifespan wiring the hot loop fetches 1m only, therefore 4h maintenance belongs to the backfill loop that fetches 1h. This reduces redundant writes without changing candle geometry or persistence schema.

## v1.0.58 operator evidence boundary

`GET /api/v1/outcomes/stats` accepts `scope=current_policy|current_model|archive` and defaults to `current_policy`. `app/main.py` derives the active fingerprint from current settings plus active risk limits. `app/db.py` filters model lineage, recomputes each persisted policy-contract digest, and aggregates only admitted rows. The frontend requests current-policy and archive payloads independently; only the former drives headline and detailed policy tables. No relational migration is required.

Status now publishes a calibration gate contract with separate monetary and probability floors, sample gaps, OOF requirements, and the observability hard-block. These are diagnostics only; they do not weaken publication gates or create execution authority.

## v1.0.57 policy/evidence architecture

The recommendation cycle canonicalizes normalized settings plus active risk limits into a full SHA-256 policy fingerprint before loading calibration. Each root persists that contract, and both the fit path and outer denominator recompute its digest before use. The calibration path is:

`pre-calibration candidate policy -> exact fingerprint cohort -> outer-join observability denominator -> monetary Student-t gates -> purged walk-forward skill -> untouched terminal holdout -> pre-holdout LogReg + Platt activation`.

`app/policy.py` owns canonical JSON hashing. `app/recommender.py` owns policy construction, verified exact-cohort selection and fail-closed inference; its standalone direction Platt is audit-only. `app/outcomes.py` owns queue rotation and waiting/censored/labeled transitions. `app/db.py` owns the verified independent denominator, immutable reconciliation snapshots and distinct profitability versus loss-conservative risk streams. `app/calibration.py` owns Student-t bounds, aggregate/final log-loss comparisons and persistence validation. `/api/v1/status` and the frontend expose the same policy counts and skill metrics.

The relational change is additive and idempotent in both `init.sql` and `init_postgres.sql`: `reco_outcome_observability` and `execution_reconciliations` plus indexes. Existing databases upgrade through normal `init_db()`; no Alembic/manual data rewrite is used. Execution reconciliation is an ingestion boundary for a trusted external read-only adapter, not private order flow.

## v1.0.56 calibration lineage boundary

`app/recommender.py::calibration_lineage_diagnostics()` is the shared source of truth for archive/current/eligible partitioning. Fit paths and `/api/v1/status` use the same filter. `app/calibration.py` uses v19 cache identities and `app/recommender.py` uses direction key v14, so stale v18/v13 objects cannot be loaded as current. PostgreSQL and SQLite schemas are unchanged.

## v1.0.55 candidate-screen and temporal-thinning flow

`app/settings.py` owns the bounded `MEAN_REVERSION_MIN_SCORE` candidate floor. `app/recommender.py::_mean_reversion_grid_blocks()` distinguishes missing evidence (hard block) from a valid score below that floor (strategy `no_trade`) and deliberately makes no PnL claim. The independent `app/calibration.py` monetary gate remains mandatory for actionability.

`app/calibration.py::_temporal_cluster_return_diagnostics()` now forms one cohort per recommendation timestamp, computes a cross-sectional weighted mean without symbol-count credit, and applies earliest-finish interval scheduling. The selected cohort intervals are pairwise non-overlapping and maximal in count. v17 bot/global keys force recomputation from retained outcomes without deleting rows or changing relational schema.

## v1.0.54 calibration activation flow

`matured historical outcomes` -> `monetary/temporal gates` -> `score-only Platt baseline` -> `feature extraction` -> `purged chronological OOF logits` -> `OOF Platt-on-top` -> `feature LogReg activation`.

`app/calibration.py` owns both fitting and the activation boundary. Full-sample feature coefficients are withheld unless the OOF stage is sufficient; persistence records the OOF diagnostics. `app/recommender.py` may report `bot_logreg` only when non-empty coefficients survived that boundary. Otherwise it reports `bot_platt` or raw confidence. This is code-only calibration-state evolution: no outcome label, relational schema or publication lifecycle change.

## v1.0.53 boundary-candle liquidity flow

`compute_outcomes_once()` separates the strategy horizon from evidence availability: `horizon_end_ts = entry_ts + horizon_sec`, while `label_available_ts = horizon_end_ts + 60`. It requires the exact boundary 1m candle to be complete before calling `_grid_outcome()`.

`_grid_outcome()` resets `candle_volume_capacity_qty` and `candle_volume_used_qty` when entering the boundary candle. The same budget is consumed by close-to-open gap fills and terminal residual liquidation. Kill-switch liquidation uses the remaining capacity in the breach candle. `ledger_invalid` is checked after all intrabar path simulations so a capacity failure cannot disappear when equivalent path snapshots are restored. No schema change is required; OHLCV volume and JSON diagnostics already exist.

## Historical kill-switch loss bound (v1.0.52)

`app/outcomes.py::_grid_outcome` separates two prices at a protective exit: the grid-processing boundary and the conservative residual-inventory liquidation bound. Resting orders are processed only up to the configured kill-switch. If the observed intrabar continuation is adverse to the residual position, the proxy closes at the corresponding candle extreme; otherwise it retains the boundary price and does not credit favorable slippage. Ledger snapshots include stop boundary and observed extreme so alternative OHLC paths cannot appear equivalent when their terminal loss bounds differ.

## v1.0.51 historical-simulation boundary

The recommendation/outcome path is intentionally independent of runtime exchange executability. `_reco_thread` does not prefetch current Bybit instrument filters for publication, and `run_recommender_once` has no exchange-normalizer callback. `compute_outcomes_once` labels persisted historical geometry without requiring an exchange snapshot.

`reasons.simulation_scope` is the authoritative boundary: `historical_proxy_only`, no order submission, no runtime execution validation, and no exchange fill attestation. Current Bybit snapping/validation helpers remain available only to explicit operator preflight endpoints; their result cannot change recommendation status, persisted geometry, outcome eligibility, or calibration.

The model remains conservative within OHLCV limits: strict trade-through, candle-volume capacity, delayed replacement activation, cost/funding rules, temporal clustering and monetary lower-bound gates. These are simulation assumptions, not claims that an order would have filled in runtime.

## v1.0.50 outcome ledger timing boundary

The proxy ledger now separates `orders` active before the current candle from `pending_orders` created by fills during that candle. Snapshots used for alternative OHLC paths include both maps. Pending replacements activate only at the next candle boundary; crossing one earlier makes the outcome unavailable because order-placement latency is not observable from OHLCV.

## v1.0.49 outcome execution-capacity boundary

`app/outcomes.py` now reads OHLCV `volume` with each one-minute candle and maintains a path-local aggregate fill budget. Recommendation sizing remains immutable input: the persisted `qty_per_order` is multiplied by simulated slot quantity before any ledger mutation. Intrabar high-first/low-first snapshots include consumed volume so path equivalence cannot hide different capacity usage. The change is computation-only and requires no schema migration because `ohlcv.volume` already exists in both SQLite and PostgreSQL schemas.

## v1.0.48 exchange-evidence boundary (historical, removed in v1.0.51)

Versions 1.0.48-v1.0.50 temporarily coupled publication/outcomes to current Bybit filters and an `exchange_execution_snapshot`. Version 1.0.51 removed the metadata prefetch, normalizer callback and mandatory snapshot check. Strict trade-through and other conservative OHLCV rules remain; current exchange filters do not participate in recommendation status or calibration.

## v1.0.46 funding-alpha boundary

- `app/outcomes.py` maintains signed settled funding diagnostics and a separate conservative funding contribution for canonical proxy return. Only negative/adverse cashflows enter `ret`.
- `app/main.py` advances the outcome contract to `grid_label_v19` and deletes current bot/global/direction calibrator cache keys when labels are reset.
- Exact execution evidence remains signed account truth; proxy calibration remains conservative hypothesis evidence. No DB schema or API route changes are required.

## v1.0.45 temporal evidence aggregation

`app/calibration.py` now has two monetary uncertainty layers. `_weighted_return_diagnostics()` describes row-level returns. `_temporal_cluster_return_diagnostics()` builds interval-overlap components from matured `[ts, label_available_ts]` rows, computes one weighted mean per component, and evaluates effective cluster count, dispersion and a one-sided lower bound. `fit_logreg()` is fail-closed unless both layers pass.

The new diagnostics are persisted inside the existing `app_config.value_json`; no table or migration changes are required. `app/recommender.py` exposes `time_clusters=current/min` and `time_cluster_lower_bound` in the monetary-veto diagnostic. v9 cache keys isolate the new contract from v8 models.

## v1.0.44: terminal exact-evidence boundary

Execution-evidence persistence remains append-only. `db.get_bot_execution_summary()` now adds a deterministic signed-quantity reconciliation layer over immutable execution rows. `db.list_live_validation_records()` exposes both complete and incomplete records for audit, but marks a record eligible only when the bot is stopped and the execution ledger is terminally flat. `main._live_validation_scope_summary()` independently rechecks `total_pnl_finalized=True` before accepting a row, so a malformed caller or stale payload cannot inject partial PnL into the stop gate.

No new table or column is required: finalization is recomputed from existing `execution_evidence.side`, `qty`, `bot_instances.status`, and `stopped_ts`.

## v1.0.43: uncertainty-bounded calibration boundary

`app/calibration.py` now owns monetary uncertainty diagnostics in addition to probability fitting. The persisted LogReg payload carries weighted dispersion, Kish effective sample size, one-sided lower bound and confidence level. `app/recommender.py` treats bot-specific monetary evidence as a prerequisite publication layer: non-positive or unproven evidence creates a shadow `no_trade` before operator action, while preserving the row for independent future outcome accumulation.

The v8 bot/global cache identities prevent v7 positive-mean models from being reloaded under the stricter contract. No relational schema migration is needed because calibration state remains versioned JSON in `app_config`. Direction Platt calibration is diagnostic and remains v6; it cannot override the bot-specific monetary gate.

## v1.0.42: calibration cache lifecycle

Calibration persistence in `app_config` is now a bounded cache, not an independent source of model truth. `app/recommender.py` revalidates stale positive bot/global/direction models against the retained joined outcome dataset; sparse current evidence produces a persisted unfitted state. Negative monetary expectancy remains an asymmetric safety veto. New cache keys force this lifecycle on first startup without deleting outcomes or changing schema.

## v1.0.41: shadow publication lineage

Publication-chain теперь имеет отдельный horizon-aware путь для counterfactual `shadow_no_trade`. Он не меняет operator status (`no_trade` остаётся `no_trade`) и не превращает shadow row в active recommendation. Путь отвечает только за statistical identity: один открытый pseudo-position соответствует одному outcome root, а повторные UI/audit publications становятся children. Это устраняет псевдорепликацию без удаления истории.

## v1.0.40 monetary-expectancy calibration flow

`db.get_outcomes_with_recs()` supplies matured proxy rows including `ret`. `calibration.fit_logreg()` sanitizes timestamps, binary labels and returns, computes recency weights, weighted mean return and 20% lower-tail expected shortfall, then either fits the probability model or returns a persisted negative expectancy state.

`_load_or_fit_bot_logregs()` treats both a fitted positive model and an unfitted negative expectancy state as persistable cache states. In the recommendation loop, `_calibration_expectancy_no_trade_reason()` converts the latter into an explicit strategy `no_trade` before publication. Confidence falls back to capped raw heuristic and cannot use the rejected model. Hard feasibility/risk blocks retain precedence over `no_trade`.

The change is additive JSON inside `app_config`; SQLite/PostgreSQL schemas and public API fields are unchanged. Cache keys move to v5 so v4 coefficients cannot cross the new eligibility boundary.

## v1.0.39 exact-evidence tail-loss control flow

`_execution_preflight()` calls `_compute_live_validation_strategy_health()` before bot audit materialization. The latter reads immutable stopped-bot execution evidence, filters by venue/bot/model version, deduplicates publication roots, builds direction/symbol/portfolio summaries, and applies the sample floors. In v1.0.39 a negative cumulative exact net PnL is the sample-based stop predicate; median and win rate are emitted only as distribution diagnostics. Any resulting `LIVE_VALIDATION_*` block prevents `executed` and bot-instance creation.

## v1.0.38 unavailable-outcome state flow

`recommendation + OHLCV + settled funding` -> outcome ledger. A missing settlement is a retryable dependency state, not a malformed recommendation. The worker writes a rate-limited `OUTCOME_WAIT_FUNDING_SETTLEMENT` event and leaves the recommendation unlabeled. Permanent persisted-contract failures use structured `OUTCOME_SKIP_INVALID_GRID_CONTRACT` reasons. No new database table or API contract is introduced.

## v1.0.37 settled-funding data flow

`BybitPublicClient.get_funding_rate_history` -> collector 35-day paginated backfill -> `funding_settlement(symbol, ts, funding_rate)` -> outcome inventory ledger. Forecast snapshots remain in `funding_rate` for recommendation-time risk; immutable settlements are a separate source of truth for historical labels.

## v1.0.36 cost-layer ownership

- `app/recommender.py` публикует recurring grid fee, one-time market friction и funding как разные поля/слои.
- `app/grid_math.py` считает Grid Profit пары только после двух fill fees и публикует отдельный Total-P&L funding stress.
- `app/outcomes.py` применяет market friction к initial directional entry/terminal residual exit, grid fee к resting fills и funding к фактическому inventory во времени.
- `app/main.py` оставляет spread отдельным live-liquidity gate, recurring fee - per-grid edge gate, funding - отдельным schedule/inventory gate.

## v1.0.35 cross-margin safety boundary

The recommendation service models Bybit Linear USDT Futures Grid as a unified-account, cross-margin, one-way product. `app/grid_math.py::arithmetic_grid_cross_margin_stress` is the deterministic safety contract shared by leverage selection and execution preflight. It consumes canonical grid commitment and external kill-switch geometry and returns per-unit committed capital, adverse loss, execution/maintenance reserve and remaining equity buffer. It deliberately does not model or expose a standalone isolated liquidation price.

This remains an audit/recommendation boundary, not a private-account liquidation engine. Live wallet equity, other positions/orders, risk tiers and actual mark-price liquidation are external executor responsibilities.

## Neutral opening-order commitment boundary (v1.0.34)

`app/grid_math.py::arithmetic_grid_commitment` is the single source of truth for both reservation and exposure, but these are intentionally distinct. For NEUTRAL it sums every initial Buy/Sell opening-order price into `committed_notional_per_qty` and counts all initial orders in `committed_slot_count`. It separately returns `max_abs_position_slots` as the larger directional stack. Recommender, snap, preflight, runtime risk and outcomes may not replace the commitment sum with a max-side approximation.

The dynamic bridge contract remains: N intervals, N+1 prices, one idle bridge, exactly N initial orders. Therefore neutral committed slots are N, while maximum net position is generally smaller.

## Dynamic bridge topology boundary (v1.0.33)

`app/grid_math.py::arithmetic_grid_commitment` is the single source of truth for initial arithmetic topology. It emits N+1 prices, exactly N initial orders, one `idle_grid_index`, directional initial inventory, one-way committed slots and maximum position slots. Recommender, payload snap, execution preflight, runtime risk, daily-loss guard and outcome ledger consume this contract; no module may reconstruct an N+1 initial-order model independently.

Outcome replacement orders may later occupy the bridge only after the adjacent fill. A bridge fill before that state transition is a fabricated event and must not be labelled.

## Neutral one-way commitment boundary (v1.0.32)

`app/grid_math.py::arithmetic_grid_commitment` is the single topology/commitment source. It returns all resting orders separately from one-way committed slots and maximum directional exposure. HISTORICAL/SUPERSEDED: v1.0.32 used the larger Buy/Sell price sum. v1.0.34 requires the sum of all initial neutral opening orders; for LONG/SHORT it remains initial inventory plus adverse-side openings. Recommender, snap, preflight, runtime risk and outcomes consume this contract and may not reconstruct commitment from `grid_count` or total active orders.

# Архитектура Bybit Recommender

## Quantity-aware ledger and discontinuous-stop boundary (v1.0.31)

`app/outcomes.py` represents each resting level as a signed integer quantity, not a single side flag. Same-side replacement lots are aggregated; opposing quantities at one level invalidate the proxy contract instead of implying self-trading. Cash, inventory, fees, funding exposure and path-equivalence snapshots all include those quantities.

A continuous observed segment may terminate at a kill-switch. A discontinuous close→open or horizon gap that lands beyond the protection cannot be priced at the skipped boundary and is rejected as unavailable. `app/main.py::_execution_daily_loss_budget_guard` reuses `arithmetic_grid_commitment` for its fallback active-order count.

## Exact commitment/path-invariance boundary (v1.0.30)

`app/grid_math.py::arithmetic_grid_commitment` is the single topology/commitment source for `app/recommender.py`, auto-snap and execution validation in `app/main.py`, and proxy normalization in `app/outcomes.py`. It returns arithmetic levels, buy/sell index sets, initial directional slots, active-order count, maximum position slots and committed notional per unit quantity. Callers may not reconstruct `N × reference` independently.

The outcome engine snapshots the full ledger and executes both admissible high/low orderings when a candle has two material excursions. Non-equivalent snapshots are rejected as unavailable; this preserves temporal uncertainty instead of selecting a favorable or unfavorable path.


## Назначение системы

Проект — это **рекомендательный контур** для операторского запуска grid-ботов на Bybit.
Он **не является OMS/EMS**, не создаёт ордера на бирже автоматически и не пытается
эмулировать полный жизненный цикл биржевого execution layer.

Система должна:
- собирать и нормализовать market data Bybit;
- вычислять признаки и multi-timeframe directional/regime контекст;
- публиковать рекомендации для `futures_grid`;
- блокировать очевидно опасные идеи через risk gate / market shock / fast-veto / LLM-review;
- сохранять audit trail, publication lineage, operator actions и proxy-outcomes.

## Outcome path/stop boundary (v1.0.29)

`app/outcomes.py` now treats the persisted arithmetic grid as an explicit protected state machine. Non-grid-line directional entry creates all adjacent close orders and matching initial slots. The worker processes observable endpoint segments, accepts only unambiguous one-sided OHLC excursions, and terminates cash/inventory/funding evolution at the first valid kill-switch boundary. Missing/inside-range protection or dual-boundary intrabar ambiguity produces no label.

## Outcome temporal/contract boundary (v1.0.28)

`app/outcomes.py` treats publication time as an availability boundary. A proxy position may start only at the open of the first exact 1m candle strictly after both the signal reference bar and the persisted recommendation publication. Missing exact candles remain unavailable.

Duplicated persisted grid/funding fields are one contract. Valid duplicates must agree; explicit malformed or conflicting aliases do not receive a first-wins or conservative fallback. An invalid contract is skipped before `reco_outcomes` insertion, preserving calibration integrity.


## Границы ответственности

### Что система делает
- читает публичные REST-данные Bybit;
- формирует trade idea и operator guidance;
- проверяет исполнимость trade plan по известным ограничениям инструмента;
- ведёт журнал рекомендаций, операторских действий и агрегированных trade rows;
- даёт API/UI для ручного подтверждения и анализа качества рекомендаций.

### Что система сознательно не делает
- не выставляет и не сопровождает реальные ордера на Bybit;
- не хранит живую книгу ордеров и не восстанавливает её после рестарта;
- не моделирует websocket-order stream / execution report stream;
- не знает фактический размер leg, если его не сообщает внешний исполнитель;
- не гарантирует корректность live PnL/fee/funding без внешнего источника фактических trade/fill данных.

## Слои архитектуры

### 1. Data layer
Модули: `collector.py`, `bybit_client.py`, `sentiment.py`, `sentiment_features.py`

Функции:
- сбор тикеров и OHLCV;
- сбор funding/open interest для linear;
- backfill исторических окон;
- нормализация битых payload'ов;
- ограничение параллелизма и heartbeat runtime-lock'ов.

### 2. Inference layer
Модули: `features.py`, `direction.py`, `regime.py`, `recommender.py`, `calibration.py`

Функции:
- вычисление market features;
- multi-timeframe vote и directional aggregation;
- определение regime;
- построение grid-параметров и trade plan;
- calibration / logreg / Platt scaling для quality-контуров.

### 3. Control layer
Модули: `risk.py`, `shock_guard.py`, `llm_review.py`, части `main.py`

Функции:
- risk limits и execution-time recheck;
- market shock state machine;
- symbol fast-veto;
- LLM second opinion;
- preflight перед operator execution.

### 4. Persistence and audit layer
Модуль: `db.py`

Функции:
- SQLite schema и миграции bootstrap;
- recommendations / decision log / bots / trades / outcomes;
- publication lineage;
- runtime locks;
- app_config и техническое состояние фона.

### 5. Operator/API layer
Модуль: `main.py`, `app/ui/static/*`

Функции:
- REST API;
- UI для просмотра рекомендаций и деталей;
- ручное подтверждение `executed|ignored`;
- запись агрегированных trade rows;
- остановка bot instance в audit-контуре.

## Поток данных

1. `collector` собирает тикеры и свечи, пишет их в SQLite.
2. `recommender` читает последние признаки и формирует recommendation snapshot.
3. recommendation проходит control layer: risk gate, shock guard, dedupe, publication lineage, опционально LLM review.
4. published recommendation сохраняется в `recommendations` и `decision_log`.
5. оператор вручную подтверждает `executed`.
6. сервис повторно делает execution preflight и только после этого materialize'ит `bot_instance`.
7. внешний исполнитель или оператор пишет агрегированные `trades`.
8. `outcomes.py` размечает outcome-root записи через close-to-close arithmetic-grid order/inventory ledger по persisted range/count, применяет execution cost к каждой inferred leg и terminal close, adverse funding - только к фактическому inventory на точных событиях внутри horizon, а success - по знаку total net PnL с kill-switch precedence; результат остаётся OHLCV proxy, а не execution truth.
9. calibration использует outcome history для quality-моделей.

## Потоки и конкуренция

Фоновые контуры:
- `collector`
- `backfill`
- `futures_meta`
- `sentiment`
- `reco`
- `llm_reviewer` (если включён)

Защиты:
- отдельная SQLite sidecar-БД для runtime-lock'ов;
- heartbeat leader-lock'а;
- supervised background wrapper с явным thread state;
- bounded parallelism для REST-fetch задач;
- WAL + busy_timeout для основной SQLite.

## Модель согласованности

### Что считается источником истины
- market data snapshot — SQLite tables `ohlcv`, `ticker_snap`, `features`;
- operator publication-chain — `recommendations.publication_root_rec_id`;
- statistical outcome-chain — `recommendations.outcome_root_rec_id` plus `is_outcome_label_root`;
- operator-facing recommendation list делает adaptive raw-scan перед collapse, чтобы длинная одна chain не вытесняла остальные уникальные идеи из `top_n`;
- operator execution state — `bot_instances`;
- realised operator/audit events — `trades`, `decision_log`.

### Что считается приближением
- `trade_plan`, legacy internal `expected_rr` and separated `operator_metrics.plan_rr` / `operator_metrics.empirical_expectancy`;
- `risk_score`;
- `reco_outcomes.ret` и `success`;
- daily PnL / DD при неполных trade rows;
- LLM review.

## Execution-time preflight

Перед переводом recommendation в `executed` система перепроверяет:
- freshness candles/tickers;
- active symbol disable state;
- market shock blocks;
- symbol fast-veto;
- геометрию trade plan относительно Bybit metadata;
- внутреннюю согласованность bot_type / venue / direction / mode;
- отсутствие обязательного `margin_mode` для supported execution paths (fail-closed для legacy/manual rows);
- символическую согласованность Bybit metadata (`symbol/category` не должны относиться к другому инструменту);
- leverage bounds и alignment по `leverage_step`, если биржа их предоставляет.
- exact-evidence strategy health по direction, symbol и portfolio: persistent realised losses блокируют новые operator executions до ревизии модели.

## Ключевой архитектурный вывод

Проект можно считать **production-ready только как recommendation + audit service**.
Для production-grade auto-execution нужен отдельный OMS/EMS-контур с order/fill state machine,
идемпотентным order routing, websocket reconciliation и recovery по реальным ордерам.


## Дополнительные инварианты этой ревизии
- `runtime_locks` в PostgreSQL захватываются атомарно через одну UPSERT-операцию; схема `SELECT`→`UPDATE` для leader-election признана небезопасной из-за риска split-brain.
- `bot_instances.publication_root_rec_id` materialized и используется как DB-level инвариант для запрета двух одновременных `running`-ботов в одной publication-chain.
- mutating API-пути в PostgreSQL теперь дополнительно берут row-level lock (`FOR UPDATE`) на целевую `recommendations`/`bot_instances` строку, чтобы concurrent `execute` / `trade` / `stop` не принимали решения по устаревшему snapshot и не теряли агрегаты состояния.


## Дополнительный execution-time guard текущей ревизии

Operator execution path теперь содержит отдельный live-price guard между freshness-check и materialization `bot_instance`.
Он использует последний валидный ticker из persistence-слоя и сохранённый `trade_plan`, чтобы не позволить оператору подтвердить grid-рекомендацию, рассчитанную для уже неактуального диапазона.
Guard не отправляет и не отменяет ордера; он только блокирует операторское подтверждение в audit/recommendation контуре.


## Execution evidence validation contour

`external read-only Bybit adapter -> authenticated evidence API -> execution_evidence -> unified realised event stream -> risk/drawdown/cooldown + descriptive validation export`

`execution_evidence` is additive to dual persistence and never performs order operations. It stores immutable linkage to `bot_instances.origin_rec_id`, exact external identities, exchange fill fields, a separate benchmark snapshot and separate execution/funding event types. SQLite and PostgreSQL use the same logical contract. Legacy `trades` remains for compatibility but is mutually exclusive per bot.

The architecture deliberately separates:

- execution truth: actual fill/funding events;
- execution-quality diagnostic: adverse benchmark-to-fill deviation;
- validation claim: not produced automatically.

The realised stream also feeds a conservative preflight stop gate. It deduplicates publication roots, scopes evidence to the explicit recommendation `model_version`, and blocks continued operator execution after predefined negative direction/symbol/portfolio evidence. The gate is a safety response to losses, not an automatic claim that non-blocked cohorts have positive expectancy.

Private exchange reconciliation, raw payload archival, account inventory and unrealised PnL remain outside this repository.

## Independent range-edge validation (v1.0.20)

Inference layer теперь разделяет два разных понятия: отсутствие направленного тренда и подтверждённую anti-persistence. `app.direction` вычисляет mean-reversion diagnostics на каждом закрытом TF; `aggregate_direction` формирует weighted evidence; `app.recommender` применяет hard publication gate. Это separation-of-concerns не позволяет score/LLM/risk слоям трактовать low trend как достаточный alpha signal.

Calibration contour версионирован отдельно: recommendation identity `bybit-taxonomy-v3-mean-reversion`, LogReg/Platt keys v4 и фильтрация training rows по model version + evidence snapshot. Старые DB rows сохраняются как audit history, но не участвуют в новой калибровке. Схема БД не меняется.
## Shadow outcome branch

`candidate -> deterministic gates -> no_trade` не должен становиться тупиком обучения. Если payload полный и hard blocks отсутствуют, recommender добавляет explicit `outcome_policy(sample_role=shadow_no_trade, eligible=true)`. Outcome worker принимает только этот literal opt-in, повторно проверяет `risk_checks.passed` и после horizon создаёт counterfactual proxy label. Hard-blocked/pending/malformed/legacy rows остаются вне sample. Calibration и UI получают sample-role diagnostics; реальное исполнение по-прежнему подтверждается только external execution evidence.
