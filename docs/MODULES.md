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
