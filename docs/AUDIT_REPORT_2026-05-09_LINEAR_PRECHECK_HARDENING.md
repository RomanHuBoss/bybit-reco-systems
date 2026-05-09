# Audit report — Bybit Linear USDT Futures grid-only hardening, 2026-05-09

## Scope

Audited the uploaded project as a Bybit Linear USDT Futures / USDT Perpetual grid-only recommendation system. The reviewed scope covered backend trading math, Bybit metadata validation, execution preflight, recommendation blocking logic, static operator UI, markdown documentation and regression tests.

## Project map

| Layer | Files | Responsibility |
|---|---|---|
| Product boundary | `app/bot_types.py`, `app/settings.py` | Single supported `bot_type=futures_grid`; bootstrap filters symbols to `*USDT`; allowed venue is `linear`. |
| Bybit public data | `app/bybit_client.py`, `app/collector.py` | V5 tickers, klines, instrument metadata, funding, open interest. |
| Market features | `app/features.py`, `app/direction.py`, `app/regime.py`, `app/sentiment_features.py` | ATR, realized volatility proxy, spread, liquidity tier, MTF direction, market regime, sentiment blend. |
| Grid economics | `app/grid_math.py`, `app/recommender.py` | Decimal helpers for linear PnL, fees, funding cashflow, margin, estimated liquidation buffer, net grid economics. |
| Risk gates | `app/risk.py`, `app/shock_guard.py`, `app/recommender.py` | Runtime risk limits, market shock veto, fast veto, recommendation blocking/no-trade logic. |
| API / execution preflight | `app/main.py` | Recommendation API, operator actions, live-price guard, Bybit instrument filter validation, bot/trade audit lifecycle. |
| Persistence | `app/db.py`, `app/db_backend.py`, `migrations/*.sql` | SQLite/Postgres schema, recommendation/audit/outcome/calibration storage. |
| UI | `app/ui/static/*` | Operator dashboard and details card for grid parameters, risk report, Bybit validation, warnings. |
| Tests | `tests/*` | Unit/regression/scenario coverage for economics, API lifecycle, preflight, calibration, outcomes, resilience. |
| Docs/config | `README.md`, `CHANGELOG.md`, `docs/*.md`, `.env.example` | Product scope, launch commands, operator guidance, known risks. |

## Critical findings and fixes

| Area | Issue | Risk | Fix | Files |
|---|---|---|---|---|
| Bybit instrument metadata | Preflight could proceed when metadata existed but essential filters were missing. | Prices/qty/notional/leverage could be validated incompletely, causing unsafe operator execution. | Added mandatory filter checks for `tickSize`, `qtyStep`, `minOrderQty`, `maxOrderQty`, `minNotionalValue`, `min/max/leverageStep`. In strict execution mode they block; in details mode they warn. | `app/main.py`, `tests/test_iteration117_grid_only_strict_preflight.py` |
| Perpetual-only boundary | Linear delivery futures could be indistinguishable if only category was checked. | Non-perpetual futures might pass a grid recommendation intended only for USDT perpetual. | Added `delivery_time` capture and `BYBIT_DELIVERY_TIME_NOT_PERPETUAL` blocker. | `app/main.py`, `tests/test_iteration117_grid_only_strict_preflight.py` |
| Category mismatch detection | Bybit `result.category` was not preserved on returned instrument item. | A malformed/stubbed response could hide category mismatch from downstream validation. | Public client now propagates `result.category` into the instrument dict when item category is absent. | `app/bybit_client.py`, `tests/test_iteration112_redteam_integrity_and_bybit_meta.py` |
| Legacy leverage ambiguity | Manual/legacy recommendation without `leverage` did not make the assumption explicit. | Operator could think leverage was validated when it was absent. | Added `LEVERAGE_DEFAULTED_TO_ONE` warning; strict generated recommendations continue to include explicit leverage and liquidation economics. | `app/main.py`, `tests/test_iteration117_grid_only_strict_preflight.py` |

## Trading-logic status after audit

- Grid product scope remains single-mode: `futures_grid` on Bybit `category=linear`, USDT settlement/quote.
- Linear PnL helpers use `qty * (exit - entry)` for long and `qty * (entry - exit)` for short. Unknown side returns zero fail-closed.
- Grid economics are net of fees/spread/slippage/funding; non-positive or too-thin net profit per grid is blocked.
- Funding impact is event-counted and direction-aware; neutral grids use adverse-side funding cost.
- Liquidation buffer is estimated for linear isolated mode and uses the worst of reference price and adverse kill-switch boundary.
- Recommendation logic can return `blocked`, `no_trade`, `pending`, or `suppressed`; it is not an always-recommend engine.

