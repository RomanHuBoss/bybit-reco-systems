# Audit iteration 232 — terminal execution-evidence finalization

## 1. Название итерации

Terminal execution-evidence finalization: исключение частичного realized PnL из live-validation и `LIVE_VALIDATION_*` stop gate.

## 2. Входной ZIP

`bybit-reco-systems-1.0.43-expectancy-uncertainty-gate.zip`

## 3. SHA-256 входного ZIP

`17dce60aa24ccdb95d83cb9c1d0056813e44368b385f98bc0a52ce38ae5bf7d3`

## 4. Исходная версия

`1.0.43`, source of truth: `app/main.py`, параметр `version=` объекта FastAPI.

## 5. Новая версия

`1.0.44` (patch, обратно совместимое fail-closed исправление; схема и маршруты не менялись).

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender:

- `futures_grid`, Bybit `linear` USDT perpetual;
- recommendation/audit-only, без private order create/amend/cancel;
- FastAPI в `app/main.py`;
- SQLite + PostgreSQL compatibility layer;
- frontend в `app/ui/static/`;
- обязательные trading/risk/outcome/calibration модули, docs и обе init SQL присутствуют.

Архив: 280 entries, один root `bybit-reco-systems-main`; absolute path, `../`, symlink escape, duplicate/conflicting path и подозрительный nested archive не обнаружены.

## 7. Цель итерации

После этой итерации live-validation должна принимать только окончательный total PnL остановленного бота: полный signed Buy/Sell execution ledger, нулевая остаточная позиция и `total_pnl_finalized=true`. Частичные события должны оставаться видимыми для аудита, но не влиять на stop gate.

## 8. Критерии приемки

1. Один unmatched execution event у stopped bot не является finalized evidence.
2. Полный сбалансированный Buy/Sell ledger у stopped bot является finalized evidence.
3. `list_live_validation_records()` возвращает machine-readable причины исключения.
4. `_live_validation_scope_summary()` повторно проверяет finalization, даже если вход ошибочно помечен eligible.
5. Существующие exact-evidence и live-validation тесты проходят на сбалансированных fixtures.
6. Полный suite проходит без регрессий.
7. SQLite fresh/re-init и upgrade с 1.0.43 сохраняют данные.
8. Итоговый ZIP проходит повторную распаковку и targeted test.

## 9. Прочитанные источники

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- пять последних audit reports;
- `app/db.py`, `app/main.py`, `app/trading_semantics.py`, `app/grid_math.py`, `app/risk.py`, `app/recommender.py`, `app/calibration.py`, `app/outcomes.py`;
- execution/live-validation regression tests iterations 204, 205 и 227;
- operator DOCX/PDF/PNG.

## 10. Карта затронутого data flow

Внешний read-only adapter → `POST /api/v1/bots/{bot_id}/execution-evidence` → immutable `execution_evidence` → `db.get_bot_execution_summary()` → `db.list_live_validation_records()` → `_live_validation_scope_summary()` → direction/symbol/portfolio `LIVE_VALIDATION_*` gate → operator/API diagnostics.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `v22.16.0`;
- production Python files: 23;
- test files: 175 до итерации;
- docs files: 55;
- frontend files: 3;
- migration SQL: 2;
- API routes: 22, из них 6 mutating POST;
- DB backends: SQLite и PostgreSQL/psycopg translation layer;
- максимальный предыдущий iteration: 231.

## 12. Baseline commands и результаты

- `python -m pip check` — FAILED из-за внешнего конфликта среды: MoviePy 2.2.1 требует Pillow `<12`, установлен Pillow 12.2.0.
- `python -m compileall -q app tests main.py` — PASSED.
- `python -m ruff check .` — UNAVAILABLE (`No module named ruff`).
- `node --check app/ui/static/app.js` — PASSED.
- `python -m pytest --collect-only -q` — 1034 collected.
- первый monolithic run был прерван harness после 83% без summary и не засчитан;
- повторный baseline из неизменённой pristine copy: `1034 passed in 26.35s`.

## 13. Подтверждённый defect

### ITER232-01 — partial execution ledger accepted as finalized exact PnL

- Severity: **HIGH**.
- Тип: **CONFIRMED DEFECT**, live-validation fail-open.
- Файлы: исходные `app/db.py::get_bot_execution_summary`, `list_live_validation_records`; `app/main.py::_live_validation_scope_summary`.
- Вход: stopped bot и один execution event, например `Sell 0.1`, `gross_pnl=+10`.
- Фактическое поведение v1.0.43: `execution_count > 0` делал запись `validation_eligible=true`; realized net PnL входил в exact cohort.
- Ожидаемое поведение: total bot PnL не считается окончательным, пока signed fill ledger не доказывает flat terminal position.
- Нарушенный инвариант: exact evidence должно быть окончательным, сопоставимым и не скрывать open inventory.
- Финансовое влияние: частично реализованная прибыль могла войти в статистику, а открытая позиция и последующий tail loss — остаться вне неё; direction/symbol/portfolio stop gate мог быть отложен или не сработать.
- Почему тесты не поймали: iteration205 fixtures сами создавали единственный Sell event и тем самым закрепляли ошибочную семантику «один fill = total PnL».

## 14. Неподтверждённые claims

