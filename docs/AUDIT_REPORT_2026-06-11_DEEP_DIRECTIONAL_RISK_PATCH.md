# Audit report — Bybit futures trading semantics, TP/SL, risk caps, UI consistency

Date: 2026-06-11  
Scope: deep review and patch pass for Bybit Linear USDT futures/grid execution semantics, directional TP/SL, risk-management fail-closed behavior, UI rendering, and regression tests.

## Executive summary

The project already had a strong central directional model in `app/trading_semantics.py`, but several execution-adjacent paths could still diverge from that model or leave important risk assumptions implicit. This patch makes the Bybit protective-order contract explicit, exposes backend directional geometry status to the UI, hardens execution-time leverage/size risk checks, and adds regression tests that lock the intended long/short semantics.

Main result: all available Python tests pass after the patch: `536 passed in 24.75s`.

## External reference points checked

Bybit V5 create-order documentation was checked for current semantics of `category=linear`, `side=Buy/Sell`, `positionIdx`, `reduceOnly`, `closeOnTrigger`, and `triggerDirection`. Bybit V5 instruments-info documentation was checked for `priceFilter.tickSize`, `lotSizeFilter.qtyStep`, `lotSizeFilter.minNotionalValue`, and `leverageFilter` fields.

## Findings and fixes

| Severity | Area | File / location | Problem | Trading/financial risk | Fix |
|---|---|---|---|---|---|
| High | Bybit TP/SL order semantics | `app/trading_semantics.py:248-280` | Protective TP/SL helper set reduce-only close-side semantics, but did not encode Bybit `triggerDirection`. | A protective conditional order can be ambiguous or rejected/mis-triggered when translated to Bybit V5. For shorts, TP must trigger on a downward move and SL on an upward move. | Added canonical `triggerDirection`: long TP = 1, long SL = 2, short TP = 2, short SL = 1. Kept `reduceOnly=True`, `closeOnTrigger=True`, `positionIdx=0`. |
| High | Backend/UI directional TP/SL consistency | `app/main.py:586-608`; `app/ui/static/app.js:573-608` | Backend returned TP/SL levels without a strict geometry status; UI trusted backend values if present. | A swapped backend payload could be rendered as valid operator guidance even if fallback logic was correct. | Backend now includes `reference_price`, `geometry_valid`, and `geometry_errors`. UI validates backend directional TP/SL before rendering and falls back to local kill-switch mapping if invalid. |
| Medium | Neutral/grid semantics | `app/trading_semantics.py:83-93`; `app/ui/static/app.js:544-570` | Neutral/grid display could still use a generic “Take Profit” label even though no directional TP exists. | Operator could infer a directional exit where only two-sided kill-switch levels exist. | Changed neutral label to `Directional TP unavailable`; UI continues showing lower/upper kill-switch levels. |
| High | Execution-time leverage explicitness | `app/main.py:2325-2330`; tests | Execution preflight could previously treat missing leverage as a legacy warning even when materialising an executable bot. | Margin/liquidation buffer checks can be under-specified; execution may proceed with unproven leverage assumptions. | Execution-plan validation now emits `LEVERAGE_MISSING_FOR_EXECUTION` as an error when `require_execution_plan=True`. Legacy/details validation still reports warning only. |
| High | Runtime risk cap re-check | `app/main.py:1660-1768`, `3172-3176` | Runtime cap checks existed for max leverage/notional/margin but were less explicit about missing estimated notional/margin in payloads that already claim sizing context. | Operator may execute a snapped/generated payload whose max position notional or margin cannot be verified. | Runtime blocks now include `LEVERAGE_MISSING_AT_EXECUTION`, `MAX_LEVERAGE_PER_BOT_AT_EXECUTION`, and `POSITION_SIZE_MISSING_AT_EXECUTION` when sizing/economics context is present but estimated notional/margin is absent. Runtime size checks run after preflight so market-stale/shock blocks remain visible first. |
| Medium | Economics sanity | `app/main.py:2492-2498` | Negative `gross_profit_bps`, `execution_cost_bps`, or `funding_cost_bps` could distort net-edge interpretation. | Negative costs/profits can mask bad economics or turn cost into artificial benefit. | Added explicit execution-time errors: `GRID_GROSS_PROFIT_NEGATIVE`, `GRID_EXECUTION_COST_NEGATIVE`, `GRID_FUNDING_COST_NEGATIVE`. |
| Medium | Regression coverage | `tests/test_iteration155_deep_directional_risk_patch.py` | No single regression file locked all new invariants together. | Future edits could reintroduce short TP/SL inversion, missing trigger direction, or unsafe payload rendering. | Added 10 focused tests covering Bybit trigger direction, backend geometry status, invalid short geometry, explicit leverage, runtime size checks, negative economics, and UI fallback. |

