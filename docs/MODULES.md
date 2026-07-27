## Module changes - v1.5.0

- `app/recommender.py`: новая horizon-aligned dual-strategy lineage, уменьшение correlated trend double-counting, явный `ranking_score`, event-driven recommendation persistence, dedupe `shadow_competitor`.
- `app/direction.py`: 12h-aligned MTF weights без доминирования 1d.
- `app/db.py`: `recommendation_latest`, material-event ledger, conditional OHLCV updates, bucketed ticker/funding, grid capture scope, evidence-first retention и storage diagnostics.
- `app/collector.py`: bounded derived recomputation, separate backfill cadence, actual changed-row counters, on-demand REST trade fallback.
- `app/trade_stream.py`: bounded session refresh для изменения active grid capture scope.
- `app/main.py`: latest-state API, on-demand trade subscriptions, 300s backfill cadence, v1.5.0.
- `app/settings.py` / `.env.example`: storage cadence and retention controls.
- `migrations/init.sql`, `migrations/init_postgres.sql`: additive `recommendation_latest` schema.
- `tests/test_iteration284_dual_strategy_data_efficiency.py`: RED→GREEN contract for both strategies, storage, market data and retention.

## Module changes - v1.4.13

- `app/db.py`: transaction advisory lock, deterministic market-trade UPSERT order, serialized journal pruning.
- `app/collector.py`: REST fallback commits per symbol and rolls back/logs failures independently.
- `app/main.py`: dead local PID lease reclamation, always-on market-trade retention, version bump.
- `tests/test_iteration283_market_trade_deadlock_restart_takeover.py`: regression coverage for concurrency, restart takeover and retention.

## Module changes - v1.4.12

- `app/trade_stream.py`: protocol keepalive disabled; Bybit application heartbeat and receive watchdog are the liveness contract.
- `app/db.py`: REST coverage equal-timestamp boundary and savepoint-based PostgreSQL transaction recovery.
- `app/main.py`, `app/ui/static/index.html`: patch version/cache bump to 1.4.12.
- `tests/test_iteration282_ws_heartbeat_pg_recovery.py`: RED→GREEN coverage-boundary, transaction-rewind and heartbeat regressions.
- No schema, API, model, outcome-label or observation-provenance changes.

## Module changes - v1.4.11

- `app/trade_stream.py`: graceful network-disconnect classification, process-local stream runtime state, Bybit heartbeat, wider keepalive queue/timeout and batched commits.
- `app/main.py`: internal reconnect loop with bounded backoff, REST fallback suppression while stream is active, transition-based compact warm-up events, additive stream runtime status.
- `app/settings.py` / `.env.example`: optional WebSocket ping, queue, batching and reconnect tuning.
- `app/ui/static/app.js`: Russian labels for `RECO_WARMUP_SKIP` and `RECO_WARMUP_RECOVERED`.
- `app/ui/static/index.html`: cache/version bump to 1.4.11.
- `tests/test_iteration281_stream_resilience_warmup_noise.py`: transport, reconnect, REST fallback and warm-up dedup regressions.

## Module changes - v1.4.10

- `app/db.py`: preserve the exclusive first-trade boundary and raise only the initial coverage end to that boundary when `T == ts`, producing a valid zero-width span.
- `app/main.py`, `app/ui/static/index.html`: patch version/cache bump to 1.4.10.
- `tests/test_iteration280_market_trade_coverage_window.py`: RED→GREEN direct persistence and supervised-session regressions.
- No schema, config, outcome-label, model or observation-provenance changes.

## Module changes - v1.4.9

- `app/trade_stream.py`: documented `T`-only monotonic validation; materialized session/message/row delivery order.
- `app/db.py`: additive stream-order columns, existing-DB upgrader, session-scoped path query and local message-index guard.
- `app/outcomes.py`: WebSocket delivery-order validation and `grid_intrabar_observation_v3` provenance.
- `migrations/init.sql`, `migrations/init_postgres.sql`: dual-backend nullable delivery-order fields/index.
- `app/main.py`, `app/ui/static/index.html`: version/cache bump to 1.4.9.
- `tests/test_iteration279_public_trade_ordering.py`: RED→GREEN parser, persistence and SQLite upgrade regressions.

## Module changes - v1.4.8

- `app/collector.py`: short funding retry, recent overlap refresh, durable targeted repair worker, REST trade fallback/bootstrap, overlap/gap coverage и retention pruning.
- `app/bybit_client.py`: strict public `/v5/market/recent-trade` fallback sanitizer.
- `app/trade_stream.py`: strict read-only `publicTrade.{symbol}` WebSocket parser/session, runtime-heartbeat integration и disconnect-bounded coverage.
- `app/db.py`: funding repair queue, market trade journal, coverage ledger, status и pruning API.
- `app/outcomes.py`: funding repair scheduling и OHLC-consistent public-trade replay с observation provenance.
- `app/settings.py` / `.env.example`: journal/repair limits and explicit public stream switch.
- `app/main.py`: v1.4.8, hot collector + supervised public stream wiring и status diagnostics; model/outcome label identities сохранены.
- `app/ui/static/app.js`: funding recovery и trade journal в Health.
- `migrations/init.sql`, `migrations/init_postgres.sql`: additive dual-backend tables/indexes.
- `tests/test_iteration278_funding_recovery_trade_journal.py`: RED->GREEN regression package.

## Module changes - v1.4.7

- `app/direction.py`: log-price helpers; mirror-symmetric RSI/MACD/MA/Bollinger/ATR; `indicator_space=log_price_v1`; complete component diagnostics.
- `app/recommender.py`: new recommender/direction/trend lineage identifiers after feature-semantics change.
- `app/calibration.py`: new grid/trend/global calibrator storage keys; old coefficients cannot load under the new representation.
- `app/trend_events.py`: new first-touch model/key lineage using the same updated recommendation feature identity.
- `app/db.py`: exact observation-window normalization, temporal sample diagnostics, `sample_observability`, cohort-aware `by_bot_cohort` aggregation.
- `app/ui/static/app.js`: truthful sample-structure renderer; cohort-aware Results table; localized, escaped master-detail decision journal.
- `app/ui/static/styles.css`: responsive journal cards and structured detail grid.
- `app/ui/static/index.html`: v1.4.7 asset cache token.
- `tests/test_iteration277_direction_observability_journal_ui.py`: independent mirror truth, temporal-dependence/cohort fixture and production JS renderer regression tests.

