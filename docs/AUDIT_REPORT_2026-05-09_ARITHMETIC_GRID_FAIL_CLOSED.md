# Audit report — Arithmetic grid fail-closed hardening, 2026-05-09

## Scope

Проверен проект рекомендательной системы для Bybit Linear USDT Futures / USDT Perpetual grid-ботов. Продуктовая граница оставлена строгой: `bot_type=futures_grid`, `venue=linear`, USDT-quoted/USDT-settled LinearPerpetual.

## Project map

| Layer | Files | Responsibility |
|---|---|---|
| Bybit public API | `app/bybit_client.py`, `app/collector.py` | Ticker/OHLCV/funding/open-interest/instrument-info collection for `category=linear` and USDT symbols. |
| Market features | `app/features.py`, `app/direction.py`, `app/regime.py`, `app/shock_guard.py`, `app/sentiment_features.py` | ATR, spread/liquidity, funding/OI signals, multi-timeframe direction/regime and market shock vetoes. |
| Grid economics | `app/grid_math.py`, `app/recommender.py` | Decimal-based linear PnL/fees/funding/margin/liquidation-buffer estimates and arithmetic grid parameter generation. |
| Risk gates | `app/risk.py`, `app/recommender.py`, `app/main.py` | Runtime risk limits, recommendation blocks, execution preflight, instrument metadata validation. |
| API/backend | `app/main.py`, `app/db.py`, `app/db_backend.py`, `migrations/*` | REST API, persistence, recommendation lifecycle, bot/trade audit records, SQLite/PostgreSQL support. |
| UI | `app/ui/static/index.html`, `app/ui/static/app.js`, `app/ui/static/styles.css` | Operator dashboard with recommendation status, risk report, grid economics, funding/liquidation warnings and Bybit preflight output. |
| Tests | `tests/*` | Unit, API, preflight, scenario, DB, calibration and hardening tests. |
| Docs/config | `README.md`, `.env.example`, `docs/*`, `CHANGELOG.md` | Operator docs, product scope, known risks, launch commands and audit trail. |

## Critical findings and fixes

| Area | Finding | Risk | Fix | Files |
|---|---|---|---|---|
| Grid type validation | Execution preflight accepted `grid_type=geometric` while the recommender/economics engine only generates and proves arithmetic grid math. | A manual/legacy geometric payload could pass preflight with arithmetic-style step/net-profit assumptions, overstating profitability and tick validity. | Block every non-`arithmetic` grid type fail-closed until separate geometric ratio-level, net-profit and tick-rounding math is implemented. | `app/main.py`, `tests/test_iteration117_grid_only_strict_preflight.py`, `README.md`, `docs/TRADING_LOGIC.md`, `CHANGELOG.md` |
| Grid count validation | `grid_count`/`grid_levels` product-cap validation was nested under complete range/step checks. | A malformed payload without full `trade_plan.levels` could bypass the Bybit 2..400 interval limit. | Validate `grid_count`/legacy `grid_levels` independently from price metadata and trade-plan completeness. | `app/main.py`, `tests/test_iteration117_grid_only_strict_preflight.py` |
| Documentation consistency | Docs said arithmetic/geometric was accepted while generated economics were arithmetic-only. | Operator could assume geometric mode was safe although no dedicated proof existed. | Rewrite docs to state arithmetic-only execution support and geometric fail-closed behavior. | `README.md`, `docs/TRADING_LOGIC.md`, `CHANGELOG.md` |

## Trading logic status

- Linear USDT PnL remains `qty * (exit - entry)` for long and `qty * (entry - exit)` for short in `app/grid_math.py`.
- Fees remain explicit round-trip bps and are included in `grid_leg_economics()` as execution cost floor.
- Funding remains event-aware and direction-aware in `_estimate_cost_model()`; neutral futures grid uses adverse-side absolute funding cost.
- Margin remains `notional / leverage`, and leverage does not change absolute PnL.
- Liquidation risk remains approximate/fail-closed: reference and adverse kill-switch boundary buffers are used for gating and UI risk display.
- Recommendation logic can return `blocked` / `no_trade`; it does not always recommend a bot.
- New hardening: only arithmetic grid is considered proven in this codebase. Geometric grid is rejected until its own formulas and tests exist.

## Backend/API changes

- Added constants for supported recommender grid type and Bybit Futures Grid min/max grid count.
- Moved grid-count validation out of the range/step-only branch.
- Replaced permissive arithmetic/geometric validation with arithmetic-only fail-closed validation.
- Error message now explains why geometric is blocked: missing dedicated ratio-level, net-profit and tick-rounding math.

## Frontend/UI changes

No UI layout changes were required in this iteration. The UI already displays `grid_type`, net/grid, fees, funding impact, liquidation buffer, required margin, risk report, warnings and Bybit validation blocks. Since backend now rejects `geometric`, the existing validation card will surface the blocker to the operator.

## Docs/config changes

- `README.md`: clarified that generation and execution preflight allow only `grid_type=arithmetic`.
- `docs/TRADING_LOGIC.md`: corrected grid geometry invariant to arithmetic-only.
- `CHANGELOG.md`: added the hardening entry.

## Tests

Added:

- `test_bybit_preflight_blocks_geometric_until_geometric_math_is_implemented`
- `test_bybit_preflight_validates_grid_count_even_without_complete_trade_plan`

Validation result:

```text
385 passed in 16.56s
```

## Residual risks

- Real Bybit fee tier / VIP maker-taker fees must be configured from account context before production execution.
- Instrument limits must be refreshed live from Bybit immediately before operator confirmation.
- Liquidation price is an approximation; Bybit risk tiers, mark price, wallet margin and maintenance margin can move the real value.
- Slippage model is conservative heuristic, not an order-book simulator.
- Funding uses latest funding rate and interval; historical/forward funding path still needs production data.
- Paper trading / staging is required before live capital.
- Geometric grid remains unsupported until implemented with separate math and tests.

## Commands

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
ruff check app tests main.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
