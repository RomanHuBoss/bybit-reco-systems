## Диагностика публикации не меняет торговый допуск — v1.0.69

Outcome-root является identity для labeling, но не фильтром операторской сводки. Строка, повторно использующая прежний root, остаётся частью текущей публикации и должна учитываться в `no_trade`/`blocked` статистике. Изменение не снижает evidence thresholds, не включает LLM в deterministic gate и не делает рекомендацию actionable.

## Разделение технической готовности и торгового допуска — v1.0.68

`operator_readiness.runtime_healthy` отвечает только за инфраструктурную готовность: применена ли outcome-схема, завершён ли materialization, живы ли обязательные фоновые контуры и не находится ли outcome-worker в `stalled/error`. `operator_readiness.trading_actionable` отвечает только на наличие `recommended/active` в последней публикации.

Состояние `healthy_not_actionable` является штатным fail-closed результатом. В частности, `PROXY_MONETARY_EXPECTANCY_UNPROVEN`, `CALIBRATED_CONFIDENCE_UNAVAILABLE` и родственные evidence-коды могут удерживать все идеи в `no_trade`, пока exact-policy outcomes и калибровка не удовлетворяют действующему контракту. Версия 1.0.68 не снижает минимальное число наблюдений, не кредитует положительный funding, не меняет PnL/grid math и не позволяет LLM отменять deterministic gate.

В UI `no_trade` визуально отделён от hard block: жёлтый означает «идея сейчас не допущена по качеству/экономике/доказательности», красный — «жёсткая блокировка по риску, данным или контракту». Оба статуса остаются неисполняемыми.

## Outcome observability и fail-closed причины — v1.0.67

Изменение runtime не меняет торговую математику или `grid_label_v26`. Outcome создаётся только при прежнем полном контракте геометрии, объёма свечей, funding и временной наблюдаемости. Неопределимый intrabar-порядок, пересечение обеих аварийных границ в одной свече, конфликт активных/ожидающих заявок, невалидная строка OHLCV и другие невозможные для восстановления состояния теперь получают отдельные machine-readable reasons. Они остаются `censored` или `waiting` по прежней fail-closed семантике и не обучают calibrator как вымышленный win/loss.

## Операторская терминология v1.0.64

Торговая семантика не изменена; изменён только язык представления. **Покупка (рост)** соответствует `long`, **Продажа (снижение)** — `short`, **Нейтральная сетка** — `neutral`. Цель прибыли, ограничение убытка и аварийная граница выхода отображаются по канонической directional-модели. **RR плана** остаётся отношением расчётного чистого результата плана к стресс-убытку на аварийной границе; **доходность по наблюдениям** остаётся статистикой созревших наблюдений текущего набора правил. Подсказки прямо предупреждают, что оба показателя не являются вероятностью прибыли и не отменяют итоговое решение.

## Operator decision hint contract (v1.0.63)

The primary table exposes only symbol, direction, Plan RR, empirical expectancy and the final decision. The decision label carries one bounded, human-readable Russian hint on hover/focus. Raw model messages, numeric thresholds, diagnostic codes and full gate traces remain in Details. An unmapped internal code must use a status-level fallback such as `Не пройдены условия запуска`; it must never render the raw diagnostic payload in the table. This presentation change does not alter the underlying status or any deterministic gate.

## Outcome/LLM contract and operator decision surface (v1.0.62)

When LLM review is enabled, actionable recommendation roots require a completed eligible LLM verdict before outcome labeling. An explicit `no_trade` root may bypass that prerequisite only when `outcome_policy.eligible=true`, `policy_evaluation_eligible=true`, `sample_role=shadow_no_trade`, and deterministic `risk_checks.passed=true` with no blocks. This exception cannot make a recommendation actionable and exists only to prevent research/calibration bootstrap deadlock.

The primary table is not a diagnostic dashboard. It contains only symbol, direction, Plan RR, empirical expectancy, the operator decision and one primary reason. Confidence values, risk buffer and all underlying economics remain available in Details.

## Operator reward/risk metrics (v1.0.61)

The operator contract separates plan geometry from historical evidence.

### Plan RR

`reasons.operator_metrics.plan_rr` is calculated only from a complete generated plan:

`projected_pairs = estimated_active_orders × fill_efficiency`

`projected_net_reward = net_profit_usdt_per_completed_pair × projected_pairs - one_time_market_friction - adverse_funding`

`kill_switch_loss = qty_per_order × (worst_side_gross_loss_per_qty + worst_side_terminal_execution_cost_per_qty)`

`Plan RR = max(projected_net_reward, 0) / kill_switch_loss`

Recurring pair fees are already included in `net_profit_usdt` and are not deducted twice. `one_time_market_friction_bps` is the separate spread/slippage layer. Positive funding receipts are never credited. Maintenance margin reserve remains a stress/equity requirement and is not called realised loss. Invalid or incomplete numeric inputs produce `status=unavailable`. Plan RR is scenario analysis, not a probability forecast and not an execution attestation.

### Empirical expectancy and tail ratio

`reasons.operator_metrics.empirical_expectancy` uses retained matured outcomes from the exact current policy fingerprint. The mean and standard error prefer non-overlapping temporal cohorts; when that complete diagnostic is unavailable, the existing recency-weighted retained-outcome diagnostic is used and the basis is disclosed. A two-sided Student-t confidence interval is reported. Expected shortfall remains the downside-tail statistic. `empirical_rr` is explicitly a mean-to-tail ratio (`positive mean / abs(negative expected shortfall)`), not trade geometry. It is unavailable when the mean is non-positive, the tail is not negative, or evidence is incomplete.

The old `expected_rr` remains a bounded heuristic capture/volatility score for compatibility/internal ranking. It is stored under `heuristic_capture_score` with `operator_visible=false` and is not rendered as operator R/R. No new metric overrides deterministic hard blocks or changes the current policy fingerprint.

## Outcome scopes and readiness truth (v1.0.58)

- The default operator scope is `current_policy`: active model lineage + exact active policy fingerprint + successfully re-hashed persisted policy contract.
- `current_model` is a diagnostic lineage view and may contain multiple policy fingerprints.
- `archive` is immutable research/audit history. It is never the default performance headline and never proves current-policy edge.
- The 80-row default is the monetary-return floor only. With `REQUIRE_CONF_GATE=1`, feature probability inference requires at least 300 exact-policy labels, sufficient purged OOF predictions, accepted aggregate skill, and accepted terminal-future-block skill.
- Any censored, unresolved or invalid matured current-policy root remains a hard observability veto. This avoids labeled-subset survivorship bias but creates an acknowledged liveness risk pending a bounded-censor model.

## Policy-conditioned calibration contract (v1.0.57)

`RECOMMENDER_MODEL_VERSION=bybit-taxonomy-v8-policy-conditioned-censor-aware`; bot/global keys are v19 and direction calibration is v14. Each publication root stores `outcome_policy.policy_contract`, its full SHA-256 `policy_fingerprint`, an exact canonical maturity timestamp and a role: `current_policy_evaluation`, `shadow_exploration` or excluded. The contract covers model/outcome/feature versions, selection thresholds, universe, LLM gate and active normalized risk limits. Readers recompute canonical JSON SHA-256 from the stored contract; a missing/tampered contract is unresolved even when its claimed digest matches a current cache key.

Policy evaluation deliberately excludes only calibration-evidence vetoes from its pre-calibration candidate decision. This prevents a circular deadlock where a missing calibrator prevents collecting the very outcomes needed to validate it. Hard data, scope, risk, geometry and economics vetoes remain exclusions. Calibration fits only roots that passed the exact pre-calibration candidate policy and the configured mean-reversion floor.

