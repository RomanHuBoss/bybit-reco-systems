# AUDIT REPORT — Operator Details Bot Lifetime

Date: 2026-05-10
Scope: Bybit Linear USDT Perpetual Futures Grid recommendations only.

## Issue

The minimal Details panel exposed side, recommended position size, margin, range, grid count, leverage, TP/SL and LLM recommendation, but did not show the intended bot lifetime / holding window.

For manual Futures Grid operation this is not enough: the operator needs to know not only where and with what size to launch the bot, but also how long the recommendation expects the bot to remain valid after launch before manual stop or re-evaluation.

## Change

The Details panel now includes `Время работы` on the main operator level.

The value is resolved from `params.trade_plan.expected_horizon` first and displayed as a compact operator window, for example:

- `6 ч — 48 ч`
- `до 12 ч`
- `от 6 ч`

Fallback fields such as `label_horizon_hours`, `bot_lifetime_hours` and related runtime-hour aliases are supported so legacy or manually injected payloads still render a useful value.

## Operator meaning

`Время работы` means the planned post-launch holding window for the grid bot. It is intentionally separate from recommendation `ttl_sec`, which only describes how long the recommendation row remains fresh before it expires.

The main Details panel now answers the complete manual-launch checklist:

- Long / Short;
- recommended position size;
- bot lifetime / holding window;
- required margin;
- entry range and reference price;
- grid count;
- leverage;
- TP/SL;
- LLM recommendation and probability.
