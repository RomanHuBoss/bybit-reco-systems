# UI detail panel badge fit audit — 2026-05-09

## Scope

Small operator dashboard fix for the recommendation detail panel. The selected recommendation hero used the full product label `Bybit Linear USDT Futures Grid` inside a narrow details column. On medium-width screens it could overlap the metric cards and look truncated.

## Changes

- Kept the full table label unchanged: `Bybit Linear USDT Futures Grid`.
- Changed compact detail/modal badges to display `Linear USDT Grid` while preserving the full label in the `title` attribute.
- Changed the detail subtitle row from `inline-flex` to constrained `flex` so badges, direction and status wrap inside the panel instead of overflowing.
- Added `max-width`, `min-width`, overflow protection and compact badge wrapping rules.
- Bumped static asset cache keys from `manual-ui-v13` to `manual-ui-v14`.

## Product-scope note

The shortened compact label does not relax the product boundary. The dashboard title, table label, helper text and API still enforce Bybit Linear USDT Perpetual `futures_grid` only.

## Validation

- `node --check app/ui/static/app.js`
- `python -m pytest -q tests/test_iteration122_ui_detail_badge_fit.py`
- `python -m pytest -q`
