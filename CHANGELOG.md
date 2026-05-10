# Changelog

## 2026-05-10 — TP outcome and generated grid geometry preflight hardening

- Outcome-labeling no longer marks a per-leg TP touch as success when the TP distance is smaller than execution costs; `tp_success` now requires net-positive edge after the cost floor.
- Strict execution preflight now treats generated payloads with `grid_geometry_model=bybit_arithmetic_range_width_div_grid_count` as blocking-invalid when `range / grid_step` implies a different interval count than `grid_count`.
- Legacy/manual payloads without generated geometry markers keep a warning instead of a hard error, preserving old audit rows while keeping new generated recommendations fail-closed.
- Added regression coverage in `tests/test_iteration137_grid_outcome_and_preflight_hardening.py`; validation: `447 passed`, `python -m compileall -q app tests`.

## 2026-05-10 — Collector funding interval fallback hardening

- Public collector now fills missing ticker `fundingIntervalHour` from Bybit `/v5/market/instruments-info` `fundingInterval` for the exact same Linear USDT perpetual symbol.
- The fallback is accepted only when instrument metadata proves `contractType=LinearPerpetual`, `quoteCoin=USDT`, `settleCoin=USDT`, `status=Trading` and no delivery/pre-listing state.
- This prevents recommendation/execution funding event counts from degrading to missing/implicit interval assumptions when ticker payloads omit the interval.
- Added regression coverage in `tests/test_iteration136_collector_funding_interval_fallback.py`; validation: `444 passed`, `python -m py_compile app/*.py tests/*.py main.py`, `node --check app/ui/static/app.js`.

## 2026-05-10 — Execution funding event count and outcome funding hardening

- Execution-time funding preflight now counts missing `next_funding_ts` conservatively as `ceil(horizon / interval)` instead of assuming at most one event; this can block grids whose current funding carry turns net edge negative before launch.
- Outcome labeling no longer credits negative signed funding as durable edge: calibration labels subtract adverse funding cost only and do not boost success/return from funding receipt.
- Added regression tests for unknown next funding timestamp at execution and no funding-receipt boost in outcome labels; full suite: `430 passed`.

## 2026-05-10 — Strategy grid geometry and funding schedule hardening

- Fixed arithmetic futures-grid geometry: published `grid_spacing_pct` now matches the executable Bybit arithmetic step `(upper - lower) / grid_count`, while the old minimum spacing floor is kept separately as `economic_min_grid_spacing_pct`.
- Grid economics, `trade_plan.levels.grid_step` and TP hints now use the executable range-derived step instead of an understated minimum floor when ATR/range padding expands the range.
- Funding approval economics now fail more conservatively when `funding_rate` and interval are known but `next_funding_ts` is missing: the model assumes possible funding events across the recommendation horizon instead of assuming zero carry.
- Added `funding_event_schedule_assumption` and `grid_geometry_model` diagnostics to make these assumptions visible in audit/UI payloads.
- Added a stronger weak-range veto `RANGE_EDGE_TOO_WEAK_FOR_GRID` so grid is blocked before execution when trendiness is already too high for a credible range setup.
- Updated tests for executable arithmetic step consistency and conservative funding-event counting; full suite: `428 passed`.

## 2026-05-10 — UI score near-tie segmentation

## 2026-05-10 — Non-actionable launch link guard

- UI больше не показывает ссылку на создание Bybit Futures Grid для `blocked`, `no_trade` или `pending` рекомендаций; create-link доступен только для `futures_grid`/`linear` со статусом `recommended`/`active`, `risk_report.decision=recommended` и без ошибок Bybit validation.
- Удалён product-url helper, зависящий от произвольного `bot_type`; UI использует фиксированный Futures Grid create URL.
- Operator card labels упрощены под единственный поддерживаемый Bybit Linear USDT Futures Grid product.
- Добавлены регрессии `tests/test_iteration130_non_actionable_launch_links.py`; полный suite: `425 passed`.



## 2026-05-10 — UI single-product simplification

- Operator UI now uses concise `Futures Grid` product wording instead of repeating the full `Bybit Linear USDT Futures Grid` label.
- Removed redundant single-value `Площадка` selector and main-table `Тип бота` column; frontend still sends `venue=linear` to preserve backend fail-closed scope.
- Removed redundant product/venue dimensions from details, health and outcomes subwindows.
- Added regression coverage in `tests/test_iteration129_ui_single_product_simplification.py` and updated the detail badge UI regression.

