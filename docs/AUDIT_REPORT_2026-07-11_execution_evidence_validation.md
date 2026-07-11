# Аудит execution evidence, realised PnL и live-validation integrity — v1.0.16

## 1. Название итерации

Execution evidence and realised PnL integrity.

## 2. Входной ZIP

`bybit-reco-systems-1.0.15-signal-durability-identity.zip`

## 3. SHA-256 входного ZIP

`9a35dab2eb8da4c1b2533219291d4bff5b4d09a17bcb93a3a218a541c4c178d2`

## 4. Исходная версия

`1.0.15`, source of truth: `FastAPI(..., version=...)` в `app/main.py`.

## 5. Новая версия

`1.0.16` (patch с additive schema/API extension).

## 6. Project fingerprint

Fingerprint совпадает с Bybit Recommender: обязательные production-модули, frontend, tests, dual SQLite/PostgreSQL persistence, единственный штатный `futures_grid`, Bybit `category=linear` USDT perpetual и recommendation/audit-only boundary присутствуют. Private order create/amend/cancel/batch endpoints и SDK-вызовы размещения ордеров в production-коде не обнаружены.

## 7. Цель итерации

После итерации проект должен уметь доказуемо связать исходную immutable-рекомендацию с фактическими execution/funding events и корректным realised net PnL, не превращаясь в OMS/EMS. Risk/drawdown/cooldown должны использовать тот же de-duplicated поток, а live-validation export не должен выдавать descriptive statistics за доказательство live edge.

## 8. Критерии приёмки

1. Каждый fill хранится отдельным immutable event с unique Bybit `execId`, `orderId` и прямой связью `bot_id -> origin_rec_id`.
2. Funding хранится отдельным signed transaction-log event с собственным external id.
3. Канонический net для actual fills равен `gross_pnl + funding - fee`; fill-based slippage не вычитается повторно.
4. Execution-quality deviation рассчитывается относительно отдельного timestamped benchmark, а не `orderPrice`.
5. Exact evidence и legacy `/trades` нельзя смешивать для одного bot; risk stream защищён от double count даже для исторически смешанной БД.
6. Legacy funding участвует в realised PnL, daily drawdown и cooldown.
7. Evidence-read endpoints защищены admin authorization.
8. SQLite/PostgreSQL schema upgrades additive и idempotent; runtime DB не попадает в release ZIP.
9. Live-validation response явно остаётся descriptive-only и не утверждает прибыльность.
10. Full regression suite и повторно распакованный release проходят проверки.

## 9. Прочитанные источники

README, CHANGELOG, requirements, `.env.example`, `KNOWN_RISKS`, `TRADING_LOGIC`, `ARCHITECTURE`, `MODULES`, `SCENARIOS`, operator artifacts, последние audit reports, `app/db.py`, `db_backend.py`, `risk.py`, `main.py`, `outcomes.py`, `calibration.py`, `recommender.py`, frontend и релевантные tests. Для внешней семантики сверены официальные Bybit V5 документы Execution, Get Trade History, Closed PnL и Transaction Log: один order может иметь несколько executions; execution содержит `orderId`, `execId`, `execPrice`, `execQty`, `execFee`, `execPnl`; funding и fee имеют signed cashflow semantics.

## 10. Карта затронутого data flow

`immutable rec_id -> operator executed audit state -> bot_instance -> external read-only adapter -> execution/funding evidence API -> execution_evidence -> bot summary -> unified realised events -> daily PnL/drawdown/cooldown -> descriptive live-validation export`.

Legacy compatibility path:

`bot_instance -> /trades aggregate -> funding/fee diagnostics -> unified realised events`, только если у bot нет exact execution events.

## 11. Baseline environment

- Python `3.13.5`.
- Node `v22.16.0`.
- Input archive: 213 entries, один project root; absolute paths, traversal, symlinks и duplicate/conflicting paths не обнаружены.
- Input release содержал `data/app.runtime_locks.sqlite` (runtime artifact).
- Production Python files: 24 до итерации; test files: 147; frontend files: 3; migration SQL files: 2.
- Максимальный существующий iteration test: 203.

