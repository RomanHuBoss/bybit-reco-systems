## v1.0.61 operator decision metrics

- Main table: **Plan RR**, **Empirical expectancy**, **Risk buffer**, direction and status. Raw rank/confidence/direction-confidence proxies are not primary operator columns.
- Plan RR = projected net result of the concrete grid plan / worst-side kill-switch price-and-exit loss. Recurring pair fees are counted once; spread/slippage and adverse funding are separate horizon costs.
- Empirical expectancy = exact-current-policy matured proxy return with a two-sided Student-t confidence interval. Detail view also shows expected shortfall and mean/tail ratio.
- A confidence interval crossing zero means uncertain. Insufficient exact-policy evidence means unavailable, not zero.
- Legacy `expected_rr` is a backend/internal heuristic capture score and is not rendered anywhere in the operator UI.
- Plan RR is not a probability; empirical proxy outcomes are not exchange-attested live PnL. Neither overrides NO TRADE/BLOCKED gates.
- Application `1.0.61`; model/outcome/policy identities are unchanged, so current-policy evidence is not reset.

## v1.0.58 outcome-scope and readiness rule

- Application `1.0.58`; model `bybit-taxonomy-v8-policy-conditioned-censor-aware`; outcome target `grid_label_v26`.
- Outcomes headline = verified `current_policy` only. `archive` is shown separately and never proves current edge.
- `CALIB_MIN_SAMPLES=80` is a monetary floor, not full readiness. With `REQUIRE_CONF_GATE=1`, probability needs at least 300 exact-policy labels plus accepted purged OOF and terminal holdout skill.
- Any censored/unresolved/invalid matured root remains `NO TRADE`; this is a documented liveness risk pending a conservative bounded-censor model.
- No order execution or profitability claim is introduced.

## v1.0.57 evidence-contract rule

- Application `1.0.57`; model `bybit-taxonomy-v8-policy-conditioned-censor-aware`; outcome target `grid_label_v26`; bot/global v19; direction v14.
- Compare the full policy fingerprint, not only model version. Its digest must recompute from the persisted contract; different thresholds, universe, LLM gate or risk limits mean different evidence.
- Check `policy_matured_total = labeled + censored + unresolved`. Any censored/unresolved/invalid or vanished cache support means **NO TRADE**.
- Probability requires purged aggregate and terminal future-block skill over score-only/null baselines. The terminal block is never refit into the active model. Score-only Platt is not an inference fallback.
- Direction Platt is audit-only; the decision feature remains raw until a separate chronological skill gate exists.
- With `REQUIRE_CONF_GATE=1`, raw confidence is audit-only and cannot unlock publication.
- Live positive PnL requires stopped + locally flat + complete matching external Bybit reconciliation. Before it, gains get zero credit and losses remain conservative.
- This repository remains recommendation/audit-only: no order create/amend/cancel and no claim of live edge.

## v1.0.56 calibration-lineage rule

- Application `1.0.56`; outcome target remains `grid_label_v26`.
- Recommendation lineage: `bybit-taxonomy-v7-mr-floor-temporal-cohorts`.
- Bot/global calibrators v18; direction calibrator v13.
- A non-empty historical archive is not current evidence. Operator progress is based only on current-model feature-eligible rows and independent matured cohorts.

## v1.0.55 mean-reversion and temporal-evidence rule

- Current contracts: application `1.0.55`, outcome `grid_label_v26`, bot/global calibrators v17, direction calibrator v12.
- `MEAN_REVERSION_MIN_SCORE=0.25` is a candidate-screen default, not a promise of profit and not proof of negative expectancy below the floor.
- `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` is hard blocked; `MEAN_REVERSION_EDGE_UNCONFIRMED` is strategy `NO TRADE`.
- Same-timestamp symbols count as one decision cohort. Temporal diagnostics use a maximal pairwise non-overlapping cohort set, so overlap chains cannot freeze `time_clusters` at one forever.
- Monetary lower bounds, purged OOF activation, economics, risk and operator-profile gates remain mandatory.

