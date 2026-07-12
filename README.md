## Outcome dependency diagnostics (v1.0.38)

Исправлена MEDIUM-ошибка диагностики outcome worker. В v1.0.37 отсутствие ещё не загруженной settled funding row возвращало тот же `None`, что и действительно повреждённая grid-геометрия, поэтому журнал ошибочно показывал `OUTCOME_SKIP_INVALID_GRID_CONTRACT`. Теперь transient-зависимость записывается как `OUTCOME_WAIT_FUNDING_SETTLEMENT` с точным funding timestamp и текущим inventory; worker автоматически повторит расчёт после backfill. Настоящие конфликты funding/grid contract содержат машинно-читаемый `reason` и подробности. Повтор одинакового сообщения ограничен cooldown, чтобы decision log не заполнялся каждую минуту.

FastAPI version: `1.0.38`. Outcome math не менялась, поэтому `OUTCOME_LABEL_VERSION` остаётся `grid_label_v18`: обновление с v1.0.37 не удаляет уже рассчитанные v18 outcomes или calibrators.

## Settled funding outcome integrity (v1.0.37)

Исправлена HIGH-ошибка исторической статистики: `fundingRate` из ticker является изменяющимся прогнозом следующего funding settlement, но прежний outcome worker использовал его задним числом как фактическую ставку и учитывал только неблагоприятные списания. Это систематически искажало Total P&L, win rate и calibration: SHORT не получал положительный funding, LONG не получал отрицательный funding, а позднее изменившаяся ставка не отражалась в label.

Теперь collector backfill-ит immutable settlement rows из публичного `/v5/market/funding/history` в таблицу `funding_settlement`. Исторический outcome использует только фактически рассчитанную signed rate и реальный inventory на timestamp события. Платежи уменьшают P&L, получения увеличивают P&L. Если schedule указывает funding event, позиция была ненулевой, но settlement row отсутствует, label не создаётся fail-closed. Forecast funding остаётся только approval/risk input. `OUTCOME_LABEL_VERSION=grid_label_v18`; первый запуск v1.0.37 очищает несовместимые proxy outcomes/calibrators, сохраняя recommendations, bot lifecycle, trades и exact execution evidence.

# Bybit Recommender — Bybit Linear USDT Futures grid-only build

## Grid cost-layer separation (v1.0.36)

Исправлена HIGH-ошибка economics/outcome: прежний код вычитал bid/ask spread, slippage и полный ожидаемый funding **из каждой завершённой grid-пары**. Это умножало разовые/временные расходы на число циклов, искусственно расширяло шаг сетки и систематически занижало proxy PnL активных сеток.

Текущий контракт разделяет три слоя:
- `grid_round_trip_fee_bps`: recurring комиссии двух resting Buy/Sell fills; только они уменьшают Bybit Grid Profit каждой завершённой пары;
- `one_time_market_friction_bps` / `market_round_trip_cost_bps`: spread и slippage market setup/terminal liquidation; они не повторяются на каждом grid cycle;
- funding: signed position-time Total P&L, рассчитываемый по фактическому inventory и событиям, а не распределяемый на каждую пару.

Grid spacing, density, `net_profit_per_grid` и live gross/fee coverage теперь используют recurring grid fees. Live spread остаётся отдельным liquidity gate, а market friction, adverse funding и cross-margin stress остаются консервативными launch/Total-P&L проверками. `OUTCOME_LABEL_VERSION=grid_label_v17`; первый запуск v1.0.36 очищает только несовместимые proxy outcomes/calibrators. Исправление устраняет систематический pessimistic bias, но не доказывает наличие live edge.

## Bybit cross-margin safety contract (v1.0.35)

Bybit Futures Grid Bot is modelled as `account_mode=unified`, `margin_mode=cross`, `position_mode=one_way`. A standalone isolated-position liquidation price is not used as a safety oracle. The deterministic gate recomputes a conservative cross-margin equity stress from exact grid commitment, leverage, execution cost and both kill-switch boundaries. Funding receipts and hypothetical grid profits are not credited to the stress buffer. Exact wallet equity, other positions/orders, risk tier and mark-price liquidation remain external executor checks. Legacy isolated-mode payloads are blocked fail-closed.


## Neutral full opening-order commitment (v1.0.34)

Исправлена критическая ошибка sizing/risk/outcome-математики NEUTRAL-сетки. В версиях 1.0.32-1.0.33 ошибочно предполагалось, что в one-way режиме достаточно резервировать только более дорогой из двух первоначальных opening stacks: `max(sum(Buy), sum(Sell))`. Однако NEUTRAL стартует без позиции, а все первоначальные Buy и Sell лимитные заявки являются opening orders и требуют доступной маржи. One-way netting ограничивает максимальную **позицию**, но не делает противоположные первоначальные заявки бесплатными.

Канонический `arithmetic_grid_commitment` теперь разделяет две величины:
- `committed_notional_per_qty = sum(all initial Buy prices) + sum(all initial Sell prices)`;
- `max_abs_position_slots = max(Buy slots, Sell slots)`.

Dynamic bridge topology v1.0.33 сохраняется: N интервалов образуют N+1 цен, но одна bridge-цена пуста, поэтому первоначальных заявок ровно N. Для NEUTRAL committed slots также равны N, а maximum one-way position остаётся размером большей стороны. Recommender, auto-snap, strict preflight, runtime caps и outcome denominator используют один и тот же контракт.

Текущий `OUTCOME_LABEL_VERSION=grid_label_v15`; первый запуск v1.0.34 очищает только несовместимые proxy outcomes/calibrators. Recommendations, bot lifecycle, trades, exact execution evidence и risk settings сохраняются. Исправление делает sizing более консервативным и уменьшает процентную доходность NEUTRAL при том же абсолютном PnL; оно не доказывает наличие live edge.

Сервис собирает рыночные данные Bybit Linear USDT Futures / USDT Perpetual, рассчитывает multi-timeframe признаки и строит рекомендации только для `futures_grid`. Любые другие классы ботов и стратегий в этой сборке не поддерживаются. Дополнительно может подключаться локальный LLM-reviewer по свечам; полный журнал решений и состояний хранится в выбранном backend: SQLite или PostgreSQL.

Проект рассчитан прежде всего на **операторский / полуавтоматический контур**: система формирует интерпретируемую рекомендацию, показывает причины, ограничения и риск-контекст, а оператор уже принимает решение о запуске бота на бирже.

Карточка конкретной рекомендации привязана к immutable `rec_id`: кнопка обновления перечитывает именно выбранную audit-row. Новые публикации по тому же символу, включая `no_trade` или смену направления, показываются отдельно в истории и не должны незаметно подменять открытую карточку.

## Dynamic off-grid bridge topology (v1.0.33)

Исправлена критическая ошибка initial-order topology для reference между arithmetic grid levels. `grid_count=N` по-прежнему означает N интервалов и N+1 ценовых уровней, но Bybit dynamic mode создаёт ровно N начальных ордеров: один соседний pivot/bridge level остаётся пустым до исполнения прилегающей заявки. Версия 1.0.32 ошибочно размещала ордер на всех N+1 уровнях.

Ошибка завышала active orders, committed capital, margin, worst-case exposure и initial directional inventory; одновременно outcome-ledger создавал fills на bridge-уровне, где исходной заявки быть не должно. Канонический `arithmetic_grid_commitment` теперь возвращает `idle_grid_index`, а recommender, auto-snap, strict preflight, runtime caps, daily-loss guard и outcome используют одну dynamic topology.

Для NEUTRAL/LONG между уровнями idle bridge — ближайший верхний уровень; для SHORT — ближайший нижний. После исполнения соседней заявки replacement-order может появиться на bridge level. Dynamic bridge topology сохраняется, но утверждение v1.0.32 о финансировании только более дорогого directional opening stack отменено v1.0.34: для NEUTRAL резервируются все первоначальные opening orders обеих сторон, а maximum position по-прежнему равна большей стороне.

