## Остаточные риски после v1.4.12 application-heartbeat fix

- Public WebSocket всё ещё может разрываться из-за сети, прокси, Bybit или длительной локальной DB-паузы. v1.4.12 классифицирует это как session boundary и не мостит неизвестный chronology gap.
- Application heartbeat выполняется в том же synchronous consumer loop. Если PostgreSQL блокирует поток дольше watchdog timeout, соединение будет безопасно переподключено; неизвестный интервал останется fail-closed.
- REST fallback доказывает chronology только при overlap/coverage contract. Zero-width initial span не считается доказательством полной минуты.
- Live long-duration Windows/PostgreSQL soak и disposable PostgreSQL integration требуют отдельного стенда; offline regression не доказывает отсутствие всех инфраструктурных disconnects.

## Остаточные риски после v1.4.11 WebSocket resilience fix

- Public WebSocket может разрываться по сети, прокси, локальной перегрузке или стороне Bybit. v1.4.11 переподключается штатно и закрывает coverage gap, но не может восстановить точную chronology неизвестного интервала.
- `MARKET_TRADE_STREAM_MAX_QUEUE=256` и batched commits рассчитаны на кратковременное отставание writer. Устойчивое превышение DB throughput всё равно приведёт к reconnect/gaps; контролируйте `stream_runtime`, reconnect count и размер таблицы.
- REST recent-trade остаётся fallback без доказательства полной chronology. Он не объединяется с WebSocket coverage и не заменяет отсутствующий session interval.
- Existing `COLLECT_ERROR` и старые повторные `RECO_WARMUP_SKIP` остаются в audit history; релиз прекращает новые дубликаты, но не удаляет прошлые записи.
- Startup handover может временно показывать «система запускается», пока новый process не завершил собственный collector+publication cycle. Это fail-closed состояние, а не ошибка Bybit.
- Live long-duration soak под конкретным Windows/proxy/PostgreSQL окружением не выполнялся; offline deterministic tests не моделируют все сетевые задержки.

## Остаточные риски после v1.4.10 coverage-boundary fix

- Первый WebSocket пакет может создать нулевой coverage span `[ts+1, ts+1]`, если `data[].T == envelope ts`. Это намеренно безопасное состояние: первый millisecond остаётся исключённым, а span не пригоден для минутного replay, пока последующие сообщения не расширят его до полного окна.
- Публичная trade chronology по-прежнему не доказывает queue priority, фактические fills, partial fills, latency или момент активации replacement order.
- Live long-duration WebSocket reconnect/throughput зависит от сети и инфраструктуры deployment; offline regression tests проверяют контракт, но не заменяют production monitoring.
- Historical crash records v1.4.9 остаются audit history и могут быть оставлены в decision log.

## Остаточные риски после v1.4.9 publicTrade ordering fix

- Исправление сохраняет порядок строк, доставленный Bybit WebSocket, но public trade journal по-прежнему не доказывает queue priority, частичное исполнение конкретного grid order, latency или момент активации replacement order.
- Несколько сделок могут иметь одинаковые `T` и `seq`. Их delivery order сохраняется, однако это остаётся публичной последовательностью сообщений, а не доказательством внутреннего matching-engine fill order для счёта оператора.
- Coverage до обновления v1.4.9 не содержит materialized session/message/row order и не преобразуется задним числом в доказанный WebSocket replay. Такие интервалы остаются на прежнем fail-closed fallback.
- Live reconnect/throughput зависит от сети и инфраструктуры Bybit; disconnect по-прежнему закрывает span и создаёт явный gap.

## Остаточные риски после v1.4.8 funding recovery и trade journal

- Основной publicTrade WebSocket может отключиться, потерять runtime lease или быть недоступен во время restart; каждый такой интервал образует явную границу coverage и не мостится. REST recent-trade fallback возвращает ограниченное число последних сделок: на высокоактивном инструменте между опросами может не сохраниться overlap trade ID, поэтому span закрывается с gap и не используется как полное intrabar evidence.
- Public trade chronology доказывает порядок публичных сделок, но не queue priority, actual fill, partial fill, network latency или момент размещения replacement order. Если эти факторы способны изменить ledger, outcome остаётся censored.
- Retention по умолчанию составляет 72 часа. Старые неоднозначные свечи за пределами retained journal автоматически не восстанавливаются.
- Funding history может быть опубликована с задержкой или временно недоступна. Durable repair queue предотвращает потерю задачи, но outcome остаётся waiting до получения фактического settlement.
- Новые observability tables увеличивают объём БД. Retention ограничивает trade rows, однако оператор должен контролировать storage growth на своём universe и cadence.
- Изменение наблюдаемости не является новой торговой моделью и не доказывает edge или live fills.

## Остаточные риски после v1.4.7

- Log-space mirror symmetry устраняет программную асимметрию направления, но не доказывает predictive edge, прибыльность LONG/SHORT или пригодность текущих thresholds.
- Новая feature semantics начинает пустую exact-policy/model lineage. Старые v12/v22/v5/v2 coefficients и outcomes не переиспользуются для inference новой линии; доказательность должна накопиться заново.
- `temporal_clusters` и `max_non_overlapping_windows` исправляют ложную интерпретацию количества строк, но не являются полноценным correlation-adjusted `n_eff`. Cross-symbol beta, sector clusters, regime persistence и common market shocks остаются зависимостями.
- `by_bot_cohort` предотвращает смешение eligibility cohorts в основной таблице, однако внешние потребители legacy `by_bot` всё ещё обязаны явно выбирать cohort fields.
- Journal показывает до 200 последних записей API; structured details ограничены защитным пределом 160 leaf fields на карточку. Полный durable payload остаётся в backend/DB и API, но очень глубокий объект может быть сокращён в UI.
- UI/observability changes не ослабляют no-trade, calibration, monetary, censoring, funding или execution preflight gates. Нулевая actionable-когорта остаётся отсутствием evidence по разрешённым сделкам.
- Проверка не включала live PostgreSQL disposable integration, реальные Bybit credentials, private account truth или внешний executor. Сервис по-прежнему не отправляет orders.

## Остаточные риски после v1.4.6

Закрыты три подтверждённых дефекта: structural under-min generated sizing, risk-increasing nearest-step leverage alignment и post-exit contamination directional MFE/MAE. Минимальное повышение generated qty не является разрешением увеличивать пользовательский размер: оно допустимо только для provisional system default и всегда повторно проверяется по full-grid notional, margin и loss budget. Реальный внешний executor всё равно обязан сверять private account balance, фактические лимиты order endpoint и preview создаваемого Bybit grid bot.

OHLCV proxy по-прежнему не доказывает queue priority, частичные исполнения, точный intrabar порядок, liquidation waterfall или фактические комиссии. Исправленная MFE/MAE семантика консервативна: на exit candle она намеренно не кредитует неизвестное движение после триггера.

## Остаточные риски после v1.4.5

- Direction-blind LONG/SHORT representation исправлен, но реальный edge не доказан: приложенный ZIP не содержит заполненной runtime DB или live-fill reconciliation.
- Генератор кандидатов остаётся rule-based. Обучаются calibration/validation layers, а не direction, feature representation, entry или exit policy.
- Даже fitted calibration имеет вес не более 50% в итоговом confidence; исходный heuristic confidence остаётся структурно доминирующим.
- Changelog содержит 74 versioned releases за июль 2026 и 24 distinct `grid_label_vN`: дальнейшая смена lineage без доказанного safety-дефекта снова фрагментирует evidence.
- 300 строк не равны 300 независимым наблюдениям: строки разных символов одного timestamp коррелированы, а 12-часовой horizon ограничивает скорость накопления temporal cohorts.
- Если frozen exact-policy lineage после достаточного числа whole-timestamp cohorts не превосходит null и score-only baselines и не проходит monetary holdout, текущая strategy thesis должна считаться не подтверждённой, а не бесконечно «ремонтироваться».

## Остаточные риски после v1.4.4 label-maturity repair

- Исправление восстанавливает корректный допуск evidence, но не создаёт статистический edge. При текущих exact-policy counts bot calibrators и trend first-touch model остаются unfitted.
- `shadow_no_trade` outcomes нельзя интерпретировать как performance разрешённых сделок; actionable/executed cohort в приложенном snapshot равен нулю.
- Base score остаётся детерминированной эвристикой. Обучаемый слой корректирует confidence/strategy evidence только после прохождения minimum samples, class balance, purged OOF, terminal holdout и positive monetary lower-bound gates.
- Startup repair меняет только преждевременный `label_available_ts` при доказанном stored due contract. Повреждённый fingerprint, malformed horizon или несовпадающий due остаются fail-closed и не ремонтируются догадкой.

## Остаточные риски после v1.4.3 compact observability / sign audit

1. **Нет доказанного live edge.** Зеркальные тесты подтверждают математические знаки, но не прибыльность strategy family. Текущий диагностический снимок имеет нулевую actionable-когорту и недостаточный exact-policy evidence.
2. **Contract success и денежная прибыльность различаются.** Grid kill-switch может дать `success=0` при положительном terminal proxy P&L; оператор должен смотреть одновременно на terminal reason и net result.
3. **Shadow outcomes не равны торговым результатам.** Большинство текущих observations может быть `no_trade`/research. UI теперь предупреждает об этом, но внешние выгрузки всё ещё требуют явной проверки cohort fields.
4. **Lineage mismatch.** Старые `stats.json` и новые diagnostics нельзя объединять, если не совпадают `model_version` и `policy_fingerprint`; небольшие группы легко создают ложный вывод об эффективности направления.
5. **Disclosure-блоки требуют осознанного раскрытия.** Подробные technical fields не удалены, но скрыты по умолчанию. Для incident review оператор обязан раскрыть advanced diagnostics и скачать полный JSON.
6. **UI compaction не меняет gates.** Отсутствие сделок нельзя исправлять отключением confidence/expectancy/censoring checks без отдельного walk-forward evidence audit.

## Остаточные риски после v1.4.2 rejected-evaluation boundary

- A high rate of `trend_evaluation_rejected` may be legitimate market ambiguity or may reveal an overly conservative direction classifier. It must be analysed as diagnostic coverage, not as strategy failures or negative first-touch labels.
- Legacy databases may retain immutable outcomes attached to formerly neutral trend rows. They are preserved for audit and deliberately fail semantic-integrity readiness; operators should investigate rather than delete evidence automatically.
- `candidate_kind` prevents invalid lifecycle participation but does not prove that confirmed LONG/SHORT trends are profitable. First-touch holdout, EV lower bounds and tail-risk gates remain mandatory.
- External consumers must use `candidate_kind`, not infer a formed position solely from `bot_type=directional_trend`.

## Остаточные риски после v1.4.1 strategy-native Details fix

- A blocked `directional_trend/neutral` row is expected during parallel candidate evaluation; it is evidence that direction was not confirmed, not a third «neutral trend» strategy.
- Machine codes remain visible for audit. Exact Russian mappings cover the main strategy guards, but a newly introduced code may temporarily fall back to sanitized source text until documentation/UI mapping is updated.
- Legacy rows without `bot_type` are intentionally classified as `futures_grid`; if an externally modified database contains old non-grid rows without identity, they remain semantically unverifiable.
- This release fixes operator presentation/remediation isolation; it does not prove strategy profitability or enable exchange order submission.

## Остаточные риски после v1.4.0 strategy observability audit

- Durable waiting rows доказывают наличие обязательства рассчитать outcome, но не гарантируют наличие будущего непрерывного 1m path или funding history.
- Историческая геометрия воспроизводит сохранённый план; legacy-строки без уровней остаются разрывом и не восстанавливаются догадкой.
- Health semantic-integrity checks обнаруживают известные cross-table нарушения, но не заменяют резервное копирование и внешнюю проверку PostgreSQL production dataset.
- UI smoke-тесты проверяют mixed grid/trend fixtures и browser rendering, но не доказывают корректность конкретной биржевой сделки или live edge.
- Strategy-specific labels и router math не менялись; ошибки модели, режимный сдвиг и proxy/live execution gap остаются.

## Остаточные риски после v1.3.0 first-touch event model

- Вероятностная uncertainty является консервативным validation bound, а не доказанной индивидуальной confidence interval для каждой сделки.
- `trend-first-touch-softmax-v1` является proxy-моделью на исторических 1m OHLCV и не доказывает реальный fill, очередь, market impact или live edge.
- Одна минутная свеча может скрывать порядок TP/SL; такие строки цензурируются. Высокая доля `AMBIGUOUS` уменьшает эффективную выборку и может создавать selection bias, который необходимо контролировать отдельно.
- Трёхклассовая модель требует достаточного числа каждого события. Class collapse, слабый terminal holdout или отсутствие превосходства над null baseline оставляют модель unfitted.
- `HORIZON_EXIT` return моделируется отдельно и может зависеть от режима. Среднее timeout-значение не гарантирует устойчивость на новых режимах; router использует lower bound, но хвостовой риск остаётся.
- 12-часовой horizon остаётся исследовательским контрактом, а не доказанным оптимумом. Смена horizon требует новой label/model lineage и purged walk-forward сравнения.
- Положительное получение funding не кредитуется, но фактические расходы, slippage и latency внешнего executor могут оказаться хуже proxy.
- Реальные Bybit ордера сервис не размещает; single-order package остаётся recommendation/audit artifact.

## Остаточные риски после v1.2.0 strategy profitability router

