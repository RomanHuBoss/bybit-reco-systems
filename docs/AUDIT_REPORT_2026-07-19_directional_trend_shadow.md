# Audit iteration: independent directional trend shadow policy

## 1. Input and release identity

- Input ZIP: `bybit-reco-systems-1.0.78-operator-outcome-horizon.zip`
- Input SHA-256: `83466429d0c1673e42aec431114a631e67e73205bbd2a98e9a718f7c15cb27c6`
- Source version: `1.0.78`
- Release version: `1.1.0`
- Version source of truth: `FastAPI(..., version="1.1.0")` in `app/main.py`
- Iteration test number: `266`
- Release type: backward-compatible **minor** extension; execution scope is not widened.

## 2. Project fingerprint

The extracted archive contained one project root and matched the Bybit Recommender fingerprint:

- `README.md`, `CHANGELOG.md`, `requirements*.txt`, `main.py`;
- `app/main.py`, `app/recommender.py`, `app/outcomes.py`, `app/calibration.py`, `app/bot_types.py`;
- SQLite and PostgreSQL support;
- frontend in `app/ui/static/`;
- existing executable product `futures_grid` for Bybit Linear USDT Perpetual;
- recommendation/audit service boundary, not OMS/EMS.

Archive traversal, absolute-path, outward symlink and duplicate-path checks passed. The input ZIP was not modified.

## 3. Goal

After this iteration the system must distinguish three policy routes:

1. range / mean-reversion -> existing `futures_grid`;
2. strong coherent long/short trend -> independent `directional_trend` research policy;
3. uncertain or unsupported evidence -> `no_trade`.

The new trend policy must model one directional position with TP/SL, must not average or pyramid against the move, must have a separate outcome/calibration lineage, and must remain impossible to execute until a future evidence-based go/no-go decision.

## 4. Acceptance criteria

1. `directional_trend` is a separate supported bot type, not an alias of long/short grid.
2. Trend scoring rewards trend strength, direction strength and coherence; mean reversion is not a mandatory trend gate.
3. The generated plan contains one entry, TP and SL, with no grid levels, averaging or pyramiding.
4. Every trend recommendation is `shadow_no_trade` with `DIRECTIONAL_TREND_SHADOW_ONLY` and cannot create `bot_instance`.
5. Trend outcome uses a continuous exact 1m path, first unambiguous TP/SL, actual stop gap, capped favorable TP gap, settled funding and round-trip costs.
6. Same-candle TP/SL and missing 1m chronology are censored fail-closed.
7. Grid and trend labels/calibrators are never pooled.
8. Existing grid behavior, API contracts, SQLite/PostgreSQL support and operator artifacts remain valid.

## 5. Sources read

Relevant project sources included:

- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- recent audit reports;
- `app/bot_types.py`, `app/recommender.py`, `app/outcomes.py`, `app/calibration.py`, `app/main.py`, `app/trading_semantics.py`, `app/risk.py`, `app/db.py`, `app/db_backend.py`;
- `app/ui/static/app.js`, `index.html`, `styles.css`;
- relevant lifecycle, calibration, outcome, frontend, security and dual-database regression tests.

## 6. Data-flow map

`OHLCV / ticker / funding / OI` -> multi-timeframe features -> direction and regime aggregation -> strategy routing -> family-specific score -> family-specific plan -> deterministic market/risk vetoes -> recommendation persistence -> family-specific proxy outcome -> family-specific calibrator and observability -> API/UI.

Execution remains a separate operator audit action and accepts only an actionable `futures_grid` plan.

## 7. Baseline environment

- Python: `3.13.5`
- Node: `22.16.0`
- Production Python files: 24
- Baseline tests: 1206 nodes
- Docs: 89
- Frontend files: 3
- Migration SQL files: 2
- Database backends: SQLite and PostgreSQL compatibility layer

## 8. Baseline checks

- `python -m compileall -q app tests main.py`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- exhaustive deterministic pytest batches: **1206/1206 PASSED**
- `python -m pip check`: FAILED due to external shared-environment conflict: MoviePy 2.2.1 requires Pillow `<12`, installed Pillow 12.2.0
- `python -m ruff check .`: UNAVAILABLE (`No module named ruff`)

## 9. Confirmed gap: trend opportunities were not a separate strategy

