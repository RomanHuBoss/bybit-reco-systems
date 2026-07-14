# Audit iteration 246 — outcome scope, readiness truth and strategy-liveness diagnosis

## 1. Название итерации

Bybit Recommender v1.0.58 — отделение текущей policy-когорты от архива, честная индикация калибровочной готовности и диагностика причин постоянного `NO TRADE`.

## 2. Входной ZIP

`bybit-reco-systems-main(1).zip`.

Архив не изменялся. Он распакован в отдельные pristine/red/working каталоги. Фактический единственный root — `bybit-reco-systems-main`.

## 3. SHA-256 входного ZIP

`09aab8d79aee1577f92e466e2dcde78046c203f23f3e2efb1e7c5c43e2494fbe`.

## 4. Исходная версия

- FastAPI: `1.0.57` (`app/main.py`).
- Recommendation lineage: `bybit-taxonomy-v8-policy-conditioned-censor-aware`.
- Outcome target: `grid_label_v26`.
- Bot/global calibrators: v19; direction calibrator: v14.
- Последняя существовавшая iteration: 245.

## 5. Новая версия

- FastAPI: `1.0.58`.
- Recommendation lineage, outcome target и calibrator identities не изменены.
- DB schema, migrations и environment variables не изменены.
- Новая iteration: 246.

Сохранение model/policy identities принципиально: это исправление представления и диагностики, а не очередной reset текущей evidence-когорты.

## 6. Project fingerprint

**PASSED.** Подтверждены README/CHANGELOG/dependency entry points, FastAPI backend, `futures_grid`, Bybit Linear USDT perpetual scope, recommendation/audit-only boundary, SQLite/PostgreSQL persistence, canonical directional/grid/risk/calibration/outcome modules, static frontend, migrations, tests и operator artifacts.

Static scan не обнаружил private order create/amend/cancel endpoints или hard-coded credentials. Проект по-прежнему не является OMS/EMS и не исполняет биржевые ордера.

## 7. Цель итерации

После этой итерации оператор должен видеть только исходы действующей model + exact policy fingerprint как текущую evidence-когорту, архив должен быть явно отделён, а калибровочная панель должна показывать реальные минимальные контракты готовности и физический temporal floor. Одновременно требовалось определить, означает ли отсутствие запусков априорную убыточность стратегии либо является следствием ошибок/блокирующей методологии.

## 8. Критерии приёмки

1. `GET /api/v1/outcomes/stats` по умолчанию возвращает `current_policy`, а не весь архив.
2. Поддерживаются явные scopes `current_policy`, `current_model`, `archive`.
3. Current-policy admission требует active model, exact SHA-256 fingerprint и успешного повторного хэширования persisted policy contract.
4. Старые outcomes сохраняются как immutable audit history, но не входят в текущий headline.
5. UI запрашивает current-policy и archive раздельно и показывает lineage/policy metadata.
6. UI/status различают 80-row monetary floor и 300-row probability floor.
7. Status/UI показывают, что 12-часовой horizon и 20 независимых temporal cohorts дают абсолютный минимум 10 суток неизменной policy.
8. Invalid/missing scope lineage отклоняется fail-closed.
9. Новый regression test падает на pristine и проходит после исправления.
10. Все collected tests, SQLite checks, PostgreSQL offline checks, frontend syntax и clean re-extracted ZIP checks проходят.

## 9. Прочитанные источники

Проверены фактический ZIP, README, CHANGELOG, requirements, settings, последние audit reports, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, outcome/calibration/recommender/DB/API/frontend code, regression tests, operator DOCX/PDF/PNG и приложенный audit protocol.

## 10. Карта затронутого data flow

`current settings + active normalized risk limits` → canonical policy contract → SHA-256 fingerprint → recommendation root → immutable `reco_outcomes` archive → verified scope admission in DB → `/api/v1/outcomes/stats?scope=...` → separate current/archive UI.

Readiness flow:

`exact-policy matured roots` → labeled/censored/unresolved denominator → monetary floor 80 + 20 non-overlapping temporal cohorts → positive one-sided return bounds → probability floor 300 → purged OOF + terminal holdout skill → calibrated confidence → actionability.

## 11. Baseline environment

- Python: `3.13.5`.
- Node: `v22.16.0`.
- Input archive: 307 entries, one root, no traversal, duplicate/conflicting paths, external symlinks or nested archives.
- Baseline inventory: 24 production Python files, 190 test files, 68 docs, 3 frontend files, 2 migration SQL files.
- Baseline collection: 1106 tests.
- Baseline `compileall`: PASSED.
- Baseline JavaScript syntax: PASSED.
- Baseline Ruff: UNAVAILABLE (`No module named ruff`).
- Baseline `pip check`: FAILED only due to host-environment conflict: MoviePy 2.2.1 requires Pillow `<12`, installed Pillow is 12.2.0.
- Monolithic baseline pytest did not produce a final summary within the harness timeout and is not counted as a passed full-suite run.

## 12. Подтверждённые defects/gaps

### DEF-246-01 — HIGH — CONFIRMED DEFECT — архив выдавался за текущую производительность

