# Audit iteration 278: funding settlement recovery and public trade chronology journal

## 1. Название итерации

`v1.4.8 - funding settlement recovery + public trade chronology + fail-closed intrabar replay`.

## 2. Входной ZIP

`bybit-reco-systems-1.4.7-direction-observability-journal(1).zip`.

## 3. SHA-256 входного ZIP

`69c109cb8842f34fd6651f75ac872f2f202b19389df1e8beb35b6660895e315b`.

## 4. Исходная версия

`1.4.7`, source of truth: `app/main.py`, параметр `version=` конструктора FastAPI.

## 5. Новая версия

`1.4.8` (patch release). Торговая модель, score, features, router, risk policy, target labels и calibrator inputs не изменены. `RECOMMENDER_MODEL_VERSION` остаётся `bybit-taxonomy-v13-log-symmetric-direction`, `OUTCOME_LABEL_VERSION` остаётся `grid_label_v26`. Очистка БД, удаление outcomes и сброс calibrator artifacts не требуются.

## 6. Project fingerprint

Fingerprint подтверждён. Единственный project root содержит README, CHANGELOG, requirements, `main.py`, `app/main.py`, `app/recommender.py`, `app/trading_semantics.py`, `app/grid_math.py`, `app/risk.py`, `app/calibration.py`, `app/trend_events.py`, `app/strategy_router.py`, `app/outcomes.py`, `app/db.py`, `app/db_backend.py`, `app/bybit_client.py`, frontend, tests, docs и обе migration SQL-схемы.

Scope сохранён:

- venue: Bybit;
- category: `linear`;
- instruments: USDT perpetual;
- strategy families: `futures_grid`, `directional_trend`;
- persistence: SQLite и PostgreSQL;
- service boundary: recommendation/audit-only, без private order create/amend/cancel.

Входной архив: 370 entries. Absolute path, `../` traversal, external symlink, duplicate/conflicting path и подозрительный вложенный archive не обнаружены. Входной ZIP не изменялся; созданы отдельные pristine, red-test и working copies.

## 7. Цель итерации

После этой итерации система должна:

1. не блокировать повторный funding-history запрос на час после временной ошибки;
2. создавать durable idempotent repair job для конкретного отсутствующего funding settlement или диапазона;
3. восстанавливать недавние funding gaps через overlap refresh, даже если более новый settlement уже существует;
4. собирать строгий public trade journal для активного Linear-USDT universe через read-only `publicTrade.{symbol}` WebSocket;
5. закрывать coverage на каждом disconnect/restart и никогда не мостить неизвестный интервал; REST fallback признавать непрерывным только при доказанном overlap соседних snapshots;
6. использовать public trade chronology для grid intrabar replay только при полном coverage и OHLC consistency;
7. сохранять fail-closed censoring при gap, malformed data, OHLC mismatch или остаточной execution ambiguity;
8. показывать repair/journal state в `/api/v1/status` и Health UI;
9. сохранить существующие outcomes и model/calibrator lineage.

## 8. Критерии приёмки

- Failed regular funding refresh повторяется через 60 секунд, а не через 3600 секунд.
- Один и тот же missing settlement создаёт одну repair identity.
- Repair worker сохраняет фактический settlement и переводит job в `resolved`.
- WebSocket parser отклоняет malformed envelope/rows, cross-symbol data, future timestamps, duplicate/non-monotonic trade identity и неверную side/numeric semantics.
- Каждая WebSocket session создаёт отдельный coverage span; disconnect/restart закрывает его, а новая session не продолжает старый span.
- Recent-trade fallback отклоняет boolean/fractional/malformed server time, future trades, неверный symbol и non-finite price/qty. Первый REST snapshot создаёт warm-up coverage; следующий расширяет только собственный source-span при common trade ID; отсутствие overlap закрывает span с `gap_reason`.
- Full-minute public trade path с first/max/min/last, совпадающими с OHLC, разрешает доказуемый dual kill-switch order.
- Без полного coverage прежний `intrabar_extreme_order_unobservable` остаётся censored.
- SQLite fresh/upgrade и обе SQL-схемы содержат новые таблицы.
- Production Health renderer исполняется в Node и показывает funding repair/trade journal diagnostics.
- Model/outcome-label identities не изменяются.
- New tests RED на pristine/red copy и GREEN на working copy.
- Union exhaustive post-check batches равен exact collected set.

