# Audit report — strategy observability, Details/history UI and outcome integrity

## 1. Итерация

- Входной релиз: `bybit-reco-systems-1.3.0-trend-first-touch-model.zip`
- SHA-256 входного ZIP: `a564c0da663f1112980d4b50728299e003701fdd3024bd48436c65bbf0175f7d`
- Исходная версия: `1.3.0`
- Новая версия: `1.4.0`
- Scope: сквозная согласованность `futures_grid` и `directional_trend` между БД, API, окнами «Детали», «Журнал», «Исходы», «Здоровье» и историческим графиком.

## 2. Критерии приёмки

1. Каждая strategy family отображает собственную торговую геометрию и собственный outcome contract.
2. Канонические strategy/event identities сохраняются в БД и не выводятся из диагностического текста задним числом.
3. До созревания 12-часового horizon оператор видит durable waiting-запись и точный `label_due_ts`.
4. История использует только persisted geometry конкретной публикации; неизвестные уровни не достраиваются по текущим metadata.
5. Multi-point график выполняется в JavaScript для grid и trend без исключений.
6. Журнал, исходы и здоровье разделяют обе стратегии и показывают semantic-integrity failures fail-closed.
7. SQLite fresh/upgrade и PostgreSQL dialect/locking пути сохраняют совместимость.

## 3. Подтверждённые дефекты

### HIGH — trend TP/SL в backend-проекции «Деталей» строились через grid kill-switch

`_directional_exit_payload_for_reco()` использовал сеточную защитную геометрию и для `directional_trend`. В результате корректный single-position trade plan мог выводиться с пустыми либо неверными TP/SL и RR. Исправлено строгим branch по `bot_type`: trend читает `entry/take_profit/stop_loss` только из сохранённого trade plan, grid сохраняет прежний kill-switch contract.

### HIGH — до первой попытки outcome-worker отсутствовала durable запись ожидания

Рекомендация могла существовать в БД, но до созревания горизонта и первой попытки worker окно «Детали» и «Здоровье» показывали нули без срока созревания. Исправлено publication-time materialization в `reco_outcome_observability` с `state=waiting`, `reason=scheduled_for_label_horizon`, `last_attempt_ts=0` и точным `label_due_ts`.

### MEDIUM — multi-point график истории падал в браузере

`Array.from(..., callback)` ошибочно ожидал третий аргумент `array`; фактически он отсутствует. При двух и более публикациях renderer выбрасывал `TypeError`, а UI называл это сетевой ошибкой. Исправлено явным `timeTickCount`, добавлен реальный Node/browser execution regression для grid и trend.

### MEDIUM — канонический event type мог расходиться между колонкой и diagnostics

`event_type` из SQL и `diagnostics.event_type` могли показывать разные события. Исправлена единая канонизация: `GRID_OUTCOME` для grid; `TP_FIRST / SL_FIRST / HORIZON_EXIT / LEGACY_BINARY` для trend; `AMBIGUOUS` запрещён к сохранению как labeled outcome.

### MEDIUM — здоровье outcome-worker учитывало только futures_grid

Matured/waiting counters и horizon fallback были grid-only. Исправлены общие и per-bot счётчики, ближайший due time, schedule/waiting state и calibrator/outcome presentation для обеих стратегий.

### MEDIUM — журнал решений не содержал явную strategy identity

Без `bot_type` одинаковые status/reason codes нельзя было безопасно интерпретировать. Journal API теперь возвращает venue, symbol, bot type, direction, recommendation status и model version; UI выводит структурированную таблицу.

### MEDIUM — архив исходов скрывал распределение strategy/event

Archive summary показывал общий success/net result, но не различал `GRID_OUTCOME`, `TP_FIRST`, `SL_FIRST`, `HORIZON_EXIT`. Добавлены `by_bot`, `event_type_counts` и `event_type_counts_by_bot`.

### MEDIUM — история могла реконструировать или соединять недоказанную геометрию

Backend не передавал полноценную immutable price geometry, а frontend мог визуально соединять пропуски. Теперь history API сохраняет strategy-native geometry и root outcome tracking; отсутствие уровня разрывает линию.

### MEDIUM — отсутствие cross-table semantic integrity в health

Добавлены проверки orphan outcomes, missing/non-labeled observability, recommendation identity mismatch, invalid event type и persisted ambiguous rows. Ненулевой счётчик деградирует readiness кодом `OUTCOME_SEMANTIC_INTEGRITY_FAILED`.

### LOW/MEDIUM — ошибки API и renderer маскировались пустым состоянием

Malformed JSON, HTTP/network failure и JavaScript rendering exception теперь показываются раздельно. Ошибка построения истории больше не называется «ошибкой сети».

### LOW — N+1 чтение outcome tracking в списке рекомендаций

Добавлен batch endpoint/helper `get_outcome_tracking_many`; list API обогащает строки одним запросом.

## 4. Фактические изменения

### Production

