# Audit iteration 214 - inventory-aware total-PnL finalization

## 1. Название итерации

Исправление terminal execution cost, funding-by-position-value и success semantics в arithmetic Futures Grid proxy outcomes.

## 2. Входной ZIP

- Файл: `bybit-reco-systems-1.0.25-grid-pnl-ledger.zip`
- SHA-256: `539eb32ebfb20293852855835bf54f0b10bbe6efc240d7f3af3559d19dec2823`
- Archive entries: `234`
- Единственный project root: `bybit-reco-systems-main`
- Safe-entry scan: absolute paths `0`, traversal `0`, duplicate paths `0`, symlinks `0`, nested archives `0`.

## 3. Исходная и новая версии

- Исходная FastAPI version: `1.0.25`
- Новая version: `1.0.26`
- Исходный outcome contract: `grid_label_v6`
- Новый outcome contract: `grid_label_v7`
- SemVer class: patch. Публичные routes, JSON field names, DB schema и environment variables не изменены.

## 4. Project fingerprint

Fingerprint совпал: присутствуют README/CHANGELOG, requirements, `main.py`, FastAPI app, canonical trading semantics, grid/risk/calibration/outcome modules, Bybit public client, SQLite/PostgreSQL persistence, frontend, tests, operator artifacts и обе reference SQL migrations.

Сохранены инварианты:

- только `futures_grid`;
- Bybit `category=linear`, USDT perpetual;
- recommendation/audit-only, не OMS/EMS;
- arithmetic grid;
- SQLite + PostgreSQL;
- fail-closed execution preflight;
- отсутствие private order create/amend/cancel flow.

## 5. Цель итерации

После этой итерации система должна:

1. сравнивать закрытые и остаточные позиции на одной liquidation-equivalent net basis;
2. начислять funding на фактический position value, а не на весь grid capital;
3. сохранять корректный знак funding для LONG/SHORT и не кредитовать receipt как alpha;
4. не начислять funding neutral-grid при нулевом inventory;
5. при неизвестном schedule использовать достигнутый adverse inventory, а не полный капитал;
6. считать положительный net total PnL выигрышем без скрытого порога 5 bps;
7. отклонять нецелый millisecond funding timestamp вместо усечения;
8. не смешивать новые labels с прежней v6 calibration sample.

## 6. Критерии приемки

1. Neutral: sell at 101, horizon close at 102, 10 bps round-trip cost -> exit fee включена; return `-1.1015/200`.
2. При completed pair и net return между `0` и `5 bps` сохраняется `success=1`.
3. Neutral flat/no-inventory с положительным funding rate получает funding cost `0`.
4. LONG с одной slot из двух и одним adverse 10 bps event получает `-5 bps`, не `-10 bps`.
5. SHORT с одной slot из двух и отрицательной ставкой получает симметричные `-5 bps`.
6. Unknown schedule использует maximum adverse inventory, реально достигнутый ledger.
7. Millisecond timestamp с остатком `%1000 != 0` остаётся invalid.
8. `OUTCOME_LABEL_VERSION=grid_label_v7`, FastAPI version `1.0.26`.

## 7. Прочитанные источники

- текущее сообщение пользователя и фактический ZIP;
- README, CHANGELOG, requirements, `.env.example`;
- `KNOWN_RISKS`, `TRADING_LOGIC`, `ARCHITECTURE`, `MODULES`, `SCENARIOS`, infographic source;
- последние audit reports, особенно iterations 209-213;
- `app/outcomes.py`, `main.py`, `grid_math.py`, `recommender.py`, `risk.py`, `calibration.py`, `db.py`, `trading_semantics.py`;
- релевантные outcome/funding/temporal regression tests;
- официальные Bybit Help Center материалы по Futures Grid P&L, funding и trading fees, проверенные 2026-07-12.

Независимые внешние identities: grid profit uses completed interval minus fees; funding fee is position value times funding rate; trading fee is executed order value times fee rate. Potential receipt не использовался как canonical alpha.

## 8. Карта затронутого data flow

