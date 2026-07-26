# Audit iteration 279: Bybit publicTrade delivery ordering

## 1. Входной релиз

- ZIP: `bybit-reco-systems-1.4.8-funding-recovery-trade-journal.zip`
- SHA-256: `f9f73ec3995f1eb3cf986e4b0afa4ad90011fe4a33b579a120d3289d303ed58a`
- Исходная версия: `1.4.8`
- Новая версия: `1.4.9`
- Project root: `bybit-reco-systems-main`

## 2. Project fingerprint

Подтверждены Bybit Recommender, FastAPI в `app/main.py`, strategy families `futures_grid` и `directional_trend`, Bybit Linear USDT scope, recommendation/audit-only boundary, SQLite/PostgreSQL dual persistence, frontend в `app/ui/static/`, отсутствие private order placement endpoints.

## 3. Цель итерации

После итерации корректный Bybit `publicTrade.{symbol}` payload с несколькими сделками в одну миллисекунду не должен аварийно завершать background stream. Система должна сохранять доставленный порядок строк внутри WebSocket session и использовать только session-scoped chronology при grid outcome replay.

## 4. Влияние на model/outcome lineage

Итерация не меняет trading score, features, candidate generation, risk policy, router, target labels или calibrator inputs.

- `RECOMMENDER_MODEL_VERSION`: без изменений.
- `OUTCOME_LABEL_VERSION=grid_label_v26`: без изменений.
- Существующие recommendations/outcomes/calibrators не удаляются и не обнуляются.
- Observation provenance повышен с `grid_intrabar_observation_v2` до `grid_intrabar_observation_v3`, поскольку способ доказательства внутрисвечного порядка исправлен.
- Окончательные исторические outcomes массово не переписываются.

## 5. Критерии приемки

1. Equal-`T`, equal-`seq` rows с opaque IDs в произвольном порядке принимаются.
2. Реальное уменьшение `data[].T` внутри одного payload блокируется fail-closed.
3. Session/message/row delivery order сохраняется в SQLite/PostgreSQL.
4. WebSocket path выбирает только rows exact coverage session и не смешивает REST fallback.
5. Повторяющийся или уменьшающийся top-level message `ts` не уменьшает coverage; локальный delivery index остаётся monotonic guard.
6. Existing v1.4.8 SQLite schema обновляется additively/idempotently.
7. Полный test set остаётся зелёным.

## 6. Подтверждённый defect

### PT-ORDER-001 — HIGH — CONFIRMED DEFECT

- Файл: `app/trade_stream.py`
- Функция: `parse_public_trade_message`
- Runtime symptom: `ValueError: non-monotonic public trade WebSocket row order`
- Фактическая проверка v1.4.8: `(T, seq, trade_id)` обязан строго не уменьшаться.
- Контракт Bybit: `data` сортируется по времени match `T`; `seq` может повторяться, trade ID является identity, а не документированным tie-breaker.
- Reproducer: две строки с одинаковыми `T=...995`, `seq=42`, IDs `z-trade`, затем `a-trade`.
- Фактический результат: stream crash, coverage span закрывается, supervisor перезапускает worker и пишет повторные `COLLECT_ERROR`.
- Ожидаемый результат: payload принимается, delivered row order сохраняется.
- Финансовое влияние: торговый сигнал не становится fail-open, но intended trade chronology недоступна; неоднозначные grid outcomes остаются censored, а журнал решений засоряется ошибками.
- Почему тесты не поймали: regression fixture v1.4.8 проверял искусственную строгую сортировку по composite key и тем самым закреплял недокументированное предположение.

## 7. Связанные gaps, закрытые в work package

- В `market_trade` отсутствовал materialized delivery order для equal-millisecond rows.
- `get_market_trade_path()` сортировал ties по `seq, trade_id`, меняя фактически доставленный порядок.
- Path lookup выбирал rows по symbol/time без exact WebSocket session filter, что допускало смешение с REST rows.
- Межсообщенческая проверка использовала exchange-generated `ts`, хотя локальный message delivery index является более надёжным source of truth.

## 8. Baseline

Environment:

- Python `3.13.5`
- Node `v22.16.0`
- Production Python files: 27
- Test files: 224
- Docs: 103
- Max prior iteration: 278

Commands/results:

- `python -m compileall -q app tests main.py` — PASSED.
- `node --check app/ui/static/app.js` — PASSED.
- `python -m pytest --collect-only -q` — 1341 collected.
- Monolithic `python -m pytest -q` — TIMED OUT by harness after partial progress; no failure summary, not counted as pass.
- Exhaustive deterministic execution: 12 disjoint file batches; one combined batch was additionally verified file-by-file due harness timeout. Union equals all 224 test files and all 1341 collected items; 1341 passed.

