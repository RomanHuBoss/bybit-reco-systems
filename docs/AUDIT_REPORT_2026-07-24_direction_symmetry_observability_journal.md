# Audit iteration 277: log-symmetric direction, temporal observability and decision journal

## 1. Название итерации

`v1.4.7 — log-symmetric direction + temporal outcome observability + decision journal master-detail UI`.

## 2. Входной ZIP

`bybit-reco-systems-main.zip`.

## 3. SHA-256 входного ZIP

`a9cbc43c5d569120749260336ad681230a7c6e43890f6f66c7c14351f51825ae`.

## 4. Исходная версия

`1.4.6`, source of truth: `app/main.py`, параметр `version=` конструктора FastAPI.

## 5. Новая версия

`1.4.7` (patch release). Публичные endpoint и DB schema не изменены; добавлены observability fields и новая feature/model lineage.

## 6. Project fingerprint

Fingerprint подтверждён. В единственном root `bybit-reco-systems-main` присутствуют README, CHANGELOG, requirements, `main.py`, `app/main.py`, canonical trading modules, `app/ui/static/`, tests, docs и обе SQL-схемы. Strategy families: `futures_grid` и `directional_trend`; exchange scope: Bybit V5 Linear USDT Perpetual; persistence: SQLite + PostgreSQL; сервис остаётся recommendation/audit-only.

Безопасность архива: 375 entries; absolute path, `../` traversal, external symlink, duplicate/conflicting path и вложенный archive не обнаружены. Входной ZIP не модифицировался. Созданы отдельные pristine, red-test и working copies.

## 7. Цель итерации

После итерации система должна:

1. давать математически симметричный directional score для зеркальных лог-доходностей;
2. не компенсировать LONG искусственным бонусом и не менять threshold ради текущей малой выборки;
3. показывать временную зависимость outcome rows и не называть количество строк независимым sample size;
4. не смешивать mutually exclusive eligibility cohorts в основной strategy-таблице;
5. отображать decision journal как читаемый master-detail audit UI вместо узкой таблицы с raw JSON;
6. начать новую model/calibrator lineage после изменения feature meaning;
7. сохранить fail-closed, payoff, funding, TP/SL, outcome label и recommendation/audit-only invariants.

## 8. Критерии приёмки

- Independent mirror fixture: `score(path) == -score(mirror(path))` и component parity с `abs <= 1e-12`.
- 5 000 seeded OHLC mirror paths: 0 LONG/SHORT/NEUTRAL classification mismatches.
- Outcomes API содержит `sample_observability` и cohort-aware `by_bot_cohort`.
- Fixture с одинаковыми timestamps и overlapping horizons показывает 4 rows, 3 timestamps, 2 overlap clusters и 2 non-overlapping windows.
- Main Results renderer использует `by_bot_cohort`; legacy `by_bot` остаётся в payload для compatibility/audit.
- Journal production helper исполняется в Node, экранирует HTML, не содержит `JSON.stringify(row.details)` и открывается wide modal.
- Новые tests сначала RED на pristine, затем GREEN на working.
- Exhaustive collected test set проходит без пропущенных nodes.

## 9. Прочитанные источники

Прочитаны релевантные разделы README, CHANGELOG, requirements, `.env.example`, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, последние audit reports, protocol PDF v1.4.5, `app/direction.py`, trading semantics, recommender, calibration, trend events, outcomes, DB, main API, settings, frontend и related regression tests. Runtime code использован как primary factual source; документация и старые audit reports рассматривались как intent/history.

## 10. Карта затронутого data flow

`OHLCV -> app.direction.vote_for_tf(log_price_v1) -> MTF aggregate -> recommender candidate/model identity -> persistence -> outcome root/window -> app.db outcome aggregation -> API /outcomes/stats -> Results UI`

и отдельно:

`decision_log rows -> /api/v1/decisions -> loadDecisions -> renderDecisionJournal -> escaped master-detail cards`.

Trading semantics, grid geometry, sizing, funding settlement, first-touch label target, router math и execution preflight не изменялись.

## 11. Baseline environment

- Python `3.13.5`.
- Node `v22.16.0`.
- Production Python files: 26.
- Baseline test files: 222; baseline collection: 1319 tests.
- Docs files: 101; frontend files: 3; migration SQL files: 2.
- API routes: 24, из них 7 mutating POST routes.
- Background threads: collector, backfill, futures metadata, sentiment, recommender, outcomes и optional LLM reviewer.
- `pip check`: shared-host conflict `moviepy 2.2.1` требует `Pillow <12`, installed `Pillow 12.2.0`.
- `ruff`: unavailable in environment (`No module named ruff`).

## 12. Baseline commands и результаты

