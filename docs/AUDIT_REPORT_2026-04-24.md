# Audit Report 2026-04-24

## Scope

Red-team audit of live-price guards, Bybit instrument status validation and explicit sizing filters.

## Findings

- Execution confirmation must re-check the current ticker against saved grid range and kill-switch.
- Bybit instrument status other than `Trading` must block a new grid launch.
- If explicit order size is known, it must be validated against qty step, min order quantity and min notional.

## Fixes

- Added live-price drift and kill-switch execution blocks.
- Added instrument status and pre-listing checks.
- Added explicit order sizing validation for qty/notional filters.

## Риски

- Min notional cannot be fully checked if the operator/external executor does not provide actual order size.
- Spread/slippage and partial-fill behavior still require live execution telemetry.
