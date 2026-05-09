# Audit report — Bybit Linear USDT Futures grid hardening, 2026-05-09

## Scope

Project scope remains intentionally narrow: only Bybit Linear USDT Futures / USDT Perpetual `futures_grid` recommendations. Other bot classes and non-linear/non-USDT instruments are unsupported and must fail closed.

## Findings and fixes

| Area | Finding | Risk | Fix | Files |
|---|---|---|---|---|
| Symbol scope | Symbol validation accepted any string ending with `USDT`, including malformed values such as `USDT`, `BTC/USDT` or `BTCUSDT-PERP`. | Bad operator config could reach Bybit REST paths, poison collection scope or create confusing UI/API state. | Added exact alphanumeric `*USDT` validation and settings-level filtering before collection/scoring. | `app/bybit_client.py`, `app/settings.py`, `tests/test_iteration119_linear_perpetual_scope.py` |
| Grid geometry | Generator documented `grid_count` as Bybit Number of Grids / intervals, but range span used `grid_levels - 1`. | Published range could be narrower than implied by interval count; step/range geometry could be inconsistent for manual setup. | Range span now scales with `grid_count` itself; regression covers interval-count semantics. | `app/recommender.py`, `tests/test_grid_linear_economics.py` |
| Runtime risk caps | Runtime limits covered concurrent bots, per-symbol bots and daily drawdown, but not per-bot leverage/notional/margin caps. | A recommendation could pass thesis/economics gates while exceeding operator exposure constraints. | Normalized `max_leverage`, `max_position_notional_usdt` and `max_margin_per_bot_usdt`; recommender blocks candidates exceeding caps. | `app/risk.py`, `app/recommender.py`, `.env.example`, tests |
| Documentation | Risk and module docs did not describe the new symbol strictness or per-bot caps. | Operators could miss why a symbol/candidate is rejected. | README, module docs, trading logic and changelog updated. | `README.md`, `docs/TRADING_LOGIC.md`, `docs/MODULES.md`, `CHANGELOG.md` |

## Trading logic impact

- `futures_grid` remains arithmetic-only and Bybit Linear USDT-only.
- `grid_count` now consistently means number of price intervals across generation, trade plan and preflight.
- Net grid economics still subtract execution cost and expected funding.
- Liquidation checks still use conservative reference and adverse-boundary buffers.
- New caps make the risk layer capable of returning `MAX_LEVERAGE_PER_BOT`, `MAX_POSITION_NOTIONAL_PER_BOT` or `MAX_MARGIN_PER_BOT` before publication/execution.

## Tests

Executed command:

```bash
python -m pytest -q
```

Result:

```text
391 passed in 14.20s
```

New regression coverage:

- malformed Linear USDT symbols are rejected/filtered;
- settings keep only exact symbols such as `BTCUSDT`/`ETHUSDT`;
- arithmetic grid span uses `grid_count` as interval count;
- normalized risk limits include leverage/notional/margin caps.

## Residual risks

- Exact Bybit fees, funding interval, instrument filters and risk tiers must still be refreshed from live Bybit data before execution.
- Approximate liquidation price is not a substitute for Bybit's account/risk-tier liquidation engine.
- Fill sequence, queue priority, partial fills and live slippage remain outside the recommendation engine.
- Paper trading / sandbox execution should be used before production keys.