## Module changes - v1.4.6

- `app/main.py`: generated/manual sizing classification; minimum exchange-executable qty; conservative leverage-step rounding; full commitment revalidation; primary qty diagnostic suppression.
- `app/outcomes.py`: first-touch/gap exit-candle MFE/MAE observability boundary.
- `app/recommender.py`: empty directional input fails closed without referencing unavailable aggregation state.
- `app/ui/static/app.js`: concrete blocker codes take precedence over duplicate generic risk prose.
- `tests/test_iteration276_exchange_sizing_math.py`: independent exchange-sizing, leverage, UI, exit-path and fail-closed regressions.

## Module changes - v1.4.5

- `app/recommender.py`: owns creation and persistence of canonical direction-aware feature snapshots.
- `app/calibration.py`: owns direction normalization, sentiment alignment, contradiction rejection and the 15-field binary calibration schema.
- `app/trend_events.py`: uses the shared schema under first-touch softmax v2 lineage.
- `app/strategy_router.py`: consumes the canonical `TREND_EVENT_MODEL_VERSION` constant rather than a duplicated string.
- `tests/test_iteration275_direction_aware_learning.py`: proves binary and multiclass learnability on mirrored LONG/SHORT samples and fail-closed snapshot integrity.

## v1.4.4: label maturity and calibration lineage

- `app/policy.py`: canonical exact-positive integer parsing and shared label due calculation.
- `app/recommender.py`: effective-horizon due persistence; strict availability validation before policy calibration; unchanged heuristic score and model lineage.
- `app/outcomes.py`: market-data availability and policy maturity are combined conservatively before labeling.
- `app/db.py`: bounded legacy timestamp repair and complete compact lineage projection (`recommendation_ts`, `horizon_sec`, `label_available_ts`).
- `tests/test_iteration274_label_maturity_learning.py`: RED/GREEN coverage for worker timing, lineage rejection, startup repair and numeric fail-closed semantics.

## v1.4.3: compact modal observability helpers

- `app/ui/static/app.js / renderModalDisclosure()` — раскрываемый контейнер для advanced outcome/health tables.
- `app/ui/static/app.js / closeAllDialogs()` — единая точка закрытия modal-диалогов, включая Escape.
- `loadOutcomes()` — один primary strategy aggregation; cohort and monetary semantics stay separate.
- `loadHealth()` — объединённые operator reasons и evidence readiness; deep runtime/DB/LLM diagnostics остаются доступными в disclosure.
- `app/ui/static/styles.css` — 1600 px wide contract и compact disclosure styling.

Backend modules, schemas and strategy math remain unchanged in this patch.

## v1.4.2: candidate-kind lifecycle

- `app/recommender.py` classifies preliminary trend analysis before constructing any position geometry. Neutral trend becomes `trend_evaluation_rejected`; valid LONG/SHORT becomes `strategy_recommendation`.
- `app/db.py` persists and indexes `candidate_kind`, upgrades/repairs legacy rows, excludes rejected evaluations from outcome/history/training queries and exposes integrity counters.
- `app/main.py` projects rejected evaluations as diagnostics, suppresses TP/SL and blocks materialization before any live validation.
- `app/strategy_router.py` accepts only formed strategy recommendations and uses router identity `strategy-profitability-router-v3`.
- `app/trend_events.py` excludes rejected or explicitly neutral trend rows from first-touch fitting.
- `app/ui/static/app.js` renders a preliminary-evaluation card with no position/history controls.

## v1.4.1: strategy-native direction and remediation ownership

- `app/main.py`: `_operator_next_actions_for_reco` gates every strategy-specific action by `bot_type`; missing legacy `bot_type` defaults to historical `futures_grid`.
- `app/ui/static/app.js`: `strategyDirectionRu` and `strategyDirectionBadge` render `(bot_type, direction)` together; Details uses strategy-native titles, blocker translations and backend-localized actions.
- `app/ui/static/styles.css`: `dir-invalid` distinguishes an unconfirmed trend direction from a valid neutral grid.
- `tests/test_iteration270_strategy_native_direction_ui.py`: regression coverage for API isolation, labels, remediation, localization and deduplication.

## v1.4.0: strategy observability and operator-history responsibilities

- `app/db.py` — durable pre-horizon outcome schedule, canonical event persistence, per-strategy counts, semantic-integrity checks, batch outcome tracking and immutable history geometry.
- `app/main.py` — strategy-native exit projection, enriched Details/list APIs, strategy-aware decision journal and health readiness degradation on outcome inconsistency.
- `app/ui/static/app.js` — distinct grid/trend Details, outcomes, health, journal and price-history rendering; visible failures instead of silent empty states.
- `app/ui/static/styles.css` — strategy history graph lines, legends and responsive operator tables.

## v1.3.0: first-touch trend event model

- `app/trend_events.py` — трёхклассовая softmax-модель `TP_FIRST / SL_FIRST / HORIZON_EXIT`, chronological holdout, purging, persistence и plan-specific monetary assessment.
- `app/outcomes.py` — определяет первый однозначный TP/SL touch, сохраняет `event_type`, цензурирует `AMBIGUOUS` и missing-minute path.
- `app/db.py` — additive migration и чтение/запись `reco_outcomes.event_type` для SQLite/PostgreSQL-compatible persistence.
- `app/calibration.py` — отдельная binary trend lineage v2; не объединяет trend и grid.
- `app/recommender.py` — загружает/обучает event model, добавляет first-touch assessment и reason codes.
- `app/strategy_router.py` — требует положительную консервативную first-touch EV и доказанный порядок TP-first против SL-first.
- `app/main.py` — публикует readiness и метрики first-touch модели в system status.
- `app/ui/static/app.js` — показывает P(TP first), P(SL first), timeout probability и EV/lower bound.

