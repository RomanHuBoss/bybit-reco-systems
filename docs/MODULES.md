# Модули и контракты

## `app/bybit_client.py`
- fail-closed публичный REST-клиент Bybit с retry/backoff, строгим scope `category=linear` + exact `*USDT` symbol и exact-symbol проверкой `instruments-info`;
Публичный REST-клиент Bybit.

Контракт:
- возвращает уже санитизированные структуры;
- retry для transport/retryable upstream cases, включая transient protocol/decode failures уровня CDN/WAF;
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
- range-biased direction разрешён только при усиленных условиях согласованности.

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
- уважает risk/shock/LLM/dedupe/persistence gates.

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
- outcome-модель честно считается приближением, а не биржевой truth.

## `app/db.py`
- persistence layer, JSON sanitation, runtime-locks и savepoint-safe duplicate classification для `bot_instances` / `trades`;
Persistence и audit backbone.

Контракт:
- JSON из БД нормализуется по ожидаемой форме;
- mutating операции должны быть транзакционными;
- duplicate bot/trade requests должны быть идемпотентными;
- publication lineage и runtime lock state должны переживать рестарт процесса.

## `app/main.py`
API, background lifecycle, preflight, operator actions.

Контракт:
- mutating endpoints защищены ADMIN_API_KEY или loopback policy;
- `executed` допускается только после повторной проверки risk/preflight;
- `trade` ingestion не должен silently портить bot state.
