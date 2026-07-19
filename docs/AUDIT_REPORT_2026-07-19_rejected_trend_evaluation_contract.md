# Audit report: rejected trend evaluation contract

**Release:** Bybit Recommender v1.4.2  
**Date:** 2026-07-19  
**Scope:** `directional_trend/neutral` lifecycle, persistence, API/UI projection, history, outcomes, training, router, execution, documentation and release verification.

## Executive conclusion

The v1.4.1 UI correctly stopped calling a neutral trend candidate a grid, but the underlying lifecycle still represented `directional_trend + neutral` too much like a malformed position. That produced derivative direction/level/geometry blockers, permitted invalid rows to appear in positional history and left insufficiently explicit boundaries for outcome scheduling, training and execution.

Version 1.4.2 introduces a durable two-kind contract:

- `strategy_recommendation` — a formed, strategy-native candidate;
- `trend_evaluation_rejected` — a preliminary trend analysis that did not establish LONG or SHORT.

A rejected evaluation has no position semantics. It owns no entry, TP, SL, sizing, trade plan, outcome root, observability schedule, history point, training sample or execution lifecycle. The one causal reason is `TREND_DIRECTION_UNCONFIRMED`.

## Confirmed defects

### High — neutral trend was represented as a malformed position

The previous branch could preserve `bot_type=directional_trend`, `direction=neutral` and then run position-geometry checks. The operator consequently saw a cascade such as:

- `DIRECTIONAL_TREND_DIRECTION_INVALID`;
- `DIRECTIONAL_TREND_LEVELS_MISSING`;
- `DIRECTIONAL_TREND_GEOMETRY_INVALID`.

These were not independent failures. The upstream fact was that no LONG/SHORT position existed, so TP/SL geometry should never have been constructed or validated.

### High — invalid evaluations could enter lifecycle-adjacent projections

Without a durable candidate-kind discriminator, downstream code had to infer intent from `bot_type`, direction, status and nested JSON. This made it possible for neutral trend rows to be treated as candidates by history, outcome scheduling, router or execution code unless every consumer repeated the same inference correctly.

### Medium — legacy databases lacked a repairable indexed discriminator

Existing rows needed an additive migration and idempotent startup repair. Merely changing frontend wording would not correct old roots, eligibility flags or waiting schedules.

### Medium — health could not explicitly detect retained outcomes attached to rejected evaluations

Immutable outcomes must not be silently deleted. At the same time, an outcome attached to a rejected preliminary evaluation is not valid evidence for a formed trend strategy and must fail semantic-integrity readiness.

## Implemented contract

### Candidate kind

`recommendations.candidate_kind` is a new indexed field:

```text
strategy_recommendation
trend_evaluation_rejected
```

Canonicalization is fail-closed: any `directional_trend` row whose direction is not `long` or `short` is classified as `trend_evaluation_rejected`, even if stale nested metadata claims it is a strategy recommendation.

### Recommender state machine

```text
preliminary trend evaluation
  ├─ direction = long/short
  │    └─ strategy_recommendation
  │       └─ build entry, TP, SL, sizing and single-position plan
  └─ direction unresolved/neutral
       └─ trend_evaluation_rejected
          └─ no position contract
```

The rejected branch persists bounded diagnostic observations separately but exposes only one operator reason: `TREND_DIRECTION_UNCONFIRMED`.

### Persistence and startup repair

`init_db()` now:

1. adds `candidate_kind` to an existing SQLite database;
2. creates `idx_reco_candidate_kind_ts`;
3. classifies legacy neutral trend rows as rejected;
4. clears `is_outcome_label_root`, outcome eligibility and policy-evaluation eligibility;
5. sets sample role to excluded;
6. removes a waiting observability row when no immutable outcome exists;
7. retains existing immutable outcomes for audit;
8. reports retained rejected-evaluation outcomes through semantic-integrity health.

Fresh SQLite and PostgreSQL reference schemas include the same field and index.

### Outcome and training exclusion

Rejected evaluations are excluded from:

- outcome-root materialization;
- `reco_outcome_observability` scheduling;
- current outcome/training joins;
- directional binary calibration;
- first-touch softmax fitting;
- recommendation history price geometry.

The training boundary also rejects explicit neutral direction and rejected candidate kind.

### Profitability router

Router identity is now:

```text
strategy-profitability-router-v3
```

Before monetary utility is evaluated, a candidate must be `candidate_kind=strategy_recommendation`. A neutral trend evaluation cannot compete against grid or a valid LONG/SHORT trend plan.