## v1.2.0: profitability router and trend audit execution

- `app/strategy_router.py`: validates comparable monetary evidence and selects a strategy by conservative risk-adjusted utility.
- `app/recommender.py`: builds both candidates, invokes the router, preserves losing candidates for paired outcomes, and builds the trend single-order package.
- `app/main.py`: validates and snaps single-position trend plans, applies live market/funding/risk/conflict preflight and creates an audit instance without exchange order submission.
- `app/calibration.py`: continues separate grid/trend calibrators and supplies selected-policy/terminal monetary evidence to the router.
- `app/ui/static/app.js`: displays router decision, utility/edge and the distinction between grid and one-position trend.

## Historical v1.1.0: directional trend shadow responsibilities

- `app/bot_types.py`: registers the separate trend family and its shadow-only boundary.
- `app/recommender.py`: regime routing, trend score, one-position TP/SL plan, shadow publication, trend contract/version metadata and separate calibration eligibility.
- `app/outcomes.py`: deterministic `directional_trend_label_v1` path accounting with exact 1m continuity, TP/SL ambiguity censoring, funding and costs.
- `app/calibration.py`: independent `logreg_directional_trend_v1` storage key; grid and trend labels are not pooled for inference.
- `app/main.py`: explicit `DIRECTIONAL_TREND_SHADOW_ONLY` execution-preflight rejection.
- `app/ui/static/app.js`: separate strategy labels and non-executable trend detail presentation.

The new module responsibilities do not add order submission, an OMS/EMS or private Bybit order methods.

## v1.0.78: operator/outcome lineage separation

- `app/recommender.py`: independently resolves live operator publication TTL and open statistical outcome horizon.
- `app/db.py`: additive `outcome_root_rec_id` schema, backfill, serializers and maintenance repair.
- `app/main.py`: history API exposes publication/outcome root counts and kinds separately.
- `app/ui/static/app.js`: operator history labels fresh publications and shared outcome windows explicitly.
- `migrations/init*.sql`: dual-backend outcome-lineage column and index.
- `tests/test_iteration265_operator_outcome_horizon_separation.py`: red/green lifecycle, maturity, repair and SQLite upgrade contracts.

## v1.0.77: outcome eligibility and offline evidence

- `app/db.py`: exact eligibility read model, reason counters and 14/90-day
  selective retention.
- `app/ui/static/app.js`: non-overlapping cohort table, gate reasons and
  per-outcome score/mean-reversion columns.
- `scripts/offline_walk_forward.py`: purged timestamp walk-forward using only
  labels available before each validation timestamp.
- `tests/test_iteration264_outcome_eligibility_walk_forward.py`: cohort,
  retention, no-leakage and UI contracts.

## v1.0.76: outcome audit semantics

- `app/outcomes.py`: формирует terminal reason и kill-switch diagnostics для завершённой proxy-метки.
- `app/db.py`: сохраняет diagnostics в существующем `reco_outcome_observability` и включает их в enriched read model.
- `app/ui/static/app.js`: строго валидирует outcome numeric/boolean fields и объясняет terminal outcome.
- `app/ui/static/styles.css`: нейтральное отображение неизвестного/некорректного исхода.
- Схема, migrations, calibration formulae и execution preflight не изменены.

## v1.0.75: денежная проверка terminal-selected policy

- `app/calibration.py`: вычисляет aggregate и terminal selected-policy monetary diagnostics, требует positive terminal row/temporal lower bounds, сохраняет/валидирует новый cache contract v21.
- `app/recommender.py`: policy schema v3/model v10, не активирует LogReg без positive terminal-selected evidence, публикует отдельные поля в `confidence_model`.
- `app/main.py`: FastAPI 1.0.75 и additive terminal-selected поля/contract в `/api/v1/status`.
- `app/ui/static/app.js`: явно показывает состояние и размер денежной проверки выбранной политики на итоговом периоде.
- `tests/test_iteration262_terminal_selected_policy_monetary.py`: regression недавнего денежного reversal и rejection старого fitted cache.

## v1.0.74: денежная цель выбранной политики

- `app.calibration._chronological_validation_blocks`: целые timestamp blocks и минимальный terminal contract.
- `app.calibration._selected_policy_return_diagnostics`: row/temporal monetary evidence exact OOF-подвыборки.
- `app.calibration.selected_policy_confidence`: единая adaptive blend + adjustment формула для fit и runtime.
- `app.calibration.LogRegScaler` persistence: terminal rows/cohorts и selected-policy diagnostics с fail-closed loader validation; bot/global keys v20.
- `app.recommender`: model/policy v9/v2, запись selection inputs, exact gate parity и новые confidence diagnostics.
- `app.main` и `app/ui/static/app.js`: status/readiness объясняют обе новые границы.
- `tests/test_iteration261_selected_policy_and_terminal_holdout.py`: negative selected-policy, one-row terminal, formula parity, persistence и fail-closed gates.

## v1.0.73: завершение LLM reviewer

- `app.main._llm_reviewer_thread`: прекращает цикл после общего shutdown-event и не запускает повторный sweep во время остановки.
- `app.main._run_supervised_background_target`: существующий clean-stop и owner-safe lock-release контракт теперь достижим для всех зарегистрированных background components.
- `tests/test_iteration260_llm_shutdown_liveness.py`: динамически проверяет stop, отсутствие второго sweep, состояние `stopped` и удаление owned reviewer lock.

## v1.0.72: быстрые диагностические чтения

- `app.db.get_outcome_history_summary()` — SQL-only totals/class balance исторического архива.
- `app.db.iter_calibration_lineage_rows(current_model_version=...)` — bounded JSON stream только текущей модели.
- `app.db.get_outcomes_stats(include_breakdowns=False)` — краткая архивная сводка без полной Python-агрегации.
- `GET /api/v1/outcomes/stats?...&detail=summary` — контракт краткого архива для UI.

