# Audit report — operator blocked/no_trade clarity and fail-closed status review (2026-06-14)

## Scope

Triggered by the operator screenshot where all visible futures-grid rows were either `blocked` or `no_trade` and the Details panel showed the diagnostic card `Ранг не равен разрешению запуска` directly after the red “Не запускать” decision. The audit used the attached deep-review prompt and focused on operator status semantics, hard blockers, launch-score/no_trade gates, UI ordering, Bybit one-way protective semantics, and regression coverage for long/short TP/SL display.

## Answer to the operator question

Seeing only `blocked` and `no_trade` rows can be correct in the current fail-closed design:

- `blocked` means the recommendation must not be launched manually because at least one hard runtime/risk/Bybit/preflight guard rejected it.
- `no_trade` means the setup is not launchable by scoring/confidence/economics gates, but it is not necessarily a Bybit execution error.
- A high relative rank in the visible table is not a launch permission. The table rank is only a UI percentile among currently visible symbols.

However, the previous Details ordering was misleading: after “Причина показана ниже”, the first card below was the rank-diagnostics card, not the actual blocker card. This could make the operator think the row was blocked *because of rank*. That was a UI/UX safety bug even if backend fail-closed logic was correct.

## Findings and fixes

| ID | Severity | Area | File | Problem | Trading risk | Fix | Tests |
|---|---:|---|---|---|---|---|---|
| OBN-001 | Medium | UI/UX operator safety | `app/ui/static/app.js` | A `blocked` row said the reason was below, but the immediately visible card was rank diagnostics, while actual blockers were rendered much lower after price/risk/launch/LLM sections. | Operator could misread a hard risk/Bybit/preflight block as a harmless rank explanation and manually recreate the bot outside the guided flow. | Rendered `blockersHtml` immediately after the main decision card, before rank diagnostics. Renamed the block title to `Фактическая причина блокировки / предупреждения`. | `tests/test_iteration166_operator_blocked_notrade_clarity.py::test_blocked_details_show_actual_blocker_before_rank_diagnostics` |
| OBN-002 | Medium | UI/UX status semantics | `app/ui/static/app.js` | The top banner used a generic `NO-TRADE` wording even when the visible set contained hard `blocked` rows. | Operator could confuse score-based no_trade with hard exchange/risk/preflight blocking. | Banner now says `НЕТ ЗАПУСКАЕМЫХ` and explicitly differentiates `blocked` from `no_trade`. | `tests/test_iteration166_operator_blocked_notrade_clarity.py::test_no_launchable_banner_distinguishes_blocked_from_no_trade` |
| OBN-003 | Low | Static asset coherency | `app/ui/static/index.html` | JS changed but cache key had to be bumped to prevent browsers from keeping stale operator UI. | Old UI could continue to hide the actual blocker below diagnostics after deployment. | Bumped static asset key from `manual-ui-v31` to `manual-ui-v32`; updated cache-key tests. | Existing UI cache-key tests + `test_static_asset_cache_key_bumped_after_blocked_notrade_clarity_patch` |

## Directional/Bybit semantics rechecked

The canonical model still matches one-way Bybit linear USDT semantics:

- long open = `Buy`, long close/protective exit = `Sell` with `reduceOnly=true`, `closeOnTrigger=true`;
- short open = `Sell`, short close/protective exit = `Buy` with `reduceOnly=true`, `closeOnTrigger=true`;
- long TP must be above reference and long SL below reference;
- short TP must be below reference and short SL above reference;
- `positionIdx=0` is kept for one-way mode;
- neutral grids do not expose a directional TP.

No backend sign inversion was found in the audited canonical helpers. Existing tests continue to cover short TP/SL mapping, protective trigger geometry, reduce-only protective orders, risk/reward math, kill-switch lower/upper mapping, tick/qty rounding, min-notional, operator payload validation and runtime leverage profile guards.

## Files changed

- `app/ui/static/app.js`
  - actual blocker/warning card is now shown immediately after the red/yellow/green operator decision;
  - wording separates hard `blocked` from scoring/economics `no_trade`;
  - top no-launchable banner now explains both statuses.
- `app/ui/static/index.html`
  - static asset cache key bumped to `manual-ui-v32`.
- `tests/test_iteration166_operator_blocked_notrade_clarity.py`
  - added regression tests for blocker ordering, no-launchable banner semantics, and cache-key bump.
- Updated existing UI regression tests for the new wording/cache key.
- `docs/STATIC_SCAN_2026-06-14_OPERATOR_BLOCKED_NOTRADE_CLARITY.txt`
  - static scan artifact for reviewed status and trading-semantics keywords.

## Verification performed

```text
python3 -m compileall app tests
node --check app/ui/static/app.js
pytest -q
```

Result:

```text
583 passed in 16.78s
```

## Checks not performed

- Live/testnet Bybit order placement was not executed: no API keys or explicit permission to place orders were provided in the archive context.
- Browser E2E rendering was not run: the project does not include a Playwright/Cypress setup. The UI change is covered by static regression tests and `node --check`.
- npm/yarn tests were not run because the project has no package test configuration for this static JS frontend.

## Residual risks

- The table can legitimately show zero launchable pairs during guarded market regimes, stale snapshots, low confidence, thin grid economics, failed Bybit metadata validation, runtime leverage cap changes, or other hard risk gates. This is a conservative design choice, not by itself a defect.
- Operators should treat `blocked` as a hard stop. `no_trade` is a launch-gate refusal, not a guarantee that the market direction is wrong.
- Any manual Bybit bot created outside the UI still must be checked against current instrument metadata, position mode, available balance, leverage, min-notional, tick/qty step, active position state and protective reduce-only exits.
