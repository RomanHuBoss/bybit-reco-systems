# Audit iteration: grid-ledger topology and protective-stop finalization

## 1. Название итерации

Bybit Recommender v1.0.28 -> v1.0.29: исправление directional order topology, observable minute-path accounting и terminal kill-switch semantics.

## 2. Входной ZIP

`bybit-reco-systems-1.0.28-outcome-entry-contract-integrity.zip`

## 3. SHA-256 входного ZIP

`b73ea3cf02b9d8718747eda882c932e6f85c9bbbab6b924607d484eecfe49f89`

## 4. Исходная версия

- FastAPI: `1.0.28`
- Outcome target: `grid_label_v9`
- Источник версии: `app/main.py`

## 5. Новая версия

- FastAPI: `1.0.29`
- Outcome target: `grid_label_v10`
- SemVer: patch

## 6. Project fingerprint

Совпадает: Bybit Recommender; поддерживается только `futures_grid`; Bybit `category=linear`; USDT perpetual; recommendation/audit-only, не OMS/EMS; SQLite и PostgreSQL; FastAPI в `app/main.py`; frontend в `app/ui/static`; canonical directional semantics в `app/trading_semantics.py`. Private Bybit order create/amend/cancel endpoints не обнаружены.

## 7. Цель итерации

После итерации explicit arithmetic-grid ledger должен:

1. создавать симметричную LONG/SHORT topology при входе между уровнями;
2. учитывать наблюдаемые `previous close -> current open` и `open -> close` сегменты;
3. учитывать односторонний intraminute excursion только когда OHLC задаёт однозначный порядок;
4. завершать cash/inventory/funding evolution при первом kill-switch breach;
5. не размечать отсутствующую, внутреннюю или intrabar-неоднозначную protective geometry.

## 8. Критерии приёмки

1. LONG между уровнями имеет ближайший upper sell и matching initial long slot.
2. SHORT между уровнями имеет ближайший lower buy-to-close и matching initial short slot.
3. Наблюдаемый close->open gap и возврат open->close могут завершить grid pair.
4. Односторонний high/low excursion с возвратом к endpoint учитывается.
5. После kill-switch breach дальнейшее восстановление цены не изменяет outcome.
6. Residual inventory ликвидируется на protective boundary, а не на horizon exit.
7. Missing/inside-range kill-switch делает label unavailable.
8. Одновременное касание обеих outer boundaries в одной свече делает label unavailable.
9. Полный suite, документация и повторно распакованный ZIP проходят проверки.

## 9. Прочитанные источники

README, CHANGELOG, requirements, `.env.example`, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, последние audit reports, `app/outcomes.py`, `app/grid_math.py`, `app/recommender.py`, `app/trading_semantics.py`, `app/main.py`, `app/db.py`, relevant outcome/grid/funding/temporal tests. Дополнительно проверены официальные Bybit Help Center материалы по Futures Grid Bot modes и Futures Grid P&L.

## 10. Карта затронутого data flow

Persisted recommendation grid range/count/direction -> post-publication entry -> initial directional slots and resting orders -> observable minute segments -> replacement orders -> kill-switch termination -> residual liquidation -> fees/funding -> liquidation-equivalent `ret`/`success` -> `reco_outcomes` -> calibration/statistics.

## 11. Baseline environment

- Python `3.13.5`
- Node `v22.16.0`
- Production Python files: 23
- Test files before iteration: 160
- Docs before iteration: 40
- Frontend files: 3
- Migration SQL files: 2
- API routes: 23, mutating routes: 6
- DB backends: SQLite and PostgreSQL compatibility layer
- Input ZIP did not contain a user runtime database; test-created `data/app.db` and runtime lock were removed from release.

## 12. Baseline commands and results