## Long/short semantics verified

Canonical model after patch:

- Long: TP above entry/range; SL below entry/range; close side `Sell`; TP `triggerDirection=1`; SL `triggerDirection=2`.
- Short: TP below entry/range; SL above entry/range; close side `Buy`; TP `triggerDirection=2`; SL `triggerDirection=1`.
- Neutral/grid: no directional TP; lower/upper are kill-switch exits, not directional TP/SL.

The new tests validate both backend mapping and UI hardening. Existing regression tests covering PnL/risk-reward directional symmetry also pass.

## Risk-management behavior after patch

Execution materialisation now follows this order:

1. Re-check current active-bot/symbol/DD/cooldown limits.
2. Snap the recommendation payload to Bybit metadata where safe.
3. Run execution preflight for freshness, market data, live price, funding, market shock, fast veto, and strict Bybit plan validation.
4. Only if preflight passes, run runtime max leverage/notional/margin validation on the exact snapped payload to be stored.

Lower leverage is not treated as an exposure-increasing runtime hazard; Bybit minimum leverage remains enforced by Bybit metadata validation, and margin impact is checked through margin caps. Max leverage remains a runtime block.

## Tests added

New file: `tests/test_iteration155_deep_directional_risk_patch.py`

Added tests:

- `test_bybit_protective_orders_include_directional_trigger_direction`
- `test_backend_directional_exit_payload_includes_geometry_status_for_short`
- `test_backend_directional_exit_payload_marks_invalid_short_geometry`
- `test_execution_preflight_requires_explicit_leverage_for_materialised_bot`
- `test_runtime_risk_blocks_missing_position_size_when_payload_claims_sizing_context`
- `test_execution_preflight_rejects_negative_grid_economics_components`
- `test_operator_ui_rejects_invalid_backend_exit_payload_before_rendering_short_tp_sl`

## Checks executed

| Check | Result |
|---|---|
| `python -m compileall app tests` | Passed |
| `node --check app/ui/static/app.js` | Passed |
| Static grep/scan over TP/SL/upper/lower/long/short/side/Buy/Sell/reduceOnly/kill/leverage/PnL/ROI/risk terms | Completed: 186 code/docs files scanned |
| Targeted regression set | Passed: 54 tests |
| Extended API/preflight/regression set | Passed: 72 tests |
| Full test suite: `python -m pytest -q` | Passed: `536 passed in 24.75s` |
| npm/yarn tests | Not applicable: no `package.json` found |
| configured lint/type checks | Not applicable: no `pyproject.toml`, `ruff.toml`, `mypy.ini`, `tox.ini`, or ESLint config found |

## Residual risks

- This patch does not implement real Bybit order placement; it hardens the semantic contract used before order construction.
- Exact liquidation price remains approximate without Bybit risk-tier/account-margin details; the project already treats this as an estimate and keeps strict leverage/liquidation-buffer validation in preflight.
- Legacy/manual recommendations with no sizing context are still represented as legacy payloads; generated or sizing-aware payloads now fail closed when notional/margin cannot be verified.
- Hedge-mode remains intentionally unsupported by the canonical helper; one-way `positionIdx=0` is enforced for these protective semantics.

## Files changed

- `app/trading_semantics.py`
- `app/main.py`
- `app/ui/static/app.js`
- `tests/test_iteration155_deep_directional_risk_patch.py`
- `docs/AUDIT_REPORT_2026-06-11_DEEP_DIRECTIONAL_RISK_PATCH.md`
