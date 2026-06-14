# Audit Report — Runtime leverage profile guard and stale operator recommendations

Date: 2026-06-14
Scope: Bybit futures / linear USDT futures-grid recommendation publication, operator API, execution-time risk checks, UI status rendering, regression tests.

## Executive summary

A fail-open condition was confirmed around operator leverage profiles. A futures-grid row could remain operator-facing as `recommended` while its payload had `params.leverage = 1`, even though the current runtime profile exposed by `/api/v1/risk/status` required a fixed higher leverage such as `min_leverage=3`, `max_leverage=3`. The same row could also carry a stale embedded `leverage_policy` generated under an older profile such as `5x..10x` and still have empty `blocks` / empty `effective_status`.

This patch makes the system fail closed at three layers:

1. New recommendations use the same normalized runtime risk limits for both publication blocks and `params.leverage_policy` / sizing.
2. Persisted recommendations are revalidated for the operator view against the current runtime profile.
3. Execution-time risk checks reject real operator payloads whose leverage is below the current operator minimum.

## Findings and fixes

### 1. Critical — recommendation params used stale/default risk limits instead of active runtime limits

- File: `app/recommender.py`
- Area: `_params(...)` and `run_recommender_once(...)`
- Problem: `run_recommender_once()` loaded active DB risk limits for `gate_candidate(...)`, but `_params()` built `leverage_policy` from `settings.risk_limits`. If runtime DB limits had been changed after startup, the recommendation payload and the risk-status endpoint could disagree.
- Trading risk: UI could show a grid launch sheet whose leverage, margin and notional were computed under a different operator profile from the currently enforced profile.
- Fix:
  - `limits = normalize_risk_limits(db.get_active_risk_limits(conn), settings.risk_limits)` is now used in the recommender cycle.
  - `_params(..., risk_limits=limits)` now builds `leverage_policy` from the exact normalized runtime limits used for publication checks.
- Tests:
  - `tests/test_iteration164_runtime_leverage_profile_guard.py::test_operator_api_blocks_legacy_one_x_recommendation_when_current_profile_is_fixed_three_x`

### 2. Critical — persisted 1x row could remain actionable after runtime profile changed

- File: `app/main.py`
- Area: `_augment_reco_for_ui(...)`, new `_apply_runtime_risk_limits_guard(...)`
- Problem: UI/API augmentation validated Bybit metadata and LLM state, but did not re-check current runtime leverage limits against persisted operator payloads. A stale row could be returned as `recommended` even after `/risk/status` had moved to a fixed higher leverage.
- Trading risk: operator could manually launch an old 1x grid sheet while the system currently required a fixed higher leverage, or launch a row whose embedded policy was generated under a different risk profile.
- Fix:
  - Added `_apply_runtime_risk_limits_guard(...)`.
  - Added `RUNTIME_RISK_PROFILE_CHANGED` block when embedded `leverage_policy` differs from current runtime limits.
  - Added `MIN_LEVERAGE_PER_BOT_AT_EXECUTION` block for real operator payloads below current `min_leverage`.
  - Operator payloads with these errors are exposed as `status=blocked`, `effective_status=blocked`, `risk_report.decision=not_recommended`.
- Tests:
  - `tests/test_iteration164_runtime_leverage_profile_guard.py::test_operator_api_blocks_legacy_one_x_recommendation_when_current_profile_is_fixed_three_x`
  - `tests/test_iteration164_runtime_leverage_profile_guard.py::test_execution_runtime_guard_blocks_leverage_below_operator_minimum`

### 3. High — stale snapshot remained launchable in operator API

- File: `app/main.py`
- Area: `/api/v1/recommendations`
- Problem: endpoint returned `snapshot_is_stale=true`, but individual rows could still appear as actionable if their stored status was `recommended` / `active`.
- Trading risk: operator could launch grid parameters based on stale price, risk profile and market regime assumptions.
- Fix:
  - Added `_apply_snapshot_stale_guard(...)`.
  - If snapshot age exceeds `max(180, reco_interval_sec * 3)`, actionable rows are converted to `blocked` with `SNAPSHOT_STALE_FOR_OPERATOR_LAUNCH`.
- Tests:
  - `tests/test_iteration164_runtime_leverage_profile_guard.py::test_operator_api_blocks_stale_snapshot_even_when_stored_status_is_recommended`

### 4. High — `effective_status` could be empty while UI relied on `status`

