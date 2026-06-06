# Audit report — Operator Decision Panel re-audit

Date: 2026-06-06
Scope: Bybit Linear USDT Futures Grid operator details panel, backend/API payload supporting the panel, Russian tooltips for exchange/quant abbreviations, and prompt-driven re-audit of trading/risk/UI semantics.

## Executive summary

The prior Details panel contained the core bot-creation fields, including entry price, but it was still too execution-form oriented: the operator could copy Bybit parameters without seeing enough decision-critical context in the same panel. The main high-risk gap was not TP/SL directionality anymore; it was the absence of a compact pre-launch decision sheet covering freshness, current price drift, grid-range position, preflight state, net grid economics, funding/execution costs, and liquidation buffer.

This patch adds a backend-derived `operator_decision_context` and renders it in two new top-level cards: `Цена и актуальность` and `Риск и экономика запуска`. `Цена входа` remains in the primary launch card and is also shown in the price-context card so the operator can both create the bot and judge whether the recommendation is still actionable. English exchange terms and abbreviations retained in the UI now have Russian tooltip explanations.

## Findings and fixes

| ID | Severity | Area | Files | Problem | Risk | Fix |
|---|---:|---|---|---|---|---|
| ODP-001 | High | UI / decision support | `app/ui/static/app.js` | Details panel showed bot launch parameters but lacked current price, recommendation age/TTL, price drift from entry, and distance to grid boundaries. | Operator could launch a stale recommendation or one whose current price had moved materially from the computed entry/range. | Added `Цена и актуальность` card with entry, current price, entry drift, range position, boundary distances, ticker age, recommendation age and TTL. |
| ODP-002 | High | Backend/API | `app/main.py` | UI had to infer decision-critical context from scattered raw `params`, `trade_plan`, `economics`, ticker and preflight data. | Frontend/backend divergence and stale/incorrect display of price/risk context. | Added `_operator_decision_context_for_reco()` and exposed `operator_decision_context` through recommendation list/detail augmentation. |
| ODP-003 | High | Risk-management / UI | `app/ui/static/app.js`, `app/main.py` | Details panel did not put liquidation buffer, estimated liquidation price, preflight state, and net grid economics into the operator-first area. | Operator could see entry/range/leverage but miss whether leverage/funding/costs made the bot unsafe. | Added `Риск и экономика запуска` card with preflight status, risk profile, liquidation buffer, estimated liquidation, net profit per grid, execution costs and funding risk. |
| ODP-004 | Medium | UX / terminology | `app/ui/static/app.js`, `app/ui/static/styles.css`, `app/ui/static/index.html` | English abbreviations and exchange terms such as LLM, Take Profit, Stop Loss, Funding and bps were visible without Russian explanations. | Operator may misunderstand a value or assume it is a direct probability/guarantee rather than an estimate. | Added `field-help` tooltip UI and Russian explanations for retained abbreviations/terms; table header `Ож. RR` was replaced with `Прибыль/риск` plus tooltip. |
| ODP-005 | Medium | UI cache coherency | `app/ui/static/index.html`, tests | Static asset cache key still referenced the previous UI version. | Browser could reuse stale JS/CSS and show the old Details panel after deployment. | Bumped cache key to `manual-ui-v27` and updated regression tests. |
| ODP-006 | Low | Tech diagnostics | `app/ui/static/app.js` | Technical modal did not include the new compact decision context. | Harder to debug discrepancies between backend context and rendered fields. | Added `operator_decision_context` to the tech payload. |

## Changed files

- `app/main.py`
  - Added `_pct_delta()`.
  - Added `_operator_decision_context_for_reco()`.
  - Added `operator_decision_context` into `_augment_reco_for_ui()` for list and detail APIs.
