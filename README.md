## Отдельная directional trend-ветка в shadow-режиме (v1.1.0)

Версия 1.1.0 добавляет самостоятельный `directional_trend` рядом с существующим `futures_grid`. Это не переименование `long_grid`/`short_grid`: сеточные варианты по-прежнему зарабатывают на повторных пересечениях уровней и остаются range/mean-reversion механикой с направленным bias. Новая trend-ветка выбирается только при согласованном long/short тренде на нескольких таймфреймах, положительно оценивает `trend_strength`, direction strength и coherence и не требует mean-reversion gate.

`directional_trend` создаёт один proxy-position с отдельными TP/SL, без grid levels, усреднения и pyramiding. В этой поставке он **только shadow/research**: каждая строка имеет `status=no_trade`, код `DIRECTIONAL_TREND_SHADOW_ONLY`, не создаёт `bot_instance` и блокируется execution-preflight. Исполнимым продуктом остаётся только `futures_grid`.

Trend-outcome имеет отдельные контракты `directional_trend_shadow_v1`, `directional_trend_label_v1`, audit identity `bybit-taxonomy-v11-separated-operator-outcome-lineage+directional-trend-v1` и calibrator `logreg_directional_trend_v1`. Outcome использует непрерывный 1m path, первое однозначное TP/SL, консервативный stop-gap, exact horizon-open, фактические funding settlements и полную round-trip cost-модель. Если одна свеча касается TP и SL либо до выхода отсутствует минутная свеча, наблюдение цензурируется fail-closed.

Существующая grid-lineage и `grid_label_v26` не сбрасываются. Legacy global calibrator остаётся grid-only diagnostic; вероятностное решение никогда не использует pooled grid+trend labels. Наличие proxy trend outcomes не доказывает live edge и не разрешает торговлю.

## Разделение operator TTL и outcome horizon (v1.0.78)

Версия 1.0.78 устраняет смешение двух разных временных контрактов. `RECO_TTL_SEC` остаётся **операторским TTL свежести**: после его истечения прежняя publication-chain не может быть исполнена и новый подтверждённый сигнал вправе получить свежий `publication_root_rec_id`. Статистическая независимость задаётся отдельно: пока исходная псевдо-позиция того же `(venue, symbol, bot_type, direction)` находится внутри label horizon, свежая операторская публикация наследует её `outcome_root_rec_id`, получает `is_outcome_label_root=false` и не создаёт второй перекрывающийся обучающий пример.

Для поддерживаемого `futures_grid` канонический horizon сохранён равным 12 часам. Это не эмпирически доказанный оптимум, а действующий target contract `grid_label_v26`: 6 часов быстрее дают метки, но чаще обрезают медленный цикл сетки и наблюдение funding; 24 часа дают более полный путь, но вдвое замедляют накопление независимых когорт и сильнее смешивают режимы. Менять 12 часов без отдельного purged walk-forward сравнения 6/12/24 нельзя: это меняет целевую переменную, maturity/embargo и пригодность всей калибровочной lineage. В этой итерации label math не менялась.

В SQLite/PostgreSQL добавлена materialized-колонка `recommendations.outcome_root_rec_id`. Operator history теперь отдельно показывает операторские publication roots и независимые outcome windows. Model identity повышена до `bybit-taxonomy-v11-separated-operator-outcome-lineage`, поэтому прежние потенциально перекрывающиеся actionable roots не используются как evidence новой модели.

## Exact-policy evidence и offline walk-forward (v1.0.77)

Версия 1.0.77 разделяет понятия «тот же проверенный policy fingerprint» и
«допущено к калибровке». `/api/v1/outcomes/stats` теперь публикует
взаимоисключающие eligibility-когорты, `mean_reversion_score`, gate values и
причины допуска/исключения для каждой строки. Обычный outcome archive остаётся
14-дневным, а sparse exact-policy candidate lane хранится 90 дней и повторно
проверяется по immutable policy contract при чтении.

В `scripts/offline_walk_forward.py` добавлен purged walk-forward по score,
mean-reversion и direction. Он использует только labels, доступные до validation
timestamp, и не подбирает production thresholds. Фактический разбор приложенного
среза включён в каталог `docs`. Universe и торговые пороги в этой версии не
менялись.

## Диагностика терминального исхода и строгая числовая семантика UI (v1.0.76)

Версия 1.0.76 устраняет разрыв между математикой proxy outcome и операторским журналом. Расчётчик `grid_label_v26` и ранее корректно считал `success=1` только при положительном net proxy P&L и отсутствии kill-switch, однако при сохранении завершённой метки терялись терминальные diagnostics. Поэтому архив мог показывать положительный P&L рядом с «Неуспех», не объясняя, что причиной был пробой защитной границы.

Теперь `_grid_outcome` сохраняет terminal reason, сторону и цену kill-switch, наблюдавшийся экстремум, цену консервативной ликвидации и итоговый net proxy return в существующем `reco_outcome_observability.details_json`. Enriched outcomes API передаёт diagnostics frontend-у, а журнал явно разделяет «Исход по правилам стратегии», «Расчётный net proxy P&L» и «Причину исхода». Boolean, пустые и non-finite значения больше не преобразуются UI в `1`, `0` или `0%`. Схема БД, API-маршруты, `grid_label_v26`, калибровочная lineage и торговые gates не изменены.

## Денежная проверка выбранной политики на итоговом периоде (v1.0.75)

Версия 1.0.75 закрывает HIGH-дефект активации вероятностной модели. В v1.0.74 итоговый whole-timestamp holdout проверял binary log-loss модели, а денежные lower bounds выбранной порогом политики считались только по объединённым OOF-строкам. Поэтому старые прибыльные периоды могли перекрыть денежный убыток выбранной политики в последних пяти временных когортах, и модель всё равно становилась `fitted`.

Теперь тот же exact confidence selector отдельно применяется к terminal holdout. Активация требует не менее `CALIB_MIN_SAMPLES` выбранных terminal-строк, не менее пяти целых decision timestamps, положительного row-level Student-t lower bound и положительного temporal lower bound. `negative`, `uncertain` или `insufficient` дают `terminal_selected_policy_unproven`; raw confidence остаётся audit-only, а рекомендация — `no_trade`. `/api/v1/status`, `confidence_model` и UI показывают отдельные terminal-selected diagnostics.

Контракты обновлены до `bybit-taxonomy-v10-terminal-selected-policy-money`, `candidate-policy-v3`, bot/global calibrators v21; FastAPI/cache build — `1.0.75`. `grid_label_v26`, direction v14, API routes, SQLite/PostgreSQL schema и `.env` не изменены. Приложенная диагностика 1.0.74 содержала 0 current-model outcomes, поэтому смена lineage не отбрасывает уже накопленную пригодную калибровочную выборку; исторические 29 078 outcomes остаются архивом. Исправление не доказывает live edge и не добавляет выставление ордеров.

## Денежная проверка выбранной моделью политики и безопасный terminal holdout (v1.0.74)

Версия 1.0.74 закрывает два дефекта калибровки, из-за которых формально принятая модель могла не соответствовать денежной цели. Раньше положительное ожидание проверялось на всей pre-calibration candidate-когорте, а затем LogReg обучался на бинарном `success`. Поэтому модель могла выбрать группу с высокой долей мелких выигрышей, но отрицательным средним денежным результатом, отбросив редкие крупные выигрыши другой группы. Теперь каждая purged OOF-вероятность проходит тот же adaptive blend, тот же записанный context/OI/shock multiplier и тот же `MIN_CONF_TO_RECOMMEND`, что runtime publication gate. Активация требует положительных row-level и temporal Student-t lower bounds именно у этой выбранной подвыборки.

Terminal validation больше не строится остатком целочисленного fold. Границы всех validation blocks совпадают с границами целого recommendation timestamp; terminal block содержит не менее `CALIB_MIN_SAMPLES` строк и пяти целых decision timestamps. Недостаточная история, испорченные selection inputs, маленький terminal block, отсутствие бинарного skill или неположительное selected-policy expectancy оставляют модель unfitted и confidence audit-only.

