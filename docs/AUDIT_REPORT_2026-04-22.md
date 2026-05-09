# Audit Report 2026-04-22

## Scope

Deep audit of publication-chain idempotency, bot lifecycle materialization, trade ingestion and rollback safety.

## Findings

- Duplicate execution attempts must collapse by publication root and never create multiple running bots for one active recommendation chain.
- Trade ingestion must be idempotent and safe under duplicate-key races.
- Audit trail should preserve operator actions without masking failed state transitions.

## Fixes

- Hardened transaction rollback and savepoint-safe duplicate classification.
- Added regression tests for idempotent execute/stop and duplicate trade no-op behavior.
- Updated docs with operational boundaries and release artifacts.

## Риски

- The bot lifecycle tables are audit-state, not live exchange order state.
- External executor reconciliation is required before trusting balances, fills or realized PnL.