В v1.0.33 использовался `OUTCOME_LABEL_VERSION=grid_label_v14`; текущий контракт v1.0.34 — `grid_label_v15`. Первый запуск новой версии очищает только несовместимые proxy outcomes/calibrators. Recommendations, bot lifecycle, trades, exact execution evidence и risk settings сохраняются. Исправление устраняет систематическое искажение sizing/outcomes, но не доказывает live edge.

## Exact grid commitment and path-ambiguity integrity (v1.0.30)

Исправлен подтверждённый слой sizing/outcome-математики. `grid_count` в Bybit arithmetic grid означает число ценовых интервалов, поэтому сетка содержит `grid_count + 1` ценовых уровней. В v1.0.30 ошибочно предполагалось, что между уровнями активны все `N+1` уровней. Это историческое допущение superseded v1.0.33: один pivot/bridge level остаётся idle, поэтому начальных ордеров всегда `N`. Для directional mode капитальное обязательство состоит из исходной позиции плюс adverse-side opening orders, а не из условного `reference × grid_count`.

Recommender, auto-snap, strict preflight, runtime risk caps и outcome denominator теперь используют один канонический helper. Это устраняет занижение required margin/worst-case notional и завышение proxy return; при двух интервалах ошибка могла достигать 50%. Конфликт старых полей `estimated_active_orders=N` или `estimated_total_order_notional=N×reference×qty` блокируется fail-closed.

Для свечи с верхним и нижним intrabar excursion worker независимо моделирует допустимые пути `O→H→L→C` и `O→L→H→C`. Label создаётся только если cash, inventory, fees, resting orders, stop state и terminal PnL совпадают. Если порядок меняет результат — outcome unavailable, а не произвольно выбранная прибыль/убыток. В v1.0.30 использовался `OUTCOME_LABEL_VERSION=grid_label_v11`; первый запуск этой версии очищал только несовместимые proxy outcomes/calibrators, сохраняя recommendations, bot lifecycle, trades, exact execution evidence и risk settings. Исправление не доказывало live edge.


## Grid ledger topology and protective stop finalization (v1.0.29)

Исправлен шестой подтверждённый слой outcome-математики. Если LONG/SHORT входит между двумя arithmetic grid levels, ledger теперь создаёт исходный directional slot и ближайший TP-order на соседнем уровне. Ранее ближайший уровень пропускался: у LONG возле верхней границы и SHORT возле нижней границы могло не оказаться ни исходной позиции, ни первого TP, поэтому реальное направленное движение записывалось как ноль.

Minute path больше не схлопывается в один `previous close -> current close`. Отдельно учитываются наблюдаемые `previous close -> current open` и `open -> close`; односторонний OHLC excursion учитывается, когда его порядок однозначен. Двусторонний intrabar order по-прежнему не выдумывается из OHLC.

Kill-switch теперь является terminal event: ledger обрабатывает fills только до защитной границы, закрывает остаточный inventory на ней и не начисляет последующие grid trades/funding. Отсутствующий kill-switch, граница внутри grid range или одновременное касание обеих защитных границ в одной неоднозначной свече делают proxy label unavailable.

Текущий `OUTCOME_LABEL_VERSION=grid_label_v10`. Первый запуск v1.0.29 очищает только несовместимые proxy outcomes и связанные calibrators; recommendations, bot lifecycle, trades, exact execution evidence и risk settings сохраняются. Это исправляет расчёт, но не доказывает наличие live edge.

## Post-publication entry and persisted grid-contract integrity (v1.0.28)

Исправлен пятый подтверждённый слой outcome-математики. Proxy-entry теперь берётся по open первой точной минутной свечи, которая начинается **после фактической публикации рекомендации**, а не автоматически по `features_ref_ts + 60`. Если recommender завершил цикл позже, уже открывшаяся свеча не используется задним числом. Это исключает невозможный pre-publication fill и look-ahead в entry price.

Outcome worker больше не превращает повреждённый или противоречивый grid-план в искусственный `ret=0, success=0`. Конфликтующие `grid_count/grid_levels`, несовпадающие валидные range aliases, malformed explicit range, а также конфликтующие или повреждённые funding aliases делают label unavailable и записывают диагностический skip. Worker не выбирает «более удобную» геометрию и не конструирует funding-модель из полей разных блоков.

Текущий `OUTCOME_LABEL_VERSION=grid_label_v9`. Первый запуск v1.0.28 очищает только несовместимые proxy outcomes и связанные calibrators; recommendations, bot lifecycle, trades, exact execution evidence и risk settings сохраняются. Изменение не доказывает прибыльность: оно устраняет temporal leakage и fabricated labels.

## Outcome label integrity and exact funding-window precedence (v1.0.27)

Исправлен четвёртый подтверждённый слой outcome-математики. `success` теперь строго следует liquidation-equivalent total net PnL: любое finite `ret > 0` является win, если kill-switch не нарушен. Удалён оставшийся скрытый `mode activity`/`0.1% drift` gate, из-за которого небольшая прибыль LONG/SHORT и положительный остаточный PnL NEUTRAL записывались как проигрыши при положительном `ret`.

Точный funding schedule теперь имеет приоритет над aggregate estimate. Если `next_funding_ts` и interval подтверждены, но внутри label horizon нет события, funding равен нулю даже при устаревшем `expected_funding_events=1`. Fallback по expected events используется только когда точный schedule действительно отсутствует.

Дублирующие `params.cost_model` и `trade_plan.cost_model` разрешаются fail-closed по максимальному валидному execution cost: нулевой, boolean или повреждённый alias больше не может скрыть более строгую стоимость. Malformed OHLCV row делает horizon incomplete и не превращается в fabricated loss. В версии v1.0.27 использовался `OUTCOME_LABEL_VERSION=grid_label_v8`; её первый запуск очищал только несовместимые proxy outcomes/calibrators, сохраняя recommendations, bot lifecycle, trades и exact execution evidence.

## Inventory-aware horizon finalization and funding (v1.0.26)

Исправлен третий подтверждённый слой outcome-математики. На границе label horizon остаточная LONG/SHORT-позиция теперь приводится к единой liquidation-equivalent net basis: к mark-to-market добавляется недостающая выходная половина round-trip execution cost. До v1.0.26 два одинаковых результата с закрытой и незакрытой позицией сравнивались на разных cost bases, а остаточный inventory выглядел лучше на величину exit fee/slippage proxy.

Funding больше не вычитается плоским процентом от капитала всей сетки. Worker применяет adverse funding только к фактическому net inventory в момент подтверждённого funding event. Neutral без позиции не платит funding; LONG/SHORT платит пропорционально position value, а возможное получение funding по-прежнему не кредитуется как alpha. Если точный schedule отсутствует, используется консервативный fallback по максимальному adverse inventory, реально достигнутому ledger, а не по полному grid capital.

`success` теперь следует заявленному контракту total net PnL: любое конечное положительное значение выше численной погрешности может быть win при наличии mode activity и без kill-switch breach. Скрытый порог `5 bps` удалён, потому что он делал положительные outcomes проигрышами и расходился с `ret`. Из-за несовместимой target-семантики текущий `OUTCOME_LABEL_VERSION=grid_label_v7`; первый запуск v1.0.26 очищает только прежние proxy `reco_outcomes` и связанные calibrators, сохраняя recommendations, bot audit lifecycle, trades и exact execution evidence.

## Exact arithmetic-grid ledger and exchange interval economics (v1.0.25)

Исправлен второй, более глубокий слой математических ошибок в рекомендации и OHLCV proxy-outcome. Завершённая arithmetic-grid пара теперь получает полный соседний ценовой интервал: коэффициент `fill_efficiency=0.70` больше не уменьшает PnL уже состоявшейся сделки и используется только как отдельный сценарный показатель `projected_capture_bps`. `tp_per_leg`, `gross_profit_bps`, cost-floor и live economics теперь относятся к одной и той же канонической геометрии `(upper-lower)/grid_count`.

