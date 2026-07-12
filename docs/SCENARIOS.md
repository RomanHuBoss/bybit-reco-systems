## 0. Settled funding outcome scenarios - v1.0.37

- Positive settled rate + LONG inventory -> payment; positive rate + SHORT inventory -> receipt.
- Negative settled rate reverses those cashflows.
- Recommendation-time forecast may differ from settlement without changing the historical result.
- Expected event + non-zero inventory + missing settlement -> outcome unavailable.
- Expected event + zero inventory + missing settlement -> zero funding impact is safe.
- Old `grid_label_v17` outcomes are not mixed with `grid_label_v18`.

## 0. Grid cost-layer separation - v1.0.36

Expected behavior:
- completed grid pair pays exactly the two resting-fill fee legs;
- bid/ask spread and slippage are not multiplied by completed trade count;
- directional initial market inventory and terminal residual close use market-friction legs;
- funding is charged by actual inventory/event timing, never once per grid pair;
- grid spacing/density use recurring fee only, while spread/funding remain separate launch and Total-P&L controls;
- old `grid_label_v16` outcomes are not mixed with `grid_label_v17`.

## 0. Bybit Futures Grid cross-margin contract - v1.0.35

Expected behavior:
- generated Linear USDT Futures Grid payloads use `account_mode=unified`, `margin_mode=cross`, `position_mode=one_way`;
- `margin_mode=isolated` is blocked before operator execution;
- leverage above 1x requires a finite cross-margin equity buffer computed from exact grid commitment and both external kill-switches;
- the stress includes adverse inventory loss, entry/exit friction and maintenance reserve, and credits no funding receipt or hypothetical grid profit;
- the UI does not publish an isolated liquidation price for Bybit Futures Grid;
- exact private-account liquidation remains an external executor check.

## 0. Neutral full initial-order reservation (v1.0.34)

Expected behavior:
- NEUTRAL starts flat, therefore every initial Buy and Sell order is opening/margin-bearing;
- dynamic bridge topology still creates exactly N initial orders for N intervals;
- `committed_slot_count=N` and committed notional is the sum of prices of all N initial orders per unit qty;
- `max_abs_position_slots=max(Buy slots, Sell slots)` is separate and must not replace commitment;
- for levels 99/100/101 at reference 100, commitment is `99+101=200`, while maximum net position is one slot;
- for the six-price N=5 example at reference 20,000, active/committed initial orders are 10k, 14k, 18k, 26k and 30k, totaling 98k per unit qty; max one-way stack is 56k/three slots;
- a legacy payload reporting only max-side committed notional/slots is blocked by preflight;
- old `grid_label_v14` outcomes are not mixed with `grid_label_v15`.

## 0. Dynamic off-grid bridge topology (v1.0.33)

Ожидаемое поведение:
- N intervals create N+1 prices but exactly N initial orders;
- if reference is between levels, one adjacent bridge level has no initial order;
- NEUTRAL/LONG leave the nearest upper bridge idle; SHORT leaves the nearest lower bridge idle;
- reaching the bridge before an adjacent fill produces no execution and no PnL;
- after an adjacent fill, the replacement order may be placed on the bridge and can then execute;
- sizing, margin, worst-case exposure, daily loss and outcome denominator all use the same topology;
- old `grid_label_v13` outcomes are not mixed with `grid_label_v14`.

## 0. Neutral one-way capital reservation (v1.0.32)

HISTORICAL/SUPERSEDED: v1.0.32 treated only the larger neutral side as committed. v1.0.34 requires the sum of all initial opening orders; maximum one-way position remains the larger side.

# Ключевые сценарии

## 0. Same-level order quantity and gap-through protection (v1.0.31)

Expected behavior:
- if an adjacent replacement TP shares a price with an initial directional TP, the level quantity becomes two rather than discarding one lot;
- cash, position, execution cost and funding inventory use the entire aggregated quantity;
- a close→open or final-horizon gap beyond the kill-switch produces no proxy label because stop/grid-order chronology and fill price are not observable;
- daily-loss fallback uses `arithmetic_grid_commitment.active_order_count`, including `N+1` for an off-grid reference.

## 0. Exact commitment and ambiguous intrabar path (v1.0.30)

Expected behavior:
- `grid_count=N` creates `N+1` price levels; active order count is `N` only when reference is exactly on a grid level, otherwise `N+1`;
- directional capital includes initial inventory plus adverse-side opening orders at actual prices;
- generated, snapped, validated and outcome payloads agree on active orders, total notional and margin;
- if `O→H→L→C` and `O→L→H→C` lead to different fills, inventory, stop or PnL, no proxy label is stored.


## 0A. Between-level directional entry and protective stop

Ожидаемое поведение:
- LONG entry between levels creates the nearest upper sell plus one matching initial long slot; SHORT creates the nearest lower buy plus one matching initial short slot;
- a close->open gap and a subsequent open->close reversal are two observable segments and may complete a grid pair;
- a one-sided OHLC excursion is counted only when its order is unambiguous;
- first kill-switch breach stops the ledger and liquidates at the boundary; later recovery is irrelevant;
- missing/inside-range kill-switch or both boundaries touched in one candle means no proxy label.