Контракты обновлены до `bybit-taxonomy-v9-selected-policy-terminal-cohorts`, `candidate-policy-v2`, bot/global calibrators v20; FastAPI/cache build — `1.0.74`. `grid_label_v26`, direction v14, API routes, SQLite/PostgreSQL schema и `.env` не изменены. Из-за изменения политики начинается новая exact-policy когорта; прежние рекомендации и исходы сохраняются как архив. Исправление не доказывает будущую или live-прибыльность и не добавляет выставление ордеров.

## Завершение LLM reviewer без зависшего runtime-lock (v1.0.73)

Версия 1.0.73 исправляет shutdown-контракт фонового LLM reviewer. Его цикл теперь, как и остальные supervised background loops, проверяет общий stop-event перед следующим проходом. При штатном завершении после текущего прохода target возвращается в supervisor, состояние фиксируется как `stopped`, а принадлежащий процессу `runtime:llm_reviewer` удаляется owner-safe операцией. Это исключает прежний повторный sweep после сигнала остановки и сокращает риск наследования живого lease при обычном restart.

Торговые пороги, LLM verdict semantics, recommendation statuses, policy fingerprint, outcome/calibration contracts, API, схема SQLite/PostgreSQL и `.env` не изменены. Аварийное завершение процесса и уже выполняющийся внешний LLM-запрос по-прежнему могут оставить lock до штатного TTL; новый процесс обязан ждать takeover и не должен обходить блокировку.

## Быстрые окна «Здоровье» и «Исходы» на крупной БД (v1.0.72)

Версия 1.0.72 устраняет линейный рост времени открытия диагностических окон по мере накопления истории. `/api/v1/status` больше не переносит и не разбирает `reasons_json` всех архивных исходов: исторические totals/class balance считаются SQL-агрегацией, а полная проверка feature/policy lineage выполняется только для текущей версии модели. Exact-policy observability фильтрует `policy_evaluation_eligible=1` и созревшие строки до JSON-декодирования.

Окно «Исходы» запрашивает полную детализацию только для текущей policy-когорты. Исторический архив отдаёт отдельный `detail=summary`: headline/cohort totals считаются в SQL, а JSON читается лишь для ограниченного списка последних 20 записей. Оба окна открываются сразу и показывают состояние загрузки. Торговые пороги, policy fingerprint, fail-closed цензурирование, LLM-gates и правила допуска к калибровке не изменены.

## Безопасный перезапуск Windows/PostgreSQL и целостный журнал (v1.0.71)

Версия 1.0.71 объединяет исправление исследовательских `shadow_no_trade` из промежуточной 1.0.70 с безопасной передачей фоновых runtime-lock при перезапуске. Штатно завершающийся процесс теперь освобождает принадлежащие ему блокировки `collector`, `backfill`, `futures_meta`, `sentiment`, `reco`, `outcomes` и `llm_reviewer`; новый процесс не обязан ждать полного TTL старого lease. Если предыдущий процесс завершился аварийно, `/api/v1/status` различает состояние `handover` и реальную остановку, показывает владельца/heartbeat блокировки и время до разрешённого перехвата. Устаревшие данные при этом остаются fail-closed и не становятся торгово пригодными.

Локальный advisory-LLM больше не блокирует расчёт риск-чистых исследовательских исходов с `policy_evaluation_eligible=false`: такие результаты сохраняются как `shadow_exploration`, но не входят в exact-policy калибровку и не разрешают торговлю. Журнал решений локализуется по точным кодам: машинные `action`, `reason`, `rec_id` и версии сохраняются без разрушительного пословного перевода. Причины вроде `intrabar_extreme_order_unobservable` и недостаточного свечного объёма остаются терминальным fail-closed цензурированием; версия не придумывает внутрисвечную последовательность или исполнение.

## Целостность диагностики после перезапуска (v1.0.69)

Версия 1.0.69 исправляет два подтверждённых дефекта диагностики версии 1.0.68. Сводка последней публикации теперь включает все строки цикла, а не только новые outcome-root. После перезапуска состояние остаётся `starting`, пока текущий процесс сам не завершит цикл сборщика и публикацию. В `/api/v1/status` добавлены `runtime_provenance` и `database_continuity` с безопасным постоянным идентификатором БД и агрегированными счётчиками истории. Торговые, риск- и calibration-gates не изменены.

## Проверяемая готовность оператора и единая семантика статусов (v1.0.68)

Окно **«Здоровье системы»** теперь отвечает на два разных вопроса: работает ли инфраструктура и существует ли сейчас хотя бы одна разрешённая сделка. Оно совместно читает `/api/v1/health/symbols`, `/api/v1/status` и последние 200 операторских решений, показывает версию приложения, применение outcome-миграции, состояние materialized-полей, фоновых потоков, outcome-worker, калибратора, число `МОЖНО ТОРГОВАТЬ` / `НЕ ТОРГОВАТЬ` / `ЗАБЛОКИРОВАНО` и ранжированные причины последней публикации. Состояние `healthy_not_actionable` означает: обязательные контуры работают, но текущий набор правил ещё не доказал достаточную экономику/калибровку либо не прошёл иные обязательные gates. Это не технический сбой и не разрешение торговать.

Из окна здоровья можно скопировать или скачать единый диагностический JSON. Для разбора production-состояния оператору достаточно прислать этот файл: он содержит health/status payload и последние 200 решений, но не содержит `.env`, API-ключи или полный DSN.

Визуальная семантика статусов унифицирована во всех основных поверхностях: **`НЕ ТОРГОВАТЬ` и `ОЖИДАЕТ ПРОВЕРКИ` — жёлтые**, **`ЗАБЛОКИРОВАНО` — красное**, **`МОЖНО ТОРГОВАТЬ` — зелёное**; статус всегда различается также текстом. Главная таблица теперь имеет шесть колонок: **символ · направление · RR плана · доходность по наблюдениям · решение · детали**. Кнопка **«Детали»** находится в отдельном крайнем правом столбце, что позволяет последовательно перебирать строки без смещения точки нажатия.

Торговые пороги, fail-closed gates, калибровка, outcome target, API действий и recommendation/audit-only граница не изменены. Версия 1.0.68 добавляет наблюдаемость и исправляет UI-контракт, но не превращает отсутствие доказательств в торговый сигнал.

## Независимый контур обработки наблюдений (v1.0.67)

Обработка созревших учебных наблюдений вынесена из рекомендательного цикла в отдельный supervised worker `outcomes` с собственной распределённой блокировкой `runtime:outcomes`. Большая очередь результатов больше не задерживает публикацию новых рекомендаций. Каждый цикл сохраняет устойчивое состояние и показатели прогресса: выбранные и просмотренные строки, созданные метки, ожидания, окончательное цензурирование, ошибки, длительность, старейшую запись и последний `rec_id`.

Состояние очереди теперь различает `processing`, `backlog`, `stalled`, `error` и `ok`. Наличие большого остатка само по себе не считается остановкой, если недавний цикл реально продвинул очередь. Диагностика выполняет SQL-агрегацию и получает не более десяти примеров идентификаторов вместо передачи и разбора всех `reasons_json` в Python. При наличии терминального прогресса backlog обрабатывается ускоренными контролируемыми циклами; fail-closed правила пригодности и сеточного исполнения не ослаблены. Неоднозначные сеточные траектории получают явные машинные причины вместо общей неизвестной ошибки.

Схема SQLite/PostgreSQL расширена шестью служебными материализованными полями eligibility/LLM и двумя индексами. `db.init_db()` добавляет их идемпотентно и один раз заполняет legacy-строки ограниченными пакетами; ручная SQL-миграция не требуется. Переменные окружения, публичные статусы рекомендаций и recommendation/audit-only граница не изменены. Новые API-поля состояния и метрики являются обратно совместимыми диагностическими дополнениями.

## Ограниченная память калибровки и диагностических выборок (v1.0.66)

v1.0.66 устраняет повторную материализацию крупных `reasons_json` в горячем цикле рекомендаций и при чтении состояния системы. Observability, outcome-liveness и lineage-статистика теперь читаются порциями; PostgreSQL использует именованные серверные курсоры. Global, bot-specific и direction calibrators в одном цикле используют общий компактный набор exact-policy outcomes вместо трёх независимых загрузок до 200 000 строк. `/api/v1/status` агрегирует lineage в потоке и не удерживает полный исторический массив. Схема БД, policy fingerprint, outcome target, торговые статусы и API-поля не изменены.