- Router сравнивает proxy-outcome evidence, а не фактические live fills. Даже положительные lower bounds и terminal holdout не доказывают будущую прибыльность.
- Utility weights и minimum edge являются начальным versioned contract; их нельзя подбирать на terminal holdout или снижать ради большего числа запусков.
- Grid и trend outcomes одного timestamp коррелированы. Сравнение должно оставаться paired и использовать purged temporal cohorts, а не считать строки независимыми испытаниями.
- Реальное исполнение trend-пакета находится вне сервиса. Без read-only reconciliation нельзя доказать fill price, partial fills, fees, funding и TP/SL execution внешнего слоя.
- В one-way account одновременно допускается только один strategy family на символ. Это снижает конфликт позиций, но может упустить независимые возможности; менять правило можно только вместе с hedge-mode/portfolio accounting.
- Историческое имя `directional_trend_shadow_v1` сохранено ради совместимости lineage и может вводить оператора в заблуждение без актуальной документации; runtime permission определяется status/router/preflight, а не строкой версии контракта.

## Исторические остаточные риски после v1.1.0 directional trend shadow

- The score thresholds, ATR multipliers and 12-hour trend target are engineering priors, not proven optimums. They require purged walk-forward and prospective shadow validation.
- 1m OHLCV cannot reveal the order of TP and SL inside one candle; such rows are censored. It also cannot attest queue priority, market impact or actual executable fills.
- A single-position proxy omits real account margin, liquidation, partial fills and operational latency. The branch is therefore non-executable by design.
- Trend and grid can be simultaneously attractive under transitional regimes. Current routing preserves both audit rows but does not yet provide a validated capital-allocation policy between families.
- Cross-symbol correlation can make apparently separate outcomes one market event. Calibration still requires non-overlapping temporal cohorts and should additionally be reviewed by regime and symbol cluster before any execution proposal.
- The 12-hour horizon may be too short or too long for some symbols. Changing it requires a new label/model lineage and matching purge/embargo, not an environment toggle.
- Live PostgreSQL behavior was not tested without an explicitly disposable DSN; dialect/unit coverage remains mandatory but is not equivalent to a live integration run.

## Остаточные риски после v1.0.78

- Закрыт HIGH-дефект: operator TTL больше не разрешает новый перекрывающийся same-direction outcome-root. Свежая операторская публикация и статистический sample имеют отдельные roots.
- Новая model identity исключает старые actionable roots из текущей calibration lineage; после обновления система штатно должна заново накопить пригодные v11 outcomes. Это может продлить `no_trade` и не является основанием снижать gates.
- 12-часовой horizon сохранён как канонический контракт, но его оптимальность не доказана. 6/12/24 должны сравниваться отдельными versioned targets с purged walk-forward, одинаковыми costs и regime-aware diagnostics.
- Lock разделён только для точного `(venue, symbol, bot_type, direction)`. Противоположные направления и разные символы могут иметь перекрывающиеся окна; temporal validation всё равно обязана применять purge/embargo и cross-sectional cohort controls.
- OHLCV proxy не доказывает queue priority, реальные fills, market impact или live edge.

## Остаточные риски после v1.0.77

- Старые outcome exports не содержат `mean_reversion_score` и
  `label_available_ts`; offline tool помечает coverage как неполный и не
  импутирует значения.
- 90-дневное retention window увеличивает размер sparse exact-policy lane;
  selector ограничен materialized policy-evaluation roots, но ёмкость БД нужно
  наблюдать.
- Даже корректно разделённые cross-sectional rows одного timestamp не являются
  независимыми испытаниями. Для решения о порогах нужны horizon-separated folds.
- Eligibility API диагностический: он не подтверждает real execution или live
  edge и не должен использоваться как команда запуска.

## Остаточные риски после v1.0.76

- Новые labeled outcomes сохраняют терминальную причину и kill-switch diagnostics. Старые архивные строки не переписываются: если legacy outcome имеет положительный `ret` и `success=0`, но не содержит diagnostics, UI честно сообщает об отсутствии терминальной причины, а не реконструирует её задним числом.
- Outcome остаётся консервативной OHLCV proxy-моделью: он не доказывает queue priority, реальные partial fills, фактические комиссии или live edge.
- Изменение не меняет торговые gates и не делает recommendations исполнимыми автоматически.

## Остаточные риски после v1.0.75

- Terminal-selected денежный gate запрещает активацию при наблюдаемом недавнем monetary reversal, но пять timestamps не равны пяти независимым рыночным режимам и не доказывают устойчивость за пределами retained history.
- Требование минимум 80 выбранных terminal-строк может продлить `no_trade`, если confidence threshold пропускает малую долю кандидатов. Нельзя снижать этот floor только ради ускорения активации.
- Проверка остаётся OHLCV proxy: queue priority, partial fills, market impact, реальные fees/funding и account-level cross-margin PnL требуют внешнего read-only reconciliation.
- Смена model/policy identity начинает новую exact-policy когорту. Диагностика перед исправлением показывала 0 current-model outcomes и 29 078 исторических строк старых contracts; архив не является evidence v1.0.75.

## Остаточные риски после v1.0.74

- Исправление запрещает активировать денежно-отрицательную selected-policy на известной OOF-выборке, но не доказывает устойчивость edge вне retained proxy history и тем более по реальным Bybit fills.
- Новые model/policy identities начинают отдельную exact-policy когорту. Пока она не накопит 300 пригодных labels, 20 непересекающихся денежных когорт и достаточную selected подвыборку, `REQUIRE_CONF_GATE=1` штатно оставляет рекомендации в `no_trade`.
- Пять целых timestamps защищают terminal holdout от однострочного остатка и разреза cross-section, но сами по себе не доказывают пять независимых рыночных режимов. Отдельный monetary gate по-прежнему требует 20 pairwise non-overlapping temporal cohorts.
- OOF selected-policy evidence относится к историческому OHLCV proxy. Queue priority, partial fills, market impact, фактические fees/funding и account-level cross-margin PnL требуют внешней сверки.
- Если confidence threshold выбирает слишком мало строк или выбранные returns имеют lower bound около/ниже нуля, модель остаётся unfitted. Снижать threshold или sample floors только ради liveness нельзя.

## Остаточные риски после v1.0.73

- Все supervised loops теперь выходят по общему stop-event, включая LLM reviewer. Owner-safe release выполняется после возврата target в supervisor.
- `kill -9`, авария ОС и принудительное завершение во время уже выполняющегося внешнего LLM HTTP-запроса могут не дать supervisor завершить release до остановки процесса. В этом случае lease остаётся fail-closed до TTL; новый процесс не должен удалять lock другого owner.
- Исправление не меняет reviewer cadence, timeout, pending semantics или торговые gates и не является доказательством качества LLM verdict либо live edge.

## Остаточные риски после v1.0.72

- Полная архивная детализация (`detail=full`) остаётся O(N) и предназначена для редкого аудита, а не для обычного операторского окна.
- SQL-агрегации всё ещё зависят от производительности PostgreSQL и состояния индексов, но больше не передают крупный JSON-массив приложению.
- При очень большом immutable archive потребуется отдельная политика партиционирования/архивирования; v1.0.72 устраняет latency hot path, но не ограничивает рост таблиц.

## v1.0.71 - остаточные риски перезапуска и наблюдаемости исходов

Штатный shutdown освобождает runtime-lock, но при принудительном завершении процесса (`kill -9`, авария ОС, отключение питания) takeover по-прежнему возможен только после TTL. В этот период состояние `handover` не означает свежесть рынка: stale/missing данные остаются заблокированными. SQLite остаётся single-node backend; PostgreSQL нужен для распределённой блокировки между узлами.

Большая историческая очередь outcome может содержать высокую долю терминально цензурированных строк. `intrabar_extreme_order_unobservable`, `intrabar_replacement_fill_timing_unobservable`, недостаточный свечной объём и неизвестный порядок kill-switch не должны автоматически превращаться в метки. Это ограничение разрешимости OHLCV, а не ошибка worker. `shadow_exploration` не входит в exact-policy denominator/fit и не доказывает live edge.

## v1.0.69 — остаточные риски непрерывности

Постоянный идентификатор БД появляется при первом запуске 1.0.69; поэтому он позволяет надёжно сравнивать последующие обновления, но не может ретроспективно доказать, что 1.0.68 использовала ту же БД. Состояние `starting` в пределах boot grace является штатным. После завершения grace отсутствие собственного цикла переводит readiness в `degraded`.

## v1.0.68 — остаточные риски проверяемой готовности

- Длительное преобладание `no_trade` само по себе не доказывает неисправность. Оно может быть корректным результатом недоказанной положительной денежной ожидаемости, непройденной калибровки, недостаточного числа exact-policy outcomes или других обязательных gates. Окно «Здоровье системы» теперь показывает преобладающие коды, но экономическую пригодность всё равно нельзя выводить только из времени работы сервиса.
- `recommendation_readiness` анализирует только последнюю публикацию и ограничен 1000 корневыми строками. Это намеренная защита latency/памяти; раздел не заменяет исторический audit/reporting.
- Диагностический JSON содержит последние 200 операторских решений и может включать рыночные/аудитные детали. Он не содержит секретов по контракту, но перед передачей вне доверенного контура оператор всё равно должен соблюдать внутреннюю политику обращения с журналами.
- `database_schema.migration_applied=true` доказывает наличие требуемых колонок в текущей БД, а `materialization_pending=0` — завершение materialization по диагностируемому контракту. Это не является полной проверкой целостности всех исторических записей PostgreSQL или SQLite.
- Зелёное состояние инфраструктуры не является доказательством live edge, прибыльности или корректного исполнения внешним торговым слоем. Проект остаётся recommendation/audit service.

## Outcome backlog и proxy-наблюдения после v1.0.67

Закрыт подтверждённый operational risk: длительная проверка outcome-liveness и обработка backlog больше не блокируют публикацию рекомендаций в одном потоке, а остаток полностью неразмеченных строк больше не является достаточным доказательством остановки worker.

Остаточные ограничения:
- proxy-outcome по минутным OHLCV не восстанавливает очередь заявок, точный intrabar-порядок и реальные частичные исполнения; такие случаи по-прежнему fail-closed цензурируются;
- большой исторический backlog может потребовать много ограниченных циклов, хотя теперь он разгружается независимо и наблюдаемо;
- SQL-агрегация JSON снижает передачу данных и Python-память, но на очень большой PostgreSQL-таблице всё ещё зависит от индексов, размера JSON и производительности сервера;
- live PostgreSQL и реальный production backlog в этой итерации не воспроизводились без disposable DSN/production VM.

## Устранено в v1.0.66: крупные calibration/observability reads удерживали полный JSON-массив

До v1.0.66 рекомендатор мог несколько раз за один цикл загрузить до 200 000 outcome/recommendation rows и сохранить полный разобранный `reasons_json`; observability и outcome-liveness использовали `fetchall()` без верхней границы. В PostgreSQL обычный client cursor также не гарантировал ограниченную передачу результата. Это создавало многогигабайтные пики и удержание RSS allocator-ом, визуально неотличимое от утечки памяти.

Теперь большие чтения выполняются порциями по 256 строк, PostgreSQL применяет именованный server-side cursor, calibration payload компактен, а один цикл делит общий evidence context между тремя calibrators. Остаточный риск: абсолютный 24-ГБ production OOM не воспроизведён без runtime DB и process telemetry; после установки необходимо проверить, что растёт один процесс, supervisor не запускает несколько workers и RSS стабилизируется после первого refit.

## Устранено в v1.0.65: неограниченное восстановление разрыва могло исчерпать память хоста

**Подтверждённый HIGH-дефект.** Горячий минутный цикл строил все REST-окна от последней локальной свечи до текущего времени. При двухнедельном простое это давало около 61 страницы на инструмент. `_run_tasks_bounded()` заранее отправлял задачи для всей вселенной и возвращал общий список результатов только после завершения всех futures; затем сборщик создавал второй список нормализованных строк. Пиковая память поэтому росла пропорционально `число инструментов × глубина простоя`, а при нескольких процессах приложения умножалась на число Python workers.

v1.0.65 сначала пишет последние 360 минутных свечей, сохраняет границу исторического пропуска в `app_config` и восстанавливает её порциями по 360 строк. Исполнитель держит только активный набор futures. Остаточный эксплуатационный риск: внешний supervisor всё ещё может запустить несколько Uvicorn/Gunicorn workers; для этого приложения с внутренними background loops рекомендуется ровно один прикладной процесс.

## Устранено в v1.0.64: смешанная англо-русская лексика затрудняла операторское решение

UI ранее показывал внутренние торговые термины и диагностические фразы (`long/short/neutral`, `funding`, `spread`, `preflight`, `shadow`, `policy`, `kill-switch`, `leverage` и другие) без обязательного объяснения. Это создавало риск неверной интерпретации статуса, направления и причины запрета. v1.0.64 вводит единый русский словарь, преобразует динамические сообщения API и добавляет подсказки к сложным полям. Машинные коды остаются неизменными и доступны в подробной диагностике.

Остаточный риск: неизвестный новый код может получить общее русское описание до добавления отдельного словарного соответствия. Такой текст не разрешает торговлю и не ослабляет fail-closed проверки.

## Resolved in v1.0.63: technical diagnostic text occupied the decision table

The former `Причина` column rendered the first raw gate message, including thresholds and mixed Russian/English implementation terminology. This was not a compact operator signal and could dominate the entire table. v1.0.63 removes that column, maps known reason codes to short Russian hints on the decision label, and uses a bounded status fallback for unknown codes. The full original message remains auditable in Details/API as `primary_reason_detail`.

## Resolved in v1.0.62: LLM-enabled shadow outcomes could never mature

The LLM reviewer intentionally accepts only actionable candidates, but the outcome worker previously required `llm_review.status=ok` for every root when the reviewer was enabled. Explicit `shadow_no_trade` roots therefore could never be reviewed or labeled, blocking the evidence needed for calibration. v1.0.62 permits only publisher-opted-in, risk-clean shadow roots to bypass the impossible LLM prerequisite. Actionable, pending, blocked, suppressed and malformed rows remain fail-closed.