- Archive SHA/safety scan: PASSED.
- Project fingerprint: PASSED.
- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `pytest --collect-only -q`: 1319 collected.
- Monolithic baseline pytest: harness timeout; не объявлен full-suite success.
- Exhaustive deterministic baseline batches/per-file completion: 1319 passed, 0 failed по union collected nodes.
- `pip check`: FAILED только из-за указанного pre-existing host dependency conflict.
- `python -m ruff check .`: UNAVAILABLE.

## 13. Подтверждённые defects/gaps

### D-277-01 — HIGH — CONFIRMED DEFECT — directional boundary asymmetry

- Файл/function: `app/direction.py`, `rsi14`, `macd_hist`, `ma_slope`, `atr_pct`, `vote_for_tf`.
- Вход: OHLC path и reciprocal mirror `p'_t=p_0^2/p_t`.
- Фактическое поведение: arithmetic price-level indicators не были строго antisymmetric; synthetic mirror audit ранее давал преимущественно boundary mismatches в пользу SHORT.
- Ожидаемое поведение: equally strong mirrored returns должны давать equal-magnitude opposite score и LONG/SHORT parity.
- Нарушенный invariant: protocol sign/multi-timeframe mirror contract.
- Trading/model impact: selection bias около directional threshold; состав shadow/evidence cohorts мог слегка смещаться по стороне.
- Почему tests не поймали: прежние tests проверяли знак на простых rising/falling paths, но не reciprocal mirror equality каждого component.
- RED: `KeyError: indicator_space` и независимый antisymmetry test на pristine.
- Fix: единое log-price representation, log ATR normalization, complete component diagnostics, new lineage.
- GREEN: targeted test passed; 5 000-path audit — 0 mismatches, max score error `3.045e-13`.
- Остаточный риск: symmetry не доказывает market edge.

### D-277-02 — MEDIUM — CONFIRMED GAP — misleading sample reliability and cohort mixing

- Файлы: `app/db.py` около `_outcome_window`, `_sample_observability`, `get_outcomes_stats`; `app/ui/static/app.js` Results renderer.
- Вход: несколько symbols одного/близких timestamps с overlapping horizon и разными eligibility cohorts.
- Фактическое поведение: UI показывал row count как «наблюдения» с qualitative reliability badge; main `by_bot` строка объединяла разные eligibility cohorts.
- Ожидаемое поведение: row count отделён от temporal structure; main table не смешивает calibration/policy/shadow cohorts.
- Model/data impact: псевдорепликация и неверная операторская интерпретация 14 LONG или 59 SHORT как независимых испытаний.
- RED: отсутствовали `sample_observability` и `by_bot_cohort`.
- Fix: exact observation windows, unique timestamps/symbols, connected overlap clusters, maximum non-overlapping intervals; additive cohort-aware grouping.
- GREEN fixture: 4 rows / 3 timestamps / 4 symbols / 2 clusters / 2 non-overlapping windows; 2 calibration + 2 shadow rows раздельно.
- Остаточный риск: diagnostics не заменяет полноценный correlation-adjusted effective sample size.

### D-277-03 — MEDIUM — CONFIRMED UX DEFECT — cramped decision journal

- Файлы: `app/ui/static/app.js`, `styles.css`.
- Фактическое поведение: 9-column table, narrow cells, raw nested JSON in one cell, long IDs and payload compressed the whole row.
- Ожидаемое поведение: primary context visible without horizontal decoding; full payload available on demand; responsive wide dialog.
- UX/operational impact: incident review was slow and error-prone; symbol/strategy/status could be visually lost among JSON.
- RED: production helper absent.
- Fix: summary cards, master-detail cards, localized action/status, short audit IDs with full title, structured flattened details, HTML escaping, responsive breakpoints.
- GREEN: production helper executed in Node; malicious HTML rendered escaped; raw JSON cell absent; Chromium screenshot reviewed.
- Остаточный риск: UI leaf-field safety cap 160; durable backend payload is not truncated.

## 14. Неподтверждённые claims

- LONG payoff/P&L sign inversion не подтверждена.
- TP/SL geometry inversion не подтверждена.
- Funding sign error не подтверждена.
- Текущие 14 LONG losses не доказали, что LONG strategy structurally unprofitable; snapshot predominantly shadow exploration и temporally dependent.
- Не заявляется наличие live edge, production readiness auto-execution или прибыльность новой lineage.

## 15. План исправления

1. Создать one-file iteration 277 RED suite.
2. Исправить source-of-truth direction math, не thresholds.
3. Bump all affected model/calibrator identities.
4. Добавить additive backend sample/cohort projections.
5. Перевести Results UI на cohort-aware canonical table.
6. Заменить Journal table на master-detail cards.
7. Синхронизировать docs/operator artifacts.
8. Выполнить targeted, relevant, exhaustive batched, syntax, DB dialect и release checks.

