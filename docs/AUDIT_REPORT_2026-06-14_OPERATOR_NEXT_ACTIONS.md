# Audit report — operator next actions for blocked recommendations

Date: 2026-06-14  
Scope: Bybit Linear USDT `futures_grid` operator UI/API diagnostics for `blocked` / `no_trade` rows, especially `LIQUIDATION_BUFFER_TOO_LOW`.

## Section 0 intake and baseline

Read before changes:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- Recent audit reports: `AUDIT_REPORT_2026-06-14_OPERATOR_FIXED_LEVERAGE_NOTRADE.md`, `AUDIT_REPORT_2026-06-14_OPERATOR_BLOCKED_NOTRADE_CLARITY.md`, `AUDIT_REPORT_2026-06-14_RUNTIME_LEVERAGE_PROFILE_GUARD.md`, `AUDIT_REPORT_2026-06-14_UI_WORST_CASE_MARGIN.md`, `AUDIT_REPORT_2026-06-14_WORST_CASE_NOTIONAL_RISK_REAUDIT.md`.

Confirmed project boundary: repository is a recommendation + fail-closed operator preflight/audit layer, not a live OMS/EMS. Real order lifecycle, fills, private exchange reconciliation and exact Bybit liquidation truth remain external execution-layer responsibilities.

Baseline before modification:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- `pytest -q`: `668 passed in 18.78s`.

## Issue reported

Operator details showed the actual blocker, for example:

- `LIQUIDATION_BUFFER_TOO_LOW`: estimated liquidation buffer `5.53% < 12%`.
- Runtime/preflight `RISK`: worst-side liquidation buffer is too small for `futures_grid` with `leverage=5`.
- Warnings for volatility, fees/funding, trend/grid-break risk and spread.

This is a correct fail-closed trading decision. The defect was not that these rows were blocked; the defect was that the UI/API did not expose a concrete next-step recommendation. A portfolio dominated by blocked rows looked like “no recommendations” instead of “the current 5x grid profile is not actionable under the risk floor”.

## Trading semantics map reviewed

Single-source and display/validation surfaces checked:

- Canonical directional model: `app/trading_semantics.py`.
- Approximate linear liquidation and buffer math: `app/grid_math.py`.
- Operator API/detail context and Bybit preflight guard: `app/main.py`.
- Recommender payload and risk report semantics: `app/recommender.py`.
- Operator UI details panel: `app/ui/static/app.js` and `app/ui/static/index.html`.
- Existing regression tests for UI blocked/no_trade clarity, runtime leverage guards and worst-case margin display.

No TP/SL, side, PnL, ROI, risk:reward or Bybit Buy/Sell/reduceOnly semantics were changed. No launchability predicate was relaxed.

## Findings and fixes

### MEDIUM — blocked rows exposed the cause but not a safe operator recommendation

- **Files / lines:**
  - `app/main.py:1089-1130` — advisory max-safe-leverage estimate using the same approximate liquidation helper as the guard.
  - `app/main.py:1133-1227` — `operator_next_actions` builder.
  - `app/main.py:1323-1379` — detail context now attaches `operator_next_actions`.
  - `app/ui/static/app.js:497-516` — details panel renders a “Что делать дальше” card.
  - `app/ui/static/app.js:1136-1147` and details template below it — next actions are shown immediately after blockers and before rank diagnostics.
- **Problem:** UI could tell the operator “blocked” but not what to change. For `LIQUIDATION_BUFFER_TOO_LOW`, the actionable answer is not “force launch”; it is: do not launch, reduce fixed leverage / increase isolated margin / narrow adverse range, then wait for a new recommendation.
- **Trading/financial risk:** operator might treat repeated blockers as system failure and manually bypass safety, or incorrectly lower the liquidation-buffer floor. This is an interpretation risk, not an execution bug.
- **Fix:** backend now adds advisory `operator_decision_context.operator_next_actions`. For low liquidation buffer it includes:
  - `DO_NOT_LAUNCH_LOW_LIQUIDATION_BUFFER` with current buffer, required 12% floor, current leverage and an approximate safe leverage ceiling if derivable.
  - `RECALCULATE_WITH_LOWER_LEVERAGE_OR_NARROWER_RANGE` telling the operator to change risk profile/range and not lower the 12% floor.
  - Additional generic actions for thin net edge, stale funding and stale price/range blockers.
- **Safety direction:** strictly safer UX. The recommendation remains `blocked`/`no_trade`; execution preflight remains fail-closed.

### LOW — stale frontend cache key after detail-card change

- **Files / lines:**
  - `app/ui/static/index.html:7,126`.
- **Fix:** static asset version bumped from `manual-ui-v34` to `manual-ui-v35`.

## Tests added / updated

Added `tests/test_iteration174_operator_next_actions.py`:

1. `test_low_liquidation_buffer_details_expose_next_safe_actions`
   - Red before fix: no `operator_next_actions` existed in backend detail context.
   - Green after fix: `LIQUIDATION_BUFFER_TOO_LOW` produces “do not launch” + “recalculate lower leverage/narrower range”, and estimates `≤3x` for the sample `5.53%` buffer case.
2. `test_frontend_renders_next_actions_after_blockers_before_rank_diagnostics`
   - Red before fix: frontend had no `operatorNextActionsHtml` and no “Что делать дальше” card.
   - Green after fix: next actions render between blocker details and rank diagnostics.
3. `test_static_asset_cache_key_bumped_after_next_actions_patch`
   - Ensures browser does not keep stale `manual-ui-v34` JS.

Updated existing UI cache-key tests from `manual-ui-v34` to `manual-ui-v35`.

## Verification after changes

Commands executed from project root:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q tests/test_iteration174_operator_next_actions.py
pytest -q
```

Results:

```text
python compileall: passed
node --check app/ui/static/app.js: passed
targeted test: 3 passed in 0.94s
full pytest: 671 passed in 17.70s
```

`npm` / `yarn` tests were not run because the project root has no `package.json` test/lint configuration.

Live Bybit/private API execution tests were not run in the offline container. This matches the documented system boundary: exact exchange liquidation, account margin and order/fill reconciliation remain external execution-layer checks.

## Static scan

Saved as `docs/STATIC_SCAN_2026-06-14_OPERATOR_NEXT_ACTIONS.txt`.

Changed hits reviewed:

- `app/main.py`: safe; adds advisory explanation only. Does not mutate `status`, `blocks`, `ok`, risk guards or execution checks.
- `app/ui/static/app.js`: safe; renders backend guidance only. No launch/execute predicate changed.
- `app/ui/static/index.html`: safe cache-bust.
- `tests/test_iteration174_operator_next_actions.py`: safe regression coverage.

## Residual risks and `KNOWN_RISKS.md` delta

No known-risk category was removed. The following remain unchanged:

- no real OMS/EMS in this repository;
- exact Bybit liquidation depends on live risk tier, mark price, account margin and wallet state;
- proxy outcome labels are not real fill/funding/liquidation truth;
- operator must still rerun preflight before any real launch.

New clarification: when most rows are `blocked` due to `LIQUIDATION_BUFFER_TOO_LOW`, the system is doing the safe thing. The next operator action is to change the risk profile or wait for a new market setup, not to disable the guard or force recommended status.
