# Audit report — prompt re-audit, grid-only Linear USDT hardening

Date: 2026-05-09

## Scope

Re-audited the project against the attached senior quant / backend / frontend / risk prompt. The product boundary remains strict: only `futures_grid` recommendations for Bybit `category=linear` USDT perpetual contracts are supported.

## Findings

| Area | Finding | Risk | Fix | Files |
|---|---|---|---|---|
| Recommendation logic | A symbol could still proceed to scoring with only 1m features and fewer than 3 usable higher-timeframe histories. Confidence was penalized, but the system did not explicitly reject the setup. | Grid recommendations can be published on insufficient regime evidence, creating false confidence in range detection. | Added fail-closed `INSUFFICIENT_MTF_HISTORY_FOR_GRID` block for futures-grid candidates with fewer than 3 closed multi-timeframe histories. | `app/recommender.py`, `tests/test_iteration124_prompt_reaudit.py` |
| Risk report consistency | `params.risk_report.decision` was built before later gates could change status to `pending` or `blocked`. | UI/API could show a non-actionable row while the risk report still said `recommended`. | Added status-to-risk-report synchronization inside `_sync_recommendation_metadata()`. Only `recommended` and `active` stay `recommended`; all other statuses become `not_recommended`. | `app/recommender.py`, `tests/test_iteration124_prompt_reaudit.py` |
| UI text | Details text did not explicitly include `pending` as a non-executable state. | Operator may treat pending confirmation as launchable. | Updated helper copy to include `pending`. | `app/ui/static/app.js` |

## Trading logic impact

- Grid setup now requires enough multi-timeframe history before it can be considered actionable.
- The rejection is explicit and auditable through `reasons.risk_checks.blocks`.
- No new strategy types were introduced.
- Arithmetic grid, net-per-grid economics, funding, execution friction, margin, leverage and liquidation-buffer checks remain intact.

## Tests

Targeted regression command:

```bash
python -m pytest -q tests/test_iteration124_prompt_reaudit.py tests/test_grid_linear_economics.py tests/test_iteration117_grid_only_strict_preflight.py tests/test_iteration118_grid_bot_risk_caps.py tests/test_iteration119_linear_perpetual_scope.py tests/test_iteration121_operator_guard_fail_closed.py
```

Result: `32 passed`.

A full `python -m pytest -q` run reached the 100% progress marker in this container but did not return a final summary before the execution timeout, so only the targeted regression result above is treated as confirmed.

## Residual risks

- Live Bybit instrument limits, tick/lot filters, funding intervals and fee tiers still require fresh exchange metadata at operator/execution time.
- Liquidation price remains a conservative approximation; exact exchange liquidation depends on account state, mark price, maintenance tier and wallet margin.
- Slippage and fill quality remain modeled, not guaranteed.
- Paper/live execution still needs separate validation before production capital is used.
