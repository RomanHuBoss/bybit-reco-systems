# Audit update — Bybit Linear USDT Futures Grid operator guard

Date: 2026-05-09

## Scope

This update audits the operator-facing recommendation path for the Bybit Linear USDT Futures grid-only product boundary.
The system remains limited to `bot_type=futures_grid`, `venue=linear`, USDT-quoted and USDT-settled perpetual contracts.

## Finding

Execution preflight already rejected recommendations when fresh Bybit instrument metadata could not confirm:

- `category=linear`;
- `contractType=LinearPerpetual`;
- `quoteCoin=USDT` and `settleCoin=USDT`;
- trading status;
- tick size, quantity step, min/max quantity, min notional and leverage filters.

However, the operator API/UI enrichment path could still display an existing database row as `recommended` before the execution action was attempted. This created a misleading operator state: the UI could look actionable while execution preflight would later fail closed.

## Fix

`app/main.py` now computes a strict `bybit_operator_guard` during recommendation enrichment with `require_meta=True` and merges any guard errors into the operator-facing payload:

- actionable statuses `recommended`, `pending`, and `active` become `blocked` when the strict guard fails;
- guard errors are appended to `blocks` with `source=bybit_operator_guard`;
- `params.risk_report.decision` is changed to `not_recommended`;
- rejection reasons are copied into `params.risk_report.rejection_reasons`;
- `reasons.risk_checks.passed` becomes `false`;
- `reasons.decision_layers.bybit_operator_guard` and `final_status` become `blocked`.

Malformed legacy JSON rows that the API deliberately normalizes to empty payloads keep the existing shape-normalized fail-open contract and are not rebuilt into synthetic `params`, `reasons` or `blocks` objects.

## Regression tests

Added `tests/test_iteration121_operator_guard_fail_closed.py`:

1. A `recommended` futures-grid recommendation is shown as `blocked` when Bybit `min_notional` rejects the order sizing.
2. A `recommended` futures-grid recommendation is shown as `blocked` when Bybit instrument metadata is unavailable.

Full test result after the patch:

```text
387 passed in 10.18s
```

## Residual risks

This guard validates metadata and static exchange constraints in the operator view. Live execution still requires the existing execution-time preflight for fresh ticker/candle data, live price drift, market-shock lockdown, slippage, funding freshness and current account constraints.