`persisted cost_model -> strict funding rate/schedule -> exact funding event times -> current ledger inventory -> adverse position-value cashflow -> terminal residual close cost -> capital-normalized net total PnL -> activity/kill-switch/success -> reco_outcomes -> calibration/stats`.

Не изменялись market-data ingestion, recommendation publication, operator execute lifecycle, API routes, frontend contract, DB schema или security boundary.

## 9. Baseline environment и inventory

- Python `3.13.5`
- Node `v22.16.0`
- 23 production Python files under `app/`
- 157 baseline test files
- 37 baseline docs
- 3 frontend files
- 2 migration SQL files
- maximum existing iteration: `213`
- DB backends: SQLite + PostgreSQL compatibility layer
- disposable PostgreSQL DSN: not supplied

Input ZIP contained `data/app.db` (221184 bytes) and runtime lock DB. The database had `0` recommendations, `0` outcomes, `0` bot instances, `0` trades and `0` OHLCV rows, so user-month statistics were unavailable. Runtime DB files are excluded from the release.

## 10. Baseline commands и результаты

| Команда | Результат |
|---|---|
| ZIP safe scan / SHA | PASSED; 234 entries, one root, no traversal/duplicates/symlinks/nested archives |
| `python -m pip check` | FAILED: global `moviepy 2.2.1` requires `pillow<12`, installed `12.2.0`; project dependencies unchanged |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 900 collected |
| `python -m pytest -q` | PASSED: 900/900 in 24.85s |

Baseline был зелёным; ошибки были закреплены отсутствующими/неверными economic-oracle expectations.

## 11. Подтверждённые defects

### DEF-214-01 - residual inventory omitted terminal execution cost

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- File/function: `app/outcomes.py::_grid_outcome`.
- Pristine behavior: остаточная позиция marked at exit, но exit leg fee/slippage proxy не начислялась.
- Reproducer: neutral `100 -> 102`, sell fill at `101`, round-trip cost `10 bps`.
- Actual v6: `-0.0052525`.
- Expected liquidation-equivalent net: `-0.0055075`.
- Impact: open-residual outcomes систематически выглядели лучше fully-closed equivalents; training target and average return were biased upward.
- Why tests missed it: v6 tests проверяли mark-to-market, но не equality of cost basis at horizon.
- Fix: terminal half-leg cost on absolute residual position at exact horizon exit.

### DEF-214-02 - funding charged to full grid capital instead of position value

- Severity: **critical**
- Type: **CONFIRMED DEFECT**
- Files/functions: `app/outcomes.py::_grid_outcome`, `compute_outcomes_once`.
- Pristine path: ledger result -> subtract `expected_funding_bps / 10000` from full capital.
- Reproducers:
  - neutral with zero inventory: v6 `-10 bps`, expected `0`;
  - LONG with one of two slots: v6 `-10 bps`, expected `-5 bps`;
  - SHORT with one of two slots and negative rate: v6 `-10 bps`, expected `-5 bps`.
- Expected: adverse funding cashflow = `abs(position quantity) × event price × abs(rate)` only when the current side pays. Receipt is excluded, not credited.
- Financial/model impact: systematic false losses and distorted direction/regime calibration; especially severe for neutral/low-inventory paths.
- Why tests missed it: prior test asserted conservative sign only and did not compare funding notional to actual inventory.
- Fix: strict funding model extraction, exact event schedule, current position slots, side-aware adverse cashflow, no post-ledger full-capital subtraction.

### DEF-214-03 - unknown funding schedule still assumed full configured capital

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- File/function: `app/outcomes.py::_grid_outcome`.
- Pristine behavior: aggregate expected bps applied to full grid capital even when only half the slots were reached.
- Expected: conservative unknown-schedule charge uses maximum adverse position value actually reached by the ledger times persisted expected events/rate.
- Fix: `max_adverse_position_value` fallback. Empty neutral inventory remains zero.

### DEF-214-04 - hidden 5 bps threshold contradicted stored positive return