- Files: `app/db.py`, `app/main.py`, `app/ui/static/app.js`.
- Old behavior: outcome statistics selected the full immutable `reco_outcomes` table without model/policy scope. The UI rendered those rows under current-looking headline cards.
- Reproducer: call `get_outcomes_stats(..., scope="current_policy", ...)` on pristine v1.0.57 — API contract does not exist; endpoint and frontend use the unscoped archive.
- Operator impact: a newly changed model appeared to have inherited sample count, win rate and average return. The screenshot's 72 rows were therefore not evidence for the running policy.
- Trading/model impact: operator could incorrectly conclude that the current strategy was already validated or already losing/profitable.
- Expected behavior: history remains queryable, but the current headline admits only current model + exact verified policy contract.

### DEF-246-02 — HIGH — CONFIRMED DEFECT — readiness UI implied that 80 observations were the effective finish line

- Files: `app/main.py`, `app/ui/static/app.js`.
- Actual backend contract: `CALIB_MIN_SAMPLES=80` is only the monetary floor; full probability activation is hard-coded at 300 rows and, with default `REQUIRE_CONF_GATE=1`, also requires accepted purged OOF and terminal holdout skill.
- Old UI behavior: progress was communicated toward 80 without an equally prominent 300-row actionability contract.
- Operational impact: the system could appear “almost trained” while it was structurally unable to publish actionable confidence.

### GAP-246-03 — HIGH — CONFIRMED GAP — zero-tolerance censoring can permanently disable the strategy

- Files: `app/recommender.py::_apply_outcome_observability_gate`, `app/outcomes.py`, `app/db.py`.
- Reproducer result:

```text
censored=0 unresolved=0 invalid=0 -> status=positive fitted=True coef=13 platt=True
censored=1 unresolved=0 invalid=0 -> status=censored fitted=False coef=0 platt=False
censored=0 unresolved=1 invalid=0 -> status=censored fitted=False coef=0 platt=False
censored=0 unresolved=0 invalid=1 -> status=censored fitted=False coef=0 platt=False
```

- Actual behavior: one permanent censored/unresolved/invalid matured root clears an otherwise positive fitted model and keeps the bot in `NO TRADE` indefinitely.
- Why it is not blindly “fixed” here: dropping censored rows or converting them to zero return would reintroduce survivorship bias and could manufacture positive expectancy.
- Required safe solution: pre-registered partial-identification/sensitivity model with reason-specific conservative return bounds, worst-case binary labels, admissible censor fraction and chronological robustness checks.

### GAP-246-04 — HIGH — CONFIRMED OPERATIONAL LIVENESS GAP — evidence contract is much slower than the UI previously disclosed

- `futures_grid` label horizon: 12 hours.
- Required non-overlapping temporal cohorts at `CALIB_MIN_SAMPLES=80`: 20.
- Absolute theoretical minimum under uninterrupted unchanged policy: `20 × 12h = 10 days` before the temporal monetary floor can possibly pass.
- Full default probability floor: 300 exact-policy labels plus accepted OOF/terminal skill.
- Any model/policy fingerprint change starts a new exact-policy cohort. The included CHANGELOG contains 42 releases dated 11–14 July; v1.0.56 and v1.0.57 both changed the effective evidence identity. This release deliberately does not.
- Conclusion: frequent policy/model revisions can keep the system permanently in cold start even if no trading-math bug remains.

### LIMIT-246-05 — DOCUMENTED LIMITATION — profitability cannot be concluded from this release

The supplied ZIP contains no runtime production database, no exact fill stream and no independently reconciled account PnL sample. The 72 screenshot outcomes are mixed historical OHLCV proxy rows. They cannot establish inherent profit, inherent loss or live edge.

The correct current conclusion is: **the strategy is unproven and operationally evidence-starved**, not “proven profitable” and not “proven априори убыточной”.

## 13. Неподтверждённые claims

- Не подтверждено, что arithmetic futures grid имеет отрицательное математическое ожидание во всех режимах.
- Не подтверждено, что current v8 policy имеет положительное ожидание.
- Не подтверждено, что historical proxy returns воспроизводимы на реальных fills after fees, spread, slippage, funding and queue effects.
- Не подтверждено, что один конкретный remaining defect объясняет все прошлые `NO TRADE` rows.

## 14. План исправления

1. Add explicit outcome scope contract in DB/API.
2. Verify policy contracts instead of trusting stored hashes.
3. Make current-policy the endpoint default.
4. Split current and archive UI rendering.
5. Expose 80/300 readiness and temporal floor.
6. Preserve fail-closed censoring, document it as an open liveness risk.
7. Keep model/outcome/calibrator identities unchanged to avoid another evidence reset.
8. Synchronize tests, docs and operator artifacts.

## 15. Фактический diff

### Production

- `app/db.py` — scope normalization, model matching, policy-contract re-hashing, scoped recent/stat aggregation and lineage fields.
- `app/main.py` — current-policy API default, 80/300 gate contract, censor diagnostics, 10-day theoretical temporal floor, FastAPI 1.0.58.

