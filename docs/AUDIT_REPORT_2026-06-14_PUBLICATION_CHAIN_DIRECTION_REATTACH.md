# Audit report — publication-chain direction reattach guard — 2026-06-14

## Scope

A fresh pass was performed against the uploaded Bybit Linear USDT futures project archive using the supplied audit prompt. The focus was not limited to lint/pytest: the review followed high-risk trading semantics across backend execution lifecycle, one-way long/short state, TP/SL, kill-switch levels, Bybit V5 order intent, UI exposure, risk gates, rounding, and regression coverage.

Primary files reviewed:

- `app/trading_semantics.py` — canonical long/short TP/SL, PnL, risk/reward, Bybit open/close/protective order intent.
- `app/main.py` — operator execution lifecycle, Bybit metadata/preflight guards, UI/API augmentation, active-bot materialization.
- `app/recommender.py` — grid plan generation, direction, leverage, sizing, funding/cost economics.
- `app/grid_math.py` — linear PnL, liquidation proxy, tick/qty rounding helpers and grid economics.
- `app/bybit_client.py` — Bybit public metadata client and Linear USDT scope enforcement.
- `app/ui/static/app.js` — operator UI rendering of long/short, TP/SL, kill-switch and risk/economics values.
- `tests/` — regression coverage for directional semantics, UI helpers, Bybit filters, execution guards and API lifecycle.

## External Bybit V5 cross-check

The code was compared against current public Bybit V5 documentation for the following production-relevant assumptions:

- `/v5/order/create` supports the linear category and exposes `positionIdx`, TP/SL-related fields, side and close/reduce controls for order placement semantics.
- `/v5/position/trading-stop` creates system-managed TP/SL conditional orders for open positions.
- `/v5/market/instruments-info` is the authoritative source for instrument filters such as tick size, qty step, minimum order quantity and minimum notional.
- Bybit help documentation describes reduce-only orders as orders intended to strictly reduce an existing position and avoid unintended exposure increase.

## Findings and fixes

### HIGH — same publication-chain reattach could mark the wrong one-way direction as executed

- **Files:** `app/main.py`, `tests/test_iteration168_execution_direction_conflict_guard.py`, `tests/test_api.py`.
- **Area:** execution lifecycle, one-way Linear USDT futures state, flip long → short / short → long handling.
- **Problem:** the system already had a same-symbol one-way direction conflict guard, but it skipped all running bots from the same `publication_root_rec_id` as idempotent re-attachment. Separately, `_materialize_bot_from_rec()` reused a running bot from the same publication chain before proving that the later recommendation had the same executable direction as the already running bot.
- **Why this is an error:** a publication chain can receive a later active/recommended row while the root bot is still running. If that later row flips from `long` to `short` or from directional to `neutral`, returning the existing bot falsely marks the new row as `executed` while the actual local/exchange state is still the previous side.
- **Financial/trading risk:** the operator or a future execution adapter could believe that a short recommendation was executed while only a long bot exists. That breaks the single source of truth for TP/SL, reduce-only close side, exposure, reconciliation and UI risk display. In a one-way Bybit account this can cause wrong-side protection or hidden position conflict.
- **Fix:**
  - `_execution_symbol_direction_conflict_blocks()` now skips same publication-root bots only when the existing running bot direction equals the candidate direction.
  - Added `_running_publication_root_bot_direction_blocks()` to validate idempotent chain reuse before returning an existing running bot.
  - `_materialize_bot_from_rec()` now blocks publication-chain reattach when the existing running bot has unknown direction or a different direction than the candidate recommendation.
  - The block is fail-closed with `PUBLICATION_CHAIN_DIRECTION_CHANGED` or `EXISTING_CHAIN_DIRECTION_UNKNOWN`.
- **Regression tests added/updated:**
  - same publication-root + same direction remains idempotent and allowed;
  - same publication-root + direction flip is blocked by the low-level conflict guard;
  - API execution of a chain member that flips long → short returns `409` and does not create a second bot;
  - the later flipped recommendation remains `active`, not incorrectly marked `executed`;
  - the original running bot remains the only bot and keeps its original direction.

## Verified existing controls

No new short TP/SL inversion was found in the canonical backend/UI path. The current canonical semantics remain:

| Direction | TP | SL | Profit move | Loss move |
|---|---:|---:|---|---|
| `long` | above entry / upper kill-switch | below entry / lower kill-switch | price rises | price falls |
| `short` | below entry / lower kill-switch | above entry / upper kill-switch | price falls | price rises |
| `neutral` | no directional TP | lower/upper kill-switch exits | not modelled as single directional trade | not modelled as single directional trade |

Verified components:

- `directional_exit_levels()` maps kill-switch lower/upper to the correct directional TP/SL.
- `validate_directional_exit_geometry()` rejects swapped or non-strict TP/SL around reference price.
- `directional_trade_math()` computes positive reward and positive risk magnitudes only for valid long/short geometry.
- `bybit_linear_protective_order_plan()` emits reduce-only, close-on-trigger protective intent only after geometry validation.
- UI reads backend `directional_exit_levels`, validates geometry and falls back to kill-switch-only rendering if backend geometry is invalid.
- Execution preflight validates Linear USDT metadata, grid range, kill-switch containment, qty step, min qty, min notional, leverage limits and directional exit geometry.

## Static scan

A keyword scan was run for trading-risk terms: `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`, `positionIdx`, `closeOnTrigger`, `triggerDirection`, `minNotional`, `qtyStep`, `tickSize`, `lookahead`, `future`, `rolling`, `shift`, `sort`, `timestamp`, `partial`, `fill`, `retry`, `order`, `publication_root`, `direction`.

The scan is stored at:

`docs/STATIC_SCAN_2026-06-14_PUBLICATION_CHAIN_DIRECTION_REATTACH.txt`

The actionable defect found and fixed from this pass was the publication-chain same-root direction reattach hole described above.

## Checks run

| Check | Result |
|---|---:|
| `python -m compileall -q app tests main.py` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| `pytest -q` | PASS — `654 passed in 17.16s` |
| `npm/yarn tests` | Not applicable: no `package.json` present |
| configured lint/type-check | Not found in project configuration |

## Files changed

- `app/main.py`
- `tests/test_iteration168_execution_direction_conflict_guard.py`
- `tests/test_api.py`
- `docs/AUDIT_REPORT_2026-06-14_PUBLICATION_CHAIN_DIRECTION_REATTACH.md`
- `docs/STATIC_SCAN_2026-06-14_PUBLICATION_CHAIN_DIRECTION_REATTACH.txt`

## Residual risks

- Authenticated Bybit private order placement, live positions, partial fills, rejected orders, insufficient balance and exchange-side reconciliation cannot be fully proven in this offline audit environment.
- Liquidation estimates remain conservative approximations; exact values depend on Bybit risk tier, mark price, margin mode, maintenance margin and account state.
- The current project is an operator/recommendation layer. Any future direct OMS/executor must reuse the same canonical direction helpers and must add exchange-side idempotent `orderLinkId`/reconciliation tests before live automation.
- Browser cache invalidation cannot be proven from static files; production deployment should force UI asset cache busting after JS changes.