- `python -m pip check`: FAILED due external environment conflict: `moviepy 2.2.1` requires `pillow<12`, installed Pillow is `12.2.0`; unrelated to repository changes.
- `python -m compileall -q app tests main.py`: PASSED.
- `python -m ruff check .`: UNAVAILABLE; ruff not installed.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest --collect-only -q`: `927 tests collected`.
- `python -m pytest -q`: `927 passed in 25.84s`.

## 13. Confirmed defects/gaps

### GLTS-01 - CRITICAL - CONFIRMED DEFECT

- File/function: `app/outcomes.py::_grid_outcome`, non-grid-line LONG/SHORT initialization.
- Original lines: approximately 719-733 in v1.0.28.
- Input: arithmetic range `99..101`, `grid_count=2`, LONG entry `100.5` or SHORT entry `99.5`.
- Actual behavior: nearest adjacent TP order and matching initial directional slot were omitted. Near the range edge the directional bot could start with zero position and no profit-taking order.
- Expected behavior: LONG has sells at every level above entry and one initial slot per sell; SHORT has buys at every level below entry and one initial short slot per buy.
- Financial/model impact: directional gain/loss could be stored as exactly zero; win rate, average return and calibration were distorted.
- Why tests missed: prior tests placed entry exactly on a grid line.

### GLTS-02 - HIGH - CONFIRMED DEFECT

- File/function: `app/outcomes.py::_grid_outcome`, candle traversal.
- Original lines: approximately 773-825 in v1.0.28.
- Input: previous close `100`, next open `101`, next close `100`.
- Actual behavior: only previous close -> current close was compared, therefore no movement and no trade were recorded.
- Expected behavior: close->open crosses the sell at 101 and open->close crosses the replacement buy at 100; one completed grid pair.
- Financial/model impact: observable completed cycles were systematically omitted, especially for narrow grids and minute-boundary gaps.

### GLTS-03 - HIGH - CONFIRMED DEFECT

- File/function: `app/outcomes.py::_grid_outcome`, intraminute single-sided path.
- Input: `open=100, high=101, low=100, close=100`.
- Actual behavior: zero activity.
- Expected behavior: OHLC makes `100 -> 101 -> 100` unambiguous; one completed grid pair.
- Impact: profitable oscillation was erased from proxy PnL.

### GLTS-04 - CRITICAL - CONFIRMED DEFECT

- File/function: `app/outcomes.py::_grid_outcome`, kill-switch handling.
- Original lines: approximately 862-878 in v1.0.28.
- Input: neutral grid sells at 101, upper kill-switch 102 is breached, later price recovers to 100.
- Actual behavior: code only forced `success=0` but continued virtual orders to horizon; stored `ret` became `+0.5%` after a bot that should have stopped.
- Expected behavior: process fills only to 102, liquidate residual short at 102, stop subsequent fills/funding; `ret=-0.5%`.
- Financial/model impact: impossible post-stop profits or losses contaminated return distributions and calibration.

### GLTS-05 - HIGH - CONFIRMED DEFECT

- File/function: `app/outcomes.py::_grid_outcome`, protective geometry validation.
- Input: missing kill-switch or lower/upper boundary inside the persisted range.
- Actual behavior: contract remained labelable.
- Expected behavior: no executable protected grid exists; label unavailable.
- Impact: labels could represent a different risk contract than the one required by preflight/operator semantics.

### GLTS-06 - HIGH - CONFIRMED GAP

- File/function: intrabar protective boundary ordering.
- Input: one OHLC candle reaches both lower and upper kill-switches.
- Actual behavior before fix: final min/max check marked failure but still used a fabricated horizon ledger.
- Expected behavior: OHLC cannot identify first hit; no label should be created.
- Impact: arbitrary first-hit chronology could change position, costs and return sign.

## 14. Unconfirmed claims

The claim that the strategy is intrinsically or necessarily unprofitable remains unconfirmed. The release ZIP contains no user runtime database, so the observed production statistics could not be recomputed. Corrected proxy accounting is not evidence of positive live expectancy.

## 15. Fix plan

- Correct non-grid-line directional order/slot boundaries.
- Add a segment processor for observable price movement.
- Process previous-close->open separately from open->close.
- Count only unambiguous one-sided OHLC excursions; do not invent two-sided sequence.
- Make kill-switch a terminal ledger event and cut off later funding.
- Require outer protective geometry and suppress dual-boundary ambiguity.
- Bump outcome target and synchronize docs/operator artifacts.

## 16. Actual diff by file

### Production

- `app/outcomes.py`
- `app/main.py`

### Tests

- New `tests/test_iteration217_grid_ledger_topology_and_stop.py`
- Updated `tests/test_iteration212_grid_outcome_accounting.py` fixture to preserve valid outer kill-switch after widening range.
- Current-version assertions updated in iteration209, 211, 213, 214, 215 and 216.

### Documentation/artifacts

- README, CHANGELOG, TRADING_LOGIC, KNOWN_RISKS, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC
- Operator DOCX/PDF and `how_to_trade.png`
- This audit report

No route, schema, migration, frontend API or environment-variable change.

## 17. RED -> GREEN evidence

RED command on pristine v1.0.28:

`python -m pytest -q tests/test_iteration217_grid_ledger_topology_and_stop.py`

RED result before version assertion was added:

`8 failed in 0.60s`

Material RED values:

- LONG between levels: `0.0` instead of `0.5 / 201`.
- SHORT between levels: `0.0` instead of `0.5 / 199`.
- close->open round trip: `0.0` instead of `0.005`.
- single-sided intraminute cycle: `0.0` instead of `0.005`.
- post-kill recovery: `+0.005` instead of `-0.005`.
- intraminute kill breach: `0.0` instead of `-0.005`.
- invalid/missing kill-switch returned `(0, 0.0)` instead of unavailable.

GREEN command:

`python -m pytest -q tests/test_iteration217_grid_ledger_topology_and_stop.py`

GREEN results after version assertion: `9 passed in 0.44s`; deterministic repeat `9 passed in 0.44s`.

## 18. Database/schema compatibility

No schema change. Fresh and repeated SQLite bootstrap both produced 16 application tables. Existing version guard clears only incompatible proxy `reco_outcomes` and associated calibrators when `grid_label_v10` is detected. Recommendations, bot instances, trades, exact execution evidence and risk settings remain.

## 19. API compatibility

No route or JSON field change. FastAPI application version changes from `1.0.28` to `1.0.29`.

## 20. Config/env compatibility

No `.env` variable change. Existing configuration remains valid.

## 21. Security boundary

No private Bybit order create/amend/cancel method was added. No production credentials were used. `.env`, runtime DB, runtime lock DB, caches and bytecode are excluded from release ZIP.

## 22. Post-check commands and results

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- Collection: `936 tests collected`.
- New regression: `9 passed`, repeated twice.
- Related outcome/grid/funding suite: `85 passed in 2.36s`.
- PostgreSQL dialect/locking/deadlock suite: `18 passed in 0.65s`.
- Full isolated suite before the final documentation-only consistency correction: `936 passed in 26.60s`.
- Final collection after that correction: `936 tests collected`.
- Final exhaustive deterministic batches: `234 + 234 + 234 + 234 = 936`; all passed (`6.64s`, `12.76s`, `6.50s`, `6.16s`). The four node lists are disjoint and their union equals the collected set.
- SQLite fresh/repeated bootstrap: `16/16` tables.
- Private order endpoint static search: no hits.
- DOCX: 5 rendered pages visually inspected.
- PDF: 5 rendered pages visually inspected.
- PNG infographic visually inspected.
- A final monolithic rerun was terminated by the harness after displaying 76% without a pytest failure summary; it is not counted as a pass. The exhaustive disjoint batches above provide the final complete result.
- First packaged-ZIP verification: archive integrity PASSED; one project root; 242 files; fingerprint PASSED; compileall and Node syntax PASSED; targeted regression `9 passed`; `936` unique nodes collected; four disjoint extracted-archive batches of `234` each all passed (`5.98s`, `12.83s`, `7.36s`, `6.24s`). The extracted monolithic run was also terminated by the harness after partial progress and is not counted.

## 23. Not verified and why

- User live SQLite database and screenshot-derived rows were not present, so observed statistics could not be recalculated.
- Live PostgreSQL integration was not run because no explicitly disposable test DSN was supplied.
- Exact two-sided intrabar order, queue priority, partial fills, maker/taker mix and exchange stop slippage remain outside OHLCV proxy capability.
- Ruff unavailable.
- External MoviePy/Pillow environment conflict remains unrelated.

## 24. Residual risks

- For candles where both high and low extend beyond open/close without reaching both kill-switches, only endpoint movement is counted; intrabar cycles remain intentionally unknown.
- Protective liquidation is modeled at the configured boundary and does not include gap-through/slippage beyond it.
- Proxy labels remain research/calibration targets, not live PnL truth.
- Strategy edge remains unproven until enough chronological `grid_label_v10` and exact execution evidence accumulate.

## 25. Rollback procedure

1. Stop the application.
2. Restore v1.0.28 code.
3. Restore the `data/app.db` backup taken before first v1.0.29 startup if old v9 proxy outcomes/calibrators must be retained.
4. Do not restore a stale runtime lock DB.

## 26. Recommended next work package

After enough `grid_label_v10` observations accumulate, compare proxy ledger against immutable exact fills/funding by symbol, direction and regime. Quantify disagreement specifically for between-level entries, minute-open gaps, single-sided excursions and protective exits before changing strategy thresholds.
