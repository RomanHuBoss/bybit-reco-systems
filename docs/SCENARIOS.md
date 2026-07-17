## Сценарий: общая когорта прибыльна, выбранная моделью — нет (v1.0.74)

1. Pre-calibration candidate rows в целом имеют положительные row/temporal lower bounds.
2. Purged walk-forward model присваивает высокий `P(success)` группе частых мелких выигрышей с редкими крупными убытками.
3. Shared confidence transform и threshold выбирают эту группу, а более денежно-прибыльную low-hit-rate группу отклоняют.
4. Selected-policy mean/lower bounds рассчитываются отдельно и получают `negative`/`uncertain`.
5. Binary log-loss skill остаётся видимым в диагностике, но `oof_status=selected_policy_unproven`, fitted coefficients не загружаются и рекомендация остаётся `no_trade`.

## Сценарий: размер ряда оставляет однострочный хвост

1. Последовательность из 301 строки раньше давала integer-fold starts с terminal remainder из одной строки.
2. Новый splitter группирует строки по recommendation timestamp и идёт назад от конца, пока terminal block не содержит минимум 80 строк и 5 целых timestamps.
3. Train prefix проходит label-availability purge; общий timestamp никогда не оказывается одновременно в train/validation из-за границы block.
4. Если terminal contract нельзя построить или проверить, candidate не активируется.

## Scenario: штатный restart при включённом LLM reviewer (v1.0.73)

1. FastAPI lifespan устанавливает общий shutdown-event.
2. Reviewer завершает текущий bounded sweep, если он уже выполняется; после ожидания новый sweep не начинается.
3. Reviewer target возвращается в supervisor без ложной классификации crash, потому что stop-event уже установлен.
4. Supervisor сохраняет `state=stopped` и удаляет только `runtime:llm_reviewer`, принадлежащий текущему `RUNTIME_OWNER`.
5. Новый процесс может атомарно получить освобождённый lock. При аварийном kill или незавершённом внешнем запросе он fail-closed ждёт TTL и не удаляет чужой lease.

## Сценарий: окна «Здоровье» и «Исходы» на большой БД

1. UI немедленно открывает модальное окно со статусом загрузки.
2. Health запрашивает bounded `/status`, symbol health и последние 200 решений параллельно.
3. Outcomes получает full current-policy статистику и `archive&detail=summary` параллельно.
4. Исторический архив не передаёт полный `reasons_json`; последние 20 строк остаются доступны для аудита.
5. Ошибка или отсутствие доказательств не преобразуются в торговый допуск.

## Сценарий: обновление Windows-процесса v1.0.71

1. Старый процесс получает stop-event, фоновые потоки завершают текущую безопасную операцию и освобождают только принадлежащие им runtime-lock.
2. Новый процесс получает collector lease без ожидания полного TTL и формирует собственный цикл/публикацию.
3. Если старый процесс завершился аварийно, status показывает `handover`, владельца блокировки и секунды до takeover; до получения свежих данных торговля остаётся закрытой.
4. После истечения handover grace отсутствие собственного цикла становится `COLLECTOR_STALLED`. Повторный бесконтрольный restart не является способом лечения.
5. Outcome backlog обрабатывается независимо. Размеченные строки сохраняются, неоднозначные OHLCV-пути получают явную причину `censored`, а worker продолжает следующую строку.

## Сценарий обновления и перезапуска — v1.0.69

1. До обновления запишите «Идентификатор БД».
2. После запуска дождитесь, пока «Цикл сборщика текущего процесса» и «Публикация текущего процесса» станут `Да`.
3. Сверьте идентификатор БД. Его изменение означает другое хранилище и требует проверки `DB_PATH`/`DATABASE_URL`.
4. Сумма статусов последней публикации должна охватывать весь configured universe, включая non-root audit rows.

## Сценарий проверки системы после запуска — v1.0.68