- ID: `ITER266-GAP-01`
- Severity: medium
- Type: CONFIRMED GAP
- Files: `app/bot_types.py`, `app/recommender.py`, `app/outcomes.py`, `app/calibration.py`
- Previous behavior: only `futures_grid` was supported. Long/short grid remained a mean-reversion grid with directional inventory; strong trend was penalized or blocked rather than evaluated by an independent trend policy.
- Expected behavior: retain grid semantics and add an independent trend policy with different mechanics, score and label.
- Model impact: trend and range hypotheses must not share incompatible labels or calibration coefficients.
- Financial claim: none. The new policy is research-only and does not establish live edge.

## 10. Confirmed safety requirement: trend execution must remain blocked

- ID: `ITER266-SAFETY-01`
- Severity: high
- Type: CONFIRMED GAP / required fail-closed boundary
- Previous risk if implemented incompletely: a new bot type could fall through grid execution parsing or be presented as an actionable bot before its outcome evidence exists.
- Resolution: explicit shadow-only classification, `status=no_trade`, blocking reason `DIRECTIONAL_TREND_SHADOW_ONLY`, no portfolio capacity consumption, no `bot_instance`, and explicit execution-endpoint rejection.

## 11. Implemented strategy routing

- Existing `futures_grid` remains the only executable bot family.
- `directional_trend` is generated only for explicit `long` or `short` direction and coherent trend regime.
- Trend evidence includes minimum timeframe coverage, trend strength, structural direction, coherence, spread, ATR and data-quality checks.
- Mean-reversion evidence is not a trend gate.
- Uncertain evidence remains `no_trade`.
- The research branch does not consume max-running-bot, daily drawdown or execution cooldown capacity, because it never places or audits a running bot. Market-data, funding, spread and shock vetoes remain observable policy conditions.

## 12. Implemented trend plan

The trend contract is `directional_trend_shadow_v1`:

- one proxy position;
- one reference entry;
- one directional TP;
- one directional SL;
- no grid levels;
- no averaging;
- no pyramiding;
- 1x shadow leverage and a fixed proxy-notional used only to normalize outcomes;
- explicit expected RR and cost fields.

## 13. Implemented outcome contract

The trend label is `directional_trend_label_v1`:

- exact continuous 1m chronology;
- first unambiguous TP or SL;
- same candle touching both TP and SL -> censored because intrabar order is unknowable;
- missing minute before exit/horizon -> censored;
- adverse stop gap uses actual candle open;
- favorable TP gap is capped at the stored TP;
- no early-exit funding after the actual exit;
- full round-trip execution-cost floor;
- directional net return, MFE and MAE diagnostics;
- no reconstruction of nonexistent fills or order-book priority.

## 14. Calibration and audit lineage

- Grid label remains `grid_label_v26`.
- Trend label is `directional_trend_label_v1`.
- Grid calibrator key remains `logreg_futures_grid_v21`.
- Trend calibrator key is `logreg_directional_trend_v1`.
- The legacy global calibrator explicitly filters to `futures_grid`; it does not pool trend rows.
- Trend recommendations use a distinct model/audit suffix `+directional-trend-v1` without resetting the existing grid lineage.
- A missing new trend calibrator with no trend evidence is initialized as insufficient without forcing a fresh, valid grid calibrator to refit.

## 15. Frontend and operator semantics

The UI now:

- labels the new family as `Направленный тренд · shadow`;
- renders a single-position entry/TP/SL research plan rather than grid fields;
- states that execution is prohibited;
- does not show the grid creation action for trend rows;
- displays separate per-bot calibration readiness;
- retains textual, not color-only, distinction between grid, trend shadow and no-trade.

The DOCX/PDF operator guide was regenerated and visually checked page by page. `how_to_trade.png` was replaced with a v1.1.0 diagram separating executable grid, shadow trend and no-trade routing.

## 16. Red -> green evidence

Red command on pristine source plus the new test:

```bash
python -m pytest -q tests/test_iteration266_directional_trend_shadow.py
```

Material red evidence:

```text
assert "directional_trend" in ("futures_grid",)
assert 0.0 > 0.3
KeyError: 'strategy_family'
assert callable(_directional_trend_outcome)  # False
assert feature_eligible_total == 1          # got 0
assert trend recommendation row is not None # got None
7 failed
```

Green command on final source:

```bash
python -m pytest -q tests/test_iteration266_directional_trend_shadow.py
```

Green evidence:

```text
16 passed
```

The final 16-test regression was executed twice and passed both times.