На синтетической проверке 4 000 строк с дополнительным диагностическим блоком около 25 КБ удерживаемая Python-память calibration reader снизилась с 104,85 МБ до 8,44 МБ по `tracemalloc`; это проверка топологии памяти, а не обещание конкретного RSS на production VM. Для подтверждения устранения именно 24-ГБ OOM после обновления необходимо наблюдать `runtime.process_memory` и журнал supervisor на реальной машине.

## Быстрый запуск после длительного простоя и ограничение памяти (v1.0.65)

При разрыве истории больше шести часов минутный сборщик больше не пытается удержать в памяти и загрузить весь пропуск до публикации свежих данных. Горячий цикл одним запросом получает последние 360 минутных свечей по инструменту, сразу записывает их в БД и создаёт устойчивое фоновое задание на восстановление пропущенного диапазона. Фоновый цикл заполняет пропуск порциями не более 360 свечей и не более `BACKFILL_PER_TF_BUDGET` инструментов на временной интервал за один цикл.

Параллельный исполнитель REST-задач теперь ленивый: одновременно существуют только `COLLECTOR_MAX_WORKERS` futures, а не futures для всего списка инструментов. Это исключает прежнее удвоенное накопление сырых и нормализованных свечей при двухнедельном и более простое. В окне «Здоровье символов» показываются текущая и пиковая память процесса Python, число потоков, размер максимального буфера, число порций фонового восстановления и оставшиеся задания.

Штатные безопасные значения: `BACKFILL_FULL_SWEEP_ON_WARMUP=0`, `BACKFILL_PER_TF_BUDGET=8`. Сервис следует запускать одним прикладным процессом (`python main.py` или один worker Uvicorn): каждый дополнительный worker импортирует полный Python/ML runtime и создаёт собственные фоновые потоки, даже если runtime-lock не даёт им одновременно писать один и тот же поток данных.

## Русский операторский интерфейс (v1.0.64)

Интерфейс оператора переведён на однозначную русскую терминологию. В видимых названиях, статусах, подсказках, карточках, журналах, экране наблюдений, экране состояния и сообщениях больше не используются англоязычные торговые термины, если для них существует понятный русский эквивалент. Исключения: **LLM**, **UI**, **RR**, собственное имя **Bybit**, обозначение расчётной валюты **USDT** и машинные идентификаторы, которые нужны для аудита.

Основные соответствия: `long` → **Покупка (рост)**; `short` → **Продажа (снижение)**; `neutral` → **Нейтральная сетка**; `funding` → **платёж финансирования**; `spread` → **разница цен покупки и продажи**; `slippage` → **проскальзывание**; `preflight` → **предзапусковая проверка**; `kill-switch` → **аварийная граница выхода**; `policy` → **набор правил**; `shadow outcome` → **учебное наблюдение**.

Главная таблица остаётся минимальной: **символ · направление · RR плана · доходность по наблюдениям · решение**. Сложные показатели снабжены подсказками, доступными при наведении мыши и клавиатурном фокусе. Полные значения, пороги, внутренние коды и исходные диагностические поля находятся в **«Деталях»**; машинные коды API и БД не переименованы, чтобы не нарушить совместимость.

## Compact decision hints (v1.0.63)

The primary recommendation table now has five visible columns: symbol, direction, Plan RR, empirical expectancy and decision. The former separate reason column was removed. Hovering or focusing the decision label shows one short Russian operator hint, while raw diagnostic text, codes, thresholds and model details remain only in **Details**. Unknown internal reason codes fall back to a bounded status-level phrase and can no longer leak long mixed-language diagnostic payloads into the table.

The additive `operator_summary` keeps `primary_reason_code`, publishes the short `primary_reason`, and retains the original message as `primary_reason_detail`. No trading gate, database schema, policy fingerprint, outcome lineage or execution boundary changed.

## Runtime outcome recovery and minimum operator table (v1.0.62)

v1.0.62 fixes an LLM/outcome bootstrap deadlock: explicitly opted-in, risk-clean `shadow_no_trade` roots can now mature without an LLM verdict, while actionable recommendations still require the completed LLM verdict when that gate is enabled. Existing matured roots are processed automatically by the normal outcome worker; no data rewrite or schema migration is required. `/api/v1/status` now exposes `outcome_worker` liveness and reports `OUTCOME_WORKER_STALLED` when matured eligible roots remain unattempted.

The primary recommendation table is intentionally limited to five visible decision fields: symbol, direction, Plan RR, empirical expectancy and decision. One short reason is available as a tooltip on the decision label. All confidence, risk-buffer, price, range, sizing, funding, calibration and diagnostic fields remain in **Details**. The API publishes an additive `operator_summary` contract so Plan RR and the primary reason do not depend on frontend re-parsing of technical payloads. Bybit retCode `10006` retries honor the exchange reset timestamp, and an exact ticker miss is converted into a temporary symbol disable only after public instrument metadata also confirms absence.

## Operator decision metrics: Plan RR and empirical expectancy (v1.0.61)

The main recommendation table is decision-focused: raw direction-confidence, rank, model-confidence and the visible minimum-confidence filter were removed from the primary surface. It no longer presents legacy `expected_rr` as a trading reward/risk measure. That field remains in stored/API payloads only for backward compatibility and internal heuristic diagnostics; the frontend does not render it. Operator-facing economics are now split into two independent contracts:

- **Plan RR** is a scenario metric for the concrete generated grid. The numerator is projected completed-pair net P&L after recurring pair fees, distinct one-time spread/slippage and adverse funding. The denominator is the worst applicable price loss plus terminal execution cost at the configured kill-switch. Maintenance reserve is shown in cross-margin stress but is not mislabeled as realised loss.
- **Empirical expectancy** is the mean retained proxy return for the exact current policy, preferably over non-overlapping temporal cohorts, with a two-sided Student-t confidence interval. The detail card also shows expected shortfall and a mean-to-tail ratio when both sides are estimable. A cross-margin risk buffer remains visible in the primary table.
- **Heuristic capture score** is the old bounded capture/volatility ranking proxy. It is not displayed as an operator decision metric and does not become Plan RR or empirical evidence.

Missing, boolean, non-finite or incomplete plan/cost/stress inputs make Plan RR unavailable rather than silently substituting zero cost. Empirical statistics remain unavailable until exact-policy matured evidence is sufficient and valid. Neither metric proves live profitability; no policy fingerprint, outcome label, database schema, environment variable or order-execution boundary changed in this release. Existing recommendations created before v1.0.61 may show the new fields as unavailable until a new publication stores `reasons.operator_metrics`.

## PostgreSQL OHLCV transaction-order hardening (v1.0.60)

The market-data collector now treats every OHLCV batch as its own retry-capable transaction. This closes a production PostgreSQL deadlock path where the hot collector and backfill worker inserted overlapping `ohlcv` primary-key rows through caller-managed `commit=False` transactions.

Key behavior:

- API, bootstrap and derived OHLCV writes use the existing rollback/retry boundary in `db.upsert_ohlcv(..., commit=True)`;
- bootstrap results are aggregated before persistence, so `as_completed()` network order cannot become database lock order;
- derived rows are aggregated and canonically ordered by `(venue, symbol, tf_sec, ts)` per transaction;
- the hot 1-minute collector derives only timeframes whose source series changed in that cycle and no longer rewrites 4-hour rows owned by the 1-hour backfill path;
- SQLite/PostgreSQL schemas and the recommendation, risk, outcome and operator contracts are unchanged.

This release improves liveness and market-data integrity. It does not establish strategy profitability: economic viability still requires a frozen-policy runtime evidence cohort with reconciled fills, fees, spread, slippage and funding.

## Outcome lineage truth and calibration readiness diagnostics (v1.0.58)

Version `1.0.58` fixes the operator-facing evidence view. `GET /api/v1/outcomes/stats` now defaults to `scope=current_policy`; `scope=current_model` and `scope=archive` are explicit alternatives. Current-policy rows must match the active recommendation model and the exact active policy fingerprint, and the persisted policy contract is re-hashed before a row enters the current headline. The UI obtains current-policy and archive statistics separately, uses only the current-policy cohort for the headline/tables, and labels the immutable archive as historical. Old proxy outcomes remain in the database by design; they no longer masquerade as evidence for the running policy.