- Severity: **high**
- Type: **CONFIRMED DEFECT**
- File/function: `app/outcomes.py::_grid_outcome`.
- Reproducer: one completed pair, high but survivable execution cost, `0 < ret < 0.0005`.
- Pristine behavior: `ret > 0`, `success=0` because `material_profit_floor=0.0005`.
- Expected: stated total-PnL target uses sign after costs, with separate activity and kill-switch gates. A hidden threshold must not relabel positive PnL as loss.
- Impact: false-negative labels and mismatch between average return and win-rate.
- Fix: numerical epsilon `1e-12`, not a business threshold.

### DEF-214-05 - new funding timestamp parser could manufacture schedule by truncation

- Severity: **high**
- Type: **CONFIRMED DEFECT PREVENTED DURING IMPLEMENTATION**
- File/function: `app/outcomes.py::_extract_inventory_funding_model`.
- Adversarial input: `1700000000123` ms.
- Unsafe behavior in initial working draft: floor to `1700000000` seconds.
- Expected: exact whole-second milliseconds only; non-zero remainder remains unknown.
- Fix: require `%1000 == 0` before conversion. This was caught before release by iteration214 regression.

## 12. Неподтверждённые claims и limitations

- Не доказано, что стратегия прибыльна после исправления.
- Не доказано, что наблюдавшаяся пользователем месячная отрицательная статистика вызвана только этими defects: рабочая база пользователя не была приложена, а database внутри ZIP пуста.
- Future realised funding rates are unknown from a recommendation snapshot.
- Exact intraminute ordering of a funding event versus a grid fill is not recoverable from 1m close-to-close OHLCV.
- Proxy model does not know queue priority, partial fills, live fee tier, per-level qty rounding or liquidation waterfall.

## 13. План исправления

1. Add independent terminal-close/funding/success regressions.
2. Show RED on pristine v1.0.25.
3. Move funding into the inventory ledger.
4. Add exact schedule and conservative actual-inventory fallback.
5. Charge terminal residual close.
6. Remove hidden 5 bps outcome threshold.
7. Preserve strict timestamp semantics.
8. Bump label contract and synchronize docs/operator artifacts.
9. Run targeted, related, full/batched, DB/dialect and clean-ZIP checks.

## 14. Фактический diff по файлам

### Production

- `app/outcomes.py`
  - strict inventory funding model and exact event schedule;
  - side-aware adverse funding on current position value;
  - maximum reached adverse-inventory fallback;
  - terminal close cost;
  - positive-PnL epsilon success semantics;
  - removed full-capital post-ledger funding subtraction.
- `app/main.py`
  - version `1.0.26`;
  - `OUTCOME_LABEL_VERSION=grid_label_v7`.

### Tests

- Added `tests/test_iteration214_total_pnl_finalization.py` - 8 tests.
- Updated prior expectations only where they encoded the old version or omitted terminal close:
  - iteration106;
  - iteration209;
  - iteration211;
  - iteration213.

### Documentation/operator artifacts

