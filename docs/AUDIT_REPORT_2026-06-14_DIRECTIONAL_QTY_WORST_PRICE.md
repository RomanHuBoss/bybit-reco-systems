# Audit report — directional TP/SL quantity derivation after worst-case grid notional patch, 2026-06-14

## Scope

This pass was performed as a follow-up regression audit on the mature Bybit Linear USDT futures/grid recommendation repository. The audit was explicitly anchored to:

- `docs/KNOWN_RISKS.md`: the project is a recommendation + fail-closed preflight/audit service, not a live OMS/EMS;
- `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`;
- `app/trading_semantics.py`: canonical long/short TP/SL, PnL and Bybit one-way side/protective-order semantics;
- latest 2026-06-14 audit reports, especially the invalid-price, protective-reference/qty, operator-payload and worst-case-notional re-audits.

The review focused on regressions introduced or made possible by the most recent hardening around worst-case grid notional/margin fields, backend/UI directional exit payloads and operator-facing TP/SL math.

## Baseline before changes

Commands run before code changes:

```text
python -m compileall -q app tests main.py
PASS

node --check app/ui/static/app.js
PASS

pytest -q
TIMED OUT in this container after reaching 76% progress. This appears to be a process-liveness/background-thread completion issue in the audit environment rather than an assertion failure; no failing test output was produced before timeout.
```

To avoid hiding assertion regressions behind the monolithic process timeout, the suite was also run in isolated chunks using a fresh Python process with `pytest.main(...); os._exit(code)` for each chunk. All existing chunks run before the patch passed. The received archive baseline was therefore treated as functionally green for assertions, with the caveat that monolithic `pytest -q` does not terminate within the local tool timeout.

## Source map / single source of truth notes

Directional semantics were found in the expected canonical path:

- `app/trading_semantics.py`: `directional_exit_levels`, `validate_directional_exit_geometry`, `directional_trade_math`, `bybit_linear_order_semantics`, `bybit_linear_protective_order_plan`.
- `app/main.py`: API/UI augmentation through `_directional_exit_payload_for_reco`, execution preflight validation and Bybit metadata/risk checks.
- `app/ui/static/app.js`: UI renders backend-provided `directional_exit_levels` and `trade_math`; no independent short TP/SL truth was introduced in this pass.
- `app/recommender.py`, `app/grid_math.py`: grid range, order qty/notional, worst-case notional/margin and liquidation-buffer approximations.

No new live OMS/EMS code was invented or audited as if it existed. Order lifecycle, partial fills, real open-order reconciliation and authenticated exchange state remain external executor requirements under `KNOWN_RISKS.md`.

## Finding and fix

### HIGH — directional TP/SL gross PnL quantity could be overstated by upper-bound worst-case notional

- **Files:**
  - `app/main.py:764-866`
  - `tests/test_iteration170_directional_qty_worst_case.py:23-85`
- **Area:** `_directional_exit_qty_for_reco(...)`, operator/API `directional_exit_levels.trade_math`.
- **Problem:** after the previous worst-case notional hardening, `_directional_exit_qty_for_reco(...)` preferred `estimated_worst_case_total_order_notional_usdt` and divided it by `reference_price`. For a fixed-qty grid, worst-case notional is intentionally priced at the highest executable grid price, not at entry/reference. Dividing it by reference price overstates the base quantity whenever `range.upper > reference_price`.
- **Concrete example:** `qty_per_order=1`, `grid_count=10`, `reference=100`, `range.upper=150`, `estimated_worst_case_total_order_notional_usdt=1500`. The correct base quantity is `10`, but the old derivation returned `1500 / 100 = 15`. Short TP/SL gross PnL in the operator payload was therefore overstated by 50% in that case.
- **Trading/financial risk:** the repository does not submit live orders, but this is still operator-facing risk. `trade_math.gross_profit_usdt` and `gross_loss_usdt` could appear larger than the actual grid base quantity supports, weakening the audit trail and potentially misleading manual execution sizing review. Risk/reward ratio and directional TP/SL geometry were not inverted, but gross USDT PnL was wrong.
- **Fix:** quantity derivation now prefers explicit base quantity (`estimated_position_qty`, then `qty_per_order * grid_count`) before any notional inference. If only a worst-case total notional is available, it is divided by the same price convention that produced it: `max(reference_price, range.lower, range.upper)`. Legacy/reference notional fields continue to divide by `reference_price`.
- **Safety property:** this change does not weaken any fail-closed guard, preflight gate, Bybit metadata validation or runtime cap. It only makes operator-facing directional PnL quantity consistent with fixed-qty grid economics.