## 0. Delayed publication or damaged persisted grid contract

Ожидаемое поведение:
- если `features_ref_ts + 60` candle уже открылась до публикации, entry переносится на первую точную 1m candle, открывшуюся строго после publication timestamp;
- если эта exact candle отсутствует, outcome остаётся unavailable;
- conflicting `grid_count/grid_levels`, разные валидные range aliases, malformed explicit range или конфликтующие funding aliases не превращаются в `ret=0`/loss;
- worker пишет `OUTCOME_SKIP_INVALID_GRID_CONTRACT`, не вставляет `reco_outcomes` и не обучает calibrator на вымышленной геометрии.


## 1. Холодный старт на пустой БД
Ожидаемое поведение:
- collector начинает наполнять 1m/ticker слой;
- recommender не публикует actionable рекомендации до прохождения warm-up;
- backfill расширяет историю до минимально достаточного окна.

## 2. Повторный same-direction сигнал внутри открытой publication-chain
Ожидаемое поведение:
- новая запись может получить `active`, а не новый outcome-root;
- старый publication_root_rec_id сохраняется;
- outcome labeling не удваивает псевдо-позицию.

## 3. Operator execution подтверждает рекомендацию
Ожидаемое поведение:
- risk limits проверяются повторно;
- execution-time preflight проверяется повторно;
- только после этого materialize'ится `bot_instance`;
- recommendation переводится в `executed` транзакционно.

## 4. Recommendation протухла по TTL
Ожидаемое поведение:
- `executed` должен быть заблокирован с `409`;
- recommendation должна стать `expired`, а не быть тихо исполненной.

## 5. Повторный execute того же rec_id
Ожидаемое поведение:
- создаётся не второй bot, а идемпотентный reuse уже существующего origin/publication-chain bot;
- статус остаётся согласованным.

## 6. Execution blocked by market shock / fast-veto / stale data
Ожидаемое поведение:
- API возвращает `409`;
- `bot_instance` не создаётся;
- в `decision_log` пишется причина блокировки.

## 7. Trade ingestion дублируется
Ожидаемое поведение:
- одинаковый `trade_id` и payload возвращают идемпотентный duplicate-result;
- bot state не портится;
- trade count не удваивается.

## 8. Trade приходит после остановки бота
Ожидаемое поведение:
- запись отклоняется с `409`, если это не точный идемпотентный повтор уже принятой сделки.

## 9. Runtime lock потерян
Ожидаемое поведение:
- соответствующий background loop должен остановиться fail-closed;
- split-brain background leadership быть не должно.

## 10. Bybit metadata указывает несовместимый leverage/mode
Ожидаемое поведение:
- recommendation details показывают ошибки валидации;
- `executed` блокируется, пока идея не исправлена оператором или новым publish cycle.

## 11. Одна publication-chain выпускает длинную серию `active` updates
Ожидаемое поведение:
- operator-facing `GET /api/v1/recommendations` не должен возвращать только эту одну идею, если в том же snapshot есть другие уникальные roots;
- API обязан расширить raw-scan и добрать `top_n` по уникальным `publication_root_rec_id`, пока это разумно по budget.

## 12. Bybit отдаёт 200/OK с битым JSON, malformed `retCode` или protocol-level transport error
Ожидаемое поведение:
- публичный клиент делает повторную попытку вместо мгновенного hard-fail первого же цикла;
- отсутствующий, boolean, fractional или иной malformed `retCode` не подменяется нулём и не открывает доступ к `result`;
- boolean/fractional request limits и timestamps, отрицательные или инвертированные временные окна блокируются до сетевого запроса;
- после исчерпания retry возвращается явная transport/decode ошибка, а не partially parsed payload.


## Execution blocked by live-price drift

1. Рекомендация была опубликована при `reference_price=100` и диапазоне сетки `[99, 101]`.
2. Перед тем как оператор подтвердил `executed`, свежий ticker показывает mid/last price вне диапазона или вне `kill_switch`.
3. `/api/v1/recommendations/{rec_id}/action` возвращает `409`, не создаёт `bot_instance` и пишет audit-событие блокировки.
4. Оператор должен дождаться нового цикла recommender или вручную пересчитать уровни; запуск старой сетки считается другой сделкой с другим риск-профилем.

## 14. Funding interval отсутствует при материальном funding
Ожидаемое поведение:
- recommendation-path не должен молча считать все USDT perpetual как 8h funding;
- если Bybit ticker/instrument metadata не дала interval, а expected funding impact материален, рекомендация получает `FUNDING_INTERVAL_UNCONFIRMED`;
- UI/API должны показать причину отказа и funding interval source.


## 15. Partial fills and funding reconciliation

1. External read-only adapter receives two fills with different `execId` for one `orderId`.
2. Both execution events are stored separately and linked to the same immutable `rec_id`; an exact retry is idempotent, while the same external id with changed economics is rejected.
3. A funding transaction is stored as a separate event with its own transaction id and signed cashflow.
4. Summary net equals actual gross fill PnL plus funding minus fee. Benchmark-to-fill slippage remains a separate diagnostic and is not deducted twice.
5. Daily risk/cooldown sees the same de-duplicated net stream.