Outcome worker больше не заменяет путь заявок формулой `completed_steps + end_drift`. Он строит равноколичественный ledger уровней для LONG, SHORT и NEUTRAL, учитывает исходную directional-позицию, каждое пересечение уровня между последовательными close, replacement order, цену фактического grid-fill, исполненную половину round-trip cost и mark-to-market оставшейся позиции на границе horizon. Один прибыльный neutral pair уже является валидной grid-активностью; directional mode может получить положительный total PnL через закрытие исходной позиции. Kill-switch breach по-прежнему делает label неуспешным.

Изменение несовместимо с предыдущей target-семантикой, поэтому текущий `OUTCOME_LABEL_VERSION=grid_label_v6`. Первый запуск v1.0.25 удаляет только старые proxy `reco_outcomes` и связанные calibrators. Recommendations, bot audit lifecycle, trades и exact execution evidence сохраняются. Модель остаётся консервативным close-to-close OHLCV proxy: она не доказывает intrabar sequence, queue priority, partial fills, live fee tier или прибыльность.

## Grid outcome accounting and outcome cohorts (v1.0.24)

Исправлена систематически заниженная OHLCV proxy-статистика arithmetic futures grid. `grid_count` теперь используется как число одновременно финансируемых ценовых интервалов и знаменатель капитала, но не как потолок общего числа завершённых сделок за horizon: одна и та же сетка может закрываться повторно после replacement order. Для каждого подтверждённого matched crossing gross proxy равен полному arithmetic interval, после чего один раз вычитается round-trip execution cost.

Neutral grid начинается без позиции: если не было завершённого пересечения и не возникло остаточного inventory, proxy-return равен нулю, а не фиктивному убытку от комиссии и полного движения цены. Для directional LONG/SHORT благоприятное движение теперь добавляет, а неблагоприятное вычитает mark-to-market PnL только на оценочную долю оставшегося inventory. Первый candle move считается от фактического entry/open, а не от его close.

`GET /api/v1/outcomes/stats` сохраняет общую исследовательскую выборку, но дополнительно возвращает отдельные `cohorts.actionable` и `cohorts.shadow_no_trade`. Главные карточки UI показывают actionable-когорту; общая и shadow-статистика остаются отдельным research/control контуром и не выдаются за результаты запускаемых идей.

Для исторической версии v1.0.24 изменение было несовместимо с прежней target-семантикой и использовало `OUTCOME_LABEL_VERSION=grid_label_v5`. При первом запуске сервис штатно очищает только старые `reco_outcomes` и связанные calibrators; recommendations, bot audit lifecycle, trades и exact execution evidence сохраняются. Proxy outcome по-прежнему не реконструирует реальную очередь заявок, partial fills или live PnL.

## Temporal data lineage и calibration sample (v1.0.23)

Биржевое время тикера берётся из подтверждённого Bybit V5 response envelope и сохраняется до freshness-gate. Локальное время получения ответа больше не подменяет неизвестное/устаревшее event time. Kline допускается только при exact-integer millisecond timestamp, кратном секунде и запрошенному timeframe; сдвинутые, boolean и fractional timestamps не округляются в допустимую свечу. Feature layer повторно отклоняет boolean/fractional timestamps.

Proxy-outcome строится только при наличии точной следующей 1m-свечи после `features_ref_ts`, непрерывной минутной последовательности на всём горизонте и свечи ровно на `label_available_ts`. Gap не переносит гипотетический вход или выход на более поздний рынок. Calibration использует только строки с exact `label_available_ts`, который не раньше recommendation timestamp и уже наступил к моменту fit; legacy/malformed/future labels исключаются.

Строгий temporal contract был введён в `grid_label_v4`; v1.0.24 использовала `grid_label_v5`, v1.0.25 использовала `grid_label_v6` для явного arithmetic-grid ledger, v1.0.26 использовала `grid_label_v7` для inventory-aware funding/finalization, v1.0.27 использовала `grid_label_v8` для sign-consistent success, exact funding-window precedence и fail-closed cost aliases, v1.0.28 использовала `grid_label_v9` для post-publication entry и strict persisted-contract integrity, v1.0.29 использовала `grid_label_v10` для directional order topology, observable endpoint path и terminal kill-switch accounting, v1.0.30 использовала `grid_label_v11` для exact capital commitment и path-ambiguity integrity, v1.0.31 использовала `grid_label_v12` для quantity-aware resting-order ledger и gap-stop integrity, v1.0.32 использовала `grid_label_v13` для ошибочной max-side neutral commitment, v1.0.33 использовала `grid_label_v14` для dynamic off-grid bridge topology, а текущая v1.0.34 использует `grid_label_v15` для полного резервирования всех первоначальных NEUTRAL opening orders. При первом запуске version guard сбросит только несовместимые proxy outcomes и calibrators, сохранив recommendations, bot audit lifecycle, trades и exact execution evidence. Temporal fix и accounting fix не превращают OHLCV proxy в доказательство live edge.

## No-recommendation state, status semantics and shadow outcomes (v1.0.22)

Отсутствие `recommended/active` не является ошибкой само по себе. Если текущие кандидаты не подтверждают торговый тезис, сервис обязан показать `no_trade`, а не создавать рекомендацию ради заполнения таблицы. `MEAN_REVERSION_EDGE_UNCONFIRMED` теперь относится именно к `no_trade`: evidence вычислен, но его недостаточно для запуска. `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` остаётся жёстким `blocked`, потому что обязательные данные отсутствуют.

Необученный bot-specific calibrator также не является самостоятельным блокером. До fit интерфейс показывает `raw` confidence; deterministic data/risk/economics gates продолжают работать независимо.

Чтобы длительный период без запусков не останавливал исследовательский контур, новые `no_trade`-кандидаты без hard blocks и с полным trade plan получают явный `outcome_policy.sample_role=shadow_no_trade`. После созревания horizon они размечаются как counterfactual proxy outcomes и могут участвовать в calibration. Hard-blocked, malformed, pending и неявные legacy `no_trade` строки в эту выборку не попадают. В журнале outcomes shadow и non-shadow roots показываются раздельно; ни один proxy outcome не называется фактическим биржевым исполнением.

## Outcome и дневной risk budget (v1.0.21)

Proxy-outcome теперь оценивает результат в единицах капитала всей сетки, а не одной заявки: прибыль и execution-cost завершённых grid-leg нормируются на подтверждённый `grid_count`. Отдельное касание directional `tp_per_leg` больше не считается доказательством прибыли всего grid, потому что OHLCV не показывает queue priority, фактическую закрытую позицию и остаточный inventory. Положительная метка требует завершённых двусторонних осцилляций, положительного net proxy и сохранённого kill-switch.

Семантика outcome изменена несовместимо с прежней calibration sample, поэтому `OUTCOME_LABEL_VERSION` повышен до `grid_label_v3`. При первом запуске версии 1.0.21 сервис штатно удалит старые `reco_outcomes` и связанные calibrator keys, после чего накопит новые метки. Это намеренный data reset, а не потеря execution evidence или рекомендаций.

Execution preflight дополнительно оценивает консервативный loss от reference price до adverse kill-switch по максимальному position notional и explicit execution costs. Запуск блокируется кодом `DAILY_LOSS_BUDGET_EXCEEDED`, если эта оценка превышает остаток `max_daily_dd_usdt - daily_dd`. Дневной лимит теперь ограничивает не только уже реализованный drawdown, но и риск нового запуска.

Эти исправления устраняют подтверждённое завышение proxy-return и fail-open дневного риска, но не доказывают положительное математическое ожидание стратегии.

## Независимое подтверждение диапазонного режима

До версии 1.0.20 диапазонный score в нескольких местах фактически сводился к `1 - trend_strength`. Это логически недостаточно: отсутствие тренда совместимо и с возвратным диапазоном, и с driftless random walk. У второго до издержек нет доказанного положительного ожидания самофинансируемой grid-стратегии, а комиссии, spread, slippage и adverse selection делают ожидание отрицательным.

