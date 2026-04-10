# Известные риски и ограничения

## 1. Нет реального OMS/EMS
Это главный системный риск. Проект не управляет live order lifecycle и не знает реальные open orders/fills.
Следствие: нельзя считать его завершённой автоторговой системой без внешнего execution layer.

## 2. Qty/min-notional validation неполна без фактического размера позиции
Сервис знает ограничения инструмента Bybit, но не знает размер leg/ордера, если его не задаёт внешний исполнитель.
Поэтому `qty_step`, `min_order_qty`, `max_order_qty`, `min_notional` в этой ревизии проверяются только частично.

## 3. Outcome labeling остаётся proxy-моделью
Даже усиленная grid-разметка не заменяет реальные fill/funding/liquidation данные.
Использовать её как единственный источник истины для PnL/WR нельзя.

## 4. SQLite — практичный, но ограниченный backend
Для operator-grade single-node контура это допустимо. Для multi-node/multi-writer production
нужна более сильная persistence model.

## 5. Публичный Bybit REST не гарантирует полную временную согласованность
Сервис делает защитные retry/backoff, transport/decode retry и stale checks, но не получает execution truth.
Если metadata Bybit временно недоступна, проект в этой ревизии всё ещё деградирует к warning-path, а не к жёсткой блокировке исполнения.
Это осознанный компромисс ради operator workflow, но он остаётся источником остаточного риска.

## 6. LLM reviewer может быть полезен только как вторичный фильтр
LLM не должен принимать финальное торговое решение вместо scoring/risk/shock логики.

## 7. Cross margin / hedge mode / live liquidation modeling не поддержаны
В этой ревизии проект исходит из `futures_grid + isolated` как из безопасного operational minimum.

## 8. Telegram alerts best-effort
Оповещения не гарантируют доставку и не заменяют внешний мониторинг / process supervisor.

## 9. Raw publication history по-прежнему хранится полностью
UI/operator-list теперь по умолчанию схлопывает repeated rows одной publication-chain и адаптивно добирает raw-кандидаты,
если одна длинная chain доминирует в snapshot. Audit-след в БД при этом сознательно не удаляется.
Это правильно для расследований и калибровки, однако raw SQL-выгрузки без учёта `publication_root_rec_id`
всё ещё могут визуально выглядеть как поток похожих сигналов.

## 10. Legacy/manual payload compatibility остаётся частично семантической
Execution-time validation теперь fail-closed блокирует futures/spot recommendations без явного `margin_mode`,
а также рекомендации, для которых Bybit metadata относится к другому `symbol` или другой `category/venue`.
Это безопаснее, но означает, что старые вручную заведённые записи могут перестать быть исполнимыми без миграции payload'а.

`account_mode=one_way` сохраняется как legacy-совместимость старых тестовых/исторических rows, однако
это не полноценная модель account-mode текущей ревизии и не должно использоваться как основание для
расширения execution-логики на hedge/cross сценарии.
