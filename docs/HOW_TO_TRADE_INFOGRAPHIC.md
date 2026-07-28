## v1.5.1 - grid and trend remain active

Both strategies remain available. A journal entry about `trade_journal_ohlcv_mismatch` is an evidence-quality outcome event, not a Bybit order error. From v1.5.1, REST recent trades cannot create that exact-replay mismatch; only complete WebSocket evidence is compared to OHLC.

## v1.5.0 - обе стратегии и новая model lineage

Оператор по-прежнему видит две независимые идеи по каждому symbol: **фьючерсную сетку** и **направленный тренд**. Grid не отключён. Переключение на v1.5.0 не делает идеи автоматически торговыми: изменён ranking contract, поэтому новая lineage должна накопить собственные outcomes.

В Health контролируйте отдельно:

- `recommendation_latest_total` — bounded current snapshot, обычно до `symbols × 2`;
- `recommendation_outcome_root_total` — sparse immutable evidence roots;
- `market_trade_journal` — chronology только для открытых grid windows;
- `calibrator_fitted`, bot-specific sample counts и monetary/terminal gates.

`ranking_score` служит для сортировки. Пока bot-specific calibrator не validated, поле confidence нельзя читать как вероятность выигрыша. Порядок ручного запуска, sizing, preflight и отсутствие автоматической отправки Bybit orders не изменены; бинарная инфографика поэтому сохранена без перерисовки.

## v1.4.8 - как читать funding recovery и intrabar journal

В окне «Здоровье системы» появились два диагностических контура:

- **Funding settlement recovery**: `pending` означает, что обязательный фактический settlement ещё дозагружается; это не отрицательный торговый исход и не доказанная ошибка Bybit. `resolved` означает, что адресная дыра была закрыта.
- **Intrabar trade journal**: основной read-only WebSocket и REST fallback сохраняют публичную chronology; количество строк, spans и разрывов показывает её наблюдаемость. Нулевые gaps не доказывают actual fills; journal используется только как дополнительное evidence для outcome replay.

Оператор не должен удалять БД или outcomes после обновления. Остановите сервис, сделайте backup, замените release и перезапустите. Новые таблицы создаются автоматически. При отключении `MARKET_TRADE_JOURNAL_ENABLED=0` система сохраняет прежнее fail-closed OHLC поведение. `how_to_trade.png` не менялся: порядок ручного запуска, sizing и execution preflight не изменены.

## v1.4.7 - как читать направление, статистику и журнал

- Не сравнивайте LONG/SHORT по старой и новой lineage: после log-space fix новая статистика начинается заново.
- В «Результатах наблюдений» сначала смотрите когорту допуска, затем строки / уникальные времена / временные группы / неперекрывающиеся окна. Количество монет не равно количеству независимых экспериментов.
- `shadow_exploration` не является результатом разрешённой сделки. При `actionable_total=0` вывод о live strategy performance запрещён.
- В «Журнале решений» основная причина видна на карточке. Полный machine payload раскрывайте для incident review; длинный Rec ID и model version доступны через title и structured details.
- Машинные коды сохраняются для аудита, но русское действие и статус являются первичным операторским текстом.

Текущий `how_to_trade.png` иллюстрирует execution/preflight workflow и не менялся: v1.4.7 не изменяет порядок запуска сделки, sizing или Bybit execution boundary.

## v1.4.6 - как читать sizing-блокировку

- Для generated provisional plan система может использовать только минимальный исполнимый `qty` по live `minOrderQty/qtyStep/minNotional` и после этого обязана заново проверить полный notional и margin всей сетки.
- Ручной/явный `qty` никогда не увеличивается автоматически.
- `ORDER_QTY_BELOW_MIN` является первичной причиной; одинаковый generic `RISK` и производный `ORDER_QTY_OFF_STEP` к нулю не должны дублировать её.
- Если минимально исполнимая сетка превышает risk limits, правильный результат - `НЕ ТОРГОВАТЬ` с кодом лимита позиции/маржи.
- На exit-candle directional MFE/MAE учитывают только gap open либо TP/SL trigger; дальнейшие high/low этой свечи не относятся к уже закрытой позиции.

## v1.4.3 — как читать компактные «Результаты» и «Здоровье»

