# Торговая логика Bybit Linear USDT Futures grid
## Bybit Linear USDT product boundary

- Public Bybit REST client принимает только `category=linear`; другой category отклоняется до сетевого запроса.
- Symbol-specific market-data/metadata calls принимают только символы `*USDT`; non-USDT symbols не попадают в ticker/kline/funding/open-interest/instrument-info path.
- Если upstream/stub возвращает список без точного совпадения `symbol`, строка отбрасывается: collector не должен присваивать чужую цену, funding или metadata запрошенному контракту.
- Broad ticker fetch дополнительно фильтруется по `*USDT`, потому что продуктовый scope сервиса уже API-scope Bybit `linear`: рекомендации строятся только для USDT perpetual.
- HTTP 2xx не является достаточным доказательством успешного Bybit V5 ответа: `retCode` обязан присутствовать и быть exact integer. Только `retCode=0` допускает чтение `result`; missing/boolean/fractional/malformed control value повторяется как response-shape error и после исчерпания retry блокирует цикл.
- Kline/open-interest request `limit`, `start/end` и `startTime/endTime` нормализуются только из exact integers. Отрицательные или инвертированные временные окна, boolean и fractional значения отклоняются до REST-запроса, чтобы collector не строил историю по усечённым границам.
- Funding в risk/recommendation payload теперь хранит `directional_funding_bps_per_event`; legacy alias `directional_funding_bps_8h` не использовать для новой логики.

## Важная граница

Несмотря на терминологию `bot_instance`, проект не является реальным grid execution engine.
Он формирует рекомендации для запуска бота оператором и ведёт audit-контур вокруг этого решения.

## Поддерживаемые рекомендации
- `futures_grid` только для Bybit `category=linear`, USDT perpetual. Расчёты PnL/margin/funding ведутся в USDT по linear-модели.

## Разрешённые направления
- `futures_grid`: `neutral`, `long`, `short`

## Режимы, которые система считает поддержанными
- `futures_grid`: `venue=linear`, `account_mode=unified`, `margin_mode=isolated`

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
7. Если net profit per grid после fees/spread/slippage/funding <= 0 или слишком тонкий, рекомендация получает блок `GRID_NET_PROFIT_*` и не должна запускаться. Минимальный шаг grid, score и expected RR рассчитываются от execution-cost плюс неблагоприятный expected funding carry; положительный funding receipt не уменьшает шаг, не повышает score/RR и не засчитывается как approval-edge. Если `next_funding_ts` отсутствует, recommendation и execution-preflight консервативно считают возможные funding events по горизонту и interval вместо предположения «funding не будет» или «будет только один event».
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
- для supported execution-path обязательно присутствует явный `margin_mode=isolated`, иначе recommendation блокируется fail-closed;
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

## Что outcome labeling умеет и чего не умеет

### Умеет
- учитывать grid spacing, cost floor и adverse funding-carry; funding receipt не кредитуется как durable edge для calibration;
- сохранять точный момент доступности proxy-label (`label_available_ts = entry_ts + effective_horizon`) и использовать purged chronological OOF: train-label обязан быть полностью известен строго до первой validation-рекомендации; legacy labels без точного availability timestamp исключаются из OOF train;
- штрафовать break-out, kill-switch breach и плохую occupancy range;
- применять fail-closed precedence: любой breach нижнего или верхнего `kill_switch` делает proxy outcome неуспешным и не позволяет отдельному `tp_per_leg` touch повысить label;
- считать success по факту достижения per-leg TP только если TP-touch остаётся net-positive после execution-cost floor, либо по oscillation proxy.

### Не умеет
- реконструировать реальные fill sequence;
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

Multi-timeframe aggregate считается валидным при минимум трёх TF и весовом покрытии не менее 40%. В publication gate требуется `mean_reversion_score >= 0.55`; иначе recommendation получает `MEAN_REVERSION_EDGE_UNCONFIRMED`. При недостатке истории применяется `MEAN_REVERSION_EVIDENCE_INSUFFICIENT`. Эти блокировки fail-closed и не могут быть отменены высоким legacy range score, LLM verdict или raw confidence.

Threshold был sanity-checked на детерминированной Monte-Carlo выборке: среди 200 IID paths gate пропускает не более одного, тогда как для материально anti-persistent AR(1), `phi=-0.35`, пропускается не менее 150. Это unit-level discriminative check, а не оценка live profitability. Bid/ask bounce и transient anti-persistence могут быть неисполняемыми после costs, поэтому положительный score остаётся только предварительным evidence.

Feature/calibration identity изменена: текущая recommendation model — `bybit-taxonomy-v3-mean-reversion`, а logistic/Platt keys имеют v4. Для fit принимаются только outcomes текущей модели с явным `mean_reversion_evidence_valid=1` и finite `mean_reversion_score`; legacy outcomes не используются даже для score-only fallback.

Поле `expected_rr` исторически вычисляется как bounded capture-to-volatility heuristic. Оно не использует точную monetary loss distribution и не является каноническим reward:risk. UI поэтому показывает «Прокси capture/risk» / «Прокси C/R».

## Семантика score и confidence

Launch-score для `futures_grid` оценивает прежде всего пригодность режима для сетки: range suitability, trend/ATR penalties, multi-timeframe coherence, execution costs и adverse funding. В raw-режиме `confidence` является ограниченным нелинейным отображением того же эвристического score с дополнительными penalties за неполный контекст; это не независимая вероятность прибыли. Только bot-specific fitted calibrator добавляет статистический слой, но его target остаётся proxy-outcome, а не фактический биржевой net PnL. Поэтому ни raw, ни calibrated confidence не доказывают live edge без отдельной walk-forward/shadow проверки по реальным fills и costs.

## Exact-evidence strategy-health stop gate

Operator execution preflight не ограничивается проверкой текущего payload. Перед materialization `bot_instance` он читает stopped bots с immutable execution evidence и строит три newest-first cohort: `(venue, bot_type, symbol, direction)`, symbol-wide и portfolio-wide. Один `publication_root_rec_id` учитывается не более одного раза, поэтому repeated updates одной signal chain не создают ложную статистическую мощность. При explicit `model_version` используются только результаты той же версии; новая модель не наследует блок старой. Long/short/neutral не смешиваются в directional cohort.

Fail-closed коды:

- `LIVE_VALIDATION_DIRECTION_LOSS_STREAK`: пять последних независимых stopped bots того же symbol/direction имеют `realized_pnl_net < 0`;
- `LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY`: минимум 8 независимых observations, total и median net PnL отрицательны, positive-bot rate < 50%;
- `LIVE_VALIDATION_SYMBOL_NEGATIVE_EXPECTANCY`: те же условия после минимум 12 observations по символу;
- `LIVE_VALIDATION_PORTFOLIO_NEGATIVE_EXPECTANCY`: те же условия после минимум 20 observations по всему Linear USDT `futures_grid` contour.

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
7. Evidence GET endpoints require admin authorization. The live-validation endpoint is descriptive and always reports that a live-edge claim is unsupported without chronological comparator evidence.
8. External timestamps are stored as UTC seconds; adapters must convert Bybit millisecond fields exactly and reject boolean/fractional timestamps.