## Open limitations in v1.0.62

Outcome labels remain OHLCV-based proxy evidence rather than exchange-attested fills. `OUTCOME_WORKER_STALLED` is a diagnostic, not proof that every unresolved root is defective: attempted roots may still wait for missing closed candles or other explicit contracts. A symbol disabled after ticker and instrument-metadata absence is retried after the existing disable TTL.

## Resolved in v1.0.61: operator UI treated a bounded heuristic as reward/risk

The legacy `expected_rr` proxy had a structurally compressed scale and did not use the concrete plan's kill-switch monetary loss or current-policy outcome distribution. Even after being relabelled as capture/risk, it occupied the primary decision table and could be mistaken for actionable R/R. v1.0.61 removes it from operator-facing table/history/detail fields, removes raw rank/confidence proxies from the primary table/history, and publishes independent Plan RR plus exact-policy empirical expectancy/CI and risk buffer. The legacy field remains only for compatibility/internal diagnostics.

## Open limitation in v1.0.61: Plan RR and empirical expectancy are different evidence classes

Plan RR is scenario analysis based on generated sizing, a 70% opportunity-capture assumption, current spread/slippage/funding estimates and monotonic kill-switch stress. It is not a probability and is not based on live fills. Empirical expectancy is based on proxy OHLCV outcomes, not exchange-attested execution. Operators must not multiply or average these metrics into a single score. A recommendation remains subject to all deterministic blockers, and positive values do not prove live alpha. Existing pre-v1.0.61 rows may lack stored `operator_metrics` and correctly display unavailable.

## Resolved in v1.0.60: PostgreSQL OHLCV deadlock retry was bypassed by collector transactions

**Confirmed HIGH defect.** `db.upsert_ohlcv()` already sorted rows and retried deadlock victims only when called with `commit=True`, but the production hot/backfill paths used `commit=False` and committed later. Backfill bootstrap also persisted per task in completion order, while the hot collector rewrote derived 4-hour rows every minute. Two workers could therefore lock overlapping `ohlcv` primary-key tuples in different order; the deadlock victim surfaced as `COLLECT_ERROR` and the cycle lost its market-data write.

v1.0.60 uses canonical aggregated OHLCV transactions with rollback/retry and derives only from source timeframes touched in the current cycle. Residual risk remains: no disposable live PostgreSQL DSN was supplied, so the fix is proven by deterministic transaction/deadlock simulations and dialect tests, not by a two-session integration run against a real PostgreSQL server. Operational monitoring should still alert on SQLSTATE `40P01`, `40001` and `55P03` rather than treating them as harmless noise.

## Resolved in v1.0.58: historical outcomes were still presented as current-policy performance

The database intentionally retains immutable outcomes across releases, but the operator endpoint and modal still aggregated the full archive and labelled it as the active headline. A new model/policy could therefore appear to start with dozens of observations and an inherited win rate. v1.0.58 makes `current_policy` the API default, verifies the current model plus exact policy fingerprint by re-hashing the persisted contract, and renders the archive separately. Archive performance is research history only and does not enter the current-policy headline.

## Open HIGH risk in v1.0.58: zero-tolerance censoring can make calibration permanently non-actionable

`_apply_outcome_observability_gate()` invalidates positive expectancy and clears fitted coefficients when `censored_total`, `unresolved_total`, or `invalid_labeled_total` is non-zero. This is safe against survivorship bias, but a single permanently unobservable 12-hour grid root can disable an otherwise large exact-policy cohort forever. Legitimate proxy conditions such as gap-through stops, ambiguous replacement timing, insufficient candle volume or permanent data gaps can produce such roots, so the condition is a structural liveness risk rather than a rare UI state.

This patch does not ignore censored rows or convert them to zero loss. Either shortcut could manufacture positive expectancy. A safe follow-up requires a pre-registered partial-identification/sensitivity contract: explicit reason classes, conservative return bounds where derivable, worst-case binary labels, maximum admissible censor fraction, and chronological validation showing that actionability is robust under the pessimistic bound. Until then, `PROXY_OUTCOME_CENSORING_UNBOUNDED` correctly means shadow `NO_TRADE`, and the project is not a validated live strategy.

## Open evidence limitation in v1.0.58: profitability cannot be inferred from the release archive

No runtime SQLite/PostgreSQL database, exact exchange fill history, or terminal externally reconciled account sample was supplied. The screenshot's 72 outcomes are mixed historical lineage and are now explicitly treated as archive. Therefore neither inherent profitability nor inherent loss can be established from this ZIP. A valid conclusion requires the current-policy dataset, censor reasons, chronological proxy returns, and independently reconciled net execution PnL.

## Resolved in v1.0.57: policy contamination, censored-outcome omission and terminal-holdout refit

The previous lineage reset separated model versions but did not identify the full decision policy. Outcomes produced under different candidate thresholds, symbol universes, LLM settings or active risk limits could still share one calibrator. The fitted inner join also had no independent denominator for matured roots that could not be labeled because of gap-through stops, replacement timing, missing settlement/candle capacity or persistent data gaps. A positive labeled subset could therefore hide unbounded missing outcomes. Version `1.0.57` stores a canonical policy contract/fingerprint per root, recomputes the digest on read and fits only the exact cohort. Missing/tampered contracts become unresolved rather than trusting a copied hash. `reco_outcome_observability` counts every matured root as labeled, censored or unresolved; any nonzero omission, malformed label or disappeared cache support disables positive inference.

The prior purged OOF gate was still insufficient in two ways: a score-only mapping could be fitted in sample, and the feature model was retrained on all rows after the terminal block had been used for selection. The active v1.0.57 pipeline must beat score-only and null log-loss on aggregate purged future predictions and on the terminal future block, then retains the exact model/Platt pipeline trained before that block. Raw confidence is not a calibrated probability and cannot satisfy `REQUIRE_CONF_GATE=1`.

The direction Platt now uses the economically correct horizon-price sign target, but it still lacks an independent chronological skill gate. It is therefore descriptive audit output only. The feature model receives raw pre-decision direction confidence, preventing a mutable in-sample direction cache from changing the evaluated feature distribution.

Residual risk remains material. OHLCV labels still cannot reconstruct queue priority, exact partial fills, order-book impact or exchange liquidation. A policy-conditioned positive proxy lower bound is not live alpha. Regime dependence, multiple-comparison/model-selection uncertainty and cross-market dependence still require longer pre-registered walk-forward and exact external execution evidence.

## Resolved in v1.0.57: local execution rows were called live profitability evidence

A stopped, locally flat signed-quantity ledger could previously finalize positive PnL even though every row was supplied through the service API and no terminal exchange/account snapshot verified open orders, position, gross PnL, fees or funding. v1.0.57 requires an immutable matching terminal reconciliation before a bot enters `list_realized_net_events` or live validation. Unreconciled positive amounts are clamped to zero for risk accounting; unreconciled losses remain conservative. A reconciliation before bot stop, with residual position/orders, count mismatch or monetary mismatch cannot finalize.

Residual trust boundary: Bybit private responses are not cryptographically signed for offline verification. This service authenticates the submitting caller with `ADMIN_API_KEY` and validates internal consistency, but a trusted external read-only Bybit adapter must establish account/API provenance. The project remains recommendation/audit-only and does not implement private order placement or an OMS/EMS.

## Resolved in v1.0.56: archived outcomes were presented as current calibration evidence

A model-policy change previously advanced only calibrator cache keys while retaining the same recommendation lineage. Old outcomes could therefore be reconsidered as current evidence, and the UI displayed the whole archive as calibration progress. v1.0.56 introduces a new model lineage and separates immutable audit history from current-model and feature-eligible rows. Residual risk: a new lineage necessarily starts with no empirical evidence and remains shadow `NO_TRADE` until enough matured independent v7 cohorts exist.

## Resolved in v1.0.55: unreachable mean-reversion gate and temporal-cluster percolation

The fixed publication cutoff `0.55` was a synthetic false-positive filter, not a runtime-calibrated candidate threshold. The supplied 10,000-row PostgreSQL export had maximum `0.3510`, p95 `0.2926`, and zero passes, so the rule could suppress every recommendation regardless of separately positive grid economics. The message also incorrectly stated that commissions *gave* negative expectancy although that conclusion was not established by the score.

`MEAN_REVERSION_MIN_SCORE` is now explicit and defaults to `0.25`, which is only a selective candidate floor. Missing multi-timeframe evidence remains hard blocked. Weak valid evidence remains `no_trade`, but positive/negative expectancy is asserted only by the retained monetary-outcome gate. The default is not claimed to be optimal and should be reviewed against longer chronological outcomes; `0.25` was chosen to restore a non-empty high-tail candidate set without bypassing monetary, confidence, economics or risk gates.

The previous connected-component temporal algorithm could also merge a continuous chain of 12-hour intervals forever when each new cohort started before the prior horizon ended. Same-timestamp cross-sectional rows are now collapsed to one decision cohort and an earliest-finish greedy schedule selects pairwise non-overlapping cohorts. This prevents symbol-count inflation and transitive percolation. Residual risk remains: non-overlapping windows can still share a persistent market regime, so block bootstrap, regime-aware walk-forward and exact execution evidence remain necessary.

## Resolved in v1.0.54: feature LogReg activated without purged OOF evidence

Before v1.0.54 `fit_logreg()` trained feature coefficients on the full retained sample and returned `fitted=true` even when chronological purging left fewer than the required OOF predictions. A temporally concentrated 320-row sample could therefore expose 13 in-sample coefficients while OOF validation contained zero rows. Recommender diagnostics labelled that source `bot_logreg`, overstating probability calibration and model readiness.

The feature model now requires sufficient purged OOF logits plus a fitted Platt-on-top. Otherwise coefficients are withheld and inference degrades to score-only Platt or raw capped confidence. The residual limitation is explicit: score-only Platt is a simpler calibration fallback, not proof of feature-model generalisation or live edge. Purged OOF reduces leakage but does not replace regime-aware walk-forward, block bootstrap or exact-fill validation.

## Resolved in v1.0.53: horizon and stop liquidation could exceed observed volume

Before `grid_label_v26`, the horizon-open segment inherited the volume budget of the last in-window candle. A gap through a resting level could therefore be filled using liquidity from the wrong minute. The terminal close of residual inventory and the market-equivalent close at a kill-switch did not consume candle volume at all. The proxy could consequently close more quantity than the entire relevant minute traded.

The current ledger resets the volume budget at the exact horizon candle, requires its full OHLCV row, and shares that budget between gap fills and residual liquidation. Kill-switch liquidation shares the already-consumed breach-candle budget. Insufficient capacity makes the label unavailable. `label_available_ts` is one minute after the strategy horizon so the boundary volume is historically observable. Residual risk remains: total candle volume is only a necessary capacity bound and does not prove price-level depth, queue priority, partial fills or market impact.

## Resolved in v1.0.52: perfect kill-switch boundary execution understated tail loss

Before `grid_label_v25`, an intrabar breach stopped the proxy ledger at the configured kill-switch and liquidated residual inventory exactly at that price. If the candle continued adversely beyond the trigger, this assumed a perfect market-stop fill and systematically reduced the modeled tail loss. Example: a neutral grid sells at 101, upper kill-switch is 102, and the candle trades to 102.5; the old proxy closed the short at 102 rather than using the observable adverse bound 102.5.

The current model processes grid fills only to the boundary, then uses the candle extreme only when continuation is adverse to the remaining inventory. Favorable continuation is not credited. Gaps that skip the trigger remain unlabelable. This is conservative proxy evidence; exact stop latency, depth and slippage still require external execution data.

## v1.0.51 - historical simulation is not runtime executability

The system deliberately does not verify whether a real order could be submitted or filled at runtime. Current tick/quantity/minimum-order rules, queue position, partial fills, latency, account state and available margin are outside the historical outcome contract. Therefore `recommended` means a model signal, not an executable order.

The corrected architecture avoids a temporal error introduced in v1.0.48-v1.0.50: current instrument filters are no longer imposed on past recommendations. Residual risk remains substantial because OHLCV proxy fills cannot reconstruct order-book liquidity or actual exchange fills. Use outputs for paper/shadow analysis only.

## Intrabar replacement-order timing - v1.0.50

Before v1.0.50, the endpoint ledger immediately activated a replacement order after a modeled parent fill. A single one-minute candle could therefore fill the parent on one excursion and the newly created replacement on the reversal, even though OHLCV does not expose the parent fill time, submission latency, acknowledgement time or queue entry of the replacement. This zero-latency assumption could manufacture completed cycles, positive proxy return and calibration evidence.

`grid_label_v23` keeps replacements pending until the next candle. If the same candle would cross a pending replacement, the label is unavailable with `intrabar_replacement_fill_timing_unobservable`. Residual risk remains: next-candle activation is still a proxy and does not prove queue priority, partial fills, network latency or exchange acknowledgement; exact fills remain authoritative.

## Closed in v1.0.49 - proxy full fills could exceed total candle volume

Before v1.0.49, strict price trade-through still converted every crossed order into a full fill without comparing `qty_per_order` with the Bybit one-minute kline `volume`. A 10-unit order could therefore be marked fully bought and sold in candles whose total traded volume was only 1 unit. The same defect allowed several crossed orders to consume more quantity than the entire candle and allowed oversized initial LONG/SHORT inventory to appear at the entry open. This could manufacture completed cycles, positive `ret`, win rate and calibration evidence.

