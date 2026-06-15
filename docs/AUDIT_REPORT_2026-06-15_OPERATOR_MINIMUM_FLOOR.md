# Audit report — 2026-06-15 — operator minimum leverage floor

## Scope and intake

This was an offline regression audit of the uploaded Bybit futures / Linear USDT recommender archive, focused on the reported absence of any `active-recommended` rows and the claim that the operator 3-5x leverage profile had become a second, anti-correlated wall after the score/confidence thesis gate.

Read before changes, as requested:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- Latest audit reports dated 2026-06-14 / 2026-06-15, especially:
  - `AUDIT_REPORT_2026-06-14_RUNTIME_LEVERAGE_PROFILE_GUARD.md`
  - `AUDIT_REPORT_2026-06-14_OPERATOR_FIXED_LEVERAGE_NOTRADE.md`
  - `AUDIT_REPORT_2026-06-14_ADAPTIVE_LEVERAGE_INTERVAL.md`
  - `AUDIT_REPORT_2026-06-15_3X_5X_LEVERAGE_SYNC.md`
  - `AUDIT_REPORT_2026-06-15_WORST_CASE_QTY_KEY_PARITY.md`

The repository boundary remains unchanged: this is a recommendation + operator preflight/fail-closed service, not a live OMS/EMS. Real order lifecycle, partial fills, websocket reconciliation, private liquidation truth and open-order idempotency remain requirements for an external execution/reconciliation layer.

## Baseline before changes

Commands run from project root before code changes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q
```

Baseline results:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- `pytest -q`: printed progress into the 90% range but did not exit before local timeout because of externally auto-loaded pytest plugins/teardown behavior in this container.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q`: `692 passed in 30.41s`.

The plugin-disabled run is the recorded green baseline because it executes the repository test suite without unrelated environment plugins.

## Trading-semantics / risk map reviewed

Single-source directional model and related consumers:

- `app/trading_semantics.py`: canonical long/short/neutral normalization, TP/SL mapping, directional gross PnL and risk/reward, Bybit one-way open/close side mapping, protective TP/SL `reduceOnly` / `closeOnTrigger` and `triggerDirection` mapping.
- `app/recommender.py`: recommendation score/conf gate, futures-grid economics, funding/cost model, leverage profile selector, liquidation-buffer clamp, risk blocks, no-trade reasons and payload construction.
- `app/main.py`: operator API augmentation, execution preflight, runtime risk limits guard, operator next-actions, Bybit metadata validation and live-price/freshness guards.
- `app/grid_math.py`: linear USDT PnL, directional funding cashflow, margin and approximate liquidation-buffer helpers.
- `app/ui/static/app.js`: operator display for status, side, TP/SL, worst-case notional/margin, risk/reward, next-actions and launchability.
- Tests covering directional semantics, UI short TP/SL, invalid protective exits, execution runtime caps, operator leverage policy, adaptive 3-5x leverage, worst-case qty/notional/margin and UI parity.

No new long/short TP/SL inversion, `Buy`/`Sell` inversion, `reduceOnly` weakening, triggerDirection inversion, or backend↔frontend TP/SL parity bug was found in this bounded pass.

## Findings and fixes

### HIGH — operator minimum 3x floor was gated by signal-quality after score/conf thesis gate

- **Files:**
  - `app/recommender.py`, lines 2003-2124.
  - New regression tests: `tests/test_iteration180_operator_minimum_floor.py`, lines 6-52.
- **Problem:**
  - `_select_operator_grid_leverage()` first required hard economics/safety checks: ATR not too high, execution cost below emergency ceiling, and projected net grid edge after execution/funding costs at least 2 bps.
  - After these checks it still required either:
    - directional quality: `dir_strength >= 0.45`, or
    - neutral range quality: `range_score >= 0.70 and trendiness <= 0.35`.
  - If neither quality branch passed, the selector returned `operator_minimum_approved=false` / `signal_quality_too_low_for_operator_minimum` even for the minimum 3x floor.
  - This created a second model-quality gate after the normal thesis gate (`MIN_SCORE_TO_RECOMMEND` / `MIN_CONF_TO_RECOMMEND`). In the reported live diagnostics that explains the anti-correlation: thesis-favoured rows could be downgraded solely by leverage-profile signal quality, while leverage-allowed rows could still fail the thesis gate.
- **Why this is an error:**
  - The active 3-5x profile is an interval. The floor is the base actionable leverage after hard safety/economics checks; quality is supposed to control promotion above the floor.
  - The code comment/docstring already described promotion toward the maximum as adaptive. The implementation incorrectly reused quality as an approval precondition for the floor itself.
  - For grid bots, especially range/grid mechanics, low directional strength is not automatically a reason to reject the minimum floor if the recommendation thesis has already passed score/confidence and grid economics are positive.
- **Trading risk before fix:**
  - Operator-visible `no_trade` could persist for hours even with fresh data and no global risk lock, because the two independent model-quality gates could fail to intersect.
  - This did not create a fail-open exchange path, but it made the recommender overly silent and pushed the operator toward manual policy weakening instead of letting the canonical floor semantics work.
- **Fix:**
  - Kept all hard safety/economics declines unchanged:
    - `unsafe_volatility_or_execution_cost`
    - `atr_too_high_for_operator_minimum`
    - `insufficient_net_edge_for_operator_minimum`
  - Preserved `directional_quality` and `neutral_range_quality` diagnostics.
  - Changed selector semantics so the minimum operator leverage is approved after hard safety/economics checks.
  - Left `_adaptive_grid_leverage_from_quality()` as the promotion gate. Low-quality setups remain at 3x; they do not promote to 4x/5x.
