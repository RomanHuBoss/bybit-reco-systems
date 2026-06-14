# Deep Bybit Futures Directional Audit Report — 2026-06-14

## Scope

Аудит выполнен для проекта `bybit-reco-systems-main` из архива `bybit-reco-systems-main(7).zip` как для потенциального торгового контура Bybit futures / linear USDT. Проверялись не только lint/pytest, но и торговая семантика long/short, TP/SL, округление Bybit instrument filters, UI/API consistency, risk gates, kill-switch, grid-логика, эконометрические признаки, устойчивость временных рядов и тестовая фиксация правильной математики.

Отдельно были просмотрены зоны кода, связанные с:

- directional semantics: `app/trading_semantics.py`, `app/grid_math.py`, `app/recommender.py`, `app/main.py`;
- Bybit instrument metadata/order semantics: `app/bybit_client.py`, `app/trading_semantics.py`, `app/main.py`;
- risk gates and preflight: `app/risk.py`, `app/main.py`;
- frontend/operator UI: `app/ui/static/app.js`, `app/ui/static/index.html`;
- econometric/time-series features: `app/features.py`;
- regression coverage in `tests/`.

Official Bybit V5 documentation was used as an external correctness reference for instrument filters (`priceFilter.tickSize`, `lotSizeFilter.qtyStep`, `lotSizeFilter.minNotionalValue`, `lotSizeFilter.minOrderQty`), `positionIdx` semantics and order-side/protective-order fields.

## Executive summary

The project already contained a comparatively strong central directional model in `app/trading_semantics.py`: long TP above entry / SL below entry, short TP below entry / SL above entry, neutral grid without directional TP/SL, plus Bybit protective-order helpers. Existing tests also covered many long/short TP/SL regressions.

Two additional correctness bugs were found and fixed:

1. **HIGH** — frontend Bybit tick rounding could visually shrink directional boundaries because it rounded the raw price before applying `ceil`/`floor`.
2. **MEDIUM** — BTC beta calculation independently dropped invalid prices from symbol and BTC series, which could misalign close-only return vectors and produce spurious econometric signals.

All available automated checks pass after the patch.

## Static/project map

### Trading semantics and order semantics

| Area | Primary files/functions | Audit result |
|---|---|---|
| Long/short direction normalization | `app/trading_semantics.py::normalize_execution_direction` | Centralized and reused. |
| TP/SL directional mapping | `directional_exit_levels`, `validate_directional_exit_geometry` | Correct canonical semantics verified by existing and retained tests. |
| PnL / ROI / R-multiple | `directional_trade_math`, `app/grid_math.py::linear_pnl_usdt` | Directional sign handling is explicit. |
| Bybit side mapping | `bybit_linear_order_semantics` | Buy/Sell mapping is explicit for one-way and hedge modes. |
| Protective order semantics | `bybit_linear_protective_order_semantics`, `bybit_linear_protective_order_plan` | `reduceOnly` / `closeOnTrigger` safety semantics are centralized. |
| UI payload exit levels | `app/main.py::_directional_exit_payload_for_reco`, `app/ui/static/app.js` | Backend remains canonical source; UI renders backend fields and validates geometry. |

### Risk and preflight

| Area | Primary files/functions | Audit result |
|---|---|---|
| Config/risk limits | `app/risk.py::_normalize_risk_limits`, `compute_risk_status`, `gate_candidate` | Risk gates run before operator materialization paths. |
| Runtime Bybit meta | `app/main.py::_fetch_bybit_instrument_meta`, `_validate_trade_plan_against_bybit_meta` | Uses instrument specs for tick/qty/minNotional style checks. |
| Runtime trade plan materialization | `app/main.py::_materialize_bot_from_rec` | Uses preflight and risk blockers before bot materialization. |
| UI dangerous state prevention | `app/ui/static/app.js` | UI warnings and fail-closed display logic exist; no direct live private order sending was found in this project. |

### Econometric/time-series features

| Area | Primary files/functions | Audit result |
|---|---|---|
| Open-candle/look-ahead prevention | `app/recommender.py::_drop_open_candle` and related tests | Existing logic drops open candle before scoring paths. |
| Rolling/NaN hardening | `app/features.py`, existing tests | Mostly fail-closed; one beta-alignment defect fixed below. |
| BTC correlation/beta | `app/features.py::btc_beta` | Fixed invalid-price compression/misalignment bug. |

