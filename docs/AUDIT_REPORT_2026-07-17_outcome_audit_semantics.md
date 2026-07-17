# Audit iteration: outcome audit semantics

## 1. Название итерации

**v1.0.76 — доказуемая терминальная причина proxy outcome и строгая numeric-семантика журнала.**

## 2. Входной ZIP

`bybit-reco-systems-main(1)(1).zip`

## 3. SHA-256 входного ZIP

`4577e2113c786285ce5679ee7b39a9ce4be568a63f755a70b954ed233fd10c07`

ZIP проверен до распаковки: 343 entries; CRC ошибок нет; absolute path, `../` traversal, symlink, duplicate/conflicting path и вложенные архивы не обнаружены; один root `bybit-reco-systems-main`.

## 4. Исходная версия

`1.0.75`, source of truth: `version=` при создании FastAPI в `app/main.py`.

## 5. Новая версия

`1.0.76` (patch). Frontend cache build синхронизирован с `1.0.76`.

## 6. Project fingerprint

Fingerprint совпал:

- recommendation/audit service, не OMS/EMS;
- единственный bot type: `futures_grid`;
- Bybit `category=linear`, USDT perpetual;
- FastAPI в `app/main.py`;
- frontend в `app/ui/static/`;
- canonical directional helpers в `app/trading_semantics.py`;
- SQLite и PostgreSQL через `app/db.py`/`app/db_backend.py`;
- обязательные README, docs, migrations и operator artifacts присутствуют;
- статический поиск не выявил private order create/amend/cancel endpoints или SDK-вызовов размещения ордеров.

### Выявленный конфликт протокола, не являющийся дефектом проекта

Раздел 3.2 приложенного протокола фиксирует `margin mode: isolated`. Текущий проект и его tests реализуют `cross` для Futures Grid. Официальная документация Bybit для Futures Grid Bot описывает cross-margin и one-way mode. По порядку доверия протокола фактический код и официальное внешнее поведение имеют приоритет над устаревшей проектной предпосылкой. Перевод проекта в isolated не выполнялся: это изменило бы торговую модель без доказанного основания.

## 7. Цель итерации

После итерации операторский журнал должен доказуемо объяснять, почему положительный расчётный net proxy P&L может иметь бинарный исход `Неуспех`, и не должен преобразовывать boolean/malformed значения в торговые числа.

## 8. Критерии приёмки

1. `_grid_outcome` формирует явный terminal reason и сторону kill-switch.
2. Labeled outcome сохраняет эти diagnostics в durable audit read model.
3. Enriched outcome API возвращает diagnostics без schema migration.
4. UI отдельно показывает исход стратегии, net proxy P&L и причину.
5. Boolean success/ret не превращаются в `1`, `100%` или `0%`.
6. Legacy outcome без terminal diagnostics не реконструируется предположением.
7. Новый test падает на pristine и проходит после fix.
8. Полный collected set тестов проходит исчерпывающими непересекающимися batches.

## 9. Прочитанные источники

- приложенный `Bybit_Recommender_Iteration_Prompt.pdf` (25 страниц, SHA-256 `1e2d759151c2df3ea6781ddcb9bead7c467d4b8c59a97546550be77a17415647`);
- README, CHANGELOG, `.env.example`, requirements;
- KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC;
- последние audit reports;
- `app/trading_semantics.py`, `grid_math.py`, `risk.py`, `recommender.py`, `calibration.py`, `outcomes.py`, `features.py`, `direction.py`, `regime.py`, `collector.py`, `bybit_client.py`, `db_backend.py`, релевантные части `db.py`, `main.py`, settings, LLM/security и frontend;
- regression tests по directional/grid/PnL/funding/calibration/concurrency/frontend.

## 10. Карта затронутого data flow

`OHLCV/funding settlements` → `_grid_outcome` → `(success, net_proxy_return, diagnostics)` → `compute_outcomes_cycle` → `db.insert_outcome` → `reco_outcomes` + `reco_outcome_observability.details_json` → `get_outcomes_recent_enriched` → outcomes API → `renderOutcomeResult` / `renderOutcomeReturn` / `outcomeReasonText`.

Execution preflight, recommendation publication gate, sizing, risk caps и calibration formulae не изменены.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `22.16.0`;
- production Python files: 24;
- test files: 208;
- collected tests: 1195;
- docs: 86;
- frontend files: 3;
- migration SQL files: 2;
- max iteration before work: 262;
- disposable PostgreSQL DSN не предоставлен; live PostgreSQL integration не запускался.

