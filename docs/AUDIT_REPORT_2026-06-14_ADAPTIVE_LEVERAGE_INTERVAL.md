# Audit Report — Adaptive operator leverage interval for futures grid

Date: 2026-06-14  
Scope: `futures_grid` recommendation payload leverage selection for Bybit linear USDT.

## Summary

The recommender previously interpreted an operator leverage interval such as `min_leverage=3`, `max_leverage=5` as an approval profile whose selected payload leverage was always the lower bound. This was safe, but it did not implement the intended operator behavior: use 3x for acceptable setups, 4x for better setups, and 5x only for the strongest setups.

This patch keeps all runtime/preflight guards fail-closed and changes only the publication-time leverage selector:

- fixed profiles such as `5x..5x` remain fixed;
- interval profiles such as `3x..5x` now select dynamically inside the operator-approved range;
- promotion above the minimum requires setup quality, net grid edge after costs, and low ATR;
- promotion is clamped by an approximate liquidation-buffer safety precheck before the normal downstream liquidation guard runs.

## Finding and fix

### Medium — operator leverage interval was effectively treated as fixed at the lower bound

- File: `app/recommender.py`, lines ~1927-2464.
- Problem: `_select_operator_grid_leverage()` normalized `min_operator_leverage` and `max_operator_leverage`, but selected `target_leverage = min(max_lev, min_lev)`. With a `3x..5x` profile, the payload was always `3x`, even when diagnostics showed a strong, low-volatility, high-edge setup.
- Trading impact: fewer high-quality ideas used the operator-approved leverage interval; the operator had no automatic differentiation between acceptable, medium, and strong setups.
- Fix:
  - added `_adaptive_grid_leverage_from_quality()`;
  - extended `_select_operator_grid_leverage()` with adaptive interval selection;
  - added `liquidation_safe_max_leverage` support so the selector cannot promote above the approximate safe liquidation-buffer cap;
  - added `_max_liquidation_safe_grid_leverage()` and wired it into `_params()` using the same adverse-boundary convention already used by economics/liquidation checks.

## Adaptive leverage semantics

For a `3x..5x` operator profile:

- baseline actionable setup: `3x`;
- medium-quality setup: can promote to `4x`;
- strong setup: can promote to `5x`;
- if approximate worst-side liquidation buffer allows only `4x`, the selector caps at `4x` even if signal quality would otherwise promote to `5x`;
- if the final payload still violates downstream risk checks, existing `LIQUIDATION_BUFFER_TOO_LOW`, leverage, notional, margin, Bybit preflight and runtime guards still block it.

The patch does not lower any risk threshold and does not convert any fail-closed path into fail-open behavior.

## Tests added

File: `tests/test_iteration177_adaptive_leverage_interval.py`

- `test_adaptive_operator_interval_promotes_medium_setup_to_four_x`
- `test_adaptive_operator_interval_promotes_strong_setup_to_3x_5x`
- `test_adaptive_operator_interval_respects_liquidation_safe_leverage_clamp`
- `test_params_use_adaptive_3x_5x_operator_interval_for_strong_grid_setup`

Red baseline against the previous audited archive:

```text
4 failed
- medium setup returned 3x instead of 4x
- strong setup returned 3x instead of 5x
- selector did not accept liquidation_safe_max_leverage
- _params() returned 3x instead of 5x for a strong 3x..5x setup
```

Post-patch targeted tests:

```text
13 passed
pytest -q tests/test_iteration159_no_trade_regression.py \
          tests/test_iteration164_runtime_leverage_profile_guard.py \
          tests/test_iteration173_operator_leverage_no_trade_policy.py \
          tests/test_iteration177_adaptive_leverage_interval.py
```

Post-patch full checks:

```text
python -m compileall -q app tests main.py: passed
node --check app/ui/static/app.js: passed
pytest -q: 682 passed
```

Baseline before this patch:

```text
python -m compileall -q app tests main.py: passed
node --check app/ui/static/app.js: passed
pytest -q: 678 passed
```

## Residual risks

- Liquidation price remains an approximation; Bybit risk tiers, mark price, account equity and wallet margin must still be checked by live preflight/executor.
- The repository remains a recommender + fail-closed preflight, not a full live OMS/EMS.
- Existing persisted recommendations generated before this patch retain their historical `params.leverage`; operator API/runtime guards still revalidate stale rows.