## Findings and fixes

### Finding 1 — HIGH — Frontend tick rounding shrank directional boundaries

**File:** `app/ui/static/app.js`  
**Function:** `quantizeByStep`  
**Original issue:** the UI converted price to tick precision with `Math.round(v * factor)` before applying `ceil` or `floor`.

For Bybit-style display rounding, this is unsafe. Examples with `tickSize=0.01`:

- `quantizeByStep(100.001, 0.01, "up")` previously became `100.00`, but the correct upper boundary display is `100.01`.
- `quantizeByStep(99.999, 0.01, "down")` previously became `100.00`, but the correct lower boundary display is `99.99`.

**Trading risk:**

- For a short position, SL is above entry/current reference. A visually rounded-down SL can understate risk and make the operator believe protection is closer/safer than the executable Bybit tick boundary.
- For a short TP/lower kill-switch, a visually rounded-up level can incorrectly move the displayed profit/trigger boundary toward the market.
- The bug was visual, but in trading systems UI/operator mismatches are high severity because they can drive manual approvals and risk interpretation.

**Fix:**

`quantizeByStep` now divides by tick first and applies `ceil`/`floor` to tick units without pre-rounding the value:

- `unitsRaw = v / tick`
- `floor(unitsRaw + eps)` for lower-bound/down rounding
- `ceil(unitsRaw - eps)` for upper-bound/up rounding
- final value formatted to tick precision

**Changed lines:** `app/ui/static/app.js:103-116`.

**Regression tests added:**

- `tests/test_iteration160_frontend_tick_directional_rounding.py::test_frontend_tick_rounding_preserves_directional_boundaries_for_bybit_levels`
- `tests/test_iteration160_frontend_tick_directional_rounding.py::test_frontend_quantize_no_longer_pre_rounds_value_before_ceil_floor`

**Cache-busting:**

The static asset version was bumped from `manual-ui-v30` to `manual-ui-v31` in `app/ui/static/index.html` and related cache-key tests were updated, so browsers do not keep the old JS.

---

### Finding 2 — MEDIUM — BTC beta could misalign return vectors after invalid-price filtering

**File:** `app/features.py`  
**Function:** `btc_beta`

**Original issue:** invalid/non-positive closes were filtered independently inside each close series before calculating log returns. Because the function receives close-only vectors, there is no timestamp left to re-align the symbol and BTC series after filtering. If invalid values occurred at different positions, the two return vectors could silently represent different time intervals.

**Econometric risk:**

- Spurious correlation / beta.
- Incorrect `is_btc_driven` or `independent_signal` flags.
- Possible signal-confidence distortion under missing/dirty candle conditions.

**Fix:**

The active beta window is now treated as atomic. If any value inside the active `window + 1` close-only slice is invalid, non-finite or non-positive, the function returns the fail-closed empty beta result instead of compressing data and calculating a potentially shifted correlation.

**Changed lines:** `app/features.py:368-382`.

**Regression test added:**

- `tests/test_iteration160_frontend_tick_directional_rounding.py::test_btc_beta_fails_closed_on_unaligned_invalid_prices_inside_active_window`

---

### Finding 3 — INFO — Core directional semantics already centralized and retained

**Files:** `app/trading_semantics.py`, `app/main.py`, `app/recommender.py`, `app/ui/static/app.js`

The audit confirmed that the project already had a single strict long/short model:

- long: TP above reference, SL below reference;
- short: TP below reference, SL above reference;
- neutral/grid-only: no directional TP/SL when not mathematically meaningful;
- protective Bybit orders are planned as reducing/closing protection, not position-increasing orders.

No architectural rewrite was necessary. The patch avoids creating a second source of truth.

---

### Finding 4 — RESIDUAL RISK — No private live exchange lifecycle can be fully proven from this archive

**Files/areas:** project-wide; especially execution lifecycle, private Bybit fills/reconciliation

The codebase is primarily an operator/recommendation/grid-control application. It contains Bybit public metadata clients, semantics helpers, risk checks, materialization and UI controls, but a complete private OMS/fill-reconciliation layer cannot be fully validated without:

- real/testnet private API credentials;
- exchange-side order placement/fill streams;
- deterministic fixtures for rejected orders, partial fills, stale orders, cancel/replace, retry idempotency and post-restart reconciliation.

**Residual trading risk:** rejected/partial/retried exchange orders and real exchange position drift must still be validated in a private API integration environment before enabling real capital.

**Mitigation in this patch:** all available code-level semantics and UI display checks were verified; new tests lock down dangerous UI tick rounding and beta fail-closed behavior. A future private execution module should call the existing canonical helpers in `app/trading_semantics.py` instead of re-implementing direction logic.

## Tests added or updated

### New file

`tests/test_iteration160_frontend_tick_directional_rounding.py`

Added tests:

1. `test_frontend_tick_rounding_preserves_directional_boundaries_for_bybit_levels`
   - Executes the real extracted JS rounding/formatting functions with Node.
   - Verifies upper boundary `100.001 @ tick 0.01` displays as `100.01`.
   - Verifies lower boundary `99.999 @ tick 0.01` displays as `99.99`.
   - Verifies short SL/TP formatted display preserves directional boundaries.

2. `test_frontend_quantize_no_longer_pre_rounds_value_before_ceil_floor`
   - Prevents regression to the old `scaledValue = Math.round(v * factor)` implementation.

3. `test_btc_beta_fails_closed_on_unaligned_invalid_prices_inside_active_window`
   - Ensures dirty close-only windows do not produce shifted BTC beta/correlation.

### Updated tests

Existing static cache-key tests were updated from `manual-ui-v30` to `manual-ui-v31` so the test suite enforces serving the patched frontend assets.

## Checks performed

### Passed

```text
python -m compileall -q app tests main.py
OK

node --check app/ui/static/app.js
OK

pytest -q
557 passed in 17.03s
```

### Static scan performed

File-hit scan across `app`, `tests`, `docs` for key risk/trading terms:

```text
tp          99
sl          95
stop        53
take        55
upper       69
lower       79
short       59
long        88
side        69
Buy         19
Sell        18
reduceOnly  20
kill        60
leverage    88
pnl         56
roi         11
risk        123
```

These hits were used to focus review on the central semantics, UI, risk and Bybit metadata paths rather than relying on test pass/fail alone.

### Not run / not applicable

```text
npm test / yarn test
NOT RUN: no package.json or yarn.lock exists in this archive.
```

No live/testnet Bybit private execution tests were run because the archive does not include private credentials or a full private exchange fixture harness.

## Files changed

```text
app/features.py
app/ui/static/app.js
app/ui/static/index.html
tests/test_iteration122_ui_detail_badge_fit.py
tests/test_iteration129_ui_single_product_simplification.py
tests/test_iteration132_operator_details_compaction.py
tests/test_iteration133_operator_details_minimal_llm.py
tests/test_iteration134_operator_position_size_details.py
tests/test_iteration135_operator_bot_lifetime_details.py
tests/test_iteration139_ui_no_trade_not_hard_blocker.py
tests/test_iteration146_bybit_chart_url_and_ui_hardening.py
tests/test_iteration147_short_tp_sl_ui_hardening.py
tests/test_iteration149_operator_decision_panel.py
tests/test_iteration151_operator_distance_and_ui_failclosed.py
tests/test_iteration157_ui_invalid_exit_failclosed.py
tests/test_iteration160_frontend_tick_directional_rounding.py
docs/AUDIT_REPORT_2026-06-14_DEEP_BYBIT_DIRECTIONAL_REAUDIT_PATCH.md
```

## Recommended next validation before real capital

1. Add private Bybit V5 testnet fixtures for rejected order, partial fill, canceled order, stale order, retry after timeout and post-restart reconciliation.
2. Add a fixture that compares generated local protective orders with actual exchange order payloads in one-way and hedge mode.
3. Add a shadow/live dry-run that records exchange position state before and after every materialization attempt, then blocks if local and exchange state diverge.
4. Keep `app/trading_semantics.py` as the only allowed source of truth for long/short/TP/SL/order-side semantics.

## Final status

The patched archive passes all available local checks. The two newly found defects are fixed and protected by regression tests. Remaining risks relate to private exchange execution/fill lifecycle, which cannot be fully validated without a live/testnet private integration harness.