## 12. Baseline commands и точные результаты

| Команда | Результат |
|---|---|
| `python -m pip check` | FAILED: внешний environment conflict `moviepy 2.2.1` требует `pillow<12`, установлена `12.2.0`; проектные requirements это сочетание не задают |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 1195 collected, exit 0 |
| `python -m pytest -q` | TIMED OUT после 20 минут примерно на 34%; до timeout failure output отсутствовал |
| критический deterministic package | 215 passed in 11.10 s |

Baseline не объявлялся полным green suite.

## 13. Подтверждённые defects/gaps

### OA-263-01 — terminal diagnostics терялись после успешного label lifecycle

- Severity: **medium**.
- Тип: **CONFIRMED DEFECT**.
- Файлы pristine: `app/outcomes.py`, `app/db.py`.
- Функции: `_grid_outcome`, `compute_outcomes_cycle`, `insert_outcome`, `get_outcomes_recent_enriched`.
- Вход: valid grid outcome с `success=0`, `ret=+0.0135`, `stopped=true`, upper kill-switch.
- Фактическое поведение: `_grid_outcome` рассчитывал kill-switch boundary/extreme/liquidation diagnostics, но `compute_outcomes_cycle` не передавал их в `insert_outcome`; `insert_outcome` заменял details минимальным `bot_type`; enriched read model diagnostics не возвращал.
- Ожидаемое поведение: immutable outcome audit row должен сохранять terminal evidence и отдавать его оператору.
- Нарушенный инвариант: audit integrity, backend/frontend parity, fail-closed explainability.
- Финансовое влияние: денежная формула и label не искажались; искажалась интерпретируемость результата.
- Trading/risk влияние: оператор мог ошибочно решить, что `success` противоречит знаку P&L, и ослабить kill-switch semantics.
- Почему tests не поймали: tests проверяли формулу stop/P&L, но не durable passage diagnostics до enriched API.

### OA-263-02 — frontend принимал boolean за outcome number и скрывал unknown как 0%

- Severity: **medium**.
- Тип: **CONFIRMED DEFECT**.
- Файл pristine: `app/ui/static/app.js`.
- Фактическое поведение: `Number(true) === 1`; `Number(row.ret || 0)` мог показать `+100%` для boolean и `+0.00%` для missing/malformed.
- Ожидаемое поведение: boolean, blank, null, NaN и Infinity остаются unknown; явный numeric zero сохраняется.
- Нарушенный инвариант: строгая numeric-семантика frontend.
- Operational/UX влияние: misleading status/precision в операторском журнале.

## 14. Неподтверждённые claims и проверенные инварианты

В выбранном work package не подтверждены дополнительные critical/high дефекты в:

- canonical LONG/SHORT PnL и TP/SL mapping;
- arithmetic grid step/count semantics;
- qty floor rounding, minQty/minNotional и margin/notional caps;
- разделении fee/spread/slippage/funding;
- conservative funding sign и исключении funding receipt из canonical edge;
- purged chronological OOF, whole-timestamp terminal holdout и terminal-selected monetary gate;
- publication identity, one running bot per root, SQLite/PostgreSQL locks;
- absence of private order execution.

Это означает только отсутствие воспроизведённого дефекта в проверенном scope, а не доказательство абсолютной полноты проекта или live edge.

## 15. План исправления

1. Добавить независимый regression test в iteration 263.
2. Сохранить terminal diagnostics через уже существующий observability table, без schema migration.
3. Присоединить diagnostics к enriched outcomes read model.
4. Ввести frontend strict parsers для outcome success/return.
5. Разделить UI-колонки исхода, P&L и причины.
6. Синхронизировать operator documentation и patch version.

## 16. Фактический diff по файлам

### Production

- `app/outcomes.py` — terminal reason, breach side, net return и passage diagnostics.
- `app/db.py` — durable observability details и enriched API fields.
- `app/main.py` — version 1.0.76.

### Frontend

- `app/ui/static/app.js` — strict outcome parsing, reason rendering, ясные колонки.
- `app/ui/static/styles.css` — neutral unknown badge.
- `app/ui/static/index.html` — cache build 1.0.76.

### Tests

- новый `tests/test_iteration263_outcome_audit_semantics.py`;
- version assertions синхронизированы с 1.0.76;
- operator document assertion синхронизирован и усилен новой семантикой.

### Database/migrations

