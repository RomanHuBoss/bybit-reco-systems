# Торговая логика Bybit Linear USDT Futures grid
## Bybit Linear USDT product boundary

- Public Bybit REST client принимает только `category=linear`; другой category отклоняется до сетевого запроса.
- Symbol-specific market-data/metadata calls принимают только символы `*USDT`; non-USDT symbols не попадают в ticker/kline/funding/open-interest/instrument-info path.
- Если upstream/stub возвращает список без точного совпадения `symbol`, строка отбрасывается: collector не должен присваивать чужую цену, funding или metadata запрошенному контракту.
- Broad ticker fetch дополнительно фильтруется по `*USDT`, потому что продуктовый scope сервиса уже API-scope Bybit `linear`: рекомендации строятся только для USDT perpetual.
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
- если payload содержит явный sizing (`order_qty`, `qty_per_leg`, `base_qty`, `order_notional` и совместимые алиасы), preflight блокирует значения ниже `min_order_qty`/`min_notional`, выше `max_order_qty` или не кратные `qty_step` (`ORDER_QTY_OFF_STEP`, `ORDER_QTY_BELOW_MIN`, `ORDER_NOTIONAL_BELOW_MIN`). Для base-qty проверка `minNotionalValue` использует минимальную положительную цену из reference/lower/upper основного grid range, потому что Bybit валидирует notional на фактической цене каждого ордера. Если одновременно переданы qty и quote-notional, preflight блокирует внутренне несогласованный sizing как `ORDER_QTY_NOTIONAL_MISMATCH`. Генератор больше не использует фиксированные 25 USDT для всех контрактов: qty округляется вверх по conservative fallback step до live Bybit preflight, чтобы дорогие USDT perpetual вроде BTCUSDT не публиковались заведомо ниже minQty.
- operator-facing `params.operator_sheet.sizing` / `params.operator_sheet.economics` / `params.operator_sheet.leverage` считаются тем же источником исполнимых override-полей, что и `params.sizing`, `params.economics` и `trade_plan`: strict execution-preflight обязан проверять эти значения по Bybit filters, а UI обязан считать размер позиции/маржу из того же fallback-порядка, чтобы оператор не видел непроверенный sizing.

## Linear-USDT PnL, funding и liquidation

- Long PnL: `qty * (exit_price - entry_price)` USDT.
- Short PnL: `qty * (entry_price - exit_price)` USDT.
- Round-trip fee и execution friction вычитаются из каждой сетки до публикации рекомендации.
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