- UI `Скор UI` no longer converts tiny raw-score differences into hard 100/50/0 percentile splits when the visible candidate set is small.
- Added near-tie grouping with a material raw-score delta of 0.025; candidates inside one group receive the same averaged UI percentile/grade.
- Sorting by `Скор UI` now uses the grouped UI percentile instead of raw score, reducing false visual precision.
- Added regression tests for 0.245 / 0.242 / 0.232 and materially separated score groups.


## 2026-05-10 — Adverse funding score hardening

- Score/ranking now penalizes adverse funding carry through `economic_cost_bps`, including neutral futures grids that can accumulate either side.
- Feature snapshots no longer encode negative signed funding as a cost benefit; `funding_norm` and `funding_cost_norm` are based on approval-only adverse carry.
- Grid density (`grid_count`) now uses execution cost plus adverse funding carry, so expensive carry reduces order density instead of only widening spacing.
- Added regression tests for funding-aware score, feature snapshot and grid-density behaviour.

## 2026-05-10 — Funding receipt score/RR hardening

- Recommendation score and `expected_rr` now use conservative `net_cost_bps = execution_cost_bps + max(expected_funding_bps, 0)`; funding receipts are no longer allowed to improve score or RR.
- Added `signed_net_cost_bps` as a diagnostic so UI/audit can still see the signed carry effect without using it as approval edge.
- `_funding_score_adjustment()` now penalizes only the paying side and returns no positive boost for receiving funding.
- Removed a duplicate chart link in the symbol actions UI and made the remaining bot link explicitly point to Bybit grid-bot creation.
- Added regression tests for funding-receipt score/RR behavior and UI symbol-link shape.

## 2026-05-10 — fail-closed liquidation side and exact ticker scope hardening

### Исправлено
- liquidation helpers больше не трактуют неизвестный `side` как long: `estimate_linear_liq_price()` и `liquidation_buffer_pct()` возвращают `None`, чтобы malformed payload не получал ложный liquidation buffer.
- collector теперь требует exact echoed `symbol` даже в symbol-specific ticker fallback; payload без `symbol` не записывается как рыночные данные целевого Bybit Linear USDT perpetual.
- `.env.example` уточняет, что `REQUIRE_CONF_GATE=1` переводит low-confidence кандидатов в `no_trade`, а не в recommended.

### Добавлено
- регрессионный тест на fail-closed liquidation side.
- регрессионный тест на отказ collector записывать ticker без exact symbol.
- отдельный audit report по fail-closed side/ticker scope hardening.

### Тесты
- `python -m pytest -q` → `413 passed`.

## 2026-05-10 — Tick-safe operator grid snapping

- Operator-facing Bybit metadata snapping now preserves generated grid containment: lower range/kill-switch boundaries snap down and upper boundaries snap up instead of all prices rounding to the nearest tick.
- Exchange-aligned `grid_step.step_abs` and `tp_per_leg.abs` now snap upward so UI/preflight values cannot become thinner than the economics model that covered fees, spread, slippage and adverse funding.
- Added regression coverage for tick-safe range/kill-switch snapping and no-thinner step/TP snapping.
- Validation after this pass: `411 passed`; `python -m py_compile app/*.py tests/*.py main.py`; `node --check app/ui/static/app.js`.

## 2026-05-10 — Funding interval / grid spacing hardening

- `BybitPublicClient.get_funding_rate()` now falls back to `/v5/market/instruments-info` for `fundingInterval` when ticker payload lacks `fundingIntervalHour`, keeping funding event counts tied to Bybit instrument metadata instead of a silent 8h fallback.
- `futures_grid` spacing now includes adverse expected funding carry in the minimum cost floor; funding receipts remain diagnostics and cannot tighten the grid.
- Added regression coverage for funding-interval fallback and funding-aware grid spacing.

## 2026-05-10 — execution-time funding and strict linear-USDT scope hardening
### Исправлено
- execute-preflight для полноценных costed-рекомендаций теперь повторно проверяет свежий `funding_rate`/`funding_interval_min` перед материализацией bot instance и блокирует запуск при missing/stale funding, экстремальном carry или ухудшении funding, которое делает net edge сетки неположительным;
- Bybit trade-plan validation больше не принимает malformed legacy symbols вида `BTC/USDT`/пустой base только потому, что строка оканчивается на `USDT`; нужен точный alphanumeric USDT perpetual symbol;
- pre-listing detection теперь распознаёт строковые upstream-флаги (`"true"`, `"1"`, `"yes"`), а не только boolean `true` и status aliases.