Теперь actionable `futures_grid` требует независимого multi-timeframe evidence возвратности:

- отрицательной lag-1 автокорреляции доходностей;
- variance ratio на четырёх шагах ниже random-walk benchmark;
- повышенной частоты смены знака доходности;
- валидного evidence минимум на трёх закрытых timeframes с достаточным весовым покрытием.

Если данных недостаточно, публикуется `MEAN_REVERSION_EVIDENCE_INSUFFICIENT`. Если агрегированный `mean_reversion_score < 0.55`, публикуется `MEAN_REVERSION_EDGE_UNCONFIRMED`. Низкий trend score сам по себе больше не является grid edge. Порог является консервативным safety gate, а не доказательством прибыльности: microstructure bounce, regime shift и execution costs всё ещё должны проверяться walk-forward/shadow статистикой по фактическим fills.

Модель имеет новую audit identity `bybit-taxonomy-v3-mean-reversion`. Старые calibration coefficients и outcome features с семантикой `range = 1 - trend` не смешиваются с новой выборкой. Поле `expected_rr` сохранено для API-совместимости, но в UI отображается как **прокси capture/risk**: это эвристика ранжирования, не фактическое отношение прибыли к убытку.

## Фактическое исполнение и realised PnL

Проект по-прежнему не выставляет ордера. Внешний read-only execution/reconciliation adapter может передавать в защищённый endpoint `/api/v1/bots/{bot_id}/execution-evidence` два типа immutable events:

- `bybit_execution`: отдельный fill с `execId`, `orderId`, side, qty, `execPrice`, `orderPrice`, gross `execPnl` и signed fee; несколько fills одного order сохраняются отдельными строками;
- `bybit_transaction_log`: отдельный signed funding cashflow с уникальным transaction id.

Каждое событие напрямую связано с исходным `rec_id`. Для execution event дополнительно требуется timestamped benchmark (`pre_submit_mid`, `pre_submit_opposite` или `decision_reference`), относительно которого рассчитывается adverse fill deviation. Этот показатель является диагностикой исполнения. Поскольку gross PnL уже рассчитан по фактическим fill prices, канонический realised net PnL равен `gross_pnl + funding - fee`; slippage повторно не вычитается.

Точный evidence-ledger и legacy `/trades` нельзя смешивать для одного `bot_id`. Risk/drawdown/cooldown используют единый поток с приоритетом exact evidence, а endpoints чтения evidence защищены `ADMIN_API_KEY`. `/api/v1/validation/live-evidence` формирует только descriptive dataset и не доказывает live edge.

Execution preflight теперь использует этот exact-evidence контур как **операционный stop gate**. Новое подтверждение `executed` блокируется для конкретного `(symbol, direction)` после пяти последовательных независимых убыточных остановленных ботов либо после восьми независимых наблюдений, если одновременно отрицательны total и median net PnL, а доля прибыльных запусков ниже 50%. Более широкие stop-условия применяются после 12 наблюдений по символу и 20 по всему `futures_grid`-контуру. Повторные публикации одного `publication_root_rec_id` не увеличивают выборку; при заданном `model_version` учитывается только evidence той же версии модели, чтобы старая стратегия не блокировала явно новую. Это консервативная защита от продолжения доказанно убыточного режима, но не статистическое доказательство alpha.

## Поддерживаемые bot_type
- `futures_grid` — только Bybit `category=linear`, USDT perpetual, settlement/margin/PnL в USDT.

Неподдерживаемые стратегии не должны появляться в API, UI, тестах или конфигурациях. Если legacy/manual payload содержит иной `bot_type`, несовместимый `venue` или malformed symbol вроде `BTC/USDT`, он фильтруется/блокируется fail-closed.

## Что делает система
- собирает только `linear` тикеры и OHLCV по USDT perpetual символам; public Bybit client отклоняет нецелевой category/symbol до сетевого запроса, фильтрует exact-symbol responses и не пропускает delivery/pre-market ticker rows в recommendation контур;
- принимает Bybit V5 response как успешный только при присутствующем exact-integer `retCode=0`; отсутствующий, boolean, fractional или иной malformed `retCode` считается retryable response-shape error, а не успешными market data;
- перед REST-запросом проверяет exact-integer `limit` и неотрицательные millisecond `start/end` для kline/open-interest; boolean, fractional, negative и инвертированные временные окна отклоняются без сетевого вызова;
- собирает `funding rate`, `fundingIntervalHour` и `open interest` для perpetual linear;
- ведёт эвристический sentiment pipeline (`global`, `symbol`, `topic` scopes);
- определяет direction/regime на нескольких ТФ;
- считает score / confidence / expected RR / risk score;
- UI `Ранг в выборке` показывает не «точный рейтинг», а grouped percentile: близкие raw-score внутри material delta `0.025` объединяются в near-tie band и получают одинаковый averaged percentile/grade, чтобы 0.245/0.242/0.232 не выглядели как 100/50/0;
- применяет risk-gate, publication-gate, market shock guard и symbol fast-veto; для `futures_grid` actionable-публикация требует двух разных последовательно закрытых evidence snapshots (`features_ref_ts`), а повторные циклы на одной и той же свече не считаются подтверждением;
- при необходимости отправляет кандидат в локальный LLM-reviewer;
- перед operator-confirmation повторно проверяет risk limits, остаток дневного loss budget относительно консервативного kill-switch loss, exact-evidence strategy-health stop gate, свежесть market-data, актуальный market shock / fast-veto, live-price относительно сохранённого диапазона сетки, текущий best bid/ask spread и net edge после live execution costs, а также базовую исполнимость trade plan относительно metadata инструмента Bybit;
- сохраняет рекомендации, решения, outcome-labeling, calibration state, trade history и risk limits в SQLite или PostgreSQL;
- отдаёт REST API и операторский UI.

## Поведение при перезапуске на существующей БД
При штатном рестарте сервис больше не должен выполнять тяжёлый исторический repair всей таблицы рекомендаций только потому, что в БД накопилось несколько дней данных.

На старте теперь делаются только:
- schema/bootstrap операции;
- дешёвые проверки на наличие legacy-строк без materialized publication lineage;
- точечный backfill только если такие строки действительно найдены.

Глубокий исторический retrofit `repair_async_llm_pending_publication_chains()` оставлен как отдельная maintenance-операция и не запускается автоматически при каждом `python main.py`. Это сделано сознательно, чтобы обычный перезапуск не превращался в долгий full-scan/replay на живой БД.

## Архитектурный принцип
Система разделяет несколько слоёв:

1. **Data layer** — сбор и нормализация рыночных данных.
2. **Inference layer** — признаки, direction aggregation, regime, scoring.
3. **Control layer** — risk gate, shock guard, publication gate, LLM reviewer.
4. **Audit layer** — `recommendations`, `decision_log`, `bot_instances`, `trades`, `reco_outcomes`.
5. **Operator layer** — UI и API для ручного исполнения и анализа.

Это **не execution engine биржевого уровня** и не полноценный симулятор исполнения. Сервис оценивает пригодность сетапа и его качество, но не заменяет отдельный production-grade execution layer. Ордеры на Bybit из этого проекта не отправляются: `bot_instances` и `trades` отражают операторский / audit-контур, а не живой OMS/EMS.

## Что входит в проект
- сбор Bybit Linear USDT Futures тикеров и OHLCV;
- сбор funding и open interest для Bybit USDT perpetual;
- sentiment pipeline с global и symbol scopes;
- multi-timeframe direction/regime inference;
- scoring + risk gating + calibration;
- outcome labeling для проверки качества рекомендаций;
- операторский UI;
- REST API для рекомендаций, risk status, sentiment, bot lifecycle и trade ingestion;
- persistence layer с decision log и outcome history (SQLite или PostgreSQL);
- краткая инструкция оператора в `docs/instrukciya_operatora_bybit_recommender.docx` и `docs/instrukciya_operatora_bybit_recommender.pdf`;
- операторская инфографика `how_to_trade.png` и её текстовый source-of-truth `docs/HOW_TO_TRADE_INFOGRAPHIC.md`.