## v1.0.71 - изменённые обязанности модулей

- `app/main.py`: lifecycle release runtime-lock, расчёт handover grace, collector state/provenance и дополнительные status fields.
- `app/db.py`: read-only snapshot runtime-lock; LLM outcome eligibility допускает явно разрешённый риск-чистый shadow exploration без exact-policy допуска.
- `app/outcomes.py`: SQL selection соответствует тому же shadow contract и не требует `policy_evaluation_eligible=true` для исследовательской метки.
- `app/ui/static/app.js`: exact-code локализация decision log, сохранение технических идентификаторов и показ lock takeover diagnostics.

## v1.0.69 — диагностические обязанности

- `app/db.py`: создаёт стабильный `database_instance_id_v1` и формирует безопасную сводку непрерывности.
- `app/main.py`: агрегирует полный publication snapshot и проверяет provenance текущего процесса.
- `app/ui/static/app.js`: показывает идентификатор БД, владельца последнего collector cycle и признаки собственного цикла/публикации.

## v1.0.68 — обязанности модулей диагностики готовности

### `app/db.py`
- `get_outcome_policy_schema_status(conn)` проверяет наличие materialized eligibility/LLM колонок и сообщает состояние legacy materialization; функция read-only и не заменяет `init_db()`.

### `app/main.py`
- `_latest_recommendation_readiness()` строит ограниченный снимок последней публикации и агрегирует причины `no_trade`/`blocked`.
- `_operator_runtime_readiness()` разделяет техническое состояние runtime и наличие actionable-рекомендаций.
- `/api/v1/status` публикует `app_version`, `database_schema`, `recommendation_readiness` и `operator_readiness` как additive contract.

### `app/ui/static/app.js`
- `operatorDecisionPresentation()` является единым frontend-контрактом подписи и CSS-класса статуса.
- `loadHealth()` объединяет symbol health, runtime status и ограниченную историю решений, отображает итог и позволяет экспортировать диагностический JSON.
- `renderRecoTable()` оставляет решение отдельной ячейкой, а действие «Детали» — отдельной крайней правой колонкой.

## v1.0.67: обязанности outcome-контура

- `app/outcomes.py`: `compute_outcomes_cycle()` обрабатывает ограниченный пакет, поддерживает runtime heartbeat и возвращает структурированные показатели цикла; `compute_outcomes_once()` сохранён как count-only compatibility wrapper.
- `app/db.py`: persistence boundary материализует индексируемые outcome-policy/LLM поля; runtime migration выполняет bounded legacy backfill. `get_outcome_worker_liveness()` агрегирует eligibility/maturity только по колонкам и классифицирует состояние по durable cycle progress.
- `app/main.py`: `_outcome_thread()` владеет отдельной блокировкой, `_run_outcome_cycle_once()` сохраняет running/completed/error snapshots, supervisor перезапускает контур при исключении. Рекомендательный поток больше не выполняет outcome maintenance.

## Модули bounded calibration memory (v1.0.66)

- `app/db_backend.py`: `execute_stream`, PostgreSQL named cursor, bounded `fetchmany`.
- `app/db.py`: потоковые readers для calibration outcomes, observability, outcome-worker liveness и compact lineage rows.
- `app/recommender.py`: per-cycle `_CalibrationEvidenceContext`, общий exact-policy dataset и streaming lineage aggregation mode.
- `app/main.py`: `/api/v1/status` больше не хранит полный outcome history в Python.
- `tests/test_iteration254_bounded_calibration_memory.py`: RED → GREEN contract для bounded cursor, JSON compaction, shared evidence и non-retaining status aggregation.

## Модули восстановления истории и диагностики памяти (v1.0.65)

- `app/collector.py`: выбирает свежий минутный хвост после длинного простоя, создаёт/продвигает устойчивые gap-backfill jobs, ограничивает размер страницы и число futures в памяти.
- `app/main.py`: передаёт bounded budget, сохраняет статистику collector/backfill и публикует Linux `/proc/self/status` RSS/HWM без новой runtime-зависимости.
- `app/settings.py`: безопасные дефолты `BACKFILL_FULL_SWEEP_ON_WARMUP=0` и `BACKFILL_PER_TF_BUDGET=8`.
- `app/ui/static/app.js`: показывает оператору память процесса и прогресс фонового восстановления в окне здоровья.

## Обязанности UI-локализации в v1.0.64

- `app/ui/static/app.js`: единый словарь видимых статусов, направлений и торговых терминов; преобразование динамических сообщений; доступные подсказки; сохранение машинных кодов в техническом представлении.
- `app/ui/static/index.html`: русские названия элементов, пять колонок главной таблицы и клавиатурно доступные подсказки к ключевым показателям.
- `app/main.py`: русские операторские рекомендации по безопасным следующим действиям; внутренние коды и API-поля не переименовываются.

## v1.0.63 module responsibility update

- `app/main.py`: FastAPI `1.0.63`; maps internal reason codes to bounded operator hints and preserves raw detail additively.
- `app/ui/static/index.html`: five visible decision columns; no standalone reason column.
- `app/ui/static/app.js`: decision badge owns the hover/focus hint and accessible label; full diagnostics remain in Details.
- `tests/test_iteration251_operator_decision_hint.py`: reason translation, safe fallback, table contract and production-JS rendering regression.

## v1.0.62 module responsibility update

- `app/outcomes.py`: advances eligible shadow roots without requiring an impossible LLM review.
- `app/db.py`: canonical LLM/outcome eligibility and worker-liveness invariant.
- `app/main.py`: FastAPI `1.0.62`, stable `operator_summary`, status liveness and stall logging.
- `app/bybit_client.py`: reset-aware retry for Bybit `10006`.
- `app/collector.py`: metadata-confirmed temporary disable for absent instruments.
- `app/ui/static/`: six-field primary table; complete metrics stay in Details.

## v1.0.61 module responsibility update