## 16. Фактический diff по файлам

Production:

- `app/direction.py` — log-space indicators and marker.
- `app/db.py` — temporal observability and cohort grouping.
- `app/recommender.py`, `app/calibration.py`, `app/trend_events.py` — new immutable lineages.
- `app/main.py` — version 1.4.7.
- `app/ui/static/app.js`, `styles.css`, `index.html` — Results observability and Journal redesign.

Tests:

- new `tests/test_iteration277_direction_observability_journal_ui.py`;
- minimal version/model/token updates in exact-contract tests.

Docs:

- README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC;
- operator DOCX/PDF; this audit report.

No migration SQL change.

## 17. Red -> Green evidence

RED on pristine:

- mirror test: `KeyError: 'indicator_space'`;
- stats fixture: `KeyError: 'sample_observability'`;
- journal fixture: production helper substring absent;
- summary: `3 failed in 0.15s`.

GREEN on working:

- `tests/test_iteration277_direction_observability_journal_ui.py`: `3 passed in 0.08s`;
- repeated deterministic run: `3 passed in 0.08s`;
- relevant suite: `103 passed`;
- exhaustive 8-batch union: `1322 passed`.

## 18. Database/schema compatibility

Schema не менялась. SQLite runtime bootstrap и both migration SQL files не требуют изменения. `by_bot_cohort` и `sample_observability` являются read-time API projections. Fresh/upgrade and PostgreSQL dialect/locking suites прошли; live PostgreSQL integration не выполнялся без disposable DSN.

## 19. API compatibility

Existing endpoints and field names сохранены. `/api/v1/outcomes/stats` получил additive fields. Legacy `by_bot` остаётся. `/api/v1/decisions` payload не менялся; изменён только frontend projection. Breaking API change отсутствует.

## 20. Config/env compatibility

Новые environment variables не добавлены. `.env.example` не менялся. Перезапуск нужен для загрузки v1.4.7 assets и новой model identity. Existing DB можно использовать без очистки.

## 21. Security boundary

HTML-like journal values проходят `escapeHtml`. Boolean/numeric exactness для observation windows использует existing strict integer semantics. Private order create/amend/cancel endpoints не добавлены. Реальные API keys/DSN не использовались и не включаются в release.

## 22. Post-check commands и результаты

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `pytest --collect-only -q`: 1322 collected.
- Exhaustive deterministic batches: 166 + 166 + 165 × 6 = 1322 PASSED.
- Targeted iteration 277: 3 PASSED twice.
- Relevant direction/outcome/UI/docs: 103 PASSED.
- SQLite/PostgreSQL dialect/persistence subset: 31 PASSED.
- Mirror audit 5 000 paths: 0 mismatches.
- DOCX render: 19 pages; all pages visually inspected; PDF regenerated.
- Chromium journal preview: 6 cards rendered; screenshot visually inspected.
- `pip check`: pre-existing host conflict only.
- `ruff`: unavailable.
- Monolithic pytest: TIMED OUT and not used as the release result; complete batched union used instead.

## 23. Что не удалось проверить и почему

- Live PostgreSQL integration: no explicit disposable test DSN.
- Real Bybit account/order/fill behavior: outside recommendation/audit scope; no credentials used.
- Ruff lint: package absent in environment and dependencies were not mutated solely for this iteration.
- Full monolithic pytest completion: harness timeout; protocol-compliant exhaustive deterministic batches covered every collected node.

## 24. Остаточные риски

- New lineage initially has insufficient evidence and should remain fail-closed until existing monetary/temporal/model gates pass.
- Temporal observability does not model all cross-symbol correlation or regime dependence.
- OHLCV proxy and first-touch candles do not prove queue priority, live fill, fees, latency or account margin truth.
- Journal UI protects readability but is not a substitute for DB backup/export during forensic analysis.

## 25. Rollback procedure

1. Stop v1.4.7.
2. Preserve a backup of the current DB and audit logs.
3. Restore v1.4.6 application files/assets.
4. Do not rewrite or delete v1.4.7 recommendations/outcomes; they remain immutable historical records under their model version.
5. Restart and verify version/status. No DB down-migration is required.

## 26. Рекомендуемый следующий work package

After sufficient new-lineage evidence accumulates, perform a frozen-lineage walk-forward audit by direction, symbol cluster and regime using non-overlapping temporal cohorts, maker-fill markout/adverse-selection labels and cost-aware expected value. Do not change thresholds before that evidence exists.
