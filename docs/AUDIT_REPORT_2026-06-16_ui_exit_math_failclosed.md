# AUDIT_REPORT_2026-06-16_ui_exit_math_failclosed

## Scope
Deep regression re-audit of the Bybit Linear USDT futures-grid recommendation/audit service with emphasis on canonical directional semantics, frontend/backend TP/SL parity, fail-closed operator UI, and Bybit V5 one-way close/protective-order conventions.

The repository boundary remains the one stated in `docs/KNOWN_RISKS.md`: this project is a recommendation + audit + fail-closed preflight service, not a live OMS/EMS. Findings about partial fills, order routing, websocket reconciliation, retry/idempotent exchange order state and exact live liquidation state remain requirements for an external execution layer unless such code is added to the repository.

## Mandatory pre-read summary
Read before patching:

- `docs/KNOWN_RISKS.md`: no real OMS/EMS; proxy outcomes; external execution/reconciliation remains required; live-price guard and Bybit metadata checks are fail-closed but do not replace account/exchange truth.
- `docs/TRADING_LOGIC.md`: service publishes futures-grid recommendations only after scoring/risk/shock/LLM gates; operator confirmation is rechecked by execution preflight.
- `docs/ARCHITECTURE.md`: data, inference, control, persistence/audit, and operator/API layers are deliberately separated; external executor is out of scope.
- `docs/MODULES.md`: key contracts for Bybit client, recommender, risk, shock guard, outcomes and DB.
- `app/trading_semantics.py`: canonical source for `long|short|neutral`, directional exit levels, TP/SL geometry, gross PnL/R:R math, one-way Bybit side mapping, and protective `triggerDirection` semantics.
- Latest audit reports reviewed: `AUDIT_REPORT_2026-06-15_full_system_regression.md`, `AUDIT_REPORT_2026-06-15_ui_numeric_failclosed_reaudit.md`, `AUDIT_REPORT_2026-06-15_execution_liq_boundary_reaudit.md`, plus adjacent 2026-06-15 reports present in `docs/`.

## Baseline before changes

Commands run before any patch:

```text
python -m compileall -q app tests main.py  -> exit 0
node --check app/ui/static/app.js          -> exit 0
pytest -q                                  -> 710 passed in 20.16s, exit 0
```

## Conventions fixed for this audit

- PnL/R:R in `app.trading_semantics.directional_trade_math()` is gross price-distance math, not net of fees/funding.
- UI TP/SL distance and R:R are display-only values derived from backend `directional_exit_levels.trade_math`; they must not be shown if the backend directional payload is missing, direction-mismatched, or geometry-invalid.
- Long: TP above reference/entry; SL below. Short: TP below reference/entry; SL above. Neutral/grid: no single directional TP/SL.
- Bybit V5 one-way mapping in the repository remains: open long=`Buy`, close long=`Sell`; open short=`Sell`, close short=`Buy`; `positionIdx=0`; protective exits must reduce/close exposure and must not increase position.
- Bybit V5 `triggerDirection`: `1` = price rises to trigger, `2` = price falls to trigger.

## Semantic map / single-source-of-truth review

### Canonical backend

- `app/trading_semantics.py`
  - `normalize_execution_direction()`
  - `directional_exit_levels()`
  - `validate_directional_exit_geometry()`
  - `directional_trade_math()`
  - `bybit_linear_order_semantics()`
  - `bybit_linear_protective_order_semantics()`
  - `validate_protective_trigger_geometry()`
  - `bybit_linear_protective_order_plan()`

### Backend/API materialization and validation

- `app/main.py`
  - `_directional_exit_payload_for_reco()` augments outgoing recommendation JSON with canonical exit payload, geometry flags, trade math, and protective Bybit order intent.
  - `_directional_exit_qty_for_reco()` chooses the qty context for TP/SL math using explicit total qty or conservative notional-derived qty.
  - execution preflight checks Bybit metadata, range/kill-switch/step alignment, direction validity, supported bot type, isolated mode, exact USDT linear scope, leverage and min-notional constraints.
  - same-symbol one-way direction conflict guard prevents simultaneous incompatible local bot directions.

### Frontend/UI

- `app/ui/static/app.js`
  - `operatorExitLevels()` is fallback-only.
  - `operatorExitLevelsFromBackend()` displays backend canonical TP/SL only when backend payload direction and geometry are valid.
  - `buildOperatorValues()` now blocks local fallback TP/SL for linear directional recommendations without backend payload.
  - `buildOperatorFieldSpecs()` previously read `directional_exit_levels.trade_math` directly; patched to route through `directionalExitMathForDisplay()`.
  - direction badges, details view, operator panel, risk/economics fields and manual launch links were reviewed for long/short/neutral text drift.

### Quant/outcome/calibration

- `app/outcomes.py` uses next-candle entry semantics and proxy outcome labeling; no new leak in the patched area.
- `app/calibration.py` remains proxy/advisory with sample-size and class-balance gates; the score-only fallback remains an already documented residual risk, not a new regression.

