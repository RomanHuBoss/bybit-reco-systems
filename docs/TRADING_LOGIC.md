# Торговая логика и ограничения

## 1. Поддерживаемые режимы
- `spot_grid`
- `futures_grid`

## 2. Ключевая оговорка
Система **не исполняет ордера на бирже**. Она публикует recommendation payload, trade plan и operator-sheet.
Любая дальнейшая торговля должна исполняться внешним оператором или отдельным execution-engine.

## 3. Direction semantics
- `spot_grid`: исполнимые направления только `neutral` и `long`.
- `futures_grid`: исполнимые направления `neutral`, `long`, `short`.
- bearish thesis на `spot_grid` переводится в `execution_direction=neutral`; raw bearish context сохраняется в `reasons.execution_constraints`.

## 4. Пайплайн публикации
1. collector подготавливает market-data;
2. recommender считает features, direction, regime, score, confidence, expected RR;
3. risk/shock/fast-veto формируют feasibility blocks;
4. persistence-gate требует подтверждение в 1–2 цикла;
5. publication dedupe подавляет почти идентичные same-direction повторы;
6. LLM-reviewer может удержать публикацию в `pending` до review.

## 5. Publication-chain
Каждая идея принадлежит publication-chain через `publication_root_rec_id`.
- первый actionable сигнал создаёт новый root;
- same-direction повтор внутри cooldown или живого pseudo-position может перейти в `active` и остаться в той же chain;
- outcome-labeling и calibration считают только root-публикации.

## 6. Risk logic
На recommendation-time и execution-time учитываются:
- `max_concurrent_bots`;
- `max_symbol_bots`;
- `max_daily_dd_usdt`;
- `cooldown_after_loss_min`.

Execution-time recheck обязателен: recommendation snapshot не считается гарантией, что лимиты всё ещё свободны к моменту operator action.

Дополнительно перед подтверждением `executed` система проверяет:
- наличие и свежесть `1m` candles и ticker по символу;
- активный `market shock guard`;
- текущий `symbol fast-veto`;
- базовую исполнимость диапазона/шага сетки относительно `tick_size`, `min_price`, `max_price`, `max_leverage` и spot/futures semantics.

Если один из этих блоков срабатывает, recommendation не переводится в `executed`, а оператор получает `409 execution blocked by preflight checks`.

## 7. Cost model
Cost model учитывает:
- spread;
- round-trip taker fee;
- slippage proxy;
- direction-aware expected funding carry для `linear`.

Важно:
- `total_cost_bps` — execution friction floor;
- `net_cost_bps` — execution cost + expected funding carry;
- RR и часть funding gates используют `net_cost_bps`.

## 8. Что не покрыто торговой логикой
Внутри этого проекта отсутствует реальное управление:
- order placement/cancel/replace;
- partial fill state machine;
- reduce-only guarantees;
- hedge/one-way reconciliation;
- isolated/cross switching;
- private WS/REST reconciliation с фактическими ордерами и позициями.

Execution-time preflight теперь частично валидирует recommendation against Bybit metadata, но ограничения остаются:
- `qty_step`, `min_order_qty` и `min_notional` нельзя проверить до конца без фактического размера заявки/капитала на leg;
- нет проверки реального leverage tier по текущей позиции и margin usage;
- нет биржевой гарантии, что оператор создаст бота на Bybit без ручных отклонений от recommendation payload.

Эти ограничения должны реализовываться во внешнем OMS/EMS.
