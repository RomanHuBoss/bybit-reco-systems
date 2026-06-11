# Audit report: Bybit futures directional/risk re-audit

Дата: 2026-06-11  
Scope: Bybit futures / linear USDT recommendation and operator execution-preflight layer.  
Primary focus: long/short semantics, TP/SL mapping, Bybit one-way side semantics, risk/preflight gating, OHLC signal robustness, UI/API consistency.

## Executive summary

Проект уже содержал сильную базовую защиту: единый `app/trading_semantics.py` для `directional_exit_levels`, backend-валидацию TP/SL geometry, Bybit linear metadata validation, UI fallback для short TP/SL и регрессионные тесты. Повторная проверка выявила не столько прямую инверсию TP/SL, сколько несколько мест, где будущие изменения могли снова внести финансово опасную рассинхронизацию:

1. не было отдельной исполнимой canonical-модели PnL/risk-reward для long/short TP/SL;
2. защитная Bybit-семантика TP/SL не была явно зафиксирована отдельным helper-ом как reduce-only close-order;
3. `vote_for_tf()` как standalone-функция могла упасть или исказить directional score на пустых/несогласованных OHLC-векторах;
4. в коде был небольшой дублирующий assignment в OI-trend ветке.

Исправления минимальные и системные: добавлены fail-closed helpers, новые тесты и документация без изменения внешней API-формы существующих recommendation payloads.

## Карта проверенных зон

- `app/trading_semantics.py` — canonical long/short/neutral TP/SL mapping, geometry validation, Bybit side/reduceOnly semantics.
- `app/main.py` — `_validate_trade_plan_against_bybit_meta`, `_augment_reco_for_ui`, `_execution_live_price_blocks`, operator guard.
- `app/grid_math.py` — linear USDT PnL, margin, liquidation-buffer approximation, grid economics.
- `app/recommender.py` — open-candle dropping, time-series feature usage, generated grid params.
- `app/direction.py` — RSI/MACD/slope/ATR/BB directional score calculation.
- `app/features.py` — volatility/ATR/OI/BTC-beta feature hardening.
- `app/ui/static/app.js` — operator cards/details, TP/SL labels, `directional_exit_levels` consumption, distance display.
- `tests/test_iteration147_*`, `tests/test_iteration148_*`, `tests/test_iteration150_*`, `tests/test_iteration151_*`, `tests/test_iteration152_*` and the new `tests/test_iteration153_prompt_directional_risk_reaudit.py`.

Static scan terms used: `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`, `positionIdx`.

## Findings and fixes

### HIGH — Missing executable directional PnL/risk-reward source of truth

- File: `app/trading_semantics.py`
- Problem: TP/SL geometry was validated, and grid PnL existed in `app/grid_math.py`, but there was no compact canonical function that tied direction, entry, TP, SL, quantity, gross profit/loss, reward%, risk% and RR into one fail-closed model.
- Trading risk: future UI, reports, notifications or execution adapters could recalculate RR using a long-only formula or an absolute-distance formula. For short positions this can invert profit/loss interpretation without failing tests.
- Fix: added `DirectionalTradeMath` and `directional_trade_math()`.
- Correct semantics now fixed by tests:
  - long: TP > entry, SL < entry, profit when price rises;
  - short: TP < entry, SL > entry, profit when price falls;
  - invalid/swapped geometry returns `None` rather than a negative or misleading RR.
- Tests: `test_directional_trade_math_uses_symmetric_long_short_pnl_and_rr`, `test_directional_trade_math_rejects_invalid_or_swapped_geometry`.

### HIGH — Protective Bybit TP/SL semantics not explicitly locked as reduce-only close semantics