- `app/recommender.py`: computes scenario Plan RR without recurring-fee double counting; publishes exact-policy empirical mean/CI/tail diagnostics; keeps heuristic capture internal.
- `app/calibration.py`: persists temporal mean return and provides strict two-sided Student-t confidence intervals for operator evidence.
- `app/main.py`: FastAPI version `1.0.61`; operator decision context exposes plan and empirical metrics, not legacy `expected_rr`.
- `app/db.py`: recommendation history reads additive operator metrics from stored reasons.
- `app/ui/static/index.html`, `app/ui/static/app.js`, `styles.css`: primary table/history/detail surfaces Plan RR, empirical expectancy and risk buffer; raw model proxies move out of the primary table/history; technical payload retains compatibility fields.
- `tests/test_iteration249_operator_rr_metrics.py`: independent Plan RR oracle, fee-layer separation, numeric fail-closed, empirical/CI and UI-contract regressions.

## v1.0.60 module responsibility update

- `app/collector.py`: accumulates API/bootstrap/derived OHLCV rows before database persistence; uses retry-capable committed batches; tracks source-timeframe touches so hot 1m work cannot rewrite unrelated 4h series.
- `app/db.py`: existing `_commit_write_with_retry()` and canonical OHLCV key ordering remain the single persistence mechanism for deadlock recovery; schema unchanged.
- `app/main.py`: FastAPI version `1.0.60`; collector/backfill supervision and runtime-lock topology unchanged.
- `tests/test_iteration248_postgres_ohlcv_transaction_order.py`: reproduces localized PostgreSQL deadlock-victim behavior and verifies global canonical lock order for hot and backfill batches.

## v1.0.58 module responsibility update

- `app/db.py`: scope-normalized outcome aggregation, exact model/policy admission, policy-contract digest verification, and lineage fields in recent outcome rows.
- `app/main.py`: current-policy default API contract and truthful calibration-readiness diagnostics (80 monetary versus 300 probability by default).
- `app/ui/static/app.js`: separate current-policy and historical-archive requests/rendering; archive never drives the active headline.
- `tests/test_iteration246_outcome_scope_readiness.py`: lineage separation, fail-closed scope validation, deep-current-row retrieval, API default, and readiness-copy regressions.

## v1.0.57 module responsibility update

- `app/calibration.py`: Student-t monetary bounds; aggregate and terminal purged log-loss skill; activates the pre-terminal-holdout pipeline only; rejects malformed fitted persistence.
- `app/policy.py`: one strict canonical-JSON SHA-256 implementation shared by write and read boundaries.
- `app/recommender.py`: canonical full policy contract/fingerprint, verified exact-policy cohort selection, censor/cache-support veto, v8 model and v19/v14 identities; no score-only probability fallback; direction Platt audit-only.
- `app/outcomes.py`: durable waiting/censored/labeled attempts and starvation-free bounded queue rotation.
- `app/db.py`: observability denominator, immutable/idempotent terminal reconciliation, exchange-reconciled profitability stream and loss-conservative unreconciled risk stream.
- `app/risk.py`: consumes the loss-conservative stream, so unverified gains cannot recover drawdown while losses still tighten controls.
- `app/main.py`: FastAPI 1.0.57, reconciliation POST/admin-list GET, fresh policy/censor/skill status fields.
- `app/ui/static/app.js`: labels raw/legacy confidence as uncalibrated and exposes policy matured/labeled/censored/unresolved plus held-out skill.
- `migrations/init.sql`, `migrations/init_postgres.sql`: additive observability and reconciliation tables/indexes.
- `tests/test_iteration245_policy_conditioned_calibration.py`, `tests/test_iteration245_exchange_attestation_and_queue.py`: 24 new regressions.

## v1.0.56 module responsibility update

- `app/recommender.py`: v7 model identity and shared calibration-lineage diagnostics.
- `app/calibration.py`: v18 bot/global cache keys.
- `app/main.py`: FastAPI 1.0.56 and separated calibration dataset counts.
- `app/ui/static/app.js`: explicit archive/current/eligible/fit/temporal wording.
- `tests/test_iteration244_calibration_lineage_reset.py`: lineage, API and executed frontend regression coverage.

## v1.0.55 module responsibility update

- `app/settings.py`: validates `MEAN_REVERSION_MIN_SCORE` in `[0,1]`, default `0.25`.
- `app/recommender.py`: uses the configured floor as a candidate screen and no longer describes a weak score as proven negative expectancy.
- `app/calibration.py`: collapses same-timestamp cross-sectional outcomes and selects a maximal pairwise non-overlapping temporal cohort set; bot/global identities move to v17.
- `app/main.py`: publishes FastAPI `1.0.55`; outcome/model/direction identities and schema remain unchanged.
- `tests/test_iteration243_mean_reversion_temporal_recovery.py`: covers runtime-observed score reachability, truthful diagnostics, transitive overlap recovery, cross-sectional deduplication, deterministic thinning and env configuration.

## v1.0.54 module responsibility update

- `app/calibration.py`: requires sufficient purged chronological OOF predictions and a fitted Platt-on-top before exposing feature LogReg coefficients; persists OOF activation diagnostics.
- `app/recommender.py`: exposes purged OOF status/counts and clearly distinguishes feature LogReg from score-only Platt fallback.
- `app/main.py`: publishes FastAPI `1.0.54`.
- `tests/test_iteration242_purged_oof_activation_gate.py`: proves concentrated history cannot activate feature LogReg, distributed independent history can, and diagnostics survive persistence.

## v1.0.53 module responsibility update

- `app/outcomes.py`: owns boundary-candle evidence timing, resets volume capacity across minute boundaries, and applies capacity to gap fills, terminal residual closes and kill-switch liquidation.
- `app/main.py`: publishes FastAPI `1.0.53` and resets incompatible `grid_label_v26` outcomes/calibrators.
- `app/calibration.py`: v15 bot/global keys isolate labels built under the corrected liquidation-capacity target.
- `app/recommender.py`: direction calibration v12 isolates the same target change.
- `tests/test_iteration241_horizon_boundary_liquidity.py`: proves wrong-minute budget reuse, terminal-close capacity, kill-switch-close capacity and boundary-candle availability timing.

