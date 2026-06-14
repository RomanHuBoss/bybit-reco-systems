# Audit report — operator fixed-leverage no-trade architecture patch

Date: 2026-06-14  
Scope: Bybit Linear USDT `futures_grid` recommender, runtime risk profile, operator UI/API semantics, margin-risk display, regression tests.

## Section 0 intake and baseline

Read before changes:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- recent reports: `AUDIT_REPORT_2026-06-14_RUNTIME_LEVERAGE_PROFILE_GUARD.md`, `AUDIT_REPORT_2026-06-14_OPERATOR_BLOCKED_NOTRADE_CLARITY.md`, `AUDIT_REPORT_2026-06-14_OPERATOR_PAYLOAD_CONSISTENCY_PATCH.md`, `AUDIT_REPORT_2026-06-14_UI_WORST_CASE_MARGIN.md`, `AUDIT_REPORT_2026-06-14_WORST_CASE_NOTIONAL_RISK_REAUDIT.md`.

Confirmed project boundary from `KNOWN_RISKS.md` / `ARCHITECTURE.md`: this repository is a recommendation + fail-closed operator preflight/audit layer, not a live OMS/EMS. Order lifecycle, fills, retries, private websocket reconciliation and exact exchange liquidation truth remain requirements for an external execution layer, not bugs in nonexistent code.

Baseline before modification:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- `pytest --collect-only -q`: 665 tests collected.
- `pytest -q`: dot output reached 100% without failure text in the container harness, but the command was interrupted before the summary line was captured. No baseline code was changed before this observation.

## Trading semantics map reviewed

Single-source-of-truth and affected display / validation surfaces checked:

- Canonical directional model: `app/trading_semantics.py` (`directional_exit_levels`, `directional_trade_math`, Bybit one-way open/close/protective order semantics).
- Grid economics and liquidation helpers: `app/grid_math.py`.
- Recommender publication path: `app/recommender.py` (`_select_operator_grid_leverage`, `_params`, recommendation status/block/no_trade/risk_report assembly).
- Runtime/API execution preflight: `app/main.py` (`_current_operator_risk_blocks`, `_execution_runtime_size_risk_blocks`, `_validate_trade_plan_against_bybit_meta`, materialization guard).
- UI operator details and blockers: `app/ui/static/app.js`, `app/ui/static/index.html`.
- Persistence/audit: `recommendations.params_json`, `reasons_json`, `blocks_json`, `status` and `effective_status` in API augmentation.
- Existing tests around TP/SL, Bybit side/reduceOnly, runtime leverage guard, no_trade UI clarity, worst-case margin display and operator payload consistency.

No new long/short TP/SL inversion was found. No new implementation of directional TP/SL, PnL, ROI or Bybit Buy/Sell/reduceOnly mapping bypassing `app/trading_semantics.py` was introduced.

## Findings and fixes

### HIGH — fixed operator leverage profile produced synthetic `1x` payloads that later looked like hard `blocked` rows

- **Files / lines:**
  - `app/recommender.py:1939-2011`
  - `app/recommender.py:2287-2295`
  - `app/recommender.py:3534-3552`
  - `app/recommender.py:3614-3625`
  - `app/recommender.py:3669-3676`
- **Problem:** when runtime risk profile was fixed, e.g. `min_leverage=5`, `max_leverage=5`, weak/thin/volatile ideas were intentionally downgraded by `_select_operator_grid_leverage()` to `1x`. The later risk gate then produced `MIN_LEVERAGE_PER_BOT` / `MIN_LEVERAGE_PER_BOT_AT_EXECUTION`, so ordinary non-actionable trade ideas appeared as hard runtime blocks. With a strict fixed profile this could make every symbol appear `blocked` even though the true state was “not actionable at fixed 5x”.
- **Trading risk:** operator interpretation was distorted. `blocked` should mean fail-closed technical/risk violation; insufficient signal quality for the operator’s fixed leverage profile should be `no_trade`. The old display encouraged operators to fight the risk profile instead of understanding that the setup simply did not qualify for 5x.
- **Fix:** `_select_operator_grid_leverage()` now always returns the active operator target leverage for the evaluated profile. When the idea cannot justify that profile it sets:
  - `operator_minimum_approved=false`
  - `not_actionable_reason=<reason>`
  - no synthetic `1x` payload is emitted for new generated recommendations.
- **Status semantics:** recommendation assembly converts this condition to `status=no_trade` with `OPERATOR_LEVERAGE_PROFILE_NOT_ACTIONABLE` in `risk_report.no_trade_reasons`, but only if no hard block exists. Legacy/manual rows that already contain `1x` remain fail-closed through execution-time guards in `app/main.py`.
- **Safety direction:** fail-closed behavior is preserved. `no_trade` rows are not executable by the API, and legacy lower-leverage rows remain blocked at execution.

### MEDIUM — recommendation-time margin cap still used reference-price margin before worst-case grid-envelope margin

- **Files / lines:**
  - `app/recommender.py:3614-3625`
  - `app/recommender.py:3650-3659`