### Static scan summary

Focused scan terms over `app/**/*.py` and `app/ui/static/*.js`:

```text
tp: 401 hits / 15 files
sl: 237 hits / 15 files
stop: 144 hits / 4 files
take: 110 hits / 8 files
upper: 307 hits / 14 files
lower: 415 hits / 19 files
short: 195 hits / 13 files
long: 167 hits / 11 files
side: 133 hits / 11 files
Buy: 3 hits / 2 files
Sell: 3 hits / 2 files
reduceOnly: 5 hits / 1 file
kill: 167 hits / 6 files
leverage: 398 hits / 6 files
pnl: 108 hits / 6 files
roi: 0 hits / 0 files
risk: 390 hits / 14 files
```

No additional HIGH/CRITICAL directional math duplication was found outside the documented frontend display issue below. Remaining direct UI functions are either fallback formatting or fail-closed guards around backend payload.

## Findings and fixes

### MEDIUM — UI could show stale TP/SL distance and Risk/Reward when backend exit payload was rejected

- **Files**:
  - `app/ui/static/app.js:612-625`
  - `app/ui/static/app.js:1012-1019`
  - `tests/test_iteration187_ui_exit_math_failclosed.py:61-160`
  - `tests/test_iteration158_deep_bybit_directional_audit.py:132-140`
- **Problem**: previous UI logic correctly blocked TP/SL values when `directional_exit_levels` direction mismatched the item direction or when geometry was invalid, but `buildOperatorFieldSpecs()` still read `directional_exit_levels.trade_math` directly. A malformed/stale payload could therefore show `TP 5% / SL 5%` and `R:R=1` while the visible TP/SL fields were already blocked as unsafe.
- **Trading/financial risk**: operator could see a coherent-looking distance/R:R summary for a TP/SL payload the UI/backend had already determined unsafe. This is not an execution-path order bug, but it weakens the operator fail-closed display model and can mislead manual decision-making.
- **Fix**: added `directionalExitMathForDisplay(it)` in `app/ui/static/app.js`. It returns math only if:
  - backend exit payload exists and is an object;
  - item direction and payload direction match for `long|short`;
  - payload is directional, has `has_directional_take_profit=true`;
  - `geometry_valid !== false`;
  - the same JS geometry validator confirms TP/SL are on the correct side of the reference price.
  `buildOperatorFieldSpecs()` now uses this helper instead of direct `trade_math` access.
- **Safety direction**: stricter display fail-closed; no execution guard was weakened.

## Red → green evidence

New test file added:

- `tests/test_iteration187_ui_exit_math_failclosed.py`
  - `test_operator_risk_math_is_hidden_when_backend_exit_payload_direction_mismatches`
  - `test_operator_risk_math_is_hidden_when_backend_exit_geometry_is_invalid`

Red run before fix:

```text
pytest -q tests/test_iteration187_ui_exit_math_failclosed.py
2 failed
Expected: {"distance": "—", "rr": "—"}
Actual before patch: {"distance": "TP 5% / SL 5%", "rr": "1"}
```

Green run after fix:

```text
pytest -q tests/test_iteration158_deep_bybit_directional_audit.py tests/test_iteration187_ui_exit_math_failclosed.py
10 passed in 1.07s
```

## Post-change full verification

```text
python -m compileall -q app tests main.py  -> exit 0
node --check app/ui/static/app.js          -> exit 0
pytest -q                                  -> 712 passed in 19.52s, exit 0
```

Counts:

```text
baseline: 710 passed, 0 failed, 0 skipped
post:     712 passed, 0 failed, 0 skipped
```

## Checks completed

- Canonical directional model reviewed against UI/backend usage.
- Frontend/backend TP/SL display parity reviewed for linear long/short/neutral.
- Bybit one-way side/protective trigger semantics reviewed against canonical module.
- Static scan over directional/risk terms completed.
- Full Python compile, JS syntax check and full pytest completed.

## Checks not performed / limitations

- No live Bybit private/testnet execution was run; repository has no real OMS/EMS and the current environment does not include live API credentials.
- No websocket order/fill reconciliation audit was possible because that layer is outside current repository scope.
- No npm/yarn lint/test was run because the repository does not ship a package manifest for such tests.
- Exact account liquidation state, available balance, live order lifecycle, partial fills and exchange-side idempotency remain external execution-layer responsibilities.

## Residual risks vs `KNOWN_RISKS.md`

Unchanged:

- no real OMS/EMS;
- proxy outcome labeling;
- external execution/reconciliation required for exchange truth;
- exact liquidation modeling and account balance checks remain external;
- LLM reviewer remains secondary/advisory/gated according to configuration.

Reduced by this patch:

- operator UI now no longer displays directional TP/SL distance and Risk/Reward when the same backend exit payload is direction-mismatched or geometry-invalid.