- `README.md`, `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`, `KNOWN_RISKS.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- operator DOCX/PDF and `how_to_trade.png` updated to v1.0.26 and visually verified.

### Database/migrations/frontend

- DB schema/migrations unchanged.
- Frontend production files unchanged.

## 15. RED -> GREEN evidence

### RED

Command on pristine v1.0.25 plus only iteration214 test:

```bash
python -m pytest -q tests/test_iteration214_total_pnl_finalization.py
```

Result:

```text
FFFFFFFF
8 failed in 0.51s
terminal return: -0.0052525; expected -0.0055075
positive ret below 5 bps: success 0; expected 1
neutral no inventory: -0.001; expected 0
LONG half inventory funding: -0.001; expected -0.0005
SHORT half inventory funding: -0.001; expected -0.0005
unknown schedule half inventory: -0.001; expected -0.0005
missing grid_label_v7 / version 1.0.26
inventory funding parser absent
```

### GREEN

Same command on working v1.0.26:

```text
8 passed in 0.38s
8 passed in 0.38s  # deterministic repeat
```

Related outcome/funding suite:

```text
113 passed in 2.11s
```

## 16. Database/schema compatibility

- No table/column/index change.
- `migrations/init.sql` and `migrations/init_postgres.sql` unchanged.
- Fresh SQLite bootstrap: 17 tables.
- Repeated SQLite bootstrap: 17 tables; idempotent.
- PostgreSQL support/locking/deadlock regression files: 24 passed.
- Live PostgreSQL integration: SKIPPED because no verified disposable DSN was supplied.
- First v1.0.26 startup removes only incompatible `reco_outcomes` and related calibrators, then stores `grid_label_v7`. Recommendations, bot instances, trades, exact execution evidence and risk settings remain.

## 17. API/config/security compatibility

- No route added/removed.
- No JSON field or environment-variable change.
- No private order endpoint added.
- Recommendation/audit-only boundary unchanged.
- Input runtime DB and lock files are excluded from final ZIP.

## 18. Post-check commands и результаты

| Команда | Результат |
|---|---|
| `python -m pip check` | FAILED only for pre-existing global MoviePy/Pillow mismatch |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: ruff not installed |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 908 collected |
| monolithic `python -m pytest -q` | TIMED OUT after displaying 100%, without final summary; not counted as pass |
| exhaustive disjoint batches | PASSED: `227 + 227 + 227 + 227 = 908`, exact union, no overlaps/omissions |
| iteration214 repeated | PASSED: `8 + 8` |
| related outcome/funding suite | PASSED: 113 |
| PostgreSQL dialect/locking/deadlock suite | PASSED: 24 |
| SQLite fresh/repeated bootstrap | PASSED: 17 / 17 tables |
| DOCX render | PASSED: 5 pages visually checked |
| PDF render | PASSED: 5 pages visually checked, embedded fonts, no forms/encryption |
| PNG infographic | PASSED: v1.0.26, no clipping/overlap |

Batch results:

- batch 1: 227 passed in 4.98s;
- batch 2: 227 passed in 11.74s;
- batch 3: 227 passed in 6.15s;
- batch 4: 227 passed in 5.34s.

## 19. Что не удалось проверить

- Actual month-long user database/screenshot sample was not present; exact before/after recomputation is unavailable.
- No disposable PostgreSQL server integration.
- Ruff unavailable.
- No external exact fills/funding ledger supplied for proxy-vs-real error estimation.

## 20. Остаточные риски

1. Future funding rate may differ from the persisted snapshot.
2. Funding/fill order inside one minute remains ambiguous.
3. Close-to-close model can undercount intrabar fills and same-candle round trips.
4. Equal-quantity slots do not reproduce live qtyStep/minNotional rounding per level.
5. Kill-switch breach forces failure, but proxy ledger is not stopped at the first intrabar breach.
6. Corrected accounting removes known target distortion; it does not create or prove market edge.

## 21. Rollback procedure

1. Stop all application processes.
2. Restore v1.0.25 code.
3. Restore a backup of `data/app.db` made before first v1.0.26 startup if v6 proxy outcomes/calibrators must be retained.
4. Do not copy runtime lock DB between versions.

## 22. Recommended next work package

After enough v7 labels and exact evidence accumulate, perform chronological comparison of:

- proxy inventory/funding/terminal-close PnL;
- exact fills, fees and funding;
- high/low sensitivity bounds;
- symbol/direction cohorts and regime stability.

Thresholds must be selected on training/validation periods and evaluated prospectively on an untouched final interval.

## 23. Clean release verification

The release was assembled with exactly one root directory, `bybit-reco-systems-main`, excluding runtime databases, lock files, caches, bytecode, `.env`, credentials and build artifacts.

Verification from a clean re-extraction:

- ZIP integrity (`unzip -t`): PASSED;
- project fingerprint: PASSED, no required file missing;
- one root directory: PASSED;
- pre-execution archive hygiene: PASSED;
- `python -m compileall -q app tests main.py`: PASSED;
- `node --check app/ui/static/app.js`: PASSED;
- iteration214 regression: 8 passed;
- deterministic repeat: 8 passed;
- test collection: 908 unique nodes;
- static production-code scan: no private Bybit order create/amend/cancel endpoints.

The test run itself creates temporary SQLite databases, `__pycache__` and `.pytest_cache` in the re-extracted verification directory. These generated verification artifacts are not present in the release ZIP.