- `app/db.py`
  - canonical event types и upgrade/backfill grid rows;
  - immediate durable outcome schedule;
  - strategy-aware history geometry;
  - batch outcome tracking;
  - per-bot worker liveness и DB continuity;
  - cross-table semantic integrity;
  - bot-specific exact-policy eligibility.
- `app/main.py`
  - strategy-native Details payload;
  - outcome tracking in list/detail/history;
  - strategy-aware decision journal;
  - semantic-integrity readiness gate;
  - version `1.4.0`.
- `app/ui/static/app.js`
  - grid/trend-specific Details, outcomes, health and journal;
  - canonical event/result rendering;
  - strategy-native price timeline;
  - multi-point chart fix;
  - fail-visible network/data/render errors.
- `app/ui/static/styles.css`, `index.html`
  - graph legends/line semantics and cache/version update.

### Tests

- расширен `tests/test_iteration269_strategy_observability_ui.py`;
- синхронизированы version/document assertions существующих release tests.

### Documentation

Обновлены README, CHANGELOG, architecture/trading/modules/scenarios/known-risks/infographic docs, operator DOCX/PDF, `how_to_trade.png`, текстовый и PDF итерационный протокол.

## 5. Red → green evidence

Первый новый regression-suite на исходной логике:

```text
7 failed
```

Отдельный regression неправильной trend TP/SL проекции:

```text
1 failed: expected trend take_profit=104.0, obtained None
```

Browser/Node reproducer до исправления multi-point chart:

```text
TypeError: Cannot read properties of undefined (reading 'length')
```

Итоговый новый suite:

```text
22 passed
```

## 6. Database compatibility

- Новых колонок в v1.4.0 не добавлено.
- Ручная SQL-миграция не требуется.
- `init_db()` автоматически:
  - канонизирует legacy grid `event_type` в `GRID_OUTCOME`;
  - создаёт недостающие schedule-ledger rows;
  - backfill-ит observability для уже существующих outcomes.
- SQLite fresh/upgrade и PostgreSQL dialect/locking paths проверяются теми же strategy invariants.

## 7. API/UI compatibility

Существующие поля сохранены; новые поля additive:

- `outcome_tracking`;
- `price_geometry`;
- strategy/event breakdowns;
- per-bot liveness/health counters;
- journal strategy identity.

Trading labels, model versions, 12-hour horizons и strategy-profitability router semantics не изменены. Это observability/UI correctness release, поэтому ML/policy lineage не сбрасывается.

## 8. Browser smoke

На inline Playwright fixture проверены:

- trend Details;
- grid Details;
- trend multi-point history SVG;
- grid multi-point history SVG;
- outcomes;
- health;
- decision journal.

После исправления browser errors отсутствуют; линии TP/entry/SL и kill/range/reference строятся раздельно.

## 9. Проверки

### Baseline v1.3.0

```text
1252 collected
1252 passed
0 failed
```

### Post-check v1.4.0

```text
1274 collected
1274 passed
0 failed
0 skipped
0 errors
```

Дополнительно:

- новый regression-suite — `22 passed`, повторён дважды;
- SQLite/PostgreSQL strategy observability suite — `53 passed`;
- Python `compileall` — passed;
- Node `--check app/ui/static/app.js` — passed;
- DOCX/PDF render inspection — 17 страниц, passed;
- iterative PDF render inspection — 37 страниц, passed;
- infographic visual inspection — passed;
- `pip check` — external environment conflict: MoviePy 2.2.1 требует Pillow `<12`, установлена Pillow 12.2.0;
- `ruff` — unavailable in environment.

## 10. Что не проверено

- live Bybit private order submission не выполнялась и не добавлялась;
- live PostgreSQL integration не запускалась без явно disposable DSN;
- реальные market outcomes не используются как доказательство live edge.

## 11. Действия пользователя

- SQL вручную выполнять не нужно.
- `.env` изменять не нужно.
- После обновления достаточно запустить сервис на прежней БД; startup materialization создаст недостающие schedule rows.

## 12. Rollback

1. Остановить v1.4.0.
2. Вернуть релиз v1.3.0.
3. Запустить на той же БД.

Новые observability rows и canonical `GRID_OUTCOME` совместимы с additive schema. Для чистого byte-for-byte rollback БД требуется заранее сделанная резервная копия, но функциональный downgrade не требует удаления строк.

## 13. Остаточные риски

- Исторические legacy trend outcomes без сохранённой TP/SL geometry не реконструируются.
- OHLCV first-touch остаётся proxy и не доказывает внутрисвечную очередь исполнения.
- UI smoke использует детерминированные fixtures, а не production browser session с реальной Bybit сетью.

## 14. Commit message

```text
fix(observability): align grid and trend across DB, API and operator UI

- materialize per-strategy outcome schedules at recommendation publication
- preserve canonical grid/trend event types and semantic integrity checks
- render strategy-native Details and immutable history geometry
- fix multi-point history charts and fail-visible API/render errors
- expose strategy identity in journal, outcomes and health
- update operator docs and iterative audit prompt for v1.4.0
```