## Ограничения дизайна
- рекомендации не являются финансовым советом и не гарантируют доходность;
- grid опасен на трендовом рынке: система обязана уметь вернуть `blocked`/`no_trade`, если range-edge слабый;
- leverage увеличивает риск ликвидации; estimated liquidation buffer в UI — консервативный preflight-сигнал: используется худшая дистанция из reference price и adverse range/kill-switch boundary, но это всё ещё не точная формула биржи;
- комиссии, spread, slippage и funding могут полностью уничтожить прибыль на сетку;
- sentiment pipeline остаётся **эвристическим**, а не newsroom/LLM/NER-уровня;
- отсутствие sentiment-данных трактуется как неопределённость, а не как «истинный neutral»;
- grid outcomes остаются приближённой path-approximation, а не биржевой truth-моделью исполнения;
- в proxy outcome пробой любого `kill_switch` имеет приоритет над касанием directional `tp_per_leg`: остановленный grid не может стать положительной меткой для calibration из-за отдельного TP-leg; malformed/fractional `recommendations.ts` и `features_ref_ts` не усекаются, а исключаются из labeling;
- risk limits начинают полноценно отражать реальность только если в `trades` действительно пишутся realized fills / PnL / fee;
- локальный LLM-reviewer — это **консервативный reviewer поверх движка**, а не замена scoring/risk/calibration;
- проект не предназначен для немедленного запуска на полный объём капитала без staging-прогона.
- текущая технология не доказывает live alpha: launch-score в основном оценивает пригодность рыночного режима для grid, а outcome/calibration используют proxy-разметку без реальной очередности fills, partial fills и live fee/slippage distribution. До положительной walk-forward/shadow статистики по фактическим исполнениям систему следует считать генератором проверяемых гипотез, а не доказанной прибыльной стратегией.

## Операторский профиль 100-500 USDT

`how_to_trade.png` является быстрым регламентом для малого счёта, но не заменяет backend preflight. Текущая синхронизированная модель:

- проект - recommendation/audit service, а не OMS/EMS: он не выставляет реальные ордера на Bybit;
- поддерживается только `futures_grid` для Bybit Linear USDT Perpetual, `account_mode=unified`, `margin_mode=cross`, `grid_type=arithmetic`;
- shipped risk profile: 1 running bot на счёт, daily DD 10 USDT, cooldown 90 min, max position notional 500 USDT, max margin per bot 100 USDT и интервал `min_leverage=3`, `max_leverage=5`; эти же значения встроены в код и действуют даже без скопированного `.env`;
- если оператор задаёт `max_leverage < 5`, это трактуется как более строгий risk cap внутри или ниже диапазона 3-5x, а не как обещание, что каждая идея станет исполнимой;
- любой `critical`/`blocking` preflight, `INVALID_MARKET_REFERENCE_PRICE`, устаревшая publication-chain, цена вне range/kill-switch, отсутствующая валидная пара bid/ask, live spread > 14 bps, пересчитанный net edge < 2 bps, неподтверждённый funding/minNotional/qtyStep или отсутствие OK LLM-gate при включённом reviewer означает `NO TRADE`.

## Как читать ключевые поля
- `status` — итоговый допуск идеи к рассмотрению.
- `direction` — исполнимое направление для текущего bot_type.
- `confidence` — в режиме `raw` это ограниченная эвристическая функция launch-score, а не вероятность прибыльной сделки; вероятностная интерпретация допустима только при активном bot-specific calibrator и всё равно относится к proxy-outcome, не к биржевому PnL. Не читать изолированно от score, RR, `reasons.confidence_model` и risk context.
- `expected_rr` — консервативный экономический смысл идеи после учёта execution friction и только неблагоприятного funding carry; потенциальное получение funding не повышает RR.
- `score` / `reasons.score_components.economic_cost_bps` — ранжирование также штрафует adverse funding carry; signed funding receipt не превращается в положительный edge и не снижает cost-feature.
- `risk_score` — грубая оценка рыночной/исполнительной сложности.
- `reasons.direction_agg` — агрегированное направление и структура голосов по ТФ.
- `reasons.execution_constraints` — что можно, а что нельзя исполнить на выбранном bot_type.
- `bybit_meta` — metadata инструмента Bybit, доступная UI для операторской сверки диапазона, leverage и шагов.
- `params.grid_type/grid_count` — Bybit Futures Grid Bot geometry: `grid_count` означает число price intervals (“Number of Grids”), а текущая генерация и execution-preflight допускают только `grid_type=arithmetic`; `geometric` блокируется до реализации отдельной геометрической математики. Для arithmetic grid опубликованный `params.grid_spacing_pct` теперь соответствует исполнимой геометрии `(price_range_upper - price_range_lower) / grid_count`; минимальный экономический пол хранится отдельно как `economic_min_grid_spacing_pct`, а `grid_geometry_model` явно фиксирует `bybit_arithmetic_range_width_div_grid_count`. `params.economics` / `reasons.grid_economics` — net-of-fees экономика одной сетки: gross/net bps, estimated execution cost, signed funding impact, funding cost used for approval, excluded funding benefit, estimated order notional, margin required и worst-boundary liquidation buffer. Получение funding не улучшает canonical approval-edge, score, expected RR или outcome labels: оно показывается отдельно как signed diagnostic, потому что funding может измениться или стать расходом при накоплении inventory. Минимальный шаг и плотность grid строятся от recurring комиссий двух grid fills. Spread/slippage относятся к разовой market friction, а adverse funding — к position-time Total P&L и отдельным launch/risk gates; funding receipt не уменьшает spacing, не увеличивает score/RR и не используется как «бесплатный edge». Если net profit per grid не положителен или слишком тонкий, рекомендация блокируется. `reasons.funding.funding_interval_source` показывает, был ли funding interval получен из Bybit ticker/instrument metadata; если `next_funding_ts` недоступен, recommendation и execution-preflight консервативно считают возможные funding events по горизонту, а не предполагают нулевой или single-event carry. Public collector при отсутствии `fundingIntervalHour` в ticker дополнительно берёт interval из instruments-info, а при материальном funding и неизвестном interval рекомендация блокируется fail-closed.
- Временные поля market-data/funding/OI, label horizon и число funding events имеют exact-integer семантику. Значения `5` и `5.0` допустимы как точное целое; boolean, дробные и non-finite значения не усекаются и не округляются. Malformed funding schedule остаётся unknown: при материальном carry рекомендация/execute-preflight блокируется либо используется документированный консервативный unknown-schedule count, но не оптимистический single-event fallback.
- `bybit_plan_validation` — результат execution-time валидации trade plan: ошибки блокируют подтверждение, предупреждения напоминают о неполной проверке qty/min_notional без фактического размера позиции; если `trade_plan.sizing` или `params` уже содержит явный `order_qty`/`qty_per_leg`/`base_qty` либо `order_notional`, эти значения проверяются против Bybit `qty_step`, `min_order_qty`, `max_order_qty` и `min_notional`; для base-qty minNotional проверяется по минимальной цене основного grid range, а не только по reference price, и payload блокируется при существенном расхождении `qty * reference_price` с заявленным `order_notional`. Дополнительно блокируются рекомендации с любым `bot_type` кроме `futures_grid`, любым `venue` кроме `linear`, `reference_price` вне диапазона, внутренним `kill_switch`, схлопыванием сетки после округления по `tick_size`, отсутствующим или неподдерживаемым `margin_mode`, metadata Bybit от другого `symbol` или другого `category/venue`, instrument `status` отличным от `Trading`, несогласованным `grid_count`/`grid_step`, `grid_count > 400`, неподдержанным `grid_type`, off-tick ценами/шагом/`tp_per_leg` в строгом execution-mode, некорректным `leverage` относительно `min/max/leverage_step` Bybit, отсутствующими обязательными Bybit filters (`tickSize`, `qtyStep`, `min/max qty`, `minNotionalValue`, `leverageFilter`), delivery-контрактом вместо perpetual, а также слишком малым worst-side/worst-boundary estimated liquidation buffer при leverage > 1. Legacy/manual payload без `leverage` получает предупреждение и preflight рассматривает его только как 1x; новые рекомендации обязаны хранить явное leverage. Execute-path дополнительно блокирует подтверждение, если текущий ticker уже вышел за сохранённый диапазон сетки или `kill_switch`, либо если свежий ticker не содержит пригодной `last`/`bid`/`ask` live price (`LIVE_PRICE_UNAVAILABLE`), даже при свежих candles/ticker. Для полноценных costed-рекомендаций с `cost_model` валидная пара best bid/ask обязательна: execute-preflight пересчитывает текущий spread/slippage, сохраняет консервативный fee floor и блокирует `LIVE_SPREAD_UNAVAILABLE`, spread > 14 bps, live grid edge < 2 bps или gross interval без запаса 1,10x над recurring grid fees; spread остаётся отдельным liquidity cap, а funding проверяется отдельным inventory/schedule guard. Отдельно повторно проверяются свежий `funding_rate`, `funding_interval_min`; запуск блокируется, если funding стал stale/недоступен, экстремален или ухудшился настолько, что net edge сетки становится неположительным (`FUNDING_RATE_UNAVAILABLE_AT_EXECUTION`, `STALE_FUNDING_RATE`, `FUNDING_EXTREME_AT_EXECUTION`, `FUNDING_EDGE_TURNED_NEGATIVE`). Metadata инструмента теперь берётся только при точном совпадении `symbol` и сохраняет `result.category`, чтобы preflight не валидировал payload ограничениями чужого или нецелевого инструмента. Auto-snap для сгенерированных operator payload расширяет range/kill-switch наружу по `tick_size` и округляет `grid_step`/`tp_per_leg` вверх, чтобы UI/preflight не показывали более узкую и более прибыльную сетку, чем допускает exchange-aligned geometry. Quantity является отдельной risk-boundary: provisional target-notional не округляется вверх по фиктивному step, а при live metadata qty может только округляться вниз; если после этого не выполнены minQty/minNotional, recommendation блокируется вместо автоматического увеличения позиции. Public Bybit client дополнительно блокирует не-`linear` category и non-USDT symbols до REST-запроса, а ticker collector отбрасывает non-perpetual/pre-market rows и не переименовывает чужой `symbol` в запрошенный.
- `bybit_operator_guard` — строгий operator-facing слой поверх `bybit_plan_validation`: если свежая Bybit metadata недоступна или `require_meta=True` выявляет ошибку exchange constraints, API/UI переводят actionable `recommended`/`pending`/`active` в `blocked`, добавляют причины в `blocks`, меняют `params.risk_report.decision` на `not_recommended` и показывают rejection reasons до попытки исполнения.
- `params.risk_report` — операторский риск-отчёт: итоговое решение, conservative/moderate/aggressive profile, net/grid после издержек, funding impact, execution cost, funding interval, required capital, liquidation buffer, adverse scenario, rejection reasons, warnings и approval factors. UI показывает этот блок явно; при `not_recommended`/blocking reasons запуск запрещён до пересчёта.
- `reasons.llm_review` — second opinion LLM, включая источник (`live`, `cache`, `cache_inherited`, `async_live`, `async_inherited`).

