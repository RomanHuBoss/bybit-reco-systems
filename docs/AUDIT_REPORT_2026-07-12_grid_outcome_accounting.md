# Audit iteration 212 — grid outcome accounting and cohort statistics

## 1. Название итерации

Исправление систематически заниженной proxy-экономики arithmetic futures grid и отделение actionable outcome-статистики от shadow research sample.

## 2. Входной ZIP

- Файл: `bybit-reco-systems-main(1).zip`
- SHA-256: `37fed6a2967d61482d136adfd199818a311a55b0cbece449312909368e491f79`
- Единственный project root: `bybit-reco-systems-main`

## 3. Исходная и новая версии

- Исходная FastAPI version: `1.0.23`
- Новая version: `1.0.24`
- Исходный outcome contract: `grid_label_v4`
- Новый outcome contract: `grid_label_v5`

## 4. Project fingerprint

Fingerprint совпал: README/CHANGELOG/requirements, `main.py`, `app/main.py`, `app/recommender.py`, `app/trading_semantics.py`, `app/grid_math.py`, `app/risk.py`, `app/calibration.py`, `app/outcomes.py`, dual persistence, frontend, tests, docs и обе reference SQL migrations присутствуют. Scope остаётся Bybit `category=linear`, USDT perpetual, `futures_grid`, recommendation/audit-only.

Static production scan не обнаружил private order create/amend/cancel endpoints или SDK-equivalent order placement methods.

## 5. Цель итерации

После этой итерации система должна считать повторные завершённые grid trades, комиссии и остаточный inventory в согласованной capital-normalized модели, не превращать neutral no-fill path в фиктивный убыток, не штрафовать прибыльное направление LONG/SHORT и показывать operator headline только по actionable roots.

## 6. Критерии приемки

1. Три повторных цикла двухинтервальной сетки дают `3 × (1% - 0.10%) / 2 = 1.35%`, а не обрезаются до `grid_count`.
2. Neutral grid без matched crossing и residual inventory имеет proxy-return `0`, без фиктивной комиссии.
3. Благоприятное движение LONG/SHORT даёт положительный signed mark-to-market на оценочную долю остаточного inventory.
4. Движение первой свечи считается от entry/open, а не теряется из-за старта с first close.
5. API отдельно возвращает `actionable`, `shadow_no_trade` и `all_roots` cohort summaries.
6. UI headline читает actionable cohort; combined/shadow sample остаются явно исследовательскими.
7. Новый test падает на pristine source и проходит после production fix.
8. Exact union полного post-check suite остаётся зелёным.

## 7. Прочитанные источники

- проектные README, CHANGELOG, requirements, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports;
- `app/outcomes.py`, `db.py`, `main.py`, `trading_semantics.py`, `grid_math.py`, `recommender.py`, `calibration.py`, frontend outcome renderer и связанные tests;
- официальные материалы Bybit Help Center: *Introduction to Futures Grid Bot* и *P&L Calculations (Futures Grid Bot)*, проверенные 2026-07-12.

## 8. Карта затронутого data flow

`exact contiguous 1m OHLCV -> entry/open and grid level crossings -> completed trade count -> full interval gross -> round-trip execution cost -> committed-grid capital normalization -> residual inventory mark-to-market -> reco_outcomes -> aggregate cohort statistics -> /api/v1/outcomes/stats -> operator headline`.

Не изменялись recommendation generation, deterministic risk gate, execution preflight, publication lifecycle, sizing, Bybit public client, DB schema или order boundary.

## 9. Baseline environment и inventory

- Python `3.13.5`
- Node `v22.16.0`
- 24 production Python files (`app/**/*.py` + root `main.py`)
- 155 test files
- 35 docs
- 3 frontend files
- 2 migration SQL files
- 22 FastAPI routes, из них 6 mutating
- SQLite + PostgreSQL compatibility layer
- supervised background contours: collector, backfill, futures metadata, sentiment, recommender, optional LLM reviewer
- disposable PostgreSQL DSN не предоставлен