`reco_outcome_observability` is the independent denominator. Every matured exact-policy root is `labeled`, `censored` or `unresolved`; malformed maturity, missing/invalid labels and vanished cache support are unresolved. Any censored/unresolved/invalid count produces `expectancy_status=censored`, `PROXY_OUTCOME_CENSORING_UNBOUNDED`, empty coefficients and no actionable probability. Waiting rows are rotated by `last_attempt_ts`, preventing an old missing market from monopolizing a bounded worker batch.

Monetary row/cohort lower bounds use a one-sided Student-t critical value derived from effective sample size. Probability inference requires sufficient purged walk-forward predictions and lower log-loss than both score-only and null models on the aggregate and terminal future block. The activated LogReg/Platt is the exact candidate trained before the terminal block; the terminal labels never enter its fit. In-sample score-only Platt is unavailable. Direction calibration targets the sign of `exit_close-entry_close` for LONG/SHORT, not whole-grid success, but remains audit-only without its own chronological skill gate; `direction_confidence_feature` stays pre-decision raw.

## Terminal external reconciliation for live PnL (v1.0.57)

Execution rows remain immutable audit inputs, not exchange proof. Positive live PnL is recognized only when the bot is stopped, the signed execution ledger is flat, and the latest later complete reconciliation reports zero open orders/position and exactly matching execution/funding counts, gross PnL, fees and funding. Pre-stop or mismatched snapshots fail closed. Risk accounting is asymmetric before reconciliation: positive values are zero-credit; negative values remain losses. The external read-only Bybit adapter and its account provenance remain outside this recommendation service.

## Calibration lineage and evidence reset contract (v1.0.56)

`RECOMMENDER_MODEL_VERSION=bybit-taxonomy-v7-mr-floor-temporal-cohorts`. Only outcomes whose recommendation model version matches this lineage (or an explicit `+` derivative) and whose feature snapshot has valid finite mean-reversion evidence may enter calibration. Old v6 outcomes remain queryable audit history but contribute zero rows to v18 bot/global and v13 direction calibration. Status diagnostics distinguish historical archive, current-model rows, feature-eligible rows, sanitized fit rows and selected non-overlapping temporal cohorts.

## Mean-reversion candidate and temporal-independence contract (v1.0.55)

`mean_reversion_score` is a continuous candidate-quality feature, not a direct estimate of net PnL. Publication requires valid evidence on at least three closed timeframes and compares the aggregate score with `MEAN_REVERSION_MIN_SCORE` (default `0.25`). Below the floor the row is strategy `no_trade`; absent/invalid evidence is hard `blocked`. Neither branch may be overridden by LLM review. The score gate does not assert negative expectancy: only matured retained proxy returns and their uncertainty bounds may produce `positive`, `negative`, `uncertain` or `insufficient` monetary states.

Temporal evidence is computed from decision cohorts. All rows with the same recommendation `ts` are collapsed to one cross-sectional weighted return and the longest maturity boundary. Cohorts are ordered by end time and greedily thinned to the maximum-cardinality pairwise non-overlapping set. At `CALIB_MIN_SAMPLES=80`, at least 20 effective selected cohorts and positive one-sided 95% lower bounds at row and cohort levels remain required.

## Purged OOF feature-calibration contract (v1.0.54)

A full-sample feature LogReg fit is not itself out-of-sample probability evidence. Feature coefficients may influence `confidence` only when chronological OOF predictions are generated after label-availability purging, their count is at least `CALIB_MIN_SAMPLES`, and Platt-on-top is fitted on those OOF logits.

If OOF evidence is insufficient or invalid, `coef=[]`, `confidence_model.logreg_active=false`, and the service uses score-only Platt when available; otherwise it uses capped raw confidence. Monetary expectancy, temporal-cluster lower bounds and no-trade gates remain prerequisites and are not weakened by this fallback. Operator diagnostics expose OOF status, actual sample count and required count.

## Horizon-boundary liquidity contract (v1.0.53)

The configured `horizon_sec` still ends at the exact boundary candle open. However, a liquidation-equivalent outcome that uses quantity evidence must wait until that boundary minute closes, so `label_available_ts = horizon_end_ts + 60`.

At the boundary the prior candle's fill budget is discarded. Gap-crossed resting orders and terminal residual liquidation consume one shared budget equal to the boundary candle's observed base/contract volume. At a kill-switch, prior grid fills and residual liquidation consume one shared breach-candle budget. Any required full quantity above the remaining capacity returns outcome-unavailable (`insufficient_candle_volume_for_full_fill`, `insufficient_candle_volume_for_terminal_liquidation`, or `insufficient_candle_volume_for_kill_switch_liquidation`). This remains a conservative historical proxy and not an assertion of runtime execution.

## Kill-switch liquidation bound (v1.0.52 / grid_label_v25)

A one-minute candle that trades through a kill-switch proves that the trigger region was crossed, but not that a market close filled perfectly at the trigger price. The historical proxy first processes resting grid orders only up to the protective boundary. It then prices the residual close conservatively within the observed candle:

- upper breach with residual short inventory: liquidation at candle `high`;
- lower breach with residual long inventory: liquidation at candle `low`;
- continuation favorable to the residual position: liquidation remains at the kill-switch boundary, so favorable slippage is not credited;
- close-open or horizon gap that skips the boundary: outcome unavailable.

Diagnostics expose `kill_switch_fill_confirmation=adverse_observed_extreme_v1`, the trigger boundary, observed extreme and proxy liquidation price. This is a conservative historical loss bound, not a reconstruction of a real stop order or runtime execution.

## Historical proxy contract (`grid_label_v24`)

The system models outcomes from historical market data only. It does not submit orders and does not establish runtime executability. Current Bybit filters are not a publication gate and are not required for outcome maturity. Persisted recommendation geometry is the simulation input; when historically contemporaneous instrument constraints are unavailable, the label remains a model result rather than an exchange-executable claim.

Every recommendation exposes `simulation_scope=historical_proxy_only`. Fill rules remain deliberately conservative: price must trade through a resting level, total simulated quantity cannot exceed candle volume, replacements activate no earlier than the next candle, positive funding receipts do not manufacture alpha, and ambiguous paths produce no label.

Optional explicit preflight may show current snapping/minimum-order diagnostics to an operator, but it is outside the scoring, publication, outcome and calibration contracts.

## Intrabar replacement-order activation (`grid_label_v23`)

A replacement Buy/Sell is created only after its parent grid order fills. One-minute OHLCV cannot prove when that parent fill occurred or whether the replacement reached Bybit before a reversal later in the same candle. New replacement quantities therefore remain pending for the rest of their creation candle and become active at the next candle boundary. If the current candle crosses a pending replacement, `_grid_outcome` returns unavailable with `intrabar_replacement_fill_timing_unobservable`. Existing orders that were active before the candle continue to use strict trade-through and the shared candle-volume capacity check.

## Proxy fill-volume capacity contract (v1.0.49)

For Bybit Linear klines, `volume` is base/contract quantity. Current-model proxy execution therefore applies the following necessary condition per one-minute candle:

`initial market qty + sum(simulated resting fill qty) <= observed candle volume`.

Each simulated slot consumes `qty_per_order`. The budget resets for each candle and is preserved across alternative intrabar path simulation. If an individual fill or cumulative fills exceed the candle volume, `_grid_outcome` returns unavailable and records `insufficient_candle_volume_for_full_fill`; an oversized initial directional position records `insufficient_candle_volume_for_initial_inventory`. Sufficient volume does not prove a fill; strict side-aware trade-through and all other geometry/path gates still apply.

## Canonical proxy execution contract (v1.0.48)

A current-model proxy outcome is eligible only when the persisted recommendation contains an immutable verified Bybit Linear USDT instrument snapshot and its actual trade plan is aligned to the snapshot. Arithmetic range, reference, kill switches and step must be tick-aligned; quantity must be qty-step aligned and satisfy minimum quantity/notional at the adverse lower price. Recommendation-time normalization uses the same snapping and validation helpers as execution preflight. Missing metadata is fail-closed.

