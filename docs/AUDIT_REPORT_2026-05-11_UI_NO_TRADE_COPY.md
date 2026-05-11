# UI no_trade copy clarification — 2026-05-11

## Summary

The Details panel incorrectly treated every `no_trade` recommendation as a hard launch blocker. In the observed SUIUSDT payload, there were no explicit `blocks`, no Bybit validation errors and no `risk_report.rejection_reasons`; the recommendation was `no_trade` because the aggregate score/risk profile was weak and warnings were present.

## Risk

The UI copy said that a blocker existed even when no hard blocker existed. This made operator diagnostics confusing and blurred the distinction between:

- explicit blockers: Bybit/preflight errors, backend `blocks`, risk rejection reasons, `blocked` status;
- no-trade decision: not recommended by scoring/risk warnings, but not a technical preflight blocker.

## Changes

- Split UI state into `explicitHardBlocked` and `noTradeDecision`.
- `no_trade` no longer triggers the hard-blocker text by itself.
- For `no_trade` without explicit blockers, the Details panel now shows: “Не запускать сейчас”.
- The explanation now says that `no_trade` is a scoring/risk refusal, not a technical blocker.
- Added a synthetic `NO_TRADE` reason with UI score/grade for clarity.
- Renamed warning-only card title to `Причины no_trade / предупреждения`.
- Added a separate warning-style card border instead of hard blocker styling for warning-only `no_trade` rows.
- Bumped static asset cache key to `manual-ui-v21`.

## Tests

- Added `tests/test_iteration139_ui_no_trade_not_hard_blocker.py`.
- Updated existing static UI cache-key tests.

Validation:

```bash
python -m pytest -q
# 453 passed

PYTHONDONTWRITEBYTECODE=1 python -m compileall -q app main.py
# passed
```
