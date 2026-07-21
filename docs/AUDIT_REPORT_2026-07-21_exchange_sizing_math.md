# Audit report: exchange-executable sizing and trading-math recheck

Date: 2026-07-21  
Input release: 1.4.5  
Input ZIP SHA-256: `b4f3f954d9875689fc7b4156bc804776b8eacc914633e366373a1725b0e70f0a`  
Output release: 1.4.6  
Scope: screenshot reproduction, Bybit quantity/leverage normalization, grid/trend payoff paths, UI blocker semantics.

## Acceptance criteria

1. Generated provisional grid/trend quantity below live minimum becomes the smallest exchange-executable step, not zero.
2. Explicit/manual quantity is never increased.
3. Any generated minimum lift is followed by full-grid notional and margin revalidation and can still be blocked by runtime limits.
4. Generated leverage never increases during step alignment.
5. One primary exchange failure is shown once in Details.
6. Directional gap/first-touch MFE/MAE contains no post-exit candle prices.
7. LONG/SHORT P&L and funding signs remain mirror-symmetric; grid completed-pair fees and funding layers remain separated.
8. Full test collection passes without schema, policy-lineage or threshold changes.

## Confirmed defects

### EXSZ-01 - HIGH - generated provisional qty was structurally non-executable

The default target notional produced a BTC quantity below `minOrderQty=qtyStep=0.001`. The old universal down-only alignment either preserved the raw sub-step amount in non-auto-snap paths or reduced it to zero. The recommendation could not become exchange-executable even when the operator risk profile had enough capacity.

Fix: classify sizing provenance. Explicit/manual qty remains down-only. Generated provisional qty may be lifted only to the exact minimum satisfying `minOrderQty`, `qtyStep` and `minNotional` at the conservative grid price. The normalized payload then recomputes arithmetic-grid commitment, maximum position slots, worst-case notional and margin. Runtime limits remain authoritative.

### EXSZ-02 - MEDIUM - derivative and duplicated operator reasons

A below-min quantity also emitted `ORDER_QTY_OFF_STEP` with nearest value `0.000`, while `risk_report` repeated the same prose as generic `RISK`. The UI therefore displayed one invariant multiple times.

Fix: below-min is primary and suppresses the derivative off-step error. The frontend removes a generic item when the same normalized message exists under a concrete machine code, independent of source ordering.

### EXSZ-03 - HIGH - post-exit MFE/MAE contamination

The directional outcome updated MFE/MAE from the complete exit candle before checking a gap or first-touch exit. Prices that may have occurred after the position was closed therefore changed excursion diagnostics.

Fix: gap exits use the observed open; first-touch exits use the terminal TP/SL trigger. Full candle high/low is applied only when the candle does not terminate the trade. Same-candle TP+SL remains censored.

### EXSZ-04 - MEDIUM - generated grid leverage could round upward

Grid leverage used nearest-step alignment, unlike directional leverage. A value such as 3.06 with a 0.1 step became 3.1 and silently worsened leverage risk.

Fix: generated grid leverage aligns down.

### EXSZ-05 - MEDIUM - empty trend direction raised NameError

The fail-closed branch referenced a local aggregation object unavailable in `_directional_trend_params`.

Fix: use only the explicit function argument; unknown direction returns `trend_evaluation_rejected`.

## RED -> GREEN evidence

RED command:

`pytest -q tests/test_iteration276_exchange_sizing_math.py` on pristine 1.4.5 plus tests.

RED result: `8 failed, 1 passed`. Failures independently reproduced under-min generated grid and trend qty, derivative off-step cascade, duplicate UI RISK, post-exit MFE, risk-increasing leverage rounding and the empty-direction NameError.

GREEN command:

`pytest -q tests/test_iteration276_exchange_sizing_math.py` on working 1.4.6.

GREEN result: `9 passed`.

## Trading-math review

- Canonical LONG and SHORT gross profit/loss geometry remains symmetric.
- Positive funding is a cost for long and a receipt for short; negative funding is the mirror image.
- Grid completed-pair profit remains adjacent interval minus both trading fills. Funding remains a position-time Total-P&L layer and is not multiplied into every grid cycle.
- MinNotional for grid base quantity is checked at the conservative minimum grid price.
- Full commitment and maximum net-position exposure remain distinct quantities.
- No confirmed sign inversion was found in canonical P&L, TP/SL or settled funding paths.

Independent randomized check: 2,000 mirrored directional/funding cases plus grid fee/funding-layer assertions passed. Focused math/path collection: 508 passed.

## Compatibility

- Database schema: unchanged.
- Model/policy/outcome lineage: unchanged.
- Outcome event class, `success` and net-return formula: unchanged.
- Existing recommendations/outcomes remain auditable. No database deletion is required.
- Restart is required so new recommendation payloads and frontend assets use v1.4.6 semantics.

## Verification

- `pytest --collect-only -q`: 1319 tests collected.
- Exhaustive non-overlapping execution: 161 + 88 + 174 + 80 + 208 + 129 + 155 + 151 + 173 = 1319 passed. The union of the nine deterministic file chunks equals the collected set.
- The monolithic harness timed out without a failure summary and is not reported as a pass.
- New regression: 9 passed.
- Focused trading-math/path collection: 508 passed.
- Randomized mirror check: 2,000 LONG/SHORT and funding cases passed.
- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- DOCX/PDF operator guide: 19 pages rendered and visually inspected; no clipping or overlap found.
- Iterative audit prompt PDF: 21 pages rendered and visually inspected; Cyrillic text, headings and page flow verified.
- `python -m ruff check .`: unavailable in the environment.
- `python -m pip check`: shared-host `moviepy 2.2.1` / `Pillow 12.2.0` conflict remains; project code did not change these packages.

## Residual risks

Historical outcomes are OHLCV proxies. They cannot prove queue priority, exact intrabar order, partial fills, private account balance, actual exchange fees or liquidation waterfall. The service remains recommendation/audit-only and does not create Bybit orders.

## Rollback

Stop v1.4.6 and restore the v1.4.5 application files. No database rollback or migration reversal is required. Recommendations produced under v1.4.6 retain immutable audit records; do not rewrite them as v1.4.5 rows.
