# Audit iteration 213 - exact arithmetic-grid PnL ledger

## 1. Название итерации

Исправление канонической экономики завершённой arithmetic-grid пары и замена грубого outcome drift-proxy на явный close-to-close order/inventory ledger.

## 2. Входной ZIP

- Файл: `bybit-reco-systems-1.0.24-grid-outcome-accounting.zip`
- SHA-256: `06bf8e4185723414a30f6ac0deb489cc07b1d57bc9cab339b7d5d1be3525631a`
- Archive entries: `240`
- Единственный project root: `bybit-reco-systems-main`
- Safe-entry scan: absolute paths `0`, traversal `0`, duplicate paths `0`, symlinks `0`, nested archives `0`.

## 3. Исходная и новая версии

- Исходная FastAPI version: `1.0.24`
- Новая version: `1.0.25`
- Исходный outcome contract: `grid_label_v5`
- Новый outcome contract: `grid_label_v6`
- SemVer class: patch; публичные routes, JSON field names, DB schema и environment variables не ломаются.

## 4. Project fingerprint

Fingerprint совпал. Присутствуют README, CHANGELOG, requirements, root/main, FastAPI app, recommender, canonical directional semantics, grid/risk/calibration/outcome modules, Bybit public client, dual SQLite/PostgreSQL persistence, frontend, tests, operator artifacts и обе reference SQL migrations.

Сохраняются инварианты:

- только `futures_grid`;
- Bybit `category=linear`, USDT perpetual;
- recommendation/audit-only, не OMS/EMS;
- arithmetic grid;
- SQLite и PostgreSQL;
- fail-closed execution preflight;
- отсутствие private order create/amend/cancel flow.

Static scan не обнаружил `/v5/order/create`, amend/cancel/batch endpoints или SDK-equivalent order placement methods в production-коде.

## 5. Цель итерации

После этой итерации система должна:

1. считать gross PnL уже завершённой grid-пары по полному соседнему ценовому интервалу, а не по 70% интервала;
2. использовать одинаковую arithmetic geometry в recommendation, trade plan, live economics и outcome;
3. моделировать LONG/SHORT/NEUTRAL через cash, initial inventory, level orders, replacement orders и marked residual position;
4. не подменять persisted range/count cost-widened или stale spacing alias;
5. сохранять conservative close-to-close boundary и не выдавать OHLCV proxy за exchange fill truth.

## 6. Измеримые критерии приемки

1. `step_pct=0.60%` даёт `gross_profit_bps=60`, независимо от `fill_efficiency=0.70`; projected capture остаётся отдельным полем `42 bps`.
2. `tp_per_leg.abs` для range `90..110`, `grid_count=20` равен `1.0`, а не `0.7`.
3. Neutral path `100 -> 101 -> 100` в двухинтервальной сетке даёт `+0.5%` без costs и `success=1`.
4. LONG path `100 -> 110` в двадцатиинтервальной сетке даёт `+2.75%`, а не `0`.
5. Neutral monotonic path `100 -> 110` даёт weighted inventory loss `-2.25%`, а не coarse `-5%`.
6. LONG adverse path `100 -> 90` даёт weighted inventory loss `-7.25%`, а не coarse `-10%`.
7. Outcome использует exact `(upper-lower)/grid_count`; stale `grid_spacing_pct` не переписывает geometry.
8. Один и тот же новый regression test падает на pristine `1.0.24` и проходит на working `1.0.25`.
9. Полный suite, compileall, frontend syntax и clean-ZIP targeted checks проходят.

## 7. Прочитанные источники

- текущее сообщение пользователя и текущий ZIP;
- README, CHANGELOG, requirements, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports, особенно iteration209-212;
- `app/grid_math.py`, `recommender.py`, `outcomes.py`, `main.py`, `trading_semantics.py`, `risk.py`, `calibration.py`, `db.py`;
- frontend economics/outcome renderers и связанные regression tests;
- официальные Bybit Help Center materials: *Introduction to Futures Grid Bot*, *P&L Calculations (Futures Grid Bot)* и Futures Grid FAQ, проверенные 2026-07-12.

