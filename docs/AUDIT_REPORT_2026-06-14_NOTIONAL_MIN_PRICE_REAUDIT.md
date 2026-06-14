# Audit report: Bybit Linear USDT notional/min-price re-audit

Date: 2026-06-14  
Scope: Bybit V5 Linear USDT futures/grid recommendation system; canonical trading semantics, execution preflight, Bybit lot/minNotional validation, UI-facing risk fields, calibration/outcome boundaries, and regression tests.

## Executive summary

The repository is explicitly a recommendation/operator layer with fail-closed preflight, not a live OMS/EMS. That boundary remains unchanged: live order lifecycle, partial fills, exchange reconciliation, API-key health, wallet balance and actual Bybit order state are requirements for an external execution/reconciliation layer rather than code paths implemented in this repository.

Baseline on the received archive was green before any code change:

```text
python -m compileall -q app tests main.py: passed
node --check app/ui/static/app.js: passed
pytest -q: 676 passed in 19.14s
```

A bounded deep re-audit did not find a new short TP/SL inversion. The canonical directional model in `app/trading_semantics.py` remains the single source of truth for long/short exit geometry, gross directional PnL, risk/reward, Bybit `Buy`/`Sell`, `reduceOnly`, `closeOnTrigger`, `positionIdx=0`, and protective triggerDirection semantics.

One new high-severity fail-closed gap was found in Bybit minNotional validation for notional-only sizing payloads. A quote-notional value that is barely above Bybit `minNotional` at `reference_price` can still produce lower-grid orders below Bybit's floor after conversion to fixed base qty. The patch now conservatively converts notional-only sizing to its lowest executable grid notional using `order_notional * grid_min_price / reference_price` and blocks if that falls below Bybit `minNotional`.

After the fix and regression tests:

```text
python -m compileall -q app tests main.py: passed
node --check app/ui/static/app.js: passed
pytest -q: 678 passed in 18.15s
npm/yarn tests: not applicable; no package.json in project root
```

## Required document and model review

Reviewed before modification:

- `docs/KNOWN_RISKS.md`: confirms the project is a recommender + fail-closed preflight, not a live OMS/EMS; exact wallet/margin/liquidation/execution truth remains external.
- `docs/TRADING_LOGIC.md`: Bybit Linear USDT product boundary, futures grid assumptions and operator flow.
- `docs/ARCHITECTURE.md` and `docs/MODULES.md`: component map, persistence and runtime roles.
- `app/trading_semantics.py`: canonical long/short/neutral exit and Bybit protective-order semantics.
- Recent audit reports, especially 2026-06-14 reports for independent full re-audit, invalid price fail-closed, protective reference/qty, worst-case notional, nested grid qty/PnL, UI worst-case margin and operator diagnostics.

## Trading-semantics map reviewed

Canonical and UI/execution touchpoints checked:

- Backend canonical model: `app/trading_semantics.py`
  - `directional_exit_levels()` for long/short/neutral TP/SL/kill-switch mapping.
  - `validate_directional_exit_geometry()` for fail-closed entry/TP/SL geometry.
  - `directional_trade_math()` for gross PnL, reward/risk %, and risk:reward.
  - `bybit_linear_order_semantics()` and protective-order helpers for `Buy`/`Sell`, `reduceOnly`, `closeOnTrigger`, `positionIdx`, and trigger direction.
- Backend UI payloads: `app/main.py`
  - `_directional_exit_payload_for_reco()` uses canonical helpers for backend-to-frontend exit payloads.
  - `_validate_trade_plan_against_bybit_meta()` validates product scope, tick/qty/minNotional/leverage/range/kill-switch/grid geometry/economics before execution.
  - `_snap_reco_payload_to_bybit_meta()` preserves conservative tick/qty/notional semantics.
  - `_execution_preflight()` combines freshness, market data, live price, funding, shock guard and Bybit validation blocks.
- Recommendation economics: `app/recommender.py`
  - Uses existing worst-case grid notional/margin fields and fixed-leverage/no-trade policy.
- Outcome/calibration boundary: `app/outcomes.py`, `app/calibration.py`
  - Outcome labels remain proxy-based; calibration fallback remains advisory as documented in `KNOWN_RISKS.md`.
- Frontend: `app/ui/static/app.js`
  - Backend-provided `directional_exit_levels` are preferred over fallback UI mapping.
  - UI revalidates geometry before rendering directional TP/SL and falls back to kill-switch display when backend exit geometry is invalid.
  - Worst-case notional/margin fields are preferred over legacy reference-price fields in operator-facing cards.
- Tests: directional, UI parity, Bybit semantics, runtime caps and operator payload tests across `tests/test_iteration147_*` through `tests/test_iteration176_*`.

## Static scan

Saved to `docs/STATIC_SCAN_2026-06-14_NOTIONAL_MIN_PRICE_REAUDIT.txt`.

The scan covered `app/` and `tests/` terms: `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`, `notional`, `margin`, `min_notional`, `order_notional`, `qty_step`, `funding`, `directional_exit_levels`, `trading_semantics`.

No raw scan dump is repeated here. The only changed semantic files in this patch are:

- `app/main.py`
- `tests/test_iteration176_notional_min_price.py`

## Findings and fixes

### HIGH: notional-only sizing could pass minNotional at reference price but fail at lower grid prices

- Severity: high
- Files and ranges:
  - `app/main.py`, lines 3190-3216
  - `tests/test_iteration176_notional_min_price.py`, lines 85-113
