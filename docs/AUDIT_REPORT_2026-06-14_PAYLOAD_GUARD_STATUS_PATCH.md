# Audit report: Bybit Linear USDT operator payload guard status patch

Date: 2026-06-14  
Scope: futures_grid recommendations for Bybit V5 Linear USDT Perpetual, operator UI/API payloads, long/short TP/SL guard path, risk fail-closed behavior, and `how_to_trade.png` operator infographic.

## Executive summary

A focused re-audit was performed around trading semantics, TP/SL display, Bybit execution metadata, risk preflight and operator-facing UI status. The existing codebase already contains a centralized directional model in `app/trading_semantics.py` and extensive regression tests for long/short TP/SL, PnL, risk/reward, grid ranges, Bybit snapping and UI consistency.

One high-severity operator-facing gap was found and fixed: a legacy/corrupted recommendation row with empty `params` could still retain an actionable-looking `status=recommended` even though the Bybit operator guard correctly blocked execution because there was no complete executable `params.trade_plan`. This did not create a direct order-sending path in this project, but it could mislead list banners, status badges and effective-status filters.

The patch makes empty/corrupted actionable payloads fail closed as `status=blocked` / `effective_status=blocked` while preserving the original `stored_status` for auditability. Documentation and the root infographic were synchronized to require a complete `params.trade_plan` before any operator action. The older malformed-JSON API regression was also updated from fail-open display semantics to fail-closed display semantics.

## Audit coverage

### Trading semantics and TP/SL

Reviewed areas:

- canonical long/short model in `app/trading_semantics.py`;
- directional exit payload assembly in `app/main.py`;
- Bybit protective order trigger-side and reduce-only/close-only semantics;
- UI display path in `app/ui/static/app.js`;
- existing regression tests for long/short TP/SL, short-side UI, risk/reward, PnL and guard behavior.

Observed baseline:

- Long TP/SL semantics are direction-aware: TP above reference/entry, SL below reference/entry.
- Short TP/SL semantics are direction-aware: TP below reference/entry, SL above reference/entry.
- Neutral grid payloads avoid directional TP/SL where a directional interpretation is mathematically inappropriate.
- Protective order checks reject geometry that could increase exposure or trigger on the wrong side.

No new TP/SL inversion defect was found in the currently inspected code path.

### Bybit-specific and risk-management coverage

Reviewed areas:

- instrument metadata gates: `category`, `contractType`, `quoteCoin`, `settleCoin`, `status`, `tickSize`, `qtyStep`, `minQty`, `minNotional`;
- snapping/validation path before exposing launchable recommendations;
- fail-closed operator guard for missing metadata, missing trade plans, invalid price input and unsupported payloads;
- launchability separation from visual status.

The issue fixed in this patch belongs to the final operator-display layer: guard was blocked, but status could still look actionable for legacy empty-payload rows.

## Findings and fixes

### HIGH — Empty/corrupted trade-plan payload could remain visually `recommended`

- **Files:** `app/main.py`, `tests/test_iteration163_payload_guard_status.py`
- **Affected path:** `_augment_reco_for_ui()` branch for missing or empty `params`.
- **Problem:** The code correctly created a critical `bybit_operator_guard` error for empty/corrupted payloads, including `PAYLOAD_UNAVAILABLE_FOR_OPERATOR_GUARD` and `TRADE_PLAN_MISSING`. However, because `_merge_bybit_operator_guard_into_ui_payload()` intentionally preserves empty legacy `params/reasons/blocks` shape, the row could retain `status=recommended` or similar actionable status.
- **Financial/trading risk:** An operator could see a row as recommended/active in summaries or filters even though it had no complete executable `params.trade_plan`. The project still did not send orders automatically and launchability remained blocked, but the mismatch weakened the fail-closed UI contract.
- **Fix:** Empty/corrupted payloads that arrive with `recommended`, `active` or `pending` are now converted to `status=blocked` and `effective_status=blocked`; original actionable state is copied to `stored_status` for audit traceability.
- **Code change:** `app/main.py:1035-1041`.
- **Test:** `test_empty_params_recommended_row_is_effectively_blocked_without_rebuilding_legacy_payload()`.

### LOW — Operator source text and infographic did not explicitly state that `params.trade_plan` must exist

- **Files:** `docs/HOW_TO_TRADE_INFOGRAPHIC.md`, `how_to_trade.png`, `tests/test_iteration163_payload_guard_status.py`
- **Problem:** The operator checklist required correct bot type, venue, symbol, account mode, grid type, live price and price validity, but did not explicitly require a complete `params.trade_plan` payload.
- **Risk:** Documentation gap: an operator or future contributor could interpret a recommendation row as actionable based only on status and metadata, without checking that an executable trade plan is present.
- **Fix:** Added `Complete params.trade_plan exists; no empty/corrupted payload.` to the source text and regenerated the root infographic with the same guard requirement.
- **Test:** `test_how_to_trade_source_mentions_complete_trade_plan_payload()`.

## Tests added

- `tests/test_iteration163_payload_guard_status.py`
  - `test_empty_params_recommended_row_is_effectively_blocked_without_rebuilding_legacy_payload`
  - `test_how_to_trade_source_mentions_complete_trade_plan_payload`
- `tests/test_iteration92_json_shape_hardening.py`
  - `test_api_recommendations_and_details_fail_closed_on_malformed_json_shapes`

These tests lock the fail-closed contract for legacy/corrupted payloads and keep the operator infographic source synchronized with the new rule.

## Verification performed

Commands executed in this environment:

```bash
python3 -m compileall -q app tests
node --check app/ui/static/app.js
PYTHONPATH=. pytest -q tests/test_iteration163_payload_guard_status.py tests/test_iteration144_prompt_audit_fail_closed.py tests/test_iteration92_json_shape_hardening.py
PYTHONPATH=. pytest -vv
```

Results:

- Python compileall: passed.
- JavaScript syntax check: passed.
- Targeted regression tests: passed, 11 tests.
- Full pytest suite: passed, 574 tests.

Checks not completed:

- `ruff check app tests`: not executed successfully because `ruff` is not installed in this environment.
- npm/yarn tests: no `package.json` or configured Node test suite found in the project root.
- Live/testnet Bybit API integration: not executed because no real credentials/exchange environment were provided; this audit is limited to offline static/unit/integration behavior available from the archive.

## Residual risks

- Exact exchange liquidation and risk-tier behavior can differ from local estimates and must be validated against Bybit account/instrument risk tiers in the target environment.
- Real order lifecycle cases such as exchange-side partial fill, late cancel, rate limits, API downtime and reconciliation with open positions require testnet/live integration evidence.
- Any future UI component that bypasses `_augment_reco_for_ui()` must independently preserve the same fail-closed status contract.
- The project remains a recommendation/audit service rather than a complete OMS/EMS; manual operator discipline and exchange-side verification remain required.

## Changed files

- `app/main.py`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- `how_to_trade.png`
- `tests/test_iteration163_payload_guard_status.py`
- `tests/test_iteration92_json_shape_hardening.py`
- `docs/AUDIT_REPORT_2026-06-14_PAYLOAD_GUARD_STATUS_PATCH.md`