### Добавлено
- регрессионные тесты `tests/test_iteration125_execution_funding_and_scope_hardening.py` на execution-time funding blocks, malformed symbol и string pre-listing flag.

### Тесты
- `pytest -q` → `406 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed.


## 2026-05-10 — execution trade_plan fail-closed audit

- Hardened execution preflight for Bybit Linear USDT Futures grid recommendations: mutating execution now requires a complete `params.trade_plan` with reference price, range, kill-switch and grid-step geometry.
- Kept UI/list/detail validation non-destructive for malformed historical rows while preserving fail-closed execution behavior.
- Updated API lifecycle/rollback tests to seed complete executable futures-grid plans instead of legacy params-only rows.
- Added regression tests for missing and incomplete `trade_plan` execution blocks.
- Verified the full suite: `401 passed`.


## 2026-05-10 — Adverse funding score hardening

- Score/ranking now penalizes adverse funding carry through `economic_cost_bps`, including neutral futures grids that can accumulate either side.
- Feature snapshots no longer encode negative signed funding as a cost benefit; `funding_norm` and `funding_cost_norm` are based on approval-only adverse carry.
- Grid density (`grid_count`) now uses execution cost plus adverse funding carry, so expensive carry reduces order density instead of only widening spacing.
- Added regression tests for funding-aware score, feature snapshot and grid-density behaviour.

## 2026-05-10 — Conservative funding approval edge
- Fixed grid-leg economics so signed funding receipts no longer inflate the canonical `net_profit_bps` used for approval/rejection. Positive funding remains a cost; negative funding is exposed separately as `funding_benefit_excluded_bps` and `net_profit_with_signed_funding_bps`.
- Risk reports now surface the conservative funding-cost basis, excluded funding benefit and signed-funding diagnostic, with a warning when an apparent funding receipt was not counted in approval edge.
- Operator UI now labels conservative net edge separately from signed-funding diagnostics, making it visible that a grid is not approved only because funding is currently favorable.
- README/TRADING_LOGIC updated to document the no-funding-windfall rule.
- Added regression coverage for funding-receipt windfall rejection and UI labels.
- Validation after this pass: `399 passed`; `python -m py_compile app/*.py main.py`; `node --check app/ui/static/app.js`.

## 2026-05-09 — Prompt re-audit: MTF fail-closed and risk-report sync
- Added an explicit `INSUFFICIENT_MTF_HISTORY_FOR_GRID` block: futures-grid recommendations now require at least 3 closed multi-timeframe histories for direction/regime validation, instead of relying on 1m features plus confidence penalty.
- Risk report `decision` is now synchronized whenever recommendation metadata is resynced, so persistence/LLM gates cannot leave a `pending` or `blocked` row with a stale `recommended` risk-report decision.
- UI helper text now states that `pending` is also non-executable until the relevant gate is satisfied.
- Added regression tests for insufficient MTF history and risk-report decision synchronization.

## 2026-05-09 — UI effective status sync fix
- Fixed a table/detail status mismatch where `/api/v1/recommendations` could show a persisted `active` row while `/api/v1/recommendations/{rec_id}` applied the live Bybit operator guard and showed the same row as `blocked`.
- Recommendation list responses now apply the same effective Bybit guard augmentation as detail responses before rendering/filtering statuses.
- Default `recommended+active` view now hides recommendations that are dynamically blocked; they appear only when the `blocked` filter is enabled.
- `no_trade` is now derived from effective operator-facing statuses.
- Added regression coverage for list/detail effective-status consistency.

## 2026-05-09 — UI detail badge fit fix
- Shortened only the compact details/modal bot-type badge to `Linear USDT Grid` while keeping the full `Bybit Linear USDT Futures Grid` table label and title tooltip.
- Changed the detail subtitle row to wrap inside the panel instead of overflowing into metric cards on medium-width layouts.
- Added static regression checks for the compact label, wrapping CSS and cache-key bump.

## 2026-05-09 — Operator infographic update for 100–500 USDT accounts
- Updated the root `how_to_trade.png` operator infographic for small accounts instead of the older 500 USDT / 10x-focused playbook.
- The infographic now defaults to 1 bot, 1–3x leverage, 10–15% margin allocation, 75–85% reserve outside the position, and explicit NO TRADE behavior for blocking validations.
- Added small-account sizing guidance and reminders that Bybit `minNotional`, `qtyStep` and `minQty` failures are valid rejection outcomes, especially near 100 USDT balances.
- Reconciled the infographic text with the current grid-only scope: Bybit Linear USDT Perpetual `futures_grid`, exact symbols, isolated margin, arithmetic grid, net profit after costs, funding known, liquidation buffer and kill-switch guards.

## 2026-05-09 — Linear grid hardening: symbol scope, interval geometry, per-bot caps
- Tightened Bybit Linear USDT scope: malformed symbols such as `BTC/USDT`, `USDT`, `BTC-USDT` and `BTCUSDT-PERP` are rejected/filtered before REST collection or scoring.
- Fixed arithmetic grid range generation: `grid_count` is Bybit's number of price intervals, so total range span now scales with `grid_count`, not `grid_count - 1`.
- Added normalized runtime caps for `max_leverage`, `max_position_notional_usdt` and `max_margin_per_bot_usdt`; recommender blocks candidates that exceed these per-bot risk limits.
- Added regression tests for strict symbol scope, interval-count grid geometry and new risk-cap normalization.

# 2026-05-09 — Arithmetic grid fail-closed hardening

- Execution preflight now blocks `grid_type=geometric` instead of accepting it without dedicated geometric ratio/net-profit/tick-rounding math; the recommender remains arithmetic-only.
- `grid_count` / legacy `grid_levels` is validated as a Bybit Futures Grid interval count even when a manual/legacy payload lacks a complete `trade_plan.levels` range or step.
- Added regression tests for geometric fail-closed behavior and grid-count validation without a complete trade plan.

# 2026-05-09 — Linear perpetual ticker scope hardening

- Public ticker filtering now excludes non-perpetual delivery contracts (`deliveryTime != 0`) and pre-market/pre-listing ticker rows before collector/scoring can use them.
- Per-symbol collector fallback no longer relabels a returned ticker for a different `symbol` as the requested symbol; exact-symbol mismatch is treated as missing market data and fails closed.
- Operator UI venue selector is locked to the only supported scope: Bybit Linear USDT Perpetual futures grid.
- Added regression tests for delivery/pre-market ticker filtering and wrong-symbol ticker relabel protection.
- Full regression suite after this pass: `383 passed`.

# 2026-05-09 — Runtime risk cap hardening

- Runtime risk-limit normalization now clamps `max_concurrent_bots` and `max_symbol_bots` to the Bybit Futures Grid Bot product cap of 50. Operator JSON can make limits stricter, but cannot raise the effective limit above the exchange/product cap.
- Added regression tests for clamped risk-limit normalization and execution gate enforcement.

## 2026-05-09 — strict Linear USDT client boundary and funding labels

- `BybitPublicClient` теперь fail-fast принимает только `category=linear` и символы с суффиксом `USDT`; non-USDT symbols или нецелевой category отклоняются до сетевого запроса.
- Symbol-specific ticker/kline/funding/open-interest/instrument-info paths теперь фильтруют exact `symbol`, чтобы collector не мог присвоить чужую строку market-data запрошенному USDT perpetual.
- Funding payload получил явное поле `directional_funding_bps_per_event`; старый ключ `directional_funding_bps_8h` оставлен только как backward-compatible alias.
- Добавлены regression tests на exact-symbol ticker filtering и fail-fast product boundary.
- Full regression suite after this pass: `377 passed`; `python -m py_compile app/*.py main.py`; `node --check app/ui/static/app.js`.

## 2026-05-09 — live-price preflight fail-closed

- Execution preflight теперь блокирует подтверждение grid-рекомендации, если свежая ticker-запись не содержит пригодной `last`/`bid`/`ask` цены (`LIVE_PRICE_UNAVAILABLE`); freshness без live price больше не считается достаточной для проверки диапазона, kill-switch и drift от reference price.
- Добавлен regression test на свежий, но непригодный ticker, чтобы execution-path не обходил live-price guards при `NULL` price fields.
- UI helper text уточняет, что перед запуском проверяется пригодная live `last`/`bid`/`ask` цена, а не только свежесть ticker-записи.

# 2026-05-09 — Deep grid-linear audit hardening

- Product scope rechecked as grid-only: UI/API/docs/tests no longer contain unsupported strategy examples; invalid payload tests use neutral unsupported placeholders instead of naming disallowed bot families.
- Linear USDT economics fail closed: unknown side no longer silently becomes long for PnL/funding helpers.
- Recommendation risk gates hardened: missing current funding rate blocks Linear USDT perpetual recommendations; highly trending/weak-range markets and extreme ATR get explicit grid rejections.
- Recommendation payload now includes `params.risk_report` with decision, risk profile, net/grid, execution cost, funding impact, funding interval, liquidation buffer, required capital, adverse scenario, rejection reasons, warnings and approval factors.
- Operator UI now renders the risk report directly in the recommendation detail panel.
- Regression baseline: `370 passed`; `ruff check app tests main.py`; `python -m py_compile app/*.py main.py`; `node --check app/ui/static/app.js`.


## 2026-05-09 — Grid-only safety pass
- Filter `SYMBOLS_LINEAR` to USDT perpetual symbols at bootstrap so non-USDT linear/legacy symbols never enter collection/scoring.
- Clarify and expose `grid_type=arithmetic` and `grid_count` as Bybit "Number of Grids" intervals while keeping legacy `grid_levels` compatibility.
- Block unsupported grid types and `grid_count > 400` in Bybit preflight.
- Replace fixed 25 USDT per-leg sizing with conservative qty-step fallback sizing for expensive USDT contracts; live Bybit metadata remains mandatory at execution preflight.
- UI now surfaces grid type and estimated active orders alongside net-per-grid, margin, funding and liquidation buffer.

# 2026-05-09 strict docs cleanup and worst-boundary liquidation guard

- Документация больше не ссылается на отсутствующие внешние report artifacts; регрессионные проверки обновлены на запрет таких ссылок.
- `BybitPublicClient.get_funding_rate()` теперь принимает ticker funding только при точном совпадении `symbol`, как уже делал instrument-info client.
- `params.economics.liquidation_buffer_pct` для grid теперь считается как худшая дистанция до liquidation между reference price и adverse range/kill-switch boundary; UI показывает worst/edge buffer отдельно.
- Execution preflight для leveraged neutral legacy payload проверяет худший long/short liquidation side, а не молча трактует neutral как long.
- Добавлены regression tests на exact-symbol funding ticker и worst-boundary liquidation buffer.
- Full regression suite: `365 passed`; `python -m compileall -q app tests` and `node --check app/ui/static/app.js` passed; `ruff` was not installed in the execution environment.

# 2026-05-09 strict grid-only execution preflight follow-up

- `_fetch_bybit_instrument_meta()` больше не делает linear-metadata fetch для нецелевого `venue`; unsupported payload не может выглядеть валидным из-за случайно подобранной linear metadata.
- `_validate_trade_plan_against_bybit_meta(..., require_meta=True)` теперь прямо блокирует любой `bot_type` кроме `futures_grid`, любой `venue` кроме `linear`, а также off-tick price/grid-step/tp-per-leg параметры как execution errors. В detail/UI режиме эти off-tick условия остаются предупреждениями для операторской диагностики.
- Добавлен `tests/test_iteration117_grid_only_strict_preflight.py`; существующий execution-preflight fixture исправлен на tick-aligned grid step.
- Full regression suite after strict preflight hardening: `363 passed`; `python -m py_compile main.py app/*.py tests/*.py` and `node --check app/ui/static/app.js` passed; `ruff` was not available in the execution environment.

# 2026-05-09 metadata and funding patch

- Execution preflight now fails closed when Bybit instrument metadata lacks `contractType`, `quoteCoin` or `settleCoin`; UI details may still show warnings for partial metadata, but operator execution cannot proceed without confirmed LinearPerpetual / USDT quote / USDT settlement.
- `BybitPublicClient.get_funding_rate()` now preserves `fundingIntervalHour` as `funding_interval_min`, so code paths using the helper do not silently fall back to 8h funding intervals.
- Operator UI details now expose Bybit validation errors/warnings directly instead of hiding them in the technical JSON payload.
- Confidence calibration no longer imports optional sklearn/native ML runtimes during `fit_logreg()`; it uses deterministic in-repo weighted logistic regression with chronological out-of-fold logits, eliminating full-suite hangs and preserving reproducible calibration gates.
- Full regression suite after archive repair: `360 passed`; `python -m py_compile main.py app/*.py` and `node --check app/ui/static/app.js` passed; `ruff` was not available in the execution environment.

# CHANGELOG

## 2026-05-09 — docs cleanup and funding interval hardening

- `funding_signal()` теперь annualizes funding по фактическому `funding_interval_min`, а не жёстко по 8h; UI/API получают тот же сигнал, что и event-aware cost model.
- Execution preflight округляет цены/объёмы через Decimal-based `quantize_step()`, чтобы избежать float artifacts на tick/qty step.
- Тестовые проверки неподдерживаемых payload переименованы без legacy-strategy wording: используется нейтральный `invalid_bot_type`.

## 2026-05-09 — funding interval and net grid economics hardening

### Исправлено
- Funding cost model больше не считает все Bybit Linear USDT perpetual как 8h funding: collector сохраняет `fundingIntervalHour`, DB хранит `funding_interval_min`, recommender считает funding events по фактическому interval.
- Если funding interval отсутствует и ожидаемый funding impact материален, recommendation получает fail-closed блок `FUNDING_INTERVAL_UNCONFIRMED`.
- `grid_leg_economics()` теперь имеет внутренний round-trip taker fee floor, чтобы net profit per grid не мог случайно игнорировать комиссии.
- Удалены остаточные неподдерживаемые strategy/product термины из комментариев и внутренних labels.
- Обновлены README / trading logic / known risks / scenario docs.

### Добавлено
- Regression tests на fee floor, funding interval event count и сохранение `fundingIntervalHour`.

### Тесты
- Targeted regression suite: `10 passed`.
- `python -m py_compile app/*.py main.py` → passed.

## 2026-05-08 — Bybit Linear USDT Futures grid-only economics and risk hardening

### Исправлено
- Добавлен `app/grid_math.py` с Decimal-based расчётами linear PnL, fees, funding cashflow, margin requirement и conservative liquidation buffer.
- Recommender теперь публикует `params.sizing` / `params.economics` и блокирует grid, если net profit per grid после execution friction/funding неположителен или слишком тонкий.
- Execution preflight больше не запрещает любой leverage > 1; вместо этого проверяет Bybit min/max/leverage_step и liquidation buffer.
- Health/warmup контуры больше не считают `linear` дважды.
- UI деталей рекомендации показывает net/gross per grid, estimated margin, order notional, qty/order, liquidation buffer и risk profile.

### Добавлено
- `tests/test_grid_linear_economics.py`.

### Тесты
- segmented `pytest` suite → `353 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `node --check app/ui/static/app.js` → passed.

## 2026-04-24 — live-price execution guard, Bybit status hardening and explicit sizing validation

### Исправлено
- execute-path теперь блокирует operator confirmation, если свежий ticker уже вышел за сохранённый `trade_plan.levels.range` или `kill_switch`; старая grid-рекомендация больше не может быть подтверждена как будто рынок остался в исходном диапазоне;
- Bybit instrument metadata теперь включает `status`, `baseCoin`, `quoteCoin`, `settleCoin`, `unifiedMarginTrade`; `status != Trading` блокирует подтверждение fail-closed;
- добавлена проверка `tp_per_leg` на положительность, схлопывание и выравнивание по `tick_size`;
- добавлен warning при несогласованности `params.grid_levels` и `trade_plan.levels.grid_step.step_abs`;
- удалён лишний дублирующий assignment PostgreSQL `DATABASE_URL` в `settings.py`;
- execution-time validation теперь проверяет явный `order_qty` / `qty_per_leg` / `base_qty` и notional-алиасы из `trade_plan.sizing` или `params` против `qty_step`, `min_order_qty`, `max_order_qty` и `min_notional` Bybit; если размер уже задан и проверен, ложные предупреждения `SIZE_INPUT_REQUIRED` / `MIN_NOTIONAL_NOT_CHECKED` не выводятся.

### Добавлено
- единый helper ценового контекста `trade_plan`, чтобы Bybit validation и live-price guard не расходились в парсинге payload;
- `tests/test_iteration114_live_price_and_status_guards.py`;
- `tests/test_iteration115_order_sizing_validation.py`;
- документация по live-price guard, instrument status guard и остаточным рискам.

### Тесты
- `pytest -q` → `348 passed, 1 warning`;
- targeted docs/integrity + sizing regression → `19 passed, 1 warning`;
- `python -m py_compile main.py app/*.py tests/*.py` → passed;
- smoke import `app.main` → passed.

## 2026-04-23 — startup bootstrap scalability on existing history

### Исправлено
- `db.init_db()` больше не запускает тяжёлый historical publication-lineage backfill на каждом старте. Теперь полный Python backfill выполняется только если в `recommendations` реально найдены legacy-строки без materialized `publication_root_rec_id` / `is_outcome_label_root`;
- bootstrap `bot_instances` больше не пересканирует всю таблицу на каждом рестарте: backfill `publication_root_rec_id` выполняется только если обнаружены пустые legacy-значения;
- глубокий retrofit `repair_async_llm_pending_publication_chains()` больше не вызывается автоматически на старте процесса. Это отдельная maintenance-операция для исторической БД, а не обязательный шаг штатного перезапуска.

### Добавлено
- регрессионные тесты на то, что `init_db()` не делает полный rescanning already-materialized recommendations и bot_instances при обычном рестарте.

### Практический эффект
- повторный `python main.py` на БД с накопленной историей больше не должен зависать из-за безусловного startup-repair старых рекомендаций.

## 2026-04-22 — red-team hardening: exact Bybit instrument match and savepoint-safe idempotency

### Исправлено
- `BybitPublicClient.get_instrument_info()` теперь принимает metadata только при точном совпадении `symbol`; если upstream/stub вернул список без целевого инструмента, клиент fail-closed возвращает `None`, а не берёт первый попавшийся элемент;
- `_fetch_bybit_instrument_meta()` теперь сохраняет в кэш реальные `symbol/category`, пришедшие от upstream, а не безусловно повторяет запрошенные значения. Это возвращает смысл проверкам `BYBIT_META_SYMBOL_MISMATCH` / `BYBIT_META_CATEGORY_MISMATCH`;
- `db.insert_bot_instance()` и `db.insert_trade()` переведены на `SAVEPOINT`-обёртку вокруг INSERT, чтобы после `IntegrityError` корректно классифицировать дубликаты и не ронять всю внешнюю транзакцию в PostgreSQL aborted-state.

### Добавлено
- регрессионные тесты на exact-symbol instrument metadata, на сохранение фактического upstream symbol в prefetch cache и на savepoint-safe duplicate classification для bot/trade inserts.

### Тесты
- `pytest -q` → `342 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed.

## 2026-04-15 — outcome backlog hardening under LLM mode

### Исправлено
- `compute_outcomes_once()` теперь фильтрует LLM-eligible рекомендации в SQL **до** `ORDER BY ... LIMIT`, поэтому oldest-first окно больше не засоряется legacy/root rows без финального `llm_review.status=ok` и outcome-worker снова доходит до реально созревших рекомендаций;
- release-документация приведена к фактическому составу поставки: README больше не ссылается на отсутствующие документы, baseline тестов обновлён.

### Добавлено
- row-level locking (`FOR UPDATE`) для mutating API-путей в PostgreSQL, чтобы concurrent `execute` / `trade` / `stop` не теряли согласованность состояния;
- выравнивание standalone migration-файлов `init.sql` / `init_postgres.sql` с runtime-bootstrap: добавлены индексы и уникальный инвариант по `publication_root_rec_id` для running-ботов;
- регрессионные тесты на LLM outcome backlog starvation и на целостность release-doc артефактов.

### Тесты
- `pytest -q` → `322 passed`;
- `pytest --cov=app --cov-report=term-missing -q` → passed;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `ruff check app tests main.py` → passed.

## 2026-04-10 — execution-path lock ordering, stricter Bybit metadata validation and operator UI tables

### Исправлено
- `POST /api/v1/recommendations/{rec_id}/action` больше не захватывает SQLite `BEGIN IMMEDIATE` до execution-time prefetch metadata Bybit: сетевой preflight снова выполняется вне write-lock, как и задумано архитектурой, поэтому медленный upstream не должен блокировать collector/recommender/operator writer-контур;
- execution-time Bybit validation теперь fail-closed блокирует несоответствие `metadata.category` и `recommendation.venue`, а не оставляет это предупреждением;
- модальное окно UI расширено, таблицы внутри модалок получили sticky header + независимую прокрутку тела, а самый широкий журнал исходов переведён в более компактную плотность строк.

### Добавлено
- регрессионный API-тест на порядок `Bybit prefetch -> BEGIN IMMEDIATE` в execution-path;
- регрессионный тест на fail-closed блокировку `BYBIT_META_CATEGORY_MISMATCH`;

### Тесты
- `pytest -q` → `318 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `ruff check app tests main.py` → passed.

## 2026-04-09 — fail-closed execution validation and upstream shape hardening

### Исправлено
- execution-time Bybit validation теперь блокирует legacy/manual recommendations без явного `margin_mode` вместо молчаливого допуска исполнения в неявном режиме;
- validation теперь fail-closed отклоняет рекомендации, если полученная metadata Bybit относится к другому `symbol`, а не к целевому инструменту recommendation;
- публичный Bybit REST-клиент теперь ретраит не только decode-failures, но и transient `response shape error` сценарии: не-объектный JSON и битый `retCode`.

### Добавлено
- warning `ACCOUNT_MODE_LEGACY_ALIAS` для исторических futures rows с `account_mode=one_way`, чтобы отделить legacy-совместимость от штатной модели `account_mode=unified`;
- регрессионные тесты на блокировку missing-`margin_mode`, symbol-mismatch Bybit metadata и retry битых shape-ответов публичного клиента.

### Тесты
- `pytest -q` → `316 passed`;
- `python -m py_compile app/*.py tests/*.py main.py` → passed;
- `ruff check app tests main.py` → passed.

## 2026-04-08 — red-team reliability and operator-signal hygiene

### Исправлено
- execute-path больше не держит SQLite `BEGIN IMMEDIATE` во время внешнего запроса за instrument metadata Bybit: metadata теперь подгружается заранее, вне write-lock, а внутри критической секции используется уже готовый snapshot проверки;
- operator-facing `GET /api/v1/recommendations` теперь по умолчанию скрывает дубли одной `publication_chain`, оставляя в списке один лучший элемент на `publication_root_rec_id`, чтобы repeated `active` updates не выглядели как поток одинаковых идей;
- operator-facing collapse больше не зависит от жёсткого лимита `top_n * 4`: API адаптивно расширяет сырой scan-budget, если одна publication-chain доминирует длинной серией `active` updates и вытесняет другие уникальные идеи из snapshot;
- публичный Bybit REST-клиент теперь ретраит transient transport/protocol ошибки и битые 2xx decode-failures, а также считает HTTP 408 retryable upstream-сценарием.

### Добавлено
- ответ `GET /api/v1/recommendations` дополнен блоком `publication_chain_dedupe` с числом скрытых дублей и возможностью отключить collapse через `collapse_chains=false`;
- регрессионные тесты на отсутствие внешнего Bybit fetch под SQLite write-lock, на adaptive collapse больших duplicate-chain bursts и на retry transient Bybit transport/decode failures.

### Тесты
- добавлен сценарий, который проверяет порядок `Bybit metadata fetch -> BEGIN IMMEDIATE`, чтобы execute-flow не блокировал остальные writer-контуры на сетевых задержках;
- добавлен API-тест на collapse raw-дублей `recommended/active` внутри одной publication-chain;
- добавлены transport-тесты на `RemoteProtocolError` и malformed-JSON retry-path в публичном Bybit-клиенте.

## 2026-04-08 — release consistency and stop-state determinism

### Исправлено
- `.env.example` синхронизирован с фактическими runtime-дефолтами LLM-reviewer (`LLM_REVIEWER_MAX_CANDIDATES=24`, `LLM_REVIEWER_MAX_WORKERS=2`);
- остановка бота теперь использует единый `stopped_ts` для строки `bot_instances` и `state_json`, чтобы audit/state reconciliation был детерминированным.

### Добавлено
- API-регрессии на синхронность `stopped_ts` для manual stop и `stop_bot=true` при записи trade.

### Тесты
- расширен регрессионный набор на stop-state timestamp consistency;
- подтверждена согласованность `.env.example` с runtime/default docs.

## 2026-04-07 — hardening revision

### Исправлено
- усилена execution-time Bybit validation:
  - добавлена проверка внутренних инвариантов `bot_type ↔ venue ↔ direction`;
  - добавлена проверка `account_mode` / `margin_mode` против фактической модели проекта;
  - добавлена проверка `min_leverage`, `max_leverage`, `leverage_step`;
  - validation теперь явно показывает `snapped` leverage при off-step значении;
- шаблон `.env.example` теперь явно содержит `SYMBOLS_LINEAR`, а не только закомментированный пример.

### Добавлено
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/TRADING_LOGIC.md`
- `docs/SCENARIOS.md`
- `docs/KNOWN_RISKS.md`

### Тесты
- добавлены регрессионные тесты на mode/leverage validation;
- добавлены тесты release-integrity для новых docs/env cross-reference.

## 2026-04-15 — hardening after deep review
- PostgreSQL mode теперь требует явно заданный `DATABASE_URL`; unsafe-default на localhost удалён.
- Сообщение о старте в PostgreSQL-режиме без установленного `psycopg[binary]` сделано явным и операционно полезным.
- Захват `runtime_locks` в PostgreSQL переведён на atomic UPSERT, чтобы исключить split-brain при конкурентном старте нескольких процессов.
- Для `bot_instances` введён materialized `publication_root_rec_id` и жёсткий инвариант: не более одного `running`-бота на одну publication-chain.
- Bootstrap теперь fail-closed обнаруживает исторически повреждённые БД с дублирующими running-ботами в одной chain.
- Добавлены регрессионные тесты для PostgreSQL bootstrap, runtime-lock safety и publication-chain execution safety.
