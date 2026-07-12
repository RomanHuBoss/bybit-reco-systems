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