- **Safety direction:**
  - This is a policy-correction, not removal of execution safety. The patch does not lower `MIN_SCORE_TO_RECOMMEND`, does not lower `MIN_CONF_TO_RECOMMEND`, does not change risk caps, does not weaken liquidation-buffer checks, does not bypass Bybit metadata/preflight, and does not touch any live order code.
  - It does allow more score/conf-favoured ideas to become actionable at the 3x floor if all hard economics/safety checks pass. That is the intended behavior of a floor/interval profile and should be monitored after deployment.

## Red→green tests added

New file: `tests/test_iteration180_operator_minimum_floor.py`

1. `test_operator_minimum_floor_is_actionable_when_safety_and_edge_pass`
   - Red before fix:
     - selector returned `note='signal_quality_too_low_for_operator_minimum'`;
     - `operator_minimum_approved=false`.
   - Green after fix:
     - selector returns `leverage=3`, `note='operator_minimum_selected'`, `operator_minimum_approved=true`;
     - `directional_quality=false` and `neutral_range_quality=false` remain diagnostic only;
     - no adaptive promotion is accepted.

2. `test_operator_minimum_floor_still_declines_high_atr_before_quality`
   - Guards against accidental fail-open behavior.
   - Confirms high ATR still returns `atr_too_high_for_operator_minimum` and `operator_minimum_approved=false` before any floor approval.

Targeted red evidence before code fix:

```text
1 failed, 1 passed
FAILED tests/test_iteration180_operator_minimum_floor.py::test_operator_minimum_floor_is_actionable_when_safety_and_edge_pass
AssertionError: assert 'signal_quality_too_low_for_operator_minimum' == 'operator_minimum_selected'
```

Targeted green after code fix:

```text
2 passed in 0.47s
```

Related regression suite after code fix:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q \
  tests/test_iteration159_no_trade_regression.py \
  tests/test_iteration164_runtime_leverage_profile_guard.py \
  tests/test_iteration173_operator_leverage_no_trade_policy.py \
  tests/test_iteration174_operator_next_actions.py \
  tests/test_iteration177_adaptive_leverage_interval.py \
  tests/test_iteration180_operator_minimum_floor.py --tb=short
```

Result:

```text
19 passed in 2.31s
```

## Post-change verification

Commands run after changes:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q --tb=short <full suite split into chunks>
```

Results:

- `python -m compileall -q app tests main.py`: passed.
- `node --check app/ui/static/app.js`: passed.
- Full repository suite, split by test files to avoid the container's single-process teardown timeout:
  - chunk 1: `147 passed in 10.09s`
  - chunk 2: `62 passed in 4.40s`
  - chunk 3: `101 passed in 10.94s`
  - chunk 4: `128 passed in 5.19s`
  - chunk 5: `79 passed in 6.71s`
  - chunk 6: `77 passed in 5.37s`
  - chunk 7: `100 passed in 2.03s`
  - total: `694 passed`.

A single-process post-change `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q` printed visible progress through the final tests but did not emit a final summary before container timeout; the chunked suite above covers the same collected test files and passed cleanly.

`npm` / `yarn` tests were not run because no `package.json`, `yarn.lock`, `pnpm-lock.yaml`, or `package-lock.json` is present in the project root.

## Static scan

A bounded static scan was saved to:

- `docs/STATIC_SCAN_2026-06-15_OPERATOR_MINIMUM_FLOOR.txt`

Reviewed changed/new hits:

- `app/recommender.py:_select_operator_grid_leverage` — changed intentionally. The 3x floor is approved after hard safety/economics checks; promotion above 3x remains quality-gated.
- `tests/test_iteration180_operator_minimum_floor.py` — new red→green semantics tests.
- No changed UI/Bybit side/TP/SL/reduceOnly code paths.

## Answer to the operator-diagnostics claim

The colleagues' diagnosis has substance:

- fresh data and zero active bots rule out collector outage and global concurrency lock as the main cause;
- the reported decision matrix is consistent with two non-intersecting gates;
- the 3-5x leverage selector did contain a real extra quality wall for the minimum floor;
- that extra wall could kill thesis-favoured rows even though the separate score/confidence gate was already responsible for model-quality approval.

The part I would not accept blindly is lowering `MIN_CONF_TO_RECOMMEND` or disabling `REQUIRE_CONF_GATE`. That remains a direct risk-appetite decision. This patch intentionally avoids changing those thresholds.

## Residual risks and KNOWN_RISKS delta

Changed/closed:

- The operator leverage interval now matches its intended floor/promotion semantics: 3x can be actionable after hard safety/economics checks; 4x/5x remain adaptive quality promotions.
- The observed “favoured thesis but leverage not_actionable solely due signal_quality_too_low” wall should be materially reduced.

Still residual:

- The project remains a recommender + preflight layer, not a live OMS/EMS.
- External executor must still re-check wallet balance, open positions, Bybit metadata, qty step, min notional, margin, leverage, current price, account mode, order/fill state and reconciliation immediately before any real bot creation.
- Calibration/outcome labels remain proxy-based and can be pessimistic or optimistic under regime shifts.
- `MIN_CONF_TO_RECOMMEND=0.62` and `MIN_SCORE_TO_RECOMMEND=0.14` in `.env.example` are stricter than runtime code defaults. I did not lower them; if the operator later wants more recommendations, that is an explicit risk-policy change, not a bug fix.

## Files changed

- `app/recommender.py`
- `tests/test_iteration180_operator_minimum_floor.py`
- `docs/AUDIT_REPORT_2026-06-15_OPERATOR_MINIMUM_FLOOR.md`
- `docs/STATIC_SCAN_2026-06-15_OPERATOR_MINIMUM_FLOOR.txt`
