# Audit report: compact observability and grid/trend strategy math

Date: 2026-07-20  
Release: 1.4.3  
Input archive SHA-256: `08893307db2e85d3537747987d5dab52d774f01380e01ceae32aaf3cc7b9efcd`

## Scope

One connected work package was completed:

1. remove or collapse repeated aggregations in the operator dialogs «Результаты наблюдений» and «Здоровье системы»;
2. reduce wide-dialog dimensions to 1600 px and add global Escape close behavior;
3. independently re-audit LONG/SHORT signs, TP/SL geometry, funding cashflow, first-touch semantics and grid outcome accounting before changing any strategy threshold;
4. synchronize the iterative prompt, operator manual and active project documentation.

No private Bybit order endpoint, auto-execution path, strategy threshold, calibration gate, router threshold or risk limit was added or weakened.

## Evidence supplied by the operator

The diagnostic snapshot is operationally healthy: 35/35 symbols are ready, no stale/missing/disabled symbols and no errors in the last ten minutes. The latest publication contains 70 rows, all `no_trade`; actionable count is 0.

Current outcome lineage:

- model lineage: `bybit-taxonomy-v11-separated-operator-outcome-lineage`;
- current policy fingerprint: `296ae20b000f671da48e7c11d7f2163af2ae7db8912e4c05b9474cd99f9cc0d5`;
- current-model outcomes: 84;
- futures_grid: 13 current-model outcomes, 0 policy-eligible;
- directional_trend: 71 current-model outcomes, 2 policy-eligible;
- historical outcomes: 276;
- outcome semantic integrity: OK.

The attached `stats.json` belongs to an older `bybit-taxonomy-v10-terminal-selected-policy-money` lineage with fingerprint `96f87a...`; its aggregates were not combined with the newer diagnostic snapshot.

## Confirmed defects

### MEDIUM - repeated outcome aggregations obscured the primary answer

The previous Results layout repeated the same current-policy rows through strategy, execution direction, raw-to-execution pair, neutral subtype and raw thesis tables. These views were not independent evidence and required the operator to reconcile near-duplicate totals manually.

Fix: the primary Results level now contains summary cards, mutually exclusive eligibility cohorts, one canonical strategy table, decision-oriented insights and one LLM table. Symbol rows, detailed LLM matrix, current journal and archive remain available in disclosure blocks.

### MEDIUM - Health repeated the same readiness state in several sections

Cards and multiple tables repeated outcome queues, calibrator readiness, runtime and database continuity. Important blocks and operator explanations were visually diluted.

Fix: explanations, `no_trade` reasons and hard blocks are one operator-status table; market readiness, outcome queues and model evidence are one readiness table. Runtime, database continuity, semantic integrity, collector/backfill and LLM configuration remain in advanced diagnostics.

### HIGH operator-interpretation defect - shadow evidence looked like trading-candidate performance

The screenshot’s strategy rows were easy to read as the performance of allowed forecasts, while the diagnostic snapshot has `actionable_count=0`. The displayed sample is primarily shadow/no-trade research evidence.

Fix: Results explicitly states when there are no actionable outcomes and separates actionable, shadow, calibration-eligible and policy-evaluation cohorts. The headline for current trading rules is not populated from all research roots.

### MEDIUM - `success` and `net P&L` were visually conflated

For grid, the label contract defines a kill-switch terminal event as unsuccessful even if earlier completed grid cycles leave positive terminal proxy P&L. Therefore `0% success` and positive mean net result are mathematically possible. For trend, TP_FIRST/SL_FIRST/HORIZON_EXIT and monetary return are also distinct.

Fix: columns and help text now use «Доля успешных по контракту» and «Средний net результат» as separate metrics and explain the distinction.

### LOW/MEDIUM - oversized dialogs and incomplete keyboard close contract

The 1900 px / near-full-height layout was wider than requested and Escape did not close every open modal consistently.

Fix: wide dialogs use `min(1600px, calc(100vw - 32px))`, `min(88vh, 900px)` and local scrolling. Escape invokes one global `closeAllDialogs()` path.

