# Audit report — invalid directional TP/SL UI fail-closed patch

Date: 2026-06-12
Scope: Bybit Linear USDT futures grid recommendation system; directional semantics, TP/SL display, operator UI, cache coherency, tests, static checks.

## Executive summary

The project already contained a centralized directional model in `app/trading_semantics.py` and regression tests for long/short TP/SL, PnL, Bybit `side`, `reduceOnly`, `closeOnTrigger`, `positionIdx`, tick/qty/min-notional validation, runtime risk caps, funding checks and recommendation freshness. The additional re-audit focused on fail-open gaps where a malformed backend/API payload could still make the operator UI display directional TP/SL values.

One medium-severity UI/UX safety issue was fixed. If backend `directional_exit_levels` existed but failed geometry validation, the UI previously fell back to local kill-switch mapping and could still display a directional Take Profit / Stop Loss pair. This was better than blindly trusting backend data, but still not fail-closed: for a malformed short payload it could keep showing a TP/SL-looking pair instead of making the directional exit unusable. The UI now suppresses directional TP, renders only lower/upper kill-switch bounds, labels the state as `Directional TP blocked`, and preserves the invalid-geometry warning text. Static asset cache keys were bumped to prevent stale browser JS.

## Findings and fixes

| ID | Severity | Area | File / region | Problem | Trading / financial risk | Fix | Tests |
|---|---:|---|---|---|---|---|---|
| IFX-001 | Medium | Frontend/UI TP/SL rendering | `app/ui/static/app.js`, `operatorExitLevelsFromBackend()` | When backend directional exit payload was present but invalid, UI fell back to locally mapped kill-switch TP/SL. This could still render a directional-looking TP/SL pair despite backend geometry failure. | Operator might copy a misleading TP/SL pair into Bybit instead of stopping and treating the payload as unsafe. This is especially dangerous for short grids, where TP below entry and SL above entry are direction-sensitive. | Invalid backend directional payload now renders `takeProfitValue: "—"`, `takeProfitLabel: "Directional TP blocked"`, and `stopLossValue` as raw lower/upper kill-switch bounds only. It no longer displays local directional TP/SL after backend geometry failure. | `tests/test_iteration157_ui_invalid_exit_failclosed.py`; updated `tests/test_iteration155_deep_directional_risk_patch.py`. |
| IFX-002 | Low | Static asset cache coherency | `app/ui/static/index.html` and UI cache tests | JS changed but cache key was still `manual-ui-v29`. | Browser could keep old operator UI and continue rendering the previous fail-open fallback. | Bumped CSS/JS query key to `manual-ui-v30`; updated cache-key regression tests. | Existing UI cache tests + `test_static_asset_cache_key_bumped_after_invalid_exit_failclosed_patch`. |

## Trading semantics reviewed

The following areas were checked by direct code review and regression tests:

- `app/trading_semantics.py`: canonical long/short/neutral exit mapping; long TP above entry and SL below entry; short TP below entry and SL above entry; neutral grid has no directional TP; Bybit one-way order side mapping; protective exits are reduce-only and close-on-trigger.
- `app/grid_math.py`: linear USDT long/short PnL signs; margin; approximate liquidation price; liquidation buffer direction; conservative grid leg economics.
- `app/main.py`: Bybit metadata validation; tick size, qty step, min order quantity, min notional, leverage step; range/kill-switch geometry; directional exit validation; runtime size/leverage/notional/margin cap re-checks; execution-time freshness, market, funding and shock blocks.
- `app/recommender.py`: range construction, grid count/step consistency, cost model, funding cost treatment, liquidation buffer estimates and operator trade plan generation.
- `app/ui/static/app.js`: operator detail fields; long/short labels; short TP/SL display; invalid backend payload fail-closed rendering; Bybit price formatting; cache-busting.

## Added / changed tests

