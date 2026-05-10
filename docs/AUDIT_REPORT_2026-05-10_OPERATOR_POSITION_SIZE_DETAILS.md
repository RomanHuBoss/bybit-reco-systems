# AUDIT REPORT — Operator Details Position Size

Date: 2026-05-10
Scope: Bybit Linear USDT Perpetual Futures Grid recommendations only.

## Issue

The compact Details panel showed side, range, entry price, grids, leverage, margin, TP/SL and LLM recommendation, but did not expose the recommended total position size for the displayed margin.

For a manual operator this left an important gap: margin alone does not answer how much notional exposure the bot is expected to open under the recommended leverage.

## Change

The Details panel now includes `Размер позиции` on the main operator level.

The value is resolved from the recommendation payload in this order:

1. `estimated_max_position_notional_usdt`
2. `max_position_notional_usdt`
3. `estimated_total_order_notional_usdt`
4. `total_order_notional_usdt`
5. `position_notional_usdt`
6. `notional_usdt`
7. fallback: `marginRequired * leverage`

When a reference price is available, the UI also shows the approximate base-asset quantity next to the USDT notional.

## Operator meaning

The primary panel is now sufficient for manual creation of a Bybit Futures Grid bot:

- side: Long / Short;
- recommended position size;
- required margin;
- entry range and reference price;
- grid count;
- leverage;
- TP/SL;
- LLM recommendation and probability.

Per-order sizing and other diagnostics remain outside the main panel to avoid turning Details into a technical dump.