## v1.0.52 outcome responsibility update

- `app/outcomes.py`: models kill-switch exits with separate trigger-boundary and liquidation-bound semantics. Residual short inventory after an upper breach uses the observed candle high; residual long inventory after a lower breach uses the observed candle low. Favorable continuation is not credited, and gap-through-stop paths remain unavailable.
- `tests/test_iteration240_kill_switch_slippage_bound.py`: proves symmetric adverse-tail pricing and the `grid_label_v25` identity reset.

## v1.0.51 historical-only simulation responsibilities

- `app/recommender.py`: publishes model recommendations with explicit `historical_proxy_only` scope; no runtime instrument-normalization callback.
- `app/outcomes.py`: labels persisted historical geometry using conservative OHLCV assumptions; no mandatory exchange snapshot.
- `app/main.py`: application `1.0.51`, outcome contract `grid_label_v24`; background recommendation loop has no current-metadata publication dependency.
- Explicit Bybit preflight helpers remain separate operator diagnostics and do not feed recommendation status or calibration.

## v1.0.50 module delta

- `app/outcomes.py`: tracks active and intrabar-pending replacement quantities separately; rejects same-candle replacement crossings with a machine-readable timing reason; activates surviving replacements on the next candle.
- `app/main.py`: outcome contract `grid_label_v23`, application `1.0.50`, startup reset of incompatible proxy outcomes/calibrators.
- `app/calibration.py` / `app/recommender.py`: bot/global/direction calibration identities v12/v9.

## v1.0.49 module responsibility update

- `app/outcomes.py`: validates candle volume, tracks cumulative simulated base quantity per minute, blocks impossible full fills and initial directional inventory, and exposes `fill_volume_confirmation=aggregate_candle_volume_cap_v1`.
- `app/main.py`: publishes FastAPI v1.0.49 and resets incompatible `grid_label_v22` outcomes/calibration.
- `app/calibration.py` / `app/recommender.py`: use v11/v8 calibration identities so v21 labels cannot remain actionable.
- `tests/test_iteration237_proxy_fill_volume_capacity.py`: proves single-fill, cumulative-fill, initial-inventory and sufficient-volume behavior.

## v1.0.48 module responsibility update

- `app/main.py`: recommendation-time Bybit metadata acquisition, exchange normalization, immutable filter snapshot and release/label reset.
- `app/recommender.py`: fail-closed publication policy for missing or invalid exchange-normalized geometry; model/direction identities v5/v7.
- `app/outcomes.py`: independent snapshot verification and strict side-aware trade-through fill reconstruction.
- `app/calibration.py`: v10 identities prevent loading coefficients trained on theoretical/touch-filled outcomes.

## v1.0.46 module responsibility update

- `app/outcomes.py`: exclude positive settled funding receipts from canonical proxy `ret` while preserving adverse payments and diagnostic signed totals.
- `app/main.py`: reset `grid_label_v19` outcomes and every current calibrator identity, including `DIRECTION_CALIBRATION_KEY`.
- `tests/test_iteration234_funding_receipt_not_alpha.py`: prove that LONG/SHORT receipts cannot create proxy edge and adverse funding remains charged.

## v1.0.45 cross-symbol temporal evidence responsibilities

- `app/calibration.py`: merges directly or transitively overlapping outcome intervals into temporal components; calculates cluster count, Kish effective cluster count, cluster return dispersion and one-sided lower bound; requires both row and cluster evidence before fitting.
- `app/recommender.py`: reports row and temporal lower bounds plus `time_clusters=current/min` when monetary evidence is unproven.
- `app/main.py`: exposes FastAPI version `1.0.45`.
- `tests/test_iteration233_cross_symbol_temporal_dependence.py`: proves that 80 contemporaneous symbols equal one temporal experiment, that 21 non-overlapping horizons can qualify, that clock-boundary overlap remains one component, and that diagnostics persist.
- Persistence remains additive JSON in `app_config`; SQLite/PostgreSQL schemas are unchanged.

## v1.0.44 module responsibility update

- `app/db.py`: execution summary now reconciles signed fill quantity and distinguishes an event stream from terminal total-PnL evidence. Live-validation records include `validation_ineligible_reasons`.
- `app/main.py`: live-validation aggregation and API summary require `total_pnl_finalized=True`; bot state mirrors reconciliation diagnostics after each evidence event.
- External execution/reconciliation adapter: must deliver every bot fill and funding transaction. A partial stream remains auditable but cannot authorize or statistically validate the strategy.

## v1.0.43 module responsibility update

- `app/calibration.py`: computes monetary proxy mean, expected shortfall, unbiased weighted dispersion, Kish effective sample size, and one-sided 95% lower confidence bound; returns `unknown/insufficient/negative/uncertain/positive`; persists v8 diagnostics.
- `app/recommender.py`: converts every non-positive or unproven bot-specific monetary state into explicit shadow `no_trade`, exposes diagnostics in `confidence_model`, and prevents raw confidence from becoming actionable before positive evidence.
- `tests/test_iteration231_expectancy_uncertainty_gate.py`: independent payoff-distribution, persistence, identity and end-to-end publication regressions.

## v1.0.42 module responsibility update

- `app/recommender.py`: expires stale positive bot/global/direction calibrators when current retained evidence is insufficient; preserves stale negative monetary veto only.
- `app/calibration.py`: bot/global cache identity v7; existing strict persistence format is reused for fitted and insufficient states.
- `app/db.py`: unchanged schema; `app_config` remains the calibrator cache store and outcome retention remains 14 days.
- `tests/test_iteration230_stale_calibrator_fail_closed.py`: proves stale positive cache eviction and restart-safe persistence for all three calibration paths.

## v1.0.41 module responsibility update