## Документация в репозитории
- `docs/ARCHITECTURE.md` — фактическая архитектура, потоки данных и границы ответственности.
- `docs/MODULES.md` — назначение ключевых модулей и их контракты.
- `docs/TRADING_LOGIC.md` — торгово-логические правила, ограничения и жизненный цикл recommendation/publication-chain.
- `docs/SCENARIOS.md` — ключевые эксплуатационные сценарии и expected behavior.
- `docs/KNOWN_RISKS.md` — оставшиеся риски и осознанные ограничения.
- `CHANGELOG.md` — журнал существенных исправлений этой ревизии.

## Быстрый запуск
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

API поднимется на `127.0.0.1:8000`.

## Минимальная проверка после установки
```bash
pip install -r requirements.txt -r requirements-dev.txt
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
PYTHONDONTWRITEBYTECODE=1 python -m py_compile app/*.py main.py
ruff check app tests main.py
```

Эта проверка сознательно разделяет runtime- и dev-зависимости: prod-установка может ограничиться `requirements.txt`, а релизная/аудиторская проверка использует дополнительный `requirements-dev.txt`.

Текущий проверочный baseline этой ревизии:
- исходный full test suite: `833 passed`; post-check: `838 passed`;
- `PYTHONDONTWRITEBYTECODE=1 python -m py_compile app/*.py main.py` — passed;
- `ruff check app tests main.py` — в текущем build-окружении модуль Ruff недоступен; lint-result этой итерации не заявляется;
- `pytest --cov=app --cov-report=term-missing` — запускать в release/dev-контуре при изменениях покрытия;
- `requirements-dev.txt` входит в поставку и фиксирует quality-gate (`pytest`, `pytest-cov`, `ruff`) как часть репозитория, а не как неявную зависимость локального окружения
- регрессионные тесты покрывают collector / hot-vs-backfill separation / Bybit client / health semantics / stale-ticker semantics / long-gap kline catch-up / open-interest pagination / runtime lock loss rollback / heartbeat fail-closed / poisoned historical rows / DB validation / metrics endpoint / bounded-parallel collector soak / sentiment feature compression / bootstrap stage commit / batch ticker fallback / future-poisoned ticker and health paths / dedicated heartbeat connection wiring / transactional rollback для execute-trade-stop API paths / atomic recommender publish rollback / duplicate-trade no-op semantics / latest-operator snapshot selection for non-actionable views / execute-idempotency across one publication-chain / idempotent stop retries without duplicate audit events / rollback on silent-false execute-status transition / rollback on failed stop_bot trade finalization / boot-grace honesty for inherited stale rows / malformed sentiment adapter payloads / poisoned Reddit posts / safe fail-open of `collect_sentiment_once()` / malformed legacy JSON-shapes in recommendation-bot-trade-sentiment APIs / malformed app_config payloads in status and metrics / rejection of blank audit keys for `risk limits version` and explicit `trade_id` / persistence of normalized effective risk limits in bootstrap and mutating API / fail-open fallback from poisoned top-level grid range bounds to valid `trade_plan.levels.range` and `trade_plan.levels.kill_switch` / rejection of `NUL` in sentiment tags and GET-filters / explicit transaction cleanup on idempotent execution paths / sanitization of non-finite `trade_plan` и `cost_model` payloads / correct decomposition of legacy `net_cost_bps` into execution-cost plus funding-carry for outcome-labeling / execution-time preflight по свежести market-data / live-price drift относительно диапазона и kill-switch / live bid/ask spread и net-edge revalidation / market shock / fast-veto / базовой Bybit-валидации сетки / adaptive publication-chain collapse under large duplicate bursts / retry of transient Bybit decode- and protocol-level failures / strict grid-only execution preflight for unsupported bot_type, non-linear venue and off-tick prices/steps/TP / exact-symbol funding ticker / execution-time funding carry preflight / malformed symbol and pre-listing metadata hardening / worst-boundary liquidation buffer / tick-safe operator snapping for range, kill-switch, grid step and TP / запрет funding receipt повышать score, expected RR и outcome labels / запрет net-negative TP-touch повышать outcome success / strict generated-grid geometry mismatch preflight.

