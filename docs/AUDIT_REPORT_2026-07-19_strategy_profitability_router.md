# Audit iteration: validated strategy profitability router and single-position trend audit package

## 1. Input and release identity

- Input ZIP: `bybit-reco-systems-1.1.0-directional-trend-shadow.zip`
- Input SHA-256: `9a0b2399fa6b876cdb5ab79a382d582e571505b5e0e80f12e9ad837ad2ac3069`
- Source version: `1.1.0`
- Release version: `1.2.0`
- Version source of truth: `FastAPI(..., version="1.2.0")` in `app/main.py`
- Iteration test number: `267`
- Release type: backward-compatible **minor** extension of strategy selection and operator audit lifecycle.

## 2. Project fingerprint

The extracted archive contained one project root and matched the Bybit Recommender fingerprint: recommendation/audit service, Bybit Linear USDT Perpetual scope, SQLite/PostgreSQL support, frontend in `app/ui/static/`, existing `futures_grid`, independent `directional_trend`, tests and operator artifacts. Archive traversal, absolute-path, outward-symlink and duplicate-path checks passed. The input ZIP was not modified.

## 3. Goal

After this iteration the system must evaluate grid and trend candidates from the same market snapshot, but select an operator candidate only when one strategy has demonstrably stronger **comparable risk-adjusted monetary evidence**. It must not compare raw grid and trend scores, must preserve the non-winning candidate for paired outcome learning, and must choose `no_trade` when evidence is unavailable, negative, statistically ambiguous or too close.

A selected `directional_trend` candidate must produce one single-position entry/TP/SL audit package. It must not be interpreted as a grid bot and the service must not submit a Bybit order.

## 4. Acceptance criteria

1. Grid and trend raw scores are never compared across strategy families.
2. A candidate is router-eligible only with fitted bot-specific calibration, exact policy fingerprint, positive selected-policy and terminal monetary evidence, positive row/temporal lower bounds, expected shortfall, confidence above its validated threshold, the same 12-hour horizon and the same net-return basis.
3. The winner is selected by a conservative risk-adjusted monetary utility; an immaterial utility edge yields `no_trade`.
4. The loser remains persisted as `shadow_competitor` and receives its own strategy-specific outcome.
5. `futures_grid` remains a grid. `directional_trend` remains one position without grid levels, averaging or pyramiding.
6. A selected trend plan passes separate Bybit tick/qty/leverage, live-price, funding, notional, margin and daily-loss fail-closed checks.
7. Grid and trend cannot simultaneously create running audit instances for the same symbol in one-way mode.
8. Trend materialization creates an internal `external_single_order_audit` and an external/manual execution package, with `exchange_order_submitted=false`; no private order endpoint is added.

## 5. Sources read

Relevant sources included `README.md`, `CHANGELOG.md`, `.env.example`, recent audit reports, `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`, `app/bot_types.py`, `app/recommender.py`, `app/calibration.py`, `app/outcomes.py`, `app/main.py`, `app/risk.py`, `app/trading_semantics.py`, `app/db.py`, `app/db_backend.py`, frontend files and relevant outcome/calibration/execution/dual-database tests.

## 6. Data-flow map

`common market snapshot` -> independent `futures_grid` and `directional_trend` candidates -> bot-specific confidence and monetary evidence -> `strategy-profitability-router-v1` -> selected winner / no clear winner / no eligible strategy -> persistence of both candidates -> family-specific outcome -> family-specific recalibration.

For an operator-confirmed winner:

- grid -> existing external grid-bot audit workflow;
- trend -> `directional-single-order-package-v1` for one external/manual position;
- neither path sends an exchange order from this service.

## 7. Baseline environment

- Python: `3.13.5`
- Node: `22.16.0`
- Source version: `1.1.0`
- Baseline collection: `1222` test nodes
- Database backends: SQLite and PostgreSQL compatibility layer

## 8. Baseline checks

- exhaustive deterministic pytest batches: **1222/1222 PASSED**
- input archive fingerprint and safe extraction: PASSED
- baseline project fingerprint: PASSED

## 9. Confirmed gap: no statistically valid selector between strategy families