`grid_label_v22` uses aggregate candle volume as a hard necessary capacity bound for current exchange-normalized recommendations. Insufficient volume makes the label unavailable. Residual risk remains: aggregate volume does not reveal queue priority, volume at the exact level, partial fills or market impact; only exchange-attested fills can prove those.

## Closed in v1.0.48 - theoretical grid and exact-touch fills contaminated proxy calibration

Before v1.0.48, shadow outcomes could be calculated from the pre-snap ATR grid even though Bybit execution preflight later changed its prices and quantity or rejected it under `tickSize`, `qtyStep`, `minOrderQty` or `minNotional`. Separately, an OHLC high/low equal to a resting limit was treated as a confirmed fill. Both mechanisms biased proxy return upward and allowed calibration to learn from trades or geometry that were not proven executable.

The fix persists a verified `bybit_linear_filters_v1` snapshot and the snapped trade plan before publication. Current-model outcomes reject missing, mismatched or non-aligned snapshots. Resting Buy/Sell fills require trade-through below/above the level. Residual risk remains: OHLC trade-through is still a conservative proxy and cannot prove queue priority, available volume or partial fills. Final profitability requires exchange-attested fills and reconciliation.

## Closed in v1.0.46 - settled funding receipt used as canonical alpha

До v1.0.46 `_grid_outcome()` прибавляла положительный signed settled funding к `reco_outcomes.ret`. Плоский SHORT при положительной ставке или плоский LONG при отрицательной ставке мог получить `success=1` без grid profit. Поскольку `ret` используется monetary calibration и publication gate, временный carry мог выдать себя за устойчивый strategy edge, несмотря на отдельное задокументированное правило не кредитовать funding receipt.

Исправление: `grid_label_v19` включает в canonical proxy return только adverse funding cashflows; положительные receipts остаются диагностическим signed cashflow и не увеличивают proxy win rate/expectancy. Startup очищает старые outcomes и текущие bot/global/direction calibrators. Остаточный риск: exact realised PnL обязан включать фактический signed funding, но это не делает carry устойчивым alpha и не заменяет проверку price/grid edge.

## Closed in v1.0.45 - cross-symbol temporal pseudoreplication

До v1.0.45 денежная calibration считала каждую matured outcome row отдельным статистическим наблюдением. Даже после same-symbol shadow-root dedupe 80 монет, размеченных на одном перекрывающемся 12-часовом рынке, могли дать `n=80`, положительную row-level lower bound и `fitted=true`, хотя независимый временной эксперимент был один. Корреляция криптоактивов и общий market regime делали такую уверенность ложной.

Текущая реализация v1.0.55 объединяет одинаковый recommendation `ts` в один cross-sectional cohort и выбирает earliest-finish максимальный набор попарно неперекрывающихся `[ts, label_available_ts]` интервалов. Один selected cohort даёт одну mean и один recency weight. При `CALIB_MIN_SAMPLES=80` требуется минимум 20 эффективных временных наблюдений; положительными должны быть обе односторонние 95% lower bounds - row-level и cohort-level. Остаточный риск: неперекрывающиеся горизонты всё ещё могут быть зависимы из-за длительного режима, поэтому gate не заменяет block bootstrap, purged walk-forward и exact-fill validation.

## Closed in v1.0.44 — partial execution ledger treated as final exact PnL

До v1.0.44 `list_live_validation_records()` считала stopped bot пригодным для live-validation, если существовал хотя бы один execution event. Суммарный realized PnL мог относиться только к закрытой части grid, тогда как unmatched Buy/Sell quantity и открытый inventory не проверялись. Это позволяло частично реализованной прибыли попадать в exact-evidence statistics без terminal total-PnL reconciliation.

Исправление: validation eligibility теперь требует complete signed fill ledger и `abs(sum(Buy qty) - sum(Sell qty))` в строгом tolerance, а также stopped bot state. Partial/unmatched events остаются в audit API, но не участвуют в direction/symbol/portfolio stop metrics. Остаточный риск: сервис не подключается к private Bybit position endpoint; полнота внешнего read-only adapter и передача всех fills/funding по-прежнему являются обязательным external executor contract.

## Closed in v1.0.43 — positive sample mean treated as established edge

До v1.0.43 bot-specific calibration считала `weighted_mean_return > 0` достаточным monetary gate. Небольшое положительное среднее, статистически неотличимое от нуля, получало `expectancy_status=positive`; при отсутствующем fitted calibrator raw heuristic confidence мог оставаться actionable. Это позволяло запуск на шуме или до появления воспроизводимой положительной evidence-выборки.

Теперь рассчитываются weighted standard deviation, Kish effective sample size и односторонняя 95% нижняя граница среднего. Только `lower_bound > 0` снимает monetary veto. `unknown`, `insufficient` и `uncertain` остаются shadow `no_trade`; отрицательное среднее остаётся отдельным более сильным veto. Остаточный риск: normal-bound по proxy outcomes не заменяет block bootstrap, regime-aware walk-forward и exact-fill live validation. Положительная граница является необходимым gate, но не доказательством устойчивого alpha.

## Closed in v1.0.42 — stale positive calibrator without supporting rows

До v1.0.42 положительный fitted calibrator после истечения `CALIB_REFIT_INTERVAL_SEC` сохранялся, если refit не набирал `CALIB_MIN_SAMPLES`. Поскольку recommendations/outcomes очищаются по 14-дневному retention, модель могла пережить собственную доказательную выборку и продолжать влиять на confidence неограниченно долго.

Текущая семантика асимметрична fail-closed: positive/fitted evidence должно воспроизводиться из текущего retained window, иначе cache становится `insufficient` и деактивируется; negative monetary expectancy может сохраняться как консервативный veto. Keys v7/v6 гарантируют немедленный refit после upgrade. Остаточный риск: даже свежая proxy calibration не доказывает live alpha и зависит от OHLCV proxy assumptions.

## Closed in v1.0.41 — overlapping shadow outcome pseudo-samples

До v1.0.41 `shadow_no_trade` rows не участвовали в publication dedupe. Recommender мог публиковать новый outcome root каждую минуту, хотя все roots использовали перекрывающийся 12-часовой market path. Это завышало effective sample size, ускоряло fit confidence-calibration и могло создавать статистически убедительный, но не независимый proxy edge.

Текущий контракт разрешает один shadow outcome root на точный `(venue, symbol, bot_type, direction, model_version)` до конца horizon. Последующие rows остаются в audit history как non-root children. Старые rows исключены новой model/calibrator identity. Остаточный риск: даже неперекрывающиеся OHLCV proxy outcomes не заменяют реальные fills, queue priority и live PnL.

## Monetary expectancy and confidence calibration - v1.0.40

### Closed: binary hit rate could approve a losing monetary cohort

Before v1.0.40 the calibrator used `success` as the target and ignored `reco_outcomes.ret`. A strategy with frequent tiny wins and infrequent large losses could therefore receive high calibrated confidence despite negative mean return. This was a confirmed model/risk fail-open defect.

The v5 calibrator now requires finite matured returns and records recency-weighted mean return plus lower-tail expected shortfall. A sufficient cohort with non-positive weighted mean is persisted as `expectancy_status=negative`, remains unfitted, and forces `no_trade` for that bot type.

### Residual limitation

`ret` is still an OHLCV/grid-ledger proxy, not exchange execution truth. The gate is deliberately conservative but cannot prove positive live alpha, fill quality, queue position, partial fills, account-level liquidation behavior, or future regime stability. Exact execution evidence and the v1.0.39 live-validation stop remain the authoritative operational layer once real stopped-bot evidence exists.

## Tail-loss exact-evidence stop gate - v1.0.39

**Resolved HIGH/P0:** v1.0.38 required negative total PnL, negative median PnL **and** positive-bot rate below 50% before the sample-based `LIVE_VALIDATION_*_NEGATIVE_EXPECTANCY` block fired. A grid cohort with seven `+1 USDT` bots and one `-100 USDT` range-break bot therefore remained executable: total `-93`, median `+1`, win rate `87.5%`. This is a fail-open mismatch with the principal tail-risk shape of grid trading.

**Fix:** after the existing independent-bot sample floor, negative cumulative exact `realized_pnl_net` blocks the relevant direction/symbol/portfolio cohort. Median and win rate remain visible diagnostics only. Five consecutive losses still trigger the earlier direction-specific guard. Evidence remains restricted to validation-eligible stopped bots with immutable execution events, deduplicated by publication root and scoped to explicit model version.

**Residual risk:** this is a loss-containment gate, not proof of alpha. It cannot fire before enough exact stopped-bot evidence exists (8/12/20), does not estimate confidence intervals or regime-conditioned expectancy, and does not repair an intrinsically unprofitable recommender. Raw score and proxy calibration remain insufficient to establish live profitability; a monetary walk-forward comparator using exact fills, fees, funding and drawdown is still required.

## Outcome wait diagnostics - v1.0.38

**Resolved MEDIUM:** v1.0.37 reported a missing historical funding settlement as `OUTCOME_SKIP_INVALID_GRID_CONTRACT`. This did not insert a bad label, but it made a transient collector dependency look like corrupt trading mathematics and repeated the same warning every outcome cycle. v1.0.38 emits `OUTCOME_WAIT_FUNDING_SETTLEMENT`, records the exact missing timestamp/inventory, retries automatically, and rate-limits repeated log entries.

**Residual limitation:** a recommendation remains unlabeled until the public settled-funding row is available. That is fail-closed and intentional. Collector/network errors should be inspected through `COLLECT_ERROR field=funding_history`.

## Settled funding history - v1.0.37

**Resolved HIGH:** historical outcomes previously reused the recommendation-time ticker funding forecast and discarded receipts. Since the rate can change until settlement, this could create both wrong funding amounts and systematic pessimistic bias. v1.0.37 stores actual public settlements and applies their signed cashflow to modeled inventory.

**Residual risk:** the public settlement rate is exact, but proxy position quantity and price at the funding timestamp still come from the OHLCV ledger, not private account evidence. Missing settlements make a non-flat label unavailable; exact execution evidence remains authoritative for live P&L.

## Grid cost layers and repeated-cycle bias - v1.0.36

До v1.0.36 `execution_cost_bps + expected_funding_bps` вычитались из каждой завершённой grid-пары. Это создавало систематический pessimistic bias, пропорциональный числу циклов, и могло ошибочно блокировать плотные сетки. Теперь recurring grid fees, one-time market friction и position-time funding разделены. Остаточный риск: OHLCV proxy не знает maker/taker truth, partial fills и фактический fee tier; exact execution evidence остаётся обязательным для вывода о live edge.

## Bybit cross-margin Grid Bot contract - v1.0.35

**Closed defect:** previous releases generated `margin_mode=isolated` and displayed an approximate isolated-position liquidation price, although Bybit Futures Grid Bot uses cross margin and one-way position mode. That formula ignored account/bot equity interaction and could produce a false safety buffer.

The current deterministic safety gate does not claim to reproduce Bybit's private liquidation engine. It computes a conservative bot-equity stress from exact arithmetic-grid commitment, leverage, both external kill-switches, adverse inventory PnL, execution cost and a maintenance-margin reserve. Funding receipts and grid-profit recovery are not credited. `margin_mode=isolated`, missing geometry, malformed leverage or an unavailable stress calculation are fail-closed.

**Residual risk:** exact cross-margin liquidation still depends on private wallet equity, other positions/orders, risk tier, mark price, fee tier and Bybit's live engine. The external executor must recheck all private account state. The recommendation service cannot guarantee a liquidation price or production execution safety.

## 2026-07-12 neutral opening-order margin audit (v1.0.34)

### RESOLVED/CRITICAL: opposite NEUTRAL opening orders were treated as free
Version 1.0.33 correctly restored the idle bridge but inherited an incorrect v1.0.32 capital oracle: `max(Buy opening stack, Sell opening stack)`. NEUTRAL starts flat, so all initial Buy and Sell orders are opening orders and require margin availability. The old model understated committed notional/margin, overstated percentage return, and could pass payloads that lacked funds for all initial orders.

### RESOLVED/HIGH: commitment and maximum net position were conflated
v1.0.34 sums all initial neutral opening orders for `committed_notional_per_qty` and `committed_slot_count`, while retaining `max_abs_position_slots=max(Buy slots, Sell slots)` for one-way exposure. This distinction is enforced in generated payloads, snap, preflight, runtime caps and outcomes.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v15`
The denominator and sizing contract changed. First v1.0.34 startup removes only incompatible proxy outcomes and calibrators; recommendations, bot instances, trades, exact execution evidence and risk settings remain.

### RESIDUAL LIMITATION
The deterministic commitment floor does not reproduce private-account margin offsets, existing positions, fee reserve, liquidation engine or exchange-side rejection. The external executor must still query current private account/order cost. Strategy profitability remains unproven.

## 2026-07-12 dynamic off-grid bridge topology audit (v1.0.33)

### CLOSED HIGH/CRITICAL: N+1 initial orders at an off-grid reference
Version 1.0.32 treated every one of the N+1 arithmetic prices as an initial resting order when reference lay between levels. Bybit dynamic topology leaves one adjacent pivot/bridge level empty, so initial orders remain N. The old model overstated committed capital/margin and created phantom fills.

### CLOSED HIGH: excess directional initial inventory
LONG/SHORT initial inventory was derived from the same incorrect N+1 order set. v1.0.33 removes the bridge-side lot and derives inventory from the actual close-order count.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v14`
First v1.0.33 startup clears only incompatible proxy outcomes and related calibrators. Recommendations, bot instances, trades, exact execution evidence and risk settings remain.

### RESIDUAL LIMITATION
The public rules describe dynamic order placement, but the proxy still cannot reconstruct queue priority, partial fills, multiple intraminute oscillations, actual maker/taker status or exchange-side order-cost offsets. Ambiguous OHLC paths remain unavailable rather than fabricated.