- Не доказано, что стратегия априори убыточна: release не содержит representative runtime DB с полным exact-fill history.
- Не доказано, что все внешние adapters передают полный ledger; это остаётся external executor requirement.
- Flatness по signed quantities не доказывает корректность exchange-level fees, rebates, liquidation waterfall или account-level reconciliation.

## 15. План исправления

1. Добавить независимый red regression test.
2. Реконструировать signed Buy/Sell quantity из immutable execution events.
3. Ввести `position_flat`, `execution_ledger_complete`, `bot_stopped`, `total_pnl_finalized`.
4. Сделать `validation_eligible` равным только terminal finalization.
5. Добавить defensive recheck в aggregator и API.
6. Обновить fixtures, документы и operator artifacts.
7. Выполнить полный post-check и release verification.

## 16. Фактический diff по файлам

### Production

- `app/db.py`: signed-quantity reconciliation и ineligibility reasons.
- `app/main.py`: defensive finalization check, additive state diagnostics, версия 1.0.44.

### Tests

- новый `tests/test_iteration232_execution_evidence_finalization.py`;
- iteration205 fixtures теперь содержат opening Buy + closing Sell;
- iteration227 synthetic record содержит `total_pnl_finalized=true`;
- version assertions iterations 213–226 синхронизированы.

### Docs/operator artifacts

README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, DOCX, PDF и PNG.

### Database/migrations/frontend

Изменений схемы, init SQL и frontend JS/HTML/CSS нет.

## 17. Red → green evidence

Red command:

```bash
python -m pytest -q tests/test_iteration232_execution_evidence_finalization.py
```

Существенные red-строки:

```text
KeyError: 'net_position_qty'
KeyError: 'buy_qty'
assert 1 == 0
3 failed
```

Green command: тот же.

```text
3 passed in 1.17s
```

Повторный related suite:

```text
62 passed in 3.84s
```

## 18. Database/schema compatibility

Relational schema не менялась. Finalization вычисляется из существующих `execution_evidence.side`, `qty`, `bot_instances.status` и `stopped_ts`.

- fresh SQLite init + повторный init — PASSED;
- sentinel после re-init — сохранён;
- upgrade DB, созданной кодом 1.0.43, через 1.0.44 — PASSED, sentinel сохранён;
- PostgreSQL offline dialect/locking subset — `24 passed`;
- live PostgreSQL integration — SKIPPED: disposable DSN не предоставлен.

## 19. API compatibility

Маршруты и обязательные request fields не удалялись. Execution/live-validation responses получили только additive diagnostic fields. Неавторизованные security boundaries не ослаблялись.

## 20. Config/env compatibility

Новых env variables нет. `.env.example` не изменён. Пользовательских действий с конфигурацией не требуется.

## 21. Security boundary

Private Bybit order create/amend/cancel methods не добавлялись. Проект остаётся recommendation/audit-only. Execution events принимаются только через существующий ADMIN_API_KEY-protected endpoint. Реальные credentials и production network calls не использовались.

## 22. Post-check commands и результаты

- `python -m pip check` — FAILED только по pre-existing MoviePy/Pillow conflict.
- `python -m compileall -q app tests main.py` — PASSED.
- `python -m ruff check .` — UNAVAILABLE.
- `node --check app/ui/static/app.js` — PASSED.
- `python -m pytest --collect-only -q` — 1037 collected.
- `python -m pytest -q` — **1037 passed in 25.68s**.
- iteration232 — 3 passed.
- related execution/live-validation/API suite — 62 passed.
- PostgreSQL offline subset — 24 passed.
- docs/release subset — 27 passed.
- DOCX rendered: 7 pages, all pages visually inspected.
- emitted PDF rendered: 7 pages, visually inspected.
- PNG infographic visually inspected.

## 23. Что не удалось проверить

- live PostgreSQL без безопасного disposable DSN;
- completeness реального внешнего Bybit reconciliation adapter;
- реальные private positions/open orders/account equity;
- реальный live edge и net expectancy по production fills;
- Ruff из-за отсутствия пакета в среде.

## 24. Остаточные риски

1. External adapter может не передать fill; локальная система не имеет private Bybit reconciliation и не может самостоятельно доказать completeness.
2. Equal signed quantity означает flat ledger в рамках полученных событий, но не является независимой exchange-position attestations.
3. Float tolerance выбран строго и детерминированно; для будущих instrument-specific quantity contracts предпочтительнее Decimal/qtyStep-aware reconciliation.
4. Исторические live-validation записи до 1.0.44 должны быть пересмотрены: single/partial-fill bots больше не считаются finalized.
5. Положительная finalized sample всё ещё не доказывает live profitability без comparator, purged walk-forward, drawdown и cost reconciliation.

## 25. Rollback procedure

Остановить сервис, вернуть код/артефакты 1.0.43 и перезапустить. Откат БД не требуется. Rollback возвращает подтверждённый fail-open дефект, поэтому не рекомендуется. Новые additive response fields исчезнут, но сохранённые execution rows не потеряются.

## 26. Рекомендуемый следующий work package

Сделать exchange-attested reconciliation contract: terminal snapshot от внешнего read-only adapter с position qty, open orders, cumulative fees/funding и completeness cursor; сопоставить его с локальным signed ledger. Затем пересчитать historical exact cohort и выполнить monetary walk-forward только по reconciled finalized bots.
