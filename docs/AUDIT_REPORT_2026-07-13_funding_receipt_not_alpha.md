# Audit iteration: funding receipt is not strategy alpha

## 1. Итерация

**Bybit Recommender 1.0.45 → 1.0.46: conservative settled-funding outcome semantics**

Дата проверки: **2026-07-13**.

## 2. Входной ZIP

`bybit-reco-systems-1.0.45-cross-symbol-temporal-independence.zip`

## 3. SHA-256 входного ZIP

`f0129f576db1dfb79402ebcd36da017dae7f2a658953e0874f765f3aef9c0863`

## 4. Исходная версия

`1.0.45`, source of truth: `FastAPI(..., version="1.0.45")` в `app/main.py`.

## 5. Новая версия

`1.0.46` (patch release).

Outcome contract: `grid_label_v18` → `grid_label_v19`.

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender:

- присутствуют `README.md`, `CHANGELOG.md`, `requirements*.txt`, `main.py`;
- FastAPI создаётся в `app/main.py`;
- поддерживается `futures_grid` для Bybit `category=linear`, USDT perpetual;
- canonical directional semantics находятся в `app/trading_semantics.py`;
- persistence поддерживает SQLite и PostgreSQL;
- frontend находится в `app/ui/static/`;
- присутствуют migrations для обоих DB backends и operator artifacts;
- private order create/amend/cancel flow не является частью проекта.

## 7. Цель итерации

После этой итерации положительное фактическое funding-зачисление не должно создавать или улучшать canonical proxy edge, который используется outcome labeling, monetary calibration и publication gates. Неблагоприятный funding должен по-прежнему полностью ухудшать proxy result. Полный signed settled funding должен оставаться доступным как диагностическая и exact-account величина.

## 8. Критерии приёмки

1. Плоский SHORT при положительном funding не получает `success=1` только из-за receipt.
2. Плоский LONG при отрицательном funding не получает `success=1` только из-за receipt.
3. Неблагоприятный settled funding продолжает уменьшать `ret`.
4. Диагностика различает signed settled funding и консервативный вклад в proxy-return.
5. Изменение label semantics инвалидирует старые outcomes и все текущие calibrators, включая direction calibrator.
6. Новый тест падает на pristine 1.0.45 и проходит на 1.0.46.
7. Полная коллекция тестов проходит без ослабления risk/publication gates.
8. DB schema, public API routes и `.env` contract не меняются.

## 9. Прочитанные источники

Релевантно прочитаны и сопоставлены:

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports;
- `app/outcomes.py`, `app/calibration.py`, `app/recommender.py`, `app/main.py`;
- `app/grid_math.py`, `app/risk.py`, `app/trading_semantics.py`;
- persistence/bootstrap paths в `app/db.py`, `app/db_backend.py`;
- funding/outcome tests iterations 209–226 и regression history;
- operator DOCX/PDF/PNG artifacts.

## 10. Карта затронутого data flow

`funding_settlements` → `_grid_outcome()` → settled funding cashflow по inventory side → `ret`/`success` → `reco_outcomes` → bot/global/direction calibration → confidence/readiness → publication/no-trade gate → operator UI.

До исправления один и тот же положительный funding cashflow одновременно являлся фактическим account cashflow и улучшал canonical strategy outcome. После исправления эти семантики разделены:

- signed funding: полная историческая cashflow truth;
- conservative proxy funding: `min(0, signed_cashflow)`, то есть receipt исключается, payment учитывается.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `v22.16.0`;
- Ruff: `UNAVAILABLE` в окружении;
- `pip check`: `FAILED` из-за внешнего конфликта MoviePy 2.2.1 / Pillow 12.2.0;
- production credentials и private Bybit calls не использовались;
- disposable live PostgreSQL DSN не предоставлен.

Во входном ZIP присутствовал release-мусор `data/app.runtime_locks.sqlite`; при импортах создавался локальный `data/app.db`. Оба файла исключены из итогового release.

## 12. Baseline commands и результаты

- `python -m compileall -q app tests main.py` — **PASSED**.
- `node --check app/ui/static/app.js` — **PASSED**.
- `python -m pytest --collect-only -q` — **1041 unique test nodes**.
- Монолитный pytest — **TIMED OUT / no final summary**, поэтому не засчитан как успешный.
- Исчерпывающий deterministic batched run:
  - 215 passed;
  - 197 passed;
  - 184 passed;
  - 198 passed;
  - 247 passed;
  - union: **1041/1041 passed**.