## 2026-07-12 neutral one-way commitment audit (v1.0.32)

### CLOSED CRITICAL: neutral capital summed mutually exclusive sides
HISTORICAL/SUPERSEDED: v1.0.32 changed neutral commitment to the larger opening stack. The v1.0.34 audit proved this oracle incorrect for a flat NEUTRAL bot because all initial Buy/Sell orders are opening orders. Current code sums both stacks and separates that commitment from maximum one-way position.

### CLOSED HIGH: risk and preflight reused total resting-order count
Auto-snap, runtime caps and daily-loss fallback could multiply worst price by all opposite orders. v1.0.32 uses `max_abs_position_slots`, while strict preflight validates independent active, committed and maximum-position counts.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v13`
First v1.0.32 startup clears only incompatible proxy outcomes and related calibrators. Recommendations, bot instances, trades, exact execution evidence and risk settings remain.

### RESIDUAL LIMITATION
One-way commitment is a deterministic order-cost model, not private-account truth. Actual Bybit reservation may differ with existing positions, other orders, fee tier, leverage and exchange state; the external executor must re-check live available balance and order cost. Strategy edge remains unproven.

## 2026-07-12 order-quantity/gap-stop audit (v1.0.31)

### RESOLVED/CRITICAL: same-level directional lots were collapsed
The v11 ledger stored one side per price level. In directional mode, an initial TP and a replacement TP can coexist at the same adjacent level; `setdefault` discarded the replacement quantity. Repeated cycle profit, fees and position state at funding timestamps were therefore wrong. v12 stores signed integer quantity per level and applies every inferred leg.

### RESOLVED/HIGH: gap-through stop used a skipped boundary price
A previous-close to next-open jump beyond the kill-switch was processed as a continuous segment and liquidated at the boundary. The market never printed that observable path, and the ordering of resting limits, cancellation and stop execution is unknown. v12 skips the proxy label instead of understating gap loss or inventing fills.

### RESOLVED/HIGH: legacy daily-loss fallback still used grid_count
When persisted total-notional fields were absent, the guard derived `qty × max_price × grid_count`, undercounting an off-grid `N+1` topology. v1.0.31 uses `arithmetic_grid_commitment.active_order_count`.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v12`
First v1.0.31 startup clears only incompatible proxy outcomes and calibrators. Exact execution evidence and recommendation/bot audit rows remain.

### RESIDUAL: gap and intrabar execution remain unavailable without exact evidence
Skipping ambiguous gaps reduces sample size. Queue priority, partial fills, stop slippage and cancel/replace ordering require external execution evidence; OHLCV cannot establish them.

## 2026-07-12 exact commitment and intrabar-path audit (v1.0.30)

### CLOSED: interval count was treated as funded slot count
`grid_count=N` is the number of intervals, not always the number of active price levels or committed slots. For an off-grid reference there are `N+1` active levels. The old model understated margin/worst-case notional and inflated return normalization. v1.0.30 derives commitment from the exact range, reference, direction and level topology.

### CLOSED: auto-snap and runtime caps used different capital models
Generated recommendations could be corrected by one layer and then rewritten by auto-snap back to `N × reference`. All sizing, snapping, validation and runtime-cap paths now consume the same helper.

### CLOSED: two-sided OHLC could create an impossible third outcome
When both high and low excursions mattered, endpoint-only processing could return a PnL produced by neither valid ordering. v1.0.30 simulates both admissible paths and labels only path-invariant states.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v11`
First v1.0.30 startup clears only incompatible proxy outcomes and related calibrators. Recommendations, bot audit rows, trades, exact execution evidence and risk settings remain.

### RESIDUAL LIMITATION
Even path-invariant OHLCV proxy cannot prove queue priority, partial fills, maker/taker mix, gap slippage or exact execution. Strategy edge remains unproven until `grid_label_v11` is compared chronologically with immutable exact evidence.

## 2026-07-12 grid-ledger topology and protective-stop audit (v1.0.29)

### CLOSED HIGH: nearest directional TP omitted for between-level entry
LONG/SHORT initialization used an off-by-one boundary when entry was not exactly on a grid line. The nearest TP order and matching initial slot were absent, which could turn a directional gain or loss into zero. v1.0.29 restores the adjacent order symmetrically.

### CLOSED HIGH: observable open gaps and one-sided excursions ignored
The ledger compared only consecutive closes. A `100 close -> 101 open -> 100 close` completed pair was recorded as no activity. v1.0.29 processes close->open and open->close separately and includes only unambiguous single-sided OHLC excursions.

### CLOSED CRITICAL: trading continued after kill-switch breach
The old label only forced `success=0` but continued virtual orders to horizon; a later recovery could turn the stored `ret` positive after the bot should have stopped. v1.0.29 stops at the boundary, liquidates there and ignores subsequent fills/funding.

### CLOSED HIGH: invalid protective geometry remained labelable
Missing kill-switches or boundaries inside the grid range described no executable protected bot. v1.0.29 marks these contracts unavailable. A candle crossing both outer boundaries is also unavailable because OHLC cannot establish first hit.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v10`
First v1.0.29 startup clears only incompatible `reco_outcomes` and related calibrators. Recommendations, bot audit rows, trades, exact execution evidence and risk settings remain.

## 2026-07-12 post-publication entry and grid-contract integrity audit (v1.0.28)

### CLOSED/HIGH: outcome could enter before the recommendation existed
The worker used `features_ref_ts + 60` even when publication occurred after that candle had opened. This created an impossible historical fill and could materially change direction, inventory and PnL. v1.0.28 selects the first exact 1m open strictly after publication.

### CLOSED/HIGH: contradictory grid aliases simulated a different bot
Valid but different range or grid-count aliases were collapsed by first-wins/conservative-min logic. A damaged primary range could also fall through to another geometry. v1.0.28 treats these contracts as unlabelable instead of fabricating profit or loss.

### CLOSED/HIGH: invalid grid geometry was stored as a zero-return loss
`_grid_outcome` used `(0, 0.0)` both for a valid flat path and for invalid direction/geometry. v1.0.28 returns unavailable for invalid contracts; the worker logs `OUTCOME_SKIP_INVALID_GRID_CONTRACT` and does not contaminate win rate or calibration.

### CLOSED/HIGH: funding aliases could form a synthetic mixed model
Field-by-field first-wins resolution could combine a rate from one cost block with a schedule or expected bps from another. v1.0.28 requires duplicate funding fields to be valid and equal; conflicts or explicit malformed values suppress the label.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v9`
First v1.0.28 startup clears only incompatible `reco_outcomes` and related calibrators. Recommendations, bot audit rows, trades, exact execution evidence and risk settings remain.

### RESIDUAL: OHLCV remains a conservative close-to-close proxy
The worker still cannot prove intrabar order sequence, queue priority, partial fills, maker/taker truth or future realised funding. Positive proxy performance is not evidence of live edge.

## 2026-07-12 outcome label integrity audit (v1.0.27)

### CLOSED: positive net PnL could still be stored as a loss
The former activity gate required a completed neutral pair or at least 0.1% directional movement. Small profitable LONG/SHORT outcomes and profitable NEUTRAL residual inventory therefore had `ret > 0` but `success=0`. v1.0.27 defines success by positive liquidation-equivalent net PnL with kill-switch precedence only.

### CLOSED: exact no-event funding window could receive phantom carry
An empty exact event list was previously indistinguishable from an unavailable schedule, so stale expected-event metadata could charge funding after the next event was already known to lie outside the label horizon. Exact schedule presence now suppresses fallback event charging.

### CLOSED: duplicate cost aliases could understate execution friction
Outcome cost extraction previously trusted the first block. A zero or malformed primary alias could hide a stricter nested cost. All valid aliases now resolve conservatively to the maximum execution cost.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v8`
First v1.0.27 startup clears only incompatible `reco_outcomes` and related calibrators. Recommendations, bot audit rows, trades, exact execution evidence and risk settings remain.

### RESIDUAL: OHLCV remains a conservative close-to-close proxy
The worker still cannot prove intrabar order sequence, queue priority, partial fills, maker/taker truth or future funding rates. Positive proxy performance is not evidence of live edge.

## 2026-07-12 inventory-aware total-PnL finalization audit (v1.0.26)

### RESOLVED/HIGH: residual position omitted terminal execution cost

The v6 ledger marked residual inventory at the horizon but charged only the initial/fill legs. A still-open position therefore looked better than an economically equivalent closed position by one half round-trip cost. v7 adds terminal close friction and reports a liquidation-equivalent net horizon result.

### RESOLVED/CRITICAL: funding was charged to full grid capital instead of position value

`compute_outcomes_once` subtracted aggregate expected funding bps after the ledger. Neutral with zero inventory paid funding, and a directional bot holding half of grid capital paid the full-grid amount. v7 applies adverse funding to actual net inventory at event time; possible receipts remain excluded. If event timing is unavailable, the fallback uses maximum adverse inventory reached rather than total configured capital.

### RESOLVED/HIGH: positive net outcomes below 5 bps were labelled losses

The stored `ret` could be positive while `success=0` because an undocumented `0.0005` threshold overrode the total-PnL sign. v7 uses a numerical epsilon only, while still requiring valid mode activity and no kill-switch breach.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v7`

On first v1.0.26 startup the version guard clears only incompatible `reco_outcomes` and related calibrators. Recommendations, bot audit rows, trades, exact execution evidence and risk settings remain.

### RESIDUAL: future funding rates and intraminute event/fill ordering are unknown

The persisted recommendation contains a snapshot rate and schedule, not the future realised funding history. OHLCV also cannot prove whether a level fill occurred immediately before or after an intraminute funding timestamp. v7 is position-aware and conservative, but exact funding truth still requires external execution evidence.

## 2026-07-12 exact grid-ledger audit (v1.0.25)

### RESOLVED/CRITICAL: directional and neutral PnL did not follow the grid order ledger

The v5 proxy paired abstract up/down index movement and then applied one end-of-horizon drift to a guessed inventory fraction. It could report zero for a strongly favourable LONG path, overstate a neutral monotonic loss, and misprice adverse directional accumulation. v6 now maintains equal-quantity long/short lots, cash, replacement orders, actual grid-level fill prices and marked residual inventory.

### RESOLVED/HIGH: 70% fill-efficiency haircut corrupted completed-trade economics

A completed adjacent arithmetic-grid pair was multiplied by 0.70 before fees. The same heuristic reduced `tp_per_leg`, widened the minimum grid spacing and understated gross edge. Completed-trade gross now equals the full adjacent interval. The 70% value survives only as separately named projected opportunity capture and cannot alter canonical PnL or executable geometry.

### RESOLVED/HIGH: historical outcomes could rewrite persisted grid geometry

The prior worker enlarged the effective step from execution cost and stale aliases. A label could therefore evaluate a different grid from the recommendation. v6 derives the step only from finite `lower < upper` and strict integer `grid_count`; incomplete or contradictory geometry remains non-successful.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v6`

On first v1.0.25 startup the version guard clears only incompatible `reco_outcomes` and related calibrators. Recommendations, bot audit rows, trades and exact execution evidence remain.

### RESIDUAL/HIGH: close-to-close order inference is still not exchange truth

The ledger is materially closer to bot mechanics but intentionally conservative: it does not infer grid fills from intrabar high/low, exact queue order, partial fills, fee tiers, quantity rounding or account liquidation state. Only exact execution evidence can establish realised PnL; v6 proxy statistics remain hypothesis-validation data, not proof of alpha.

## 2026-07-12 grid outcome accounting audit (v1.0.24)

### RESOLVED/HIGH: cumulative completed grid trades were capped by grid_count

`grid_count` is a concurrent geometry/capital parameter, not a lifetime trade counter. The old label truncated repeated closes of the same intervals to at most `grid_count`, systematically understating oscillation revenue on active ranges. The worker now counts all matched close-to-close interval crossings during the exact horizon and still normalises each completed trade by total committed grid capital.

### RESOLVED/HIGH: neutral no-fill paths and directional drift had wrong PnL semantics

A neutral grid starts flat, but the old proxy charged at least one execution cost and the full entry-to-exit displacement even when no grid cell completed and no residual inventory existed. For LONG/SHORT, favourable movement was subtracted as a penalty. The new contract charges execution cost only for inferred completed trades and applies signed mark-to-market only to estimated residual inventory.

### RESOLVED/MEDIUM: outcome headline mixed actionable and shadow research cohorts