OHLCV does not establish queue execution at equality. A resting Buy requires an observable price segment strictly below its limit; a resting Sell requires a segment strictly above. A prior exact touch can be confirmed by later continuation through the level. Completed-cycle PnL, fees, funding and committed-capital formulas are unchanged once fills are confirmed. Outcome contract: `grid_label_v23`.

## Canonical funding treatment for proxy outcomes (v1.0.46)

Historical settlement rows are still required whenever modeled inventory is non-flat at a funding timestamp. Their sign is computed correctly for LONG/SHORT. However, the canonical proxy return used by `success`, monetary expectancy and calibration is asymmetric: adverse settled funding is charged; positive settled funding is excluded from edge. A receipt may be displayed as signed diagnostic cashflow, but it cannot turn a flat/negative grid path into a positive label.

This differs intentionally from exact account PnL. Exact terminal execution evidence sums realised execution PnL, signed funding and fees. Proxy labels are a conservative strategy-validation contract and must not promote a strategy because a temporary funding regime paid the held side. Outcome contract: `grid_label_v19`.

## Cross-symbol temporal-independence contract - v1.0.45

Monetary evidence is not counted by symbol rows alone. Each valid matured outcome contributes an interval `[ts, label_available_ts]`. Intervals with any direct or transitive overlap are merged into one temporal component, because they share future market information. The component contributes one recency-weighted mean return and one recency weight, independent of how many symbols were present.

The default `CALIB_MIN_SAMPLES=80` implies `minimum_temporal_clusters=20` (`min(20, ceil(min_samples/4))`). Eligibility requires both the ordinary Kish effective row sample floor and the effective temporal-cluster floor. The one-sided 95% lower bound must be strictly positive for row returns and for temporal cluster means. Otherwise expectancy remains `insufficient` or `uncertain`, LogReg stays unfitted, and the recommendation remains shadow `no_trade` with `PROXY_MONETARY_EXPECTANCY_UNPROVEN`.

This is a conservative dependence correction, not a proof of independent markets. Non-overlapping clusters can still share a long regime; final validation requires purged/block walk-forward and exact execution PnL.

## Terminal execution-evidence contract — v1.0.44

`LIVE_VALIDATION_*` использует только terminally finalized exact evidence. Для каждого bot execution ledger строится по immutable `bybit_execution` rows: Buy quantity имеет положительный знак, Sell quantity — отрицательный. Stopped bot допускается в статистику только если:

1. есть хотя бы один execution event;
2. каждый execution содержит валидные `side` и `qty`;
3. signed net position равна нулю в tolerance `max(1e-12, total_executed_qty * 1e-9)`;
4. bot имеет terminal status `stopped` и `stopped_ts`.

Realized formula остаётся `gross_pnl + funding - fee`; benchmark slippage не вычитается повторно, потому что фактические fill prices уже входят в gross PnL. Но ненулевой `net_position_qty` означает, что эта realised сумма ещё не является total bot PnL. Такая строка audit-visible, получает `validation_eligible=false` и не влияет на stop gate.

## Monetary expectancy uncertainty contract — v1.0.43

For `futures_grid`, probability calibration and raw heuristic confidence are subordinate to a monetary-evidence gate. Matured finite proxy returns are recency weighted; the system records the weighted mean, unbiased weighted standard deviation, Kish effective sample size, worst-20% expected shortfall, and a one-sided 95% lower confidence bound.

The decision rule is fail-closed:

- fewer than the required effective samples or missing diagnostics -> `expectancy_status=insufficient`;
- weighted mean `<= 0` -> `negative`;
- weighted mean `> 0` but lower bound `<= 0` -> `uncertain`;
- lower bound `> 0` -> `positive`, after which class-balance, purged temporal validation, feature integrity, risk and execution gates still apply.

Only `positive` can remove the bot-specific monetary thesis veto. `unknown`, `insufficient`, and `uncertain` add `PROXY_MONETARY_EXPECTANCY_UNPROVEN` and retain the candidate only as a shadow `no_trade` outcome root. Raw confidence remains descriptive and cannot make the candidate actionable. Confirmed negative expectancy uses `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE` and is preserved conservatively until a new cohort satisfies the positive lower-bound contract.

## Calibration evidence lifetime — v1.0.42

- `CALIB_REFIT_INTERVAL_SEC` является только cache interval, а не бессрочной лицензией на использование коэффициентов.
- После истечения interval положительный bot/global/direction calibrator должен быть воспроизведён из текущей retained outcome-выборки.
- Если current fit возвращает `insufficient`, positive/fitted model деактивируется и insufficient-state сохраняется, чтобы restart не воскресил stale coefficients.
- `expectancy_status=negative` сохраняется консервативно до новой подтверждённой positive evidence-выборки: отсутствие новых данных не превращает ранее наблюдавшийся убыток в разрешение на запуск.
- Bot/global keys v7 и direction key v6 отделяют этот cache-lifetime contract от прежней fail-open семантики. `OUTCOME_LABEL_VERSION` не меняется, потому что target/outcome math не изменялась.

## Shadow outcome independence — v1.0.41

`no_trade` может быть включён в исследовательский контур только с явным `outcome_policy.sample_role=shadow_no_trade`. Такая строка моделирует одну counterfactual grid-позицию на полном label horizon, поэтому повторный recommender cycle по тому же venue/symbol/bot/direction/model не является новым статистическим наблюдением.

Пока предыдущий shadow root не созрел, новые audit rows связываются с ним через `publication_root_rec_id` и получают `is_outcome_label_root=false`. После horizon или сохранённого outcome следующий сигнал может открыть новый root. Calibration принимает только model version `bybit-taxonomy-v4-independent-shadow-roots`; keys v6/v5 предотвращают загрузку моделей, обученных на перекрывающихся v3 roots.

## Monetary-expectancy calibration semantics - v1.0.40

Calibration estimates `P(success)` only after the same matured outcome cohort passes a monetary eligibility gate. Eligible rows must contain finite `score`, strict binary `success`, strict timestamps, a matured `label_available_ts`, and finite normalized `ret`.

The gate applies the existing recency weights to `ret` and computes:
- weighted mean proxy return;
- weighted expected shortfall over the worst 20% of observation weight.

After the existing effective-sample floor, `weighted_mean_return <= 0` produces `expectancy_status=negative`, leaves LogReg/Platt unfitted, and adds `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE` to strategy `no_trade` reasons. A positive mean is necessary but not sufficient: class-balance, temporal and feature-schema checks still apply. Funding receipts or a high binary win rate cannot override a negative monetary cohort.

Calibrator keys are `logreg_futures_grid_v5` and `logreg_global_v5`. The version bump prevents loading coefficients trained under v4 hit-rate-only eligibility. No outcome-label or schema migration is needed.

## Tail-loss stop semantics - v1.0.39

Exact execution evidence is evaluated as an operational capital-protection gate. Once an independent cohort reaches its predefined sample floor, `total_realized_pnl_net < 0` is sufficient for the corresponding negative-expectancy block. Median PnL and positive-bot rate are diagnostics, not vetoes over cumulative loss. This intentionally catches the grid-specific profile of frequent small wins offset by a rare large range-break loss.

The policy does not infer profitability when no block is present. It uses only stopped, validation-eligible bots with exact execution events; repeated publication roots count once and explicit `model_version` cohorts do not mix.

## Settled funding in historical outcomes - v1.0.37 / grid_label_v18

- Ticker `fundingRate` is a forecast for the next settlement and is used only by recommendation approval/risk logic.
- Proxy outcomes read immutable rates collected from `/v5/market/funding/history`.
- Historical funding P&L is signed: `funding_pnl = -position_qty × price_at_event × settled_rate`; positive rate makes LONG pay and SHORT receive, negative rate reverses the cashflow.
- A missing settled rate is harmless only when inventory at that timestamp is exactly zero. Otherwise the label is unavailable.
- Forecast receipts are never credited to approval edge, but actually settled receipts are part of historical Total P&L and therefore must be included in outcome/calibration.

