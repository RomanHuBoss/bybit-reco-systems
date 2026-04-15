# Торговая логика и реальные ограничения

## Важная граница

Несмотря на терминологию `bot_instance`, проект не является реальным grid execution engine.
Он формирует рекомендации для запуска бота оператором и ведёт audit-контур вокруг этого решения.

## Поддерживаемые рекомендации
- `spot_grid`
- `futures_grid`

## Разрешённые направления
- `spot_grid`: `neutral`, `long`
- `futures_grid`: `neutral`, `long`, `short`

## Режимы, которые система считает поддержанными
- `spot_grid`: `venue=spot`, `account_mode=spot`, `margin_mode=cash`, `leverage=1`
- `futures_grid`: `venue=linear`, `account_mode=unified`, `margin_mode=isolated`

`account_mode=one_way` допускается только как legacy-алиас старых payload'ов и помечается warning'ом;
штатной моделью ревизии он не считается. Поддержка `cross`, `hedge mode`, order-routing и real fill reconciliation
в этой ревизии отсутствует. Если такие режимы или пустой `margin_mode` появятся в данных вручную,
execution-time validation должна блокировать исполнение, а не притворяться, что логика проекта их понимает.

## Как строится grid idea

1. Берётся reference price.
2. По ATR и stability/range context выбирается `grid_spacing_pct`.
3. По тому же контексту выбирается число уровней `grid_levels`.
4. Строится основной диапазон `price_range_lower/upper`.
5. Вокруг диапазона строится `kill_switch` через padding от старшего ATR.
6. Для UI и operator guidance формируется `trade_plan`.

## Что именно проверяется перед `executed`

### Рыночная свежесть
- есть свежие 1m candles;
- есть свежий ticker;
- symbol не отключён после upstream ошибок.

### Рыночные блокировки
- market shock state не запрещает новый вход;
- symbol fast-veto не активен;
- instrument metadata Bybit подгружается до захвата SQLite write-lock, чтобы operator execution не тормозил остальные writer-контуры на сетевой задержке upstream.

### Геометрия grid-плана
- `reference_price` внутри диапазона;
- kill-switch лежит вне основного диапазона;
- после округления по `tick_size` диапазон не схлопывается;
- шаг сетки не меньше `tick_size` и не больше диапазона;
- сетка содержит минимум 2 интервала после выравнивания.

### Режимные инварианты
- `bot_type` согласован с `venue` и `direction`;
- `account_mode` и `margin_mode` не противоречат модели проекта;
- для supported execution-path обязательно присутствует явный `margin_mode` (`cash`/`isolated`), иначе recommendation блокируется fail-closed;
- `leverage` > 0 и укладывается в `min/max leverage`;
- `leverage` выровнен по `leverage_step`, если биржа прислала такой constraint;
- metadata Bybit относится к тому же `symbol`, а не к соседнему инструменту/битому кэшу.

## Что outcome labeling умеет и чего не умеет

### Умеет
- учитывать grid spacing, cost floor, funding-carry;
- штрафовать break-out, kill-switch breach и плохую occupancy range;
- считать success по факту достижения per-leg TP либо по oscillation proxy.

### Не умеет
- реконструировать реальные fill sequence;
- учитывать queue priority и live slippage distribution;
- учитывать частичные исполнения на уровне отдельных ордеров;
- моделировать liquidation engine и real margin waterfall.

## Что должен делать внешний execution layer

Если проект используется в production-пайплайне, внешний контур обязан:
- проверять фактический qty, qty_step, min qty и min notional;
- выставлять/менять/отменять реальные ордера;
- хранить order/fill state machine;
- восстанавливать состояние после рестарта по фактическим биржевым данным;
- присылать в этот сервис агрегированные realised trade rows для аудита.


## Инвариант publication-chain
Для одной publication-chain допускается не более одного `running` bot_instance. Это не просто UI-правило: инвариант обеспечивается persistence-слоем, чтобы гонка двух операторских `execute` не создавала две параллельные позиции на один и тот же рекомендательный корень.
