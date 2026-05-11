# Audit report — Bybit Linear USDT Futures Grid fail-closed fixes, 2026-05-11

Scope: recommender/operator UI/execution preflight for **only** Bybit Linear USDT Perpetual futures-grid recommendations.

## Summary

The re-audit found several fail-open defects where a recommendation could remain visually actionable even when the system could not prove that it had a complete executable trade plan or valid current Bybit instrument metadata.

The highest-risk defects were corrected in backend validation, operator-facing API augmentation, UI launch gating, and regression tests.

## Critical defects fixed

| Area | Defect | Risk | Fix | Files |
|---|---|---|---|---|
| Bybit metadata | `category` and `symbol` missing from upstream metadata were silently replaced with requested values. | Downstream validation could not detect malformed/stale/mismatched metadata. | Preserve missing raw metadata and let strict preflight block with `BYBIT_META_CATEGORY_MISSING` / `BYBIT_META_SYMBOL_MISSING`. | `app/main.py` |
| Bybit trading status | Missing `status` was not fail-closed. Only explicit non-Trading status was blocked. | A contract with unverified current trading status could look executable. | Strict metadata validation now blocks missing status with `BYBIT_STATUS_MISSING`. | `app/main.py` |
| Operator guard | Operator-facing guard did not require `require_execution_plan=True`. | Rows with incomplete params could be shown as actionable until a later execution-path failure. | Operator guard now requires complete execution trade plan when params exist. | `app/main.py` |
| Empty/corrupt params | Empty `params_json` received an OK operator guard with a warning. | UI/API could present a recommendation without a verifiable executable grid. | Empty params now get `bybit_operator_guard.ok=false` and `PAYLOAD_UNAVAILABLE_FOR_OPERATOR_GUARD`, while legacy malformed JSON shape remains stable. | `app/main.py` |
| Missing trade plan | Validation only produced field-level errors and could lose the explicit cause after snapping created an empty shell. | Operators/debuggers saw many missing subfields but not the root cause. | Added explicit `TRADE_PLAN_MISSING` detection for effectively missing plans. | `app/main.py` |
| Frontend launchability | Launch link logic accepted rows when validation was absent or only the details validation had no errors. | UI could show a launch action for rows without strict operator guard proof. | Launch gate now requires complete `params.trade_plan`, `risk_report.decision=recommended`, non-pending LLM review, `bybit_operator_guard.ok=true`, `meta_checked=true`, and zero errors. | `app/ui/static/app.js` |
| Regression coverage | No focused tests covered incomplete operator plan and malformed metadata fail-closed behavior. | Future edits could reintroduce unsafe active/recommended states. | Added regression tests for missing trade plan, empty params guard, missing category/symbol/status, fetch preservation, and UI launch gate. | `tests/test_iteration144_prompt_audit_fail_closed.py` |

## Validation semantics after fix

- Recommended/active rows with incomplete executable grid plans are blocked at operator guard before execution.
- Rows with malformed legacy JSON keep normalized empty shapes for compatibility, but are explicitly non-launchable through `bybit_operator_guard.ok=false` and the frontend launch gate.
- Strict metadata validation blocks if Bybit metadata cannot prove LinearPerpetual, USDT quote/settlement, trading status, tick/lot/min-notional/leverage filters.
- Unsupported grid type remains blocked. Missing explicit grid type in legacy arithmetic payloads is tolerated when the product mode is already `futures_grid`, to avoid breaking existing execution-safe legacy fixtures.

## Tests

Executed in split batches because a single full-suite run exceeded the sandbox timeout. All collected tests were covered by the split runs.

- `python -m pytest -q tests/test_iteration107_execution_and_validation_hardening.py tests/test_iteration121_operator_guard_fail_closed.py tests/test_iteration117_grid_only_strict_preflight.py tests/test_iteration143_llm_verdict_required.py tests/test_iteration92_json_shape_hardening.py tests/test_iteration144_prompt_audit_fail_closed.py` → 38 passed
- `python -m pytest -q tests/test_iteration108_outcome_queue_and_docs_audit.py ... tests/test_iteration124_prompt_reaudit.py` → 52 passed
- `python -m pytest -q tests/test_iteration125_execution_funding_and_scope_hardening.py ... tests/test_iteration65.py` → 68 passed
- `python -m pytest -q tests/test_iteration66.py ... tests/test_iteration85_integrity_and_sanitization.py` → 79 passed
- `python -m pytest -q tests/test_iteration86_atomicity_and_backfill.py ... tests/test_sentiment_pipeline.py` → 147 passed
- `python -m pytest -q tests/test_api.py tests/test_grid_linear_economics.py ... tests/test_iteration106_grid_tp_success_semantics.py` → 85 passed

Total split-suite coverage: **469 passed**.

A single `python -m pytest -q` run was also attempted and showed no failures before sandbox timeout.

## Residual risks

- Real Bybit fees/funding/instrument filters must still be validated in production with fresh public API data.
- Funding history and live execution behavior may differ under exchange load and latency.
- Slippage and partial fills remain estimates until paper/live trading telemetry is compared against fills.
- Production API key permissions, account mode, and isolated margin settings still require environment-level verification.
