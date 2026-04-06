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
- leverage tier validation;
- min notional / qty step / tick size enforcement в execution path.

Эти ограничения должны реализовываться во внешнем OMS/EMS.