## 9. RED evidence

Command:

```bash
python -m pytest -q tests/test_iteration279_public_trade_ordering.py
```

On pristine v1.4.8 with only the new regression file:

```text
3 failed in 0.18s
ValueError: non-monotonic public trade WebSocket row order
AssertionError: missing stream_session_id / stream_message_index / stream_row_index / stream_message_ts_ms
```

## 10. Реализация

### `app/trade_stream.py`

- Parser validates only non-decreasing documented match timestamp `T`.
- Equal timestamps retain input list order.
- Each row receives `stream_row_index`.
- Session assigns monotonically increasing `stream_message_index` and immutable `stream_session_id`.
- Session stats no longer regress when exchange message timestamp repeats/decreases.

### `app/db.py`

- Added nullable order fields and idempotent existing-schema upgrader.
- Stream batch validates exact session/message/row metadata.
- Direct internal callers without explicit metadata receive safe deterministic defaults for backward compatibility.
- Coverage stores `last_message_index` and `ordering_basis`.
- Coverage end and last-poll timestamp never regress.
- WebSocket path prefers exact session rows ordered by message/row index.
- Existing REST row can be enriched with first WebSocket ordering metadata without duplicating trade identity.

### `app/outcomes.py`

- WebSocket path validates local delivery keys and non-decreasing trade event time.
- REST path preserves prior conservative ordering behavior.
- Observation provenance bumped to `grid_intrabar_observation_v3`.

### Database

Added nullable columns to SQLite/PostgreSQL:

- `stream_session_id`
- `stream_message_index`
- `stream_row_index`
- `stream_message_ts_ms`

Added index `idx_market_trade_stream_order`.

## 11. Изменённые файлы

### Production

- `app/trade_stream.py`
- `app/db.py`
- `app/outcomes.py`
- `app/main.py`

### Frontend

- `app/ui/static/index.html` — cache/version token only.

### Database/migrations

- `migrations/init.sql`
- `migrations/init_postgres.sql`

### Tests

- New: `tests/test_iteration279_public_trade_ordering.py`
- Existing exact app-version assertions updated from 1.4.8 to 1.4.9.

### Docs

- `README.md`
- `CHANGELOG.md`
- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- this report.

Operator DOCX/PDF/PNG were not changed because operator workflow, statuses and controls are unchanged.

## 12. GREEN evidence

Targeted command:

```bash
python -m pytest -q tests/test_iteration279_public_trade_ordering.py
```

Result, repeated deterministically:

```text
4 passed
4 passed
```

Relevant public-trade/outcome/PostgreSQL checks were green. The complete post-check below is authoritative.

## 13. Post-check

- `python -m pytest --collect-only -q` — 1345 collected.
- Exhaustive 12 disjoint batches: `111 + 99 + 88 + 72 + 89 + 118 + 111 + 187 + 124 + 154 + 103 + 89 = 1345 passed`.
- Failed: 0.
- Skipped: 0.
- `python -m compileall -q app tests main.py` — PASSED.
- `node --check app/ui/static/app.js` — PASSED.
- Target regression repeated — 4 passed twice.
- `python -m pip check` — FAILED on pre-existing shared-environment conflict: MoviePy 2.2.1 requires Pillow `<12`, installed Pillow 12.2.0.
- `python -m ruff check .` — UNAVAILABLE (`No module named ruff`).
- Live PostgreSQL integration — SKIPPED; no verified disposable DSN.
- Live Bybit WebSocket smoke — SKIPPED; release tests use deterministic offline fixtures and no production credentials.

## 14. Compatibility and user actions

- Stop v1.4.8 and back up the DB.
- Install/run v1.4.9 normally.
- `init_db()` adds nullable fields/index automatically.
- No manual SQL, `.env` change, outcome deletion or calibrator reset is required.

## 15. Security boundary

No private Bybit order endpoint, credentials, auto-execution, OMS/EMS behavior or external data upload was added. Stream remains public/read-only.

## 16. Residual risks

- Public delivery order is not account fill order or queue priority.
- Legacy v1.4.8 coverage cannot be retroactively promoted to v3 evidence without stored order fields.
- Reconnect gaps remain explicit and are never bridged.
- Live throughput/reconnect behavior remains environment-dependent and was not network-smoke-tested here.

## 17. Rollback

Stop v1.4.9, restore v1.4.8 application files and restart. New nullable columns/index may remain; v1.4.8 ignores them. Do not delete recommendations or outcomes.

## 18. Recommended next work package

Add bounded stream metrics for messages/trades per second, reconnect reason distribution and coverage age alerts, without changing strategy/model semantics.
