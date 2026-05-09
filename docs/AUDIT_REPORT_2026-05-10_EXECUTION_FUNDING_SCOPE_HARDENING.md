# Audit report — execution-time funding and strict Linear USDT scope hardening, 2026-05-10

## Scope

Повторный аудит проекта как рекомендательной системы **только** для Bybit Linear USDT Futures / USDT Perpetual grid-ботов. Проверялись backend, frontend contract, торговая математика, risk gates, Bybit instrument metadata, funding, liquidation buffer, docs and tests.

## Project map

- Market data / Bybit integration: `app/bybit_client.py`, `app/collector.py`, `app/features.py`.
- Grid math / economics: `app/grid_math.py`, `app/recommender.py`, `app/risk.py`, `app/outcomes.py`.
- API / operator execution path: `app/main.py`, `app/db.py`, `app/db_backend.py`, `migrations/*`.
- Frontend: `app/ui/static/index.html`, `app/ui/static/app.js`, `app/ui/static/styles.css`.
- Shared product boundary: `app/bot_types.py`, `.env.example`, README and docs.
- Tests: `tests/`, including Bybit validation, grid-only scope, funding approval edge, UI status sync and execution preflight suites.

## Critical findings

| Area | Finding | Risk | Fix | Files |
|---|---|---|---|---|
| Execution funding | A costed recommendation could be executed after funding moved materially against the grid edge. | Bot instance could be materialised from stale carry assumptions; net profit per grid could become non-positive. | Added execution-time funding preflight: missing/stale rate, missing interval, extreme carry and funding-edge inversion now block execution. | `app/main.py`, `tests/test_iteration125_execution_funding_and_scope_hardening.py` |
| Symbol scope | Legacy/manual payloads could pass the simple `endswith("USDT")` symbol check despite separators or malformed base. | Non-Bybit or malformed symbol could reach later layers with misleading validation messages. | Added exact alphanumeric USDT perpetual symbol validation for futures grid payloads. | `app/main.py`, tests |
| Pre-listing metadata | `is_pre_listing` was recognised only as boolean `true`, not string booleans that can appear in stubs/proxies. | Pre-market / pre-listing contracts could be under-blocked in edge payloads. | Added boolish upstream flag parsing for `is_pre_listing`. | `app/main.py`, tests |

## Trading logic review

The existing implementation already used Decimal-based helpers for core linear USDT grid economics, including linear PnL, round-trip fees, funding cashflow, margin requirement, approximate liquidation and liquidation buffer checks. The audit preserved the arithmetic-only futures-grid boundary and added a missing execution-time check: funding is now re-evaluated at operator confirmation for recommendations that contain a full `cost_model`.

Approval/rejection behavior remains fail-closed for unsupported bot types, non-linear venue, non-USDT settlement/quote metadata, missing Bybit filters, geometric grid, off-tick prices, insufficient net profit, narrow liquidation buffer, stale market data and live-price drift. The new funding preflight specifically prevents “recommendation was safe earlier, but carry changed before execution” failures.

## Backend changes

- `app/main.py`
  - Added `EXECUTION_FUNDING_MAX_STALENESS_SEC`, `EXECUTION_FUNDING_WORSE_DELTA_BLOCK_BPS`, `EXECUTION_FUNDING_EXTREME_BPS`.
  - Added `_execution_funding_blocks()` and funding helper functions.
  - Hooked funding preflight into `_execution_preflight()`.
  - Added strict `_is_exact_linear_usdt_symbol()` validation.
  - Added `_boolish_true()` for robust pre-listing flag handling.

## Frontend / UI review

No UI component required structural changes in this iteration. The current operator UI already exposes recommended / not recommended states, risk report, Bybit plan validation, funding labels, net-of-fees grid economics, liquidation buffer and execution blocks. The new execution-time funding block codes are returned through the existing API preflight block surface and therefore appear in the same error/blocked-path UI.

## Docs / config changes

- `README.md` documents the execution-time funding preflight and new block codes.
- `docs/KNOWN_RISKS.md` now states that full costed recommendations are rechecked against a fresh funding snapshot before execution.
- `CHANGELOG.md` records this hardening revision.

## Tests

Added `tests/test_iteration125_execution_funding_and_scope_hardening.py`:

- malformed legacy symbol is rejected;
- string `is_pre_listing="true"` blocks production execution;
- missing funding blocks execution;
- stale funding blocks execution;
- worsened funding that destroys net edge blocks execution;
- fresh low funding is allowed.

Result: `pytest -q` → `406 passed`.

## Residual risks

- Real Bybit fee tiers must be checked against the production account.
- Instrument limits can change and must continue to be fetched dynamically.
- This remains a recommendation/operator-audit system, not a live OMS/EMS.
- Live slippage, partial fills, funding history and liquidation should be reconciled by the external execution layer.
- Production API keys, order previews and paper-trading/live execution need separate validation.
