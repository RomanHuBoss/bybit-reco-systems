> Superseded note (2026-06-15 3-5x sync): current shipped leverage policy is the adaptive interval `min_leverage=3`, `max_leverage=5`. See `docs/AUDIT_REPORT_2026-06-15_3X_5X_LEVERAGE_SYNC.md` for the latest patch.

# Audit report — 2026-06-15 — deep Bybit linear USDT re-audit patch

## Scope and starting point

This was a bounded offline re-audit of the received repository as a Bybit Linear USDT futures recommendation/preflight service, not as a live OMS/EMS. Before code changes I reviewed the required source-of-truth and recent-audit materials:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest audit reports dated 2026-06-14, especially the notional, margin, protective-reference/qty and UI worst-case margin patches.

The existing boundary remains valid: the repository is an operator recommendation + fail-closed preflight layer. It does not manage live order lifecycle, fills, partial fills or exchange-side reconciliation. Issues about real order idempotency, partial fills and live open-order state remain requirements for an external execution/reconciliation layer, not bugs in nonexistent OMS code.

## Baseline before changes

Commands run from project root before changes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q --tb=short -x
pytest --collect-only -q
```

Baseline results:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- `pytest -q --tb=short -x`: stopped at the first failure with `326 passed, 1 failed`.
- Failing baseline test: `tests/test_iteration162_docs_and_infographic_sync.py::test_env_example_matches_current_shipped_3x_5x_operator_profile`.
- `pytest --collect-only -q`: `682 tests collected`.
- Full non-`-x` baseline run did not produce a final summary before the local timeout, but the first-failure run above gives the blocking regression precisely.

The baseline was therefore not green. The first blocking problem was already covered by an existing red regression test.

## Trading semantics map reviewed

Single-source directional model:

- `app/trading_semantics.py`: canonical long/short/neutral normalization, TP/SL mapping, directional PnL/risk-reward, Bybit one-way `Buy`/`Sell` semantics and protective `reduceOnly`/`closeOnTrigger` trigger mapping.
- `app/main.py`: API/operator payload enrichment, execution preflight, Bybit metadata snapping, runtime size/risk guards, directional exit payload construction, current-price and freshness gates.
- `app/recommender.py`: grid construction, risk-gated recommendation publication, leverage and funding economics, worst-case grid notional/margin fields.
- `app/grid_math.py`: linear PnL, funding cashflow sign convention, margin and approximate liquidation buffer helpers.
- `app/ui/static/app.js`: operator UI rendering of direction, TP/SL, worst-case notional/margin and position-size fields.
- Tests: directional semantics, Bybit protective order semantics, UI short TP/SL, invalid exits, worst-case notional/margin, directional qty and nested grid count regressions.

No new backend TP/SL inversion was found. Long/short TP/SL and protective trigger semantics remain centralized in `app/trading_semantics.py` and are covered by existing tests.

## Findings and fixes

### HIGH: shipped `.env.example` drifted from the documented 3-3-5x operator profile

- Severity: high.
- Files:
  - `.env.example`, line 62.
  - Existing red test: `tests/test_iteration162_docs_and_infographic_sync.py`, lines 18-25.
- Problem:
  - Documentation and runtime defaults describe the shipped profile as `min_leverage=3`, `max_leverage=5`.
  - `.env.example` still shipped `min_leverage=3`, `max_leverage=5`.
  - The existing regression test failed before any modifications.
- Trading/risk impact:
  - Operator deployments copied from `.env.example` could run under a 3-3-5x profile while the operator documentation and UI copy described exactly 5x.
  - That creates a semantic mismatch between published recommendation/actionability assumptions and the runtime risk profile.
  - This is not a fail-open code-path change, but it is a high-severity configuration drift because it changes which recommendations are considered actionable.
- Fix:
  - Updated `.env.example` to `"min_leverage":3,"max_leverage":5`.
  - This aligns the sample deployment profile with `README.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`, `docs/KNOWN_RISKS.md` and runtime default tests.
- Red→green:
  - Existing test was red in the received archive: `assert 3 == 5`.
  - After the fix the test passes.

### MEDIUM: operator UI could overstate displayed base qty when notional came from worst-case grid price

- Severity: medium.
- Files:
  - `app/ui/static/app.js`, lines 685-706 and 901-978.
  - `app/ui/static/index.html`, lines 7 and 126.
  - New test: `tests/test_iteration173_env_and_ui_qty_consistency.py`, lines 14-36.
- Problem:
  - The previous UI correctly preferred `estimated_worst_case_total_order_notional_usdt` for position notional display.
  - However, if explicit base qty was absent, it derived displayed base qty as `positionNotional / referencePrice`.
  - For a fixed-qty grid with `reference=100`, `upper=150`, and `worst_case_total_notional=1500`, that displays `15` base units even though the executable qty is `1500 / 150 = 10` base units.
- Trading/risk impact:
  - Execution preflight and backend risk caps already use the more conservative worst-case notional model, so this did not weaken execution gates.
  - The operator UI could still present a misleading base-asset quantity, making manual review and copy/check steps inconsistent with backend directional qty semantics.
- Fix:
  - Added `firstFiniteField(...)` so the UI knows which notional field supplied `positionNotional`.
  - Added `gridMaxNotionalPrice(referencePrice, rangeLower, rangeUpper)` mirroring the backend worst-case price convention.
  - If the selected notional source is a worst-case/max grid exposure field, inferred base qty now divides by the highest positive executable grid/reference price, not by `referencePrice`.
  - Explicit qty fields still take precedence.
  - Bumped static asset cache key from `manual-ui-v37` to `manual-ui-v38` and updated cache-key tests so stale JS does not mask the fix.
- Red→green:
  - New regression file was copied to the original received tree and failed with two failures: missing `gridMaxNotionalPrice(...)` and old `manual-ui-v37` cache key.
  - The same tests pass after the patch.

### LOW: static asset cache-key tests needed synchronized update after UI change

- Severity: low.
- Files:
  - `app/ui/static/index.html`, lines 7 and 126.
  - Existing UI cache-key tests across `tests/test_iteration122_*` through `tests/test_iteration175_*` that assert the current static version.
- Problem:
  - The repository intentionally uses static cache-key assertions to prevent stale operator JS after UI safety patches.
  - Once `app.js` changed, leaving `manual-ui-v37` would risk browsers serving stale code.
- Fix:
  - Bumped index references to `manual-ui-v38`.
  - Synchronized existing cache-key regression tests to the same key.

## Added / changed tests

New test file:

- `tests/test_iteration173_env_and_ui_qty_consistency.py`
  - `test_operator_ui_derives_worst_case_position_qty_from_worst_grid_price`
  - `test_static_asset_cache_key_bumped_after_worst_case_qty_ui_patch`

Existing tests that now pass because of the patch:

- `tests/test_iteration162_docs_and_infographic_sync.py::test_env_example_matches_current_shipped_3x_5x_operator_profile`
- UI cache-key tests updated from `manual-ui-v37` to `manual-ui-v38`.

Red→green evidence:

```text
Original tree + new iteration173 test: 2 failed in 0.27s
Patched tree + new iteration173 test: 2 passed in 0.14s
```

Targeted regression check after fixes:

```text
pytest -q tests/test_iteration162_docs_and_infographic_sync.py \
          tests/test_iteration172_ui_worst_case_margin_display.py \
          tests/test_iteration173_env_and_ui_qty_consistency.py \
          tests/test_iteration170_directional_qty_worst_case.py \
          tests/test_iteration169_grid_worst_case_notional.py