- `app/recommender.py`: определяет explicit shadow-no-trade eligibility, ищет открытый shadow root на label horizon и назначает lineage без изменения operator status.
- `app/calibration.py`: v6 keys отделяют новую независимую sample policy от ранее сохранённых calibrators.
- `app/outcomes.py`: без изменений; как и раньше, размечает только `is_outcome_label_root=1`, теперь получая корректно дедуплицированный shadow stream.

## v1.0.40 monetary-expectancy responsibilities

- `app/calibration.py`: validates finite proxy returns, computes weighted mean/expected shortfall, stores expectancy state, and prevents LogReg/Platt fitting on non-positive monetary cohorts.
- `app/recommender.py`: persists/loads negative expectancy cache states, emits `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE`, and exposes expectancy diagnostics in `reasons.confidence_model`.
- `app/outcomes.py`: remains the producer of normalized net proxy `ret` and binary `success`; its OHLCV limitations remain explicit.
- `app/db.py`: unchanged schema; existing `app_config.value_json` stores v5 calibrator metadata.
- `tests/test_iteration228_monetary_expectancy_calibration.py`: independent many-small-wins/few-large-losses oracle, persistence, cache and strict numeric regressions.

## v1.0.39 tail-loss gate responsibilities

- `app/main.py::_live_validation_scope_summary`: derives independent-bot cumulative, mean, median, win-rate and consecutive-loss diagnostics from exact evidence.
- `app/main.py::_negative_expectancy_condition`: enforces the sample floor and blocks on negative cumulative/mean exact net PnL; it deliberately does not require negative median or sub-50% win rate.
- `app/main.py::_compute_live_validation_strategy_health`: applies direction/symbol/portfolio scopes and exposes diagnostic policy metadata.
- `app/main.py::_execution_preflight`: propagates the block before any `bot_instance` materialization.
- `tests/test_iteration227_tail_risk_stop_gate.py`: locks the grid tail-loss reproducer and non-blocking controls.

## v1.0.38 outcome diagnostic responsibilities

- `app/outcomes.py`: returns optional structured failure diagnostics while preserving the existing `None` compatibility contract.
- `compute_outcomes_once`: maps transient funding-history gaps to `OUTCOME_WAIT_FUNDING_SETTLEMENT`; permanent contract failures remain `OUTCOME_SKIP_INVALID_GRID_CONTRACT` with a concrete reason.
- decision log cooldown prevents one recommendation from emitting the same unavailable-state row every minute.
- `grid_label_v18` mathematics and database schema are unchanged.

## v1.0.37 funding responsibilities

- `app/bybit_client.py`: validates and parses settled funding-history rows.
- `app/collector.py`: bounded 35-day backfill with hourly refresh throttling.
- `app/db.py`: dual-backend persistence/query of immutable settlements.
- `app/outcomes.py`: applies signed settled funding to inventory; never substitutes a ticker forecast for historical P&L.

## v1.0.36 grid-cost responsibilities

- `recommender._estimate_cost_model`: формирует `grid_round_trip_fee_bps`, `one_time_market_friction_bps`, `market_round_trip_cost_bps`.
- `grid_math.grid_leg_economics`: не распределяет funding и market friction на каждую завершённую пару.
- `outcomes._grid_outcome`: использует разные half-leg rates для market entry/exit и resting grid fills.
- `main._execution_live_cost_blocks`: live spread проверяется отдельно; per-grid edge сравнивается с recurring fee floor.

## v1.0.35 cross-margin responsibilities

- `app/grid_math.py`: exact arithmetic-grid commitment plus conservative cross-margin equity stress; generic isolated-price helpers are not used by Futures Grid production paths.
- `app/recommender.py`: publishes `margin_mode=cross`, `position_mode=one_way`, clamps leverage through the stress model and stores stress diagnostics instead of a fabricated liquidation price.
- `app/main.py`: strict preflight rejects isolated Grid Bot payloads and independently recomputes stress from canonical persisted geometry.
- `app/ui/static/app.js`: labels cross margin and equity buffer explicitly; isolated liquidation price is shown as not calculated.

## v1.0.34 responsibility update

- `app/grid_math.py`: separates all-initial-order neutral commitment from maximum one-way position exposure.
- `app/recommender.py`: publishes `neutral_all_initial_opening_orders` and sizes qty/margin against the full initial neutral order set.
- `app/main.py`: preserves and validates the same commitment during metadata snap, strict preflight and runtime risk checks; exposes version 1.0.34 / `grid_label_v15`.
- `app/outcomes.py`: continues to consume the canonical helper, so neutral percentage returns use full initial opening-order commitment.
- `tests/test_iteration222_neutral_full_opening_commitment.py`: independent exact-level, official-style N=5, sizing, snap, preflight and outcome-denominator regressions.

## v1.0.33 responsibility update

- `app/grid_math.py`: resolves dynamic arithmetic prices, N initial orders and the idle bridge index; calculates direction-specific inventory and commitment.
- `app/recommender.py`: publishes sizing/economics from the canonical dynamic topology.
- `app/main.py`: auto-snap, preflight, runtime caps and daily-loss fallback validate the same N-order contract.
- `app/outcomes.py`: seeds only actual initial orders; the bridge appears only through a valid replacement transition.
- `tests/test_iteration221_off_grid_bridge_topology.py`: independent official-example topology and phantom-fill regressions.

## v1.0.32 responsibility update

- `app/grid_math.py`: resolves active resting topology, one-way committed slots/notional and maximum directional position.
- `app/recommender.py`: publishes active-order count separately from committed/max-position slots and HISTORICAL/SUPERSEDED: v1.0.32 sized neutral margin from the larger directional stack; v1.0.34 sums every initial opening order.
- `app/main.py`: preserves those fields through Bybit snapping and validates/uses them in preflight, runtime caps and daily-loss calculations.
- `app/outcomes.py`: normalizes neutral total PnL by the full initial opening-order commitment through the canonical helper.

# Модули и контракты

## v1.0.31 responsibility update