- Problem:
  - `_validate_trade_plan_against_bybit_meta()` already checked explicit `order_qty` at `_grid_min_notional_price(reference_price, lower, upper)`.
  - But if a payload contained only `order_notional_usdt` / quote-notional sizing and no base `order_qty`, validation only compared `order_notional` directly against Bybit `min_notional`.
  - For a fixed-base grid, a notional value estimated at `reference_price` implies a base qty. Lower grid orders then have notional `order_notional * grid_min_price / reference_price`.
  - Example: `order_notional_usdt=5.1`, `reference_price=100`, `grid_min_price=80`, Bybit `minNotional=5`. The direct reference check passes (`5.1 > 5`), but the lower-grid order is only `4.08 USDT` and can be rejected by Bybit.
- Financial/trading risk:
  - Operator preflight could show a notional-only manual/legacy recommendation as executable while lower grid levels would be rejected by the exchange.
  - Partial grid creation or missing lower orders changes exposure, TP cadence, margin envelope and risk assumptions.
  - Because this is an execution-readiness check, the safe behavior is fail-closed.
- Fix:
  - In `app/main.py`, when `order_notional` is present without qty-derived protection, the validator now computes a conservative lower-grid notional:

```text
grid_min_notional = order_notional * grid_min_price / reference_price
```

  - If this inferred lower-grid notional is below Bybit `minNotional`, execution preflight emits `ORDER_NOTIONAL_BELOW_MIN` with `reference_price` and `grid_min_price` in the message.
  - If reference/range price is missing for a notional-only payload, preflight warns that conservative lower-grid minNotional validation cannot be completed.
  - The change is fail-closed only; no severity guard was weakened and no warning was converted into an approval path.
- Red -> green proof:
  - Added `tests/test_iteration176_notional_min_price.py::test_notional_only_payload_checks_min_notional_at_lowest_grid_price`.
  - Red on received code: `1 failed, 1 passed` in the new test file; the failing assertion was that the notional-only payload should block but did not.
  - Green after patch: new test file and related minNotional tests passed.

### LOW / residual: quote-notional-only payloads remain less precise than explicit qty payloads

- Severity: low residual after the high-severity guard fix
- Files:
  - `app/main.py`, lines 3190-3216
- Rationale:
  - Without exchange-side order preview, notional-only sizing still cannot prove every eventual Bybit-created order exactly matches the intended base qty and price.
  - The repository is not an OMS/EMS, so exact order-level truth remains external.
- Mitigation:
  - Preflight now uses the conservative lower-grid conversion where possible.
  - External executor must still re-check actual qty, price, minQty, qtyStep, minNotional, margin and wallet balance immediately before creating any real Bybit grid bot.

## Added tests

`tests/test_iteration176_notional_min_price.py`

- `test_notional_only_payload_checks_min_notional_at_lowest_grid_price`
  - Red condition: notional-only payload of `5.1 USDT` at reference `100` and lower grid price `80` incorrectly passed Bybit `minNotional=5` validation.
  - Green condition: validator blocks with `ORDER_NOTIONAL_BELOW_MIN` and message includes `reference_price` and `grid_min_price`.
- `test_notional_only_payload_accepts_grid_min_adjusted_notional_above_bybit_floor`
  - Guard against overblocking: notional-only payload of `6.25 USDT` at reference `100` and lower grid price `80` yields exactly `5.0 USDT` at the lower grid level and remains accepted.

Related regression subset after patch:

```text
pytest -q tests/test_iteration176_notional_min_price.py \
          tests/test_iteration115_order_sizing_validation.py \
          tests/test_iteration121_operator_guard_fail_closed.py \
          tests/test_iteration165_operator_payload_consistency.py
11 passed in 1.26s
```

## Verification results

### Baseline before modifications

```text
python -m compileall -q app tests main.py
COMPILE_EXIT:0

node --check app/ui/static/app.js
NODE_EXIT:0

pytest -q
676 passed in 19.14s
PYTEST_EXIT:0
```

### Red test before fix

```text
pytest -q tests/test_iteration176_notional_min_price.py
1 failed, 1 passed in 1.07s
```

Failing test:

```text
test_notional_only_payload_checks_min_notional_at_lowest_grid_price
assert validation["ok"] is False
```

### Post-fix full verification

```text
python -m compileall -q app tests main.py
COMPILE_EXIT:0

node --check app/ui/static/app.js
NODE_EXIT:0

pytest -q
678 passed in 18.15s
PYTEST_EXIT:0
```

## Checks not performed

- Live Bybit private API / testnet order placement was not performed; the repository does not contain a live OMS/EMS and the local environment has no exchange credentials.
- Real wallet balance, fee tier, risk-tier liquidation, open orders, partial fills and reconciliation could not be verified offline and remain external executor obligations.
- npm/yarn tests were not run because there is no project-root `package.json`.

## Residual risks relative to `KNOWN_RISKS.md`

Unchanged:

- No real OMS/EMS or exchange reconciliation in this repository.
- Outcome labels are proxy labels and not real fill/funding/liquidation truth.
- Exact liquidation, available balance and live margin truth must come from external execution/reconciliation.
- Telegram/alerting remains best-effort.
- Public Bybit REST remains a metadata/market-data source, not execution truth.

Narrowed by this patch:

- Notional-only manual/legacy payloads are less likely to pass execution preflight while lower grid orders would violate Bybit `minNotional`.

## Changed files

- `app/main.py`
- `tests/test_iteration176_notional_min_price.py`
- `docs/AUDIT_REPORT_2026-06-14_NOTIONAL_MIN_PRICE_REAUDIT.md`
- `docs/STATIC_SCAN_2026-06-14_NOTIONAL_MIN_PRICE_REAUDIT.txt`
