# Audit report 2026-06-11 — Runtime risk caps re-audit

## Scope

Performed a focused post-prompt re-audit of the Bybit Linear USDT Futures recommendation system with emphasis on:

- long/short directional semantics;
- TP/SL and kill-switch mapping;
- execution-time risk gates;
- Bybit V5 linear instrument constraints;
- UI/API consistency for operator-facing execution payloads;
- available static and dynamic checks.

This project remains an operator/recommendation layer, not a production-grade OMS/EMS. No live order-sending adapter was added.

## External Bybit V5 reference checks

Reviewed current Bybit V5 documentation for the audited execution assumptions:

- `/v5/order/create`: `side` is `Buy`/`Sell`; perps/futures order qty is always base qty; price precision must follow `priceFilter.tickSize`; `reduceOnly=true` is a reduce-only order behavior.
- `/v5/market/instruments-info`: instrument constraints include product category, symbol, tick/lot filters, leverage filters, and fields that may change; metadata cannot be treated as static.
- position mode: one-way mode supports a single Buy/Sell-side position; hedge mode can hold both sides.
- `positionIdx`: `0` one-way, `1` hedge Buy side, `2` hedge Sell side.
- `/v5/position/trading-stop`: TP/SL uses explicit `takeProfit`, `stopLoss`, trigger fields, and `positionIdx`.

## Summary of findings

| Severity | Finding | Risk | Status |
|---|---|---|---|
| High | Execution path re-checked active bot count, daily DD, cooldown and symbol count, but did not re-check per-bot notional/margin/max-leverage caps against the exact snapped payload immediately before materializing a bot. | Operator could tighten runtime risk limits after a recommendation was published, or auto-snap could increase qty/notional to satisfy Bybit filters, while the execution path still materialized the bot using stale publication-time sizing assumptions. | Fixed |
| Medium | Legacy tests and lifecycle fixtures may carry minimal `trade_plan`/sizing data. The new check must not block safer low leverage merely because `min_leverage` defaults to 5. | False-positive execution blocks in legacy/manual payloads; lower leverage is not a safety violation. | Fixed by enforcing max leverage, not min leverage, at runtime-size guard |
| Low | Static search still shows many occurrences of directional terms (`tp`, `sl`, `short`, `long`, `upper`, `lower`, `risk`) across app/UI/tests. | Ongoing maintenance risk: future changes can reintroduce divergent sign or label assumptions. | Covered by existing and added regression tests |

## Fixes applied

### 1. Execution-time runtime size/leverage cap guard

**File:** `app/main.py`

Added `_execution_runtime_size_risk_blocks(rec, limits)`.

The helper:

- runs only for `bot_type=futures_grid` and `venue=linear`;
- uses `normalize_risk_limits()` to read current effective runtime limits;
- checks the snapped payload that will be persisted in `bot_instances`;
- blocks if explicit leverage exceeds `max_leverage`;
- blocks if estimated max position notional exceeds `max_position_notional_usdt`;
- blocks if estimated margin exceeds `max_margin_per_bot_usdt`;
- infers missing margin from `notional / leverage` when possible;
- infers missing notional from `margin * leverage` when possible;
- does not block on `min_leverage`, because lower leverage is safer and legacy/manual payloads previously relied on 1x fallback.

### 2. Execution lifecycle integration

**File:** `app/main.py`

The operator execution path now:

1. fetches current risk limits;
2. runs existing `gate_candidate()` count/DD/cooldown/symbol gates;
3. snaps the recommendation payload to current Bybit metadata;
4. runs the new runtime size/leverage cap guard on the snapped payload;
5. logs `EXECUTION_SIZE_RISK_BLOCKED` and rejects with HTTP 409 if caps are breached;
6. only then proceeds to execution preflight and bot materialization.

This closes the gap between publication-time risk checks and operator-action-time risk checks.

## Tests added

**File:** `tests/test_iteration154_execution_runtime_risk_caps.py`

Added tests for:

1. execution-time block when `max_leverage`, `max_position_notional_usdt`, and `max_margin_per_bot_usdt` are breached;
2. no false block when snapped payload values remain within current caps;
3. inference of missing notional from margin and leverage.

## Existing relevant protections confirmed

Existing tests already cover:

- canonical long/short TP/SL mapping;
- short TP below entry and SL above entry;
- long TP above entry and SL below entry;
- directional PnL and risk/reward math;
- neutral grid not being silently converted to a single Bybit directional order;
- one-way Bybit side mapping (`long open=Buy`, `long close=Sell`, `short open=Sell`, `short close=Buy`);
- protective TP/SL semantics as reduce-only close-side orders;
- UI consumption of backend `directional_exit_levels`;
- Bybit tick/qty/minNotional validation;
- execution preflight for market-data freshness, live-price drift, funding staleness, funding deterioration, market shock, fast-veto, unsupported bot/venue, off-tick geometry and range/step mismatches.

## Checks run

Passed:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q app main.py tests
node --check app/ui/static/app.js
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
```

Result:

```text
526 passed in 17.18s
```

Not run:

```text
ruff check app tests main.py
```

Reason: `ruff` is not installed in this execution container. `requirements-dev.txt` includes `ruff==0.15.9`, so this check should be run in a dev/release environment after installing dev dependencies.

## Static scan snapshot

Keyword occurrence counts across `app`, `app/ui/static/app.js`, and `tests` after the fix:

```text
tp: 597
sl: 249
stop: 278
take: 139
upper: 420
lower: 503
short: 307
long: 402
side: 149
Buy: 6
Sell: 6
reduceOnly: 7
kill: 201
leverage: 428
pnl: 145
roi: 0
risk: 588
```

The volume is expected for this project, but these terms remain high-risk maintenance areas. Future changes to any of these blocks should include regression tests.

## Residual risks

- The repository remains a recommendation/operator-control layer and does not implement a full exchange-grade OMS with live fill tracking, order state machines, WebSocket reconciliation, partial-fill reconstruction, or durable idempotent order submission to Bybit.
- Liquidation estimates are conservative approximations; exact liquidation depends on account state, risk tier, mark price, maintenance margin and exchange-side rules.
- UI and API are now aligned through backend `directional_exit_levels`, but future UI-only edits can still reintroduce label drift unless tests are maintained.
- Runtime risk caps depend on the recommendation payload carrying reliable sizing/economics fields; if size is entirely absent, execution preflight can warn but cannot prove notional/margin exposure.
- Full ruff lint was not executable in the current container because `ruff` was unavailable.

## Files changed

- `app/main.py`
- `tests/test_iteration154_execution_runtime_risk_caps.py`
- `docs/AUDIT_REPORT_2026-06-11_RUNTIME_RISK_CAPS_REAUDIT.md`