Calibration readiness is now reported as two different contracts. The default monetary floor is `CALIB_MIN_SAMPLES=80`, but with the default `REQUIRE_CONF_GATE=1` the probability model cannot activate before at least 300 exact-policy labels and accepted purged OOF plus terminal-holdout skill. Reaching 80 rows is therefore not full readiness. With the 12-hour `futures_grid` label horizon and the required 20 non-overlapping temporal cohorts, even an ideal uninterrupted unchanged policy cannot satisfy the temporal floor in less than 10 days. Any policy-fingerprint change starts a new exact-policy evidence cohort. Status diagnostics expose both floors, both gaps, the theoretical temporal floor, and the zero-tolerance observability block.

The investigation did not establish that the strategy is inherently profitable or inherently loss-making: the release archive contains no runtime database, exact fill stream or reconciled account evidence. It did confirm a material liveness risk in the current research design: one censored, unresolved or invalid matured root disables an otherwise positive cohort indefinitely. That rule is left fail-closed in this patch because removing it without a pre-registered conservative bound would reintroduce survivorship bias. Treat the project as a shadow/audit system, not a validated trading strategy, until a separate bounded-censor sensitivity model and long chronological external-execution validation are completed. No schema, model identity, outcome target or environment variable changed, so this patch does not reset the current evidence cohort.

## Policy-conditioned, censor-aware calibration and reconciled live evidence (v1.0.57)

The prior calibration path could still mix outcomes selected under different runtime policies, omit matured-but-unlabelable roots, activate a probability mapping without demonstrated future-block skill, and refit the selected model on its terminal holdout. Version `1.0.57` makes those states fail closed. Every recommendation root carries a canonical SHA-256 policy fingerprint covering the model/outcome/feature schema, selection thresholds, universe, LLM gate and active risk limits. Readers recompute the digest from the persisted contract rather than trusting the claimed hash. Calibration uses only the exact fingerprint cohort and an independent `waiting` / `censored` / `labeled` denominator. Any censored, unresolved, invalid or missing supporting row disables positive expectancy and probability inference.

The current identities are `bybit-taxonomy-v8-policy-conditioned-censor-aware`, bot/global v19 and direction v14; the outcome target remains `grid_label_v26`. In-sample score-only Platt is no longer an inference fallback. Feature LogReg must beat score-only and null log-loss on purged walk-forward predictions and on the terminal chronological block; the activated coefficients and Platt mapping are the candidate trained before that terminal block. The standalone direction Platt uses the corrected horizon-price target but remains audit-only until it receives its own chronological skill gate; decision features retain the raw pre-decision direction confidence. With `REQUIRE_CONF_GATE=1`, capped raw confidence is audit-only and yields `CALIBRATED_CONFIDENCE_UNAVAILABLE` / `NO TRADE`.

Live PnL is now a separate evidence boundary. Local execution rows and legacy aggregate trades do not establish positive profitability. A stopped and locally flat bot must have a later complete `bybit_private_reconciliation` whose position, open-order count, event counts, gross PnL, fees and funding match the immutable ledger. Use `POST /api/v1/bots/{bot_id}/execution-reconciliation` to ingest a snapshot produced by a trusted external read-only Bybit private-API adapter and `GET /api/v1/execution-reconciliations` for audit. The service validates consistency but does not cryptographically authenticate Bybit response provenance and still does not place, amend or cancel orders. Until reconciliation, positive values receive zero risk/PnL credit while losses remain conservative inputs.

Existing SQLite and PostgreSQL deployments need no manual command: normal `init_db()` creates the additive `reco_outcome_observability` and `execution_reconciliations` tables and indexes idempotently. No new environment variable is required. Historical recommendations/outcomes remain audit history; the new policy fingerprint starts a separate evidence cohort.

## Calibration lineage reset and transparent dataset counts (v1.0.56)

The recommendation model identity is now `bybit-taxonomy-v7-mr-floor-temporal-cohorts`. Bot/global calibrator keys are v18 and direction calibration is v13. Existing `reco_outcomes` remain immutable audit history, but v6 outcomes are excluded from v7 fitting. `/api/v1/status` and the UI now separate historical archive, current-model outcomes, feature-eligible outcomes, fit rows and temporal cohorts. A fresh v7 deployment therefore begins with zero eligible calibration rows even when the audit archive is non-empty. No schema migration is required.

## Mean-reversion and temporal-evidence recovery (v1.0.55)

The fixed `mean_reversion_score >= 0.55` publication rule was not calibrated to the runtime distribution. In the supplied PostgreSQL export of 10,000 recommendations, the maximum was `0.3510`, the 95th percentile was `0.2926`, and no row reached `0.55`. The gate is now an explicit candidate-screen setting, `MEAN_REVERSION_MIN_SCORE` (default `0.25`). It remains fail-closed below the floor, but it no longer claims that a weak score proves negative expectancy. Profitability remains a separate retained-outcome requirement: `PROXY_MONETARY_EXPECTANCY_UNPROVEN/NON_POSITIVE` still keeps the recommendation in shadow `no_trade` until uncertainty-bounded monetary evidence is positive.

Temporal validation no longer merges an indefinitely chained sequence of partially overlapping horizons into one permanent connected component. Rows published at the same recommendation timestamp are collapsed to one cross-sectional decision cohort, then the standard earliest-finish interval-scheduling rule selects a maximum-cardinality set of pairwise non-overlapping cohorts. Many symbols in one decision still count once, but continuous operation can now accumulate genuinely non-overlapping time evidence.

FastAPI version is `1.0.55`; bot/global calibration identities are v17. Outcome contract remains `grid_label_v26`, direction calibration remains v12, and model identity is unchanged. Existing outcomes are retained and re-evaluated under the v17 temporal contract. DB schema and public routes are unchanged; existing deployments may omit the new env variable and receive the default `0.25`.

## Purged OOF activation gate for feature calibration (v1.0.54)

Bot-specific feature LogReg is no longer exposed as calibrated confidence merely because a full-sample fit produced coefficients. The feature model now requires at least `CALIB_MIN_SAMPLES` genuinely out-of-fold predictions from the existing purged chronological validation path, followed by a fitted Platt-on-top calibrator. If temporal concentration or label-availability purging leaves fewer validation predictions, the feature coefficients are withheld and confidence degrades to the simpler score-only Platt baseline (or raw capped confidence if that baseline is also unavailable).

`confidence_model` now reports `purged_oof_status`, `purged_oof_samples` and `purged_oof_required_samples`. FastAPI version is `1.0.54`; bot/global calibration identities are v16. Outcome contract remains `grid_label_v26`, direction calibration remains v12, and historical outcomes are retained because the label target did not change. DB schema, routes, model identity and env are unchanged.

## Horizon-boundary and liquidation volume integrity (v1.0.53)

`grid_label_v26` closes a HIGH historical-proxy liquidity defect. The exact horizon open belongs to a new one-minute candle, but older code reused the previous candle's remaining volume budget for gap-crossed grid orders. Residual inventory at the label horizon and at a kill-switch was then liquidation-equivalent at full size without consuming observed volume. A model could therefore buy, sell or close more quantity than the relevant minute traded.

The ledger now resets its path-local volume budget at the exact horizon candle, makes horizon gap fills and terminal residual liquidation share that candle's completed volume, and makes kill-switch residual liquidation share the breach candle's remaining volume. Insufficient capacity makes the outcome unavailable rather than profitable or losing. Because the boundary candle volume is known only after that minute closes, `label_available_ts` is now `horizon_end_ts + 60` while `horizon_sec` remains unchanged.

This is historical-only proxy validation, not runtime execution checking. FastAPI version is `1.0.53`; outcome contract is `grid_label_v26`; bot/global calibrators are v15 and direction calibration is v12. Model identity, schema, routes and env are unchanged. Existing proxy outcomes/calibrators are reset because prior labels could contain impossible boundary or liquidation fills.

## Conservative kill-switch fill bound (v1.0.52)

