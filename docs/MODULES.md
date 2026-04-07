# Описание модулей

## `app/bybit_client.py`
Публичный REST-клиент Bybit.
- retry/backoff для сетевых и части upstream-ошибок;
- нормализация ответов;
- market data и instrument metadata.

## `app/collector.py`
Сбор market-data и backfill.
- bounded parallelism;
- защита от битых payload;
- heartbeat-aware hot/backfill fetch;
- derived timeframe bootstrap.

## `app/recommender.py`
Ядро inference/publishing.
- scoring и confidence;
- trade-plan/operator-sheet;
- persistence-gate;
- publication dedupe и lineage;
- integration с LLM-reviewer.

## `app/outcomes.py`
Outcome labeling.
- path-approximation для grid outcome;
- decomposition execution-cost vs funding-carry;
- root-only outcome semantics по publication-chain.

## `app/risk.py`
Runtime risk gates.
- active bot cap;
- symbol cap;
- daily drawdown;
- cooldown after realized losses.

## `app/shock_guard.py`
Рыночные режимы fail-safe.
- market shock state;
- symbol fast-veto;
- operator note и block semantics.

## `app/main.py`
FastAPI, background supervision и mutating operator API.
- execution-time preflight для operator-confirmation;
- safe JSON normalization для UI/API;
- частичная Bybit-валидация trade plan для панелей деталей и execute-path.

## `app/db.py`
SQLite persistence, нормализация JSON, runtime locks, lifecycle helpers.

## `app/security.py`
Проверка `ADMIN_API_KEY` и loopback-only fallback при пустом ключе.

## `app/settings.py`
Bootstrap и нормализация env/config.