- File: `app/trading_semantics.py`
- Problem: `bybit_linear_order_semantics(direction, "close")` already encoded one-way close sides, but TP/SL protective orders were not separately documented/tested as reduce-only/closeOnTrigger stop-order semantics.
- Trading risk: a future adapter could create protective trigger orders that increase exposure instead of reducing it, especially for short exits where close side is `Buy`.
- Fix: added `bybit_linear_protective_order_semantics(direction, exit_kind)`.
- Correct semantics:
  - long TP/SL close side: `Sell`;
  - short TP/SL close side: `Buy`;
  - `reduceOnly=True`, `closeOnTrigger=True`, `positionIdx=0`, `category=linear`, `position_mode=one_way`.
- Tests: `test_bybit_protective_tp_sl_orders_are_always_reduce_only_close_orders`.

### MEDIUM — Standalone `vote_for_tf()` could fail open/crash on malformed OHLC vectors

- File: `app/direction.py`
- Problem: the recommender normally provides cleaned closed candles, but `vote_for_tf()` itself assumed non-empty, aligned `closes/highs/lows` and used `closes[-1]`. Empty or badly mismatched vectors could raise or distort signal calculations.
- Quant/econometric risk: malformed data could create an exception in signal generation or an unstable score after dropped/duplicated/partial candle rows. This is a fail-open risk in a trading pipeline.
- Fix: added `_safe_ohlc_vectors()` and `_neutral_tf_vote()`; `vote_for_tf()` now sanitizes finite positive rows, repairs crossed OHLC bounds conservatively, and returns neutral score if valid history is insufficient.
- Tests: `test_vote_for_tf_fails_neutral_on_empty_or_malformed_ohlc_instead_of_crashing`, `test_vote_for_tf_sanitizes_mismatched_vectors_without_using_future_or_bad_rows`.

### LOW — Duplicate assignment in OI trend branch

- File: `app/features.py`
- Problem: duplicate `trend = "falling"` assignment in one branch.
- Trading risk: no functional risk, but it adds noise in a risk-sensitive feature module and increases review ambiguity.
- Fix: removed duplicate assignment.

### VERIFIED — Short TP/SL UI/backend mapping

- Files: `app/trading_semantics.py`, `app/main.py`, `app/ui/static/app.js`, `tests/test_iteration147_short_tp_sl_ui_hardening.py`, `tests/test_iteration148_directional_semantics_hardening.py`, `tests/test_iteration153_prompt_directional_risk_reaudit.py`
- Result: no new inversion found in audited operator paths.
- Existing and retained behavior:
  - backend `directional_exit_levels("short", lower, upper)` returns TP=`lower`, SL=`upper`;
  - UI fallback `operatorExitLevels("short", killLower, killUpper)` returns TP=`killLower`, SL=`killUpper`;
  - UI prefers backend `directional_exit_levels` when present;
  - text labels explain that long TP is above entry and short TP is below entry, while long SL is below entry and short SL is above entry.

### VERIFIED — Neutral/grid semantics

- Files: `app/trading_semantics.py`, `app/main.py`, `app/ui/static/app.js`
- Result: neutral grids are not silently mapped to a single directional Bybit order by `bybit_linear_order_semantics`; directional TP is disabled for neutral exit-level mapping.
- Residual risk: a true live OMS adapter is outside this repository scope; if added later, it must call the canonical helpers and preserve reduce-only protective semantics.

### VERIFIED — Bybit linear USDT preflight constraints

- Files: `app/main.py`, `app/grid_math.py`, `tests/test_iteration105_execution_preflight_and_bybit_validation.py`, `tests/test_iteration115_order_sizing_validation.py`, `tests/test_iteration116_linear_usdt_fail_closed.py`, `tests/test_iteration117_grid_only_strict_preflight.py`, `tests/test_iteration118_grid_bot_risk_caps.py`, `tests/test_iteration145_execution_economics_fail_closed.py`
- Result: targeted regression tests confirm strict validation around linear venue, tick/qty steps, minNotional, unsupported bot types, leverage bounds, liquidation buffer, grid-only execution preflight and operator guard.
- Residual risk: live Bybit V5 behavior was not queried from the sandbox; external API checks require configured credentials/network and should be repeated in testnet before production.