`grid_label_v25` removes another optimistic OHLCV assumption. When an observed intrabar move crosses a kill-switch, the proxy ledger still processes resting grid orders only up to the protective boundary, but it no longer assumes that the residual market-close executed perfectly at the trigger price. If continued movement is adverse to the remaining inventory, the liquidation price uses the adverse observed candle extreme: upper `high` for residual short inventory and lower `low` for residual long inventory. For inventory helped by the continuation, the boundary remains the conservative price. Close-to-open and horizon gaps that skip the boundary remain outcome-unavailable.

This changes only historical proxy labeling; the service still does not submit orders or claim runtime execution truth. FastAPI version is `1.0.52`; outcome contract is `grid_label_v25`; bot/global calibrators are v14 and direction calibration is v11. Model identity remains `bybit-taxonomy-v6-historical-proxy-shadow-roots`. Existing proxy outcomes/calibrators are reset because prior labels systematically understated some kill-switch tail losses. DB schema, routes and env remain unchanged.

## Historical-only simulation boundary (v1.0.51)

Bybit Recommender is a historical recommendation/audit simulator, not an order-placement or runtime execution-validation system. Recommendation publication and proxy-outcome labeling no longer depend on current Bybit `tickSize`, `qtyStep`, `minOrderQty`, `minNotional`, instrument status, or `params.exchange_execution_snapshot`. Missing current instrument metadata does not change a recommendation to `blocked` and does not suppress a matured historical outcome.

Every persisted recommendation now declares `reasons.simulation_scope.mode=historical_proxy_only`, `runtime_order_submission=false`, `runtime_execution_validation=not_performed`, and `exchange_fill_attestation=not_available`. Conservative OHLCV assumptions remain in force: strict trade-through, aggregate candle-volume capacity, next-candle replacement activation, costs, adverse funding, temporal independence, and uncertainty-bounded monetary gates. Explicit Bybit snapping/preflight helpers remain separate optional operator diagnostics and never mutate publication or calibration evidence.

FastAPI version is `1.0.51`; model identity is `bybit-taxonomy-v6-historical-proxy-shadow-roots`; outcome contract is `grid_label_v24`; bot/global calibrators are v13 and direction calibration is v10. Existing proxy outcomes/calibrators are reset because v1.0.48-v1.0.50 labels were coupled to current exchange metadata. DB schema, routes and env remain unchanged.

## Intrabar replacement-order timing (v1.0.50)

`grid_label_v23` no longer assumes that a replacement grid order is available immediately after its parent fill inside the same one-minute candle. OHLCV reveals candle extremes but not the parent fill timestamp, bot reaction latency, or the moment the replacement reached the exchange queue. If a later segment of the same candle would cross a newly created replacement, the outcome is unavailable with `intrabar_replacement_fill_timing_unobservable`. The replacement becomes eligible at the next candle boundary.

This removes a systematic optimistic path in which one candle could manufacture a completed cycle and positive `ret` under an unobservable zero-latency assumption. FastAPI version is `1.0.50`; bot/global calibrators are v12 and direction calibration is v9. Existing proxy outcomes/calibrators are reset; DB schema, routes, model identity and env are unchanged.

## Aggregate candle-volume capacity for proxy fills (v1.0.49)

`grid_label_v22` adds a necessary physical-capacity check to OHLCV proxy execution. A strict trade-through proves only that the market traded beyond a resting limit; it does not prove that an order larger than the entire one-minute traded volume was fully filled. When persisted model geometry contains `qty_per_order`, every simulated initial directional position and resting-grid fill consumes that quantity against that candle's Bybit base-quantity `volume`. If one fill or the cumulative fills of the minute exceed total observed volume, the outcome is unavailable with `insufficient_candle_volume_for_full_fill` or `insufficient_candle_volume_for_initial_inventory`.

This is still conservative proxy evidence, not queue reconstruction: sufficient total volume is necessary but does not prove price-level liquidity, queue priority or absence of market impact. FastAPI version is `1.0.49`; outcome contract is `grid_label_v22`; bot/global calibrators are v11 and direction calibration is v8. Schema, routes, model identity and env are unchanged. Existing proxy outcomes/calibrators are reset because prior labels could contain mathematically impossible full fills.

## Exchange-normalized proxy execution evidence (v1.0.48, superseded by v1.0.51)

> Historical note: the mandatory current-metadata publication/outcome coupling described below was removed in v1.0.51.

In v1.0.48-v1.0.50 the project temporarily snapped recommendations against current public Bybit filters before publication and required an exchange snapshot for outcome labeling. That coupling was removed in v1.0.51 because the service models historical outcomes and does not establish runtime executability. The paragraph is retained only to explain why v5/v10/v7 evidence was reset.

`grid_label_v21` also requires strict side-aware trade-through for OHLCV proxy fills: a resting Buy is confirmed only after price trades below the limit, and a resting Sell only after price trades above it. Exact candle equality is not proof of queue execution. The app version is `1.0.48`; model/calibrator identities are v5/v10/v7. Schema, routes and env remain unchanged. Historical proxy outcomes/calibrators are reset because both the geometry and fill-label contracts changed.

## Funding receipt is not strategy alpha (v1.0.46)

A settled funding receipt is real account cashflow, but it is not treated as durable grid edge. Before v1.0.46 the OHLCV proxy added positive receipts to `reco_outcomes.ret`; a flat directional grid could therefore become `success=1`, raise monetary expectancy and unlock calibration solely because one historical funding settlement paid its inventory side.

`grid_label_v19` keeps adverse settled funding as a cost and excludes positive receipts from canonical proxy return. This is deliberately conservative: exact execution reports may still show signed funding in realised total PnL, but proxy calibration, win rate and publication readiness cannot be manufactured by temporary carry. Upgrade resets old proxy outcomes and every current calibrator, including direction calibration. FastAPI version: `1.0.46`; DB schema, routes and env are unchanged.

## Cross-symbol temporal-independence gate (v1.0.45)

Исправлена **HIGH model-validation fail-open ошибка**: до v1.0.45 bot/global calibration считала outcomes разных символов независимыми строками, даже если все они использовали один и тот же перекрывающийся 12-часовой рыночный интервал. Поэтому 80 коррелированных монет могли выполнить `CALIB_MIN_SAMPLES=80`, дать положительную row-level lower bound и включить fitted calibration после фактически одного временного эксперимента.

В v1.0.55 matured returns сначала объединяются в один cross-sectional decision cohort по одинаковому recommendation `ts`, после чего earliest-finish scheduling выбирает максимальный набор попарно неперекрывающихся интервалов `[ts, label_available_ts]`. Один cohort даёт одно временное наблюдение и один recency weight. Для штатного `CALIB_MIN_SAMPLES=80` требуется не менее 20 эффективных selected cohorts, а односторонняя 95% нижняя граница должна быть положительной как по строкам, так и по cohort means. Большое число коррелированных символов и транзитивная overlap-цепочка не создают ложные степени свободы и не замораживают count на единице.

FastAPI version: `1.0.45`. Bot/global calibrator identities обновлены до `logreg_futures_grid_v9` и `logreg_global_v9`, поэтому прежние v8 coefficients не используются под новым контрактом. Схема БД, routes, env и `grid_label_v18` не менялись. Это устраняет псевдорепликацию, но не доказывает live profitability; cross-cluster dependence и proxy-to-fill gap остаются предметом walk-forward/bootstrap validation.

## Terminal exact-PnL finalization gate (v1.0.44)

Исправлена **HIGH live-validation fail-open ошибка**. До v1.0.44 остановленный bot становился `validation_eligible` после любого одного `execution` event. Система суммировала realized gross PnL, funding и fee, но не проверяла, что полный Buy/Sell ledger передан и остаточная позиция равна нулю. Поэтому частично реализованная прибыль могла выглядеть как окончательный exact net PnL, а открытый inventory и его хвостовой убыток оставались вне stop-gate статистики.

Теперь execution summary отдельно рассчитывает `buy_qty`, `sell_qty`, `net_position_qty`, `position_flat`, `execution_ledger_complete` и `total_pnl_finalized`. В live-validation входят только stopped bots с ненулевым execution stream, полным side/qty ledger и нулевой terminal position в строгом числовом tolerance. Неполные события не удаляются: они остаются audit-visible, но получают `validation_eligible=false` и причины `residual_position`, `execution_ledger_incomplete`, `no_execution_events` или `bot_not_stopped`.