Внешний контракт, использованный как независимый oracle: завершённая grid trade использует полный interval и fees; total PnL включает realised/grid component и unrealised/marked open position. Neutral начинается без initial position, directional modes используют initial directional inventory.

## 8. Карта затронутого data flow

`range + strict grid_count -> adjacent arithmetic interval -> recommendation cost floor / tp_per_leg / gross edge -> exact next-candle entry -> close-to-close level crossings -> cash + lots + replacement orders -> per-leg execution cost -> marked residual inventory -> kill-switch success gate -> reco_outcomes -> calibration/stats`.

Не изменялись Bybit network ingestion, publication-chain, operator execute lifecycle, DB schema, API routes, security model или real-order boundary.

## 9. Baseline environment и inventory

- Python `3.13.5`
- Node `v22.16.0`
- 23 production Python files under `app/`
- 156 baseline test files
- 36 docs before this report
- 3 frontend files
- 2 migration SQL files
- maximum existing iteration: `212`
- DB backends: SQLite + PostgreSQL compatibility layer
- disposable PostgreSQL DSN: not supplied; live integration skipped

## 10. Baseline commands и результаты

| Команда | Результат |
|---|---|
| ZIP safe scan / SHA | PASSED; 240 entries, one root, no traversal/duplicates/symlinks/nested archives |
| `python -m pip check` | FAILED because global `moviepy 2.2.1` requires `pillow<12`, installed `12.2.0`; project dependencies were not changed |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 891 collected |
| `python -m pytest -q` | PASSED: 891/891 in 24.29s; process duration 27.68s |

Baseline был зелёным. Это важно: обнаруженные дефекты были закреплены существующими expectations, а не проявлялись как случайно падающие тесты.

## 11. Подтверждённые defects

### DEF-213-01 - completed-trade economics received a 30% haircut

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- Original files/functions: `app/grid_math.py::grid_leg_economics`, `app/recommender.py::_params`, `_build_trade_plan`, `app/outcomes.py::_resolve_grid_tp_leg_abs`.
- Reproducer: `step_pct=0.60`, `fill_efficiency=0.70`.
- Pristine behavior: `gross_profit_bps=42` instead of `60`; `tp_per_leg=0.7*step`; minimum spacing divided by 0.70; live gross edge understated.
- Expected: once an adjacent pair is completed, gross price PnL is the full adjacent interval. Fill/opportunity capture may estimate how often a trade completes, but cannot reduce the PnL of a trade already classified as complete.
- Financial/trading impact: distorted grid density, overly wide ranges, understated gross/net edge, inconsistent operator TP and live preflight economics.
- Why tests missed it: unit expectations copied the same 70% coefficient and therefore tested implementation, not an independent arithmetic identity.
- Fix: canonical gross is full step; `fill_efficiency` moved to `projected_capture_bps` / `projected_net_profit_bps`; TP and cost floor use full interval.
- Regression: first three tests in `test_iteration213_grid_pnl_ledger.py`.

### DEF-213-02 - outcome used abstract drift instead of the bot order/inventory path

- Severity: **critical**
- Type: **CONFIRMED DEFECT**
- Original file/function: `app/outcomes.py::_grid_outcome`.
- Pristine data path: close index movement -> `min(up_moves, down_moves)` -> guessed inventory fraction -> one signed end drift.
- Reproducers:
  - LONG `100 -> 110`: pristine `0`, expected ledger `+2.75%`;
  - NEUTRAL `100 -> 110`: pristine `-5%`, expected `-2.25%`;
  - LONG `100 -> 90`: pristine `-10%`, expected `-7.25%`.
- Expected: directional initial inventory is closed at successive sell/buy levels; neutral inventory is opened at actual crossed levels; residual position is marked at exit.
- Financial/model impact: wrong sign/magnitude of training target and win-rate; calibrator could learn from synthetic losses or omit real directional gains.
- Why tests missed it: iteration212 intentionally accepted a fractional residual-drift approximation and did not test monotonic directional order closure.
- Fix: equal-quantity order ledger with cash, initial lots, grid lots, replacement orders, actual level prices, half round-trip cost per executed leg and horizon mark-to-market.
- Regression: monotonic LONG, monotonic NEUTRAL and adverse LONG tests.

