# how_to_trade.png — operator source text

This file is the text source-of-truth for the root infographic `how_to_trade.png`. Keep it synchronized with `README.md`, `docs/TRADING_LOGIC.md`, `.env.example`, `app/settings.py` and the operator DOCX/PDF instruction.

## Title

Регламент оператора: счёт 100-500 USDT

Subtitle:
Bybit Linear USDT Perpetual • futures_grid only • recommendation/audit service, not OMS/EMS

Warning banner:
Малый счёт: сначала защита капитала и fail-closed preflight. Проект не отправляет ордера сам; оператор запускает вручную только после OK.

## 1. Базовый режим счёта

- Капитал: 100-500 USDT.
- Ботов: 1 running bot на счёт и symbol/publication-chain.
- Маржа/бот: 10-15% депозита как верхний ориентир.
- Резерв: минимум 75-85% вне позиции.
- Дневной стоп: 3-5% депозита или жёсткий USDT-лимит.

Bottom note:
Не использовать весь депозит как маржу.

## 2. Что должно быть в сигнале

All of these must be true:

- `bot_type=futures_grid`; `venue/category=linear`.
- Complete `params.trade_plan` exists; no empty/corrupted payload.
- `symbol` exact USDT perpetual, for example `BTCUSDT`, not `BTC/USDT`.
- `settleCoin/quoteCoin=USDT`; `contractType=LinearPerpetual`.
- `account_mode=unified`; `margin_mode=isolated`.
- `grid_type=arithmetic`; `status=recommended` or effective `active`.
- Live price is inside grid range and outside kill-switch.
- `price_input_valid=true`; no synthetic fallback price.

Bottom note:
Любой blocking/critical статус = NO TRADE.

## 3. Плечо и размер позиции

Current shipped risk profile:

| Balance | Margin/bot guide | Daily stop guide | Runtime leverage |
|---:|---:|---:|---:|
| 100-199 USDT | 10-20 USDT | 3-5 USDT | 3-5x after OK |
| 200-349 USDT | 20-35 USDT | 5-10 USDT | 3-5x after OK |
| 350-500 USDT | 35-60 USDT | 10-15 USDT | 3-5x after OK |

Rules:

- Default runtime limits: `min_leverage=3`, `max_leverage=5`.
- 3-5x is the baseline actionable leverage interval of this revision.
- If `max_leverage < 5` or `min_leverage < 3`, it is a stricter safety cap; expect more `no_trade` / `blocked` outcomes.
- 10x is not default for the small-account profile.

## 4. Качество сигнала

All of these must pass:

- Score / confidence / expected RR pass gates.
- Net profit per grid is above fees + spread + slippage + adverse funding.
- Funding rate and interval are known or conservatively blocked.
- Regime is range / mean-reversion; no market shock.
- Liquidity and spread fit small order size.
- Liquidation buffer remains outside adverse boundary / kill-switch.
- `grid_count`, price levels and TP hints pass `tickSize`; qty passes `qtyStep`, `minQty`, `minNotional`.

Bottom note:
Funding receipt is diagnostic only; it must not create the edge.

## 5. Long/short TP/SL

Canonical model:

- Long: TP above entry/reference, SL below entry/reference, profit on price rise.
- Short: TP below entry/reference, SL above entry/reference, profit on price fall.
- Neutral grid: no directional TP/SL; use grid range and kill-switch levels.
- Protective TP/SL orders must be reduce-only / close-only and must not increase exposure.

Bottom note:
If UI and backend disagree on TP/SL geometry, do not trade.

## 6. Когда пропускать

Skip immediately if any of these is true:

- `INVALID_MARKET_REFERENCE_PRICE`, stale ticker/candles, or expired publication-chain.
- Current price left range or crossed kill-switch.
- Unknown funding rate/interval where funding is material.
- Net profit per grid <= all trading costs.
- Bybit metadata missing, wrong symbol/category, `status != Trading`.
- qty/minQty/qtyStep/minNotional not executable at current range.
- LLM reviewer is enabled but no fresh `ok` verdict exists.

Bottom note:
Manual override must not be used to bypass blockers.

## 7. Когда выключать / закрывать

Close / stop the bot when:

- Price crosses kill-switch or leaves the allowed range.
- Market switches to clear trend, shock, flash-crash or squeeze.
- Publication idea is stale, TTL expired, or levels are no longer current.
- Daily stop or realized-loss cooldown is triggered.
- Exchange state, local state, fills or protection cannot be reconciled.

Bottom note:
Recalculate before any restart.

## 8. Дневная дисциплина

- 1 bot per account.
- 3-5x leverage is allowed only after all guards pass.
- 10-15% of deposit maximum as working margin.
- 75%+ reserve outside the position.
- No DCA, martingale, spot, inverse, options or unsupported bot types.
- Keep a decision journal: entry reason, rejection reason and outcome.
- Remote deployment requires explicit `ADMIN_API_KEY`.

Bottom summary:
1 bot • 3-3-5x gated • 10-15% margin cap • net profit > costs • NO TRADE при блоках