## Cost-layer separation - v1.0.36 / grid_label_v17

Bybit Grid Profit одной завершённой arithmetic-пары равен полному соседнему интервалу минус комиссии двух resting fills. `spread + slippage` являются разовой market setup/terminal friction, а funding является signed position-time Total P&L. Эти слои нельзя вычитать из каждой пары: при K циклах это умножает разовые/горизонтные расходы на K.

Канонический контракт:
- `grid_round_trip_fee_bps` определяет spacing, density и net grid profit;
- `market_round_trip_cost_bps` используется для initial/terminal execution stress и диагностики;
- funding начисляется по фактическому inventory на событиях и проверяется отдельным funding guard;
- live bid/ask spread остаётся отдельным liquidity cap и не становится recurring fee каждой limit-пары.

## Current neutral opening-order reservation rules (grid_label_v15)

## Bybit cross-margin and one-way execution contract (v1.0.35)

Bybit Futures Grid Bot is modelled as `account_mode=unified`, `margin_mode=cross`, `position_mode=one_way`. A standalone isolated-position liquidation price is not used as a safety oracle. The deterministic gate recomputes a conservative cross-margin equity stress from exact grid commitment, leverage, execution cost and both kill-switch boundaries. Funding receipts and hypothetical grid profits are not credited to the stress buffer. Exact wallet equity, other positions/orders, risk tier and mark-price liquidation remain external executor checks. Legacy isolated-mode payloads are blocked fail-closed.


- NEUTRAL starts with zero position; every initial Buy and Sell resting order is an opening order and therefore part of the deterministic commitment floor.
- `committed_notional_per_qty = sum(initial Buy prices) + sum(initial Sell prices)`.
- `committed_slot_count = number of all initial opening orders` (exactly N under the current dynamic bridge topology).
- `max_abs_position_slots = max(Buy opening slots, Sell opening slots)` remains a separate one-way exposure metric.
- One-way position netting constrains simultaneous net inventory; it does not make opposite initial opening orders free or remove their preflight margin requirement.
- Recommender, auto-snap, preflight, runtime caps and outcome normalization must consume the same `arithmetic_grid_commitment` result.
- `grid_label_v15` is incompatible with v14 proxy outcomes/calibrators; the version guard resets those derived rows while preserving recommendations, bot lifecycle, trades and exact execution evidence.

The v1.0.32 max-side commitment rule and its iteration220 oracle are superseded. Dynamic bridge topology from v1.0.33 remains valid: N intervals create N+1 prices, one bridge price is idle, and exactly N initial orders remain.

## Current dynamic bridge topology rules (grid_label_v14)

- `grid_count=N` is the number of arithmetic intervals and creates N+1 prices, but dynamic Futures Grid starts with exactly N resting orders.
- Reference exactly on a price level leaves that pivot idle. Reference between levels also leaves one adjacent bridge level idle until the neighbouring order fills.
- For NEUTRAL and LONG off-grid entry, the nearest upper level is the idle bridge; for SHORT, the nearest lower level is idle.
- Directional initial inventory equals the number of initial close-side orders that actually exist after excluding the bridge; no phantom lot is created for the idle level.
- Neutral commitment now sums every actual initial Buy/Sell opening order; maximum one-way position remains the larger directional stack.
- Recommender, auto-snap, preflight, runtime caps, daily-loss guard and outcome ledger must use the same `arithmetic_grid_commitment` result, including `idle_grid_index`.
- `grid_label_v14` is incompatible with prior proxy outcomes/calibrators and is reset by the version guard; recommendations, trades, bot audit lifecycle and exact execution evidence remain.
- The OHLCV ledger remains a proxy and does not prove queue priority, partial fills, actual fee tier or live edge.

## Historical quantity-aware ledger rules (grid_label_v12, superseded)

`grid_label_v12` introduced quantity-aware same-level lots and gap-stop exclusions. `grid_label_v13` then corrected neutral one-way commitment, but still used an N+1 initial-order off-grid topology. Both are superseded by `grid_label_v14`: grid_count denotes intervals, exactly N initial orders exist, one pivot/bridge price is idle, directional commitment includes only actual initial inventory plus adverse-side openings, same-price lots retain quantities, and path-dependent OHLC/gap-through stops remain unavailable.

## Bybit Linear USDT product boundary

- Public Bybit REST client принимает только `category=linear`; другой category отклоняется до сетевого запроса.
- Symbol-specific market-data/metadata calls принимают только символы `*USDT`; non-USDT symbols не попадают в ticker/kline/funding/open-interest/instrument-info path.
- Если upstream/stub возвращает список без точного совпадения `symbol`, строка отбрасывается: collector не должен присваивать чужую цену, funding или metadata запрошенному контракту.
- Broad ticker fetch дополнительно фильтруется по `*USDT`, потому что продуктовый scope сервиса уже API-scope Bybit `linear`: рекомендации строятся только для USDT perpetual.
- HTTP 2xx не является достаточным доказательством успешного Bybit V5 ответа: `retCode` обязан присутствовать и быть exact integer. Только `retCode=0` допускает чтение `result`; missing/boolean/fractional/malformed control value повторяется как response-shape error и после исчерпания retry блокирует цикл.
- Kline/open-interest request `limit`, `start/end` и `startTime/endTime` нормализуются только из exact integers. Отрицательные или инвертированные временные окна, boolean и fractional значения отклоняются до REST-запроса, чтобы collector не строил историю по усечённым границам.
- Funding в risk/recommendation payload теперь хранит `directional_funding_bps_per_event`; legacy alias `directional_funding_bps_8h` не использовать для новой логики.

## Current grid-ledger topology and protective-stop rules (grid_label_v10)

- For a LONG entry between levels, every level above entry is a TP sell and the initial position contains one equal slot per such sell; SHORT is symmetric for buy-to-close levels below entry.
- Observable minute movement is processed as separate close->open and open->close segments. A one-sided high/low excursion is counted only when OHLC makes its chronology unambiguous; two-sided intrabar sequencing is not invented.
- `levels.kill_switch.lower` and `upper` are mandatory and must strictly contain the persisted range. A breach terminates the ledger at the boundary, liquidates residual inventory there and suppresses all later fills/funding.
- If one OHLC candle reaches both outer kill-switches, first-hit order is unknowable; the outcome remains unavailable rather than choosing a profitable or losing chronology.
- `grid_label_v10` is incompatible with earlier proxy outcomes/calibrators and is reset by the version guard.

## Current proxy-entry and persisted-contract rules (grid_label_v9)

- Signal evidence is available from `features_ref_ts`, but a hypothetical order cannot be filled before the recommendation is published. Entry is the open of the first exact 1m candle strictly after publication; an already-open candle is never backfilled as an entry.
- A label represents the exact persisted arithmetic grid. Duplicate valid `grid_count/grid_levels`, range and funding aliases must agree. Explicit malformed or conflicting aliases make the outcome unavailable.
- Invalid direction, range, grid count, entry outside range or inconsistent funding model is a diagnostic skip, not `ret=0, success=0`.
- `grid_label_v9` remains liquidation-equivalent net PnL from the explicit equal-slot ledger; it does not reconstruct intrabar queue/fill truth.


## Важная граница

Несмотря на терминологию `bot_instance`, проект не является реальным grid execution engine.
Он формирует рекомендации для запуска бота оператором и ведёт audit-контур вокруг этого решения.

## Поддерживаемые рекомендации
- `futures_grid` только для Bybit `category=linear`, USDT perpetual. Расчёты PnL/margin/funding ведутся в USDT по linear-модели.

## Разрешённые направления
- `futures_grid`: `neutral`, `long`, `short`

## Режимы, которые система считает поддержанными
- `futures_grid`: `venue=linear`, `account_mode=unified`, `margin_mode=cross`

## Операторский профиль плеча и малого счёта