FastAPI version: `1.0.44`. Схема БД и API routes не менялись; новые summary fields additive. Proxy-calibration contract v8 и `grid_label_v18` остаются прежними. Исправление делает exact stop evidence terminally comparable, но не доказывает прибыльность стратегии.

## Uncertainty-bounded monetary evidence gate (v1.0.43)

Actionable `futures_grid` recommendations now require more than a positive observed proxy-return mean. The bot-specific retained cohort must reach the effective weighted sample floor and its one-sided 95% lower confidence bound for recency-weighted mean return must be strictly positive. `unknown`, `insufficient`, and `uncertain` evidence remains shadow `no_trade` with `PROXY_MONETARY_EXPECTANCY_UNPROVEN`; confirmed non-positive mean remains `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE`. Raw heuristic confidence is still recorded for audit and shadow labeling, but it cannot make an unproven strategy actionable.

The calibration payload now records weighted return standard deviation, Kish effective sample size, one-sided lower bound, and confidence level. Bot/global calibration identities are v8 so stale v7 coefficients cannot bypass the new contract. FastAPI version: `1.0.43`. No DB schema, public API field removal, env, or outcome-label-version change is required. This is a fail-closed evidence rule, not proof of future profitability.

## Stale positive calibrator fail-closed (v1.0.42)

Исправлена **HIGH model/risk fail-open ошибка**: после истечения часового cache interval положительный bot/global/direction calibrator продолжал использоваться бессрочно, если текущая 14-дневная outcome-выборка стала недостаточной или была очищена. Сохранённые коэффициенты оставались `fitted` и влияли на confidence, хотя поддерживающих строк в активном контуре данных уже не было.

Теперь stale positive/fitted calibration обязана быть воспроизведена из текущих retained outcomes. Если refit возвращает `insufficient`, активная модель деактивируется и это состояние сохраняется в `app_config`, поэтому restart не воскрешает старые коэффициенты. Устаревший `expectancy_status=negative` сохраняется консервативно как NO_TRADE veto до появления новой подтверждённой положительной выборки. Ключи bot/global обновлены до v7, direction key — до v6, чтобы upgrade немедленно выполнил current-evidence refit.

FastAPI version: `1.0.42`. Схема БД, API, env, model identity и `OUTCOME_LABEL_VERSION=grid_label_v18` не изменены. Исправление удаляет ложную статистическую уверенность, но не доказывает live profitability.

## Independent shadow-outcome roots (v1.0.41)

Исправлена **HIGH model-validation ошибка псевдорепликации**. Явные `no_trade`-кандидаты используются как `shadow_no_trade` counterfactual outcomes, но до v1.0.41 каждый recommender cycle создавал новый `is_outcome_label_root=1` даже внутри уже открытого 12-часового horizon. Поэтому 80 минутных циклов могли дать 80 почти одинаковых перекрывающихся labels по одному ценовому пути и ошибочно выполнить `CALIB_MIN_SAMPLES=80`.

Теперь для одного `(venue, symbol, bot_type, direction, model_version)` в пределах label horizon существует только один независимый shadow root. Последующие `no_trade` audit-строки сохраняются, но получают `is_outcome_label_root=false` и ссылку на исходный root. После завершения horizon или появления outcome разрешается новый root. Model identity обновлена до `bybit-taxonomy-v4-independent-shadow-roots`, confidence calibrators — до v6, direction calibrator — до v5; старые перекрывающиеся v3 rows не участвуют в новом fit.

FastAPI version: `1.0.41`. Схема БД, API, env и `OUTCOME_LABEL_VERSION=grid_label_v18` не изменены. Калибровка начнёт накапливать новую независимую выборку и временно может оставаться unfitted/raw. Исправление устраняет ложное количество наблюдений, но не доказывает live profitability.

## Monetary-expectancy calibration gate (v1.0.40)

Исправлена **HIGH model/risk fail-open ошибка**: confidence-calibration обучалась на бинарном `success`, хотя monetary proxy return уже сохранялся в `reco_outcomes.ret`, но не участвовал в eligibility gate. Поэтому cohort из множества малых выигрышей и меньшего числа крупных убытков мог получить высокую calibrated `P(success)` при отрицательном совокупном денежном ожидании.

Теперь bot-specific calibration v5 использует только matured outcomes с finite `ret`, рассчитывает recency-weighted mean return и lower-tail expected shortfall. При достаточной выборке и `weighted_mean_return <= 0` модель не обучается, состояние `expectancy_status=negative` сохраняется, а новые рекомендации этого `bot_type` получают явный `no_trade` с кодом `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE`. Бинарный win rate больше не может сделать отрицательный monetary cohort actionable.

FastAPI version: `1.0.40`. Ключи calibrator обновлены с v4 до v5; схема БД, API, env и `OUTCOME_LABEL_VERSION=grid_label_v18` не менялись. Это защитный proxy gate, а не доказательство live profitability: OHLCV outcome не воспроизводит queue priority, exact fills, partial fills и account-level execution truth.

## Tail-loss exact-evidence stop gate (v1.0.39)

Исправлена **HIGH/P0 fail-open ошибка** операционного stop gate. До v1.0.39 отрицательный cumulative exact net PnL блокировал новый `executed` только если одновременно были отрицательны median PnL и доля прибыльных ботов была ниже 50%. Для arithmetic grid это пропускало характерный tail-loss профиль: много небольших прибыльных циклов и один крупный выход из диапазона могли оставить median и win rate положительными, хотя независимый cohort уже был убыточен в сумме.

Теперь после прежнего минимального числа независимых stopped bots отрицательный cumulative `realized_pnl_net` сам по себе блокирует соответствующий direction (8), symbol (12) или portfolio (20). Пятиботовый consecutive-loss gate сохранён. Median и positive rate остаются диагностикой, но не могут отменить накопленный убыток. Gate использует только terminally finalized exact execution evidence: stopped bot, complete signed Buy/Sell ledger и нулевая остаточная позиция; затем дедуплицирует `publication_root_rec_id` и изолирует explicit `model_version`.

FastAPI version: `1.0.39`. Схема БД, API, env и `OUTCOME_LABEL_VERSION=grid_label_v18` не менялись. Исправление предотвращает продолжение уже подтверждённо убыточного режима, но не доказывает прибыльность оставшихся режимов.

## Outcome dependency diagnostics (v1.0.38)

Исправлена MEDIUM-ошибка диагностики outcome worker. В v1.0.37 отсутствие ещё не загруженной settled funding row возвращало тот же `None`, что и действительно повреждённая grid-геометрия, поэтому журнал ошибочно показывал `OUTCOME_SKIP_INVALID_GRID_CONTRACT`. Теперь transient-зависимость записывается как `OUTCOME_WAIT_FUNDING_SETTLEMENT` с точным funding timestamp и текущим inventory; worker автоматически повторит расчёт после backfill. Настоящие конфликты funding/grid contract содержат машинно-читаемый `reason` и подробности. Повтор одинакового сообщения ограничен cooldown, чтобы decision log не заполнялся каждую минуту.

FastAPI version: `1.0.38`. Outcome math не менялась, поэтому `OUTCOME_LABEL_VERSION` остаётся `grid_label_v18`: обновление с v1.0.37 не удаляет уже рассчитанные v18 outcomes или calibrators.

## Settled funding outcome integrity (v1.0.37)

Исправлена HIGH-ошибка исторической статистики: `fundingRate` из ticker является изменяющимся прогнозом следующего funding settlement, но прежний outcome worker использовал его задним числом как фактическую ставку и учитывал только неблагоприятные списания. Это систематически искажало Total P&L, win rate и calibration: SHORT не получал положительный funding, LONG не получал отрицательный funding, а позднее изменившаяся ставка не отражалась в label.

Теперь collector backfill-ит immutable settlement rows из публичного `/v5/market/funding/history` в таблицу `funding_settlement`. Исторический outcome использует только фактически рассчитанную signed rate и реальный inventory на timestamp события. В legacy `grid_label_v18` платежи уменьшали proxy P&L, а получения увеличивали его. Начиная с v1.0.46 / `grid_label_v19` положительные receipts исключаются из canonical proxy edge, хотя сохраняются как signed account-cashflow diagnostics; adverse payments по-прежнему уменьшают `ret`. Если schedule указывает funding event, позиция была ненулевой, но settlement row отсутствует, label не создаётся fail-closed. Forecast funding остаётся только approval/risk input.

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

