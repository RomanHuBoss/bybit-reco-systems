# Audit Report — Operator diagnostics visibility for blocked/no_trade portfolios, 2026-06-14

## Scope

Audit performed on the uploaded `bybit-reco-systems-main.zip` as a Bybit Linear USDT futures/grid recommendation layer. The repository boundary from `docs/KNOWN_RISKS.md` was preserved: this is a recommendation + fail-closed preflight system, not a live OMS/EMS. No change in this patch turns a non-actionable row into an executable one.

Reviewed first, before changes:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest audit reports under `docs/AUDIT_REPORT_2026-06-14_*`

Reviewed code paths:

- canonical directional semantics: `app/trading_semantics.py`
- operator decision context and next-action generation: `app/main.py`
- recommendation status / blocked / no_trade publication logic: `app/recommender.py`
- operator UI filters/details: `app/ui/static/app.js`, `app/ui/static/index.html`
- regression tests under `tests/`

External reference checked: current Bybit V5 Place Order documentation for `side`, `triggerDirection`, `positionIdx`, `reduceOnly` and conditional order semantics; the patch does not change order semantics.

## Baseline before changes

Commands run from project root:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
```

Results:

```text
compileall: PASS
node --check: PASS
pytest: 672 passed in 37.63s
```

## Findings and fixes

### Finding 1 — MEDIUM — Common blocked causes fell through to generic operator guidance

- **Files:** `app/main.py`, `_operator_next_actions_for_reco`, lines around 1231-1325 after patch.
- **Problem:** Several common fail-closed blockers (`FUNDING_RATE_UNKNOWN`, `INSUFFICIENT_MTF_HISTORY_FOR_GRID`, `RANGE_EDGE_TOO_WEAK_FOR_GRID`, `MARKET_TOO_TRENDY_FOR_GRID`, `LIQUIDITY_UNKNOWN`/low liquidity) were rendered through the generic `READ_GUARD_AND_REFRESH` fallback.
- **Trading/operator risk:** When the portfolio is dominated by blocked rows, the operator sees that launch is prohibited but not what data collection or regime condition must change. This can look like “the recommender has no recommendations” and may encourage unsafe manual overrides.
- **Fix:** Added explicit next-action items:
  - `REFRESH_FUNDING_RATE_SNAPSHOT`
  - `WAIT_FOR_MTF_HISTORY`
  - `WAIT_FOR_RANGE_REGIME`
  - `WAIT_FOR_CONFIRMED_LIQUIDITY`
- **Safety impact:** No gate was weakened. `FUNDING_RATE_UNKNOWN` remains fail-closed; the system now tells the operator to refresh collector/Bybit ticker data and wait for a new publication instead of launching without funding.

### Finding 2 — LOW/MEDIUM — Default operator UI could show an empty recommendation table while diagnostic rows existed

- **Files:** `app/ui/static/app.js`, lines around 1753-1810 and 1893 after patch.
- **Problem:** The default filter shows only `recommended`/`active`. If there are no launchable rows but the latest snapshot contains `pending`, `blocked` or `no_trade`, the table can be empty until the operator manually toggles diagnostic filters. This is safe from an execution standpoint but poor for operational diagnosis.
- **Trading/operator risk:** The operator may conclude that the recommender is broken or that there are “no recommendations at all,” while the system actually has non-actionable rows with safety reasons.
- **Fix:** Added `shouldAutoExpandDiagnostics()`. When the actionable-only filter returns no rows but status counts show diagnostic rows, the UI automatically enables `pending`, `blocked` and/or `no_trade` filters and reloads the table. The banner copy now explains this behavior.
- **Safety impact:** Backend statuses, risk checks, preflight, TP/SL semantics and execution permissions are unchanged. This is a visibility-only patch.

### Finding 3 — LOW — Static asset cache key had to be bumped after JS changes

- **Files:** `app/ui/static/index.html`; cache-key assertions across UI regression tests.
- **Problem:** Browser cache could keep the previous UI JS after the diagnostics patch.
- **Fix:** Bumped static asset key from `manual-ui-v36` to `manual-ui-v37` and updated cache-key tests.

## Directional / Bybit semantics review notes

No new TP/SL inversion or Bybit side mapping issue was found in this bounded pass. The canonical module still enforces:

- long TP above entry and SL below entry;
- short TP below entry and SL above entry;
- one-way linear close/protective side mapping via `bybit_linear_order_semantics()` and `bybit_linear_protective_order_plan()`;
- protective exits as reduce-only/close-on-trigger and trigger-direction-aware.

The new patch does not modify `app/trading_semantics.py`, order side mapping, leverage caps, min-notional logic, liquidation checks or execution preflight.

## Tests added / red→green proof

New file:

- `tests/test_iteration175_operator_diagnostics_visibility.py`

Coverage:

1. `FUNDING_RATE_UNKNOWN` now exposes `REFRESH_FUNDING_RATE_SNAPSHOT` as the first safe next action.
2. MTF-history, range-regime and liquidity blockers have specific next-action codes instead of falling through to generic guidance.
3. Frontend contains `shouldAutoExpandDiagnostics()` and automatically enables diagnostic filters when actionable rows are empty but blocked/no_trade/pending rows exist.
4. Static cache key is `manual-ui-v37`.

Red check against the unmodified uploaded archive with the new test file copied in:

```text
4 failed
- funding-rate test returned READ_GUARD_AND_REFRESH instead of REFRESH_FUNDING_RATE_SNAPSHOT
- MTF/range/liquidity test returned only READ_GUARD_AND_REFRESH
- frontend helper was absent
- static cache key was still manual-ui-v36
```

Post-patch targeted run:

```text
14 passed in 3.10s
```

## Post-change verification

Commands run from project root:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
```

Results:

```text
compileall: PASS
node --check: PASS
pytest: 676 passed in 38.71s
```

`npm`/`yarn` tests were not run because there is no `package.json` in the project root.

## Static scan summary

Created: `docs/STATIC_SCAN_2026-06-14_OPERATOR_DIAGNOSTICS_VISIBILITY.txt`

Changed hits were reviewed as safe:

- `app/main.py`: next-action guidance only; no permission/status relaxation.
- `app/ui/static/app.js`: diagnostic row visibility only; no backend or execution-path change.
- `app/ui/static/index.html`: cache-bust only.

## Residual risks

Unchanged from `docs/KNOWN_RISKS.md`:

- The repository still does not implement real OMS/EMS or live exchange reconciliation.
- Funding, liquidity and MTF-history blockers still depend on fresh collector data.
- A `blocked` or `no_trade` row is still not launchable. The UI now makes the reason and next safe action more visible; it does not authorize execution.
- Exact live margin/liquidation/funding truth must be rechecked by an external execution/reconciliation layer immediately before any real bot creation.

## Changed files

- `app/main.py`
- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `tests/test_iteration122_ui_detail_badge_fit.py` and other existing cache-key assertion tests (`manual-ui-v37`)
- `tests/test_iteration175_operator_diagnostics_visibility.py`
- `docs/STATIC_SCAN_2026-06-14_OPERATOR_DIAGNOSTICS_VISIBILITY.txt`
- `docs/AUDIT_REPORT_2026-06-14_OPERATOR_DIAGNOSTICS_VISIBILITY.md`
