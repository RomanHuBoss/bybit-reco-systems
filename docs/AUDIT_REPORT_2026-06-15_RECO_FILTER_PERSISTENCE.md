# AUDIT REPORT 2026-06-15 — Recommendation status filter persistence

## Scope

Focused addendum to the deep audit prompt: UI state restoration for recommendation status filters in `app/ui/static/app.js`, specifically the `pending` filter not being restored after a full page reload when it had been selected before reload.

## Baseline

- `python -m compileall -q app tests main.py`: PASS
- `node --check app/ui/static/app.js`: PASS
- `pytest -q`: 686 passed

## Finding

### MEDIUM — Recommendation status filters were not persisted across full page reload

- File: `app/ui/static/app.js`
- Original affected areas:
  - filter values were read directly from DOM checkboxes inside `loadRecommendations()`;
  - filter change listeners only called `refreshAll()`;
  - boot sequence immediately called `refreshAll()` without restoring any previous checkbox state.

### Root cause

The UI never wrote recommendation status filter state to persistent browser storage and never restored it before the initial `/api/v1/recommendations` fetch. After a full page reload, the controls fell back to HTML defaults from `index.html`: only `showRecommended` is checked by default; `showPending`, `showBlocked`, `showNoTrade`, and `showSuppressed` are unchecked.

This made the first post-reload request use `show_pending=false`, so the visual selected state of `pending` was lost and pending rows were excluded unless the existing diagnostics auto-expand path happened to trigger. That auto-expand path is conditional and cannot be treated as state restoration.

## Risk

Financial/trading risk: indirect operator-risk. The trading backend remains fail-closed, but the operator dashboard can hide `pending` recommendations after reload despite the operator previously enabling that diagnostic/status view. This may lead to wrong situational awareness during review of LLM-held recommendations.

## Fix

- Added `RECO_FILTER_STORAGE_KEY` and `RECO_FILTER_IDS` in `app/ui/static/app.js`.
- Added `getRecommendationFilterState()`, `applyRecommendationFilterState()`, `restoreRecommendationFilterState()`, and `persistRecommendationFilterState()`.
- Changed recommendation-status checkbox handlers to persist state before refreshing data.
- Restored persisted filter state before the initial `refreshAll()` boot call, so the first post-reload API request uses the restored `show_pending=true` state.
- Wrapped `localStorage` access in `try/catch`; if storage is unavailable/corrupt, the UI keeps the safe HTML defaults.
- Bumped static asset cache key from `manual-ui-v38` to `manual-ui-v39` in `app/ui/static/index.html`.

## Tests added / updated

### Added

- `tests/test_iteration176_reco_filter_persistence.py`
  - Verifies `showPending` participates in the persisted filter state.
  - Verifies restore happens before initial `refreshAll()`.
  - Verifies changes are persisted before refresh.
  - Verifies static cache key is bumped to `manual-ui-v39`.

These assertions are red→green for the reported defect: before the patch the storage key, restore function, persist function, and pre-refresh restore call did not exist.

### Updated

- Existing static asset cache-key tests were updated from `manual-ui-v38` to `manual-ui-v39` so the full suite reflects the new shipped asset version.

## Post-checks

- `python -m compileall -q app tests main.py`: PASS
- `node --check app/ui/static/app.js`: PASS
- `pytest -q`: 689 passed

## Residual risks

- This is UI preference persistence only; no backend trading semantics, risk gates, Bybit order semantics, or execution lifecycle logic were changed.
- Auto-expansion of diagnostics remains intentionally separate from persisted user-selected state.
