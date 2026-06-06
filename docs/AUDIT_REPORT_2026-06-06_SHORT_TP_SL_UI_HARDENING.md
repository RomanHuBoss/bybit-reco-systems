# Audit report — short TP/SL UI hardening

## Finding

The compact operator details panel used the same exit mapping for every direction:

- Take Profit = upper kill-switch;
- Stop Loss = lower kill-switch.

That mapping is correct only for long-biased grids. For short-biased Futures Grid it reverses the economics: the profitable terminal exit is below the range, while the adverse stop is above the range.

## Fix

`app/ui/static/app.js` now resolves exit levels through a direction-aware helper:

- `long`: TP = upper kill-switch, SL = lower kill-switch;
- `short`: TP = lower kill-switch, SL = upper kill-switch;
- `neutral`: no directional TP is shown; the UI shows both kill-switch boundaries as the stop/kill-switch control.

The field labels in the details panel now use the helper output instead of hard-coded one-sided labels.

## Regression coverage

Added `tests/test_iteration147_short_tp_sl_ui_hardening.py` to assert that short/long TP-SL geometry remains side-aware, that the old hard-coded mapping does not return, and that generated long/short grid ranges keep the intended profit-side asymmetry around the reference price.

## Cache key

Static UI cache key bumped to `manual-ui-v25` so browsers reload the corrected JavaScript.

## Validation

- `node --check app/ui/static/app.js`
- `python -m compileall -q app tests main.py`
- `pytest -q` → 481 passed