### Frontend

- `app/ui/static/app.js` — separate current/archive requests and rendering, truthful labels, model/policy metadata, 80/300 and 10-day readiness explanation.

### Tests

- New `tests/test_iteration246_outcome_scope_readiness.py` — 7 regressions.
- Minimal compatibility updates to archive-specific test calls, current UI labels and exact version assertions.

### Database/migrations

- No schema or migration change.

### Documentation/artifacts

- README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC.
- `docs/instrukciya_operatora_bybit_recommender.docx`.
- `docs/instrukciya_operatora_bybit_recommender.pdf`.
- `how_to_trade.png`.
- This audit report.

## 16. Red → green evidence

RED on pristine v1.0.57 with the initial three regression cases:

```bash
python -m pytest -q tests/test_iteration246_outcome_scope_readiness.py
```

Essential output:

```text
TypeError: get_outcomes_stats() got an unexpected keyword argument 'scope'
assert '/api/v1/outcomes/stats?scope=current_policy' in source
assert 'logreg_min_samples' in source
3 failed in 0.54s
```

GREEN after the production fix and expansion to seven regressions:

```bash
python -m pytest -q tests/test_iteration246_outcome_scope_readiness.py
```

```text
7 passed
```

## 17. Database/schema compatibility

- No schema change.
- Fresh SQLite init + repeated init: PASSED.
- Existing SQLite created by pristine v1.0.57, then opened/init'd by v1.0.58: PASSED.
- PostgreSQL offline dialect/translation/locking suite: 18 passed.
- Live PostgreSQL integration: SKIPPED; no explicitly disposable test DSN was supplied.
- No manual data deletion or migration command is required.

## 18. API compatibility

- Existing fields remain.
- `scope` is additive.
- Endpoint default changes intentionally from implicit archive aggregation to `current_policy` because the prior default was operator-misleading.
- Explicit `scope=archive` preserves historical/research access.
- Invalid scope returns HTTP 400.

## 19. Config/env compatibility

No environment-variable changes. Current model, policy schema, outcome target and calibrator keys are unchanged. Existing deployments require no `.env` edit.

## 20. Security boundary

- No private order endpoint added.
- No production credentials used.
- No hard-coded secret detected.
- Current-policy contract verification rejects malformed or tampered lineage fail-closed.
- Historical archive remains read-only evidence; it cannot unlock current actionability through the outcome UI.

## 21. Post-check

- Test collection: 1113 unique nodes.
- Exhaustive deterministic run: 12 non-overlapping batches, `1113/1113` nodes passed.
- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- New regression: PASSED repeatedly.
- Relevant outcome/API/calibration suites: PASSED.
- SQLite fresh/re-init/upgrade: PASSED.
- PostgreSQL offline suite: 18 passed.
- DOCX rendered to 10 pages and visually checked; PDF is 10 pages and openable.
- Private order endpoint scan: NONE.
- Hard-coded secret scan: NONE.
- Ruff: UNAVAILABLE.
- `pip check`: host conflict MoviePy/Pillow remains; unrelated to changed runtime code.

The monolithic full pytest process was not used as the final evidence because this harness intermittently hangs during teardown after printing a complete summary. All nodes were instead collected once, split into deterministic non-overlapping batches, and every node passed with zero overlap/omission.

## 22. Что не удалось проверить

- Actual current-policy runtime rows and censor reasons: production DB not supplied.
- Live Bybit fills/order-book queue behavior: project has no executor and no fill export was supplied.
- Live PostgreSQL: no disposable DSN.
- Realized strategy profitability: no reconciled external execution sample.
- Ruff: tool unavailable in the environment.

## 23. Остаточные риски

1. Zero-tolerance censoring can preserve permanent `NO TRADE` after one unbounded missing outcome.
2. Exact-policy evidence can be reset by configuration/risk-limit/model changes; policy should be frozen during a pre-registered evaluation window.
3. 300 labels and held-out skill do not prove stable live alpha; multiple-comparison, regime drift and cross-market dependence remain.
4. OHLCV proxy cannot reconstruct queue priority, partial fills, order-book impact or liquidation mechanics.
5. The service is recommendation/audit-only; execution quality belongs to an external reconciled layer.

## 24. Rollback

1. Stop v1.0.58.
2. Restore the previous v1.0.57 code/archive.
3. Do not delete or rewrite the DB; schema is unchanged.
4. Historical outcomes remain intact. The old UI will again aggregate them unless `scope=archive/current_policy` support is retained, so rollback reintroduces DEF-246-01/02.

## 25. Рекомендуемый следующий work package

Freeze one policy fingerprint for a pre-registered evaluation window and implement bounded-censor sensitivity analysis without changing selection thresholds post hoc. Required input from runtime DB: every current-policy root, censor reason/details, complete 1m market path, policy contract, proxy PnL, and any externally reconciled execution ledger. Produce three curves: complete-case, pessimistic bound and reason-specific bounded estimate. Actionability may unlock only if the lower confidence bound remains positive under the pessimistic admissible-censor scenario.