В Results сначала проверьте `Торговые outcomes`. Если там `0`, таблица «Стратегии» описывает shadow/no-trade исследования, а не разрешённые сделки. Не делайте вывод по одной «доле успеха»: сопоставляйте её со средним net result. Для grid kill-switch всегда является неуспехом contract, даже если terminal proxy P&L положителен.

В Health основной экран показывает три вещи: операторские причины запрета, готовность данных/очередей и доказательность grid/trend models. PID, memory, collector lock, backfill, DB identity и LLM config находятся в «Расширенной диагностике». Это сокращает дубли, но не удаляет технические данные.

Большие окна ограничены 1600 px и 88% высоты. Нажмите Escape, чтобы закрыть любой открытый диалог.

## v1.4.2 — неподтверждённый тренд не является сделкой

1. `directional_trend` считается сформированной стратегией только при LONG или SHORT.
2. Надпись **«Проверка тренда отклонена»** означает: позиция не создана, entry/TP/SL отсутствуют, исход не планируется, модель на этой строке не обучается.
3. Не трактуйте `trend_evaluation_rejected` как neutral trend, нейтральную сетку или неудачную trend-сделку. Это только результат предварительного анализатора.
4. В историю и график попадают только `strategy_recommendation`; у отклонённой оценки кнопки истории нет.
5. Единственное операторское действие — дождаться следующего снимка с подтверждённым LONG/SHORT.

## v1.4.1 — сначала стратегия, затем направление

1. Прочитайте strategy family: **Фьючерсная сетка** или **Направленный тренд · одна позиция**.
2. Затем прочитайте направление. `neutral` означает **Нейтральная сетка** только для `futures_grid`. Для `directional_trend` оно выводится как **Направление не определено** и блокирует позицию.
3. Не переносите действия между стратегиями: советы про диапазон/шаги относятся только к grid; entry/TP/SL и first-touch — только к trend.
4. Кандидаты одного символа отображаются отдельными строками. Блокировка trend не является причиной блокировки grid и наоборот.

## v1.4.0 — сначала проверьте стратегию, очередь исхода и историю

- В «Деталях» убедитесь, что `futures_grid` показывает диапазон/kill-switch, а `directional_trend` — entry/TP/SL одной позиции.
- До 12 часов ожидайте статус `scheduled_for_label_horizon`; отсутствие готового outcome в этот период нормально.
- В «Исходах» не объединяйте `GRID_OUTCOME` с `TP_FIRST / SL_FIRST / HORIZON_EXIT` в одну торговую интерпретацию.
- В «Здоровье» проверяйте очереди grid и trend отдельно, ближайший due и semantic integrity БД.
- На графике истории пропуск линии означает отсутствие сохранённого уровня, а не нулевую цену.
- Ошибка API должна отображаться явно; пустой экран не является доказательством отсутствия исходов.

## v1.3.0 — trend выбирается только при доказанном TP-first преимуществе

- Trend-outcome теперь имеет явный тип: `TP_FIRST`, `SL_FIRST`, `HORIZON_EXIT` или censored `AMBIGUOUS`.
- Смотрите отдельно `P(TP раньше SL)`, `P(SL раньше TP)`, вероятность выхода по времени и first-touch EV.
- Высокая бинарная «вероятность успеха» сама по себе не разрешает сделку.
- Trend допускается только если консервативная вероятность TP-first выше SL-first и нижняя граница денежной EV положительна.
- Касание TP и SL одной минутной свечой не угадывается; такое наблюдение не обучает модель.
- При отсутствии готовой first-touch модели или доказанного преимущества — `НЕ ТОРГОВАТЬ`.
- `directional_trend` остаётся одной позицией с TP/SL, а не grid-ботом; биржевой ордер отправляет только оператор/внешний executor.

## v1.2.0 - сначала meta-router, затем подходящий внешний способ исполнения

- Один снимок рынка оценивается как `futures_grid` и как `directional_trend`.
- Router сравнивает только проверенную денежную ожидаемость и tail risk; raw score не сравнивается.
- `futures_grid` → внешний grid-bot workflow.
- `directional_trend` → одна long/short позиция с TP/SL через ручной или внешний executor.
- Подтверждение в интерфейсе создаёт только audit-instance. Сервис не отправляет Bybit order.
- При недоказанной прибыльности или близком результате → `НЕ ТОРГОВАТЬ`.
- Проигравшая модель продолжает получать paired proxy-outcome для обучения.

