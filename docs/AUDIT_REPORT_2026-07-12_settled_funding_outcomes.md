# Audit iteration: settled funding outcomes

## 1. Release identity

- Input ZIP: `bybit-reco-systems-1.0.36-grid-cost-layer-separation.zip`
- Input SHA-256: `647a964f9c9f21a8deabd3d306e43e28293328710bdb73eb0787cfe248d194dd`
- Source version: `1.0.36`
- Source outcome contract: `grid_label_v17`
- New version: `1.0.37`
- New outcome contract: `grid_label_v18`
- Iteration test: `tests/test_iteration225_settled_funding_outcomes.py`

## 2. Project fingerprint

The archive matched the Bybit Recommender fingerprint: FastAPI recommendation/audit service, `futures_grid`, Bybit Linear USDT perpetual scope, SQLite/PostgreSQL persistence, frontend in `app/ui/static`, and no private order-creation lifecycle. The input ZIP had one project root and no traversal, absolute-path, duplicate-path, symlink, or nested-archive hazard.

## 3. Goal and acceptance criteria

After this iteration, historical proxy outcomes must use actual settled funding cashflows rather than the mutable recommendation-time ticker forecast.

Acceptance criteria:

1. The public client reads `/v5/market/funding/history` only for `category=linear` and the exact normalized symbol.
2. Settled rows are stored immutably by `(symbol, ts)` in both supported database dialects.
3. Historical LONG/SHORT cashflows use the actual signed settled rate and inventory held at the event timestamp.
4. Funding receipts are included in historical Total P&L; approval scoring remains conservative and does not credit forecast receipts.
5. A non-flat expected funding event without a settlement row makes the label unavailable rather than substituting a forecast.
6. Recommendation-time funding forecasts cannot alter an outcome when the actual settlement is unchanged.
7. Existing SQLite databases upgrade additively and preserve existing data.
8. The complete test suite and the re-packed release pass.

## 4. Data-flow map

`Bybit /v5/market/funding/history`
→ `BybitPublicClient.get_funding_rate_history`
→ collector 35-day backfill / hourly refresh
→ `funding_settlement(symbol, ts, funding_rate)`
→ `db.get_funding_settlements`
→ outcome inventory ledger at each settlement timestamp
→ signed funding cashflow
→ `grid_label_v18` Total P&L and success label.

The ticker `fundingRate` remains a forward-looking approval/risk input. It is no longer treated as historical truth.

## 5. Baseline environment

- Python: `3.13.5`
- Node: `22.16.0`
- Baseline collected/passed: `992 / 992`
- Baseline duration: `28.13 s`
- `compileall`: passed
- JavaScript syntax: passed
- `ruff`: unavailable (`No module named ruff`)
- `pip check`: external environment conflict: MoviePy 2.2.1 requires Pillow `<12`, while Pillow 12.2.0 is installed. The project change did not introduce this conflict.

## 6. Confirmed defect

### FUNDING-225 — forecast funding was used as historical settlement

- Severity: **HIGH**
- Type: **CONFIRMED DEFECT**
- Main files: `app/outcomes.py`, `app/collector.py`, `app/bybit_client.py`
- Affected data: historical proxy P&L, success labels, win rate, calibration targets

#### Actual behavior in 1.0.36

The recommendation-time ticker `fundingRate` and derived expected funding fields were reused later by the historical outcome worker. The rate may change before the settlement timestamp, so it is not the immutable cashflow that occurred. The outcome logic also retained only adverse funding costs: a SHORT did not receive positive settled funding and a LONG did not receive negative settled funding.

A missing actual settlement could still produce a numerical loss using the old forecast. In the RED scenario, a flat-price LONG with an expected event but no historical settlement returned:

`(success=0, return=-0.004522613065326633)`

instead of being unavailable.

#### Expected behavior

Historical Total P&L must use the actual signed funding settlement:

- positive rate: LONG pays, SHORT receives;
- negative rate: LONG receives, SHORT pays;
- cashflow magnitude: `abs(position_slots) × event_price × abs(settled_rate)`;
- event applies only to inventory actually open at that timestamp;
- if a scheduled settlement is missing while inventory is non-zero, the label is unavailable.

Official references used to validate the contract:

- Bybit V5 Funding Rate History: `https://bybit-exchange.github.io/docs/v5/market/history-fund-rate`
- Bybit funding fee calculation: `https://www.bybit.com/en/help-center/article/Funding-fee-calculation`

#### Financial and model impact

The defect could bias results in either direction when the forecast differed from the settled rate, and created a systematic pessimistic component by discarding legitimate receipts. It affected Total P&L, binary success, win rate, and all calibration trained on those labels. The defect does not prove that the strategy has positive edge after correction.

#### Why existing tests missed it

The existing suite validated internal handling of forecast funding and conservative approval economics, but did not establish an independent boundary between mutable ticker forecasts and immutable historical settlements. No settlement-history persistence table or outcome fixture existed.

## 7. RED → GREEN evidence

RED command against pristine `1.0.36` plus the new test only:

```bash
python -m pytest -q tests/test_iteration225_settled_funding_outcomes.py
```

RED result:

```text
9 failed in 0.47s
```

Representative failures:

- `BybitPublicClient` had no `get_funding_rate_history`;
- database had no `upsert_funding_settlements`;
- missing settlement produced a fabricated forecast-funded loss;
- application remained `1.0.36 / grid_label_v17`.

GREEN command after the production fix:

```bash
python -m pytest -q tests/test_iteration225_settled_funding_outcomes.py
```

GREEN result, repeated deterministically:

```text
9 passed in 0.49s
9 passed in 0.47s
```

## 8. Implementation

### Production

- `app/bybit_client.py`
  - added strict parsing of settled funding history;
  - exact Linear USDT symbol filtering;
  - rejects booleans, malformed/non-finite rates, fractional millisecond timestamps, and duplicates.
- `app/collector.py`
  - added bounded 35-day settlement backfill and hourly refresh;
  - added collector diagnostics `funding_settlements_written`;
  - errors are logged without converting forecasts into settlements.
- `app/db.py`
  - added strict settlement normalization, upsert, range query, and latest-timestamp lookup.
- `app/db_backend.py`
  - registered the composite upsert key for dual-dialect translation.
- `app/outcomes.py`
  - applies signed settled funding to the inventory ledger;
  - includes receipts in historical P&L;
  - blocks non-flat labels with missing required settlements;
  - forecast fields no longer determine historical funding P&L.
- `app/main.py`
  - version `1.0.37`;
  - `OUTCOME_LABEL_VERSION = grid_label_v18`.

### Database

- `migrations/init.sql`
- `migrations/init_postgres.sql`

Added idempotent table and index:

```sql
funding_settlement(symbol, ts, funding_rate)
PRIMARY KEY(symbol, ts)
```

The change is additive. No destructive migration is required.

### Tests

- New: `tests/test_iteration225_settled_funding_outcomes.py` — 9 cases.
- Existing funding/outcome fixtures were updated only where they had relied on forecast funding as historical truth or asserted the previous version contract.

### Documentation and operator artifacts

Updated README, CHANGELOG, trading logic, risks, architecture, modules, scenarios, infographic source, DOCX, PDF, and PNG. DOCX and PDF were rendered to six pages and every page was visually inspected; no clipping, overlap, blank page, or broken glyph was found.

## 9. Database compatibility

Checks performed:

- fresh SQLite initialization: passed;
- repeated initialization: passed;
- upgrade from a database initialized with v1.0.36 SQL: passed;
- pre-existing sentinel data preserved: passed;
- PostgreSQL dialect/locking/deadlock tests: `24 passed`;
- live PostgreSQL integration: skipped because no explicitly disposable test DSN was supplied.

On startup, the existing outcome-label version guard clears incompatible proxy outcomes and related calibrators. Recommendations, bot lifecycle, trades, exact execution evidence, and risk settings remain preserved.

## 10. API, configuration, and security compatibility

- Public API routes: unchanged.
- Frontend API contract: unchanged.
- `.env` variables: unchanged.
- No private order create/amend/cancel endpoint was added.
- No credentials or production database is included in the release.
- Settlement history uses a public read-only Bybit endpoint.

## 11. Post-check

- Collected: `1001`
- Passed: `1001`
- Failed/errors/skipped/xfailed/xpassed: `0`
- Duration: `27.32 s`
- New regression package: `9/9`, repeated twice
- PostgreSQL dialect/locking package: `24/24`
- Python `compileall`: passed
- JavaScript syntax: passed
- SQLite fresh/repeat/upgrade: passed
- Private order endpoint scan: passed

## 12. Unverified items and residual risks

1. The user’s active monthly `data/app.db` was not available in the input release, so the displayed month cannot be recalculated record by record.
2. Collector backfill is limited to 35 days per current release contract. Older outcome horizons require separately imported historical settlement data.
3. Live PostgreSQL was not tested without a verified disposable DSN.
4. `grid_label_v18` still uses an OHLCV execution proxy for fills and inventory. It does not reconstruct queue priority, partial fills, exact maker/taker status, or intraminute path when that path is unknowable.
5. Actual funding fixes label correctness but does not establish live profitability. A negative result on fresh settled-funding labels and exact fills would be evidence of absent strategy edge rather than this defect.

## 13. Rollback

1. Stop the application.
2. Restore the v1.0.36 code.
3. Restore the `data/app.db` backup made before the first v1.0.37 startup.
4. Do not restore a stale runtime-lock database.

## 14. Recommended next work package

Import the user’s actual working SQLite database into an offline copy and produce a cohort decomposition for the last month:

- old forecast funding versus actual settlements;
- gross grid capture;
- recurring fill fees;
- one-time market friction;
- signed funding cashflows;
- LONG/SHORT/NEUTRAL;
- actionable versus shadow;
- proxy outcome versus exact execution evidence.

This is the shortest path to deciding whether the remaining losses are implementation errors or absence of trading edge.