Входной ZIP содержал пустые `data/app.db` и `data/app.runtime_locks.sqlite`; integrity check базы прошёл, но recommendations/outcomes/OHLCV отсутствовали. Поэтому пользовательскую screenshot-выборку нельзя было воспроизвести из приложенного DB. Оба runtime-файла исключаются из итогового релиза.

## 10. Baseline commands и результаты

| Команда | Результат |
|---|---|
| archive SHA/safe-entry scan | PASSED: 238 entries; no traversal, absolute paths, symlink escape, duplicates or nested archives |
| `python -m pip check` | FAILED: environment-level `moviepy 2.2.1` requires `pillow<12`, installed `12.2.0`; project dependencies не менялись |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 884 collected |
| monolithic `python -m pytest -q` | TIMED OUT at 73% without final summary |
| exhaustive deterministic batches | PASSED: 295 + 295 + 294 = 884/884; 0 overlaps, 0 omissions |

## 11. Подтверждённые defects/gaps

### DEF-212-01 — cumulative completed trades capped by grid_count

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- Original location: `app/outcomes.py::_grid_outcome`, old completed-step cap around lines 485-487.
- Input: two-grid range repeatedly traversed `100 -> 101 -> 100` three times.
- Actual pristine behavior: only two completed trades survived the cap; additional replacement-order cycles were discarded.
- Expected: `grid_count` is concurrent interval/capital geometry, not a lifetime trade limit. The same interval can complete repeatedly during the horizon.
- Financial/model impact: systematic downward bias of return, win-rate and calibrator target for active oscillating ranges.
- Why old tests missed it: iteration192 asserted a fixed value produced by the cap and an arbitrary gross haircut.
- Fix: removed cumulative cap; `completed_steps=min(up_moves, down_moves)` across exact horizon.
- Regression: `test_repeated_grid_trades_are_not_capped_by_number_of_grids`.

### DEF-212-02 — completed trade gross received an unsupported second haircut

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- Original location: `app/outcomes.py::_grid_outcome`, `fill_efficiency=0.58/0.62`.
- Actual: after a complete interval crossing had already been inferred, gross interval was multiplied by an undocumented fixed coefficient, then costs were deducted again.
- Expected: arithmetic grid profit proxy for a counted completed trade is full effective interval × capital slice minus round-trip execution friction. Fill uncertainty is represented by conservative crossing inference and remains a documented residual limitation.
- Impact: every completed trade was systematically understated by 38-42% before fees.
- Fix: `gross_leg_pct=step_pct`; execution cost deducted once per completed trade.
- Regression oracle: independent arithmetic in iteration212.

### DEF-212-03 — neutral no-fill path manufactured loss

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- Original location: `app/outcomes.py::_grid_outcome`.
- Actual: `max(1, completed_steps)` charged one execution cost even with zero completed trades, and full entry-to-exit drift was subtracted although neutral grid starts flat.
- Expected: no inferred completed trade means no trade fee; unresolved displacement is charged only on estimated residual neutral inventory.
- Impact: quiet/no-fill neutral candidates were forced negative, depressing average return and win-rate.
- Fix: costs use `completed_steps`, not `max(1, ...)`; neutral drift is inventory-weighted.
- Regression: `test_neutral_grid_without_any_fill_has_zero_proxy_return`.

### DEF-212-04 — favourable LONG/SHORT move used the wrong sign

- Severity: **critical**
- Type: **CONFIRMED DEFECT**
- Original location: `app/outcomes.py::_grid_outcome`.
- Actual: aligned directional drift was subtracted (`net_proxy -= aligned_drift * 0.25`), so LONG ending higher and SHORT ending lower became worse.
- Expected: canonical directional PnL sign from `trading_semantics`: aligned movement improves, adverse movement worsens result; only estimated remaining inventory participates.
- Trading/model impact: directional grid labels could invert the economically correct sign and corrupt calibration/operator interpretation.
- Why old tests missed it: two historical tests explicitly required a negative result after favourable LONG/SHORT movement.
- Fix: signed `_signed_return` multiplied by inventory fraction derived from final lattice position.
- Regressions: parameterised `test_directional_grid_aligned_move_adds_unrealized_pnl`.