## 9. Прочитанные источники

Прочитаны релевантные разделы README, CHANGELOG, requirements, requirements-dev, `.env.example`, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, последние audit reports, приложенный iterative protocol, `app/collector.py`, `app/bybit_client.py`, `app/outcomes.py`, `app/db.py`, `app/db_backend.py`, `app/main.py`, `app/settings.py`, canonical trading/risk/calibration modules, frontend и связанные regression tests. Исполняемый код и фактические схемы использованы как primary factual source.

## 10. Карта затронутого data flow

Funding recovery:

`outcome detects missing settled funding -> reco_outcome_observability waiting -> funding_settlement_repair durable job -> hot collector targeted funding-history query -> funding_settlement upsert -> job resolved/pending retry -> outcome worker retries unchanged label contract`.

Regular gap recovery:

`hot collector -> funding history with bounded two-day overlap -> immutable funding_settlement upsert`.

Intrabar chronology:

`Bybit publicTrade.{symbol} WebSocket -> strict parser -> session-bounded market_trade_coverage -> market_trade upsert`.

Fallback/bootstrap: `Bybit /v5/market/recent-trade -> strict client sanitation -> source-isolated overlap-aware coverage`.

Outcome: `full-minute coverage lookup -> OHLC consistency check -> chronological price replay -> existing conservative ledger`.

Health:

`repair/coverage DB status -> /api/v1/status -> loadHealth production renderer`.

Не изменялись candidate generation, direction aggregation, score, grid/trend geometry, sizing, risk gates, profitability router, first-touch target classes или calibration feature vector.

## 11. Baseline environment

- Python `3.13.5`.
- Node `v22.16.0`.
- Production Python files: 26 baseline; 27 post-change (`app/trade_stream.py` added).
- Baseline test files: 223.
- Baseline collection: 1322 tests.
- Docs files: 102.
- Frontend files: 3.
- Migration SQL files: 2.
- Maximum pre-existing iteration number: 277.
- `pip check`: shared-host conflict, `moviepy 2.2.1` requires `Pillow <12`, installed `Pillow 12.2.0`.
- `ruff`: unavailable (`No module named ruff`).

## 12. Baseline commands и точные результаты

