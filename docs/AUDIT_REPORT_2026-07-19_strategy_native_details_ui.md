# Audit report: strategy-native semantics in «Детали»

**Release:** Bybit Recommender v1.4.1  
**Date:** 2026-07-19  
**Scope:** operator UI, recommendation-details API projection, strategy-specific remediation, Russian localization, regression compatibility, documentation and release integrity.

## 1. User-visible symptom

The supplied screenshot showed one `directional_trend` candidate as a mixture of two strategies:

- the subtitle contained **«Нейтральная сетка»**;
- the same card simultaneously contained **«Направленный тренд · одна позиция»**;
- the blocker list consisted of `DIRECTIONAL_TREND_*` reasons;
- the operator remediation could tell a trend candidate not to run a grid.

This was a real semantic defect, not merely a cosmetic preference.

## 2. Root-cause analysis

### 2.1 Strategy-independent interpretation of `direction=neutral`

**Severity: High — confirmed.**

The frontend used one generic direction formatter for every bot type. It interpreted `neutral` as **«Нейтральная сетка»** even when the row was `bot_type=directional_trend`.

The database row itself was not merged with a grid row. The UI mislabeled the trend row because direction semantics were evaluated without the strategy family.

Correct contract:

- `futures_grid + neutral` → valid **neutral grid**;
- `directional_trend + neutral` → **direction not determined**, therefore an invalid/unconfirmed trend candidate;
- `directional_trend + long|short` → a directional single-position candidate.

### 2.2 Cross-strategy operator remediation

**Severity: High — confirmed.**

The backend operator-action generator detected the word `trend` in negative-factor text and generated the grid-specific action `AVOID_GRID_IN_STRONG_TREND` without first checking `bot_type`. Consequently, a rejected trend candidate could receive instructions intended only for `futures_grid`.

### 2.3 Reprocessing already-localized operator text

**Severity: Medium — confirmed.**

The frontend passed backend-localized action titles/details through an additional generic humanizer. This could corrupt mixed Russian/technical text and blur the ownership of the action.

### 2.4 Duplicate concrete guard codes

**Severity: Low/Medium — confirmed.**

The same machine guard could arrive from persisted blocks and live validation with different explanatory text. Deduplication by full message allowed duplicate cards for one concrete code.

## 3. What was not broken

The audit did **not** find database-level coalescing of grid and trend recommendations:

- grid and trend remain separate rows with separate `rec_id`, `bot_type`, publication lineage and outcome lineage;
- the details API returns the requested recommendation only;
- a grid details response does not contain `DIRECTIONAL_TREND_*` validation codes;
- a trend details response retains its own trend guards.

Therefore, the screenshot reflected a projection/presentation defect and cross-strategy remediation leak, not corruption of the recommendation table.

## 4. Implemented corrections

### 4.1 Strategy-native direction semantics

Added frontend helpers:

- `strategyDirectionRu(botType, direction)`;
- `strategyDirectionBadge(botType, direction)`.

They are now used in:

- recommendation cards and tables;
- the details header;
- recommendation history;
- outcome, health and journal projections where a strategy-bound direction is shown.

A neutral trend candidate is rendered as:

> Направленный тренд · одна позиция · Направление не определено

It is never rendered as a neutral grid.

### 4.2 Strategy-first Details header

The details header now presents the strategy family before direction:

- grid: **Фьючерсная сетка · Нейтральная сетка/LONG/SHORT**;
- trend: **Направленный тренд · одна позиция · LONG/SHORT/Направление не определено**.

An invalid neutral trend row receives the explicit title **«Trend-кандидат отклонён»** and explanatory text stating that it is a separate candidate, not a grid.

### 4.3 Strategy-owned operator actions

Backend remediation now branches by `bot_type`.

Trend-only actions include:

- `WAIT_FOR_CONFIRMED_TREND_DIRECTION`;
- `REBUILD_DIRECTIONAL_TREND_PLAN`;
- `REBUILD_DIRECTIONAL_TREND_CONTRACT`;
- `WAIT_FOR_TREND_FIRST_TOUCH_EVIDENCE`.