Сервис собирает рыночные данные Bybit Linear USDT Futures / USDT Perpetual и рассчитывает multi-timeframe признаки для двух исследуемых семейств: исполнимого `futures_grid` и неисполняемого shadow-only `directional_trend`. Только `futures_grid` может пройти operator execution lifecycle; trend-ветка сохраняет proxy outcomes для отдельной проверки. Дополнительно может подключаться локальный LLM-reviewer по свечам; полный журнал решений и состояний хранится в выбранном backend: SQLite или PostgreSQL.

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

`GET /api/v1/outcomes/stats` требует явного evidence scope: по умолчанию возвращается проверенная `current_policy`, дополнительно доступны `current_model` и исторический `archive`. Внутри каждого scope сохраняются отдельные `cohorts.actionable` и `cohorts.shadow_no_trade`; архив никогда не входит в текущий headline.

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

Если данных недостаточно, публикуется `MEAN_REVERSION_EVIDENCE_INSUFFICIENT`. Если агрегированный `mean_reversion_score` ниже `MEAN_REVERSION_MIN_SCORE` (default `0.25`), публикуется `MEAN_REVERSION_EDGE_UNCONFIRMED`. Низкий trend score сам по себе не является grid edge. Порог является candidate screen, а не доказательством прибыльности или убытка: microstructure bounce, regime shift и execution costs проверяются отдельной walk-forward/shadow статистикой.

Модель имеет новую audit identity `bybit-taxonomy-v3-mean-reversion`. Старые calibration coefficients и outcome features с семантикой `range = 1 - trend` не смешиваются с новой выборкой. Поле `expected_rr` сохранено только для API/исторической совместимости и внутренней диагностики. Основной UI вместо него показывает сценарный **Plan RR** конкретного плана и **Empirical expectancy** по exact-current-policy matured outcomes с доверительным интервалом.

## Фактическое исполнение и realised PnL

Проект по-прежнему не выставляет ордера. Внешний read-only execution/reconciliation adapter может передавать в защищённый endpoint `/api/v1/bots/{bot_id}/execution-evidence` два типа immutable events:

- `bybit_execution`: отдельный fill с `execId`, `orderId`, side, qty, `execPrice`, `orderPrice`, gross `execPnl` и signed fee; несколько fills одного order сохраняются отдельными строками;
- `bybit_transaction_log`: отдельный signed funding cashflow с уникальным transaction id.

Каждое событие напрямую связано с исходным `rec_id`. Для execution event дополнительно требуется timestamped benchmark (`pre_submit_mid`, `pre_submit_opposite` или `decision_reference`), относительно которого рассчитывается adverse fill deviation. Этот показатель является диагностикой исполнения. Поскольку gross PnL уже рассчитан по фактическим fill prices, канонический realised net PnL равен `gross_pnl + funding - fee`; slippage повторно не вычитается.

Точный evidence-ledger и legacy `/trades` нельзя смешивать для одного `bot_id`. Risk/drawdown/cooldown используют единый поток с приоритетом exact evidence, а endpoints чтения evidence защищены `ADMIN_API_KEY`. `/api/v1/validation/live-evidence` формирует только descriptive dataset и не доказывает live edge.

Execution preflight использует этот exact-evidence контур как **операционный stop gate**. Новое подтверждение `executed` блокируется для конкретного `(symbol, direction)` после пяти последовательных независимых убыточных остановленных ботов либо после восьми независимых наблюдений с отрицательным cumulative exact net PnL. Более широкие stop-условия применяются после 12 наблюдений по символу и 20 по всему `futures_grid`-контуру; median и доля прибыльных запусков остаются диагностикой, но не отменяют агрегированный убыток. Повторные публикации одного `publication_root_rec_id` не увеличивают выборку; при заданном `model_version` учитывается только evidence той же версии модели, чтобы старая стратегия не блокировала явно новую. Это консервативная защита от продолжения доказанно убыточного режима, но не статистическое доказательство alpha.

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
- для operator execution поддерживается только `futures_grid` на Bybit Linear USDT Perpetual (`account_mode=unified`, `margin_mode=cross`, `grid_type=arithmetic`); `directional_trend` существует отдельно как shadow-only single-position research contract;
- shipped risk profile: 1 running bot на счёт, daily DD 10 USDT, cooldown 90 min, max position notional 500 USDT, max margin per bot 100 USDT и интервал `min_leverage=3`, `max_leverage=5`; эти же значения встроены в код и действуют даже без скопированного `.env`;
- если оператор задаёт `max_leverage < 5`, это трактуется как более строгий risk cap внутри или ниже диапазона 3-5x, а не как обещание, что каждая идея станет исполнимой;
- любой `critical`/`blocking` preflight, `INVALID_MARKET_REFERENCE_PRICE`, устаревшая publication-chain, цена вне range/kill-switch, отсутствующая валидная пара bid/ask, live spread > 14 bps, пересчитанный net edge < 2 bps, неподтверждённый funding/minNotional/qtyStep или отсутствие OK LLM-gate при включённом reviewer означает `NO TRADE`.