1. После перезапуска оператор открывает **«Здоровье системы»**.
2. В блоке миграции должно быть `Применена`, а остаток materialization — `0`. При другом результате состояние обозначается как требующее внимания.
3. Проверяются фоновые потоки и `Контур исходов`. `processing`/`backlog` означают работу с очередью; `stalled`/`error` означают эксплуатационную проблему.
4. Если состояние **«Работает, торговых кандидатов нет»**, оператор смотрит ранжированные причины `НЕ ТОРГОВАТЬ`. Коды калибровки/недоказанной ожидаемости означают отсутствие достаточных доказательств, а не автоматическую поломку.
5. Красные `ЗАБЛОКИРОВАНО` проверяются отдельно как жёсткие причины по риску, данным или предзапусковому контракту.
6. Для внешнего разбора оператор нажимает **«Скачать диагностику JSON»** и передаёт полученный файл без `.env`, ключей и дампа БД.
7. Главная таблица перебирается по фиксированной крайней правой колонке **«Детали»**. Цвет не используется как единственный носитель смысла: статус всегда подписан текстом.
8. Наличие нуля actionable не является основанием ослаблять gates или вручную переводить `no_trade` в `recommended`.

## Сценарий: разгрузка многонедельной очереди наблюдений (v1.0.67)

1. В БД накоплены тысячи созревших outcome roots, а публикация рекомендаций должна продолжаться.
2. Отдельный `outcomes` worker получает `runtime:outcomes`; `_reco_thread` не ожидает его завершения.
3. Перед циклом сохраняется `running` snapshot и размер/возраст очереди.
4. Worker обрабатывает ограниченный пакет, обновляя heartbeat во время прохода.
5. После цикла сохраняются terminal counts и backlog после обработки.
6. Если очередь уменьшилась или строки получили outcome/censored, состояние — `backlog`, а следующий цикл запускается через контролируемый короткий интервал.
7. Если heartbeat свежий и цикл выполняется, состояние — `processing`; `stalled` появляется только при отсутствии/устаревании подтверждённого прогресса.
8. Ошибка сохраняется как `error`, supervisor перезапускает worker; рекомендационный цикл остаётся независимым.

## Сценарий: рекомендательный цикл на большой outcome-истории (v1.0.66)

1. Рекомендатор вычисляет текущий policy fingerprint.
2. Создаётся один cycle-local calibration evidence context.
3. Observability читается порциями; bot filter выполняется в SQL.
4. При необходимости refit outcomes загружаются один раз, а полный `reasons_json` после разбора сокращается до трёх необходимых разделов.
5. Global, futures-grid и direction calibrators используют один набор ссылок.
6. После загрузки calibrators evidence rows явно освобождаются.
7. Параллельный запрос `/api/v1/status` агрегирует lineage в потоке и не создаёт второй полный массив истории.

При malformed JSON/contract/label система сохраняет прежнее fail-closed поведение. Обновление не требует миграции или изменения `.env`.

## Сценарий: запуск после двухнедельного простоя (v1.0.65)

1. В БД есть последняя минутная свеча старше безопасного свежего хвоста.
2. Горячий collector получает текущий тикер и одним запросом последние 360 минутных свечей.
3. Свежий хвост сразу сохраняется; health показывает актуальную минутную свечу, не ожидая полного ремонта истории.
4. В `app_config` создаётся pending-задание с началом сразу после старой свечи и концом перед свежим хвостом.
5. Backfill за цикл обрабатывает ограниченное число инструментов и по одной странице до 360 свечей.
6. Курсор продвигается только после успешной записи; после рестарта восстановление продолжается идемпотентно.
7. Когда `next_start_ts > target_end_ts`, задание получает `complete` и больше не выполняется.
8. Recommendation остаётся fail-closed до выполнения собственных multi-timeframe warm-up требований; свежесть 1m не выдаётся за полную готовность алгоритма.

## Сценарий чтения интерфейса v1.0.64

1. Оператор смотрит только на пять колонок главной таблицы.
2. Направление читается как «Покупка (рост)», «Продажа (снижение)» или «Нейтральная сетка».
3. Хинт у решения даёт краткую русскую причину; хинты у RR плана и доходности по наблюдениям объясняют смысл и ограничения показателей.
4. Для чисел, порогов, издержек, плеча, маржи, платежа финансирования и внутренних кодов оператор открывает «Детали».
5. Неизвестный или неполный показатель отображается как «нет данных» и не превращается в разрешение торговли.

## Scenario: operator scans a no-trade row (v1.0.63)

1. The table shows symbol, direction, Plan RR, empirical expectancy and `НЕ ТОРГОВАТЬ`.
2. Hovering or focusing the decision badge shows a short reason such as `Возвратность цены не подтверждена`.
3. The table never shows raw thresholds, JSON codes or long mixed-language model diagnostics.
4. Opening **Details** reveals the full reason code, original message, thresholds and all supporting diagnostics.