## Ключевые env
- `DB_ENGINE` — backend persistence: `sqlite` или `postgresql`;
- `DB_PATH` — путь к основной SQLite БД. Если указан относительный путь, он автоматически разворачивается относительно корня проекта;
- `RUNTIME_LOCK_DB_PATH` — путь к отдельной sidecar-БД runtime lock для SQLite; по умолчанию это `*.runtime_locks.sqlite` рядом с основной БД. Значение обязано отличаться от `DB_PATH`, иначе bootstrap завершится ошибкой конфигурации;
- `DATABASE_URL` — обязательный DSN основной PostgreSQL БД в режиме `DB_ENGINE=postgresql`; теперь он должен быть задан явно, чтобы сервис не пытался молча подключаться к локальному `postgresql://127.0.0.1/...` по unsafe-default;
- `RUNTIME_LOCK_DATABASE_URL` — опциональный отдельный DSN для runtime lock в PostgreSQL-режиме; если не задан, используется `DATABASE_URL`;
- `SYMBOLS_LINEAR` — список только USDT perpetual symbols для `venue=linear`; дубли удаляются, а не-USDT symbols fail-closed отфильтровываются на bootstrap, чтобы нецелевой Bybit payload не попал в сбор и scoring;
- `MIN_SCORE_TO_RECOMMEND`, `MIN_CONF_TO_RECOMMEND` — publish thresholds;
- `FUTURES_COLLECT_INTERVAL_SEC` — интервал обновления funding/open-interest;
- `RISK_LIMITS_JSON` — runtime risk caps; `max_concurrent_bots` и `max_symbol_bots` дополнительно clamp-ятся к product cap 50 Futures Grid Bots, даже если оператор передал большее значение; `min_leverage` задаёт минимальное операторское плечо для actionable futures-grid идей, `max_leverage`, `max_position_notional_usdt` и `max_margin_per_bot_usdt` блокируют публикацию/запуск grid-рекомендаций, если расчётный leverage/notional/margin превышает операторский лимит; shipped-профиль использует интервал `min_leverage=3` и `max_leverage=5`: 3x является базовым actionable минимумом, 4-5x выбираются адаптивно только при более сильной экономике/качестве сигнала; значения ниже 3x должны быть осознанным safety-cap, при котором идеи не становятся actionable автоматически;
- `CALIB_MIN_SAMPLES` — минимум данных для calibration fit;
- `RECO_REPUBLISH_COOLDOWN_SEC` — cooldown для подавления почти идентичных повторных публикаций одной и той же идеи; после этого окна same-direction сигнал всё равно не откроет новый outcome-root, пока предыдущая псевдо-сделка той же chain не доживёт до своего horizon или не получит outcome;
- `OUTCOME_HORIZON_FALLBACK_SEC` — fallback horizon для legacy/неизвестных bot_type;
- `ADMIN_API_KEY` — ключ для mutating endpoints; если ключ пуст, mutating API разрешён только с loopback (`127.0.0.1` / `::1` / `localhost`). Для любого удалённо доступного стенда ключ обязателен;
- `MASTER_KEY` — Fernet-ключ для шифрования секретов. Теперь валидируется fail-fast на старте: битое значение больше не принимается молча;
- `COLLECTOR_MAX_WORKERS`, `FUTURES_COLLECT_MAX_WORKERS` — bounded parallelism for collector REST fetches;
- `RISK_DAY_TZ` — часовой пояс дневной отсечки для daily PnL / drawdown limits (по умолчанию `UTC`);
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional alerts.

### Опциональный локальный LLM-reviewer
Основные настройки:
- `LLM_REVIEWER_ENABLED=1`
- `LLM_REVIEWER_MODE=advisory` или `gate`
- `LLM_REVIEWER_PROVIDER=ollama`
- `LLM_REVIEWER_URL=http://127.0.0.1:11434`
- `LLM_REVIEWER_MODEL=qwen3:8b`
- `LLM_REVIEWER_TFS=15m,1h,4h`
- `LLM_REVIEWER_CANDLES_PER_TF=32`
- `LLM_REVIEWER_MAX_CANDIDATES=24`
- `LLM_REVIEWER_MAX_WORKERS=2`
- `.env.example` синхронизирован с этими runtime-дефолтами; drift между шаблоном env, README и `settings.py` теперь считается регрессией и проверяется тестами.
- `LLM_REVIEWER_MIN_CONFIDENCE=0.65`
- `LLM_REVIEWER_CADENCE_SEC=300`
- `LLM_REVIEWER_PENDING_TIMEOUT_SEC=900` — максимальное время операторского `pending`: если включён LLM-reviewer, `recommended/active` допустимы только после `llm_review.status=ok`; при таймауте pending переводится в `no_trade` fail-closed
- `LLM_REVIEWER_TTL_SEC=` — отдельный TTL валидности LLM-review для повторного использования по тому же `(venue, symbol, bot_type, direction)`; оставьте пустым для auto-режима: по умолчанию не короче TTL самой рекомендации
- `LLM_REVIEWER_KEEP_ALIVE=90s`

Режимы:
- `advisory` — LLM пишет second opinion; пока нет OK-вердикта, actionable-идея удерживается в `pending`, а после OK возвращается к целевому `recommended/active`;
- `gate` — LLM также удерживает идею в `pending`, а уверенное расхождение с `execution_direction` или timeout переводит идею в `no_trade` fail-closed.

Важно:
- При `LLM_REVIEWER_ENABLED=1` операторский запуск запрещён без `llm_review.status=ok`. Новые и legacy-строки со stored `recommended/active`, но без OK-вердикта, API/UI показывают как effective `pending`. Этот hold ограничен `LLM_REVIEWER_PENDING_TIMEOUT_SEC`: затем рекомендация переводится в `no_trade` fail-closed. Для same-direction reuse используется отдельный TTL валидности reviewer-кэша, поэтому `active` остаётся actionable только при свежем OK-cache/review.
- В shipped-профиле reviewer настроен консервативно для локальных GPU уровня RTX 3060: короткий keep-alive, сниженный parallelism и ограниченное число live-кандидатов на sweep.
- UI и API умеют показывать `pending`, `ok`, `error`, `cache_inherited`, `async_live` и другие состояния reviewer.
- После изменения `LLM_REVIEWER_TFS` или `LLM_REVIEWER_CANDLES_PER_TF` старый кэш reviewer больше не переиспользуется автоматически.

## Основные API
### Read-only
- `GET /api/v1/recommendations`
  - по умолчанию схлопывает repeated rows одной `publication_chain` и возвращает только один operator-facing сигнал на `publication_root_rec_id`; при длинной chain API теперь адаптивно расширяет budget сырой выборки, чтобы `top_n` не схлопывался до 1–2 уникальных идей только из-за доминирующего потока `active` updates. Для raw-аудита можно передать `collapse_chains=false`.
- `GET /api/v1/recommendations/{rec_id}`
- `GET /api/v1/risk/status`
- `GET /api/v1/bots`
- `GET /api/v1/bots/{bot_id}`
- `GET /api/v1/trades`
- `GET /api/v1/outcomes/stats`
- `GET /api/v1/health/symbols`
- `GET /api/v1/decisions`
- `GET /api/v1/sentiment`
- `GET /api/v1/status`
  - теперь показывает не только `collector`, но и отдельный `backfill`-контур с его last-cycle/thread state.
- `GET /metrics`

### Mutating (`X-API-Key`, если задан `ADMIN_API_KEY`)
> Для любого окружения с сетевым доступом следует считать `ADMIN_API_KEY` обязательным operational minimum. Если ключ не задан, проект сознательно оставляет mutating endpoints открытыми ради локального/dev-режима.

- `POST /api/v1/recommendations/{rec_id}/action` с `{"action":"executed|ignored","operator":"..."}`
  - для `executed` endpoint теперь делает execution-time preflight и может вернуть `409`, если recommendation устарела по market-data, блокируется текущим market shock / fast-veto, текущий bid/ask spread уничтожил экономический edge или trade plan не проходит базовую Bybit-валидацию.
- `POST /api/v1/bots/{bot_id}/trades`
- `POST /api/v1/bots/{bot_id}/stop`
- `POST /api/v1/risk/limits`
- `POST /api/v1/sentiment`