## Как читать ключевые поля
- `status` — итоговый допуск идеи к рассмотрению.
- `direction` — исполнимое направление для текущего bot_type.
- `confidence` — в режиме `raw` это ограниченная эвристическая функция launch-score, а не вероятность прибыльной сделки; вероятностная интерпретация допустима только при активном bot-specific calibrator и всё равно относится к proxy-outcome, не к биржевому PnL. Не читать изолированно от Plan RR, empirical expectancy, `reasons.confidence_model` и risk context.
- `reasons.operator_metrics.plan_rr` — сценарное отношение projected net reward конкретного grid-плана к worst-side kill-switch loss. Это не вероятность и не историческая статистика.
- `reasons.operator_metrics.empirical_expectancy` — средняя доходность matured outcomes exact current policy, Student-t CI, expected shortfall и mean/tail ratio. Это proxy evidence, не live PnL.
- `expected_rr` — legacy heuristic capture score. Сохранён для совместимости, скрыт из основного операторского интерфейса и не должен использоваться как reward:risk.
- `score` / `reasons.score_components.economic_cost_bps` — ранжирование также штрафует adverse funding carry; signed funding receipt не превращается в положительный edge и не снижает cost-feature.
- `risk_score` — грубая оценка рыночной/исполнительной сложности.
- `reasons.direction_agg` — агрегированное направление и структура голосов по ТФ.
- `reasons.execution_constraints` — что можно, а что нельзя исполнить на выбранном bot_type.
- `bybit_meta` — metadata инструмента Bybit, доступная UI для операторской сверки диапазона, leverage и шагов.
- `params.grid_type/grid_count` — Bybit Futures Grid Bot geometry: `grid_count` означает число price intervals (“Number of Grids”), а текущая генерация и execution-preflight допускают только `grid_type=arithmetic`; `geometric` блокируется до реализации отдельной геометрической математики. Для arithmetic grid опубликованный `params.grid_spacing_pct` теперь соответствует исполнимой геометрии `(price_range_upper - price_range_lower) / grid_count`; минимальный экономический пол хранится отдельно как `economic_min_grid_spacing_pct`, а `grid_geometry_model` явно фиксирует `bybit_arithmetic_range_width_div_grid_count`. `params.economics` / `reasons.grid_economics` — net-of-fees экономика одной сетки: gross/net bps, estimated execution cost, signed funding impact, funding cost used for approval, excluded funding benefit, estimated order notional, margin required и worst-boundary liquidation buffer. Получение funding не улучшает canonical approval-edge, score, legacy heuristic capture score, Plan RR или outcome labels: оно показывается отдельно как signed diagnostic, потому что funding может измениться или стать расходом при накоплении inventory. Минимальный шаг и плотность grid строятся от recurring комиссий двух grid fills. Spread/slippage относятся к разовой market friction, а adverse funding — к position-time Total P&L и отдельным launch/risk gates; funding receipt не уменьшает spacing, не увеличивает score/RR и не используется как «бесплатный edge». Если net profit per grid не положителен или слишком тонкий, рекомендация блокируется. `reasons.funding.funding_interval_source` показывает, был ли funding interval получен из Bybit ticker/instrument metadata; если `next_funding_ts` недоступен, recommendation и execution-preflight консервативно считают возможные funding events по горизонту, а не предполагают нулевой или single-event carry. Public collector при отсутствии `fundingIntervalHour` в ticker дополнительно берёт interval из instruments-info, а при материальном funding и неизвестном interval рекомендация блокируется fail-closed.
- Временные поля market-data/funding/OI, label horizon и число funding events имеют exact-integer семантику. Значения `5` и `5.0` допустимы как точное целое; boolean, дробные и non-finite значения не усекаются и не округляются. Malformed funding schedule остаётся unknown: при материальном carry рекомендация/execute-preflight блокируется либо используется документированный консервативный unknown-schedule count, но не оптимистический single-event fallback.
- `bybit_plan_validation` — результат execution-time валидации trade plan: ошибки блокируют подтверждение, предупреждения напоминают о неполной проверке qty/min_notional без фактического размера позиции; если `trade_plan.sizing` или `params` уже содержит явный `order_qty`/`qty_per_leg`/`base_qty` либо `order_notional`, эти значения проверяются против Bybit `qty_step`, `min_order_qty`, `max_order_qty` и `min_notional`; для base-qty minNotional проверяется по минимальной цене основного grid range, а не только по reference price, и payload блокируется при существенном расхождении `qty * reference_price` с заявленным `order_notional`. Дополнительно `directional_trend` блокируется отдельным кодом `DIRECTIONAL_TREND_SHADOW_ONLY`, неизвестные `bot_type` блокируются как unsupported, а также блокируются рекомендации с любым `venue` кроме `linear`, `reference_price` вне диапазона, внутренним `kill_switch`, схлопыванием сетки после округления по `tick_size`, отсутствующим или неподдерживаемым `margin_mode`, metadata Bybit от другого `symbol` или другого `category/venue`, instrument `status` отличным от `Trading`, несогласованным `grid_count`/`grid_step`, `grid_count > 400`, неподдержанным `grid_type`, off-tick ценами/шагом/`tp_per_leg` в строгом execution-mode, некорректным `leverage` относительно `min/max/leverage_step` Bybit, отсутствующими обязательными Bybit filters (`tickSize`, `qtyStep`, `min/max qty`, `minNotionalValue`, `leverageFilter`), delivery-контрактом вместо perpetual, а также слишком малым worst-side/worst-boundary estimated liquidation buffer при leverage > 1. Legacy/manual payload без `leverage` получает предупреждение и preflight рассматривает его только как 1x; новые рекомендации обязаны хранить явное leverage. Execute-path дополнительно блокирует подтверждение, если текущий ticker уже вышел за сохранённый диапазон сетки или `kill_switch`, либо если свежий ticker не содержит пригодной `last`/`bid`/`ask` live price (`LIVE_PRICE_UNAVAILABLE`), даже при свежих candles/ticker. Для полноценных costed-рекомендаций с `cost_model` валидная пара best bid/ask обязательна: execute-preflight пересчитывает текущий spread/slippage, сохраняет консервативный fee floor и блокирует `LIVE_SPREAD_UNAVAILABLE`, spread > 14 bps, live grid edge < 2 bps или gross interval без запаса 1,10x над recurring grid fees; spread остаётся отдельным liquidity cap, а funding проверяется отдельным inventory/schedule guard. Отдельно повторно проверяются свежий `funding_rate`, `funding_interval_min`; запуск блокируется, если funding стал stale/недоступен, экстремален или ухудшился настолько, что net edge сетки становится неположительным (`FUNDING_RATE_UNAVAILABLE_AT_EXECUTION`, `STALE_FUNDING_RATE`, `FUNDING_EXTREME_AT_EXECUTION`, `FUNDING_EDGE_TURNED_NEGATIVE`). Metadata инструмента теперь берётся только при точном совпадении `symbol` и сохраняет `result.category`, чтобы preflight не валидировал payload ограничениями чужого или нецелевого инструмента. Auto-snap для сгенерированных operator payload расширяет range/kill-switch наружу по `tick_size` и округляет `grid_step`/`tp_per_leg` вверх, чтобы UI/preflight не показывали более узкую и более прибыльную сетку, чем допускает exchange-aligned geometry. Quantity является отдельной risk-boundary: provisional target-notional не округляется вверх по фиктивному step, а при live metadata qty может только округляться вниз; если после этого не выполнены minQty/minNotional, recommendation блокируется вместо автоматического увеличения позиции. Public Bybit client дополнительно блокирует не-`linear` category и non-USDT symbols до REST-запроса, а ticker collector отбрасывает non-perpetual/pre-market rows и не переименовывает чужой `symbol` в запрошенный.
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
- `RECO_TTL_SEC` — операторская свежесть publication-chain (по умолчанию auto: `max(900, RECO_INTERVAL_SEC × 15)`). После TTL прежнюю рекомендацию нельзя исполнять; это не закрывает её статистическое outcome-window;
- `RECO_REPUBLISH_COOLDOWN_SEC` — cooldown для near-identical updates внутри ещё свежей operator publication-chain. После operator TTL новый подтверждённый сигнал может начать свежий `publication_root_rec_id`, но до созревания прежней псевдо-позиции наследует её `outcome_root_rec_id` и не становится новым label-root;
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
2. если same-direction сигнал пришёл, пока текущая operator publication-chain ещё свежа, запись сохраняется как `active`, наследует её `publication_root_rec_id` и тот же `outcome_root_rec_id`;
3. если operator TTL уже истёк, но исходная псевдо-позиция ещё внутри outcome-horizon, новый подтверждённый сигнал публикуется как свежий `recommended` со своим `publication_root_rec_id`, однако наследует прежний `outcome_root_rec_id` и получает `is_outcome_label_root=false`; оператор видит актуальную идею, а outcome/calibration не получают второй перекрывающийся sample;
4. только после созревания прежнего outcome-horizon либо наличия сохранённого outcome следующая идея может стать одновременно новым publication-root и новым outcome-root; republish-cooldown после закрытия псевдо-позиции по-прежнему подавляет нематериальные повторы;
5. если включён `LLM_REVIEWER_ENABLED=1`, actionable grid-рекомендация получает `pending` до `llm_review.status=ok`; без OK-вердикта `recommended/active` в UI/API не показываются как запускаемые, а hold ограничен `LLM_REVIEWER_PENDING_TIMEOUT_SEC`;
6. проигравшие альтернативы по тому же `(venue, symbol)` уходят в `suppressed` с явной причиной в `reasons.suppression`;
7. оператор вызывает `/recommendations/{rec_id}/action` с `executed` для `recommended` или `active`;
8. перед созданием `bot_instance` сервис повторно проверяет текущие риск-лимиты, свежесть candles/ticker, live-price относительно диапазона/kill-switch, текущий best bid/ask spread и net economics, актуальный market shock / fast-veto и базовую Bybit-валидность сетки; instrument metadata Bybit подгружается заранее, вне SQLite write-lock, чтобы медленный upstream не блокировал collector/recommender; при ошибке возвращается `409`, а в `decision_log` пишется `EXECUTION_BLOCKED` или `EXECUTION_PRECHECK_BLOCKED`;
9. если preflight пройден, создаётся `bot_instance`, recommendation переводится в `executed`;
10. realized trades/PnL пишутся через `/bots/{bot_id}/trades`;
11. risk engine использует `bot_instances` + `trades` для cooldown и дневного PnL / DD;
12. бот останавливается через `/bots/{bot_id}/stop` или `stop_bot=true` в trade request.

### Семантика статусов recommendation
- `recommended` — новый actionable сигнал, который прошёл подтверждение на двух разных закрытых evidence snapshots и готов к исполнению;
- `active` — повторно актуальный signal-update внутри ещё свежей operator publication-chain; наследует оба root и не создаёт отдельный outcome-root. После operator TTL новый подтверждённый сигнал становится свежим `recommended`, но может продолжать прежнее outcome-window;
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