## Backend/API changes

- Strengthened `_validate_trade_plan_against_bybit_meta()` with mandatory exchange-filter checks.
- Added `delivery_time` to `_fetch_bybit_instrument_meta()` normalized metadata.
- Added explicit legacy leverage warning.
- Strengthened `BybitPublicClient.get_instrument_info()` to preserve `result.category`.

## Frontend/UI status

No static UI code change was required in this pass. The existing UI already renders `bybit_plan_validation.errors/warnings`, `params.risk_report`, `net/grid`, funding, liquidation buffer, required margin, status and blocking reasons. The backend now sends stronger validation warnings/errors into the same UI path.

## Docs/config changes

- Updated `README.md` to describe mandatory Bybit filters, delivery-contract rejection, category propagation and legacy leverage handling.
- Updated `CHANGELOG.md` with this hardening pass.
- Added this audit report.

## Tests

Added/updated regression coverage for:

- mandatory Bybit filter absence in strict and details modes;
- linear delivery contract rejection;
- legacy missing leverage warning;
- Bybit `result.category` propagation.

Verified commands:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_api.py tests/test_iteration68.py tests/test_iteration92_json_shape_hardening.py tests/test_iteration96_runtime_and_payload_hardening.py tests/test_iteration101_resilience_hardening.py tests/test_iteration117_grid_only_strict_preflight.py tests/test_iteration112_redteam_integrity_and_bybit_meta.py
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_grid_linear_economics.py tests/test_logic.py tests/test_sentiment_pipeline.py tests/test_iteration100_outcome_cost_components.py tests/test_iteration102_shutdown_and_llm_rank_hardening.py tests/test_iteration103_settings_and_docs_consistency.py tests/test_iteration104_security_and_transport_hardening.py tests/test_iteration105_execution_preflight_and_bybit_validation.py tests/test_iteration106_grid_tp_success_semantics.py tests/test_iteration107_execution_and_validation_hardening.py tests/test_iteration108_outcome_queue_and_docs_audit.py tests/test_iteration109_postgres_support.py tests/test_iteration110_postgres_lock_and_publication_root_safety.py tests/test_iteration111_postgres_row_locking_and_release_artifacts.py tests/test_iteration113_startup_backfill_scaling.py tests/test_iteration114_live_price_and_status_guards.py tests/test_iteration115_order_sizing_validation.py tests/test_iteration116_linear_usdt_fail_closed.py
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_iteration63.py tests/test_iteration65.py tests/test_iteration66.py tests/test_iteration67.py tests/test_iteration69.py tests/test_iteration70.py tests/test_iteration71.py tests/test_iteration72.py tests/test_iteration73.py tests/test_iteration74_runtime_locks.py tests/test_iteration75_runtime_heartbeat.py tests/test_iteration76.py tests/test_iteration77.py tests/test_iteration78.py tests/test_iteration79.py tests/test_iteration80.py tests/test_iteration81_regression.py tests/test_iteration82_publish_atomicity.py tests/test_iteration83_db_write_retries.py tests/test_iteration84_alerts_and_security.py tests/test_iteration85_integrity_and_sanitization.py tests/test_iteration86_atomicity_and_backfill.py tests/test_iteration87_publication_lineage.py tests/test_iteration88_calibration_guardrails.py tests/test_iteration89_env_and_docs_integrity.py tests/test_iteration90_audit_hardening.py tests/test_iteration91_sentiment_release_hardening.py tests/test_iteration93_outcomes_and_feature_hardening.py tests/test_iteration94_risk_limits_and_outcome_bounds.py tests/test_iteration95_bybit_client_and_alert_transport.py tests/test_iteration97_json_loader_and_transport_hardening.py tests/test_iteration98_release_artifact_integrity.py tests/test_iteration99_config_and_llm_guardrails.py
PYTHONDONTWRITEBYTECODE=1 python -m py_compile app/*.py main.py
```

Result: `374 passed` across split pytest runs; `py_compile` passed. `ruff` was not available in the container.

## Residual risks

- Real fee tier must be supplied for the operator account; fallback taker-fee bps is conservative but not account-specific.
- Bybit instrument limits are time-varying; execution preflight must fetch live metadata before launch.
- Slippage model remains heuristic and should be calibrated from real fills or paper-trading logs.
- Funding history is not a full forecast; extreme carry can still change after recommendation publication.
- Exact liquidation price depends on account state, risk tier, maintenance margin, mark price and wallet balance; project estimate is a safety gate, not the exchange truth.
- The project remains an operator recommendation/audit layer, not a production OMS/EMS.