Текущая shipped-политика риска синхронизирована с `settings.py`, `.env.example`, `README.md`, операторской DOCX/PDF-инструкцией и `how_to_trade.png`:

- один `running` grid-bot на счёт и один bot на symbol/publication-chain;
- интервал `min_leverage=3`, `max_leverage=5` как базовый операторский профиль этой ревизии;
- 3-5x является базовым actionable-диапазоном: слабая/дорогая/волатильная идея остаётся non-actionable и блокируется `MIN_LEVERAGE_PER_BOT`, а не публикуется как безопасная low-leverage сделка;
- если оператор задаёт `max_leverage` ниже 5 или `min_leverage` ниже 3, это более строгий safety-cap; effective minimum не должен обходить верхний лимит;
- 10x и выше не являются default-политикой для малого счёта; это отдельный осознанный профиль, который должен быть подтверждён в `RISK_LIMITS_JSON` и worst-boundary liquidation buffer.

`account_mode=one_way` допускается только как legacy-алиас старых payload'ов и помечается warning'ом;
штатной моделью ревизии он не считается. Поддержка `cross`, `hedge mode`, order-routing и real fill reconciliation
в этой ревизии отсутствует. Если такие режимы или пустой `margin_mode` появятся в данных вручную,
execution-time validation должна блокировать исполнение, а не притворяться, что логика проекта их понимает.

## Как строится grid idea

1. Берётся reference price. Если price отсутствует, не положителен, `NaN` или не finite, генератор не подставляет synthetic fallback: рекомендация получает `INVALID_MARKET_REFERENCE_PRICE`, нулевую геометрию/экономику и fail-closed статус.
2. По ATR и stability/range context выбирается минимальный экономический шаг `economic_min_grid_spacing_pct`, который обязан покрывать execution-cost и adverse expected funding carry.
3. По тому же контексту выбирается число интервалов `grid_count` / legacy `grid_levels`.
4. Строится основной диапазон `price_range_lower/upper`; для Bybit arithmetic grid исполнимый шаг публикуется как `grid_spacing_pct = (upper - lower) / grid_count / reference_price`.
5. Вокруг диапазона строится `kill_switch` через padding от старшего ATR.
6. Рассчитывается `params.economics`: gross/net profit per grid, execution cost, funding impact, minimum viable order notional, estimated margin required и approximate worst-boundary liquidation buffer.
7. Если interval minus recurring fees двух grid fills <= 0 или слишком тонок, рекомендация получает блок `GRID_NET_PROFIT_*`. Spread/slippage относятся к разовой market setup/terminal friction, а adverse funding — к position-time Total P&L и отдельным launch/risk gates; эти расходы не вычитаются из каждой пары. Funding receipt не повышает score/RR и не засчитывается как approval-edge. Если `next_funding_ts` отсутствует, recommendation и execution-preflight консервативно оценивают возможные funding events по горизонту и interval, но не умножают этот horizon cost на число grid cycles.
8. Для UI и operator guidance формируется `trade_plan`.

## Что именно проверяется перед `executed`

### Рыночная свежесть
- есть свежие 1m candles;
- есть свежий ticker;
- symbol не отключён после upstream ошибок.

### Рыночные блокировки
- market shock state не запрещает новый вход;
- symbol fast-veto не активен;
- instrument metadata Bybit подгружается до захвата SQLite write-lock, чтобы operator execution не тормозил остальные writer-контуры на сетевой задержке upstream; malformed symbols вроде `BTC/USDT`, `USDT` или `BTCUSDT-PERP` отсекаются до REST-запроса;
- execution-preflight fail-closed блокирует запуск, если metadata не подтверждает `contractType=LinearPerpetual`, `quoteCoin=USDT` и `settleCoin=USDT`;
- текущий live ticker сверяется с сохранённым `trade_plan.levels.range` и `kill_switch`: если цена уже вышла за диапазон или защитную границу, подтверждение `executed` блокируется до пересчёта рекомендации; если свежая ticker-запись не содержит пригодной `last`/`bid`/`ask` цены, execution preflight блокируется fail-closed с `LIVE_PRICE_UNAVAILABLE`.
- для generated/costed `futures_grid` одного `lastPrice` недостаточно: preflight требует валидные best bid/ask, считает `live_spread_bps=(ask-bid)/mid*10000`, обновляет slippage как `max(1 bps, 0.35*spread)`, сохраняет больший из stored/configured round-trip fee floor и консервативный остаток исходной cost model. Запуск блокируется при отсутствии bid/ask (`LIVE_SPREAD_UNAVAILABLE`), spread > 14 bps, live net edge < 2 bps или gross edge без запаса > 1.10x над execution cost. Legacy/manual payload без `cost_model` сохраняет прежний compatibility path.

### Геометрия grid-плана
- `reference_price` внутри диапазона;
- kill-switch лежит вне основного диапазона;
- operator-facing auto-snap по Bybit metadata не сужает рассчитанный диапазон: lower range / lower kill-switch округляются вниз, upper range / upper kill-switch округляются вверх;
- после округления по `tick_size` диапазон не схлопывается;
- `grid_step.step_abs` и `tp_per_leg.abs` округляются вверх для auto-generated payload, чтобы exchange-aligned значения не стали тоньше economics-модели net edge;
- шаг сетки не меньше `tick_size` и не больше диапазона;
- сетка содержит минимум 2 интервала после выравнивания;
- `grid_type` в этой ревизии допускается только `arithmetic`; `geometric` блокируется fail-closed, потому что для него нужна отдельная проверка ratio-levels, net-profit и tick rounding;
- `grid_count` / legacy `grid_levels` трактуется как Bybit Number of Grids, то есть число price intervals, и должен быть в диапазоне 2..400; генератор диапазона масштабирует total span по числу интервалов, а опубликованный arithmetic `grid_step.step_abs` соответствует `(upper - lower) / grid_count`;
- `grid_step.step_abs` и `params.grid_count`/`params.grid_levels` не должны описывать разные сетки; для generated payload с `grid_geometry_model=bybit_arithmetic_range_width_div_grid_count` mismatch блокируется strict execution-preflight, legacy/manual payload получает warning для ручной сверки;
- `tp_per_leg.abs` должен быть положительным и не схлопываться после округления по `tick_size`; off-tick TP помечается warning'ом с рассчитанным snapped-значением.

### Режимные инварианты
- `bot_type` согласован с `venue` и `direction`;
- `account_mode` и `margin_mode` не противоречат модели проекта;
- для supported execution-path обязательно присутствует явный `margin_mode=cross`, иначе recommendation блокируется fail-closed;
- `leverage` > 0 и укладывается в `min/max leverage`; дополнительно runtime risk caps могут ограничить `max_leverage`, `max_position_notional_usdt` и `max_margin_per_bot_usdt` на один futures grid;
- `leverage` выровнен по `leverage_step`, если биржа прислала такой constraint; leverage > 1 допускается только с явным worst-side/worst-boundary estimated liquidation buffer и блокируется, если buffer слишком мал;
- recommendation-layer выбирает operator minimum leverage не по фиксированному ceiling издержек, а по projected net grid edge после fees/slippage/adverse funding. Это предотвращает starvation-сценарий, когда default taker fee floor уже выше старого threshold, из-за чего все идеи падали в `MIN_LEVERAGE_PER_BOT`;
- metadata Bybit относится к тому же `symbol`, а не к соседнему инструменту/битому кэшу;
- instrument `status` должен быть `Trading`; `PreLaunch`, `Delivering`, delisted/other statuses блокируются fail-closed для новых operator confirmations.
- если payload содержит явный sizing (`order_qty`, `qty_per_leg`, `base_qty`, `order_notional` и совместимые алиасы), preflight блокирует значения ниже `min_order_qty`/`min_notional`, выше `max_order_qty` или не кратные `qty_step` (`ORDER_QTY_OFF_STEP`, `ORDER_QTY_BELOW_MIN`, `ORDER_NOTIONAL_BELOW_MIN`). Для base-qty проверка `minNotionalValue` использует минимальную положительную цену из reference/lower/upper основного grid range, потому что Bybit валидирует notional на фактической цене каждого ордера. Если одновременно переданы qty и quote-notional, preflight блокирует внутренне несогласованный sizing как `ORDER_QTY_NOTIONAL_MISMATCH`. Recommendation-time generator хранит provisional target-notional без фиктивного step; после получения live metadata qty округляется только вниз по фактическому `qty_step`. Невозможность выполнить minQty/minNotional приводит к blocked/no-trade, а не к увеличению позиции.
- operator-facing `params.operator_sheet.sizing` / `params.operator_sheet.economics` / `params.operator_sheet.leverage` считаются тем же источником исполнимых override-полей, что и `params.sizing`, `params.economics` и `trade_plan`: strict execution-preflight обязан проверять эти значения по Bybit filters, а UI обязан считать размер позиции/маржу из того же fallback-порядка, чтобы оператор не видел непроверенный sizing.