## 17. Post-suite compatibility defects caught and fixed

The exhaustive suite caught two implementation regressions before release:

1. `buildOperatorFieldSpecs()` depended on an external trend helper when extracted by existing Node contract tests. It was made self-contained; the existing fail-closed UI tests pass.
2. Adding a missing trend calibrator initially triggered/refactored bot calibrator loading in ways that could disturb fresh negative or stale grid-cache semantics. Loading now preserves the previous grid behavior while lazily initializing a trend calibrator only when no trend evidence exists. Existing monetary-expectancy and stale-calibrator tests pass.

No test was weakened to hide either issue.

## 18. Changed files

### Production

- `app/bot_types.py`
- `app/recommender.py`
- `app/outcomes.py`
- `app/calibration.py`
- `app/main.py`

### Frontend

- `app/ui/static/app.js`
- `app/ui/static/index.html`

### Tests

- added `tests/test_iteration266_directional_trend_shadow.py`
- minimally updated version assertions and the former single-product UI expectation for the documented additive bot family

### Documentation

- `README.md`
- `CHANGELOG.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- `docs/KNOWN_RISKS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- operator guide DOCX/PDF
- `how_to_trade.png`

### Database/migrations

No schema or migration files changed.

## 19. Database compatibility

- SQLite fresh schema: PASSED
- Existing SQLite database copied to a disposable temporary path and passed `db.init_db()` upgrade/bootstrap: PASSED
- PostgreSQL dialect, row-locking and transaction-order tests: **20 PASSED**
- Live PostgreSQL integration: SKIPPED because no explicitly disposable DSN was supplied
- No manual SQL action is required.

The new bot type is stored in existing text fields and does not require an enum/schema migration.

## 20. API compatibility

The change is additive:

- recommendation payloads may now contain `bot_type=directional_trend`;
- trend rows are always non-actionable shadow rows;
- existing `futures_grid` fields and execution behavior remain unchanged;
- execution preflight explicitly rejects trend with a stable diagnostic code.

## 21. Configuration compatibility

- No `.env` variable was added or changed.
- Existing universe, grid thresholds, leverage profile and execution settings remain unchanged.
- No operator configuration action is required.

## 22. Security and execution boundary

Static search found no private Bybit order-create/amend/cancel endpoints in production code. No `.env`, key files or obvious credential assignments are included. The service remains recommendation/audit-only; no live order method, automatic TP/SL placement or real execution adapter was added.

## 23. Final post-checks

- pytest collection: **1222**
- exhaustive deterministic non-overlapping batches: **1222/1222 PASSED**
- new regression: **16/16 PASSED twice**
- focused trend/grid/outcome/UI/docs compatibility suite: **102 PASSED**
- PostgreSQL dialect/locking/transaction tests: **20 PASSED**
- `compileall`: PASSED
- Node syntax: PASSED
- SQLite fresh/upgrade: PASSED
- version consistency: PASSED
- private order endpoint scan: PASSED
- secret/release-sensitive file scan: PASSED
- DOCX/PDF visual review: PASSED, 15 pages
- infographic visual review: PASSED, 1600x1200 PNG

## 24. Unverified items and environment limitations

- `ruff`: unavailable in the environment.
- `pip check`: external MoviePy/Pillow conflict remains; the project dependencies changed neither package.
- Live PostgreSQL integration: not run without a disposable DSN.
- Live Bybit/network tests: not run; the iteration did not require external market calls and no production credentials were used.
- Profitability and live edge: not established.

## 25. Residual risks

- Trend thresholds and the 12-hour trend horizon are initial research contracts, not empirically proven optima.
- Shadow observations can remain sparse and cross-symbol correlated.
- 1m OHLCV cannot determine TP/SL order inside one candle; such rows are correctly censored, reducing sample size.
- A future execution release requires a separate purged walk-forward, calibration acceptance, tail-risk review, sizing model, operator go/no-go and another audited version.

## 26. Rollback and next work package

Rollback: deploy the previous `1.0.78` archive. No database downgrade is required because this iteration made no schema change. Existing trend shadow rows will remain inert historical text records and are unsupported by the old runtime.

Recommended next work package: accumulate trend shadow outcomes, then compare `futures_grid` and `directional_trend` with non-overlapping temporal cohorts, purged validation, calibration quality, net expectancy lower bounds, expected shortfall, MFE/MAE and regime stability. Do not enable trend execution merely because individual shadow outcomes are positive.