## v1.0.54 purged OOF confidence rule

- `bot_logreg` is permitted only when `purged_oof_status=sufficient`.
- Check `purged_oof_samples >= purged_oof_required_samples`; the default requirement follows `CALIB_MIN_SAMPLES`.
- `insufficient` or `error` means feature coefficients were withheld. Score-only Platt may remain available, but it is not feature-model validation.
- Raw or Platt confidence never overrides monetary expectancy, temporal independence, `blocked`, or `no_trade`.
- Current contracts: application `1.0.54`, outcome `grid_label_v26`, bot/global calibrators v16, direction calibrator v12.

## v1.0.53 horizon and liquidation volume rule

- Never carry a candle's liquidity budget into the next minute.
- Gap fills at the exact horizon open use the boundary candle's own volume.
- Terminal residual close shares that boundary-minute budget.
- Kill-switch close shares the breach candle budget already consumed by grid fills.
- Insufficient capacity means **NO EVIDENCE**, not a partial win/loss.
- The 12h strategy horizon remains 12h, but the label becomes available one minute later after boundary volume is complete.
- Current evidence contract: `grid_label_v26`; bot/global calibration v15; direction calibration v12.

## v1.0.52 kill-switch proxy rule

- Historical-only system: this is not a live stop-order simulator.
- An intrabar kill-switch breach no longer assumes a perfect fill at the trigger.
- Residual SHORT + upper breach: use observed candle high as the conservative liquidation bound.
- Residual LONG + lower breach: use observed candle low as the conservative liquidation bound.
- Favorable continuation is not credited; gaps that skip the trigger remain unlabelable.
- Current evidence contract: `grid_label_v25`; bot/global calibration v14; direction calibration v11.

## Simulation boundary after v1.0.51

This service models historical outcomes only. It does **not** submit orders, attest exchange fills, or decide whether a real order is executable at runtime. Missing current Bybit metadata is not a recommendation blocker. Read `reasons.simulation_scope`: `historical_proxy_only`, `runtime_order_submission=false`, `runtime_execution_validation=not_performed`.

Treat every recommendation as paper/shadow evidence. Strict trade-through, candle-volume capacity and delayed replacement activation are conservative model assumptions, not proof of queue execution. An optional explicit preflight may display current tick/qty/minimum-order diagnostics, but it does not change the recommendation, historical outcome or calibration.

## Mandatory replacement-timing check after v1.0.50

Do not treat a parent fill and its replacement fill inside the same one-minute candle as proven execution. OHLCV does not contain the parent fill time or the replacement submission/acknowledgement time. `intrabar_replacement_fill_timing_unobservable` means **NO EVIDENCE / NO TRADE**, not a loss and not a completed profitable cycle. A replacement becomes proxy-eligible only from the next candle; exact exchange fills remain authoritative.

## Mandatory proxy-volume check after v1.0.49

A candle crossing a limit level does not prove that the whole order was filled. Current `grid_label_v22` outcomes require the simulated initial/fill quantity to fit within the candle's total Bybit base-quantity volume. `insufficient_candle_volume_for_full_fill` or `insufficient_candle_volume_for_initial_inventory` means **NO EVIDENCE / NO TRADE**; do not convert the missing label into a loss or override it with confidence. Even sufficient candle volume does not prove queue priority, level liquidity or partial fills - exact exchange reconciliation remains authoritative.

## Historical v1.0.48 exchange-evidence rule (superseded by v1.0.51)

The mandatory current-metadata gate below is retained only as release history. It must not be used with v1.0.51+.

In v1.0.48-v1.0.50 the system required current-filter normalization and an exchange snapshot. Do not apply that rule to v1.0.51+: missing current metadata is not a model blocker, and historical outcomes use persisted simulation geometry.

For shadow statistics, exact candle touch is not a completed limit fill. Proxy Buy requires trade below the order level; proxy Sell requires trade above. Even trade-through remains a proxy and does not prove queue priority or partial-fill volume.

