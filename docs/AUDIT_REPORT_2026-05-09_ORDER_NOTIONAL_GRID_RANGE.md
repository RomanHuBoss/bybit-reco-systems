# Audit report — Bybit Linear USDT Futures grid sizing preflight hardening — 2026-05-09

## Scope

Reviewed the uploaded project archive as a grid-only recommendation system for Bybit Linear USDT Futures / USDT Perpetual. The focus of this iteration was execution preflight for explicit operator sizing, exchange minNotional validation, and consistency between base quantity and quote notional in `trade_plan.sizing`.

## Project map

- `app/bybit_client.py` — public Bybit V5 REST client; enforces `category=linear`, exact `*USDT` symbol filtering, ticker/funding/open-interest/instrument-info collection.
- `app/collector.py` — OHLCV/ticker/funding/open-interest collection, derived timeframes, stale/error guards.
- `app/features.py`, `app/regime.py`, `app/direction.py`, `app/shock_guard.py` — market features, multi-timeframe direction/regime and veto logic.
- `app/recommender.py` — scoring, cost/funding model, grid range/spacing/count, sizing estimate, liquidation-buffer estimate, recommendation/rejection publication.
- `app/grid_math.py` — Decimal-based linear-USDT helper math for PnL, fees, funding, margin and liquidation-buffer estimates.
- `app/risk.py` — runtime risk limits, daily drawdown/cooldown/concurrent bot caps.
- `app/main.py` — FastAPI endpoints, operator view, execution preflight, Bybit metadata validation and lifecycle actions.
- `app/ui/static/*` — static operator UI for recommendations, risk report and execution details.
- `migrations/*` and `app/db.py` — SQLite/Postgres schema and persistence helpers.
- `tests/*` — unit/integration/scenario regression suite.
- `docs/*`, `README.md`, `.env.example` — operator and architecture documentation.

## Critical finding fixed

| Area | Error | Risk | Fix | Files |
|---|---|---|---|---|
| Bybit execution preflight / order sizing | Explicit `order_qty` was checked against `minNotionalValue` at `reference_price` only. A fixed base quantity could pass at reference price while lower grid orders at `range.lower` fall below Bybit min notional. | Grid bot creation can fail on lower levels, or the UI can falsely show an executable sizing plan. | Added conservative `grid_min_price = min(reference, lower, upper)` check for base-qty notional. If `qty * grid_min_price < minNotionalValue`, preflight blocks with `ORDER_NOTIONAL_BELOW_MIN`. | `app/main.py`, `tests/test_iteration115_order_sizing_validation.py` |
| Bybit execution preflight / inconsistent sizing | Manual payloads could contain both base qty and quote notional with materially different values. | Margin/min-notional/risk report can be calculated from one field while execution uses another. | Added `ORDER_QTY_NOTIONAL_MISMATCH` fail-closed validation when `order_qty * reference_price` materially diverges from declared `order_notional`. | `app/main.py`, `tests/test_iteration115_order_sizing_validation.py` |

## Trading logic changes

- Grid remains restricted to `bot_type=futures_grid`, `venue=linear`, USDT symbols and isolated linear futures preflight.
- PnL/fees/funding/leverage/liquidation math was already present and covered by existing tests; this iteration did not replace those formulas.
- Exchange min-notional validation now uses the lowest executable main-grid price for base quantity, matching the fact that a grid order’s value is `qty * actual_order_price`.
- Sizing payload consistency is now explicitly validated before execution.

## Backend changes

- Added `_grid_min_notional_price()` in `app/main.py`.
- Reworked explicit `order_qty` min-notional check in `_validate_trade_plan_against_bybit_meta()` to use conservative grid-range pricing.
- Added fail-closed mismatch validation for combined `order_qty` + `order_notional` payloads.

## Frontend/UI

- No UI files required code changes in this iteration. The existing UI already surfaces `bybit_plan_validation` errors/warnings, so the new validation codes become visible through the existing operator details flow.

## Documentation/config changes

- `README.md` documents lower-grid min-notional validation and qty/notional consistency checks.
- `docs/TRADING_LOGIC.md` documents `ORDER_QTY_NOTIONAL_MISMATCH` and the range-aware minNotional rule.
- `docs/KNOWN_RISKS.md` clarifies that these checks do not replace live Bybit preview / available balance verification.

## Tests

Added/updated tests:

- `test_bybit_plan_validation_checks_min_notional_at_lower_grid_price`
- `test_bybit_plan_validation_blocks_inconsistent_qty_and_notional`
- Existing sizing fixtures were adjusted so accepted examples remain valid under lower-bound min-notional validation.

Commands run:

```bash
python -m pytest tests/test_iteration115_order_sizing_validation.py tests/test_iteration117_grid_only_strict_preflight.py -q
python -m pytest -q
```

Result:

```text
381 passed in 10.75s
```

`ruff` was not run in the container because the current environment does not have `ruff` installed. The project declares it in `requirements-dev.txt`; run it after installing dev requirements.

## Residual risks

- Live Bybit fees, VIP tier, funding interval/rate and instrument limits must still be refreshed from production Bybit before execution.
- Exact liquidation remains an approximation; Bybit risk tier, account mode, mark price, open positions and wallet margin can change the real liquidation price.
- Available balance and actual grid-bot order preview still require live account/execution integration.
- Slippage and partial fills remain model estimates.
- Funding path over the bot lifetime is unknown; only current/near-term funding is modeled.

## Commands

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