## Historical v1.1.0 - grid execution and directional trend shadow

- `futures_grid` remains the only operator-executable family. Neutral/long/short are grid inventory biases and still require range/mean-reversion evidence.
- `directional_trend` is a separate single-position long/short research policy. It follows a coherent trend, has its own TP/SL, and never averages or pyramids against the move.
- Every trend row is marked **НЕ ТОРГОВАТЬ / shadow** with `DIRECTIONAL_TREND_SHADOW_ONLY`. The execution endpoint and `bot_instance` creation are blocked.
- Trend outcomes are labeled separately from a continuous 1m path. Same-candle TP+SL and missing-minute paths are censored; they are not guessed.
- Do not compare or pool grid and trend win rates as one model. Each family has its own outcome contract and calibrator.

## v1.0.78 — свежесть рекомендации и независимое окно исхода

Не смешивайте два времени. `RECO_TTL_SEC` — операторская свежесть: при пустом значении используется `max(900, RECO_INTERVAL_SEC × 15)`, то есть 15 минут при минутном цикле. После TTL устойчивый сигнал может получить новую актуальную публикацию и новый `publication_root_rec_id`, но до завершения 12-часового окна сохраняет прежний `outcome_root_rec_id` и не создаёт ещё одну независимую метку.

12 часов — текущий версионированный target-контракт `grid_label_v26`, а не доказанный оптимум. 6 часов быстрее дают метки, но чаще обрезают медленный grid-path и funding exposure; 24 часа лучше охватывают длинный path, но медленнее накапливают независимые когорты и сильнее смешивают режимы. Смена горизонта требует отдельного purged walk-forward исследования, нового outcome/model lineage и embargo, соответствующего новому horizon.

В журнале истории различайте:
- **Публикация** — свежая операторская цепочка;
- **Разметка** — независимое outcome-окно;
- новая публикация внутри открытого horizon не равна новому эксперименту.

## v1.0.76 — как читать журнал исходов

В окне результатов не смешивайте два показателя: «Исход по правилам стратегии» и «Расчётный net proxy P&L». Положительный P&L рядом с «Неуспех · kill-switch» означает, что ранее накопленная grid-прибыль не компенсирует факт пробоя защитной границы для бинарной метки. Смотрите отдельную колонку «Причина исхода». Значение «Неизвестно» или `—` означает malformed/legacy/недоступное поле; UI не подставляет `0` и не принимает boolean за число.

## v1.0.75 — terminal-selected денежная проверка

Даже если общая выбранная OOF-подвыборка прибыльна, калибратор не готов, пока выбранная тем же порогом политика не имеет положительные денежные lower bounds на итоговой отложенной выборке. В деталях проверьте `terminal_selected_policy_expectancy_status=positive`, не менее 80/80 выбранных строк и не менее 5/5 целых временных когорт. `negative`, `uncertain`, `insufficient` или `not_evaluated` означают **НЕ ТОРГОВАТЬ**.

## v1.0.74 — как читать готовность калибратора

Даже положительная «Доходность по наблюдениям» всей candidate-когорты не разрешает торговлю. В окне готовности должны одновременно пройти: минимум 300 exact-policy labels, purged OOF skill, итоговая отложенная выборка минимум 80 строк/5 целых timestamps и положительные денежные lower bounds у подвыборки, прошедшей порог уверенности. `selected_policy_unproven`, маленький terminal block или отсутствующая новая когорта означают **НЕ ТОРГОВАТЬ**, а не повод вручную снижать пороги.

## v1.0.72

Окна «Здоровье» и «Исходы» теперь открываются сразу. Исторический архив загружается кратко; это изменение интерфейса и диагностических запросов, а не ослабление торговых условий.

## v1.0.71 - что делать после перезапуска

В окне «Здоровье системы» состояние **«передача управления»** означает, что новый процесс уже запущен, но ждёт безопасного получения блокировки сборщика. Проверьте владельца блокировки и показатель «До разрешённого перехвата». Не перезапускайте приложение повторно и не считайте stale-инструменты торгово пригодными. Дождитесь: блокировка принадлежит текущему процессу, цикл сборщика текущего процесса = «Да», публикация текущего процесса = «Да», stale = 0.

