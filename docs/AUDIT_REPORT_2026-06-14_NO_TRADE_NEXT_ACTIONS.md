# Audit report — no_trade next actions for operator details

## Scope

Follow-up audit of the operator details panel for futures-grid rows that are not hard `blocked`, but are persisted/effective `no_trade` because the idea does not pass the current fixed leverage profile or launch-quality gates.

The specific reproduced case is a `FILUSDT` short futures grid with:

- status badge: `no_trade`;
- no hard Bybit/preflight blocker;
- no-trade reason: `OPERATOR_LEVERAGE_PROFILE_NOT_ACTIONABLE`, `reason=signal_quality_too_low_for_operator_minimum`;
- warnings about adverse funding/costs, strong trend, high volatility and spread.

## Baseline

Before this patch, the codebase from the previous archive was green:

```text
python -m compileall -q app tests main.py: passed
node --check app/ui/static/app.js: passed
pytest -q: 671 passed
```

## Finding

### Medium — `no_trade` detail rows had reasons but no operator remediation actions

- Files:
  - `app/main.py`, `_operator_next_actions_for_reco(...)`.
  - `app/ui/static/app.js`, details panel rendering already supported `operator_next_actions`, but the backend produced actions only for hard guard errors/warnings.
- Problem:
  - Hard blocked rows with `LIQUIDATION_BUFFER_TOO_LOW` received a “Что делать дальше” card.
  - Non-hard `no_trade` rows showed the reason and warnings, but no dedicated next-action guidance.
  - For an operator, this looked similar to “no recommendations”, even though the correct semantic is: “do not launch this grid under the current fixed leverage / quality / cost regime”.
- Trading risk:
  - The absence of explicit next actions can push the operator toward manually launching a `no_trade` idea because it is not a Bybit/preflight blocker.
  - This is especially risky for `signal_quality_too_low_for_operator_minimum` at a 5x profile, where the idea is intentionally not actionable without weakening risk policy.

## Fix

### Backend

`app/main.py` now derives advisory `operator_next_actions` not only from hard guard errors, but also from:

- `params.risk_report.no_trade_reasons`;
- `reasons.decision_layers.no_trade_reasons`;
- `params.risk_report.warnings`;
- `reasons.top_negative_factors`.

Added safe, non-permissive actions for no-trade scenarios:

- `DO_NOT_LAUNCH_PROFILE_NOT_ACTIONABLE` — keep no_trade under the current fixed leverage profile.
- `WAIT_FOR_STRONGER_SIGNAL_OR_RANGE` — wait for stronger directional bias or a stable range regime.
- `WAIT_FOR_LOWER_VOLATILITY` — do not run grid while volatility/range-break risk is too high.
- `WAIT_FOR_WIDER_NET_EDGE` — wait for better post-cost grid edge.
- `CHECK_COSTS_AND_FUNDING_BEFORE_NEXT_PUBLICATION` — refresh funding/cost snapshot.
- `AVOID_GRID_IN_STRONG_TREND` — avoid grid against a strong trend.
- `WAIT_FOR_TIGHTER_SPREAD` — wait for better liquidity/spread.
- `KEEP_NO_TRADE_AND_REFRESH` — fallback for unclassified no_trade reasons.

All actions are advisory only. They do not change status, guard outcome, thresholds, launchability, TP/SL, side, PnL, ROI, risk:reward, or Bybit order semantics.

### Frontend

- `app/ui/static/index.html` cache key bumped to `manual-ui-v36`.
- Existing details rendering now displays the new backend-provided next actions for `no_trade` rows.

### Tests

Updated/added regression coverage:

- `tests/test_iteration174_operator_next_actions.py::test_no_trade_profile_reason_exposes_next_safe_actions`
  - Red before fix: backend returned no `operator_next_actions` for a no_trade profile-not-actionable row.
  - Green after fix: details context includes safe actions for profile-not-actionable, signal quality and volatility.
- Static cache-key assertions updated to `manual-ui-v36` so the changed JS is not hidden by browser cache.

## Post-fix checks

```text
python -m compileall -q app tests main.py: passed
node --check app/ui/static/app.js: passed
pytest -q: 672 passed
```

## Safety note

This patch preserves fail-closed behavior. It does **not** convert `no_trade` to `recommended`, does **not** lower the 12% liquidation-buffer floor, and does **not** weaken the fixed leverage profile automatically. It only explains the next safe operator actions.