## Linear-USDT PnL, funding и liquidation

- Long PnL: `qty * (exit_price - entry_price)` USDT.
- Short PnL: `qty * (entry_price - exit_price)` USDT.
- Round-trip fee и execution friction вычитаются из каждой сетки до публикации рекомендации.
- Publication-time execution cost не считается вечным: непосредственно перед materialization `bot_instance` costed-рекомендация повторно оценивается по текущему best bid/ask. `lastPrice` подходит для range/kill-switch drift, но не подменяет executable spread.
- Funding учитывается direction-aware: положительный funding penalizes long, отрицательный penalizes short, а потенциальное получение funding не считается устойчивым alpha. Canonical `net_profit_bps`, score и `expected_rr` для допуска считают только adverse funding cost (`funding_cost_bps=max(expected_funding_bps, 0)`), а потенциальное получение funding выводится отдельно как `funding_benefit_excluded_bps`, `net_profit_with_signed_funding_bps` и `signed_net_cost_bps`; рекомендация не должна становиться исполнимой или выглядеть сильнее только из-за funding receipt. Для Linear USDT perpetual отсутствие актуального funding rate теперь блокирует рекомендацию как `FUNDING_RATE_UNKNOWN`, чтобы UI/API не показывали net-profit без funding-компонента. Количество funding events считается по Bybit `fundingIntervalHour`/instrument metadata; если interval отсутствует и funding material, рекомендация блокируется как `FUNDING_INTERVAL_UNCONFIRMED`, а не молча использует неподтверждённое допущение.
- Liquidation price в проекте считается только как conservative approximation для preflight/UI. Для risk gate используется минимальный buffer между reference price и adverse boundary/kill-switch, чтобы не завышать безопасность leveraged grid у края диапазона. Точная ликвидация зависит от risk tier, mark price, wallet margin и текущей позиции на Bybit. Если сторона позиции неизвестна или повреждена, helper не подставляет long/short по умолчанию и возвращает `None`; такой payload должен считаться непроверенным, а не безопасным.

## Риск-отчёт в recommendation payload

Каждая рекомендация получает `params.risk_report`:
- `decision`: `recommended` или `not_recommended`;
- `risk_profile`: conservative/moderate/aggressive;
- `expected_net_profit_per_grid_bps` и `expected_net_profit_per_grid_usdt` — conservative edge без зачёта funding receipt;
- `net_profit_with_signed_funding_bps`, `funding_cost_bps_for_approval`, `funding_benefit_excluded_bps`;
- estimated execution cost, funding impact, funding interval;
- liquidation buffer, required capital;
- adverse scenario, rejection reasons, warnings и approval factors.

UI обязан показывать этот блок рядом с execution/liquidity details. Если `decision=not_recommended` или есть blocking reasons, оператор не должен запускать grid до пересчёта.

## Temporal contract market data -> features -> outcomes -> calibration

- Ticker freshness uses authoritative Bybit response time when present; local receipt time is not a substitute for exchange event time.
- Kline start timestamps are exact integers, whole-second and timeframe-aligned. Shifted values are rejected, not floored.
- Feature timestamps reject JSON booleans, fractions and non-finite/malformed values.
- Signal availability starts at `features_ref_ts`, but outcome entry is the open of the first exact 1m candle strictly after recommendation publication; the 1m window through the horizon must be contiguous and the exact horizon boundary must exist. Missing candles mean “label unavailable”.
- Calibration accepts a row only when exact `label_available_ts` is present, is not earlier than recommendation time, and has matured by fit time.
- Temporal integrity was introduced in `grid_label_v4`; v1.0.24 used `grid_label_v5`, v1.0.25 used the explicit order/inventory ledger `grid_label_v6`, v1.0.26 used inventory-aware funding/finalization `grid_label_v7`; v1.0.27 used the sign-consistent funding-window/cost-alias contract `grid_label_v8`; v1.0.28 used post-publication-entry and strict persisted-contract target `grid_label_v9`; the current topology/terminal-stop target is `grid_label_v10`. Older proxy outcomes/calibrators are reset and must not be mixed with `grid_label_v10`.

## Что outcome labeling умеет и чего не умеет

### Умеет
- использовать канонический arithmetic step `(upper-lower)/grid_count`; одна завершённая grid-пара получает полный соседний interval, execution cost начисляется на каждую inferred leg и на terminal close остаточного inventory, а funding применяется к фактическому adverse position value в момент события; funding receipt не кредитуется как durable edge для calibration;
- сохранять точный момент доступности proxy-label (`label_available_ts = entry_ts + effective_horizon`) и использовать purged chronological OOF: train-label обязан быть полностью известен строго до первой validation-рекомендации; legacy labels без точного availability timestamp исключаются из OOF train;
- сохранять fail-closed kill-switch semantics: breach не может стать успешным label даже при положительном рассчитанном total PnL;
- применять fail-closed precedence: любой breach нижнего или верхнего `kill_switch` делает proxy outcome неуспешным и не позволяет отдельному `tp_per_leg` touch повысить label;
- отдельное касание directional `tp_per_leg` не является terminal whole-grid PnL event: без фактической последовательности fills и закрытого inventory оно остаётся диагностикой и не может самостоятельно создать `success=1`;
- completed-leg gross return и execution cost переводятся в доходность капитала всей сетки через деление на подтверждённый canonical `grid_count`; legacy payload использует независимо выведенное число интервалов, если count отсутствует;
- `grid_count` задаёт число одновременно финансируемых intervals и capital denominator, но не ограничивает cumulative completed trades за horizon; replacement orders позволяют одному interval закрыться повторно;
- один inferred completed arithmetic-grid trade даёт full interval gross minus one round-trip execution cost, нормированные на весь grid capital; отдельный произвольный fill-efficiency haircut повторно не применяется;
- neutral grid starts flat; LONG/SHORT создают исходную directional-позицию по числу уровней соответствующей стороны; worker ведёт cash, signed inventory slots и replacement orders по каждому close-to-close пересечению уровня;
- исполнять cost по каждой фактически inferred leg, закрывать исходные/grid lots по цене уровня и маркировать оставшийся net inventory по exact horizon exit; благоприятное directional movement улучшает total PnL, неблагоприятное ухудшает его;
- stale `grid_spacing_pct`, `tp_per_leg` или cost floor не меняют historical geometry: outcome использует persisted range и strict integer `grid_count`;
- на label horizon остаточный inventory закрывается на liquidation-equivalent basis с terminal half-leg cost; `success` следует знаку net total PnL после activity/kill-switch gates и не использует скрытый 5 bps cutoff;
- при exact funding schedule charge считается по net inventory и event price; neutral without inventory pays zero. При неизвестном schedule conservative fallback ограничен максимальным adverse inventory, достигнутым ledger, а не всем grid capital;
- `success` определяется как finite liquidation-equivalent `ret > 0` при intact kill-switch; отдельный activity/drift threshold запрещён, потому что он создаёт противоречие `ret > 0, success=0`;
- при подтверждённых `next_funding_ts + interval` aggregate expected-event fallback не применяется: если в horizon нет точного события, funding cost равен нулю;
- дубли `params.cost_model`/`trade_plan.cost_model` разрешаются по максимальному валидному execution cost; boolean/malformed/zero alias не маскирует более строгий cost;
- любое несовместимое изменение target требует нового `OUTCOME_LABEL_VERSION`; v1.0.29 использует `grid_label_v10` и не смешивает proxy labels/calibrators, построенные по прежней accounting/temporal semantics;
- neutral `success=1` допускается уже после одной завершённой прибыльной пары; LONG/SHORT success определяется положительным total grid PnL при фактической mode activity и отсутствии kill-switch breach.