- Added `tests/test_iteration157_ui_invalid_exit_failclosed.py`:
  - verifies invalid backend directional TP/SL is not rendered as local fallback mapping;
  - verifies `Directional TP blocked`, `takeProfitValue: "—"`, lower/upper kill-switch-only display and cache key `manual-ui-v30`.
- Updated `tests/test_iteration155_deep_directional_risk_patch.py` to lock the new fail-closed wording.
- Updated UI cache-key assertions from `manual-ui-v29` to `manual-ui-v30` in existing UI regression tests.

## Checks performed

| Check | Result |
|---|---:|
| Project map / file inventory | 190 source/docs/test files found at depth <= 4; UI assets present under `app/ui/static/`. |
| Static semantic grep | Completed for `tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`. |
| `node --check app/ui/static/app.js` | PASS |
| `python -m compileall -q app tests` | PASS |
| Targeted directional/UI tests | PASS — 46 passed |
| Core tests (`test_api.py`, `test_grid_linear_economics.py`, `test_logic.py`, `test_sentiment_pipeline.py`) | PASS — 153 passed |
| Iteration tests, chunk 1 (`test_iteration100`–`test_iteration130`) | PASS — 130 passed |
| Iteration tests, chunk 2 (`test_iteration131`–`test_iteration66`) | PASS — 132 passed |
| Iteration tests, chunk 3 (`test_iteration67`–`test_iteration99`) | PASS — 128 passed |
| Total pytest coverage executed by file groups | PASS — 543 passed |

A single monolithic `python -m pytest -q` invocation was also attempted, but it exceeded the execution timeout in this sandbox before finishing. The same collected test files were then executed in deterministic chunks, covering all 543 collected tests successfully.

## Checks not performed / unavailable

- `npm test` / `yarn test`: not applicable; no `package.json` / JS test runner configuration exists in the project.
- Lint/type checks: no `pyproject.toml`, `setup.cfg`, `tox.ini`, `pytest.ini`, `mypy`, `ruff` or equivalent configured project-level checker was found. Python compile and pytest were used as the available quality gates.
- Live Bybit API calls / order placement: not performed. The project is a recommender/operator layer; live credentials and exchange state were not available in the sandbox. Bybit semantics were validated through existing and added deterministic tests.

## Residual risks

1. The system is still an operator/recommendation layer, not a full OMS. Any external executor must re-check Bybit account mode, position mode, available balance, actual open positions, active orders, reduce-only behavior and exact instrument filters immediately before order placement.
2. Bybit liquidation prices are estimated conservatively; exact liquidation depends on risk tier, account equity, margin mode, mark price and maintenance margin. The current code correctly documents this, but it should not be treated as exchange-exact.
3. Existing grid bot creation is UI/link-driven. The audit could validate UI semantics and preflight blocks, but not actual operator behavior in Bybit’s external interface.
4. Hidden browser cache/proxy layers outside the app could still serve stale assets if deployment infrastructure ignores query-string cache busting. The app-level cache key was bumped.

## Modified files

- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `tests/test_iteration122_ui_detail_badge_fit.py`
- `tests/test_iteration129_ui_single_product_simplification.py`
- `tests/test_iteration132_operator_details_compaction.py`
- `tests/test_iteration133_operator_details_minimal_llm.py`
- `tests/test_iteration134_operator_position_size_details.py`
- `tests/test_iteration135_operator_bot_lifetime_details.py`
- `tests/test_iteration139_ui_no_trade_not_hard_blocker.py`
- `tests/test_iteration146_bybit_chart_url_and_ui_hardening.py`
- `tests/test_iteration147_short_tp_sl_ui_hardening.py`
- `tests/test_iteration149_operator_decision_panel.py`
- `tests/test_iteration151_operator_distance_and_ui_failclosed.py`
- `tests/test_iteration155_deep_directional_risk_patch.py`
- `tests/test_iteration157_ui_invalid_exit_failclosed.py`
- `docs/AUDIT_REPORT_2026-06-12_INVALID_EXIT_FAILCLOSED.md`