## Scenario: LLM is enabled and all publishable ideas are no_trade (v1.0.62)

1. The publisher marks a deterministic, risk-clean no-trade root as `shadow_no_trade` and outcome-eligible.
2. The LLM reviewer correctly ignores it because it is not actionable.
3. After label maturity, the outcome worker labels it without an LLM verdict.
4. An actionable root without an eligible LLM verdict remains excluded.
5. Existing matured roots are picked up on subsequent worker cycles; no manual backfill command is required.

## Scenario: operator scans the recommendation table

The operator sees only symbol, direction, Plan RR, empirical expectancy and decision. A short reason is attached to the decision badge as a hover/focus hint. Clicking **Details** reveals confidence, risk buffer, price/range, sizing, costs, funding, model diagnostics and guards.

## Scenario: operator compares a new grid plan with current-policy evidence (v1.0.61)

1. A new recommendation contains complete grid economics, worst-side cross-margin kill-switch stress and current cost/funding diagnostics.
2. The table shows Plan RR for that concrete plan and empirical mean return for the exact current policy as separate columns.
3. Opening the card shows the Plan RR numerator/denominator, empirical Student-t confidence interval, expected shortfall and mean-to-tail ratio.
4. A positive Plan RR with insufficient empirical evidence does not imply a validated edge; the empirical field says insufficient.
5. A positive empirical mean with a confidence interval crossing zero remains uncertain and does not override deterministic blockers.
6. The legacy heuristic capture score remains only in the technical payload and is not used as operator R/R.

## Scenario: an old recommendation lacks v1.0.61 operator metrics

1. The database row was published before Plan RR and empirical metrics were persisted.
2. History/detail readers do not reconstruct economics from today's state and do not substitute zero.
3. The UI displays unavailable for the missing metrics while retaining the immutable old recommendation.
4. A newly published recommendation under v1.0.61 contains the additive fields; no database migration or historical rewrite is required.

## Scenario: hot collector and warm-up backfill overlap on PostgreSQL (v1.0.60)

1. The hot loop fetches 1m rows while the backfill loop bootstraps or refreshes slower/derived series.
2. Network futures may finish in arbitrary order, but no OHLCV statement is executed yet.
3. Each loop aggregates its pending rows; persistence canonicalizes the complete primary-key order.
4. If PostgreSQL chooses the transaction as a deadlock victim, the connection rolls back and replays the same canonical batch.
5. A successful OHLCV commit is retained even if a later diagnostic-log write fails; the outer supervisor records any terminal cycle error.
6. The hot loop derives 15m/30m from touched 1m sources and does not rewrite 4h unless it actually fetched/touched 1h (normal hot wiring does not).

## Scenario: old outcomes remain in the DB after a policy update (v1.0.58)

1. The database contains 72 outcomes from prior model/policy lineages.
2. The running policy has zero verified outcomes.
3. `GET /api/v1/outcomes/stats` without a scope returns `current_policy` and a zero current headline.
4. The UI separately requests `scope=archive` and labels the 72 rows as historical.
5. Archive win rate/return never enters current-policy cards or detailed policy tables.

## Scenario: 80 outcomes exist but confidence is still unavailable (v1.0.58)

1. The exact-policy cohort reaches the default `CALIB_MIN_SAMPLES=80`.
2. Monetary diagnostics may be evaluated, subject to temporal independence and positive lower bounds.
3. With `REQUIRE_CONF_GATE=1`, probability inference still requires at least 300 exact-policy labels plus accepted purged OOF and terminal holdout skill.
4. The UI reports both floors and does not call 80 rows full readiness.
5. Raw confidence remains audit-only until the complete probability contract passes.

## Scenario: one root is permanently censored (open risk, v1.0.58)

1. Five hundred exact-policy labels support positive monetary and probability diagnostics.
2. One additional matured root is permanently unobservable.
3. The current zero-tolerance gate changes expectancy to `censored`, clears fitted coefficients and keeps `NO_TRADE`.
4. The operator sees the observability hard block and censor reason instead of a misleading readiness percentage.
5. Actionability may resume only after a separately validated bounded-censor policy; the operator must not manually ignore the row.