### Не умеет
- доказать intrabar fill sequence: proxy исполняет только уровни, пересечённые последовательными 1m close; high/low используются для kill-switch, но не для оптимистичного fill inference;
- реконструировать реальную exchange fill sequence;
- учитывать queue priority и live slippage distribution;
- учитывать частичные исполнения на уровне отдельных ордеров;
- моделировать liquidation engine и real margin waterfall.

## Что должен делать внешний execution layer

Если проект используется в production-пайплайне, внешний контур обязан:
- повторно проверять фактический qty, qty_step, min qty и min notional по live account/instrument данным; проект проверяет эти фильтры для рекомендованного minimum viable sizing и любых операторских overrides в `trade_plan.sizing` или `params`;
- выставлять/менять/отменять реальные ордера;
- хранить order/fill state machine;
- восстанавливать состояние после рестарта по фактическим биржевым данным;
- присылать в этот сервис агрегированные realised trade rows для аудита.


## Инвариант publication-chain
Для одной publication-chain допускается не более одного `running` bot_instance. Это не просто UI-правило: инвариант обеспечивается persistence-слоем, чтобы гонка двух операторских `execute` не создавала две параллельные позиции на один и тот же рекомендательный корень.

## Signal durability и immutable UI identity

Для `futures_grid` высокий score сам по себе не является независимым подтверждением. Actionable-публикация требует минимум двух разных, строго возрастающих закрытых evidence snapshots (`features_ref_ts`), каждый из которых отдельно прошёл score/risk/economics gates. Повторные recommender-cycles на одной и той же закрытой 1m-candle не увеличивают `observed_hits`; stale, out-of-order или legacy state без evidence timestamp начинает последовательность заново. До второго независимого snapshot строка остаётся `pending`.

`recommendations.rec_id` является immutable audit identity и в UI. Обновление открытой карточки перечитывает тот же `rec_id`; более новая строка по тому же `(venue, symbol, bot_type)` не может молча заменить выбранную карточку. Новые `recommended`, `pending`, `blocked` или `no_trade` публикации видны только как отдельные строки/события истории.

## Независимый mean-reversion gate для grid

`range_score = 1 - trend_strength` больше не является достаточной торговой гипотезой. Нулевая направленная компонента не отличает возвратный процесс от мартингального/random-walk процесса, у которого self-financing grid не получает положительного математического ожидания до издержек и теряет после издержек.

`app.direction.mean_reversion_diagnostics()` использует только закрытые цены и отдельно оценивает:

- lag-1 autocorrelation лог-доходностей;
- variance ratio для четырёхшаговой доходности;
- долю последовательных доходностей с противоположным знаком.

Multi-timeframe aggregate считается валидным при минимум трёх TF и весовом покрытии не менее 40%. В publication gate используется `MEAN_REVERSION_MIN_SCORE` (по умолчанию `0.25`). Валидный, но более слабый evidence получает `MEAN_REVERSION_EDGE_UNCONFIRMED` и статус `no_trade`: это отрицательное решение candidate-screen, а не доказательство отрицательного PnL и не техническая ошибка Bybit/preflight. При недостатке обязательной истории применяется hard block `MEAN_REVERSION_EVIDENCE_INSUFFICIENT`. Ни `no_trade`, ни hard block не могут быть отменены высоким legacy range score, LLM verdict или raw confidence; monetary expectancy проверяется отдельно по matured outcomes.

Threshold был sanity-checked на детерминированной Monte-Carlo выборке: среди 200 IID paths gate пропускает не более одного, тогда как для материально anti-persistent AR(1), `phi=-0.35`, пропускается не менее 150. Это unit-level discriminative check, а не оценка live profitability. Bid/ask bounce и transient anti-persistence могут быть неисполняемыми после costs, поэтому положительный score остаётся только предварительным evidence.

Feature/calibration identity изменена: текущая recommendation model — `bybit-taxonomy-v3-mean-reversion`, а logistic/Platt keys имеют v4. Для fit принимаются только outcomes текущей модели с явным `mean_reversion_evidence_valid=1` и finite `mean_reversion_score`; legacy outcomes не используются даже для score-only fallback.

Поле `expected_rr` исторически вычисляется как bounded capture-to-volatility heuristic. Оно сохранено для совместимости и внутренней диагностики, но основной UI его больше не показывает. Оператор использует отдельные `plan_rr` и `empirical_expectancy`; их семантика определена в разделе v1.0.61 выше.


### Shadow outcomes для no_trade

Calibration не должна обучаться только на уже отобранных actionable идеях: это создаёт selection bias и при длительном отсутствии запусков останавливает накопление выборки. Поэтому publisher сохраняет явный `reasons.outcome_policy`.

- `sample_role=shadow_no_trade` допускается только для `no_trade` с полным `trade_plan`, валидной reference price и пустым hard-block list;
- worker принимает только literal JSON boolean `eligible=true`, повторно проверяет `risk_checks.passed=true` и отсутствие blocks;
- `blocked`, `pending`, `suppressed`, malformed и legacy no-trade без явного opt-in не размечаются;
- shadow outcome является counterfactual OHLCV proxy, а не доказательством реального запуска или fill sequence;
- outcome API defaults to verified `current_policy`; `current_model` and `archive` are explicit scopes. Inside a scope it still separates `actionable` and `shadow_no_trade`, while the historical archive never enters the current headline.

Необученный calibrator не меняет status автоматически: до fit используется ограниченный raw-confidence, а deterministic gates остаются source of truth.

## Семантика score и confidence

Launch-score для `futures_grid` оценивает прежде всего пригодность режима для сетки: range suitability, trend/ATR penalties, multi-timeframe coherence, execution costs и adverse funding. В raw-режиме `confidence` является ограниченным нелинейным отображением того же эвристического score с дополнительными penalties за неполный контекст; это не независимая вероятность прибыли. Только bot-specific fitted calibrator добавляет статистический слой, но его target остаётся proxy-outcome, а не фактический биржевой net PnL. Поэтому ни raw, ни calibrated confidence не доказывают live edge без отдельной walk-forward/shadow проверки по реальным fills и costs.

## Exact-evidence strategy-health stop gate

Operator execution preflight не ограничивается проверкой текущего payload. Перед materialization `bot_instance` он читает stopped bots с immutable execution evidence и строит три newest-first cohort: `(venue, bot_type, symbol, direction)`, symbol-wide и portfolio-wide. Один `publication_root_rec_id` учитывается не более одного раза, поэтому repeated updates одной signal chain не создают ложную статистическую мощность. При explicit `model_version` используются только результаты той же версии; новая модель не наследует блок старой. Long/short/neutral не смешиваются в directional cohort.

Fail-closed коды:

- `LIVE_VALIDATION_DIRECTION_LOSS_STREAK`: пять последних независимых stopped bots того же symbol/direction имеют `realized_pnl_net < 0`;
- `LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY`: минимум 8 независимых observations и отрицательный cumulative exact net PnL по direction;
- `LIVE_VALIDATION_SYMBOL_NEGATIVE_EXPECTANCY`: минимум 12 независимых observations и отрицательный cumulative exact net PnL по символу;
- `LIVE_VALIDATION_PORTFOLIO_NEGATIVE_EXPECTANCY`: минимум 20 независимых observations и отрицательный cumulative exact net PnL по всему Linear USDT `futures_grid` contour.

`median_realized_pnl_net` и `positive_bot_rate` сохраняются в metrics/сообщении для диагностики распределения, но высокий win rate больше не делает убыточный cumulative cohort исполнимым.

В расчёт входят только `validation_eligible` stopped bots с хотя бы одним exact execution event. Legacy `/trades`, running bots, malformed/non-finite PnL и повторные publication roots не используются. Это safety stop criterion: он запрещает механически продолжать подтверждённо убыточный режим, но не объявляет оставшиеся режимы прибыльными и не заменяет chronological walk-forward/comparator validation.

## UI score segmentation

`score` остаётся raw эвристическим числом для backend-гейтов и tie-break diagnostics, но операторский `Ранг в выборке` не должен выглядеть как точная вероятность или точное качество идеи. UI строит percentile по видимым кандидатам с near-tie группировкой: raw-score отличия `<= 0.025` считаются практически неразличимыми, группа получает общий averaged percentile/grade. Это предотвращает ложное разделение малой выборки, например `0.245 / 0.242 / 0.232`, на жёсткие `100 / 50 / 0`.

## Directional TP/SL semantics

Canonical long/short/neutral exit mapping lives in `app.trading_semantics` and is also exposed to the operator API as `directional_exit_levels`. For `long`, Take Profit is above entry/reference and Stop Loss is below. For `short`, Take Profit is below entry/reference and Stop Loss is above. For `neutral` futures grid there is no single directional Take Profit; both outer bounds are kill-switch exits. Execution preflight validates this geometry fail-closed for directional grids.


## Operator fixed-leverage no-trade semantics

Runtime `min_leverage/max_leverage` is an operator profile, not a reason to publish a synthetic lower-leverage trade idea. For Bybit Linear USDT `futures_grid`, the recommender evaluates the active profile and records the target `selected_leverage` in `params.leverage_policy`.

If the grid economics, volatility or signal quality cannot justify the active operator minimum (for example the shipped `3x/5x` leverage interval), the row must become `no_trade` / `not_actionable` with `OPERATOR_LEVERAGE_PROFILE_NOT_ACTIONABLE`. It must not publish a new `1x` payload that later becomes `blocked` only because `1x < min_leverage`. Legacy/manual rows that already contain `1x` remain fail-closed at execution time through runtime leverage guards.

Risk and UI semantics are separated:

- `blocks` / `risk_report.rejection_reasons` are hard fail-closed blockers;
- `risk_report.no_trade_reasons` explains soft non-actionability such as insufficient edge for the active 3-5x leverage profile;
- no_trade rows cannot be executed by the API and are rendered as "do not launch now", not as a Bybit/preflight hard block.

Recommendation-time margin checks and `risk_report.capital_required_usdt` must prefer `estimated_worst_case_margin_required_usdt` over reference-price margin when worst-case grid-envelope fields are present.

## 2026-06-17 numeric-boundary and directional-PnL provenance rule

All JSON booleans are invalid for numeric trading fields. This applies to market prices/volume/timestamps, OHLCV, funding timestamps and event counts, signal scores/confidence, direction aggregation, risk limits, grid geometry, calibration/outcome inputs and operator UI price/percent/qty rendering. Python `bool` must not cross a `float()`/`int()` boundary as `1/0`; JavaScript booleans must not cross `Number()` as `1/0`. Invalid booleans either make the value unavailable or fall back to the documented conservative default. They must never weaken a risk guard, create executable geometry or reduce a publication confirmation requirement.

`app.main::_directional_exit_payload_for_reco` may use `qty=1 base asset` only to derive dimensionless TP/SL distances and risk:reward when no position quantity exists. In that case the API must publish `qty_source=unit_qty_ratio_only`, `trade_math.qty_basis=one_base_asset_for_ratio_only` and `trade_math.gross_pnl_is_position_estimate=false`. Gross USDT fields in that payload are then per-one-base-asset arithmetic aids, not an estimate for the operator's position. When actual position/order quantity is available, `qty_basis=position_qty` and `gross_pnl_is_position_estimate=true`.

## Recommendation audit-row integrity (2026-06-18)

`recommendations.rec_id` is an immutable audit identity. Repeating the exact canonical payload is idempotent; reusing the same id with changed direction, score, confidence, status, params or lineage fails closed. Recommendation lifecycle updates must use the existing publication/state-transition mechanisms rather than SQL replacement of the original signal.

Market-shock and fast-veto calculations consume only fully closed candles. Every timestamp is exact-integer validated; future, still-open, boolean and fractional timestamps are excluded rather than truncated.

## Exact temporal and funding integer semantics (2026-07-11)

Bybit `nextFundingTime`/open-interest/OHLCV timestamps, `fundingIntervalHour`, instruments-info `fundingInterval`, label horizons and funding event counts are exact-integer fields. Numeric values such as `5.0` remain compatible because they represent an exact integer; boolean, fractional, blank and non-finite values are invalid.

Invalid upstream timestamps are discarded before they can collide with an existing integer-second persistence key. Invalid funding intervals remain unavailable rather than being rounded into a plausible schedule. The recommender cost model applies the same rule: a fractional/boolean/non-positive interval is marked `fallback_8h_invalid_interval`, stays uncertain and uses conservative possible-event counting. Execution-time funding then stays fail-closed: a missing schedule uses the conservative unknown-schedule event count, while a missing/invalid interval blocks costed execution. Purged calibration and the outcome worker exclude malformed recommendation, feature-reference or label-availability timestamps instead of manufacturing chronology through truncation.


## Execution evidence, funding and realised PnL

The repository remains recommendation/audit-only. A separate read-only adapter may write exact evidence but cannot place, amend or cancel orders through this project.

Canonical evidence rules:

1. Every execution is one immutable event keyed by `(source=bybit_execution, execId)` and directly linked to `bot_id` and immutable `origin_rec_id`. Multiple fills for one `orderId` remain separate events.
2. Funding is a separate event keyed by `(source=bybit_transaction_log, transaction id)`. It must not be embedded into an execution event.
3. `execPnl`/gross realised PnL is fill-based. Canonical net is `sum(gross_pnl) + sum(funding) - sum(fee)`. Signed negative fee represents a rebate.
4. Spread/slippage must not be deducted again from actual fill PnL. For execution-quality analysis the adapter supplies `benchmark_price`, `benchmark_ts` and `benchmark_source`; adverse benchmark-to-fill deviation is calculated by side and reported separately. `orderPrice` is evidence, not the benchmark.
5. Legacy `/trades` is compatibility-only. Exact evidence and legacy aggregates cannot be mixed for one bot. Defensive risk aggregation prefers exact execution events if a historical database already contains both.
6. Risk daily PnL, realised drawdown and cooldown consume the unified de-duplicated stream. This does not include unrealised inventory risk.

At operator execution time, the recommendation service nevertheless applies a conservative prospective guard: `estimated_max_position_notional_usdt` (or the larger qty × worst grid price × grid_count derivation) is multiplied by the adverse reference-to-kill-switch distance plus explicit execution cost. If this estimated loss is greater than `max_daily_dd_usdt - daily_dd`, execution is fail-closed with `DAILY_LOSS_BUDGET_EXCEEDED`. This is a budget guard, not mark-to-market reconciliation and not a substitute for the external executor.
7. Evidence GET endpoints require admin authorization. The live-validation endpoint is descriptive and always reports that a live-edge claim is unsupported without chronological comparator evidence.
8. External timestamps are stored as UTC seconds; adapters must convert Bybit millisecond fields exactly and reject boolean/fractional timestamps.