16 passed in 2.00s
```

## Final verification

Commands run after fixes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q --tb=short
```

Post-fix results:

```text
python compileall: passed
node --check app/ui/static/app.js: passed
pytest: 684 passed in 24.97s
```

`npm`/`yarn` tests were not run because there is no `package.json` in the project root.

## Static scan

A bounded static scan was saved to:

- `docs/STATIC_SCAN_2026-06-15_DEEP_BYBIT_REAUDIT_PATCH.txt`

Important changed/new hits were reviewed as safe:

- `.env.example`: 3-3-5x profile alignment.
- `app/ui/static/app.js`: new source-aware notional selection and worst-case grid-price qty derivation.
- `app/ui/static/index.html`: cache key bump to `manual-ui-v38`.

## Checks not performed

- No live Bybit private/testnet order lifecycle checks were performed. The repository boundary and `KNOWN_RISKS.md` define those as external execution/reconciliation responsibilities.
- No live account balance, actual order placement, partial fill, open-order reconciliation or liquidation-price verification was performed.
- No npm/yarn lint/type/test command was available from the project root.

## Residual risks relative to `docs/KNOWN_RISKS.md`

No residual-risk item was removed. The following remain unchanged:

- No real OMS/EMS in this repository.
- No exchange-side truth for fills, partial fills, cancellations or open orders.
- Exact liquidation and margin behavior still depends on live Bybit account state, risk tiers, mark price, fee tier and wallet margin.
- Outcome labeling remains proxy-based.
- Public Bybit REST remains insufficient as execution truth.
- External executor must bind future live order side/reduce-only/protective logic to `bybit_linear_order_semantics()` and `bybit_linear_protective_order_semantics()`.

This patch closes one configuration drift and one operator-UI quantity display inconsistency without weakening fail-closed execution guards.

## Changed files

- `.env.example`
- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `tests/test_iteration122_ui_detail_badge_fit.py`
- `tests/test_iteration129_ui_single_product_simplification.py`
- `tests/test_iteration132_operator_details_compaction.py`
- `tests/test_iteration133_operator_details_minimal_llm.py`
- `tests/test_iteration134_operator_position_size_details.py`
- `tests/test_iteration135_operator_bot_lifetime_details.py`
- `tests/test_iteration139_ui_no_trade_not_hard_blocker.py`
- `tests/test_iteration146_bybit_chart_url_and_ui_hardening.py`
- `tests/test_iteration147_short_tp_sl_ui_hardening.py`
- `tests/test_iteration149_operator_decision_panel.py`
- `tests/test_iteration151_operator_distance_and_ui_failclosed.py`
- `tests/test_iteration157_ui_invalid_exit_failclosed.py`
- `tests/test_iteration166_operator_blocked_notrade_clarity.py`
- `tests/test_iteration172_ui_worst_case_margin_display.py`
- `tests/test_iteration173_env_and_ui_qty_consistency.py`
- `tests/test_iteration174_operator_next_actions.py`
- `tests/test_iteration175_operator_diagnostics_visibility.py`
- `docs/STATIC_SCAN_2026-06-15_DEEP_BYBIT_REAUDIT_PATCH.txt`
- `docs/AUDIT_REPORT_2026-06-15_DEEP_BYBIT_REAUDIT_PATCH.md`