- **Problem:** prior audits hardened runtime/UI worst-case margin handling, but the recommendation-time `MAX_MARGIN_PER_BOT` check still read `estimated_margin_required_usdt` before the newer `estimated_worst_case_margin_required_usdt`.
- **Trading risk:** a fixed-qty grid can consume more margin near the upper grid boundary than at reference price. Recommendation-time risk reporting could understate capital requirement even though later runtime preflight was stricter.
- **Fix:** recommendation-time margin cap now prefers `estimated_worst_case_margin_required_usdt` and falls back to legacy `estimated_margin_required_usdt` only for legacy payloads. `risk_report.capital_required_usdt` uses the same worst-case-first convention.
- **Safety direction:** strictly more conservative; no guard was weakened.

### MEDIUM — UI mixed no_trade reasons with hard risk rejections and duplicated identical block text

- **Files / lines:**
  - `app/ui/static/app.js:1006-1023`
  - `app/ui/static/app.js:1031-1036`
  - `app/ui/static/app.js:1052-1107`
  - `app/ui/static/index.html:7,126`
- **Problem:** UI details treated `risk_report.rejection_reasons` as hard blockers, and the same reason could appear once from `blocks` and once as `RISK`. There was no separate UI channel for soft `no_trade` reasons.
- **Trading risk:** the details panel could overstate ordinary no_trade conditions as hard blocks and visually duplicate the same reason, reducing operator trust and making runtime diagnosis harder.
- **Fix:** UI now reads `risk_report.no_trade_reasons` as non-critical no_trade explanations, keeps `rejection_reasons` for hard blockers only, and deduplicates blocker/warning rows by message text. Static asset cache key was bumped to `manual-ui-v34`.
- **Safety direction:** launchability logic was not relaxed. This is display clarification only.

## Tests added / updated

Added `tests/test_iteration173_operator_leverage_no_trade_policy.py`:

1. `test_fixed_operator_profile_declines_as_no_trade_instead_of_one_x_fallback`
   - Red before fix: selector returned `1`.
   - Green after fix: selector returns active profile target `5` and marks `operator_minimum_approved=false`.

2. `test_params_fixed_operator_profile_never_publishes_one_x_payload_for_declined_idea`
   - Red before fix: `_params()` published `params["leverage"] == 1` under fixed `5x/5x`.
   - Green after fix: `_params()` keeps `params["leverage"] == 5` and records `not_actionable_reason`.

3. `test_ui_no_trade_reasons_are_not_treated_as_hard_risk_rejections`
   - Red before fix: UI had no `riskReportNoTradeReasons` path and no blocker deduplication.
   - Green after fix: UI separates no_trade reasons from hard blockers and deduplicates display items.

Updated `tests/test_iteration159_no_trade_regression.py`:

- Replaced the old expectation that thin-edge ideas fall back to `1x` with the safer invariant: they keep the active operator target leverage and are marked not actionable.

Updated UI cache-key tests from `manual-ui-v33` to `manual-ui-v34` because `app/ui/static/app.js` changed.

## Verification after changes

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- Targeted regression groups:
  - `tests/test_iteration159_no_trade_regression.py`
  - `tests/test_iteration173_operator_leverage_no_trade_policy.py`
  - `tests/test_iteration164_runtime_leverage_profile_guard.py`
  - UI status/cache/detail tests `iteration122/129/132/133/134/135/139/146/147/149/151/157/166/172`
  - Result: 55 passed.
- Grid/recommender smoke subset:
  - `tests/test_grid_linear_economics.py`
  - `tests/test_logic.py::test_run_recommender_once_smoke_generates_recommendations_without_runtime_name_error`
  - `tests/test_logic.py::test_run_recommender_once_emits_long_for_bullish_range_market`
  - Result: 15 passed.
- Full suite with verbose output captured in `/tmp/pytest_vv.log`: `668 passed in 26.51s`.
- `npm`/`yarn`: not run; no `package.json` test/lint configuration is present in the repository root.
- Live Bybit/private API execution tests: not run in this offline/container audit. This is consistent with the repository boundary as recommendation/preflight service rather than live OMS/EMS.

## Static scan summary

Saved as `docs/STATIC_SCAN_2026-06-14_OPERATOR_FIXED_LEVERAGE_NOTRADE.txt`.

Changed hits reviewed:

- `app/recommender.py:_select_operator_grid_leverage` — safe; no synthetic `1x` payload under fixed higher profile.
- `app/recommender.py` status assembly — safe; fixed-profile disqualification is `no_trade`, hard blocks remain blocks.
- `app/recommender.py` margin cap — safe; worst-case margin preferred.
- `app/ui/static/app.js` details panel — safe; no_trade reason display separated from hard blocker display.
- `app/ui/static/index.html` — safe cache-bust only.

## Residual risks and KNOWN_RISKS delta

Closed / clarified:

- Fixed-leverage operator profile no longer turns ordinary weak ideas into synthetic `1x < min_leverage` hard blocks.
- Operator UI now has distinct channels for hard blocks, no_trade reasons and warnings.
- Recommendation-time capital required follows the same worst-case-first convention as the runtime/UI patches.

Still residual:

- This repository still does not know real exchange fills, open orders, wallet margin, private liquidation state or websocket reconciliation. External OMS/EMS must repeat leverage, margin, balance, qtyStep/minNotional and account-mode checks before creating any real Bybit bot.
- Calibration/outcome labels remain proxy-based and should not be treated as real fill/funding/liquidation truth.
- Old database rows can still contain historical `1x` payloads; this is intentional audit history. They remain execution-blocked by runtime guards.