## Strategy math audit

### Directional price and P&L signs

Canonical helpers were checked in `app/trading_semantics.py` and mirrored by regression fixtures:

- LONG profits when exit > entry and loses when exit < entry;
- SHORT profits when exit < entry and loses when exit > entry;
- equal mirrored relative moves produce equal-magnitude signed returns;
- LONG TP is above entry and SL below; SHORT TP below and SL above;
- risk:reward is symmetric under a mirrored LONG/SHORT setup.

No sign inversion was confirmed.

### Multi-timeframe direction

A monotonically rising synthetic path resolves to LONG and its mirrored falling path resolves to SHORT. The slope/MACD/RSI contribution signs were not inverted.

### Funding

Settled funding follows position cashflow:

- positive rate: LONG pays, SHORT receives;
- negative rate: SHORT pays, LONG receives.

Approval economics continue to reserve only adverse funding; historical outcome accounting uses signed settled funding. No inversion was confirmed.

### Trend first-touch

LONG and SHORT high/low touch rules, gap-through-SL behavior, HORIZON_EXIT and same-candle ambiguity were reviewed. A same-candle TP+SL remains `AMBIGUOUS` and censored. No confirmed first-touch side inversion was found.

### Grid outcome accounting

Grid terminal net is calculated from strategy-native fills, fees, slippage, funding and terminal inventory handling. `success` is a strategy-contract event, not `ret > 0`. A kill-switch may coexist with positive terminal proxy P&L after prior profitable cycles. This explains the apparently contradictory grid row in the screenshot without requiring a sign flip.

## Why the supplied aggregates are weak

The snapshot does not show a proven losing executable policy. It shows a healthy fail-closed system with no allowed trade and insufficient decision-ready evidence:

- 52 rows: calibrated confidence unavailable;
- 35: positive proxy monetary expectancy unproven;
- 33: mean-reversion edge unconfirmed;
- 18: trend direction unconfirmed;
- 17: first-touch model unavailable;
- 15: trend regime unconfirmed;
- 15: trend strength insufficient.

Only two current directional-trend outcomes are policy-eligible and no current grid outcome is policy-eligible. The observed negative research averages are a reason not to trade, but they are not evidence of a software sign inversion and are not a valid estimate of live executable strategy expectancy.

## Files changed

- `app/ui/static/app.js`
- `app/ui/static/styles.css`
- `app/ui/static/index.html`
- `app/main.py`
- `tests/test_iteration273_compact_observability_strategy_audit.py`
- version assertions and the superseded Results-heading assertion in affected historical regression tests
- `README.md`, `CHANGELOG.md`
- `docs/Bybit_Recommender_Iteration_Prompt.md` and root PDF
- `docs/instrukciya_operatora_bybit_recommender.docx` and PDF
- `docs/TRADING_LOGIC.md`, `KNOWN_RISKS.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`
- this audit report

## Verification

- `python -m compileall -q app`: passed.
- `node --check app/ui/static/app.js`: passed.
- focused strategy/UI/document regression: 127 passed.
- exact collected suite: 1300/1300 passed in twelve deterministic, non-overlapping batches.
- the monolithic pytest process did not return a final summary in the harness and slowed during `test_iteration262_terminal_selected_policy_monetary`; the same nodes passed in bounded groups. It is not counted as a monolithic pass.
- operator DOCX rendered to 18 pages and was visually inspected; PDF regenerated from that DOCX.
- iterative prompt PDF rendered to 20 pages and was visually inspected; searchable text contains v1.4.3, 1600 px, Escape and the router contract.

## Residual limitations

- No claim of strategy profitability, live edge or production execution readiness is made.
- The supplied current-policy sample is too small and censored for a reliable expectancy conclusion.
- OHLCV proxy outcomes cannot prove queue priority, partial fills or actual exchange execution.
- A browser screenshot smoke test against a running operator instance was not available in this isolated archive environment; frontend production helpers and API/UI contracts were executed through Node/Python regressions.

## Suggested commit message

`fix(ui): compact observability and verify grid/trend payoff semantics`