## 12. Baseline commands и результаты

| Проверка | Результат |
|---|---|
| `python -m pip check` | FAILED: environment-level `moviepy 2.2.1` требует `pillow<12`, установлена `12.2.0` |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest -q` | **840 passed in 21.67s**, exit 0 |

## 13. Подтверждённые defects/gaps

### D204-1 — HIGH — CONFIRMED GAP — отсутствовала evidence-grade связь recommendation с execution truth

- Файлы: исходные `app/db.py`, `app/main.py`, migrations.
- Фактическое поведение: проект принимал только aggregate trade row с `trade_id`, `bot_id`, timestamp, symbol, gross pnl и fee. Прямой immutable `origin_rec_id`, Bybit `execId`, `orderId`, fill price/qty, partial-fill identity и отдельные funding events отсутствовали.
- Ожидаемое: каждый exchange event должен быть independently addressable, idempotent и связан с конкретной recommendation lineage.
- Финансовое/model влияние: невозможно было отделить ошибку signal/model от execution loss, fee/funding effects и duplicate ingestion; невозможно построить достоверный live-validation dataset.
- Исправление: additive `execution_evidence` ledger, unique `(source, external_event_id)`, immutable bot/rec/symbol checks, separate execution/funding types, conflict-aware retries и exact export.

### D204-2 — HIGH — CONFIRMED DEFECT — funding отсутствовал в legacy net/risk accounting

- Файлы: исходные `app/db.py`, `app/risk.py`, trade API.
- Фактическое поведение: `realized_pnl_net = pnl - fee`; daily PnL, drawdown и loss cooldown не знали signed funding.
- Ожидаемое: для Linear USDT actual realised stream учитывать signed funding receipts/payments.
- Финансовое влияние: adverse funding мог скрываться, положительный funding — не отражаться; daily risk state мог быть неверным.
- Исправление: additive legacy columns `funding`, `slippage`; canonical legacy net `pnl + funding - fee`; единый stream используется risk status.

### D204-3 — HIGH — CONFIRMED DESIGN RISK — slippage мог быть посчитан дважды

- Доказанная математическая истина: Bybit `execPnl`/gross realised PnL основан на фактическом execution price. Повторное вычитание benchmark-to-fill slippage из такого PnL повторно списывает уже реализованное ухудшение цены.
- Ожидаемое: actual net = gross fill PnL + funding - fee. Slippage хранится как diagnostic execution-quality metric.
- Дополнительный риск: `orderPrice` не является универсальным arrival/decision benchmark, особенно для market/protective order semantics.
- Исправление: `orderPrice` хранится как exchange evidence; отдельно обязательны `benchmark_price`, `benchmark_ts`, `benchmark_source`; adverse deviation вычисляется по side/qty/fill, но не входит второй раз в net.

### D204-4 — HIGH — CONFIRMED DEFECT — новый exact ledger мог не влиять на risk engine

- Файл: `app/risk.py`, DB daily PnL helpers.
- Фактический риск: если external adapter пишет только exact evidence, прежний risk engine продолжал бы читать legacy `trades` и не увидел бы losses.
- Исправление: `list_realized_net_events()` и risk consumption переведены на общий de-duplicated stream.

### D204-5 — HIGH — CONFIRMED DEFECT — mixed ledgers создавали double-counting ambiguity

- Вход: один bot имеет aggregate `/trades` и exact execution rows.
- Фактический риск: один экономический результат мог учитываться дважды либо частично смешиваться с funding-only rows.
- Исправление: supported writes взаимно блокируются; exact retries остаются idempotent. Defensive historical read даёт exact executions приоритет и исключает legacy execution aggregates для того же bot.

### D204-6 — MEDIUM — CONFIRMED SECURITY GAP — exact identifiers требуют закрытого read path

- Exact evidence содержит exchange order/execution ids, PnL и raw metadata.
- Исправление: `/api/v1/execution-evidence` и `/api/v1/validation/live-evidence` используют admin-key/loopback authorization; regression проверяет 401 без ключа и 200 с ключом.

### D204-7 — MEDIUM — CONFIRMED RELEASE DEFECT — runtime SQLite попал во входной ZIP

- Файл во входном archive: `data/app.runtime_locks.sqlite`.
- Риск: release может переносить runtime lock/state artifact и создавать ложную стартовую конфигурацию.
- Исправление: reproducible `scripts/build_release.py` исключает `.db`, `.sqlite`, WAL/SHM и cache/build artifacts; regression проверяет archive entries.

## 14. Неподтверждённые claims

- В приложенном ZIP нет фактических user fills, funding transactions и account-level inventory, поэтому нельзя установить, какая доля прошлых убытков вызвана signal, market regime, execution, fee/funding или ручным переносом параметров.
- Нельзя подтвердить ни положительную, ни отрицательную долгосрочную expectancy по одному коду.
- Нельзя доказать, что выбранный benchmark полностью измеряет implementation shortfall: его корректность зависит от внешнего adapter и точки фиксации.

## 15. План исправления

1. Сформировать независимый iteration-204 regression на exact identities, funding/net math, no-double-count, risk integration, ledger mixing, authorization, schema upgrade и release hygiene.
2. Добавить additive execution/funding ledger в SQLite/PostgreSQL.
3. Расширить legacy rows funding и diagnostic slippage без breaking API removal.
4. Ввести unified realised event stream и перевести risk/drawdown/cooldown.
5. Сделать benchmark-to-fill deviation явной диагностикой и запретить `orderPrice` как неявный benchmark.
6. Добавить descriptive validation export с отрицательным profitability claim flag.
7. Синхронизировать docs/operator artifacts и собрать clean release.

## 16. Фактический diff по файлам

### Production
- `app/db.py`
- `app/risk.py`
- `app/main.py`

### Tests
- `tests/test_iteration204_execution_evidence_integrity.py`
- `tests/test_iteration112_redteam_integrity_and_bybit_meta.py` — fake PostgreSQL contract синхронизирован с новым fail-closed query.

### Database/migrations
- `migrations/init.sql`
- `migrations/init_postgres.sql`

### Release tooling
- `scripts/__init__.py`
- `scripts/build_release.py`

### Docs/operator artifacts
- `README.md`
- `CHANGELOG.md`
- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- `docs/instrukciya_operatora_bybit_recommender.docx`
- `docs/instrukciya_operatora_bybit_recommender.pdf`
- `how_to_trade.png`
- этот audit report.

### Frontend

Production frontend не изменялся: текущий UI не содержит bot execution ledger. Evidence доступен через защищённый API; это явно документированная operator/external-adapter boundary, а не скрытая UI-функция.

## 17. RED -> GREEN evidence

RED command на pristine v1.0.15:

```bash
python -m pytest -q tests/test_iteration204_execution_evidence_integrity.py
```

Существенные RED-строки:

```text
AttributeError: module 'app.db' has no attribute 'insert_execution_event'
assert 404 == 422
KeyError: 'realized_funding'
assert 404 == 401
ModuleNotFoundError: No module named 'scripts'
9 failed in 1.12s
```

GREEN command: тот же.

```text
9 passed in 1.26s
```

Relevant API/risk/DB/PostgreSQL subset:

```text
95 passed in 4.68s
```

## 18. Database/schema compatibility

- Fresh SQLite/PostgreSQL init SQL создаёт `execution_evidence` и новые legacy cost columns.
- Runtime `init_db()` additive/idempotent добавляет legacy `funding`, `slippage` и evidence `order_price`, `benchmark_price`, `benchmark_ts`, `benchmark_source`, если они отсутствуют.
- Existing SQLite upgrade regression проходит.
- PostgreSQL placeholder/PRAGMA translation и locking/persistence tests входят в relevant/full suite.
- Live PostgreSQL integration: SKIPPED, подтверждённый disposable DSN не предоставлен.
- Manual SQL не требуется; перед обновлением рекомендуется backup.

## 19. API compatibility

- Existing routes не удалены.
- Legacy `BotTradeRequest` получил additive `funding` и `slippage` fields.
- Добавлены:
  - `POST /api/v1/bots/{bot_id}/execution-evidence`;
  - `GET /api/v1/execution-evidence`;
  - `GET /api/v1/validation/live-evidence`.
- OpenAPI smoke: version `1.0.16`, 27 routes, 21 paths.
- Evidence read/write требует authorization where sensitive/mutating.

## 20. Config/env compatibility

Новых environment variables нет. Внешний adapter должен использовать существующий `ADMIN_API_KEY`. Реальные Bybit credentials этому repository не нужны и не должны помещаться в его `.env`/release.

## 21. Security boundary

Private order create/amend/cancel не добавлены. Evidence endpoint принимает данные от внешнего read-only adapter и не создаёт exchange action. Реальные credentials, live account calls и production DB в тестах не использовались. Sensitive identifiers защищены admin authorization.

## 22. Post-check commands и результаты

| Проверка | Результат |
|---|---|
| Targeted iteration-204 | **9 passed in 1.26s** |
| Relevant API/risk/DB/PostgreSQL subset | **95 passed in 4.68s** |
| `pytest --collect-only -q` | **849 tests collected** |
| `python -m pytest -q` | **849 passed in 24.38s**, exit 0 |
| `python -m compileall -q app tests main.py scripts` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| OpenAPI smoke | version 1.0.16; 27 routes; 21 paths |
| SQLite existing-schema upgrade | PASSED in iteration-204 regression |
| PostgreSQL dialect/locking subset | PASSED as part of 95-test relevant run |
| DOCX render/visual QA | 3 pages rendered; all inspected, no clipping/overlap |
| PDF render/visual QA | 3 pages rendered; all inspected, no clipping/overlap |
| `python -m pip check` | same environment-level MoviePy/Pillow conflict |
| Ruff | UNAVAILABLE |

## 23. Что не удалось проверить

- Реальные Bybit private execution/transaction responses пользователя и reconciliation completeness.
- Authenticated fee tier, all possible `extraFees`, rebates in every jurisdiction/account type.
- Live PostgreSQL against disposable server.
- Unrealised PnL, open order inventory, liquidation risk and account-level margin interaction.
- Strategy expectancy/Sharpe/drawdown/calibration reliability on actual independent fills.
- Ruff lint в текущем окружении.

## 24. Остаточные риски

1. External adapter отсутствует; incorrect bot attribution, missed pages/events or bad benchmark can still corrupt evidence before ingestion.
2. Normalized numeric summaries use floating persistence. External forensic archive should retain exact source decimal strings/raw payloads; fixed-scale decimal migration is a separate package.
3. Realised risk does not include mark-to-market open inventory and liquidation proximity.
4. Funding attribution can be ambiguous if an external account violates one-bot-per-symbol assumptions or overlaps positions outside this service.
5. Benchmark-to-fill deviation is only as valid as externally captured benchmark timestamp/source; it is not automatically an exchange truth field.
6. Descriptive validation without comparator and sample-sufficiency controls still cannot prove alpha.
7. Legacy `/trades` remains lower-evidence compatibility input and should not be used for new validation datasets.

## 25. Rollback procedure

1. Остановить сервис и сделать backup текущей DB.
2. Вернуть application archive v1.0.15 и перезапустить.
3. Не удалять additive table/columns: v1.0.15 их игнорирует; manual destructive downgrade не требуется.
4. Учесть, что v1.0.15 не будет читать новые execution-evidence rows, а funding снова выпадет из legacy risk accounting.

## 26. Рекомендуемый следующий work package

Создать отдельный read-only Bybit reconciliation adapter или import pipeline, который сохраняет raw execution/transaction payloads и exact decimal strings, доказывает pagination completeness, связывает события с bot/rec lineage и формирует frozen chronological validation dataset. Затем реализовать walk-forward evaluator с no-trade/comparator baseline, regime cohorts, calibration reliability, bootstrap confidence intervals и заранее заданным stop criterion. До появления достаточной фактической выборки технология остаётся hypothesis/recommendation layer, а не validated alpha.
