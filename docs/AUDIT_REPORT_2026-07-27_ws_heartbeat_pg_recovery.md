# Audit iteration: application heartbeat and PostgreSQL fallback recovery

## 1. Название итерации

`v1.4.12 — application heartbeat + REST coverage boundary + PostgreSQL savepoint recovery`.

## 2. Входной ZIP

`bybit-reco-systems-1.4.11-stream-resilience-warmup-noise.zip`.

## 3. SHA-256 входного ZIP

`c017cfd39d82ce3ca6f5d4f0132e83e0ccedd894f43b905bc48a4bb819def579`.

## 4. Исходная версия

`1.4.11`, source of truth: `app/main.py`, `FastAPI(... version="1.4.11")`.

## 5. Новая версия

`1.4.12` — patch release.

## 6. Project fingerprint

Fingerprint подтверждён:

- Bybit Recommender recommendation/audit service;
- `futures_grid` и `directional_trend`;
- Bybit `category=linear`, USDT perpetual;
- SQLite и PostgreSQL;
- FastAPI в `app/main.py`;
- frontend в `app/ui/static/`;
- private order-create/amend/cancel endpoints не добавлялись.

Inventory working copy:

- production Python files: 27;
- test files: 228;
- docs files: 106 до добавления release report / 107 после;
- frontend files: 3;
- migration SQL files: 2.

## 7. Цель итерации

После итерации система должна:

1. Не запускать параллельный internal protocol keepalive библиотеки `websockets` поверх Bybit JSON heartbeat.
2. Не печатать internal `keepalive ping failed` traceback из timer thread библиотеки.
3. Переподключаться fail-closed, если Bybit application heartbeat не получает ни одного входящего frame в configured timeout.
4. При REST snapshot с `oldest_trade_ts_ms == snapshot_ts_ms` сохранять валидный conservative zero-width coverage span.
5. После per-symbol PostgreSQL write error оставлять outer collector transaction usable, чтобы сохранить исходную диагностику вместо вторичной `current transaction is aborted`.
6. Не менять торговую model lineage, outcome label, observation provenance или существующие outcomes.

## 8. Критерии приёмки

- `websockets.sync.client.connect()` вызывается с `ping_interval=None`, `ping_timeout=None`.
- Bybit JSON `{"op":"ping"}` сохраняется.
- Receive watchdog возвращает `disconnect_reason=application_heartbeat_timeout`.
- REST equal-timestamp fixture создаёт `[ts+1, ts+1]`.
- PostgreSQL-like aborted-transaction fixture видит `ROLLBACK TO SAVEPOINT` и допускает следующий SQL.
- Новый targeted suite проходит дважды детерминированно.
- Exhaustive batch union равен collection set и полностью зелёный.

## 9. Прочитанные источники

- текущий пользовательский traceback и screenshot;
- `app/trade_stream.py`;
- `app/db.py`;
- `app/collector.py`;
- `app/main.py`;
- `app/db_backend.py`;
- `.env.example` и `app/settings.py`;
- tests iterations 109–112, 142, 248, 278–281;
- README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS;
- официальный Bybit WebSocket connect/heartbeat contract;
- официальная `websockets` sync-client документация для `ping_interval=None` / `ping_timeout=None`.

## 10. Карта затронутого data flow

`Bybit publicTrade -> websockets sync client -> JSON application heartbeat / receive watchdog -> parse_public_trade_message -> record_market_trade_stream_batch -> market_trade + coverage`.

Fallback path:

`stream inactive -> collector REST recent-trade -> record_market_trade_poll savepoint -> market_trade + source-isolated coverage -> commit / per-symbol rollback -> COLLECT_ERROR logging`.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `22.16.0`;
- websockets pinned: `16.0`;
- OS harness: Linux container; user runtime evidence: Windows / Python 3.12 / PostgreSQL.