## Tests added

New file: `tests/test_iteration153_prompt_directional_risk_reaudit.py`

Coverage added:

1. symmetric long/short TP/SL PnL and RR;
2. rejection of swapped/invalid long-short geometry;
3. Bybit protective TP/SL close side and reduce-only semantics;
4. neutral fail-closed OHLC voting on empty/malformed data;
5. finite bounded signal output after OHLC vector sanitization;
6. UI regression coverage for short TP/SL direction labels and backend `directional_exit_levels` consumption.

## Checks executed

### Passed

- `python -m compileall -q app tests main.py`
- `node --check app/ui/static/app.js`
- `find . -name '*.js' -print | xargs -r -n1 node --check`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 timeout 120s pytest -q` → `523 passed in 23.47s`
- Split full-suite verification:
  - `pytest -q tests/test_iteration*.py` → `370 passed in 21.43s`
  - `pytest -q tests/test_api.py tests/test_grid_linear_economics.py tests/test_logic.py tests/test_sentiment_pipeline.py` → `153 passed in 6.61s`
- Focused trading/risk regression subset:
  - `pytest -q tests/test_grid_linear_economics.py tests/test_iteration105_execution_preflight_and_bybit_validation.py tests/test_iteration115_order_sizing_validation.py tests/test_iteration116_linear_usdt_fail_closed.py tests/test_iteration117_grid_only_strict_preflight.py tests/test_iteration118_grid_bot_risk_caps.py tests/test_iteration124_prompt_reaudit.py tests/test_iteration145_execution_economics_fail_closed.py tests/test_iteration147_short_tp_sl_ui_hardening.py tests/test_iteration148_directional_semantics_hardening.py tests/test_iteration150_timeseries_ui_audit.py tests/test_iteration151_operator_distance_and_ui_failclosed.py tests/test_iteration152_deep_trading_reaudit.py tests/test_iteration153_prompt_directional_risk_reaudit.py` → `89 passed in 10.29s`

### Not applicable / unavailable

- `npm test`, `yarn test`, `pnpm test`: no `package.json` / JS package manager project found.
- `ruff check`: `ruff` is not installed in the sandbox.
- Live/testnet Bybit V5 private checks: not executed because the sandbox has no configured exchange credentials and live trading must not be exercised by audit code.

### Note on default pytest plugins

A default `timeout 120s pytest -q` run displayed all test progress through 100%, but the process did not exit before the shell timeout in this environment. Re-running with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` completed normally with `523 passed in 23.47s`, and the full suite also passed when split by test groups. This points to external pytest plugin teardown/autoload behavior in the execution environment rather than a project test failure.

## Residual risks

1. This repository is still an operator/recommendation layer, not a complete live OMS. Any future live order adapter must use the canonical `trading_semantics` helpers and add exchange-level integration tests for partial fills, retries, stale orders, reduce-only triggers and reconciliation.
2. Approximate liquidation-buffer calculations are conservative preflight diagnostics, not an exact clone of Bybit's live liquidation engine.
3. Risk limits depend on accurate realized trade/PnL ingestion. If fills/fees are missing, daily loss/drawdown limits cannot fully reflect real account risk.
4. Network/API failures, Bybit account-mode mismatches, API-key scope mismatches and rate limits still require real testnet/live preflight before production use.
5. UI static tests assert key strings and semantic wiring; they do not replace browser E2E tests for every modal/table/chart state.

## Files changed

- `app/trading_semantics.py`
- `app/direction.py`
- `app/features.py`
- `tests/test_iteration153_prompt_directional_risk_reaudit.py`
- `docs/AUDIT_REPORT_2026-06-11_PROMPT_DEEP_DIRECTIONAL_RISK_REAUDIT.md`
