# Audit Report 2026-05-08

## Scope

Audit of Bybit Linear USDT Futures grid economics, liquidation/risk reporting and operator UI risk visibility.

## Findings

- Gross grid profit is unsafe unless compared against fees, spread/slippage and funding carry.
- Leverage must be treated as liquidation-risk amplification, not as free profit amplification.
- UI must show net per-grid economics, required margin and liquidation buffer.

## Fixes

- Added Decimal-based grid economics helpers for linear PnL, fees, funding, margin and conservative liquidation buffer.
- Added economics payloads under `params.sizing`, `params.economics` and `reasons.grid_economics`.
- Added UI fields for net/gross grid economics, margin and liquidation buffer.

## Риски

- Liquidation estimate is conservative and simplified; Bybit risk tier and account state are required for exact values.
- Funding history and actual funding interval should be sourced from current exchange data.