### API and execution

The recommendation detail payload exposes `candidate_kind`. For a rejected evaluation it returns:

- `status=no_trade`;
- no TP/SL or trade plan;
- `outcome_tracking.state=not_applicable`;
- no outcome root;
- `position_created=false`;
- `outcome_scheduled=false`;
- `included_in_training=false`.

Materialization is rejected before live-price or Bybit instrument validation.

### Operator UI

The Details card now says:

```text
Проверка тренда отклонена
Направление не подтверждено
```

It does not show the row as a position, neutral grid or failed TP/SL geometry. The history button is absent. The card explicitly states that no position, outcome or training row was created.

## Database compatibility

The schema change is additive. No manual SQL is required. PostgreSQL schema/migration text is kept in parity with SQLite bootstrap.

Rollback to v1.4.1 does not require dropping the new column. v1.4.1 can ignore it. Startup repair changes to invalid neutral trend eligibility are intentionally not reversed because they prevent statistically invalid samples.

## Red → green evidence

The new regression file was first executed against an unmodified v1.4.1 copy.

**RED:**

```text
4 failed, 1 passed
```

Observed failures included:

- missing `candidate_kind` in trend params;
- rejected evaluations still appearing in trend history;
- missing candidate kind in Details API;
- missing distinct rejected-evaluation frontend contract.

After the production changes, the expanded iteration 271 suite contains 11 tests and passes:

```text
11 passed
```

It covers generation, existing-database upgrade/repair, fresh migrations, history, Details, frontend semantics, training exclusion, router rejection and execution blocking.

## Regression results

### Baseline v1.4.1

Collected and executed in 16 non-overlapping deterministic batches:

```text
1280 collected
1280 passed
0 failed
```

### Post-change v1.4.2

Collected and executed in 16 non-overlapping deterministic batches:

```text
1291 collected
1291 passed
0 failed
0 skipped
0 errors
```

Batch node totals exactly equal collection totals.

### Focused compatibility

```text
67 DB/API/UI/docs tests passed
31 rejected-evaluation + PostgreSQL dialect/locking tests passed
compileall passed
Node syntax passed
```

`pip check` reports the pre-existing shared-environment conflict: MoviePy 2.2.1 requires Pillow below 12 while Pillow 12.2.0 is installed. It is unrelated to this repository change.

## Documentation artifacts

Updated:

- README and CHANGELOG;
- trading logic, architecture, modules, scenarios and known risks;
- operator infographic source and PNG;
- operator DOCX/PDF;
- historical audit prompt bridge;
- canonical iterative Markdown prompt;
- embedded root iterative PDF prompt.

The operator DOCX/PDF contains 18 rendered pages and was visually inspected page by page. The embedded iterative PDF contains 20 pages and was visually inspected page by page. No clipping, overlap, blank content page or broken Cyrillic glyph was observed.

## Modified production areas

- `app/recommender.py`
- `app/db.py`
- `app/main.py`
- `app/strategy_router.py`
- `app/trend_events.py`
- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `migrations/init.sql`
- `migrations/init_postgres.sql`

## Tests

- added `tests/test_iteration271_rejected_trend_evaluation.py`;
- synchronized exact version/model/router assertions and legacy UI expectations where the old behavior was deliberately replaced.

## Remaining risks

- A high rejected-evaluation rate may indicate real market ambiguity or an overly strict direction classifier. It should be monitored as diagnostic coverage, not counted as failed trades.
- Existing immutable outcomes attached to legacy neutral trend rows remain auditable and cause health to fail closed; remediation requires investigation rather than automatic deletion.
- The new boundary prevents invalid evidence but does not prove profitability of valid LONG/SHORT trend recommendations.
- Live PostgreSQL integration was not run without an explicitly disposable DSN; static dialect, migration and locking coverage passed.
- No real Bybit private order was submitted. The project remains recommendation/audit-only.

## Recommended commit

```text
fix(trend-contract): reject directionless trend evaluations before position lifecycle

- persist candidate_kind for formed strategies and rejected trend evaluations
- collapse neutral trend to TREND_DIRECTION_UNCONFIRMED without TP/SL geometry
- exclude rejected evaluations from outcomes, history, training, routing and execution
- repair legacy neutral trend rows during database bootstrap
- expose fail-closed integrity counters and strategy-native Details semantics
- update operator documentation, infographic and iterative PDF prompt
```
