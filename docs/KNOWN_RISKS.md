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
Сервис делает защитные retry/backoff и stale checks, но не получает execution truth.

## 6. LLM reviewer может быть полезен только как вторичный фильтр
LLM не должен принимать финальное торговое решение вместо scoring/risk/shock логики.

## 7. Cross margin / hedge mode / live liquidation modeling не поддержаны
В этой ревизии проект исходит из `futures_grid + isolated` как из безопасного operational minimum.

## 8. Telegram alerts best-effort
Оповещения не гарантируют доставку и не заменяют внешний мониторинг / process supervisor.
