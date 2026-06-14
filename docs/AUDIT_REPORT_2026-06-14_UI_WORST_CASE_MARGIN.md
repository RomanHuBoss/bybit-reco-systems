# Audit report: UI worst-case grid margin/notional display hardening

Date: 2026-06-14  
Scope: Bybit futures / linear USDT recommender, operator UI, directional/grid risk display, static asset coherency, regression tests.

## Baseline before changes

The received archive was checked before modification:

- `python -m compileall -q app tests main.py`: passed
- `node --check app/ui/static/app.js`: passed
- `pytest -q`: `662 passed in 19.84s`

The review started from the current project boundary documented in `docs/KNOWN_RISKS.md`: this repository is a recommendation + fail-closed preflight/operator layer, not a real OMS/EMS. Therefore live order lifecycle, fills, exchange reconciliation and idempotent real order submission remain requirements for an external execution layer rather than bugs in nonexistent order-management code.

Reviewed source-of-truth and recent audit context:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- Recent reports including the 2026-06-14 directional/risk/worst-case-grid reports.

## Trading semantics map reviewed

Areas checked for `side`, TP/SL, PnL, ROI/risk, leverage, notional, margin, grid and operator display semantics:

- Backend canonical semantics: `app/trading_semantics.py`
- Grid economics helpers: `app/grid_math.py`
- Runtime/preflight/operator payload normalization: `app/main.py`
- Recommendation/grid construction and sizing payloads: `app/recommender.py`
- Operator UI rendering: `app/ui/static/app.js`, `app/ui/static/index.html`
- Existing regression suite under `tests/`, especially iteration 147-171 trading-semantics and UI/risk tests.

No new long/short TP/SL inversion was found. The existing canonical model still holds:

- long: TP above entry/range, SL below entry/range;
- short: TP below entry/range, SL above entry/range;
- neutral grid: no single directional TP; lower/upper are kill-switch exits;
- Bybit one-way linear protective exits remain reduce-only / close-on-trigger through `app.trading_semantics`.

## Finding and fix

### MEDIUM — operator UI displayed legacy reference-price margin/notional before worst-case grid margin/notional

- **Files**:
  - `app/ui/static/app.js`, `buildOperatorFieldSpecs(...)`
  - `app/ui/static/index.html`
  - `tests/test_iteration172_ui_worst_case_margin_display.py`
- **Severity**: medium
- **Problem**: the backend/runtime path had already been hardened to compute fixed-qty grid exposure using the highest executable grid price (`estimated_worst_case_total_order_notional_usdt` and `estimated_worst_case_margin_required_usdt`). However the operator UI still selected `estimated_margin_required_usdt` and legacy total-notional fields first. A generated payload could therefore pass conservative runtime checks using 300 USDT worst-case margin while the visible operator panel still showed a less conservative 200 USDT reference-price margin.
- **Trading/financial risk**: the operator-facing panel understated capital requirement and exposure for a fixed-qty grid whose upper range price exceeded the reference price. This does not weaken backend execution preflight, but it creates a dangerous UI/API mismatch and can lead the operator to allocate less margin than the runtime risk model requires.
- **Fix**:
  - `buildOperatorFieldSpecs(...)` now prioritizes:
    - `estimated_worst_case_margin_required_usdt`
    - `worst_case_margin_required_usdt`
    - then legacy margin aliases.
  - Position-size display now prioritizes:
    - `estimated_worst_case_total_order_notional_usdt`
    - `worst_case_total_order_notional_usdt`
    - then legacy/max/reference notional aliases.
  - Operator help text now explicitly says the displayed exposure/margin is the conservative grid-envelope value when worst-case fields are present.
  - Static asset key bumped from `manual-ui-v32` to `manual-ui-v33` so browsers do not keep stale JS.
- **Safety direction**: the change only makes the UI more conservative and closer to backend/runtime risk checks. No fail-closed guard was weakened.

## Tests added / updated

Added `tests/test_iteration172_ui_worst_case_margin_display.py`:

- `test_operator_ui_prefers_worst_case_margin_before_reference_price_margin`
- `test_operator_ui_prefers_worst_case_total_notional_before_reference_notional`
- `test_static_asset_cache_key_bumped_after_worst_case_margin_ui_patch`

Red→green proof:

- The new regression tests were copied into an unmodified extraction of the received archive.
- Result before fix: `2 failed` for missing worst-case margin/notional precedence in `app/ui/static/app.js`.
- Result after fix: targeted suite passed, and full suite passed.

Updated existing static-asset cache-key assertions from `manual-ui-v32` to `manual-ui-v33` so the test suite remains aligned with the changed JS asset.

## Checks after changes

- `python -m compileall -q app tests main.py`: passed
- `node --check app/ui/static/app.js`: passed
- Targeted regression run:
  - `pytest -q tests/test_iteration172_ui_worst_case_margin_display.py tests/test_iteration169_grid_worst_case_notional.py`: `6 passed`
  - UI/cache-key focused run: `23 passed`
- Full suite:
  - `pytest -q`: `665 passed in 18.08s`

## Static scan

Static scan saved as:

- `docs/STATIC_SCAN_2026-06-14_UI_WORST_CASE_MARGIN.txt`

Changed/static hits reviewed:

- `app/ui/static/app.js`: worst-case margin/notional now precedes legacy margin/notional in operator display. Safe.
- `app/ui/static/index.html`: cache key bumped to `manual-ui-v33`. Safe.
- `tests/test_iteration172_ui_worst_case_margin_display.py`: regression coverage for red→green behavior and cache coherency. Safe.

## Checks not executed

- npm/yarn tests: not applicable; no `package.json` exists in project root.
- Live/testnet Bybit private API checks: not executed in offline container; this repository does not include real account credentials and is documented as a recommender/preflight layer rather than a live OMS/EMS.
- Real fills, partial fills, order cancellation, exchange reconciliation, rate-limit and insufficient-balance behavior: remain external executor requirements unless a live execution layer is added.

## Residual risks versus `docs/KNOWN_RISKS.md`

No residual risk category was removed. The following known risks remain materially unchanged:

- no real OMS/EMS or exchange reconciliation in this repository;
- proxy outcome labels remain advisory;
- exact live liquidation/margin/funding truth must still be confirmed by an external execution/reconciliation layer;
- operator infographic/docs are quick references, not executable contracts.

This patch narrows one UI-risk gap: visible operator margin/exposure now follows the conservative worst-case fixed-qty grid envelope already used by backend/runtime risk logic.
