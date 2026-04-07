# Known risks / remaining limitations

## Critical architectural limitation
Проект не является реальным execution-engine. Любое использование как fully automated trading bot без внешнего OMS/EMS создаёт риск рассинхронизации с биржей и неверного контроля позиции.

## Remaining risks
1. **Нет private Bybit WS/REST reconciliation**
   - нет подтверждения реальных fills, rejects, cancels, position state.
2. **Нет реального order state machine**
   - partial fills, duplicate intents, replace/cancel races на бирже здесь не обрабатываются, потому что проект не отправляет ордера.
3. **Outcome model приближённый**
   - `reco_outcomes` — path approximation по свечам, а не биржевая truth-модель исполнения.
4. **SQLite как single-node storage**
   - хорошо подходит для local/operator contour, но не для горизонтально масштабируемого execution service.
5. **Публичный market-data контур**
   - при нестабильности Bybit public API рекомендации могут деградировать в `stale`/`blocked` сценарии.
6. **Instrument constraints неполны**
   - execute-path теперь валидирует `tick_size`, `min_price`, `max_price`, `max_leverage` и обнаруживает схлопывание шага сетки после округления;
   - однако `qty_step`, `min_order_qty`, `min_notional` и фактическая допустимость позиции по капиталу всё ещё нельзя проверить без внешнего sizing/execution-layer.

## Практический вывод
Проект можно использовать как:
- аналитический и операторский recommendation service;
- staging-контур оценки grid-идей;
- audit/research engine для последующего подключения к отдельному execution-layer.

Проект нельзя считать самодостаточным production auto-trading engine без дополнительного сервиса исполнения.
