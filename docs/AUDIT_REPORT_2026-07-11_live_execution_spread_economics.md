# Аудит live execution spread/economics — v1.0.14

**Дата:** 2026-07-11  
**Исходная версия:** v1.0.13  
**Релиз:** v1.0.14  
**Scope:** Bybit V5 Linear USDT perpetual, `futures_grid`, arithmetic grid, unified account, isolated margin, one-way semantics  
**Граница системы:** recommendation/audit service с fail-closed operator preflight; не OMS/EMS и не источник реальных fills/PnL

## 1. Вход и безопасная подготовка

Проверен исходный архив `bybit-reco-systems-1.0.13-bybit-response-request-integer-integrity.zip`:

- SHA-256: `9da99eccc07697bac7b9dff7041aadca2c0645ae52f0f0d5d18d0ec7381902d5`;
- 215 ZIP entries;
- один корень `bybit-reco-systems-main/`;
- отсутствуют absolute/traversal paths, symlinks, duplicate names и nested archives;
- CRC: PASS.

Созданы независимые `pristine`, `red` и `working` копии. Production-код до фиксации baseline и RED не изменялся.

## 2. Baseline

Среда:

- Python `3.12.13`;
- Node.js `v24.14.0`;
- pinned isolated virtual environment;
- `pip check`: PASS.

Результаты исходной v1.0.13:

| Проверка | Результат |
|---|---:|
| Pytest collect | 833 tests |
| Full pytest | **833 passed** |
| Python compile | PASS |
| JavaScript syntax | PASS |
| Ruff | 9 ранее известных findings |

Ruff baseline: `E741` в `app/direction.py`, `F841` в `app/main.py`, шесть `E402` в `tests/test_iteration152_deep_trading_reaudit.py`, один `E402` в `tests/test_iteration160_frontend_tick_directional_rounding.py`, один `F401` в `tests/test_iteration173_operator_leverage_no_trade_policy.py`.

## 3. Проверенный торговый контур

Выполнена трассировка:

- publication-time fee/spread/slippage/funding model;
- grid gross/net economics и cost coverage;
- execution-time price drift, funding и sizing/risk guards;
- outcome-cost extraction;
- operator action → preflight → `bot_instance` materialization.

