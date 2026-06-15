# Audit report — no-trade starvation / leverage gate regression

Date: 2026-06-14  
Scope: Bybit Linear USDT futures recommender, grid-bot publication logic, no-trade/blocked diagnostics, risk/leverage policy, TP/SL directional regression surface.

## Executive summary

The observed operator symptom — roughly 12 hours with no practically tradeable proposals and the UI showing no active trade ideas — is consistent with an internal publication starvation bug, not with a pure market-data outage.

The strongest root cause found in the uploaded code was an unreachable leverage-selection condition introduced in the conservative runtime-risk layer. With the current default fee model, the recommender almost never selected the operator minimum leverage, then the downstream risk gate blocked the same idea as `MIN_LEVERAGE_PER_BOT`. The UI can present this as a no-trade state because there are no `recommended`/`active` rows.

The patch keeps the fail-closed risk model, but changes the leverage selector from a fixed cost ceiling to a net-grid-edge test. Strong directional or high-quality neutral range grids can now select the operator minimum leverage when projected net edge remains positive after fees/slippage/funding. Thin-edge, high-volatility or high-cost ideas still stay at 1x and are blocked.

## Root cause

### 1. Operator-minimum leverage was practically unreachable

- Severity: **critical**
- File: `app/recommender.py`
- Area: `_params(...)`, leverage policy before publication risk checks
- Problem:
  - The code selected the operator minimum leverage only when `execution_cost_bps <= 10.0`.
  - Default `TAKER_FEE_BPS_LINEAR=6` means round-trip fee floor is already `12 bps`.
  - `_estimate_cost_model(...)` also adds at least minimal slippage, so even a zero-spread symbol has `execution_cost_bps≈13 bps`.
  - Therefore the leverage selector fell back to `1x` for otherwise viable setups.
  - Later publication risk logic compared `leverage=1` against `min_leverage=3` and appended `MIN_LEVERAGE_PER_BOT`, blocking the proposal.
- Trading/operational risk:
  - The system can stop publishing actionable ideas even in valid range markets.
  - Operator sees long periods of no actionable proposals and may misinterpret this as market selectivity rather than a logic regression.
  - Risk policy becomes internally contradictory: default fee assumptions make the configured operator leverage policy impossible to satisfy.
- Fix:
  - Added `_select_operator_grid_leverage(...)`.
  - The selector now uses projected net grid edge:
    - estimated gross grid capture from actual arithmetic grid spacing;
    - minus execution cost;
    - minus adverse funding cost;
    - minimum projected net edge threshold: `2 bps`.
  - Strong directional quality still requires sufficient direction strength.
  - Neutral range grids can also select operator minimum leverage when the range quality is high and trendiness is low; liquidation buffers are still checked downstream.
  - High-volatility, very high-cost, and thin-edge setups remain `1x` and are blocked fail-closed.
- Tests added:
  - `test_operator_min_leverage_is_not_unreachable_below_default_fee_floor`
  - `test_leverage_selector_keeps_unsafe_or_thin_edge_ideas_at_one_x`
  - `test_neutral_high_quality_range_can_use_operator_minimum_with_liq_checks_downstream`

### 2. Lack of regression coverage for fee-floor/leverage interaction

- Severity: **high**
- Files: `tests/test_iteration159_no_trade_regression.py`
- Problem:
  - Previous tests covered many long/short, TP/SL, risk and execution-preflight cases, but did not assert that default fee assumptions still allow a good setup to satisfy `min_leverage`.
  - This allowed the fixed `10 bps` ceiling to pass the full test suite even though it was below the configured round-trip fee floor.
- Risk:
  - Future risk hardening can again make all ideas unpublishable without failing CI.
- Fix:
  - Added explicit regression tests proving that default `6 bps` taker fees still allow a strong setup to select `5x` when net grid edge is positive.
  - Added a negative test ensuring thin-edge ideas remain at `1x`.

## What was rechecked

### No-trade / publication flow

Checked:

- score threshold path: `score < min_score_to_recommend -> no_trade`;
- confidence gate path: calibrated confidence below `MIN_CONF_TO_RECOMMEND -> no_trade`;
- LLM reviewer pending timeout path: stale pending rows become `no_trade` when OK verdict is required;
- publication gate path: first-cycle grid ideas can become `pending`, not `no_trade`;
- risk blocks path: blocked ideas are not actionable and make the operator-level dashboard appear as no trade;
- best-per-symbol suppression path.