## Funding receipt rule after v1.0.46

Do not launch because historical funding paid the modeled side. Proxy `ret`, win rate and monetary expectancy exclude positive funding receipts and charge adverse payments. Signed funding belongs to exact realised PnL diagnostics, not to durable grid alpha. After upgrade, wait for new `grid_label_v19` outcomes; old receipt-inflated calibration is reset.

## Temporal independence check after v1.0.45

Do not interpret many symbols in one market window as many independent tests. Before any launch, `confidence_model` must show enough `time_clusters` (default at least 20 for `CALIB_MIN_SAMPLES=80`) and a strictly positive `time_cluster_lower_bound`, in addition to the positive row-level lower bound. `time_clusters` below the minimum, missing cluster diagnostics or a non-positive cluster lower bound means shadow `NO TRADE`. Correlated symbols cannot be used to override this gate.

## Terminal exact-evidence check after v1.0.44

A stopped audit bot is **not** automatically a completed result. Before treating realised PnL as exact validation evidence, verify `total_pnl_finalized=true`, `position_flat=true`, and `net_position_qty≈0`. Rows with `residual_position`, `execution_ledger_incomplete`, `no_execution_events`, or `bot_not_stopped` remain visible but must not enter `LIVE_VALIDATION_*` statistics. Every opening and closing fill plus signed funding must be delivered by the external read-only reconciliation adapter.

## Обязательная проверка денежного evidence после v1.0.43

Не запускайте Futures Grid только по `score`, raw confidence, win rate или положительному среднему. В `confidence_model` должны одновременно выполняться: `expectancy_status=positive`, достаточный `weighted_effective_return_samples` и `weighted_mean_return_lower_bound > 0`. Статусы `unknown`, `insufficient`, `uncertain` означают shadow `NO TRADE` с кодом `PROXY_MONETARY_EXPECTANCY_UNPROVEN`; `negative` означает подтверждённый monetary veto. Даже положительная нижняя граница не является гарантией прибыли и не отменяет risk/preflight checks.

## Проверка срока статистического evidence после v1.0.42

`calibrated confidence` допустим только пока положительный calibrator воспроизводится из текущей retained outcome-выборки. После hourly refresh недостаточная выборка переводит модель в `unfitted/raw`; старые коэффициенты не являются основанием для запуска. Отрицательный monetary expectancy остаётся `NO_TRADE`, даже если новые данные временно отсутствуют. В UI всегда проверяйте `confidence_model.source`, `fitted`, `n_samples` и `expectancy_status`.

## Статистика shadow/no-trade после v1.0.41

Повторяющиеся `NO TRADE` строки в истории не означают множество независимых тестов стратегии. В пределах одного label horizon система сохраняет их для аудита, но outcome и calibration используют только первый shadow root. После обновления calibrator может временно показывать недостаточную выборку — это ожидаемое следствие удаления ложной псевдорепликации.

## v1.0.40 monetary-expectancy safety update

A high win rate is not evidence of profit. If the matured bot-specific proxy cohort has non-positive recency-weighted mean return after the matured-return sample floor, the system must show `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE` and `NO_TRADE`. Do not bypass this because calibrated confidence, median outcome, or most individual labels look positive. The proxy gate is conservative; real fills and exact net PnL remain authoritative.

## v1.0.39 operator safety update

After 8 independent stopped bots for one direction, 12 for one symbol, or 20 portfolio-wide, a negative cumulative exact net PnL blocks a new launch even when most bots were profitable. This catches the grid tail-loss pattern in which one large range-break loss outweighs many small gains. Do not treat a high win rate or a positive median as permission to bypass `LIVE_VALIDATION_*`.

## Settled funding labels - v1.0.37 / grid_label_v18