The API still exposes the combined research sample for diagnostics/calibration continuity, but now also returns separate actionable and `shadow_no_trade` cohort summaries. The operator headline renders actionable metrics; all-roots and shadow metrics remain explicitly labelled research controls.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v5`

The accounting target changed incompatibly. On first v1.0.24 startup the existing version guard clears only `reco_outcomes` and related calibrators. Recommendations, bot instances, trades and exact execution evidence are preserved.

### RESIDUAL/HIGH: OHLCV proxy is not fill truth

The model still uses exact contiguous 1m candles and cannot prove intrabar order sequence, queue priority, partial fills, per-order inventory, live fee tier or liquidation mechanics. Positive post-reset proxy expectancy must not be interpreted as demonstrated live alpha; exact execution evidence remains the decisive operational control.

# Known risks

## 2026-07-12 temporal data-lineage audit (v1.0.23)

### RESOLVED/HIGH: local receipt time could hide stale Bybit ticker data
Bybit V5 top-level response time is now propagated to ticker rows before collector freshness checks. Missing event time remains unknown; it is not manufactured from local receipt time when the exchange supplied authoritative envelope time.

### RESOLVED/HIGH: shifted candles and sparse horizons could manufacture chronology
OHLCV starts must be exact integer milliseconds aligned to both one second and the requested timeframe. Outcome entry is the exact next minute, the complete horizon must be contiguous, and exit is the exact boundary candle. Missing minutes now produce no label rather than a later substitute price.

### RESOLVED/HIGH: unavailable labels could enter calibration
Calibration now requires a valid `label_available_ts <= fit_ts` and `label_available_ts >= recommendation.ts`. Rows with missing, malformed or future maturity are excluded. Dirty persisted mandatory numeric fields are skipped; malformed optional label availability decodes to unknown and therefore remains ineligible for fit.

### DATA ACTION: proxy outcomes/calibrators reset to `grid_label_v4`
The startup label-version guard clears incompatible `reco_outcomes` and calibrator keys once. Recommendations, bot instances, trades, risk limits and exact execution evidence are preserved. New calibration remains unfitted until enough v4 labels mature.

### Remaining limitation
A complete OHLCV horizon is still a proxy: it does not prove queue priority, individual fills, partial inventory, live fees or liquidation behavior. Profitability remains unverified.


### RESOLVED/HIGH: built-in risk defaults diverged from shipped small-account profile

До v1.0.19 запуск без пользовательского `RISK_LIMITS_JSON` получал 4 concurrent bots, daily DD 200 USDT, notional 5000 USDT и margin 1000 USDT, хотя README/.env/operator instruction описывали 1 bot, DD 10, notional 500 и margin 100. Встроенные defaults и fallback normalization синхронизированы с shipped-профилем; явный операторский override по-прежнему поддерживается.

### RESOLVED/HIGH: generated qty could be silently increased

До v1.0.19 provisional sizing округлялся вверх по фиктивному `0.001`, а live auto-snap повышал qty до minQty/minNotional. Для дорогого BTCUSDT target 25 USDT превращался в 100 USDT на одну grid-заявку до учета количества интервалов. Теперь provisional qty сохраняет target notional, live alignment выполняется только вниз, а недостаточный minQty/minNotional блокируется fail-closed.
# Известные риски и ограничения

## 2026-07-11 no-recommendation state audit (v1.0.22)

### RESOLVED/HIGH: weak edge was misreported as a hard technical block

`MEAN_REVERSION_EDGE_UNCONFIRMED` означает, что валидный торговый evidence не прошёл порог стратегии. Это `no_trade`, а не ошибка Bybit, malformed payload или risk/preflight failure. Отсутствующее обязательное evidence по-прежнему остаётся hard `blocked` через `MEAN_REVERSION_EVIDENCE_INSUFFICIENT`.

### RESOLVED/HIGH: no-trade regime stopped outcome accumulation

Outcome worker исключал все `no_trade`, поэтому при полном отсутствии actionable рекомендаций новая calibration sample переставала расти. Добавлен explicit opt-in `outcome_policy.sample_role=shadow_no_trade` только для полных кандидатов без hard blocks. Worker повторно валидирует literal boolean, risk pass и пустой block list.

### RESOLVED/MEDIUM: proxy outcome UI implied real execution

Журнал использовал формулировки «что реально торговалось», хотя сервис не является OMS и outcome строится по OHLCV. UI теперь явно говорит о proxy-кандидатах и показывает shadow/non-shadow roots отдельно.

### RESIDUAL/HIGH: отсутствие рекомендаций может быть экономически правильным

Система не обязана постоянно выдавать сделки. Низкий win-rate, отрицательный avg return или отсутствие подтверждённого range edge должны приводить к `no_trade`. Shadow labels позволяют проверять, не стал ли gate чрезмерно строгим, но не дают права ослаблять его без walk-forward/exact-evidence анализа.

## 2026-07-11 independent range-edge audit

### RESOLVED/HIGH: отсутствие тренда ошибочно считалось положительным grid edge

Legacy `range_score` почти полностью равнялся `1 - trend_strength`, поэтому low-drift random walk мог получить высокий range score, положительный вклад в launch-score и эвристический `expected_rr`. Это не доказывало возвратность и создавало систематический false-positive path после комиссий. Исправлено независимым anti-persistence diagnostic, multi-TF coverage gate и hard blockers `MEAN_REVERSION_EVIDENCE_INSUFFICIENT` / `MEAN_REVERSION_EDGE_UNCONFIRMED`.

### RESOLVED/HIGH: старая калибровка могла пережить изменение feature semantics

Сохранённые v3 coefficients и legacy outcome snapshots были обучены на прежнем диапазонном proxy. Простая замена inference feature создала бы train/inference skew. Текущая модель получила identity `bybit-taxonomy-v3-mean-reversion`, calibrator keys v4, а fit фильтруется по model version и явному независимому evidence snapshot.

### SUPERSEDED BY v1.0.61: `expected_rr` выглядел как фактический reward:risk

Поле является эвристическим capture/volatility proxy и не моделирует полную monetary loss distribution, inventory path и liquidation tail. API field сохранён, но UI и reasons теперь явно маркируют его как proxy, не доказательство прибыли.

### HIGH/RESIDUAL: положительное mean-reversion evidence не доказывает net alpha

Negative autocorrelation может отражать bid/ask bounce, stale marks или краткоживущий microstructure effect, который нельзя исполнить после spread, fees, slippage, funding и latency. `MEAN_REVERSION_MIN_SCORE` является configurable candidate filter, а не оценкой expected net PnL. До достаточной независимой walk-forward/shadow выборки по exact fills проект остаётся генератором гипотез.

### MEDIUM/RESIDUAL: новые calibrators временно будут unfitted

Legacy outcomes намеренно исключены из v4 training. Confidence будет использовать raw/fallback semantics до накопления достаточного числа matured outcomes модели v3. Это безопаснее, чем переносить коэффициенты с несовместимой feature semantics, но уменьшает статистическую информативность в переходный период.

## 1. Нет реального OMS/EMS
Это главный системный риск. Проект не управляет live order lifecycle и не знает реальные open orders/fills.
Следствие: нельзя считать его завершённой автоторговой системой без внешнего execution layer.

## 2. Qty/min-notional validation зависит от фактического размера позиции
Сервис формирует provisional `params.sizing` от целевого order notional без выдуманного `qtyStep`. После получения live Bybit metadata explicit/manual quantity может только округляться вниз. Generated provisional quantity может быть поднят только до минимального исполнимого значения, после чего система обязана повторно проверить полный commitment, worst-case notional/margin и loss budget; при превышении лимита preflight блокирует recommendation. Это всё равно не заменяет sizing от баланса аккаунта и live preview фактических ордеров в Bybit: внешний исполнитель обязан повторно сверять `qty_step`, `min_order_qty`, `max_order_qty`, `min_notional`, available balance и фактическую маржу перед созданием Bybit grid bot.

## 3. Outcome labeling остаётся proxy-моделью
Даже усиленная grid-разметка не заменяет реальные fill/funding/liquidation данные.
Использовать её как единственный источник истины для PnL/WR нельзя.


## 3A. Live edge технологии не доказан

Launch-score классифицирует пригодность текущего режима для futures grid, но не является прямой оценкой математического ожидания фактической сделки. В raw-режиме `confidence` детерминированно зависит от эвристического score и контекстных penalties; это не независимая вероятность прибыли. Даже fitted calibration обучается на proxy outcomes без queue priority, partial fills, live fee tier, latency и полной траектории inventory.

Следствие: наличие `recommended`, высокий score/confidence или зелёный proxy-backtest не подтверждает прибыльность технологии. До статистически устойчивого положительного net expectancy в chronological walk-forward/shadow данных по фактическим fills система должна использоваться как recommendation/audit и hypothesis-generation layer. Продолжающаяся отрицательная expectancy после устранения execution/data defects является основанием остановить live использование и пересмотреть саму модель признаков/target, а не поднимать пороги постфактум.

## 3C. RESOLVED/HIGH: отрицательная exact-evidence expectancy не останавливала новые запуски

До v1.0.17 сервис уже сохранял immutable execution evidence и строил descriptive live-validation export, но operator execution preflight не использовал накопленный realised net PnL. Поэтому система могла продолжать materialize новые `bot_instance`, даже когда несколько независимых остановленных ботов по тому же символу и направлению подряд дали отрицательный результат. Зелёный unit-test suite этого не выявлял, потому что проверял корректность отдельных формул и persistence, а не замкнутый feedback loop «фактический результат → разрешение следующего запуска».

Исправление: preflight применяет fail-closed stop gate только к stopped bots с exact execution evidence, дедуплицирует наблюдения по immutable publication root, ограничивает cohort текущим explicit `model_version` и отдельно считает direction/symbol/portfolio cohorts. Directional gate срабатывает после пяти последовательных убытков либо после восьми независимых наблюдений с отрицательным cumulative exact net PnL; symbol и portfolio gates требуют соответственно 12 и 20 наблюдений. Median и positive rate остаются диагностикой и не отменяют агрегированный убыток. Это заранее определённый operational stop criterion, а не статистический тест alpha.

Остаточный риск: отсутствие exact evidence означает отсутствие данных для этого gate. Внешний adapter обязан передавать все fills, fee и funding; до накопления достаточной независимой выборки технология остаётся непроверенной.

## 3B. RESOLVED/HIGH: одноцикловый signal spike мог стать actionable

Сильный эвристический score ранее обходил persistence gate с `required_hits=1`, а повторные циклы могли считаться подтверждением без проверки нового `features_ref_ts`. Теперь любой `futures_grid` требует двух разных последовательно закрытых evidence snapshots; повтор одной свечи, stale/out-of-order evidence и legacy state не продвигают gate.

## 3C. RESOLVED/HIGH: UI подменял immutable recommendation при обновлении

Кнопка обновления карточки ранее искала `latest_rec_id` только по `(venue, symbol, bot_type)` и могла заменить выбранный `recommended` новым `no_trade`/другим направлением. Теперь перечитывается exact selected `rec_id`; новые публикации остаются отдельными событиями истории.

## 4. SQLite — практичный, но ограниченный backend
Для operator-grade single-node контура это допустимо. Для multi-node/multi-writer production
нужна более сильная persistence model.

## 5. Публичный Bybit REST не гарантирует полную временную согласованность
Сервис теперь fail-closed отвергает market-data/metadata responses без точного совпадения `symbol`, блокирует нецелевой `category`/non-USDT symbol ещё до REST-запроса и блокирует instrument `status != Trading`, что снижает риск валидации чужими/неактивными лимитами, но не отменяет фундаментальное ограничение публичного REST как источника execution truth.
Сервис делает защитные retry/backoff, transport/decode retry и stale checks, но не получает execution truth.
Если metadata Bybit временно недоступна на execution-path, подтверждение fail-closed блокируется, а не превращается в warning-only запуск. В recommendation-path funding interval берётся из Bybit ticker/instrument metadata; если interval отсутствует и ожидаемый funding impact материален, рекомендация получает блок `FUNDING_INTERVAL_UNCONFIRMED`. Если известен funding rate/interval, но нет `next_funding_ts`, approval-модель считает funding events консервативно по горизонту вместо нулевого carry. На execution-path полноформатные рекомендации с `cost_model` повторно сверяются со свежим funding snapshot: stale/missing interval/rate и ухудшение carry, уничтожающее net edge, блокируют запуск. Explicit sizing validation остаётся возможной только при наличии metadata с lot/notional фильтрами.

## 6. LLM reviewer может быть полезен только как вторичный фильтр
LLM не должен принимать финальное торговое решение вместо scoring/risk/shock логики.

## 7. Cross margin / hedge mode / exact live liquidation modeling не поддержаны
Текущая ревизия использует `futures_grid + cross + one_way`. Leverage допускается только после cross-margin bot-equity stress на обеих kill-switch границах. Одиночная isolated liquidation price не является safety oracle; private account state обязан повторно проверяться внешним execution/reconciliation контуром.

## 8. Telegram alerts best-effort
Оповещения не гарантируют доставку и не заменяют внешний мониторинг / process supervisor.

## 9. Raw publication history по-прежнему хранится полностью
UI/operator-list теперь по умолчанию схлопывает repeated rows одной publication-chain и адаптивно добирает raw-кандидаты,
если одна длинная chain доминирует в snapshot. Audit-след в БД при этом сознательно не удаляется.
Это правильно для расследований и калибровки, однако raw SQL-выгрузки без учёта `publication_root_rec_id`
всё ещё могут визуально выглядеть как поток похожих сигналов.

## 10. Legacy/manual payload compatibility остаётся частично семантической
Execution-time validation теперь fail-closed блокирует futures/linear recommendations без явного `margin_mode`,
а также рекомендации, для которых Bybit metadata относится к другому `symbol` или другой `category/venue`.
Это безопаснее, но означает, что старые вручную заведённые записи могут перестать быть исполнимыми без миграции payload'а.

`account_mode=one_way` сохраняется как legacy-совместимость старых тестовых/исторических rows, однако
это не полноценная модель account-mode текущей ревизии и не должно использоваться как основание для
расширения execution-логики на hedge/cross сценарии.

## 11. Рекомендательный сервис по-прежнему не заменяет внешний reconciliation с биржей
Даже после усиления row-level locking в PostgreSQL, DB-level инвариантов publication-chain и canonical directional semantics (`app.trading_semantics`) проект видит только операторские `trades`, а не реальный поток ордеров/исполнений Bybit. Поэтому окончательная truth-модель позиции, funding и liquidation всё ещё должна жить во внешнем execution/reconciliation контуре. При добавлении live executor его side/reduceOnly mapping должен быть привязан к `bybit_linear_order_semantics()` и покрыт testnet/private API tests.

## 12. Глубокие исторические retrofit-операции больше не выполняются автоматически на каждом старте
Это сознательное решение на безопасность эксплуатации. Иначе штатный restart на БД с накопленной историей может превращаться в тяжёлый full-scan recommendations/ohlcv и визуально выглядеть как зависание сервиса.

Следствие: если нужно ретро-исправить очень старые `pending`/LLM publication chains исторической БД, это следует делать как отдельную maintenance-процедуру, а не ожидать от обычного `python main.py`.


## 13. Live-price guard защищает от устаревшей рекомендации, но не заменяет real execution precheck
Execute-path теперь блокирует подтверждение, если текущий ticker вышел за рекомендованный диапазон или `kill_switch`.
Это снижает риск запуска старой сетки после резкого движения, но внешний execution layer всё равно обязан перед реальным созданием бота заново сверять цену, spread, margin, available balance и фактические лимиты аккаунта.

## Tick-size snapping and operator UI

Auto-generated operator payloads are now snapped conservatively against Bybit metadata: lower boundaries expand downward, upper boundaries expand upward, and step/TP hints round upward. This avoids a UI-only range shrink or thinner per-grid edge after tick alignment. Manual/legacy payloads remain strict: off-tick values are warnings in UI validation and blocking errors on execution preflight.

## 14. Операторская инфографика не является исполнимым контрактом
`how_to_trade.png` и `docs/HOW_TO_TRADE_INFOGRAPHIC.md` описывают quick-reference для оператора. Исполнимость всегда определяется runtime guards: risk status, Bybit metadata, live ticker, funding snapshot, publication-chain TTL, minNotional/qtyStep/minQty и LLM gate, если он включён.

Текущий shipped-профиль использует интервал `min_leverage=3` и `max_leverage=5`. Это допустимо только при fail-closed cross-margin equity-stress проверке, выделенном капитале бота и внешней проверке private wallet state. Для lower-risk профиля оператор должен явно снизить leverage limits и принять дополнительные `no_trade`/`blocked` решения.

---

## 2026-06-14 Independent full re-audit additions

### RESOLVED/HIGH: one-way same-symbol direction conflict at execution materialization
- **Files**: `app/main.py`, `tests/test_iteration168_execution_direction_conflict_guard.py`
- **Risk**: when `max_symbol_bots` is deliberately raised above 1, the numeric risk gate alone is not enough to prove that a Bybit Linear USDT one-way symbol cannot get incompatible local bot directions. A running long/short/neutral grid on the same symbol must remain the single directional source of truth unless hedge-mode is implemented explicitly.
- **Mitigation added**: execution materialization now checks running bots on the same `(venue, symbol)` inside the serialized write transaction and fail-closed blocks different or unknown directions, while still allowing idempotent re-attach to the same publication root.

### LOW/RESIDUAL: calibration fallback remains advisory and proxy-based
- **File**: `app/calibration.py`
- **Clarification**: the full LogReg + Platt path already uses chronological out-of-fold logits for the Platt-on-top stage, so the issue is not a blanket absence of time-aware validation. The score-only fallback still fits Platt on available historical proxy outcomes when the dataset is below `logreg_min_samples`.
- **Risk**: calibrated confidence can remain over-optimistic on small/non-stationary samples, especially because labels are proxy outcomes rather than real fill/funding/liquidation truth.
- **Mitigation**: effective-sample and class-balance gates remain in place; confidence must still pass risk, shock, freshness, funding, Bybit metadata and execution-preflight gates.

## 2026-06-14 fixed-leverage no-trade clarification

The shipped leverage profile is now an adaptive interval (`min_leverage=3`, `max_leverage=5`). The recommender treats ideas that cannot justify that active interval as `no_trade` / `not_actionable` instead of emitting a synthetic `1x` recommendation and letting it appear as a runtime leverage block. This does not weaken execution safety: legacy/manual `1x` rows remain blocked by execution-time leverage guards, and `no_trade` rows are not executable.

Residual limitation: the service still does not know the operator's actual wallet balance or live liquidation state; external execution/reconciliation must re-check leverage, margin, available balance and Bybit account state immediately before creating any real bot.

## 2026-06-15 execution-preflight liquidation boundary hardening

Execution preflight recomputes cross-margin bot-equity stress from canonical grid geometry and both kill-switch boundaries whenever `leverage > 1`; a manually supplied buffer is not trusted. If exact geometry or the stress result is unavailable, execution is blocked. No standalone liquidation price is published for Futures Grid Bot.

## 2026-06-15 UI numeric parsing fail-closed hardening

Resolved: the operator UI numeric helper no longer treats `null`, `undefined`, empty strings or whitespace-only strings as numeric zero. This prevents missing backend/API fields from being rendered or propagated as zero prices, zero risk distances or zero sizing context in frontend-only diagnostics. Literal numeric zero (`0` / `"0"`) remains accepted where a caller explicitly passes it, and downstream guards still reject non-positive prices where prices are required.

## 2026-06-17 deep regression audit additions

### RESOLVED/HIGH: JSON booleans could cross numeric price/qty/UI boundaries
- **Files**: `app/trading_semantics.py`, `app/main.py`, `app/bybit_client.py`, `app/calibration.py`, `app/ui/static/app.js`.
- **Risk**: Python treats `bool` as an `int` subclass and JavaScript `Number(true/false)` yields `1/0`. A malformed manual/legacy JSON field could therefore be interpreted as a real price, qty, leverage, grid count or UI level instead of becoming invalid.
- **Mitigation**: canonical directional math, execution-price extraction, Bybit metadata parsing, calibration numeric parsing and the shared UI parser now reject booleans before numeric coercion. The operator asset cache key was bumped to `manual-ui-v42`.

### RESOLVED/HIGH: chronological OOF did not prove that train labels were observable
- **Files**: `app/outcomes.py`, `app/db.py`, `app/calibration.py`, `migrations/init.sql`, `migrations/init_postgres.sql`.
- **Risk**: an outcome horizon begins at the first tradeable candle, which can be later than recommendation time. Row-order chronology alone can place a label in the train fold even though its future window had not ended when validation decisions began.
- **Mitigation**: newly computed outcomes persist exact `label_available_ts = entry_ts + effective_horizon`; OOF fitting admits a train row only when its recommendation and exact label availability are both strictly earlier than validation time. Equal timestamps and malformed availability are purged.

### LOW/RESIDUAL: legacy outcome rows lack exact label availability
Existing `reco_outcomes` rows receive a nullable schema column but are not assigned an optimistic synthetic timestamp. They remain usable for the final model fitted at the current time, because those labels are already present, but are excluded from historical OOF train folds until enough newly timestamped outcomes accumulate. This can temporarily reduce OOF/Platt coverage; it is an intentional fail-closed trade-off against leakage.


## 2026-06-18 grid-count exact integer semantics

### RESOLVED/HIGH: fractional and conflicting grid-count aliases

`grid_count`, `grid_levels` and persisted nested aliases now use an exact-integer parser and a shared alias resolver. Fractional values, booleans, non-finite values and conflicting aliases are no longer truncated or masked by truthy/falsy fallback chains. Strict execution preflight emits `GRID_COUNT_NOT_INTEGER` / `GRID_COUNT_CONFLICT`; exposure calculations use the larger valid alias while the payload remains blocked.

### RESOLVED/HIGH: canonical grid count in proxy outcomes

Grid outcome labeling now recognises canonical `grid_count` as well as legacy/nested aliases. Historical conflicting payloads use the lower valid count as an oscillation cap, preventing optimistic proxy-return inflation. This does not make proxy labels equivalent to real fills or exchange PnL.

### RESIDUAL: exchange-specific dynamic grid limit and active-order count

Bybit documents a global Futures Grid range of 2–400 grids but may lower the actual maximum for a chosen price range/economic configuration. A running bot can also have fewer active orders than its initial grid count under dynamic-order/trailing mechanics. The recommender/preflight validates the global limit, executable geometry and economic edge, but an external executor/reconciliation layer must confirm the exact Bybit UI/API constraints and live active-order state immediately before and after bot creation.

## 2026-06-18 strict trade-plan integrity and calibration zero semantics

### RESOLVED/HIGH: partial or arbitrary `trade_plan` could satisfy strict execution validation through legacy aliases
- **Files**: `app/main.py`, `tests/test_iteration193_strict_trade_plan_integrity.py`
- **Risk**: a non-empty object such as `{"marker": ...}` or a partial canonical plan could be treated as present while reference/range/kill-switch/grid-step values were silently sourced from legacy/operator aliases. This weakened the proof that the canonical execution contract itself was complete.
- **Mitigation**: strict execution now requires positive finite canonical nested values for `trade_plan.reference_price`, range lower/upper, kill-switch lower/upper and absolute grid step. Aliases remain read-only/UI compatibility data and cannot upgrade an arbitrary object into an executable plan.

### RESOLVED/HIGH: observed zero-valued calibration features were replaced by neutral defaults
- **Files**: `app/calibration.py`, `tests/test_iteration194_calibration_zero_semantics.py`
- **Risk**: Python truthiness fallbacks changed valid `0.0` observations into `0.5`, `0.67` or `0.8` for range score, directional confidence, coherence, normalized spread, liquidity tier and regime confidence. This distorted training/inference parity and could inflate probability-like confidence on weak or absent signals.
- **Mitigation**: defaults are now applied only by `_safe_float` for missing/invalid/non-finite input; valid numerical zero is preserved in both feature snapshots and legacy reconstruction.

### MEDIUM/RESIDUAL: legacy/manual grid step versus level-count mismatch remains warning-only outside generated strict geometry
A global conversion of `GRID_STEP_LEVELS_MISMATCH` to an execution error was tested but caused 31 regressions in the repository's documented legacy/manual compatibility paths and was therefore not retained. Generated strict-geometry payloads remain fail-closed. A future migration should version the execution payload schema, recompute legacy grid geometry into a canonical plan, and only then remove the compatibility warning path. The external executor must independently recompute exact order levels and active-order count before creating a real Bybit grid.

## 2026-06-18 recommendation freshness and timeline audit

### RESOLVED/HIGH: `latest_operator` could resurrect an older LLM-ready snapshot
The operator list now always uses the actual newest recommendation cycle. Status and LLM filters are applied inside that cycle and can no longer search backward and present an older row as current. Explicit historical snapshot modes remain available for diagnostics.

### RESOLVED/HIGH: invalid/future recommendation timestamps looked age-zero
Persisted recommendations with missing, non-positive, malformed or more than 300 seconds future-skewed timestamps are now fail-closed. Age is not reported as zero; execution and operator guard emit timestamp-integrity blocks and the UI identifies the clock error explicitly.

### RESOLVED/MEDIUM: pair history was not observable
The details card now opens a chronological recommendation timeline for the selected `(venue, symbol, bot_type)`, including root/update publications, direction changes, persisted statuses and LLM state. Historical runtime Bybit guards are intentionally not reconstructed with current market data.

### RESIDUAL: history window and historical execution truth
The operator dialog returns at most 2000 recent publication rows and represents recommendation decisions, not exchange fills. Deep forensic export and real order/fill/PnL truth remain responsibilities of DB export and the external execution/reconciliation layer.

## 2026-06-18 history ordering, outcome direction and horizon hardening

### RESOLVED/HIGH: legacy direction casing could invert proxy-outcome economics
- **Files**: `app/outcomes.py`, `tests/test_iteration197_history_horizon_rr_regression.py`.
- **Risk**: support validation lower-cased a value such as `" SHORT "`, but the subsequent return and TP calculations compared the original string. The row therefore passed as a supported short and was then evaluated with long arithmetic, potentially inverting proxy return and contaminating calibration diagnostics.
- **Mitigation**: the outcome worker and its directional helpers now use `app/trading_semantics.py::normalize_execution_direction`; invalid directions fail closed and cannot default to long.

### RESOLVED/HIGH: JSON boolean could shorten the proxy-label horizon
- **Files**: `app/outcomes.py`, `app/db.py`, `tests/test_iteration197_history_horizon_rr_regression.py`.
- **Risk**: Python converted `label_horizon_hours=true` to `1.0`; the futures-grid lower bound then silently converted it to 6 hours instead of the canonical 12-hour label horizon. Runtime labeling and historical lineage repair could therefore treat a malformed row as mature too early.
- **Mitigation**: boolean horizon values are rejected before numeric coercion in both runtime and DB-backfill horizon resolvers, which then use the canonical bot horizon.

### RESOLVED/MEDIUM: zero coherence inflated recommendation expected R:R
- **Files**: `app/recommender.py`, `tests/test_iteration197_history_horizon_rr_regression.py`.
- **Risk**: a valid observed `coherence=0.0` was replaced by the neutral default `0.5` through a truthiness fallback. This increased the expected capture component and overstated recommendation R:R.
- **Mitigation**: defaults are now selected only for missing/invalid values; finite zero is preserved for coherence, trendiness and ATR inputs.

### RESOLVED/MEDIUM: history table order was opposite to operator workflow
- **Files**: `app/ui/static/app.js`, `app/ui/static/index.html`, `tests/test_iteration195_recommendation_history_ui.py`.
- **Mitigation**: the table in **«История и динамика»** is sorted by `ts DESC`, then `sequence DESC`, with invalid timestamps last. The API and SVG timeline remain chronological so graph semantics are unchanged. The helper sorts a copy rather than mutating the source array, and the frontend cache key was bumped.

## 2026-06-18 persistence, shock, funding and outcome integrity audit

### RESOLVED/HIGH: market-shock used at most one-row open-candle removal
`app/shock_guard.py` now validates every candle timestamp with exact-integer semantics and keeps only fully closed rows. Multiple future/open rows, booleans and fractional timestamps can no longer leak into market-shock or fast-veto calculations.

### RESOLVED/HIGH: recommendation rows were mutable through `INSERT OR REPLACE`
`rec_id` is now an immutable audit identity. Exact canonical retries are idempotent; conflicting payloads fail closed and the batch is rolled back to a savepoint. This protects direction, score, status, params and publication lineage from retrospective overwrite.

### RESOLVED/HIGH: recommendation numeric booleans crossed the persistence boundary
Boolean `ts`, score/confidence/R:R/risk, TTL and feature timestamps are rejected before SQLite coercion; `is_outcome_label_root` accepts only boolean or exact 0/1. Legacy poisoned TEXT fixtures remain supported solely to exercise downstream fail-closed readers.

### RESOLVED/HIGH: negative execution friction could create optimistic proxy outcomes
Outcome cost extraction now replaces negative execution/total/net cost with the conservative fallback. Signed funding remains separate; execution friction cannot become alpha.

### RESOLVED/HIGH: boolean funding timestamp undercounted expected events
Malformed/boolean `next_funding_ts` is treated as unknown. Funding event count therefore follows the conservative unknown-schedule path instead of rolling timestamp `1` into a seemingly valid future event.

### RESIDUAL: immutable recommendation identity changes retry semantics
Callers may retry an identical recommendation payload safely, but may no longer reuse a `rec_id` to mutate status or economics. Lifecycle changes must use dedicated state transitions/new publication rows rather than audit-row replacement.

## 2026-06-18 final fail-closed calibration/account-mode re-audit

### RESOLVED/HIGH: non-finite calibration data could become extreme confidence
`PlattScaler` and `LogRegScaler` now return neutral probability `0.5` when an input,
coefficient, intercept or derived logit is `NaN`/`Infinity`. This prevents poisoned
feature snapshots or malformed in-memory models from becoming artificial confidence
near `0` or `1`.

### RESOLVED/HIGH: malformed persisted calibrators could be activated by truthiness
Calibration loaders now require the exact model `type` and real JSON booleans for
`fitted` flags, including the nested Platt layer. Strings such as `"false"` no longer
activate a model. Invalid persisted payloads are rejected and the normal fallback/refit
path is used.

### RESOLVED/HIGH: execution preflight did not strictly prove account-mode compatibility
Strict preflight now blocks a missing account mode, any explicit unsupported mode, and
an instrument whose current Bybit `instruments-info` metadata explicitly reports
`unifiedMarginTrade=false` while the recommendation requires `account_mode=unified`.
Legacy `account_mode=one_way` remains warning-only solely for historical compatibility;
it is not interpreted as support for hedge mode.

### RESOLVED/MEDIUM: string `"false"` could corrupt publication lineage backfill
Historical dedupe backfill now treats only explicit true values as `active_reuse`.
An ambiguous or false string no longer links an independent recommendation to an older
publication root.

### RESIDUAL: public instrument capability is not authenticated account truth
`unifiedMarginTrade` proves instrument capability, not the operator account's current
UTA generation, position mode, wallet state or permissions. Because this repository has
no OMS/EMS/private-account execution layer, the external executor must still verify the
authenticated account mode, `positionIdx=0` one-way configuration, permissions, balance,
open positions and reconciliation immediately before real order creation.

### RESOLVED/MEDIUM: malformed Telegram `ok` value could suppress later alerts
Telegram transport success now requires the literal JSON boolean `true`. A malformed
HTTP 200 payload such as `{"ok":"false"}` no longer counts as delivery success and
therefore cannot start the alert cooldown after a failed notification.

## 2026-07-11 exact temporal/funding integer semantics

### RESOLVED/HIGH: fractional market timestamps could overwrite valid funding/OI keys

Bybit/collector/persistence boundaries previously used `int()` on some OHLCV, ticker, funding and open-interest timestamps. A malformed fractional value such as `1700000000.75` became `1700000000` and could replace a valid row at that logical key. These paths now use the shared exact-integer parser; malformed rows are discarded before persistence.

### RESOLVED/HIGH: malformed funding schedules could become executable assumptions

Fractional `fundingIntervalHour`, instruments-info `fundingInterval`, label horizons and next-funding timestamps were rounded or truncated into plausible schedules. They now remain unknown. Costed execution blocks an invalid interval, and an invalid next-event timestamp uses the conservative unknown-schedule event count rather than an optimistic single-event assumption.

### RESOLVED/MEDIUM: purged calibration accepted fractional temporal boundaries

Recommendation and `label_available_ts` values entering chronological OOF are now exact integers. Fractional values are excluded, preserving the proof that every training label was fully observable before its validation decision.

### RESIDUAL: legacy malformed rows are ignored, not deleted

This patch is schema-free and non-destructive. Existing manually inserted fractional funding/OI rows may remain physically present in SQLite; current readers skip them. If a forensic cleanup is required, back up the database and perform it as a separate maintenance operation. Live PostgreSQL behavior was covered by shared normalization/dialect tests but not by a disposable server integration run.

## 2026-07-11 Bybit response/request integer integrity

### RESOLVED/HIGH: zero-like malformed `retCode` could pass as success

The public client previously used `int(retCode or 0)`, so a missing value, `null`, JSON boolean `false`, an empty value, or a fractional code such as `0.5` became success code `0`. A malformed HTTP 2xx payload could therefore expose its `result` to market-data consumers. The client now requires a present exact integer; invalid shapes follow the existing retryable response-shape path and fail closed after retries.

### RESOLVED/MEDIUM: request windows silently truncated malformed integers

Kline and open-interest `limit`, `start/end` and `startTime/endTime` previously used direct `int()` conversion. Boolean and fractional values could become valid-looking query parameters, while negative or inverted windows were sent upstream. These fields now use exact-integer parsing, non-negative timestamp validation and ordered-window validation before any network request. Exact integral values such as `5.0` remain compatible.

### RESIDUAL: public REST remains snapshot data, not execution truth

Strict response and request controls prevent malformed payload coercion but do not make public REST atomic or authenticated. A future external executor must still re-check current instrument metadata, account state, wallet, positions and order constraints immediately before any real Bybit action.

## 2026-07-11 live execution spread/economics revalidation

### RESOLVED/HIGH: publication-time spread could outlive its economic validity

Execution preflight refreshed price and funding but did not reprice transaction costs from the current best bid/ask. A still-fresh recommendation could therefore pass range/kill-switch checks after spread widened enough to make its per-grid edge negative. Costed Linear USDT futures-grid recommendations now require valid live bid/ask and recompute spread, slippage, fee floor and net edge immediately before operator materialization. The same absolute spread, minimum net-edge and gross/cost-coverage gates used at publication are applied fail-closed.

### RESIDUAL: public top-of-book is still not fill truth

Best bid/ask is a stronger execution-cost input than `lastPrice`, but it does not model queue position, depth, market impact, partial fills or latency between preflight and a real external Bybit action. The repository remains a recommendation/audit service, not OMS/EMS. An external executor must refresh the order book, authenticated fee tier, account state and exact order preview immediately before creation, and must reconcile actual fills afterwards.


## 2026-07-11 execution evidence and realised PnL integrity

### RESOLVED/HIGH: aggregate trade rows could not prove what was actually executed

Legacy rows had no immutable direct `rec_id`, Bybit `execId`/transaction id, order id, fill price/qty or separate funding events. They could not distinguish model loss from execution loss and were insufficient for partial-fill-safe live validation. A new additive `execution_evidence` ledger now records each execution and funding event against immutable `bot_id -> origin_rec_id`, with unique source/external id and conflict-aware retries.

### RESOLVED/HIGH: funding was absent from realised risk accounting

The legacy net result and daily risk stream used gross `pnl - fee`. Signed funding is now stored and included as `gross_pnl + funding - fee` in both compatibility and exact evidence paths.

### RESOLVED/HIGH: slippage could be double-counted or measured against the wrong price

Bybit fill-based gross PnL already reflects actual execution prices. Slippage is therefore not subtracted again. `orderPrice` is retained as exchange evidence but is not treated as a slippage benchmark. An execution event requires a separately timestamped pre-submit/decision benchmark and computes only adverse benchmark-to-fill deviation as a diagnostic.

### RESOLVED/HIGH: mixed ledgers could double-count one bot

Supported writes fail closed if a bot already has rows in the other ledger. Defensive risk aggregation also gives exact execution events precedence over legacy aggregates in a pre-existing mixed database.

### RESOLVED/MEDIUM: exact execution identifiers were exposed by unauthenticated reads

The evidence and live-validation read endpoints require the same admin-key/loopback authorization model as mutating operator routes.

### RESOLVED/MEDIUM: runtime lock SQLite file could enter a release archive

The release builder excludes SQLite/DB/WAL/SHM artifacts, including `data/app.runtime_locks.sqlite`, and is covered by a regression test.

### RESIDUAL/HIGH: no authenticated Bybit reconciliation adapter is shipped

The repository still does not fetch private executions, transaction logs, wallet/position state or unrealised PnL. An external read-only adapter must normalize Bybit millisecond timestamps to UTC seconds, preserve raw exchange payloads, attribute funding to the correct bot, include all fee components/rebates and submit idempotently. No private order-create/amend/cancel code was added.

### RESIDUAL/HIGH: realised-only risk does not capture open inventory

The unified stream improves realised drawdown/cooldown but cannot see mark-to-market loss, liquidation proximity, unfilled grid orders or account-level cross-effects. Those remain external executor/reconciler responsibilities.

### RESIDUAL/MEDIUM: normalized monetary fields use floating persistence

Canonical ids and raw metadata can preserve source evidence, but summaries use SQLite REAL/PostgreSQL DOUBLE PRECISION. For forensic accounting at very large event counts, an external archive should retain exact source decimal strings; migration to fixed-scale decimal accounting is a separate work package.

### RESIDUAL: live edge remains unproven

The new dataset makes validation possible but contains no user fills by itself. Positive expectancy still requires sufficient independent stopped bots, chronological walk-forward evaluation, no-trade/comparator baselines, regime cohorts, calibration reliability and a predefined stop criterion.

## 2026-07-11 outcome capital and daily-risk correction (v1.0.21)

### RESOLVED/HIGH: one directional TP leg could label a losing whole grid as successful

The previous kill-switch precedence fix still allowed `tp_success` to replace a negative whole-grid proxy whenever the price touched `tp_per_leg` and then finished with a large unresolved adverse inventory loss without crossing the outer kill-switch. OHLCV cannot prove that the remaining grid inventory was closed. A per-leg TP is now diagnostic only; `success=1` requires matched oscillation cycles, intact kill-switch geometry and a positive whole-grid net proxy.

### RESOLVED/HIGH: grid proxy return used one-order percentage as full-grid return

Each completed step previously added `step_pct × fill_efficiency` directly to `ret_proxy`, regardless of whether the grid committed 2 or 20 capital slices. The same path therefore produced the same percentage return for different grid sizes and overstated strategy outcomes approximately in proportion to `grid_count`. Gross leg return and per-leg execution cost are now normalized by committed grid capital.

### RESOLVED/HIGH: new grid could exceed the remaining daily drawdown budget

The existing gate blocked only after realised `daily_dd` reached `max_daily_dd_usdt`. It did not ask whether the next grid's conservative loss to kill-switch could fit inside the remaining budget. Execution preflight now estimates adverse kill-switch loss from the largest available/derived position notional and explicit execution friction, and blocks `DAILY_LOSS_BUDGET_EXCEEDED` when the prospective loss is larger than `max_daily_dd_usdt - daily_dd`.

### DATA ACTION: proxy outcomes and calibrators reset to `grid_label_v3`

The return denominator and success semantics changed. On first startup, v1.0.21 deletes legacy `reco_outcomes` and the associated calibrator keys through the existing version-reset path. This does not delete recommendations, bot instances, trades, exact execution evidence or schema data.

### RESIDUAL/HIGH: kill-switch loss remains a conservative model, not live inventory truth

The guard assumes the persisted/derived maximum position notional can be exposed across the adverse reference-to-kill distance. It cannot observe open orders, partial fills, average inventory price, exchange liquidation mechanics or account-level cross-effects. The external read-only executor/reconciler remains required.

## 2026-07-11 outcome/funding integrity correction

### RESOLVED/HIGH: directional TP touch could override a kill-switch breach

The proxy grid label previously allowed `tp_success` to win over a lower/upper kill-switch breach in the same evaluation horizon. A stopped directional grid could therefore be stored as `success=1`, and that false positive could enter bot-specific calibration. Kill-switch breach now has fail-closed precedence, including ambiguous same-candle TP/kill touches; the isolated TP leg no longer replaces the loss/breach proxy.

### RESOLVED/HIGH: recommender rounded malformed funding intervals

Collector/execution boundaries already required exact integer minutes, but `_estimate_cost_model()` still accepted a fractional interval and rounded it into a confirmed schedule. A malformed `720.5` minute value could reduce the canonical 12-hour horizon from two conservative 8-hour events to one apparent event. The cost model now distinguishes missing from invalid intervals and keeps malformed evidence uncertain.

### RESOLVED/MEDIUM: outcome worker truncated recommendation chronology

Legacy/direct SQLite rows with fractional `recommendations.ts` or `features_ref_ts` were converted through `int()`, creating a synthetic decision/entry chronology. Such rows are now skipped with `OUTCOME_SKIP_INVALID_TEMPORAL_FIELDS` and cannot enter `reco_outcomes` or calibration.

### RESIDUAL: proxy outcomes remain non-execution evidence

The correction removes known false-positive paths but does not make OHLC-based grid labels equivalent to actual fills, queue priority, partial fills, open inventory, Bybit fee tier, liquidation waterfall or realised account PnL. Exact execution evidence and chronological comparator validation remain required before any profitability claim.
