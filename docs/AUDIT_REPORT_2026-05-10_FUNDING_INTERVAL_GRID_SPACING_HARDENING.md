# Audit report — funding interval and funding-aware grid spacing hardening

Date: 2026-05-10
Scope: Bybit Linear USDT Futures / USDT Perpetual `futures_grid` only.

## Summary

The project already enforced a strict grid-only product boundary and passed the existing regression suite, but the re-audit found two remaining hardening points in the funding/execution-cost path:

1. `BybitPublicClient.get_funding_rate()` depended on ticker `fundingIntervalHour` when available. Bybit documents the canonical funding interval on instruments-info as `fundingInterval` in minutes, and funding-history explicitly points consumers to instruments-info for the interval. If ticker omitted the field, the recommender could later fall back to the generic 8h interval.
2. Generated grid spacing used execution cost as the geometry floor, while adverse expected funding was checked later through net-profit gates. This was safe at approval time but could render an unnecessarily dense, non-actionable grid before the block was applied.

## Critical fixes

| Area | Issue | Risk | Fix | Files |
|---|---|---|---|---|
| Bybit funding metadata | Missing ticker `fundingIntervalHour` was not enriched from instruments-info. | Wrong funding-event count and understated/overstated carry when an instrument has non-8h funding. | Added instruments-info fallback for `fundingInterval` minutes in `get_funding_rate()`. | `app/bybit_client.py`, `tests/test_iteration126_funding_interval_and_grid_spacing.py` |
| Grid economics | Minimum spacing used fees/spread/slippage but not adverse expected funding carry. | UI could show a dense grid whose gross step was economically invalid once funding was included. | Spacing cost floor now equals execution cost plus positive expected funding bps; funding receipts are excluded from spacing and approval edge. | `app/recommender.py`, `tests/test_iteration126_funding_interval_and_grid_spacing.py` |
| Documentation | README/trading logic did not state the funding-aware spacing rule. | Operator could misread a funding receipt as a reason to tighten grid levels. | Updated README, trading logic and changelog. | `README.md`, `docs/TRADING_LOGIC.md`, `CHANGELOG.md` |

## Validation

Added regression tests:

- `test_funding_helper_falls_back_to_instrument_info_for_interval`
- `test_grid_spacing_floor_includes_adverse_expected_funding`
- `test_grid_spacing_floor_does_not_use_funding_receipt_as_free_edge`

Commands run:

```bash
python -m pytest tests/test_iteration126_funding_interval_and_grid_spacing.py -q
python -m pytest -q
```

Result: all tests passed.

## Residual risks

- Exact account fee tier still must be configured by the operator; default fee remains a conservative heuristic.
- Exact liquidation still depends on live Bybit risk tiers, mark price and wallet margin.
- Slippage model remains conservative heuristic and must be checked against paper/live execution.
- Funding rate can change before launch; execution preflight still re-checks fresh funding before confirming a bot.