Grid-only actions, including `AVOID_GRID_IN_STRONG_TREND`, cannot be attached to `directional_trend` rows.

Legacy recommendations without `bot_type` default to `futures_grid`, because all pre-trend historical rows belong to the grid-era contract.

### 4.4 Localization and deduplication

- Added exact Russian explanations for the common directional-trend and grid execution guard codes.
- Already-localized backend action titles/details are rendered directly.
- Concrete machine guards are deduplicated by code; generic warning buckets remain message-sensitive.
- Machine codes remain visible for auditability, while their operator explanation is Russian.

## 5. Red → green evidence

The new regression suite was first executed against the unmodified v1.4.0 code.

**RED:**

```text
3 failed, 2 passed
```

Failures proved that:

- strategy-aware direction functions were absent;
- UI locations still used the generic direction badge;
- the trend candidate received only `AVOID_GRID_IN_STRONG_TREND`.

After the corrections, the expanded suite contains six tests and passed twice:

```text
6 passed
6 passed
```

## 6. Browser-level verification

A headless Chromium smoke test rendered actual `buildDetailsHtml()` output using API payloads for two recommendations on the same symbol:

- `R-trend`: `directional_trend`, `direction=neutral`, blocked;
- `R-grid`: `futures_grid`, `direction=neutral`.

Assertions:

```text
trend contains «Нейтральная сетка»: false
trend contains «Направление не определено»: true
trend contains grid-specific action: false
grid contains «Нейтральная сетка»: true
grid contains DIRECTIONAL_TREND_*: false
browser JavaScript errors: 0
```

The rendered cards were visually inspected. The strategy families and their blockers/actions are now unambiguous.

## 7. Regression and compatibility results

### Baseline v1.4.0

Exact `pytest --collect-only` count:

```text
1274 tests
```

Sixteen non-overlapping deterministic batches:

```text
1274 passed
0 failed
```

### Post-change v1.4.1

Exact `pytest --collect-only` count:

```text
1280 tests
```

Sixteen non-overlapping deterministic batches:

```text
1280 passed
0 failed
```

Focused database, API, GUI, documentation and PostgreSQL-dialect suite:

```text
84 passed
```

Additional checks:

- Python `compileall`: passed;
- Node syntax check for `app.js`: passed;
- new regression suite executed twice: passed;
- browser smoke: no JavaScript errors.

Environment observations:

- `ruff` is not installed in the validation environment;
- `pip check` reports the pre-existing external dependency conflict: MoviePy 2.2.1 requires Pillow `<12`, while Pillow 12.2.0 is installed.

## 8. Database and configuration impact

No database schema changes were required.

No `.env` changes were introduced.

No manual migration is required. Existing v1.4.0 databases can be used directly.

## 9. Documentation

Updated:

- `README.md`;
- `CHANGELOG.md`;
- architecture, trading logic, modules, scenarios and known-risks documents;
- operator infographic;
- operator DOCX and PDF;
- Markdown iterative protocol;
- embedded `Bybit_Recommender_Iteration_Prompt.pdf`.

Visual inspection completed:

- operator instruction: 17 pages;
- iterative PDF prompt: 34 pages;
- infographic: checked at final resolution.

## 10. Residual risks

- Machine codes remain in English by design because they are stable diagnostic identifiers. The accompanying explanations are localized.
- A recommendation can legitimately be a rejected trend candidate while a separate grid candidate exists for the same symbol. The UI now states this explicitly, but operators must still select the correct row.
- Live Bybit private-order operations were not performed; the project remains recommendation/audit-only.

## 11. Rollback

Rollback requires only replacing the application files with v1.4.0 and restarting the service. Database rollback is unnecessary because v1.4.1 does not alter the schema.

## 12. Suggested commit

```text
fix(details): isolate grid and trend operator semantics

- interpret neutral direction by strategy family
- prevent grid remediation from leaking into trend candidates
- keep strategy-specific blockers and actions separate in Details
- preserve legacy grid rows without bot_type
- localize and deduplicate concrete operator guards
- update UI regressions, operator docs and iterative prompt
```