### DEF-213-03 - one profitable neutral pair was labelled unsuccessful

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- Original file/function: `app/outcomes.py::_grid_outcome`.
- Pristine behavior: `ret=+0.5%` but `success=0` unless at least two completed cycles existed.
- Expected: one completed profitable neutral pair is valid grid activity. Two-cycle minimum is not an exchange or mathematical requirement.
- Impact: systematic false-negative labels, especially for wide/slow grids and shorter horizons.
- Fix: neutral mode activity requires one completed pair; directional mode accepts actual initial-position closure, grid activity or material directional movement, always subject to positive total PnL and kill-switch integrity.
- Regression: `test_one_profitable_neutral_grid_pair_is_a_success`.

### DEF-213-04 - outcome silently evaluated a different grid geometry

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- Original file/function: `app/outcomes.py::_grid_outcome`.
- Reproducer: persisted `range=99..101`, `grid_count=2`, stale `grid_spacing_pct=0.1%`, execution cost `80 bps`.
- Pristine behavior: step was widened from costs and result became `0.257142857%`.
- Expected exact persisted geometry result: `[1 - 0.004*(101+100)]/200 = 0.098%`.
- Impact: historical label lineage broken; recommendation and outcome could refer to different bots.
- Fix: step derives only from finite persisted lower/upper and strict integer `grid_count`; stale spacing/cost values do not rewrite geometry.
- Regression: `test_outcome_uses_persisted_range_and_grid_count_not_cost_widened_step`.

## 12. Неподтверждённые claims и limitations

- Не доказано, что стратегия после исправления прибыльна.
- Не доказано, что все месячные losses пользователя были вызваны только этими дефектами: пользовательская working DB/screenshot dataset не входили в текущий ZIP.
- OHLCV cannot identify intrabar order sequence, queue priority, partial fills, fee tier, qty rounding or liquidation waterfall.
- Current v6 inference uses close-to-close crossings deliberately; high/low affect kill-switch only and are not used to manufacture optimistic fills.
- Funding remains conservative and horizon-level, not inventory-time-weighted. This is a documented next audit package, not silently claimed exact.

## 13. План и фактическая реализация

1. Добавить independent regression oracle.
2. Показать RED на pristine v1.0.24.
3. Separate completed-trade PnL from opportunity capture.
4. Align TP, spacing and gross edge with arithmetic interval.
5. Replace coarse outcome formula with explicit ledger.
6. Bump incompatible label contract.
7. Update historical tests only where they encoded the invalid formula.
8. Run targeted, related, full, DB/dialect and release checks.
9. Synchronize operator docs and build clean ZIP.

## 14. Фактический diff по файлам

### Production

- `app/grid_math.py`
  - full adjacent interval for canonical gross/net;
  - added diagnostic `projected_capture_bps` and `projected_net_profit_bps`.
- `app/recommender.py`
  - `tp_per_leg` equals arithmetic step;
  - cost floor no longer divides by 0.70;
  - gross edge estimate uses full step.
- `app/outcomes.py`
  - full TP fallback;
  - explicit order/inventory ledger;
  - exact persisted geometry;
  - updated success semantics.
- `app/main.py`
  - version `1.0.25`;
  - `OUTCOME_LABEL_VERSION=grid_label_v6`.

### Tests

- Added `tests/test_iteration213_grid_pnl_ledger.py` - 9 tests.
- Updated historical expectations in:
  - `test_grid_linear_economics.py`;
  - iterations 106, 126, 192, 209, 211 and 212.

No old test was weakened merely to make the suite green. Each changed expectation encoded the confirmed 70% haircut, cost-widened geometry, two-cycle rule or coarse drift approximation.

### Documentation / operator artifacts

