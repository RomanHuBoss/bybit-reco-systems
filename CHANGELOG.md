# CHANGELOG

## 2026-04-09 — fail-closed execution validation and upstream shape hardening

### Исправлено
- execution-time Bybit validation теперь блокирует legacy/manual recommendations без явного `margin_mode` вместо молчаливого допуска исполнения в неявном режиме;
- validation теперь fail-closed отклоняет рекомендации, если полученная metadata Bybit относится к другому `symbol`, а не к целевому инструменту recommendation;
- публичный Bybit REST-клиент теперь ретраит не только decode-failures, но и transient `response shape error` сценарии: не-объектный JSON и битый `retCode`.

### Добавлено
- warning `ACCOUNT_MODE_LEGACY_ALIAS` для исторических futures rows с `account_mode=one_way`, чтобы отделить legacy-совместимость от штатной модели `account_mode=unified`;
- регрессионные тесты на блокировку missing-`margin_mode`, symbol-mismatch Bybit metadata и retry битых shape-ответов публичного клиента.

### Тесты
- `pytest -q` → `316 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `ruff check app tests main.py` → passed.

## 2026-04-08 — red-team reliability and operator-signal hygiene

### Исправлено
- execute-path больше не держит SQLite `BEGIN IMMEDIATE` во время внешнего запроса за instrument metadata Bybit: metadata теперь подгружается заранее, вне write-lock, а внутри критической секции используется уже готовый snapshot проверки;
- operator-facing `GET /api/v1/recommendations` теперь по умолчанию скрывает дубли одной `publication_chain`, оставляя в списке один лучший элемент на `publication_root_rec_id`, чтобы repeated `active` updates не выглядели как поток одинаковых идей;
- operator-facing collapse больше не зависит от жёсткого лимита `top_n * 4`: API адаптивно расширяет сырой scan-budget, если одна publication-chain доминирует длинной серией `active` updates и вытесняет другие уникальные идеи из snapshot;
- публичный Bybit REST-клиент теперь ретраит transient transport/protocol ошибки и битые 2xx decode-failures, а также считает HTTP 408 retryable upstream-сценарием.

### Добавлено
- ответ `GET /api/v1/recommendations` дополнен блоком `publication_chain_dedupe` с числом скрытых дублей и возможностью отключить collapse через `collapse_chains=false`;
- регрессионные тесты на отсутствие внешнего Bybit fetch под SQLite write-lock, на adaptive collapse больших duplicate-chain bursts и на retry transient Bybit transport/decode failures.

### Тесты
- добавлен сценарий, который проверяет порядок `Bybit metadata fetch -> BEGIN IMMEDIATE`, чтобы execute-flow не блокировал остальные writer-контуры на сетевых задержках;
- добавлен API-тест на collapse raw-дублей `recommended/active` внутри одной publication-chain;
- добавлены transport-тесты на `RemoteProtocolError` и malformed-JSON retry-path в публичном Bybit-клиенте.

## 2026-04-08 — release consistency and stop-state determinism

### Исправлено
- `.env.example` синхронизирован с фактическими runtime-дефолтами LLM-reviewer (`LLM_REVIEWER_MAX_CANDIDATES=24`, `LLM_REVIEWER_MAX_WORKERS=2`);
- остановка бота теперь использует единый `stopped_ts` для строки `bot_instances` и `state_json`, чтобы audit/state reconciliation был детерминированным.

### Добавлено
- `docs/AUDIT_REPORT_2026-04-08.md` с итогами red-team-аудита;
- API-регрессии на синхронность `stopped_ts` для manual stop и `stop_bot=true` при записи trade.

### Тесты
- расширен регрессионный набор на stop-state timestamp consistency;
- подтверждена согласованность `.env.example` с runtime/default docs.

## 2026-04-07 — audit hardening revision

### Исправлено
- усилена execution-time Bybit validation:
  - добавлена проверка внутренних инвариантов `bot_type ↔ venue ↔ direction`;
  - добавлена проверка `account_mode` / `margin_mode` против фактической модели проекта;
  - добавлена проверка `min_leverage`, `max_leverage`, `leverage_step`;
  - validation теперь явно показывает `snapped` leverage при off-step значении;
- шаблон `.env.example` теперь явно содержит `SYMBOLS_SPOT`, а не только закомментированный пример.

### Добавлено
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/TRADING_LOGIC.md`
- `docs/SCENARIOS.md`
- `docs/KNOWN_RISKS.md`

### Тесты
- добавлены регрессионные тесты на mode/leverage validation;
- добавлены тесты release-integrity для новых docs/env cross-reference.
