# Audit Report — UI pending details no_trade disambiguation (2026-05-11)

## Problem

Operator details could display a `pending` recommendation as `Не запускать сейчас` with a synthetic `NO_TRADE` reason when `params.risk_report.decision=not_recommended` was present.

This was misleading because async LLM-review intentionally parks actionable rows in `pending` and can keep the risk report conservative until the reviewer finalizes the recommendation. In that state the row is not launchable, but it is also not a score/risk `no_trade` decision.

## Fix

- Detail-card decision logic now normalizes the persisted row status once and treats only `status=no_trade` as the no-trade score/risk branch.
- `status=pending` has its own wait branch: `Ждать LLM-review`.
- The synthetic `NO_TRADE` warning item is now gated only by `noTradeDecision && !explicitHardBlocked`, where `noTradeDecision` is derived from persisted status, not from `risk_report.decision`.
- Static UI cache key bumped from `manual-ui-v21` to `manual-ui-v22`.

## Why this is correct

`risk_report.decision=not_recommended` is a conservative backend field reused by multiple non-launchable states. The operator-facing decision card must follow the persisted operator status after all effective-status augmentation, otherwise `pending` rows look like rejected rows.

## Regression coverage

Added `tests/test_iteration140_ui_pending_not_no_trade.py` to assert that:

- the details fragment does not use `riskReport.decision === "not_recommended"` to set the no-trade UI branch;
- `pending` has its own detail title/copy;
- pending does not emit the synthetic `NO_TRADE` reason.

Updated cache-key expectations from `manual-ui-v21` to `manual-ui-v22` across UI regression tests.

## Validation

- `node --check app/ui/static/app.js`
- `python -m compileall -q app tests main.py`
- `pytest -q` → `455 passed`
