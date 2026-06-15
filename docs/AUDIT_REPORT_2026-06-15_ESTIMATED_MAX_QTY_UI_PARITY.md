# Audit report — 2026-06-15 — estimated max qty UI parity

## Scope and audit order

Offline re-audit and minimal safe patch of the uploaded Bybit Linear USDT futures recommendation/operator-preflight repository. I followed the requested order from the attached deep-audit prompt:

1. reviewed `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`;
2. reviewed the canonical directional source `app/trading_semantics.py`;
3. reviewed the latest audit reports dated 2026-06-14 and 2026-06-15, especially prior fixes around directional TP/SL, protective Bybit order semantics, worst-case qty/notional, operator filters and 3-5x leverage policy;
4. fixed only a newly identified backend↔frontend parity issue without weakening fail-closed guards.

The system boundary remains unchanged: this repository is a recommendation + operator UI + execution-preflight/fail-closed layer. It is not a live OMS/EMS and does not manage exchange open orders, partial fills, cancellations or exchange-side reconciliation. Those remain external execution-layer requirements under `docs/KNOWN_RISKS.md`.

## Baseline before changes

Commands attempted from project root before changes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
```

Baseline results:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- A single `pytest -q` invocation progressed through most of the suite but did not emit a final summary before the sandbox timeout. To preserve exact baseline accounting, the same collected suite was executed in deterministic file shards:
  - shard 1: `447 passed in 22.38s`;
  - shard 2: `242 passed in 9.64s`;
  - baseline total: `689 passed`, `0 failed`, `0 skipped`.

## Trading-semantics and risk map reviewed

Single source of truth and consumers checked:

- `app/trading_semantics.py`: canonical `long`/`short`/`neutral` normalization, TP/SL mapping, directional gross PnL, risk/reward, Bybit one-way `Buy`/`Sell` open/close mapping, protective TP/SL `reduceOnly` / `closeOnTrigger` / `triggerDirection` semantics.
- `app/main.py`: operator API enrichment, `_directional_exit_payload_for_reco()`, `_directional_exit_qty_for_reco()`, execution preflight, Bybit metadata validation, live-price guard, runtime risk caps, same-symbol direction-conflict guard.
- `app/grid_math.py`: linear USDT PnL, funding cashflow sign convention, liquidation-buffer approximation.
- `app/recommender.py`: grid economics, leverage interval, funding/cost model, risk caps and recommendation publication payloads.
- `app/outcomes.py`: proxy outcome labels; still not a realized fill/funding/liquidation truth source.
- `app/calibration.py`: calibration uses stored feature snapshots where available and retains effective-sample/class-balance guardrails; small-sample proxy calibration remains residual/advisory.
- `app/ui/static/app.js`: operator cards, details, TP/SL display, position size, risk/reward display, recommendation filters and frontend Bybit price formatting.
- `app/ui/static/index.html`: cache key used to prevent stale operator JavaScript after UI semantics changes.
- `tests/`: directional TP/SL, Bybit semantics, UI parity, execution-preflight and risk regression coverage.

No new long/short TP/SL inversion, protective order `side` inversion, `reduceOnly` weakening, `closeOnTrigger` weakening, or fail-open execution change was found.

## Finding and fix

### MEDIUM — UI position-size display did not consume `estimated_max_position_qty`, while backend TP/SL PnL already did

- **Files / ranges**:
  - `app/main.py`, around line 832: backend `_directional_exit_qty_for_reco()` already included `estimated_max_position_qty` in total-position qty keys.
  - `app/ui/static/app.js`, lines 953-956: patched frontend explicit position qty key list.
  - `app/ui/static/index.html`, lines 7 and 126: static cache key bumped to `manual-ui-v40`.
  - `tests/test_iteration179_estimated_max_qty_ui_parity.py`, lines 29-73: new red→green regression tests.

**Problem:**

Backend directional TP/SL math treated `estimated_max_position_qty` as a full-position quantity and used it to compute gross TP/SL PnL. The operator UI `buildOperatorFieldSpecs()` recognized `estimated_position_qty`, `position_qty`, `total_qty`, `estimated_total_qty` and `max_position_qty`, but omitted `estimated_max_position_qty`. A payload that carried only this key could therefore have backend `directional_exit_levels.trade_math.qty=7.5` while the UI position-size panel failed to display the matching base quantity.

**Why this is an error:**

The prompt requires backend↔frontend semantic parity for side / TP / SL / PnL / risk displays. This was not an execution-order bug and could not place a live order, but it created a parity gap between backend TP/SL gross PnL and the operator’s position-size display.

**Financial / trading risk:**

- Operator could see absolute TP/SL PnL based on full qty, while the position-size field omitted that same base qty.
- Manual review could under-read exposure for payloads using the `estimated_max_position_qty` key.
- Future frontend code could accidentally reintroduce backend↔UI drift if the key set was not covered by regression tests.

**Fix:**

- Added `estimated_max_position_qty` to the UI explicit position qty key list in `app/ui/static/app.js`.
- Bumped `app/ui/static/index.html` asset key from `manual-ui-v39` to `manual-ui-v40` so stale cached JS cannot mask the fix.
- Updated existing cache-key tests from `manual-ui-v39` to `manual-ui-v40`.
- No backend execution, risk guard, Bybit side mapping, protective-order semantics, or leverage policy was weakened.

## Red→green evidence

New test file:

- `tests/test_iteration179_estimated_max_qty_ui_parity.py`
  - `test_backend_directional_exit_accepts_estimated_max_position_qty`
  - `test_operator_ui_uses_same_estimated_max_position_qty_key_as_backend`
  - `test_static_asset_cache_key_bumped_after_estimated_qty_ui_patch`

Red run before the UI fix:

```text
pytest -q tests/test_iteration179_estimated_max_qty_ui_parity.py --tb=short
.FF
2 failed, 1 passed in 1.92s
```

The failing checks proved that:

- backend already used `estimated_max_position_qty`;
- UI key list did not include it;
- the static asset key was still `manual-ui-v39`.

Green run after fix:

```text
pytest -q tests/test_iteration179_estimated_max_qty_ui_parity.py --tb=short
3 passed in 1.57s
```

Related regression run after fix:

```text
pytest -q tests/test_iteration172_ui_worst_case_margin_display.py \
          tests/test_iteration178_worst_case_qty_key_parity.py \
          tests/test_iteration179_estimated_max_qty_ui_parity.py \
          tests/test_iteration173_env_and_ui_qty_consistency.py --tb=short