## Scenario: threshold or risk limit changes under the same code (v1.0.57)

1. Policy A has matured positive proxy outcomes and a fitted cache.
2. The operator changes a selection threshold, universe, LLM gate or active risk limit without changing code.
3. Canonical JSON changes, producing a different full SHA-256 policy fingerprint and cache key.
4. Policy A outcomes remain audit history but contribute zero evidence to Policy B.
5. Policy B remains `NO TRADE` until its own complete, uncensored chronological evidence proves monetary and probability skill.

## Scenario: matured root cannot be labeled (v1.0.57)

1. A current-policy shadow root reaches canonical maturity.
2. Gap-through-stop, unobservable replacement timing, missing settlement/candle capacity or malformed contract prevents a bounded label.
3. The ledger records `censored` (terminal) or `waiting` (transient); it is never silently omitted from the denominator.
4. Any censored/unresolved root produces `PROXY_OUTCOME_CENSORING_UNBOUNDED` and disables positive inference.
5. Waiting roots rotate by `last_attempt_ts`, so newer complete roots continue to be processed.

## Scenario: final future block selects but does not train the model (v1.0.57)

1. Earlier chronological folds produce sufficient purged predictions.
2. Feature LogReg + Platt beats score-only and null log-loss on aggregate folds and on the terminal future block.
3. The service activates the exact pipeline fitted before that terminal block.
4. Terminal labels remain untouched by active fitting; a full-data refit is not substituted after selection.
5. If either aggregate or terminal skill fails, confidence stays raw/audit-only and `REQUIRE_CONF_GATE=1` keeps `NO TRADE`.

## Scenario: local positive PnL lacks terminal exchange reconciliation (v1.0.57)

1. Immutable execution rows form a locally flat signed-quantity ledger with positive net PnL.
2. No later complete external Bybit reconciliation matches position, open orders, event counts, gross, fees and funding.
3. `total_pnl_finalized=false`; the bot is excluded from live profitability/validation.
4. Risk receives zero credit for the positive amount. An unreconciled negative amount would still tighten loss controls.
5. A matching snapshot from the trusted external read-only adapter unlocks finalized evidence; a pre-stop or mismatched snapshot does not.

## Scenario: claimed policy hash does not match persisted contract (v1.0.57)

1. A row claims the current 64-character fingerprint, but its stored threshold/risk contract was altered or omitted.
2. The fit path and outer denominator independently recompute canonical JSON SHA-256.
3. The row is not labeled support; once mature, it is unresolved with an invalid-contract diagnostic.
4. Positive expectancy and probability inference are disabled until the evidence set is internally reproducible.

## Scenario: direction Platt is fitted but has no chronological skill proof (v1.0.57)

1. Direction outcomes fit a one-dimensional Platt mapping against horizon price sign.
2. The mapping is exposed as an audit probability only.
3. `direction_confidence_feature` remains the raw pre-decision value; the mutable cache cannot shift candidate features.
4. A future iteration may activate it only after an independent chronological comparison against raw/null baselines.

## Scenario: non-empty audit archive after v7 deployment (v1.0.56)

1. PostgreSQL contains v6 outcomes.
2. v1.0.56 starts with model lineage v7 and calibrator keys v18/v13.
3. Historical archive count remains non-zero for audit.
4. Current-model and feature-eligible counts are zero until new v7 recommendations mature.
5. The calibrator remains `insufficient` and recommendations remain shadow `NO_TRADE`; old rows cannot accelerate the new model.

## Scenario: high-tail candidate was impossible under the fixed 0.55 gate (v1.0.55)

1. Valid evidence exists on five timeframes and aggregate `mean_reversion_score=0.351`.
2. The old fixed `0.55` rule converted the candidate to `no_trade`, although `0.351` was the maximum in the supplied 10,000-row export.
3. With the default `MEAN_REVERSION_MIN_SCORE=0.25`, this candidate passes only the mean-reversion screen.
4. It is still `no_trade` when monetary expectancy, confidence, economics, leverage profile or risk gates are unproven.
5. A score below `0.25` receives `MEAN_REVERSION_EDGE_UNCONFIRMED` without claiming proven negative expectancy.

## Scenario: continuous overlap chain still yields independent temporal evidence (v1.0.55)

