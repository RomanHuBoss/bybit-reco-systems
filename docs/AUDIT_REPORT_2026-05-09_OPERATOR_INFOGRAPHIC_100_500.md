# Operator infographic update — 100–500 USDT accounts

## Scope

Updated the root `how_to_trade.png` infographic so it matches the current product state:

- only Bybit Linear USDT Perpetual;
- only `futures_grid` recommendations;
- exact USDT perpetual symbols such as `BTCUSDT` / `ETHUSDT`;
- isolated margin;
- arithmetic grid;
- fail-closed execution when validation blocks appear;
- net economics after fees, spread, slippage and funding;
- liquidation buffer and kill-switch discipline.

## Why the old infographic was unsafe for the current request

The previous version was framed around `500 USDT • 10x`, which is too aggressive as a default operating rule for accounts in the `100–500 USDT` range. For small accounts, a large share of apparent opportunities should be rejected because exchange constraints, full trading costs, funding uncertainty, liquidation buffer or margin limits can make the grid uneconomic.

## New operator defaults

The infographic now presents small-account defaults:

| Balance | Margin per bot | Daily stop | Base leverage |
|---:|---:|---:|---:|
| 100–199 USDT | 10–20 USDT | 3–5 USDT | 1–2x |
| 200–349 USDT | 20–35 USDT | 5–10 USDT | 1–3x |
| 350–500 USDT | 35–60 USDT | 10–15 USDT | 1–3x |

Additional guardrails:

- one bot for the whole account;
- 10–15% of deposit as working margin;
- 75–85% reserve outside the position;
- 10x is not a baseline mode;
- any critical/blocking validation means no trade;
- failure of `minNotional`, `qtyStep` or `minQty` is a valid no-trade outcome, especially for ~100 USDT accounts.

## Residual note

This is an operator infographic, not executable logic. Runtime enforcement still belongs to the backend risk limits, Bybit preflight validation, live price checks, funding checks, liquidation buffer checks and execution guards.