Bybit официально описывает `bid1Price` и `ask1Price` как best bid/ask в Linear/Inverse ticker response: [Get Tickers — Bybit V5 API](https://bybit-exchange.github.io/docs/v5/market/tickers). Следовательно, `lastPrice` пригоден как ценовой snapshot, но не доказывает текущий executable spread.

## 4. Подтверждённый дефект

### HIGH — execution preflight не обновлял transaction costs по текущему bid/ask

**Root cause:** `app/main.py::_execution_live_price_blocks()` обновлял текущую цену и проверял range/kill-switch/reference drift. При отсутствии валидного bid/ask helper использовал `last`, а текущий spread, slippage и net edge не пересчитывались. Funding обновлялся отдельным guard, но publication-time spread мог устареть без блокировки.

**Риск:** costed-рекомендация могла оставаться внутри диапазона и иметь свежий ticker, хотя widened spread уже делал per-grid edge неположительным или недостаточным. Репозиторий не выставляет ордера сам, поэтому дефект находился на safety-critical operator materialization boundary, а не в несуществующем live order router.

Независимые контрольные расчёты:

| Сценарий | Live cost oracle | Ожидаемый fail-closed результат до фикса |
|---|---:|---|
| `last=100`, bid/ask отсутствуют | spread неизвестен | `LIVE_SPREAD_UNAVAILABLE` |
| bid/ask `99.8 / 100.2` | spread `40`, slippage `14`, fee `12`, cost `66` bps; gross `40` → net `-26` bps | wide spread + non-positive edge |
| bid/ask `99.95 / 100.05` | spread `10`, slippage `3.5`, fee `12`, cost `25.5` bps; gross `27` → net `1.5` bps | edge below 2 bps |
| тот же spread, gross `28` | net `2.5` bps, но `28 / 25.5 = 1.098...` | gross/cost coverage below 1.10x |
| bid/ask `99.995 / 100.005`, gross `30` | healthy positive edge | no new live-cost block |

RED на исходном коде: **4 failed, 1 passed**. Контрольный здоровый сценарий проходил, а все четыре опасных сценария не блокировались.

## 5. Исправление

В execution preflight добавлена строго ограниченная revalidation для generated/costed `linear futures_grid`:

1. Требуется валидная пара `bid > 0`, `ask > 0`, `ask >= bid`; `last` больше не используется как spread proxy.
2. `live_spread_bps = (ask - bid) / midpoint * 10000`.
3. `live_slippage_bps = max(1.0, 0.35 * live_spread_bps)`, то есть тот же консервативный grid model, что при публикации.
4. Round-trip fee берётся как максимум stored fee floor и текущего configured `2 * TAKER_FEE_BPS_LINEAR`.
5. Консервативный residual исходной execution-cost model сохраняется; смена spread/slippage не может удалить неизвестный cost component.
6. Из gross edge вычитаются live execution cost и adverse funding cost.
7. Повторно применяются publication safety floors:
   - spread `<= 14` bps;
   - net edge `>= 2` bps;
   - gross edge `> 1.10 * live execution cost`.

Новые блок-коды:

- `LIVE_SPREAD_UNAVAILABLE`;
- `LIVE_SPREAD_TOO_WIDE`;
- `LIVE_EXECUTION_EDGE_NON_POSITIVE`;
- `LIVE_EXECUTION_EDGE_TOO_THIN`;
- `LIVE_GROSS_EDGE_BELOW_COSTS`.

Legacy/manual payloads без `cost_model` не были автоматически переинтерпретированы: для них сохранён документированный compatibility path и существующая strict trade-plan validation.

## 6. RED → GREEN и post-check

| Проверка | Результат |
|---|---:|
| Targeted RED на v1.0.13 | **4 failed, 1 passed** |
| Targeted GREEN на v1.0.14 | **5 passed** |
| Соседний price/funding/economics subset | **52 passed** |
| Full post-check | **838 passed** |
| API + SQLite + PostgreSQL-dialect subset | **67 passed** |
| OpenAPI smoke | version `1.0.14`, 24 routes, 18 paths |
| Clean SQLite init + repeated init | PASS, 15 application tables both times |
| `pip check` | PASS |
| Python compile | PASS |
| JavaScript syntax | PASS |
| Ruff | те же 9 baseline findings; новых нет |

Live PostgreSQL server integration не запускался: подтверждённый disposable DSN не предоставлен. PostgreSQL normalization, locking и dialect paths покрыты существующими mock/dialect тестами. Изменений SQL/schema нет.

## 7. API, DB, env и совместимость

- Public routes и JSON contracts не изменены.
- Версия FastAPI повышена `1.0.13 → 1.0.14`.
- Миграции не требуются.
- Env-переменные не добавлены и не переименованы.
- Frontend code/cache key не изменялись.
- Mutating execution endpoint может вернуть существующий `409` с новыми fail-closed block codes.
- Реальные private Bybit order endpoints не добавлялись.

## 8. Документация и operator assets

Синхронизированы:

- `README.md`;
- `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- `how_to_trade.png`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- `docs/instrukciya_operatora_bybit_recommender.pdf`.

DOCX перерендерен в PDF/PNG. Визуально проверены все 3 страницы: нет clipping, overlap, broken glyphs или обрезанных блоков. Встроенная infographic совпадает с корневым PNG.

## 9. Изменённые файлы

- `app/main.py`;
- `tests/test_iteration202_live_execution_cost_revalidation.py`;
- `README.md`;
- `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- `docs/instrukciya_operatora_bybit_recommender.pdf`;
- `how_to_trade.png`;
- этот отчёт.

## 10. Остаточные риски

1. Public best bid/ask — snapshot, не fill truth: он не моделирует depth, queue priority, latency, market impact и partial fills.
2. Configured fee floor не доказывает фактический authenticated account fee tier. Внешний executor обязан обновить fee/account/order preview перед реальным действием.
3. Между preflight и действием внешнего оператора/исполнителя рынок может измениться; нужен повторный atomic check максимально близко к order creation.
4. Funding обновляется существующим отдельным execution guard; live-spread revalidation сохраняет adverse funding cost, но не заменяет authenticated funding/position truth.
5. Proxy outcomes и calibration не являются реальными fills или гарантией доходности.
6. Legacy/manual payload без `cost_model` сохраняет compatibility path и требует особенно строгой внешней проверки.

Исправление устраняет подтверждённый false-negative, но не обещает прибыльность и не превращает рекомендатель в автономную торговую систему.

## 11. Rollback

Rollback schema/data не требуется. Для отката вернуть перечисленные файлы из проверенного архива v1.0.13 с SHA-256 `9da99e...1902d5` либо выполнить обычный `git revert` релизного коммита. БД и env остаются совместимыми.

Рекомендуемый commit message:

```text
fix(preflight): reprice live bid-ask costs before grid execution
```