10 passed in 1.94s
```

## Final verification

Commands run after fixes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
```

Results:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.

Post-fix pytest was executed in deterministic file shards because the single full invocation repeatedly did not emit a final summary before the sandbox timeout. The shards cover all collected test files:

```text
shard 1: 405 passed in 21.82s
shard 2: 45 passed in 5.29s
shard 3: 242 passed in 10.01s
post-fix total: 692 passed, 0 failed, 0 skipped
```

Baseline vs post:

| Phase | Passed | Failed | Skipped | Note |
|---|---:|---:|---:|---|
| Baseline | 689 | 0 | 0 | deterministic shards after single full run timed out before summary |
| Post-fix | 692 | 0 | 0 | +3 red→green tests |

`npm` / `yarn` tests were not run because no `package.json` is present in the project root.

## Static scan

Saved to:

- `docs/STATIC_SCAN_2026-06-15_ESTIMATED_MAX_QTY_UI_PARITY.txt`

High-risk terms scanned across `app` and `tests`: `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `closeOnTrigger`, `triggerDirection`, `kill`, `leverage`, `pnl`, `roi`, `risk`, `tick`, `qty`, `minNotional`, `min_notional`, `positionIdx`, `estimated_max_position_qty`.

Changed/new hits for `estimated_max_position_qty` were reviewed as safe:

- backend hit: existing canonical qty extraction in `app/main.py`;
- frontend hit: added UI display parity in `app/ui/static/app.js`;
- tests: regression coverage only.

## Checks not executable offline

- Private Bybit account/testnet execution checks: not executable without credentials, account mode, live wallet/position state and exchange-side order/fill fixtures.
- Actual partial fills, rejected orders, order retries, idempotent exchange `orderLinkId` behavior and reconciliation: outside this repository’s stated boundary unless a real execution adapter is added.
- Realized ROI/net PnL after fills/funding/liquidation: not present as exchange truth in this archive; the repository remains a recommendation/preflight layer.

## Residual risks and changes relative to `docs/KNOWN_RISKS.md`

No residual risk was weakened or removed.

Still open by design:

- no real OMS/EMS and no exchange-side order lifecycle management;
- no final truth model for open orders, fills, funding, liquidation or wallet balance;
- proxy outcome labeling remains advisory;
- calibration remains guarded but cannot prove live non-stationary performance;
- exact liquidation price requires exchange/account context.

Closed in this patch:

- one backend↔frontend display parity gap for `estimated_max_position_qty` in operator position-size rendering.

## Files changed

- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `tests/test_iteration179_estimated_max_qty_ui_parity.py`
- existing cache-key tests updated from `manual-ui-v39` to `manual-ui-v40`
- `docs/AUDIT_REPORT_2026-06-15_ESTIMATED_MAX_QTY_UI_PARITY.md`
- `docs/STATIC_SCAN_2026-06-15_ESTIMATED_MAX_QTY_UI_PARITY.txt`