- Archive SHA-256 and safety scan: PASSED.
- Project fingerprint: PASSED.
- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest --collect-only -q`: 1322 collected.
- Monolithic baseline `pytest -q`: TIMED OUT in harness near 49%; not counted as pass.
- Protocol-compliant exhaustive baseline: 24 non-overlapping deterministic batches, union = 1322 collected nodes; 1322 passed, 0 failed, 0 skipped; sum batch durations 134.98 s.
- `python -m pip check`: FAILED only due the pre-existing shared-host moviepy/Pillow conflict.
- `python -m ruff check .`: UNAVAILABLE.
- Network/public Bybit smoke: NOT RUN; offline fixtures used.
- Live PostgreSQL integration: SKIPPED; no explicitly disposable DSN provided.

## 13. Подтверждённые defects/gaps

### D-278-01 - HIGH - CONFIRMED DEFECT - failed funding attempt caused one-hour lockout

- Files/lines: `app/collector.py` baseline state around the old `_LAST_FUNDING_SETTLEMENT_FETCH_TS`; fixed in `app/collector.py:22,500-586`.
- Function: `_fetch_funding_settlements_for_symbol`.
- Input: transient transport/API exception on the first funding-history request.
- Actual baseline behavior: last-attempt timestamp was persisted in memory before the request; the next call within 3600 seconds returned without retry.
- Expected behavior: failed attempts use bounded short retry; only successful refresh uses the normal hourly cadence.
- Violated invariant: background network operation must expose timeout/retry diagnostics and not silently defer recoverable data for a full refresh period.
- Financial/model impact: outcomes could remain `WAIT_FUNDING_SETTLEMENT` for up to an hour despite Bybit already publishing the settlement; evidence maturity and calibration sample arrival were delayed.
- Why tests missed it: prior tests verified history pagination and waiting semantics but not failure-then-retry timing.
- RED: missing `_FUNDING_SETTLEMENT_FETCH_STATE` and short-retry behavior.
- Fix: separate `last_attempt_ts`, `last_success_ts`, `next_retry_ts`, `failure_count`, `last_error`; backoff `60/120/300/600/1800` seconds.
- GREEN: `test_failed_funding_history_attempt_uses_short_retry_not_hourly_lockout` passed.
- Residual risk: process-local regular refresh state resets on restart; durable outcome repair state does not.

### D-278-02 - HIGH - CONFIRMED GAP - no targeted durable funding repair and incremental gaps could be stranded

- Files/lines: `app/db.py:5172-5346`, `app/collector.py:588-659`, `app/outcomes.py:1002-1035`.
- Input: outcome detects `missing_funding_settlement` after a newer settlement already exists in the local table.
- Actual baseline behavior: regular incremental start was `latest + 1`; an older missing row was no longer in the fetch window. Outcome only logged/waited; no durable exact repair request existed.
- Expected behavior: exact symbol/timestamp repair job, retry state, diagnostics and bounded overlap refresh.
- Violated invariant: missing mandatory market data remains fail-closed but must have an observable recovery path.
- Financial/model impact: potentially permanent waiting/censoring and incomplete outcome evidence.
- Why tests missed it: no old-gap fixture with a later local settlement.
- RED: absent repair table/functions and absent worker.
- Fix: additive `funding_settlement_repair`, idempotent repair identity, queue worker, overlap history refresh, status API.
- GREEN: idempotence and worker-resolution tests passed.
- Residual risk: Bybit history retention/availability can still leave old jobs pending; the system does not fabricate a rate.

### D-278-03 - MEDIUM - CONFIRMED GAP - no durable public chronology for intrabar grid order

- Files/lines: `app/bybit_client.py:439-516`, `app/db.py:5349-5680`, `app/collector.py:662-707`, `app/outcomes.py:1652-1811`.
- Input: one-minute candle touches both relevant extremes or both kill-switch boundaries.
- Actual baseline behavior: the conservative outcome engine only knew OHLC; when admissible extreme paths produced different ledgers it returned `intrabar_extreme_order_unobservable`/related censoring.
- Expected behavior: use exact public-trade chronology where continuous coverage is independently proved; otherwise preserve censoring.
- Violated invariant: do not reconstruct unknown ordering, but use stronger available public evidence with explicit provenance.
- Financial/model impact: fewer avoidably censored grid outcomes; more accurate ordering for public price path, without claiming actual fills.
- Why tests missed it: no trade journal, coverage contract or replay source existed.
- RED: absent client, tables, coverage and replay.
- Fix: strict read-only `publicTrade.{symbol}` WebSocket parser/session, session-bounded coverage, source-isolated REST fallback, immutable trade rows, retention, OHLC consistency and chronological replay.
- GREEN: exact dual-boundary path resolved; missing coverage remained censored.
- Residual risk: public trade history does not prove queue priority, partial fills, latency, actual exchange fills or replacement-order activation time.

### D-278-04 - MEDIUM - CONFIRMED DIAGNOSTIC GAP - operator could not distinguish recovery state from unknown status

- Files/lines: `app/main.py:8405-8476`, `app/ui/static/app.js:3802-3875`.
- Actual baseline behavior: Health did not expose repair queue, next retry, trade-row coverage/gaps or evidence boundary.
- Expected behavior: one operator-facing readiness view with actionable diagnostics.
- Operational impact: a missing local settlement could be misread as a Bybit fault; trade chronology completeness was invisible.
- Fix: additive status payload and production Health renderer rows.
- GREEN: Node-executed renderer test verifies displayed repair/journal values.

## 14. Неподтверждённые claims

- Скриншот `missing_funding_settlement` сам по себе не доказал ошибку Bybit. Подтверждён локальный recovery defect/gap.
- Скриншот `intrabar_extreme_order_unobservable` не был ошибкой Bybit; baseline censoring was intentionally fail-closed.
- Не подтверждена ошибка funding sign, LONG/SHORT PnL sign, TP/SL geometry, grid ledger fees или router EV.
- Не заявляется, что trade journal reconstructs actual bot fills.
- Не заявляется прибыльность, live edge или production-readiness auto-execution.

## 15. План исправления

1. Зафиксировать baseline and archive safety.
2. Создать iteration 278 regression suite on red copy.
3. Разделить successful funding cadence и failed retry.
4. Добавить durable targeted funding repair and bounded overlap.
5. Добавить strict read-only publicTrade WebSocket parser/session и supervised runtime loop.
6. Сохранить strict recent-trade REST fallback/bootstrap.
7. Добавить dual-DB trade/coverage persistence, source/session isolation and retention.
8. Интегрировать full-coverage OHLC-consistent replay в existing grid ledger.
9. Сохранить all fail-closed fallbacks and model/label identity.
10. Добавить status/UI diagnostics and execute renderer in Node.
11. Синхронизировать migrations, config, docs, operator artifacts and iterative protocol.
12. Выполнить exhaustive post-check and clean ZIP verification.

## 16. Фактический diff по файлам

Production:

- `app/bybit_client.py` - strict public recent-trade fallback snapshot client.
- `app/trade_stream.py` - strict read-only publicTrade WebSocket parser/session and disconnect-bounded coverage.
- `app/collector.py` - funding retry state, overlap refresh, repair worker, REST fallback polling/pruning.
- `app/db.py` - funding repair queue, market trades, coverage spans, status/pruning/query helpers.
- `app/outcomes.py` - repair scheduling, observation version/provenance, exact public path replay.
- `app/settings.py` - five bounded configuration options, including explicit stream enable/disable.
- `app/main.py` - version 1.4.8, collector + supervised public trade stream wiring, additive status payload.

Frontend:

- `app/ui/static/app.js` - Health repair/journal diagnostics.
- `app/ui/static/index.html` - cache build token 1.4.8.

Database/migrations:

- `migrations/init.sql`.
- `migrations/init_postgres.sql`.
- New additive tables: `funding_settlement_repair`, `market_trade`, `market_trade_coverage`.

Tests:

- New `tests/test_iteration278_funding_recovery_trade_journal.py` with 19 tests.
- Minimal version/document expectation updates in exact-contract tests for 1.4.8.

Configuration/docs:

- `requirements.txt`, `.env.example`, README, CHANGELOG.
- KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC.
- `docs/Bybit_Recommender_Iteration_Prompt.md` and regenerated root PDF.
- operator DOCX/PDF.
- this audit report.

## 17. Red -> Green evidence

RED on red copy:

- Command: `python -m pytest -q tests/test_iteration278_funding_recovery_trade_journal.py`.
- Result: `17 failed, 2 passed in 0.87s`, exit code 1.
- Representative lines:
  - `AttributeError: module 'app.collector' has no attribute '_FUNDING_SETTLEMENT_FETCH_STATE'`;
  - `AttributeError: module 'app.db' has no attribute 'request_funding_settlement_repair'`;
  - `AttributeError: 'BybitPublicClient' object has no attribute 'get_recent_public_trades'`;
  - `sqlite3.OperationalError: no such table: funding_settlement_repair`;
  - Health renderer missing `Funding settlement recovery`.

GREEN on working copy:

- Command: `python -m pytest -q tests/test_iteration278_funding_recovery_trade_journal.py`.
- Result: `19 passed in 0.74s`, exit code 0.
- Deterministic repeat: `19 passed in 0.72s`, exit code 0.

Relevant module suite:

- Command includes funding settlement, grid path ambiguity/replacement/queue, outcome worker, UI/docs and iteration 278 tests.
- Result: `146 passed in 4.95s`.

## 18. Database/schema compatibility

Schema changes are additive and idempotent in runtime bootstrap plus both SQL references. Existing tables/rows are not dropped or rewritten. Fresh SQLite and existing SQLite upgrade are tested. PostgreSQL SQL/dialect paths are tested; live PostgreSQL integration is skipped because no explicitly disposable DSN was supplied.

User action:

1. stop the 1.4.7 process;
2. back up the database;
3. start 1.4.8 once so `init_db()` adds the three tables;
4. verify `/api/v1/status` fields `funding_settlement_repair` and `market_trade_journal`.

No manual SQL and no down-migration are required.

## 19. API compatibility

Existing routes and field names are preserved. `/api/v1/status` receives additive fields:

- `outcome_observation_version`;
- `funding_settlement_repair`;
- `market_trade_journal`.

No private Bybit order endpoint was added. No real order is created, amended or cancelled.

## 20. Config/env compatibility

New optional variables with safe defaults:

- `FUNDING_REPAIR_MAX_PER_CYCLE=16`;
- `MARKET_TRADE_JOURNAL_ENABLED=1`;
- `MARKET_TRADE_STREAM_ENABLED=1`;
- `MARKET_TRADE_POLL_LIMIT=1000`;
- `MARKET_TRADE_RETENTION_HOURS=72`.

Existing `.env` remains valid; defaults activate the new recovery/journal path. `MARKET_TRADE_STREAM_ENABLED=0` disables only the WebSocket primary and keeps the fail-closed REST fallback; `MARKET_TRADE_JOURNAL_ENABLED=0` disables both chronology collection paths, accepting continued OHLC-only censoring. No secrets or private account permissions are required.

## 21. Security boundary

- Only public/read-only Bybit REST and WebSocket endpoints are used.
- No order create/amend/cancel methods were added.
- No production credentials or `.env` are included in release.
- Exact symbol/category and strict numeric validation are enforced.
- Malformed server time, future trade, cross-symbol row and non-finite values fail closed.
- Public chronology is labelled as public-price evidence only.

## 22. Post-check commands и точные результаты

- `python -m pytest --collect-only -q`: 1341 collected.
- Exhaustive deterministic post-check: 24 non-overlapping batches, union = 1341 collected nodes; 1341 passed, 0 failed, 0 skipped; sum pytest-reported batch durations 119.02 s.
- New regression repeated twice: 19 passed + 19 passed.
- Relevant outcome/funding/grid/UI/docs suite: 146 passed.
- SQLite/PostgreSQL schema/dialect-focused suite: 69 passed.
- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- Production Health renderer executed in Node: PASSED.
- Operator DOCX rendered: 20 pages, visually inspected; derived PDF: 20 pages.
- Iterative protocol PDF regenerated: 23 pages, all-page contact sheet and detailed new-section page inspected.
- `python -m pip check`: FAILED only due pre-existing shared-host moviepy/Pillow conflict.
- `python -m ruff check .`: UNAVAILABLE.
- Live PostgreSQL integration: SKIPPED.
- Network smoke tests: NOT RUN.

## 23. Что не удалось проверить и почему

- Live Bybit WebSocket reconnect/throughput and REST fallback under sustained market load were not run; tests use deterministic fixtures and no production credentials/network dependency.
- Live PostgreSQL integration was not run because an explicitly disposable DSN was not provided.
- `ruff` could not run because it is absent from the environment.
- Global host `pip check` is not green because of an unrelated pre-existing moviepy/Pillow version conflict.
- Public WebSocket availability and REST recent-trade retention/rate limits can vary externally; runtime errors remain visible and fail closed.

## 24. Остаточные риски

- WebSocket disconnects, process downtime or upstream message loss create explicit coverage boundaries; the service never bridges them. REST fallback can also gap on very high-volume symbols if more than 1000 trades occur between successful snapshots; overlap detection closes coverage rather than guessing.
- Public trades do not prove queue priority, partial fills, latency, actual bot fill state or replacement activation timing.
- A complete public path may still be censored by the existing execution-ambiguity guards.
- Funding repair can remain pending if Bybit no longer exposes the historical settlement.
- Market trade journal increases DB write volume; 72-hour pruning bounds normal retention but must be monitored for the configured universe.
- Existing outcomes are preserved; newly observable future/retried rows can change aggregate counts naturally as waiting/censored evidence matures. This is not a model reset.

## 25. Rollback procedure

1. Stop v1.4.8.
2. Preserve a backup of DB and audit logs.
3. Restore v1.4.7 application files/assets.
4. Do not delete or rewrite 1.4.8 recommendations/outcomes.
5. The three additive tables may remain unused; no down-migration is necessary.
6. Restart and verify `app_version=1.4.7`.
7. If desired before rollback, disable new collection with `MARKET_TRADE_JOURNAL_ENABLED=0`; this does not alter existing rows.

## 26. Рекомендуемый следующий work package

Add a bounded market-data capacity dashboard and retention sizing study using actual per-symbol stream throughput: DB growth/hour, WebSocket reconnect/gap rate, REST fallback overlap failure rate, receive latency and percentage of censored grid outcomes recovered. This must remain observability/operations work and must not weaken execution ambiguity guards.