- File: `app/main.py`, `app/ui/static/app.js`
- Problem: operator payloads could carry an empty `effective_status`, and frontend logic used `it.status` directly in launchability, badges, table highlighting and no-trade banners.
- Trading risk: UI could display stored status rather than effective risk-adjusted status after guards demoted a row.
- Fix:
  - Added `_ensure_effective_status(...)` in backend.
  - Frontend now uses `operatorEffectiveStatus(it)` for launchability, details badge, table badge, row highlight and no-trade banner.
- Tests updated:
  - `tests/test_iteration122_ui_detail_badge_fit.py`
  - `tests/test_iteration130_non_actionable_launch_links.py`
  - `tests/test_iteration140_ui_pending_not_no_trade.py`
  - `tests/test_iteration146_bybit_chart_url_and_ui_hardening.py`

### 5. Medium — execution-time guard checked max leverage but not operator minimum

- File: `app/main.py`
- Area: `_execution_runtime_size_risk_blocks(...)`
- Problem: lower leverage was previously treated as not exposure-increasing and therefore not blocked. That is unsafe when the operator profile is intended to be fixed, e.g. `min_leverage == max_leverage == 3`, because margin/notional/liquidation assumptions no longer match the approved payload.
- Trading risk: manual execution could bypass the selected leverage profile and change the economics of the grid.
- Fix:
  - Real operator payloads now fail execution-time size/risk checks if `leverage < current min_leverage`.
  - The guard is scoped to real recommender/operator payloads that carry publication-time risk context to avoid reclassifying historical minimal API-shape fixtures as live launch sheets.
- Tests:
  - `tests/test_iteration164_runtime_leverage_profile_guard.py::test_execution_runtime_guard_blocks_leverage_below_operator_minimum`

## Files changed

- `app/recommender.py`
- `app/main.py`
- `app/ui/static/app.js`
- `tests/test_iteration164_runtime_leverage_profile_guard.py`
- `tests/test_iteration122_ui_detail_badge_fit.py`
- `tests/test_iteration130_non_actionable_launch_links.py`
- `tests/test_iteration140_ui_pending_not_no_trade.py`
- `tests/test_iteration146_bybit_chart_url_and_ui_hardening.py`
- `docs/AUDIT_REPORT_2026-06-14_RUNTIME_LEVERAGE_PROFILE_GUARD.md`

## Checks performed

### Passed

- `python -m compileall -q app`
- `node --check app/ui/static/app.js`
- Targeted regression suite:
  - `65 passed`
- Full test suite was executed in chunks due post-run process hang in the full single pytest process:
  - chunk 1: `179 passed`
  - chunk 2: `130 passed`
  - chunk 3: `121 passed`
  - chunk 4: `147 passed`
  - total chunked coverage: `577 passed`

### Not fully clean as a single command

- `pytest -q` printed progress to `100%` with no `F` after fixes, but the process did not exit before the container timeout. The same tests pass cleanly when split into chunks. This appears to be a test-process teardown/background-thread hang rather than a test assertion failure.

### Static scan performed

Searched/inspected trading-semantics keywords across `app/`, `app/ui/static/`, and `tests/`:

- `tp`, `sl`, `stop`, `take`
- `upper`, `lower`, `kill`
- `long`, `short`, `side`, `Buy`, `Sell`
- `reduceOnly`
- `leverage`, `pnl`, `roi`, `risk`

No additional leverage-profile fail-open path was found in the patched operator publication/API/execution path.

## Residual risks

1. Stale recommendations already stored in a live DB will be demoted by the API/operator view, but their DB `status` field is not mutated by the list endpoint. This preserves audit history. A separate migration/sweeper can be added later if hard mutation of historical rows is desired.
2. Full single-process pytest still appears to hang after completing all tests. The chunked suite passes, but the teardown hang should be investigated separately to make CI behavior cleaner.
3. Exact liquidation price remains approximate and is still documented as such; Bybit risk tier / mark price / wallet margin can change true liquidation thresholds.

## Operator-facing expected behavior after patch

For a row like:

```json
{
  "status": "recommended",
  "params": {
    "leverage": 1,
    "leverage_policy": {
      "min_operator_leverage": 5,
      "max_operator_leverage": 10,
      "note": "signal_quality_too_low_for_operator_minimum"
    }
  }
}
```

when current `/api/v1/risk/status` is:

```json
{
  "limits": {
    "min_leverage": 3,
    "max_leverage": 3
  }
}
```

the operator API now returns it as non-actionable:

```json
{
  "status": "blocked",
  "effective_status": "blocked",
  "blocks": [
    {"code": "MIN_LEVERAGE_PER_BOT_AT_EXECUTION"},
    {"code": "RUNTIME_RISK_PROFILE_CHANGED"}
  ],
  "params": {
    "risk_report": {
      "decision": "not_recommended"
    }
  }
}
```