## 13. Подтверждённые defects/gaps

### FRNA-001 — funding receipt manufactures proxy alpha

- Severity: **HIGH**.
- Тип: **CONFIRMED DEFECT**.
- Файл: `app/outcomes.py`.
- Функция: `_grid_outcome()` / settled funding event application.
- Нарушенный инвариант: funding receipt не должен улучшать canonical score/expected outcome или превращать отрицательную/нулевую grid economics в положительную.

#### Входной payload

Плоская цена, отсутствие grid PnL и execution cost, одна funding settlement:

- SHORT + positive funding rate: short получает funding;
- LONG + negative funding rate: long получает funding.

#### Фактическое поведение 1.0.45

Signed receipt прибавлялся к `net_proxy`. При отсутствии grid profits это давало положительный `ret` и `success=1`.

#### Ожидаемое безопасное поведение

Receipt может отображаться как исторический account cashflow, но не должен улучшать canonical proxy `ret`, используемый для доказательства устойчивого strategy edge. Adverse funding должен учитываться полностью.

#### Финансовое и model/risk влияние

- временный carry мог выдаваться за эффективность grid-логики;
- funding-regime мог сформировать положительные outcomes без ценовой прибыли;
- monetary calibration и lower-bound gate могли открыться на receipt-driven data;
- смена знака funding после запуска могла устранить заявленный edge;
- положительная оценка могла быть результатом side exposure, а не grid execution.

#### Почему тесты не поймали

`tests/test_iteration225_settled_funding_outcomes.py` закреплял receipt-as-alpha expectation. Одновременно другой helper и документационный инвариант уже утверждали, что receipt не должен кредитоваться. Тесты по отдельности были зелёными, но контракт был внутренне противоречив.

### FRNA-002 — label reset missed current direction calibrator

- Severity: **HIGH**.
- Тип: **CONFIRMED DEFECT**.
- Файл: `app/main.py`.
- Функция: startup outcome-label version reset.

При изменении outcome label version удалялись bot/global calibrators и hard-coded legacy `platt_direction_v4`, но текущий `DIRECTION_CALIBRATION_KEY` указывал на `platt_direction_v6`. Direction calibrator мог пережить изменение label semantics и продолжить использовать коэффициенты, обученные на старых labels.

Исправление использует актуальный импортированный ключ и сохраняет удаление legacy key для совместимости.

### FRNA-003 — runtime lock database included in input release

- Severity: **LOW**.
- Тип: **CONFIRMED GAP**.
- Файл: `data/app.runtime_locks.sqlite`.

Runtime database не должна включаться в release ZIP. Она удалена из итогового архива вместе с локальной `data/app.db` и caches.

## 14. Неподтверждённые claims

- Не доказано, что стратегия априори убыточна.
- Не доказано наличие положительного live edge.
- Не проверена representative runtime-база exact fills, fees, settled funding, slippage и capital-at-risk: такой базы в release нет.
- Не проверен live PostgreSQL integration без явно disposable DSN.
- Не утверждается, что v1.0.46 устраняет все возможные ошибки grid economics.

## 15. План исправления

1. Добавить независимый red regression для двух receipt directions и adverse payment control.
2. Разделить signed funding cashflow и conservative proxy funding contribution.
3. Сохранить signed diagnostics, исключить только положительный contribution из `ret`.
4. Повысить outcome label version.
5. Сбрасывать все текущие calibrators по каноническим ключам.
6. Обновить tests, version assertions и operator documentation.
7. Выполнить полный batched post-check и clean ZIP re-extraction validation.

## 16. Фактический diff

### Production

- `app/outcomes.py` — separate signed/conservative funding accounting.
- `app/main.py` — version 1.0.46, `grid_label_v19`, current direction calibrator reset.

### Tests

- новый `tests/test_iteration234_funding_receipt_not_alpha.py`;
- обновлён settled-funding contract в iteration 225;
- синхронизированы exact version/outcome-label assertions в iterations 209, 211, 213–226.

### Documentation

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX/PDF/PNG;
- данный audit report.

### Database/migrations/frontend

Изменений schema, migrations и frontend source нет.

## 17. Red → green evidence

Red command на pristine source с новым regression test:

```bash
python -m pytest -q tests/test_iteration234_funding_receipt_not_alpha.py
```

Существенные red-результаты:

```text
assert 1 == 0
assert 1 == 0
assert "DIRECTION_CALIBRATION_KEY" in reset_block
3 failed, 1 passed
```

