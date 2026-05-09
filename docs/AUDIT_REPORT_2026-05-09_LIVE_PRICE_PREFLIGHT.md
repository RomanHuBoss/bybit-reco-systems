# Audit report — 2026-05-09 live-price preflight fail-closed

## Scope

Проверен execution-path рекомендательной системы Bybit Linear USDT Futures grid-only после базового аудита доменной логики, risk guards, API/UI contracts и regression tests.

## Finding

Существующий preflight проверял свежесть ticker-строки отдельно от пригодности live price. Если upstream/санитизация сохраняли свежий ticker с `last=NULL`, `bid=NULL`, `ask=NULL`, строка могла считаться свежей, но `_current_price_from_ticker()` возвращал `None`. В таком состоянии невозможно проверить:

- нахождение текущей цены внутри `trade_plan.levels.range`;
- нахождение текущей цены внутри `kill_switch`;
- drift от `reference_price`, на котором рассчитывались grid spacing, fees, funding и liquidation buffer.

Для futures grid это critical fail-open: оператор мог подтвердить устаревшую сетку без валидной live price.

## Fix

`_execution_live_price_blocks()` теперь fail-closed возвращает блокировку `LIVE_PRICE_UNAVAILABLE`, если ticker свежий, но не содержит пригодной `last`/`bid`/`ask` цены. Execution confirmation запрещён до получения валидной live price и нового preflight.

## Files changed

- `app/main.py` — добавлен hard block `LIVE_PRICE_UNAVAILABLE`.
- `tests/test_iteration114_live_price_and_status_guards.py` — добавлен regression test для fresh-but-unpriced ticker.
- `app/ui/static/app.js` — подсказка UI уточняет, что execution-preflight сверяет именно пригодную live `last`/`bid`/`ask` цену.
- `README.md` — описано новое execution-time ограничение.
- `docs/TRADING_LOGIC.md` — обновлена спецификация live ticker guard.
- `CHANGELOG.md` — добавлена запись изменения.

## Validation

- `python -m pytest tests/test_iteration114_live_price_and_status_guards.py -q`
- `python -m pytest -q`
- `python -m compileall -q app tests`

## Residual risk

Этот guard проверяет только наличие live price в локальной ticker-записи. Production запуск всё ещё требует мониторинга задержек Bybit, spread/slippage, реальных fee tiers, funding history и paper/live execution reconciliation.