- ID: `ITER267-GAP-01`
- Severity: high
- Type: CONFIRMED GAP
- Previous behavior: grid and trend were both evaluated, but trend was forced shadow-only and there was no validated capital-allocation selector. Existing publication priority could not safely compare strategy-family scores because those scores have different meanings and scales.
- Expected behavior: compare only common monetary evidence from each strategy's own calibrated selected policy and refuse to choose when uncertainty remains.
- Model impact: prevents raw-score scale leakage and unsupported promotion of the apparently larger number.
- Financial claim: none. The router operates on proxy evidence and does not prove future profitability or live edge.

## 10. Implemented router contract

New module: `app/strategy_router.py`.

The router requires:

- actionable candidate status;
- `confidence_model.source=bot_logreg` and `fitted=true`;
- valid exact-policy SHA-256 fingerprint;
- positive selected-policy and terminal-selected-policy expectancy status;
- decision-ready empirical expectancy;
- common `unlevered_net_return_on_committed_notional_v1` basis;
- common 12-hour horizon;
- confidence at or above the model's validated selected-policy threshold;
- positive selected and terminal row-level/temporal lower bounds;
- available expected shortfall.

It computes conservative utility from the minimum validated lower bound, a bounded confidence-dependent portion of demonstrated upside, and an expected-shortfall penalty. A candidate with non-positive utility is ineligible. When two candidates are eligible, the leading utility must exceed the runner-up by a minimum absolute or relative edge; otherwise the result is `STRATEGY_UTILITY_EDGE_INSUFFICIENT` and both are held as `no_trade`.

Raw `score` is intentionally excluded.

## 11. Paired learning and outcome integrity

The winning candidate remains actionable. A non-winning actionable peer becomes `suppressed`, is marked `shadow_competitor`, and retains its own outcome lineage. If no strategy is eligible or the edge is unclear, otherwise-actionable candidates become `no_trade` but remain paired strategy-evaluation samples.

Grid and trend outcomes are not pooled. Their observations may share a future market period and are therefore treated as paired/correlated evidence rather than independent trials.

## 12. Clarification of strategy mechanics

- `futures_grid long/short` is a directional grid: it still uses levels and grid inventory mechanics.
- `directional_trend` is not a grid with higher directional confidence. It is one long or short position with one entry, TP and SL.
- The trend plan explicitly sets `entry_model=single_position_no_pyramiding`, `averaging_allowed=false` and `pyramiding_allowed=false`.

## 13. Trend operator and execution boundary

A router-selected trend recommendation may now pass the internal operator audit lifecycle. The generated package contains symbol, side, reference entry, conservatively snapped quantity/leverage, TP, SL and reduce-only exit semantics.

The service does **not** place the order. Materialization creates:

- `bot_type=directional_trend` audit instance;
- `execution_kind=external_single_order_audit`;
- `recommendation_only=true`;
- `exchange_order_submitted=false`;
- `directional-single-order-package-v1` for a manual or external executor.

Static production-code search confirms no `/v5/order/create`, amend, cancel or batch-order endpoint.

## 14. Fail-closed single-position checks

The trend preflight separately validates:

- long/short TP/SL geometry;
- live instrument category and exact symbol metadata;
- tick alignment;
- quantity step and no upward risk rounding;
- leverage step and bounds;
- min/max quantity and minimum notional;
- max position notional and max margin per audit instance;
- current price not already at/exhausting TP or SL;
- current funding availability and adverse funding deterioration;
- remaining daily loss budget;
- no existing conflicting grid/trend instance on the same one-way symbol.

Existing same-family, same-direction lifecycle compatibility is preserved; strategy-family conflicts and opposite-direction conflicts remain blocked.

## 15. Red -> green evidence

Red command on the source copy plus the new test:

```bash
python -m pytest -q tests/test_iteration267_strategy_meta_router.py
```

Material red evidence:

```text
ImportError: cannot import name 'SINGLE_POSITION_BOT_TYPES' from 'app.bot_types'
ERROR tests/test_iteration267_strategy_meta_router.py
```

Green command on final source:

```bash
python -m pytest -q tests/test_iteration267_strategy_meta_router.py
```

Green evidence:

```text
15 passed
```

The final regression was executed twice and passed both times.

## 16. Full-suite regressions caught and fixed before release

The exhaustive suite caught two compatibility defects:

1. An existing frontend test still expected the historical `directional_trend · shadow` label after promotion to an operator-selectable audit package. The expectation was synchronized with the explicit single-position label.
2. The first symbol-conflict implementation blocked allowed same-family/same-direction lifecycle behavior. It was narrowed to block cross-family and opposite-direction conflicts while preserving the previous same-family contract.

The relevant tests were rerun and the complete final batch set is green. No risk or trading assertion was weakened to hide a production defect.

## 17. Changed files

### Production

- `app/strategy_router.py` (new)
- `app/bot_types.py`
- `app/recommender.py`
- `app/main.py`

### Frontend

- `app/ui/static/app.js`
- `app/ui/static/index.html`

### Tests

- added `tests/test_iteration267_strategy_meta_router.py`
- minimally synchronized existing bot-type/version/UI/lifecycle fixtures with the additive strategy-family contract

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

No schema or migration file changed. A temporary runtime SQLite database created by tests is excluded from the release.

## 18. Database and API compatibility

- SQLite fresh schema: PASSED
- Existing disposable SQLite database bootstrap: PASSED
- PostgreSQL dialect/locking compatibility suite included in the focused 83-test run: PASSED
- Live PostgreSQL integration: SKIPPED because no explicitly disposable DSN was supplied
- No manual SQL action is required
- Public payload change is additive: strategy-router diagnostics and a single-position external execution package may appear
- Existing grid plan and grid operator lifecycle remain supported

## 19. Configuration compatibility

No `.env` variable was added or changed. Existing universe, risk profile, grid configuration and outcome horizons remain unchanged. No operator configuration action is required.

## 20. Security boundary

No private Bybit order-create/amend/cancel implementation was added. No production credentials or `.env` are included. The project remains recommendation/audit-only. A real order requires a separate manual action or external execution layer with its own authentication, reconciliation and operational controls.

## 21. Final post-checks

- pytest collection: **1237**
- exhaustive deterministic non-overlapping batches: **1237/1237 PASSED**
- new regression: **15/15 PASSED twice**
- focused router/execution/dual-DB compatibility suite: **83 PASSED**
- `python -m compileall -q app tests main.py`: PASSED
- `node --check app/ui/static/app.js`: PASSED
- SQLite fresh/bootstrap: PASSED
- operator DOCX/PDF render and visual review: PASSED, 15 pages
- PDF metadata/title updated to v1.2.0
- private order endpoint scan: PASSED
- `python -m pip check`: FAILED due to external shared-environment conflict: MoviePy 2.2.1 requires Pillow `<12`, installed Pillow 12.2.0
- `python -m ruff check .`: UNAVAILABLE (`No module named ruff`)

## 22. Unverified items and limitations

- Live PostgreSQL integration was not run without an explicitly disposable DSN.
- Live Bybit/private-account tests were not run and no production credentials were used.
- Actual order fills, queue priority, partial fills, real fees, funding, liquidation and external executor latency are outside this service.
- The utility weights and minimum strategy edge are initial versioned risk controls, not empirically proven optimums.
- Proxy profitability and calibrated probability do not prove future or live profitability.

## 23. Residual risks

- Grid and trend evidence may be sparse and correlated across symbols and timestamps.
- Regime shifts may make historically validated utility stale; existing exact-policy and freshness gates remain mandatory.
- A single-position external package can be executed incorrectly by a manual/external layer; the service cannot attest execution without a future read-only reconciliation contract.
- One-way symbol exclusion avoids conflicting positions but can omit simultaneous opportunities.
- Router thresholds must not be tuned on terminal holdout or reduced merely to create more trades.

## 24. Rollback and next work package

Rollback: deploy the previous `1.1.0` ZIP and restart the service. No database downgrade is required.

Recommended next work package: prospective paired collection of router decisions and external/manual execution evidence, followed by purged temporal evaluation of strategy-selection regret, realized slippage, calibration drift and portfolio-level correlation. Real private order submission remains a separate OMS/executor project and is not part of this service.