No code change was made to LLM fail-closed behavior. If `LLM_REVIEWER_ENABLED=1` and the local reviewer is unavailable, recommendations will still go `pending` and then `no_trade` by design after `LLM_REVIEWER_PENDING_TIMEOUT_SEC`. This is a separate operator configuration risk to check on the live instance.

### Directional and TP/SL semantics

Rechecked the existing canonical surface:

- `app/trading_semantics.py` remains the single source for long/short TP/SL geometry.
- Long: TP above reference, SL below reference.
- Short: TP below reference, SL above reference.
- Bybit protective orders continue to use close side, `reduceOnly=True`, `closeOnTrigger=True`, `positionIdx=0`.
- Invalid directional geometry still suppresses protective-order payload publication.

No new TP/SL inversion was found in this pass.

### Risk management

The fix does **not** bypass risk checks. It only prevents a contradictory pre-risk leverage selector from forcing otherwise valid ideas to 1x. The following gates remain active after the patch:

- `MIN_LEVERAGE_PER_BOT`;
- `MAX_LEVERAGE_PER_BOT`;
- `GRID_NET_PROFIT_NON_POSITIVE`;
- `GRID_NET_PROFIT_TOO_THIN`;
- `GRID_GROSS_EDGE_BELOW_COSTS`;
- `LIQUIDATION_BUFFER_TOO_LOW`;
- `MAX_POSITION_NOTIONAL_PER_BOT`;
- `MAX_MARGIN_PER_BOT`;
- funding-rate and funding-interval blocks;
- market-shock and fast-veto blocks;
- active-bot, daily drawdown and cooldown blocks.

## Files changed

| File | Change |
|---|---|
| `app/recommender.py` | Added `_select_operator_grid_leverage(...)`; replaced impossible `execution_cost_bps <= 10` leverage gate with net-edge-based selector; added leverage diagnostics to `params.leverage_policy`. |
| `tests/test_iteration159_no_trade_regression.py` | Added regression tests for default fee floor vs operator min leverage, thin-edge fail-closed behavior, and neutral range leverage selection with downstream liquidation checks. |
| `docs/AUDIT_REPORT_2026-06-14_NO_TRADE_REAUDIT.md` | This report. |
| `docs/TRADING_LOGIC.md` | Documented net-edge leverage selection to prevent fee-floor starvation. |

## Checks executed

| Check | Result |
|---|---:|
| `python -m compileall -q app tests` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| `pytest -q` | PASS — `554 passed in 17.87s` |
| Static scan over `no_trade`, leverage, TP/SL, Bybit protective-order and risk tokens | Completed — 348 targeted matches reviewed/sampled around the changed and relevant paths |

## Checks not executed / limitations

| Check | Status | Reason |
|---|---|---|
| Live DB diagnosis of the last 12 hours | Not executed | The uploaded archive did not include the production database/logs. |
| Live/testnet Bybit order submission | Not executed | No safe API credentials or explicit exchange test scenario were present in the archive. |
| `npm test` / `yarn test` | Not executed | The project has no `package.json` or JS test runner configuration. |
| External lint/type checks | Not executed | No configured `ruff`, `mypy`, ESLint or pyproject lint/type config was present in the archive. |

## Operator follow-up checks on the live instance

Run these SQL/API checks against the live database to confirm the symptom source:

```sql
SELECT status, COUNT(*)
FROM recommendations
WHERE ts > strftime('%s','now') - 12*3600
GROUP BY status
ORDER BY COUNT(*) DESC;
```

```sql
SELECT json_extract(reasons_json,'$.risk_checks.blocks[0].code') AS first_block,
       COUNT(*)
FROM recommendations
WHERE ts > strftime('%s','now') - 12*3600
GROUP BY first_block
ORDER BY COUNT(*) DESC;
```

If `MIN_LEVERAGE_PER_BOT` dominates, this patch addresses the main regression. If `llm_timeout_no_trade` or `LLM_REVIEW_PENDING_TIMEOUT` dominates, inspect `LLM_REVIEWER_ENABLED`, local Ollama availability, model name and reviewer latency.

## Residual risks

1. A strong market trend can legitimately keep futures-grid ideas out of actionable status; this patch does not force trades in non-range regimes.
2. If `REQUIRE_CONF_GATE=1` and the fitted calibrator is pessimistic after a month of outcomes, score-good ideas can still become `no_trade` due to low calibrated confidence. That is intended, but should be monitored by counting `decision_layers.confidence_gate_applied` and confidence-threshold failures.
3. The patch estimates leverage eligibility before live Bybit preflight. Exchange-side instrument filters, wallet balance, active orders and exact liquidation must still be validated immediately before launch.