### DEF-212-05 — first candle move from entry was omitted

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- Original location: `app/outcomes.py::_grid_outcome` initial level index.
- Actual: counting started at the first candle close, dropping the price path from exact entry/open to that close.
- Expected: grid starts at entry; first close participates in level-crossing accounting.
- Impact: first completed grid leg/cycle could disappear from outcome statistics.
- Fix: initialise from `_level_idx(entry)` and iterate all closes.
- Regression: `test_first_candle_move_from_entry_participates_in_grid_cycle`.

### DEF-212-06 — operator headline mixed actionable and shadow cohorts

- Severity: **medium**
- Type: **CONFIRMED DEFECT**
- Original locations: `app/db.py::get_outcomes_stats`, `app/ui/static/app.js::loadOutcomes`.
- Actual: headline win-rate/average return used combined research sample containing `shadow_no_trade` while only counts were separated.
- Expected: operator headline reports actionable roots; all-roots and shadow remain separately labelled research/control metrics.
- Operational impact: a user could read counterfactual no-trade outcomes as the performance of launchable recommendations.
- Fix: additive `cohorts` API object and actionable-only headline.
- Regressions: cohort aggregation and UI contract tests in iteration212.

## 12. Неподтверждённые claims

- Не доказано, что стратегия после исправления прибыльна или убыточна на real fills.
- Не доказано, что найдены все математические дефекты.
- Пользовательская screenshot-статистика не была приложена отдельным изображением, а bundled DB пуст; точный before/after replay реальной месячной выборки невозможен.
- Proxy model не доказывает queue priority, intrabar sequence, partial fills, live fee tier, latency, liquidation waterfall или фактический inventory ledger.

## 13. План исправления

1. Создать independent iteration212 regression test на pristine source.
2. Получить RED по trade count, no-fill, sign, first candle, cohorts и UI.
3. Локально исправить `_grid_outcome` без изменения recommendation/risk architecture.
4. Добавить additive cohort summaries и UI separation.
5. Повысить label contract/version, синхронизировать старые tests, которые закрепляли неверную семантику.
6. Обновить README, trading/risk docs и operator DOCX/PDF/PNG.
7. Выполнить exhaustive post-check и clean-ZIP re-extract validation.

## 14. Фактический diff по файлам

### Production

- `app/outcomes.py` — repeated trade count, full interval gross, no phantom fee, entry-first crossing, residual inventory signed PnL.
- `app/db.py` — additive outcome cohort summaries.
- `app/main.py` — app `1.0.24`, `grid_label_v5`.

### Frontend

- `app/ui/static/app.js` — actionable headline; all/shadow controls separate.

### Tests

- new `tests/test_iteration212_grid_outcome_accounting.py` — 7 test cases;
- updated iteration106 — removed invalid expectation that favourable LONG/SHORT mark-to-market must be negative;
- updated iteration192 — geometry-consistent independent arithmetic oracle;
- updated iterations209/211 — label-version contract `grid_label_v5`.

### Database/migrations

- No schema or migration change.
- Existing startup label-version reset path is reused.

### Docs/operator artifacts

