# Audit report — UI rank vs launch decision (2026-05-11)

## Problem

The Details panel used the relative UI percentile as if it were the reason for a `no_trade` decision: "общий скор 77/100". This was misleading because the table value is a grouped relative rank among currently visible symbols, while launchability is decided by absolute backend gates: raw launch-score threshold, confidence gate, grid economics, trend/range regime, funding/execution costs, and hard preflight/risk blockers.

## Fix

- Renamed the table column from `Скор UI` to `Ранг`.
- Reworded tooltips to state that the value is a relative rank in the current sample and not launch approval.
- Added a Details card: `Ранг не равен разрешению запуска`.
- Added explicit diagnostics for:
  - relative sample rank;
  - raw launch-score vs threshold;
  - confidence gate vs threshold;
  - decision layers (`thesis_status / execution_status / final_status`).
- Replaced the misleading `общий скор ...` no-trade message with a launch-gate explanation.
- Kept `no_trade` semantics fail-closed: it still means do not launch a Bybit Linear USDT Futures grid now; it is not a technical Bybit/preflight blocker unless hard blockers are present.

## Files

- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `tests/test_iteration128_score_ui_segmentation.py`
- `tests/test_iteration139_ui_no_trade_not_hard_blocker.py`
- `README.md`
- `docs/TRADING_LOGIC.md`

## Test command

```bash
pytest -q tests/test_iteration128_score_ui_segmentation.py tests/test_iteration139_ui_no_trade_not_hard_blocker.py
```