1. Forty-two recommendation cohorts begin six hours apart and each matures after twelve hours.
2. Every interval overlaps the next, so connected-component merging produced one cluster indefinitely.
3. Same-timestamp symbols are first collapsed into one decision cohort.
4. Earliest-finish interval scheduling selects cohorts 1, 3, 5, ...: 21 pairwise non-overlapping observations.
5. The temporal lower bound is evaluated on those 21 means; same-time symbol count cannot inflate it.

## Scenario: many rows but no usable purged OOF validation (v1.0.54)

1. The retained cohort contains 320 valid rows and positive monetary/temporal lower bounds.
2. 280 rows belong to one early temporal cluster, so every fixed chronological validation boundary falls inside that cluster.
3. Label-availability purging removes all candidate training rows before those validation timestamps; OOF prediction count is zero.
4. The service does not expose full-sample feature coefficients: `purged_oof_status=insufficient`, `logreg_active=false`, and inference falls back to score-only Platt or capped raw confidence.
5. When independent history later produces at least `CALIB_MIN_SAMPLES` purged OOF logits and Platt-on-top fits successfully, feature LogReg may activate.

## Scenario: horizon boundary has insufficient volume (v1.0.53)

1. The final in-window candle has high volume, but that budget ends with the candle.
2. The exact horizon candle opens through a resting level or requires closing residual inventory.
3. The boundary candle's total volume is smaller than the required modeled quantity.
4. The worker does not reuse the prior minute's volume and does not create a partial fictional ledger.
5. No outcome is stored; diagnostics identify the failed gap fill or terminal liquidation.
6. The label can be evaluated only after the boundary minute closes, so availability is one minute after the configured horizon.

## Scenario: kill-switch candle cannot both fill and liquidate full size (v1.0.53)

1. A grid fill consumes part of the breach candle's observed volume.
2. The external kill-switch is crossed with residual inventory still open.
3. The remaining candle volume is insufficient to close that inventory at full modeled quantity.
4. The outcome is unavailable with `insufficient_candle_volume_for_kill_switch_liquidation`; the earlier fill is not stored as a completed label.

## Scenario: intrabar kill-switch breach with adverse continuation (v1.0.52)

1. The historical candle crosses the external kill-switch without a close-open gap.
2. The proxy processes only resting grid orders crossed before the protective boundary.
3. If the remaining position is short and the upper boundary is breached, the conservative close price is the candle high.
4. If the remaining position is long and the lower boundary is breached, the conservative close price is the candle low.
5. If continuation helps the remaining position, no favorable slippage is credited; the boundary price is retained.
6. The ledger stops permanently and later recovery is ignored.
7. A gap that skips the boundary remains unlabelable.

Expected diagnostics: `kill_switch_fill_confirmation=adverse_observed_extreme_v1`, boundary, observed extreme and liquidation price.

## Scenario: current Bybit metadata is unavailable (v1.0.51)

1. The recommender produces a historical-model futures-grid signal.
2. Current public instrument metadata is unavailable or has changed since the modeled timestamp.
3. The recommendation is not changed to `blocked` for that reason.
4. The record declares `simulation_scope.mode=historical_proxy_only` and `runtime_execution_validation=not_performed`.
5. After the historical horizon matures, outcome labeling uses the persisted model geometry and conservative OHLCV fill assumptions.
6. Any explicit current-market preflight is a separate operator diagnostic and cannot rewrite the historical recommendation or calibration evidence.

## Scenario: replacement crossed inside its creation candle (v1.0.50)

1. An initial resting Sell is confirmed by strict trade-through.
2. The ledger creates the adjacent replacement Buy.
3. The same one-minute candle later trades below that Buy level.
4. OHLCV cannot prove whether the Sell filled early enough and the replacement was acknowledged before the reversal.
5. The outcome is unavailable with `intrabar_replacement_fill_timing_unobservable`; it is not a win or loss.
6. If the replacement is crossed in the next candle, the cycle may be labeled subject to all price, volume, funding and geometry checks.

## Scenario: price crosses the grid but observed volume cannot fill it (v1.0.49)