## Tests added / red→green proof

Added `tests/test_iteration170_directional_qty_worst_case.py`:

1. `test_directional_exit_qty_prefers_explicit_grid_qty_over_worst_case_notional`
   - **Red before fix:** returned `qty=15.0` from `1500 / reference_price`.
   - **Green after fix:** returns `qty=10.0` from `qty_per_order * grid_count`.
2. `test_directional_exit_qty_derives_worst_case_notional_with_worst_grid_price_when_qty_missing`
   - **Red before fix:** returned `qty=15.0` from worst-case notional divided by entry.
   - **Green after fix:** returns `qty=10.0` from `1500 / max_grid_price`.

Red run before fix:

```text
2 failed in 2.34s
RED_TEST_EXIT_CODE=1
```

Green targeted run after fix:

```text
2 passed in 2.25s
GREEN_NEW_TEST_EXIT_CODE=0
```

Affected regression run:

```text
tests/test_iteration158_deep_bybit_directional_audit.py
tests/test_iteration161_protective_reference_and_qty.py
tests/test_iteration167_full_trading_system_audit.py
tests/test_iteration169_grid_worst_case_notional.py
tests/test_iteration170_directional_qty_worst_case.py

79 passed in 3.00s
AFFECTED_EXIT_CODE=0
```

## Static / code quality checks

Post-fix checks:

```text
python -m compileall -q app tests main.py
PASS

node --check app/ui/static/app.js
PASS
```

Static keyword scan was refreshed in:

```text
docs/STATIC_SCAN_2026-06-14_DIRECTIONAL_QTY_WORST_PRICE.txt
```

Summary counts over `app/`, `app/ui/static/*.js` and `tests/` source files:

```text
tp: 399; sl: 213; stop: 268; take: 195; upper: 465; lower: 537;
short: 456; long: 540; side: 173; Buy: 16; Sell: 13; reduceOnly: 21;
kill: 223; leverage: 531; pnl: 176; roi: 0; risk: 680;
notional: 491; margin: 373; qty: 458
```

No `package.json` exists in the project root, so npm/yarn tests and JS lint/typecheck were not applicable.

## Full post-fix test result

The monolithic `pytest -q` command in this container timed out before completion, as in the baseline, without a visible assertion failure. To prove the full assertion suite, all test files were run in isolated chunks with fresh Python processes and forced process exit after pytest returned.

Post-fix split-suite result:

```text
660 passed, 0 failed, 0 skipped across 111 test files
```

Chunk details:

```text
122 passed
58 passed
43 passed
87 passed
120 passed
62 passed
57 passed
111 passed
```

## Residual risks relative to `KNOWN_RISKS.md`

Unchanged residual risks:

- No real OMS/EMS exists in this repository. Partial fills, exchange-side order idempotency, authenticated position reconciliation, real open orders, insufficient balance and rate-limit handling remain external executor requirements.
- Exact liquidation/margin remains approximate and dependent on live Bybit account state, mark price, risk tier and wallet margin.
- Outcome labeling remains proxy-based and must not be treated as real fill/funding/liquidation truth.
- Browser cache/deployment invalidation was not validated in this offline environment.

Risk closed in this pass:

- Operator/API directional TP/SL gross PnL no longer overstates base quantity by dividing upper-bound worst-case notional by entry/reference price.

## Changed files

- `app/main.py`
- `tests/test_iteration170_directional_qty_worst_case.py`
- `docs/AUDIT_REPORT_2026-06-14_DIRECTIONAL_QTY_WORST_PRICE.md`
- `docs/STATIC_SCAN_2026-06-14_DIRECTIONAL_QTY_WORST_PRICE.txt`
