# Известные риски и ограничения

## 1. Нет реального OMS/EMS
Это главный системный риск. Проект не управляет live order lifecycle и не знает реальные open orders/fills.
Следствие: нельзя считать его завершённой автоторговой системой без внешнего execution layer.

## 2. Qty/min-notional validation зависит от фактического размера позиции
Сервис формирует minimum viable `params.sizing` с order notional/qty/margin estimate, округляя provisional qty вверх по conservative fallback step до получения live Bybit metadata, чтобы UI показывал капитал и preflight мог проверить явные значения. Execution-preflight дополнительно проверяет minNotional на нижней цене grid range и согласованность qty/notional, но это всё равно не заменяет live preview фактических ордеров в Bybit. Это не заменяет sizing от баланса аккаунта и не гарантирует совпадение с каждым `qtyStep`: внешний исполнитель обязан повторно сверять `qty_step`, `min_order_qty`, `max_order_qty`, `min_notional`, available balance и фактическую маржу перед созданием Bybit grid bot.

## 3. Outcome labeling остаётся proxy-моделью
Даже усиленная grid-разметка не заменяет реальные fill/funding/liquidation данные.
Использовать её как единственный источник истины для PnL/WR нельзя.

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
В этой ревизии проект исходит из `futures_grid + isolated` как из безопасного operational minimum. Leverage поддерживается только как Bybit Linear USDT Futures leverage с проверкой `leverageFilter` и conservative worst-boundary liquidation buffer. Точный liquidation price должен подтверждаться внешним execution/reconciliation контуром или Bybit calculator/API account data.

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