- `README.md`, `CHANGELOG.md`, `docs/TRADING_LOGIC.md`, `docs/KNOWN_RISKS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- updated `docs/instrukciya_operatora_bybit_recommender.docx` and regenerated PDF;
- regenerated root `how_to_trade.png`;
- this audit report.

## 15. Red -> green evidence

RED command on pristine source plus the final regression file:

```bash
python -m pytest -q tests/test_iteration212_grid_outcome_accounting.py
```

Material RED lines:

```text
assert 0.0048 == 0.0135
assert -0.0015735014985015553 == 0.0
assert -0.005 == 0.008          # LONG
assert -0.005 == 0.008          # SHORT
assert -0.0005 == 0.0045        # first candle move omitted
KeyError: 'cohorts'
assert 'data.cohorts?.actionable' in source
7 failed in 0.89s
```

GREEN command:

```bash
python -m pytest -q tests/test_iteration212_grid_outcome_accounting.py
```

GREEN result:

```text
7 passed in 0.68s
```

Related outcome/statistics suites: `61 passed` and `89 passed` in separate targeted runs.

## 16. Database/schema compatibility

No column/table/index change. Fresh SQLite bootstrap and repeated init passed. Existing bundled SQLite database copy upgraded/re-initialised idempotently. PostgreSQL translation/locking suite: `18 passed`. Live PostgreSQL integration was skipped because no verified disposable DSN was provided.

On first v1.0.24 startup, `_bootstrap_db()` sees `grid_label_v5`, deletes incompatible `reco_outcomes` and related calibrator keys, writes the new version and audit event. Recommendations, bot instances, trades and exact execution evidence are not deleted.

## 17. API compatibility

`GET /api/v1/outcomes/stats` remains backward compatible: existing `summary`, breakdowns and `recent` fields remain. New `cohorts` is additive:

- `cohorts.all_roots`
- `cohorts.actionable`
- `cohorts.shadow_no_trade`

No route, request or status contract was removed.

## 18. Config/env compatibility

No new or changed environment variable. `.env.example` remains compatible. No credential is included in release.

## 19. Security and execution boundary

Recommendation/audit-only boundary remains intact. No private Bybit order endpoints were added. No real API key, secret, DSN password, runtime DB or lock DB is packaged.

## 20. Post-check commands и результаты

| Команда | Результат |
|---|---|
| `python -m pip check` | FAILED: unchanged unrelated global MoviePy/Pillow conflict |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: module absent |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 891 collected |
| monolithic `python -m pytest -q` | TIMED OUT at 72% without failure summary |
| exhaustive batches | PASSED: 297 + 297 + 297 = 891/891; 0 overlaps, 0 omissions |
| iteration212 repeated targeted | PASSED: 7/7 |
| SQLite fresh/repeated init | PASSED |
| existing SQLite copy init/upgrade | PASSED |
| PostgreSQL dialect/locking tests | PASSED: 18 |
| production private-order endpoint scan | PASSED: none |
| operator DOCX render/visual review | PASSED: 5/5 pages |
| regenerated operator PDF/PNG review | PASSED |

## 21. Что не удалось проверить

- real Bybit fills and live account state;
- exact screenshot cohort because screenshot bytes and populated DB were absent;
- disposable live PostgreSQL integration;
- ruff, because dependency unavailable in environment;
- clean `pip check`, because global environment contains unrelated MoviePy/Pillow mismatch.

## 22. Остаточные риски

1. Close-to-close 1m crossing inference remains conservative and cannot prove intrabar fill order.
2. Neutral residual inventory entry prices are approximated by lattice displacement, not reconstructed from exact orders.
3. Positive proxy statistics after reset do not prove live alpha.
4. The strategy thesis may still be economically weak even after removing accounting bias; exact execution evidence and chronological walk-forward/shadow analysis remain necessary.
5. Historical proxy outcomes are intentionally discarded because they are incompatible, so the calibrator will remain unfitted until enough v5 labels mature.

## 23. Rollback procedure

1. Stop the application.
2. Restore the previous code release `1.0.23`.
3. Restore a backup of the user-owned `data/app.db` made before first v1.0.24 startup if old v4 proxy outcomes/calibrators must be retained.
4. Do not copy a bundled empty DB over the operational DB.
5. Restart and verify `/api/v1/status` and outcome label version.

## 24. Рекомендуемый следующий work package

After enough `grid_label_v5` rows mature, export actionable and shadow cohorts separately and compare proxy results with exact execution evidence by symbol/direction. The next audit should focus on inventory-path reconstruction sensitivity and whether close-to-close inference materially over- or under-counts actual fills, without changing live gates post hoc.

## 25. Итог

This iteration fixes a confirmed downward accounting bias and an inverted directional PnL sign. It does not claim profitability. The corrected release is suitable for rebuilding the proxy/calibration sample under a more coherent target contract while preserving fail-closed execution boundaries.