- Approval uses forecast funding conservatively; it never credits a possible receipt as guaranteed edge.
- Historical statistics use actual Bybit funding settlements, not the earlier ticker forecast.
- Legacy `grid_label_v18` included both settled payments and receipts with the LONG/SHORT sign. This rule is superseded by `grid_label_v19`: payments remain costs, receipts are diagnostic-only for proxy validation.
- Missing settlement data blocks a non-flat historical label.
- The settlement rate is exact; modeled inventory/price remain OHLCV proxy limitations.

## Grid cost layers - v1.0.36 / grid_label_v17

- Per completed grid pair: full adjacent interval minus the two resting-fill fees.
- Do not subtract bid/ask spread, slippage or full-horizon funding from every pair.
- Spread/slippage are market setup/terminal friction; funding is inventory-time Total P&L.
- Live spread remains a liquidity gate; funding remains a separate fail-closed schedule/inventory gate.
- New outcomes use `grid_label_v17`; prior proxy labels/calibrators are reset, exact evidence is retained.

## Cross-margin Grid Bot contract - v1.0.36 / grid_label_v17

- Required mode: unified account, `margin_mode=cross`, `position_mode=one_way`.
- Do not launch a payload marked `isolated`; it uses the wrong risk semantics for Bybit Futures Grid Bot.
- Use `cross_margin_stress_buffer_pct` at the external kill-switch, not a standalone liquidation price.
- The stress includes committed grid capital, leverage, adverse inventory PnL, execution costs and maintenance reserve.
- Funding receipt and hypothetical grid profit do not improve the safety buffer.
- The external executor must still verify wallet equity, other positions/orders, risk tier and live Bybit state.

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
- Grid screening requires independent anti-persistence evidence on at least three closed timeframes and aggregate `mean_reversion_score >= MEAN_REVERSION_MIN_SCORE` (default `0.25`). Passing this screen does not establish positive expectancy.
- `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` is hard `blocked`; `MEAN_REVERSION_EDGE_UNCONFIRMED` is strategy `no_trade`. Both mean do not launch.
- The heuristic capture score is hidden from operator R/R. Use separate Plan RR and exact-policy empirical expectancy/CI; neither proves live edge.

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
- Calibration excludes labels with missing, malformed or future `label_available_ts`; an unfitted or unproven bot-specific calibrator requires shadow `no_trade`; raw confidence is audit-only and cannot make the strategy actionable.
- Current label contract is `grid_label_v25`: entry remains the first exact 1m open strictly after publication; N intervals create N+1 prices but exactly N initial orders, with one idle pivot/bridge level; directional inventory and neutral full initial-order commitment are derived from those actual orders; kill-switch remains terminal; adverse settled funding reduces proxy `ret`, while positive receipts remain diagnostic-only and cannot create edge.
- Same-level directional lots are quantity-aware: an initial TP and an adjacent replacement TP at one price must both remain in the ledger, fees and funding state.
- Missing/inside-range kill-switch is unlabelable. For any candle with material high and low excursions, both O-H-L-C and O-L-H-C paths must produce the same ledger/stop/PnL state; otherwise no proxy label is stored.
- A close-open or horizon gap beyond the kill-switch is also unlabelable; never assume the skipped boundary was an executable stop price.
- Outcome headline uses verified `current_policy` only; `current_model` and historical `archive` are separate scopes. Within a scope, actionable and shadow no_trade metrics remain separate research/control cohorts.

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
- exact execution evidence has triggered `LIVE_VALIDATION_*`: five consecutive losses for the same symbol/direction, or negative cumulative exact net PnL after the predefined direction/symbol/portfolio sample threshold for the same explicit model version. Median and win rate are diagnostics only.

## Required operator payload

A complete `params.trade_plan` must include:

- reference_price;
- levels.range.lower / levels.range.upper;
- levels.kill_switch.lower / levels.kill_switch.upper;
- levels.grid_step.step_abs;
- levels.tp_per_leg.abs or pct; for arithmetic grid it must match the adjacent grid interval, not a 70% haircut;
- grid_count and arithmetic grid model;
- explicit leverage, cross margin and one-way position mode;
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