В журнале русское пояснение показывается вместе с исходным машинным кодом. `OUTCOME_SKIP_INVALID_GRID_CONTRACT` с причиной `intrabar_extreme_order_unobservable` означает, что минутная свеча не позволяет доказать порядок касаний; это корректное fail-closed цензурирование, а не сигнал вручную назначить прибыль/убыток.

## v1.0.69 — проверка обновления

Перед заменой версии сохраните идентификатор БД из окна здоровья. После перезапуска дождитесь двух признаков `Да`: собственный цикл сборщика и собственная публикация. Только затем интерпретируйте `healthy_not_actionable` как подтверждённо работающую инфраструктуру без разрешённой сделки.

## v1.0.68 — как доказать, что система работает

Главная таблица: **Символ · Направление · RR плана · Доходность по наблюдениям · Решение · Детали**. Кнопка **«Детали»** всегда находится в крайнем правом столбце.

Цвета одинаковы во всех основных поверхностях:
- зелёный — **МОЖНО ТОРГОВАТЬ**;
- жёлтый — **НЕ ТОРГОВАТЬ** или **ОЖИДАЕТ ПРОВЕРКИ**;
- красный — **ЗАБЛОКИРОВАНО**.

Откройте **«Здоровье системы»** и проверьте четыре слоя: миграцию БД, фоновые контуры, outcome-worker и причины последней публикации. Надпись **«Работает, торговых кандидатов нет»** означает, что инфраструктура исправна, но обязательные условия допуска сейчас не выполнены. Это не разрешение на ручной обход статуса.

Для диагностики нажмите **«Скачать диагностику JSON»**. Передавайте этот файл вместе с приблизительным временем наблюдаемой проблемы. Не передавайте `.env`, API-ключи, пароли, полный DSN или production-дамп БД.

## v1.0.65 - как читать восстановление данных

В окне «Здоровье символов» сначала проверяйте свежесть минутной свечи, затем отдельный блок «Сбор данных и восстановление после простоя». Свежая свеча означает, что горячий слой уже работает; ненулевое число оставшихся заданий означает только продолжающийся фоновый ремонт исторического пропуска. Рост RSS/пиковой памяти после запуска должен стабилизироваться, а не увеличиваться пропорционально глубине простоя.

## v1.0.64 — русский словарь интерфейса

Главная таблица: **Символ · Направление · RR плана · Доходность по наблюдениям · Решение**.

- Покупка (рост) = направление, которое выигрывает при росте цены.
- Продажа (снижение) = направление, которое выигрывает при снижении цены.
- Нейтральная сетка = работа внутри диапазона без единственной направленной цели прибыли.
- RR плана = расчётная награда конкретного плана / стресс-убыток на аварийной границе. Это не вероятность прибыли.
- Доходность по наблюдениям = результат созревших наблюдений текущего набора правил с оценкой неопределённости.
- Платёж финансирования = периодический платёж между участниками бессрочного фьючерса.
- Разница цен покупки и продажи и проскальзывание = издержки исполнения.
- Предзапусковая проверка = последняя проверка цены, геометрии, размера и риска перед операторским подтверждением.
- Значок `?` открывает подсказку; полные технические сведения находятся в «Деталях».

## v1.0.63: decision reason is a hint, not a column

Primary table: **symbol · direction · Plan RR · empirical expectancy · decision**. Hover or focus the decision badge to see one short human-readable reason. Open **Details** for the original diagnostic code, thresholds and complete explanation. Never interpret a long technical message as an extra decision metric.

## v1.0.62: one-glance decision table

Primary table: **symbol · direction · Plan RR · empirical expectancy · decision**. The one short reason is a hint on the decision badge; everything else belongs in **Details**. Do not enter on `НЕ ВХОДИТЬ`, `ЖДАТЬ`, `blocked`, `no_trade` or incomplete evidence. Shadow no-trade outcomes may mature without an LLM verdict because they are not executable and are never sent to the reviewer; actionable roots remain LLM-gated when enabled.

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
- Current label contract is `grid_label_v26`: entry remains the first exact 1m open strictly after publication; N intervals create N+1 prices but exactly N initial orders, with one idle pivot/bridge level; directional inventory and neutral full initial-order commitment are derived from those actual orders; kill-switch remains terminal; adverse settled funding reduces proxy `ret`, while positive receipts remain diagnostic-only and cannot create edge.
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