1. A neutral grid has `qty_per_order=10` and a resting Buy at 99.
2. The next candle trades below 99, but its entire Bybit kline volume is only 1 base unit.
3. Price trade-through is present, yet a full 10-unit fill is physically impossible within the observed market history.
4. The worker records `insufficient_candle_volume_for_full_fill` and creates no outcome label.
5. If several 1-unit orders cross in one candle with volume 1.5, the first may consume capacity but the second makes the whole path unavailable; partial fabricated ledgers are not stored.
6. When total observed volume is sufficient, normal strict-trade-through, path-equivalence, fee, funding and terminal-PnL rules continue.
7. Sufficient aggregate volume remains proxy evidence only; queue priority and price-level liquidity require exact exchange fills.

## Historical scenario: current-filter normalization (v1.0.48, superseded by v1.0.51)

The blocking behavior below is release history and is not the v1.0.51 contract.

1. Recommender generates lower `99.1`, upper `100.9`, step `0.9`, qty `0.26`.
2. Bybit metadata says tick `0.5` and qty step `0.1`.
3. In v1.0.48-v1.0.50 the system normalized to lower `99.0`, upper `101.0`, step `1.0`, qty `0.2` and made that snapshot mandatory.
4. Version 1.0.51 removed this behavior: persisted historical model geometry remains the simulation input, and missing current metadata does not block or suppress an outcome.
5. Optional explicit preflight may still show current snapping diagnostics, but it is outside publication and calibration.
6. During labeling, exact OHLC equality with a limit is not a fill. Buy requires trade below; Sell requires trade above.
7. Only the new `grid_label_v21` evidence may train v10 calibration.

## Scenario: funding receipt without grid profit (v1.0.46)

1. A directional grid holds inventory through a settlement while price and grid cash PnL remain flat.
2. The settlement pays the held side, so signed account funding cashflow is positive.
3. The cashflow remains a diagnostic/exact-PnL item, but canonical proxy funding contribution is zero.
4. `ret` remains zero or negative after execution costs; `success=0` and the receipt cannot improve monetary lower bounds or calibration readiness.
5. If the settlement charges the held side, the adverse cashflow is included and reduces `ret`.
6. Upgrade to `grid_label_v19` clears prior receipt-inflated proxy outcomes and all current calibrators.

## Scenario: many correlated symbols in one market horizon (v1.0.45)

1. Eighty symbols mature against the same 12-hour market interval; 40 return `+3%` and 40 return `-1%`.
2. Row-level statistics show `n=80`, mean `+1%`, and a positive one-sided lower bound.
3. The interval-overlap algorithm merges all 80 rows into one temporal component, including transitive overlaps and overlaps crossing a wall-clock bucket boundary.
4. `temporal_cluster_count=1` is below the default minimum of 20, so `expectancy_status=insufficient`, `fitted=false`, and the strategy remains shadow `no_trade`.
5. A later cohort may qualify only after enough non-overlapping temporal components accumulate and both row-level and cluster-level lower bounds are positive.
6. Even then, the result remains proxy evidence and must be confirmed by purged walk-forward and exact execution PnL.

## Scenario: stopped bot with residual execution inventory (v1.0.44)

1. External adapter records one profitable Sell fill for a bot and the audit bot is marked stopped.
2. The event remains immutable and visible; realised gross/fee/funding totals are still reported.
3. Because the matching Buy ledger is absent, `net_position_qty != 0`, `position_flat=false`, and `total_pnl_finalized=false`.
4. `/api/v1/validation/live-evidence` returns the row with `validation_eligible=false` and `residual_position`; it is excluded from all `LIVE_VALIDATION_*` metrics.
5. After the missing matching fills are ingested and signed quantity returns to zero, a stopped bot becomes eligible without rewriting prior events.

## Scenario: positive mean without demonstrated positive edge (v1.0.43)

1. The current independent matured cohort reaches the raw row count but has a small positive recency-weighted mean relative to dispersion.
2. The system computes Kish effective sample size and a one-sided 95% lower bound.
3. If effective samples are below the floor, status is `insufficient`; if the lower bound is `<= 0`, status is `uncertain`.
4. LogReg/Platt remains unfitted for actionability and the candidate receives `PROXY_MONETARY_EXPECTANCY_UNPROVEN`.
5. The recommendation is stored as shadow `no_trade`, allowing a later independent outcome without exposing the strategy to operator execution.
6. Only a new current cohort with lower bound `> 0` can produce `expectancy_status=positive`; all deterministic risk, economics, temporal and execution gates still apply.

