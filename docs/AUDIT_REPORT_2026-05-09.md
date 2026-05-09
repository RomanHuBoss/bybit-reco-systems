# Audit Report 2026-05-09

## Scope

Current audit pass over the archived project for grid-only domain enforcement, Bybit metadata handling, funding interval use and release artifact integrity.

## Findings

- Product boundary is grid-only: `futures_grid` on Bybit `category=linear`, USDT-quoted and USDT-settled perpetual contracts.
- Execution preflight must fail closed if Bybit metadata does not confirm contract type, quote coin, settle coin, status, tick/lot filters and leverage bounds.
- Funding interval must not be assumed universally; the actual interval should be preserved when Bybit returns it.
- Release documentation referenced historical audit reports that were absent from the archive.

## Fixes

- Restored audit report artifacts referenced by README, CHANGELOG and release-integrity tests.
- Added/kept strict fail-closed validation for missing Bybit contract/USDT metadata at execution time.
- Added/kept funding interval propagation and material-funding fail-closed behavior.
- Confirmed unsupported strategy families are not exposed as supported product modes.
- Added a further strict preflight hardening pass: unsupported `bot_type`/non-linear `venue` are rejected directly, and execution-mode off-tick prices/steps/TP are errors rather than soft warnings.

## Риски

- Real Bybit fees, tick/lot filters, leverage filters and funding intervals must be re-read from live exchange metadata before production launch.
- Live fills, partial fills, spread and slippage are not simulated as exchange truth.
- The system remains a recommender/operator audit loop, not a production exchange execution engine.
- Paper trading/staging is required before any real-capital use.