- `app/grid_math.py`: owns arithmetic level topology and exact committed-notional calculation.
- `app/recommender.py`: publishes sizing/economics from the canonical commitment.
- `app/main.py`: preserves the same commitment during Bybit snapping, strict preflight, runtime caps and daily-loss fallback.
- `app/outcomes.py`: owns the quantity-aware cash/inventory/order ledger, gap-stop availability and path-equivalence labeling.


## `app/outcomes.py` — current v12 contract

- chooses the first exact 1m entry open strictly after recommendation publication;
- stores signed integer order quantity per level, including multiple directional lots at the same price;
- applies fees and funding to the resulting exact inferred quantity/inventory state;
- rejects discontinuous gaps beyond the kill-switch and path-dependent OHLC candles;
- never stores invalid/contradictory grid geometry as a flat or losing outcome;
- logs and skips unlabelable contracts instead of manufacturing an alternative geometry.


## `app/bybit_client.py`
- fail-closed публичный REST-клиент Bybit с retry/backoff, строгим scope `category=linear` + exact `*USDT` symbol и exact-symbol проверкой `instruments-info`;
Публичный REST-клиент Bybit.

Контракт:
- возвращает уже санитизированные структуры;
- retry для transport/retryable upstream cases, включая transient protocol/decode failures уровня CDN/WAF;
- считает ответ успешным только при присутствующем exact-integer `retCode=0`; zero-like boolean/fractional/missing значения не проходят как success;
- формирует kline/open-interest pagination windows только из exact-integer limits и неотрицательных, неинвертированных millisecond timestamps;
- не должен прокидывать явно сломанные JSON-shapes как валидные данные и по возможности должен переживать кратковременный 2xx non-JSON шум повторной попыткой.

## `app/collector.py`
Сбор market data и backfill.

Контракт:
- один символ не должен ронять весь collect cycle;
- runtime lock heartbeat обязателен;
- hot path и backfill path разделены;
- derived TF rows не должны ломать 1m collector semantics.

## `app/features.py`
Расчёт микро- и контекстных фичей.

Контракт:
- не возвращать non-finite значения наружу;
- prefer fail-soft, а не падение recommender loop.

## `app/direction.py`
Direction voting и aggregation по TF.

Контракт:
- direction может быть только `long|short|neutral`;
- range-biased direction разрешён только при усиленных условиях согласованности;
- отсутствие тренда не является mean-reversion evidence; модуль отдельно публикует lag-1 return autocorrelation, variance-ratio, sign-reversal score и multi-TF coverage.

## `app/regime.py`
Классификация market regime.

Контракт:
- использовать те же trend/vol источники, что и scoring контур;
- confidence должна учитывать sample size и agreement.

## `app/recommender.py`
Главный движок публикации рекомендаций.

Контракт:
- формирует только поддерживаемые `bot_type`;
- строит `params`, `trade_plan`, `reasons`, publication lineage;
- не создаёт реальных биржевых ордеров;
- уважает risk/shock/LLM/dedupe/persistence gates;
- `futures_grid` fail-closed блокируется без независимого multi-TF mean-reversion evidence;
- current calibration принимает только outcomes текущей model identity и совместимого feature snapshot;
- publisher маркирует безопасные research-only `no_trade` через explicit `outcome_policy`, а outcome worker повторно проверяет opt-in и отсутствие hard blocks;
- outcome stats разделяют `shadow_no_trade` и non-shadow roots и не трактуют proxy как exchange execution.

## `app/risk.py`
Runtime risk limits.

Контракт:
- limits всегда нормализуются до канонической формы, включая per-bot caps `max_leverage`, `max_position_notional_usdt`, `max_margin_per_bot_usdt`;
- cooldown/daily DD считаются только из известных audit/trade rows;
- limits проверяются и на recommendation-time, и на execution-time.

## `app/shock_guard.py`
Маркетный аварийный guardrail.

Контракт:
- должен fail-safe блокировать новые входы при критических состояниях;
- не должен silently пропускать красный режим как normal.

## `app/llm_review.py`
Локальный LLM second opinion.

Контракт:
- advisory/gate режимы разделены;
- stale/shape-poisoned cache reuse запрещён;
- LLM не заменяет scoring/risk engine.

## `app/outcomes.py`
Proxy outcome labeling для grid-рекомендаций.

Контракт:
- labels считаются только для outcome-root записей;
- label horizon отдельна от operator-facing max holding horizon;
- новая outcome-запись сохраняет точный `label_available_ts` — конец окна от первой реально доступной tradeable candle; legacy labels без этой метки не допускаются в OOF-train folds;
- arithmetic-grid outcome uses persisted range/count and an explicit equal-quantity close-to-close order/inventory ledger; completed pairs earn the full adjacent interval, inferred legs and terminal residual close pay execution cost, and adverse funding is charged only against actual inventory at event time;
- outcome-модель честно считается приближением, а не биржевой truth: no intrabar fills, queue priority or partial-fill reconstruction.

## `app/db.py`
- persistence layer, JSON sanitation, runtime-locks и savepoint-safe duplicate classification для `bot_instances`, legacy `trades` и immutable `execution_evidence`;
Persistence и audit backbone.

Контракт:
- JSON из БД нормализуется по ожидаемой форме;
- mutating операции должны быть транзакционными;
- duplicate bot/trade/external execution requests должны быть идемпотентными и конфликтные payloads должны блокироваться;
- publication lineage и runtime lock state должны переживать рестарт процесса.

## `app/main.py`
API, background lifecycle, preflight, operator actions.

Контракт:
- mutating endpoints защищены ADMIN_API_KEY или loopback policy;
- `executed` допускается только после повторной проверки risk/preflight;
- legacy `trade` и exact execution/funding ingestion не должны смешиваться или silently портить bot state;
- sensitive execution-evidence reads требуют admin authorization;
- live-evidence validation остаётся descriptive-only и не публикует утверждение о прибыльности.
- execution preflight обязан применить exact-evidence strategy-health stop gate; отрицательные direction/symbol/portfolio cohorts текущего explicit `model_version` блокируют `executed`, но отсутствие блока не трактуется как доказанный edge.
