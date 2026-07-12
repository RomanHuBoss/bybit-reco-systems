# Модули и контракты

## `app/outcomes.py` — current v9 contract

- chooses the first exact 1m entry open strictly after recommendation publication;
- never stores invalid/contradictory grid geometry as a flat or losing outcome;
- requires valid duplicate range, grid-count and funding aliases to describe one identical persisted bot;
- logs and skips unlabelable contracts instead of manufacturing an alternative geometry;
- keeps the explicit inventory ledger, per-leg/terminal costs and adverse inventory funding from v6-v8.


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