- `app/ui/static/app.js`
  - Added duration, price-status, preflight-status and risk-profile formatters.
  - Added `buildPriceFreshnessFields()`.
  - Added `buildRiskEconomicsFields()`.
  - Added tooltip-aware `fieldBox()`.
  - Preserved `Цена входа` in `Параметры запуска Bybit Futures Grid`.
  - Added `Цена и актуальность` and `Риск и экономика запуска` cards.
  - Reworded visible launch diagnostics to reduce unnecessary English.
- `app/ui/static/styles.css`
  - Added `field-help` styling.
  - Added styling for price freshness and risk/economics cards.
- `app/ui/static/index.html`
  - Bumped static cache key to `manual-ui-v27`.
  - Replaced table header `Ож. RR` with `Прибыль/риск` and Russian tooltip.
- `tests/test_iteration149_operator_decision_panel.py`
  - Added backend and UI regression tests for the new decision panel and tooltips.
- Existing UI-cache and string-regression tests updated from `manual-ui-v26` to `manual-ui-v27` and to the new Russian wording.

## Added tests

`tests/test_iteration149_operator_decision_panel.py`:

1. `test_backend_operator_decision_context_exposes_price_freshness_risk_and_economics`
   - Verifies backend `operator_decision_context` exposes entry/current price, range status, TTL, preflight state, net profit, execution cost, funding cost, liquidation buffer and risk profile.
2. `test_details_panel_keeps_entry_price_and_adds_price_actuality_and_risk_blocks`
   - Ensures Details panel contains `Цена входа`, price actuality fields and risk/economics fields.
3. `test_details_panel_tooltips_explain_abbreviations_and_english_exchange_terms`
   - Ensures retained terms/abbreviations have Russian tooltip explanations.
4. `test_static_asset_cache_key_bumped_after_decision_panel_update`
   - Guards against stale frontend asset caching.

## Prompt re-audit result

The prompt-required areas were rechecked after the patch:

- Long/short TP/SL semantics: unchanged from the previous hardening; backend `directional_exit_levels` remains the canonical source and existing regression tests still pass.
- Backend/UI consistency: improved. New panel fields are derived from backend `operator_decision_context`, not separately reconstructed only in JS.
- Risk management: improved visibility. Liquidation buffer, estimated liquidation, preflight status, and cost/funding economics are now in the operator-first panel.
- Bybit-specific constraints: existing `bybit_operator_guard` remains the execution readiness source; its status is summarized in the new risk/economics card.
- UI/UX: improved. The panel now better matches an operator decision sheet: first decision status, then rank diagnostics, price actuality, risk/economics, launch parameters, LLM, and blockers.
- Terminology: improved. Retained English terms/abbreviations have Russian explanations through `?` tooltips.
- Residual OMS/EMS risk: unchanged. The project remains recommendation/operator tooling, not a full private-order execution system.

## Static scan summary

Suspicious-term scan was run over `app` and `tests` after changes:

```text
tp           268
sl           128
stop         172
take         93
upper        286
lower        360
short        244
long         344
side         110
Buy          4
Sell         4
reduceOnly   4
kill         148
leverage     202
pnl          98
roi          0
risk         419
```

The scan is not itself proof of correctness, but it was used as a prompt checklist. No new directionality or TP/SL discrepancy was found in the modified panel code.

## Checks performed

```text
python3 -m compileall -q app tests
node --check app/ui/static/app.js
python3 -m pytest -q
```

Result:

```text
494 passed in 11.74s
```

Additional check:

```text
package.json: absent
npm/yarn tests: not applicable
configured lint/type checks: not found
```

## Residual risks

- Exact liquidation price remains approximate. The UI now labels it as an estimate; exact liquidation requires live Bybit risk tier, mark price, account margin and private account state.
- Current-price context depends on local ticker freshness. If ticker collection is down, the panel shows missing/stale price context rather than silently approving.
- Funding/cost values are model estimates and are not guaranteed realized results.
- This patch improves operator decision support but does not add private order placement, partial-fill handling, live OMS/EMS reconciliation, or account-aware liquidation calculation.