## 12. Baseline commands и результаты

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pip check`: FAILED из-за pre-existing environment conflict: `moviepy 2.2.1` требует `pillow<12`, установлена `pillow 12.2.0`.
- `python -m ruff check .`: UNAVAILABLE, module `ruff` не установлен.
- `python -m pytest --collect-only -q`: 1352 collected.
- Монолитный baseline `pytest -q`: TIMED OUT примерно на 76%, failures до timeout не наблюдались; не засчитан как full run.
- Exhaustive deterministic 12-batch union: 1352 passed, 0 failed, 0 skipped, 0 errors.

## 13. Подтверждённые defects/gaps

### WS-282-A — parallel protocol keepalive emits internal traceback

- Severity: medium.
- Type: CONFIRMED DEFECT.
- File: `app/trade_stream.py`, `run_public_trade_stream_session()`.
- Input: Windows traceback `keepalive ping failed`, `ConnectionClosedError: sent 1011 ... keepalive ping timeout`.
- Actual: library protocol keepalive ran in a background timer in parallel with Bybit application ping. Even though outer code treated disconnect as normal, the library emitted its own traceback.
- Expected: one heartbeat contract; network liveness handled as a normal coverage-session boundary.
- Impact: misleading operator error stream, unnecessary connection closes, noisy diagnostics.

### DB-282-B — REST fallback repeats invalid exclusive-boundary bug

- Severity: high.
- Type: CONFIRMED DEFECT.
- File: `app/db.py`, `record_market_trade_poll()`.
- Reproducer: one REST trade with `trade_ts_ms == snapshot_ts_ms`.
- Actual: `coverage_start=ts+1`, `coverage_end=ts`, then `ValueError: invalid market trade coverage window`.
- Expected: preserve exclusive start and raise end to start, yielding zero-width evidence.
- Impact: REST fallback failure exactly when WebSocket disconnects.

### DB-282-C — caught per-symbol SQL error poisons PostgreSQL transaction

- Severity: high.
- Type: CONFIRMED DEFECT.
- Files: `app/db.py::record_market_trade_poll()`, caller `app/collector.py::_collect_market_trade_journal()`.
- Actual: collector catches per-symbol exception and attempts to log on same PostgreSQL transaction. Without savepoint rewind PostgreSQL rejects subsequent commands with `current transaction is aborted`, masking the first error and aborting the full collector cycle.
- Expected: atomic per-symbol unit with savepoint rollback before caller logs or continues.
- Impact: stale ticker/candle data, repeated `RECO_WARMUP_SKIP`, loss of primary diagnostic.

## 14. Неподтверждённые claims

- Не доказано, что каждый observed disconnect вызван PostgreSQL stall; сеть, proxy или Bybit также могут разрывать connection.
- Не доказано, что live Windows host больше никогда не увидит transport disconnect. Исправляется классификация, noise и recovery, а не внешняя сеть.
- Не выполнялся live Bybit soak в пользовательском окружении.

## 15. План исправления

1. Добавить RED fixtures для protocol keepalive kwargs, application heartbeat watchdog, REST equal-timestamp boundary и PostgreSQL transaction rewind.
2. Отключить library auto-ping, сохранив Bybit JSON heartbeat.
3. Добавить explicit no-frame watchdog.
4. Исправить REST effective coverage end.
5. Обернуть REST poll persistence в savepoint.
6. Синхронизировать version, tests и docs.
7. Выполнить exhaustive post-check и clean ZIP verification.

## 16. Фактический diff по файлам

### Production

- `app/trade_stream.py`;
- `app/db.py`;
- `app/main.py`.

### Frontend

- `app/ui/static/index.html` — cache/version bump only.

### Tests

- новый `tests/test_iteration282_ws_heartbeat_pg_recovery.py` — 4 tests;
- минимально исправлены obsolete keepalive expectations в iterations 278 и 281;
- current-version assertions синхронизированы с 1.4.12.

### Database/migrations

- migration SQL и schema не менялись;
- runtime data migration не требуется.

### Docs/config

- `.env.example` comments;
- README, CHANGELOG;
- KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS;
- этот audit report.

## 17. Red -> green evidence

RED command:

```bash
python -m pytest -q tests/test_iteration282_ws_heartbeat_pg_recovery.py --maxfail=4
```

RED summary on pristine 1.4.11:

```text
4 failed
ValueError: invalid market trade coverage window
RuntimeError: current transaction is aborted
assert 20.0 is None
stream_shutdown != application_heartbeat_timeout
```

GREEN command:

```bash
python -m pytest -q tests/test_iteration282_ws_heartbeat_pg_recovery.py
```

GREEN summary:

```text
4 passed in 0.09s
```

Repeated related package:

```text
34 passed
60 passed (PostgreSQL/savepoint + stream related suite)
```

## 18. Database/schema compatibility

- No new tables, columns, indexes or migrations.
- SQLite fresh database covered by targeted test.
- PostgreSQL dialect/savepoint behavior covered by fake aborted-transaction regression and existing PostgreSQL suites.
- Live PostgreSQL integration: SKIPPED; disposable DSN not provided.
- Existing `market_trade`, coverage, recommendations, outcomes and calibrators remain intact.

## 19. API compatibility

No route, schema, field name, recommendation status or operator action change.

## 20. Config/env compatibility

No new keys. Existing values remain valid:

- `MARKET_TRADE_STREAM_PING_INTERVAL_SEC=20` is Bybit JSON heartbeat cadence;
- `MARKET_TRADE_STREAM_PING_TIMEOUT_SEC=60` is no-frame receive watchdog;
- protocol auto-ping is disabled internally regardless of these values.

## 21. Security boundary

- Public/read-only Bybit endpoints only.
- No private key, order create/amend/cancel or auto-execution added.
- No credentials used in tests.
- No production DB used.

## 22. Post-check commands и результаты

- `python -m pytest --collect-only -q`: 1356 collected.
- Exhaustive deterministic 12-batch union: 1356 passed, 0 failed, 0 skipped, 0 errors.
- New targeted: 4 passed.
- Related stream package: 34 passed.
- PostgreSQL/savepoint + stream package: 60 passed.
- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pip check`: same pre-existing MoviePy/Pillow conflict.
- `ruff`: UNAVAILABLE.

## 23. Что не удалось проверить и почему

- Live Bybit WebSocket long-duration soak on Windows: environment unavailable.
- Live disposable PostgreSQL integration: safe DSN not provided.
- `ruff`: package unavailable in harness.

## 24. Остаточные риски

- A long synchronous DB stall beyond the heartbeat watchdog intentionally closes the current session and creates a coverage gap.
- Public trades do not prove queue priority, exact fills or partial fills.
- External network/proxy disconnects remain possible and require reconnect.

## 25. Rollback procedure

1. Stop v1.4.12.
2. Restore v1.4.11 files.
3. Restart service.
4. No schema rollback or data deletion required.

## 26. Рекомендуемый следующий work package

Run a controlled 6–24 hour Windows/PostgreSQL soak and add metrics for DB commit latency, WebSocket message backlog, heartbeat RTT, reconnect count and per-session gap duration. Do not change trading thresholds as part of that observability iteration.