Green command:

```bash
python -m pytest -q tests/test_iteration234_funding_receipt_not_alpha.py
```

Green result:

```text
4 passed
```

Related funding/outcome suite:

```text
121 passed in 2.71s
```

## 18. Database/schema compatibility

- Schema не менялась.
- `migrations/init.sql` и `migrations/init_postgres.sql` не менялись.
- SQLite fresh bootstrap — **PASSED**.
- Repeated SQLite initialization — **PASSED**.
- Simulated existing v18 → v19 startup reset — **PASSED**:
  - control row сохранился;
  - старые outcomes удалены;
  - current bot/global/direction calibrators удалены;
  - outcome label version записана как `grid_label_v19`.
- PostgreSQL offline dialect/locking subset — **18 passed**.
- Live PostgreSQL — **SKIPPED**, disposable DSN отсутствует.

При первом старте v1.0.46 старые proxy outcomes и calibrators будут штатно очищены из-за изменения label contract. Это ожидаемое действие, а не schema migration.

## 19. API compatibility

Публичные routes и JSON field names не менялись. Добавлены только внутренние outcome diagnostics. Private order endpoints не добавлены.

## 20. Config/env compatibility

Новых или изменённых environment variables нет. `.env.example` contract не менялся. Действий пользователя с `.env` не требуется.

## 21. Security boundary

Recommendation/audit-only boundary сохранена. Реальные order create/amend/cancel endpoints и SDK calls не добавлены. Production credentials не использовались. Runtime DB и caches исключаются из release.

## 22. Post-check commands и результаты

- `python -m pytest --collect-only -q` — **1045 unique nodes**.
- Exhaustive deterministic batches:
  - 215 passed;
  - 198 passed;
  - 185 passed;
  - 199 passed;
  - 248 passed;
  - union: **1045/1045 passed**.
- Новый test — **4 passed**.
- Funding/outcome related suite — **121 passed**.
- PostgreSQL offline subset — **18 passed**.
- Python compileall — **PASSED**.
- JavaScript syntax — **PASSED**.
- DOCX render — **7 pages**, all inspected after final render.
- PDF render — **7 pages**, all inspected after final render.
- Version consistency — **PASSED**.
- Private endpoint static check — **PASSED**.
- Final ZIP `bybit-reco-systems-1.0.46-funding-receipt-not-alpha.zip` — `unzip -t` **PASSED**.
- Re-extraction — **PASSED**: exactly one project root; fingerprint and changed files present.
- Re-extracted targeted test — **4 passed**; related funding/release subset — **23 passed**.
- Final ZIP junk/secret scan — **PASSED**: no `.env`, runtime DB, runtime lock DB, bytecode or test caches.
- Ruff — **UNAVAILABLE**.
- `pip check` — **FAILED**, pre-existing MoviePy/Pillow environment conflict.

## 23. Что не удалось проверить

- actual live profitability;
- correctness/completeness of a real external execution reconciliation adapter;
- live PostgreSQL behavior;
- real Bybit funding/fill reconciliation without production/test credentials;
- statistical representativeness of real outcomes because runtime dataset is absent;
- Ruff full-project result because Ruff is not installed.

## 24. Остаточные риски

- Excluding positive funding from proxy validation is intentionally conservative; exact account PnL still must include both receipts and payments.
- A separate account-performance report must not reuse conservative proxy funding as exact cash truth.
- Positive proxy expectancy after this change still does not prove live profitability.
- Grid fill reconstruction, fee truth, slippage, queue priority and liquidation behavior remain proxies unless reconciled with exchange evidence.
- Old reports based on `grid_label_v18` and earlier cannot be treated as comparable to v19 without relabeling.

## 25. Rollback procedure

Code rollback to 1.0.45 requires no schema rollback. However it reintroduces receipt-as-alpha and incomplete direction-calibrator invalidation. Old v18 outcomes/calibrators deleted during first v1.0.46 startup are not automatically reconstructable unless restored from a backup; restoring them is not recommended for live decisions.

## 26. Рекомендуемый следующий work package

Проверить соответствие proxy grid ledger реальному exchange-attested account PnL:

- full fill cursor completeness;
- maker/taker fee and rebate truth;
- signed settled funding in exact account PnL;
- residual positions and open orders;
- capital-at-risk and margin utilization;
- reconciliation of proxy `ret` against exact finalized net return;
- purged monetary walk-forward and block bootstrap.

До завершения этого этапа система должна оставаться в paper/shadow mode.