## Scenario: positive calibrator outlives its retained evidence (v1.0.42)

1. A bot calibrator was fitted on a positive 320-row proxy cohort and saved in `app_config`.
2. More than one refit interval passes; the underlying 14-day recommendation/outcome rows are pruned or no longer meet the current model filter.
3. Current refit returns `insufficient` with 12 or zero usable rows.
4. The positive model is deactivated and the insufficient state overwrites the cache; after restart it remains unfitted/raw rather than resurrecting old coefficients.
5. If the saved state was `expectancy_status=negative`, it remains a conservative NO_TRADE veto until a new positive cohort replaces it.

## Scenario: repeated no-trade signal during an open shadow horizon (v1.0.41)

1. Первый полный `no_trade` без hard blocks получает `sample_role=shadow_no_trade` и становится outcome root.
2. Повторные циклы с тем же venue/symbol/bot/direction/model сохраняются как новые audit rows.
3. Пока pseudo-entry + label horizon не завершены и outcome отсутствует, эти rows ссылаются на первый root и не размечаются отдельно.
4. После завершённого horizon или сохранённого outcome следующая строка может стать новым независимым root.
5. Смена direction или model version открывает отдельную статистическую chain.

## 0. Negative monetary expectancy despite high hit rate - v1.0.40

1. A matured cohort contains 160 proxy wins of `+0.1%` and 40 proxy losses of `-5%`.
2. Binary hit rate is 80%, but arithmetic mean return is `-0.92%`; recency-weighted mean is also negative.
3. The v5 calibrator records `expectancy_status=negative`, weighted mean and lower-tail expected shortfall, and does not fit LogReg/Platt.
4. A fresh persisted negative state is loaded even though `fitted=false`.
5. New `futures_grid` rows receive `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE` and status `no_trade`; a hard execution/data block, if present, still produces `blocked`.
6. Positive proxy mean only permits the remaining calibration checks; it does not prove live profitability.

## 0. Tail-loss exact-evidence stop - v1.0.39

1. Eight independent stopped bots of one symbol/direction contain seven `+1` exact-net outcomes and one `-100` range-break outcome.
2. Cohort diagnostics are total `-93`, median `+1`, positive rate `87.5%`.
3. Because the minimum sample is reached and cumulative exact net PnL is negative, preflight emits `LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY` despite the positive median/win rate.
4. Operator action `executed` remains blocked and no new `bot_instance` is materialized.
5. A cohort below the sample floor or with positive cumulative net PnL is not blocked by this predicate; absence of a block is not a profitability claim.

## 0. Outcome dependency diagnostics - v1.0.38

Expected behavior:
- missing required funding settlement + non-zero inventory -> no label, `OUTCOME_WAIT_FUNDING_SETTLEMENT`, automatic retry;
- identical retry inside one hour -> no duplicate wait row;
- conflicting duplicated funding aliases -> no label, `OUTCOME_SKIP_INVALID_GRID_CONTRACT`, reason `invalid_funding_contract`;
- invalid grid count/range/kill-switch -> no label with a specific reason;
- upgrading v1.0.37 -> v1.0.38 keeps `grid_label_v18`, so existing v18 outcomes are not reset.

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
2. Five newest independent bots for the same `(symbol, direction)` are loss-making, or a predefined minimum cohort has negative cumulative exact net PnL; median and positive-bot rate remain diagnostics and cannot override aggregate loss.
3. A new recommendation can still be published for audit, but operator action `executed` returns `409`, no `bot_instance` is created, and `decision_log` contains the relevant `LIVE_VALIDATION_*` code and cohort metrics.
4. A losing long cohort does not by itself block short until the broader symbol threshold is reached. Repeated rows from one publication root count once, and an explicit new `model_version` starts a separate evidence cohort.
5. The operator must diagnose/revise the strategy or evidence pipeline; manually downgrading the blocker is not a supported path.


## 19. Низкий тренд без подтверждённой возвратности

1. Multi-timeframe trendiness низкий, поэтому legacy `1 - trend_strength` выглядел бы как сильный range score.
2. Independent lag-1 autocorrelation / variance-ratio / sign-reversal aggregate отсутствует, недостаточен либо ниже configured `MEAN_REVERSION_MIN_SCORE` (default `0.25`).
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
