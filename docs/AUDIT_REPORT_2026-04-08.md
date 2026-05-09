# Audit Report 2026-04-08

## Scope

Red-team audit of the Bybit Linear USDT Futures grid-only recommender release lineage.

## Findings

- Проверены границы продукта: система должна публиковать только `futures_grid` для `venue=linear`.
- Проверены execution preflight gates: market-data freshness, live-price drift, market shock, fast-veto and basic Bybit metadata checks.
- Подтверждено, что unsupported payloads должны блокироваться fail-closed rather than normalized silently.

## Fixes

- Added regression coverage for exact-symbol instrument metadata and Bybit metadata mismatch handling.
- Added release-integrity documentation checks so README/CHANGELOG cannot reference missing audit artifacts.

## Риски

- Реальная биржевая ликвидация зависит от risk tier, позиции, маржи аккаунта и maintenance margin.
- Backtest/proxy-outcome remains an approximation and cannot guarantee future fills, fees, slippage or funding.
- Production execution still requires staging/paper-trading validation with live Bybit instrument filters.