- schema и `migrations/*.sql` не изменялись;
- использован существующий `reco_outcome_observability.details_json`.

### Docs

README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, operator DOCX/PDF и этот report.

`how_to_trade.png` не изменён: изображение описывает readiness главной таблицы и не содержит журнала outcome. Связанный markdown и operator DOCX/PDF обновлены.

## 17. RED → GREEN evidence

### RED

Command:

```bash
python -m pytest -q tests/test_iteration263_outcome_audit_semantics.py
```

Pristine + только новый test:

```text
KeyError: 'stopped'
AssertionError: missing production JS function renderOutcomeReturn
2 failed in 0.21s
```

### GREEN

Та же команда после fix:

```text
2 passed in 0.21s
```

Повторный deterministic run:

```text
2 passed in 0.20s
```

## 18. Database/schema compatibility

- Schema change: нет.
- Runtime migration: не требуется.
- Existing SQLite: совместимо, legacy details могут быть пустыми.
- Fresh SQLite, upgrade/materialization и PostgreSQL dialect/reference tests входят в final suite; отдельный targeted DB package: 19 passed in 1.19 s.
- Live PostgreSQL: SKIPPED, поскольку безопасный disposable DSN не предоставлен.

## 19. API compatibility

Изменение additive: recent outcome rows получили `outcome_reason` и `outcome_diagnostics`. Существующие поля и routes не удалены и не переименованы.

## 20. Config/env compatibility

Новые environment variables отсутствуют. `.env.example` не изменён. Действия оператора с конфигурацией не требуются.

## 21. Security boundary

- auto-execution и private order endpoints не добавлены;
- secrets/credentials не использовались;
- UI reason проходит HTML escaping;
- malformed numeric values не получают безопасный/положительный смысл;
- входной ZIP не изменялся.

## 22. Post-check commands и точные результаты

| Проверка | Итог |
|---|---|
| `python -m pip check` | FAILED только по внешнему moviepy/pillow conflict, идентично baseline |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE, модуль ruff отсутствует |
| `node --check app/ui/static/app.js` | PASSED |
| targeted iteration 263 | 2 passed; повтор — 2 passed |
| connected regression package | 46 passed in 2.11 s |
| DB/PostgreSQL dialect package | 19 passed in 1.19 s |
| collect-only | 1197 collected |
| monolithic `pytest -q` | TIMED OUT в harness после progress около 78%; промежуточно выявил stale operator-doc assertion, которое исправлено |
| exhaustive deterministic batches | **1197 passed**, union batches = collected set |
| operator DOCX render | 14 страниц, визуально проверены; clipping/overlap не обнаружены |
| operator PDF | пересобран из проверенного DOCX, `%PDF` valid |

Batched counts: 180 + 130 + 153 + 40 + 104 + 59 + 174 + 158 + 199 = 1197. Batches непересекающиеся и покрывают все 209 test files.

## 23. Что не удалось проверить и почему

- Live PostgreSQL integration: нет явно disposable test DSN.
- `ruff`: не установлен в доступном окружении; сеть/обновление dependencies не использовались.
- Единый монолитный pytest process не завершился в harness; вместо него выполнен exhaustive batched run всех collected node IDs.
- Реальные Bybit fills/fees/account margin не проверялись: проект не является execution system, production credentials не использовались.

## 24. Остаточные риски

- Legacy archive не содержит ранее потерянных terminal diagnostics; обратная реконструкция запрещена.
- OHLCV outcome остаётся proxy и не доказывает реальную очередность fills.
- Дополнительные diagnostics увеличивают JSON payload recent outcome rows, но bounded limits сохранены; performance tests прошли.
- `insert_outcome` и observability остаются двумя SQL writes в одной connection с commit в конце; более сильная transaction API может быть отдельным work package, если появится доказанный partial-write reproducer.

## 25. Rollback procedure

1. Остановить v1.0.76 штатным shutdown.
2. Развернуть предыдущий verified ZIP v1.0.75.
3. Config и DB rollback не требуются: schema не менялась, additive observability JSON безопасно игнорируется старой версией.
4. Проверить ownership runtime lock, freshness collector и новую publication текущего процесса.

## 26. Рекомендуемый следующий work package

**Outcome persistence transaction semantics:** независимо проверить crash/retry между записью `reco_outcomes` и `reco_outcome_observability`, особенно на PostgreSQL, и при необходимости оформить единый transaction boundary с red concurrency/crash test. Не менять schema или execution boundary без reproducer.
