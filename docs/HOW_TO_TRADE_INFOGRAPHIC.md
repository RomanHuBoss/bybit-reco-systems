# How to trade - operator quick reference

This repository is a recommendation/audit service, not OMS/EMS. It does not manage live order lifecycle, open orders, fills, partial fills, or exchange reconciliation. The executable truth must remain in an external Bybit execution/reconciliation layer.

## Current shipped risk profile

- `min_leverage=3`, `max_leverage=5`.
- 3-5x is the baseline actionable leverage interval for this revision.
- One running bot per account/symbol by default.
- Linear USDT Futures Grid only; non-linear venue, spot, options, inverse contracts, unsupported symbols, and non-USDT pairs are blocked.

## Signal durability and recommendation identity

- A `futures_grid` row is actionable only after two different, forward-moving closed evidence snapshots pass the gates independently.
- Re-running the recommender on the same closed candle is not a second confirmation; the row remains `pending`.
- Refreshing an open card keeps the exact selected immutable `rec_id`. Newer `no_trade`, blocked, pending, or direction-flip rows belong to the history timeline and must not silently replace it.
- Raw confidence is heuristic launch quality, not a probability of profit. Even calibrated confidence targets proxy outcomes and does not prove live edge.


## Independent range-edge check

- Low trend is not a trade signal. A driftless random walk can also have a flat MA slope and still lose after costs.
- Actionable grid requires independent anti-persistence evidence on at least three closed timeframes and aggregate `mean_reversion_score >= 0.55`.
- `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` is hard `blocked`; `MEAN_REVERSION_EDGE_UNCONFIRMED` is strategy `no_trade`. Both mean do not launch.
- The UI field formerly perceived as R/R is a heuristic **capture/risk proxy**, not an actual profit/loss ratio.

## Directional TP/SL model

- Long: TP above entry/reference, SL below entry/reference.
- Short: TP below entry/reference, SL above entry/reference.
- Neutral grid: no single directional TP; lower and upper outer levels are kill-switch exits.
- All initial NEUTRAL Buy/Sell orders are opening orders and belong in committed notional; one-way net position remains capped by the larger side.
- Any backend/UI disagreement in `directional_exit_levels` means no directional TP/SL should be rendered as executable.

## Temporal evidence integrity

- Do not treat a ticker as fresh unless the exchange event timestamp is valid.
- A shifted/malformed candle, a missing next-minute entry candle, any gap inside the outcome horizon, or a missing exact exit candle means no proxy label.
- An already-open candle before publication is not a tradeable entry. Conflicting persisted grid/funding aliases are skipped, never collapsed into a different bot or a zero-return loss.
- Calibration excludes labels with missing, malformed or future `label_available_ts`; an unfitted calibrator remains a diagnostic state, not permission to weaken deterministic gates.
- Current label contract is `grid_label_v15`: entry remains the first exact 1m open strictly after publication; N intervals create N+1 prices but exactly N initial orders, with one idle pivot/bridge level; directional inventory and neutral full initial-order commitment are derived from those actual orders; kill-switch remains terminal.
- Same-level directional lots are quantity-aware: an initial TP and an adjacent replacement TP at one price must both remain in the ledger, fees and funding state.
- Missing/inside-range kill-switch is unlabelable. For any candle with material high and low excursions, both O-H-L-C and O-L-H-C paths must produce the same ledger/stop/PnL state; otherwise no proxy label is stored.
- A close-open or horizon gap beyond the kill-switch is also unlabelable; never assume the skipped boundary was an executable stop price.
- Outcome headline is actionable-only; all-roots and shadow no_trade metrics are separate research/control cohorts.

## NO TRADE / BLOCKED checklist

Treat the recommendation as NO TRADE when any of the following appears:

- critical/blocking preflight status;
- `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` or `MEAN_REVERSION_EDGE_UNCONFIRMED`; low trend alone is not a valid range edge;
- INVALID_MARKET_REFERENCE_PRICE;
- stale publication-chain or stale market data;
- current ticker outside range or kill-switch;
- conservative loss to the adverse kill-switch exceeds the remaining daily max-DD budget (`DAILY_LOSS_BUDGET_EXCEEDED`);
- live best bid/ask missing or invalid, spread above 14 bps, recomputed net edge below 2 bps, or gross edge not covering live execution cost by more than 1.10x;
- missing Bybit metadata, tickSize, qtyStep, minNotional, leverageFilter, or non-Trading instrument status;
- funding rate/interval unavailable or adverse enough to destroy net edge;
- fractional/malformed market timestamp, funding interval, label horizon, or funding event schedule; such values must remain unknown and must never be rounded into an executable assumption;
- empty/corrupted payload; Complete `params.trade_plan` exists; no empty/corrupted payload. If this statement is false, do not launch;
- missing OK LLM gate when the reviewer is configured as a gate;
- unknown or conflicting same-symbol direction in one-way mode.
- exact execution evidence has triggered `LIVE_VALIDATION_*`: five consecutive losses for the same symbol/direction, or negative total+median PnL with sub-50% positive rate after the predefined direction/symbol/portfolio sample threshold for the same explicit model version.

## Required operator payload

A complete `params.trade_plan` must include:

- reference_price;
- levels.range.lower / levels.range.upper;
- levels.kill_switch.lower / levels.kill_switch.upper;
- levels.grid_step.step_abs;
- levels.tp_per_leg.abs or pct; for arithmetic grid it must match the adjacent grid interval, not a 70% haircut;
- grid_count and arithmetic grid model;
- explicit leverage and isolated margin mode;
- sizing/economics sufficient for qtyStep, minNotional, margin, and worst-case exposure validation; keep initial-order commitment separate from maximum one-way position. `grid_count` is intervals: N+1 prices exist, one pivot/bridge is idle and initial active orders remain N; neutral capital sums all N initial Buy/Sell opening orders, while max position uses only the larger side.

## Practical sequence

1. Confirm status is recommended/actionable and not blocked.
2. Check current price, best bid/ask spread, recomputed live edge, publication-chain TTL, Bybit metadata, and funding diagnostics.
3. Copy only a complete trade plan into Bybit Futures Grid.
4. Re-check leverage 3-5x, margin, estimated worst-case exposure, minNotional, and liquidation buffer. Live preflight may round qty only downward to the actual qtyStep; if minQty/minNotional is then unmet, keep the recommendation blocked instead of increasing the position.
5. Do not override a blocking guard manually.

Runtime guards are authoritative: risk status, Bybit metadata, live ticker/bid-ask economics, funding snapshot, publication-chain TTL, minNotional/qtyStep/minQty, and LLM gate if enabled.


## After external execution

- Send each Bybit fill separately with immutable `execId`, `orderId`, actual price/qty and the originating `rec_id` through the bot link.
- Record funding as a separate signed transaction-log event.
- Capture a timestamped pre-submit/decision benchmark; do not use `orderPrice` as a substitute for slippage measurement.
- Realised net is `execPnl + funding - fee`. Slippage is an execution-quality diagnostic already reflected in fill-based PnL and is not deducted twice.
- Never mix exact evidence with legacy `/trades` for the same bot.
- Evidence export contains sensitive exchange identifiers and requires `ADMIN_API_KEY`.
- Descriptive live-evidence statistics are not proof of positive expectancy.
- In proxy outcome diagnostics, a directional per-leg TP touch never proves whole-grid profit; success requires valid mode activity, positive liquidation-equivalent net proxy and an intact kill-switch. There is no hidden 5 bps win threshold.
- Nevertheless, persistent negative exact evidence is an execution stop condition; do not bypass the `LIVE_VALIDATION_*` blocker.
## No recommendations / calibration

- No `recommended/active` rows can be the correct result when current evidence is weak or historical proxy returns are negative.
- An unfitted calibrator does not itself block publication; raw confidence is shown until fit.
- Eligible `no_trade` candidates may be labeled later as `shadow_no_trade` for research. They are not live trades and cannot be executed.
- The outcomes journal separates shadow roots from actionable roots and must never call OHLCV proxy labels real fills.