- `README.md`, `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`, `KNOWN_RISKS.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX/PDF and `how_to_trade.png` updated to v1.0.25 and visually checked page by page.

### Database/migrations/frontend

- DB schema/migrations: unchanged.
- Frontend production files: unchanged; existing fields remain API-compatible.

## 15. RED -> GREEN evidence

### RED

Command on pristine v1.0.24 plus only the new test:

```bash
python -m pytest -q tests/test_iteration213_grid_pnl_ledger.py
```

Result:

```text
FFFFFFFFF
9 failed in 0.51s
Obtained gross_profit_bps: 42.0; expected: 60.0
Obtained TP fallback: 0.875; expected: 1.25
Obtained tp_per_leg: 0.7; expected: 1.0
LONG 100->110: 0.0; expected: 0.0275
NEUTRAL 100->110: -0.05; expected: -0.0225
LONG 100->90: -0.1; expected: -0.0725
cost-widened outcome: 0.0025714286; expected: 0.00098
```

### GREEN

Same command on working v1.0.25:

```text
9 passed in 0.50s
```

Repeated deterministic run:

```text
9 passed in 0.48s
```

## 16. Database/schema compatibility

- No column/table/index change.
- `migrations/init.sql` and `init_postgres.sql` unchanged.
- Fresh SQLite bootstrap: 17 tables.
- Repeated SQLite bootstrap: 17 tables; idempotent.
- PostgreSQL translation/locking regression suite: 20 passed.
- Live PostgreSQL integration: SKIPPED because no verified disposable test DSN was supplied.
- On first v1.0.25 startup, existing version guard removes only incompatible `reco_outcomes` and related calibrators, then stores `grid_label_v6`. Recommendations, bot instances, trades and exact execution evidence remain.

## 17. API/config/security compatibility

- No route added/removed.
- Existing economics fields remain; two diagnostic projected fields are additive.
- No environment-variable action required.
- No private order endpoint added.
- No credentials, `.env`, production DB or runtime lock DB may enter release ZIP.
- Recommendation/audit-only boundary unchanged.

## 18. Post-check commands и результаты

| Команда | Результат |
|---|---|
| `python -m pip check` | FAILED only for pre-existing global MoviePy/Pillow mismatch |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: ruff not installed |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 900 collected |
| `python -m pytest -q` | PASSED: 900/900 in 24.08s; process duration 27.16s |
| iteration213 repeated | PASSED: 9 + 9 |
| PostgreSQL dialect/locking + DB retry suite | PASSED: 20 |
| SQLite fresh/repeated bootstrap | PASSED: 17 / 17 tables |
| DOCX render | PASSED: 5 pages visually checked |
| PDF render | PASSED: 5 pages visually checked |
| PNG infographic | PASSED: v1.0.25, no clipping/overlap |
| clean-ZIP monolithic `pytest -q` | TIMED OUT near 80% without failure summary; not counted as a pass |
| clean-ZIP exhaustive disjoint batches | PASSED: 300 + 300 + 300 = 900 unique collected nodes; 0 overlaps/omissions |
| clean-ZIP compileall / Node / iteration213 repeated | PASSED; 9 + 9 targeted tests |
| clean-ZIP fingerprint | PASSED: one root, version 1.0.25, label v6, all required files present |

## 19. Что не удалось проверить

- User's actual month-long DB and screenshot-derived sample were unavailable in the submitted v1.0.24 ZIP, so exact before/after recomputation of that live-looking cohort was not possible.
- No disposable PostgreSQL DSN; live PostgreSQL integration skipped.
- Ruff unavailable.
- No external Bybit execution ledger was supplied, so proxy-vs-real fill error cannot be estimated.

## 20. Остаточные риски

1. Close-to-close inference can undercount a level touched intrabar and can miss same-candle round trips.
2. Funding charge is conservative but not weighted by exact inventory duration.
3. Equal-quantity normalized slots do not include live qtyStep/minNotional rounding per level.
4. Kill-switch breach forces failure but proxy return is still computed to horizon rather than stopping the ledger exactly at first breach.
5. Corrected math removes known target distortion; it does not create or prove market edge.

## 21. Rollback

1. Stop all application processes.
2. Restore v1.0.24 code.
3. Restore a backup of `data/app.db` made before the first v1.0.25 startup if retaining old v5 proxy outcomes/calibrators is required.
4. Do not copy runtime lock DB between versions.

## 22. Recommended next work package

After enough v6 labels and exact execution evidence accumulate, compare by symbol/direction:

- close-to-close inferred fills;
- high/low sensitivity bounds;
- exact fills and realised net PnL;
- inventory-time-weighted funding.

The analysis must be chronological and prospective. Do not tune thresholds on the same final validation sample.