## 16. Attempt to mix evidence ledgers

1. A bot already has a legacy `/trades` row or exact execution evidence.
2. A write to the other ledger is rejected fail-closed.
3. If a historical/corrupted database nevertheless contains both, risk uses exact execution events and does not count legacy execution aggregates again.

## 17. Live-validation export

1. Admin requests `/api/v1/validation/live-evidence` with valid authorization.
2. Only bots with immutable execution evidence appear; stopped bots with at least one execution become validation-eligible.
3. Returned aggregates are descriptive. The response explicitly does not claim live edge because no chronological comparator, no-trade baseline or sample sufficiency test is implied.
## 18. Exact-evidence stop gate after persistent losses

1. External adapter has recorded exact fills/fees/funding for independent stopped bots.
2. Five newest independent bots for the same `(symbol, direction)` are loss-making, or the predefined minimum cohort has negative total and median net PnL with positive-bot rate below 50%.
3. A new recommendation can still be published for audit, but operator action `executed` returns `409`, no `bot_instance` is created, and `decision_log` contains the relevant `LIVE_VALIDATION_*` code and cohort metrics.
4. A losing long cohort does not by itself block short until the broader symbol threshold is reached. Repeated rows from one publication root count once, and an explicit new `model_version` starts a separate evidence cohort.
5. The operator must diagnose/revise the strategy or evidence pipeline; manually downgrading the blocker is not a supported path.


## 19. Низкий тренд без подтверждённой возвратности

1. Multi-timeframe trendiness низкий, поэтому legacy `1 - trend_strength` выглядел бы как сильный range score.
2. Independent lag-1 autocorrelation / variance-ratio / sign-reversal aggregate отсутствует, недостаточен либо даёт `mean_reversion_score < 0.55`.
3. Recommendation остаётся audit-visible, но получает `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` или `MEAN_REVERSION_EDGE_UNCONFIRMED`; actionable `executed` path не создаётся.
4. Высокий raw score, LLM verdict или старый calibrator не отменяют блок. Оператор ждёт нового подтверждённого режима либо пересматривает стратегию.

## 20. Переход на новую calibration identity

1. БД содержит outcomes и calibrators модели `bybit-taxonomy-v2`.
2. v1.0.20 публикует `bybit-taxonomy-v3-mean-reversion` и использует calibrator keys v4.
3. Старые rows остаются в audit history, но fit принимает только current-model rows с явным independent evidence snapshot.
4. Пока matured sample недостаточен, bot-specific calibrator остаётся unfitted; это не снимает deterministic gates и не является ошибкой запуска.
## 21. Нет запускных рекомендаций, но research sample продолжает расти

1. Current candidate имеет полный trade plan, валидные market/risk inputs и пустой hard-block list.
2. Торговый тезис не проходит mean-reversion/score/confidence/economics gate.
3. Статус сохраняется как `no_trade`, а не `blocked`; оператор не может выполнить рекомендацию.
4. Publisher записывает `outcome_policy.sample_role=shadow_no_trade` и literal `eligible=true`.
5. После maturity worker строит counterfactual proxy outcome; legacy no-trade без opt-in и hard-blocked rows пропускаются.
6. UI считает shadow roots отдельно и не называет их фактическими сделками.
7. Необученный calibrator остаётся raw-only и сам по себе не является причиной отсутствия рекомендаций.
## 22. Arithmetic-grid outcome ledger v7

1. Version guard обнаруживает несовместимый label contract и удаляет только прежние proxy outcomes/calibrators.
2. Worker требует finite persisted range, strict integer `grid_count`, exact next-candle entry и непрерывный 1m horizon.
3. Neutral starts flat; LONG/SHORT получают исходные равноколичественные lots согласно уровням выше/ниже entry.
4. Только close-to-close crossings исполняют level order; fill меняет cash/inventory, создаёт replacement order и начисляет half round-trip execution cost.
5. На exact horizon exit остаточная позиция mark-to-market и получает terminal close cost, чтобы outcome был liquidation-equivalent net result.
6. Adverse funding event применяется к фактическому net inventory и event-price proxy. Neutral без inventory не платит; possible receipt не улучшает outcome. Unknown schedule использует maximum adverse inventory fallback.
7. Одна прибыльная neutral pair или фактическая directional activity с положительным total PnL может дать success; отдельного 5 bps cutoff нет. Kill-switch breach всегда оставляет `success=0`.
8. Статистика остаётся proxy и не заменяет exact execution evidence.



## Outcome label v8 integrity

- Positive finite liquidation-equivalent total net PnL is a win unless a kill-switch was breached.
- A confirmed funding schedule with no event in the horizon charges zero; expected-event fallback is only for an unavailable schedule.
- Conflicting duplicated execution-cost aliases resolve to the maximum valid cost.
- Malformed OHLC candles make the horizon unavailable and do not create a loss label.
