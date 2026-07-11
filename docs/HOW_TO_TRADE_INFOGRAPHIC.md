# How to trade - operator quick reference

This repository is a recommendation/audit service, not OMS/EMS. It does not manage live order lifecycle, open orders, fills, partial fills, or exchange reconciliation. The executable truth must remain in an external Bybit execution/reconciliation layer.

## Current shipped risk profile

- `min_leverage=3`, `max_leverage=5`.
- 3-5x is the baseline actionable leverage interval for this revision.
- One running bot per account/symbol by default.
- Linear USDT Futures Grid only; non-linear venue, spot, options, inverse contracts, unsupported symbols, and non-USDT pairs are blocked.

## Directional TP/SL model

- Long: TP above entry/reference, SL below entry/reference.
- Short: TP below entry/reference, SL above entry/reference.
- Neutral grid: no single directional TP; lower and upper outer levels are kill-switch exits.
- Any backend/UI disagreement in `directional_exit_levels` means no directional TP/SL should be rendered as executable.

## NO TRADE / BLOCKED checklist

Treat the recommendation as NO TRADE when any of the following appears:

- critical/blocking preflight status;
- INVALID_MARKET_REFERENCE_PRICE;
- stale publication-chain or stale market data;
- current ticker outside range or kill-switch;
- missing Bybit metadata, tickSize, qtyStep, minNotional, leverageFilter, or non-Trading instrument status;
- funding rate/interval unavailable or adverse enough to destroy net edge;
- fractional/malformed market timestamp, funding interval, label horizon, or funding event schedule; such values must remain unknown and must never be rounded into an executable assumption;
- empty/corrupted payload; Complete `params.trade_plan` exists; no empty/corrupted payload. If this statement is false, do not launch;
- missing OK LLM gate when the reviewer is configured as a gate;
- unknown or conflicting same-symbol direction in one-way mode.

## Required operator payload

A complete `params.trade_plan` must include:

- reference_price;
- levels.range.lower / levels.range.upper;
- levels.kill_switch.lower / levels.kill_switch.upper;
- levels.grid_step.step_abs;
- levels.tp_per_leg.abs or pct;
- grid_count and arithmetic grid model;
- explicit leverage and isolated margin mode;
- sizing/economics sufficient for qtyStep, minNotional, margin, and worst-case exposure validation.

## Practical sequence

1. Confirm status is recommended/actionable and not blocked.
2. Check current price, publication-chain TTL, Bybit metadata, and funding diagnostics.
3. Copy only a complete trade plan into Bybit Futures Grid.
4. Re-check leverage 3-5x, margin, estimated worst-case exposure, minNotional, and liquidation buffer.
5. Do not override a blocking guard manually.

Runtime guards are authoritative: risk status, Bybit metadata, live ticker, funding snapshot, publication-chain TTL, minNotional/qtyStep/minQty, and LLM gate if enabled.
