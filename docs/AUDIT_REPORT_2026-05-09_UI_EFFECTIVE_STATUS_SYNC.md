# UI effective status sync audit — 2026-05-09

## Problem

The recommendation table used the persisted database status returned by `/api/v1/recommendations`, while the detail card used `/api/v1/recommendations/{rec_id}` and applied the live Bybit operator guard. A row could therefore appear as `active` in the table, but open as `blocked` in the detail card for the same `rec_id`.

## Risk

This was an operator-facing safety bug: a dynamically blocked recommendation could look actionable in the left table even though the execution/detail path correctly failed closed.

## Fix

- The list endpoint now applies the same `_augment_reco_for_ui()` Bybit metadata/operator guard used by the detail endpoint.
- The table filters on the effective status after augmentation, not only on the persisted DB status.
- A recommendation changed to `blocked` by current Bybit guard is removed from the default `recommended+active` view and appears only when the `blocked` filter is enabled.
- `no_trade` is calculated from effective operator-facing statuses.
- A regression test confirms that the list and detail card return the same effective `blocked` status and blocking codes.

## Files

- `app/main.py`
- `tests/test_iteration123_ui_effective_status_sync.py`