## Жизненный цикл исполнения
1. кандидат `futures_grid` сначала удерживается в `pending`, пока два разных последовательно закрытых evidence snapshots (`features_ref_ts`) независимо не пройдут score/risk/economics gates; повторный recommender-cycle на той же свече не увеличивает счётчик; после подтверждения новый actionable выпуск публикуется как `recommended`;
2. если same-direction сигнал пришёл, пока предыдущая корневая идея по этому `(venue, symbol, bot_type, direction)` ещё находится внутри своего outcome-horizon, новый `publication_root` не создаётся даже при material-upgrade: запись принудительно сохраняется как `active` в существующей publication-chain, чтобы outcome-labeling имитировал одну открытую псевдо-сделку, а не серию повторных входов;
3. если сигнал повторился уже после закрытия псевдо-сделки, но внутри republish-cooldown и без material upgrade, он тоже сохраняется как `active` в той же publication-chain: запись остаётся исполнимой для оператора, но её lineage указывает на прежний `publication_root_rec_id`, поэтому outcome/calibration считают только корневую публикацию; если предыдущий bot этой chain уже остановлен, новый `execute` обязан создать новый running-бот, а не вернуть старый stopped-instance;
4. если включён `LLM_REVIEWER_ENABLED=1`, actionable grid-рекомендация получает `pending` до `llm_review.status=ok`; без OK-вердикта `recommended/active` в UI/API не показываются как запускаемые, а hold ограничен `LLM_REVIEWER_PENDING_TIMEOUT_SEC`;
5. проигравшие альтернативы по тому же `(venue, symbol)` уходят в `suppressed` с явной причиной в `reasons.suppression`;
6. оператор вызывает `/recommendations/{rec_id}/action` с `executed` для `recommended` или `active`;
7. перед созданием `bot_instance` сервис повторно проверяет текущие риск-лимиты, свежесть candles/ticker, live-price относительно диапазона/kill-switch, текущий best bid/ask spread и net economics, актуальный market shock / fast-veto и базовую Bybit-валидность сетки; instrument metadata Bybit подгружается заранее, вне SQLite write-lock, чтобы медленный upstream не блокировал collector/recommender; при ошибке возвращается `409`, а в `decision_log` пишется `EXECUTION_BLOCKED` или `EXECUTION_PRECHECK_BLOCKED`;
8. если preflight пройден, создаётся `bot_instance`, recommendation переводится в `executed`;
9. realized trades/PnL пишутся через `/bots/{bot_id}/trades`;
10. risk engine использует `bot_instances` + `trades` для cooldown и дневного PnL / DD;
11. бот останавливается через `/bots/{bot_id}/stop` или `stop_bot=true` в trade request.

### Семантика статусов recommendation
- `recommended` — новый actionable сигнал, который прошёл подтверждение на двух разных закрытых evidence snapshots и готов к исполнению;
- `active` — повторно актуальный signal-update внутри уже открытой publication-chain; возникает либо при обычном cooldown-reuse, либо при жёстком same-direction pseudo-position lock до завершения horizon. Исполним, но не считается новым выпуском и не создаёт отдельный outcome-root;
- `pending` — временный gate-hold перед исполнением, включая ожидание второго отличающегося закрытого evidence snapshot; не является `no_trade`, но не исполним до финального `recommended`/`active` либо fail-closed отказа;
- `suppressed` — скрытая альтернатива, проигравшая dedupe/selector и сохранённая только для аудита.

### Инварианты исполнения publication-chain
- в одной `publication_chain` допускается не более одного `running` bot_instance одновременно; этот инвариант теперь удерживается не только логикой API, но и индексом/проверкой на уровне БД;
- в PostgreSQL mutating API дополнительно берут `FOR UPDATE` на целевую recommendation/bot row; это снижает риск потерянного `state_json` при одновременных `trade`/`stop` запросах и гонок статуса между `executed`/`ignored`;
- при гонке двух `execute` для разных членов одной chain второй запрос должен идемпотентно переиспользовать уже созданный running-бот, а не создавать дублирующую позицию;
- если в существующей БД уже обнаружены два `running` bot_instance для одной chain, bootstrap завершится fail-closed с явной ошибкой конфигурационной/исторической целостности.

## Stability notes
- background loops используют runtime lock в выбранном backend; в SQLite это отдельная sidecar-БД, в PostgreSQL — тот же DSN либо отдельный `RUNTIME_LOCK_DATABASE_URL`. Для PostgreSQL захват лидерства теперь выполняется одной atomic UPSERT-операцией, а не парой `SELECT`→`UPDATE`, чтобы исключить split-brain при одновременном старте двух инстансов; operator execute-path больше не держит этот же write-контур на внешнем Bybit fetch, что снижает риск каскадных `database is locked` при деградации сети;
- публичный Bybit REST-клиент ретраит не только обычные timeout/network ошибки и HTTP 429/5xx, но и transient transport/protocol сбои уровня `RemoteProtocolError`, `408` и битые 2xx-ответы с невалидным JSON, которые периодически встречаются за CDN/WAF;
- background loops завершаются по lifespan stop-event и не должны переживать штатный stop/restart процесса как «ложно упавшие» daemon-потоки;
- collector работает с явными stage-boundary commit, а не с одной гигантской write-транзакцией через весь цикл: это осознанный компромисс ради корректного heartbeat и отсутствия скрытого split-brain;
- в SQLite включён `WAL` и увеличенный `busy_timeout`; для PostgreSQL проект использует `psycopg` и совместимый migration bootstrap;
- ошибки одного символа не должны ронять весь collect/recommend loop;
- corrupted JSON в критичных местах читается через safe fallback, а operator/UI-facing payloads дополнительно нормализуются по ожидаемой форме (`dict`/`list`) вместо прокидывания строк/массивов не того типа наружу;
- риск-лимиты и многие env-параметры нормализуются и зажимаются в разумные пределы;
- исторические trade rows с невалидными значениями не должны отравлять daily PnL / drawdown summaries.

## Рекомендации перед live-запуском
Не начинать сразу с полного размера.

Рекомендуемая последовательность:
1. Прогонить сервис на реальном market data без исполнения.
2. Проверить `decision_log`, `risk/status`, `health/symbols`, `outcomes/stats`.
3. Убедиться, что `trades` и `bot_instances` пишутся корректно на тестовом сценарии.
4. Запустить минимальный размер / paper-like режим / ручное подтверждение.
5. Только после этого переходить к рабочему объёму.

## Что особенно проверить оператору
- нет ли частых `COLLECT_ERROR`, `LLM_REVIEW_ERROR`, `STALE_DATA_SKIP`, `SYMBOL_DISABLED`;
- не «залипает» ли `pending` у LLM-reviewer;
- совпадает ли Bybit-форма бота с тем, что показывает панель деталей;
- нет ли в деталях `bybit_plan_validation.errors` или предупреждений о том, что диапазон/шаг сетки не выровнен по ограничениям Bybit;
- не деградирует ли quality score / confidence после накопления новых outcome labels;
- корректно ли отрабатывают risk limits после записи exact execution/funding evidence; не смешивается ли evidence-ledger с legacy `/trades`.

## Инженерные заметки
- используйте внешний process supervisor;
- делайте резервные копии используемого backend: SQLite-файлов или PostgreSQL БД;
- не храните реальные секреты в `.env` внутри репозитория;
- если нужен полноценный execution layer, его нужно строить отдельно от recommendation engine.


## Troubleshooting PostgreSQL bootstrap
- Ошибка `DATABASE_URL is required when DB_ENGINE=postgresql` означает, что выбран PostgreSQL-режим без явного DSN. Задайте `DATABASE_URL=postgresql://...` в `.env`.
- Ошибка `PostgreSQL mode requires installed package 'psycopg[binary]'` означает, что окружение собрано без runtime-зависимостей PostgreSQL. Исправление: `pip install -r requirements.txt`. Если PostgreSQL не нужен, переключите `DB_ENGINE=sqlite`.
