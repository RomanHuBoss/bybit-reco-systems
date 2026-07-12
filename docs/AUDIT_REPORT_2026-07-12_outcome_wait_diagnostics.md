# Bybit Recommender v1.0.38 — Outcome wait diagnostics

## 1. Input
- ZIP: `bybit-reco-systems-1.0.37-settled-funding-outcomes.zip`
- SHA-256: `6da839c8c627f981a6eb0f7f4a7652300064b09b036901cc9b0ac39942265d51`
- Source version: `1.0.37`
- New version: `1.0.38`
- Outcome contract: unchanged, `grid_label_v18`

## 2. User-observed evidence
The supplied decision-log excerpt contains:
- `OUTCOME_SKIP_INVALID_GRID_CONTRACT` at Unix `1783883462` = `2026-07-12 19:11:02 UTC`;
- `OUTCOME_LABEL_VERSION_RESET` at Unix `1783883482` = `2026-07-12 19:11:22 UTC`.

Therefore the displayed skip rows were written 20 seconds before the v18 reset. They were not produced by the newly bootstrapped v18 process. The most likely operational explanation is a still-running previous process during deployment or a log view combining rows from immediately before and after restart.

The reset itself is expected: 89 `grid_label_v17` proxy outcomes were deleted once; recommendations, bot lifecycle, trades and market data were not deleted.

## 3. Confirmed defect
**ID:** OUTCOME-DIAG-226  
**Severity:** medium  
**Type:** confirmed defect

`_grid_outcome()` used `None` for both:
1. permanent persisted-contract failures; and
2. transient absence of a required settled funding row while the collector was still backfilling `/v5/market/funding/history`.

`compute_outcomes_once()` mapped every `None` to `OUTCOME_SKIP_INVALID_GRID_CONTRACT`. As a result, a normal fail-closed wait looked like corrupt grid mathematics. The same recommendation could emit the same row each outcome cycle.

Financial impact: no bad outcome was inserted; P&L/calibration remained fail-closed. Operational impact: high diagnostic ambiguity, false incident signal, decision-log noise and inability to distinguish collector lag from damaged recommendation payloads.

## 4. Acceptance criteria
1. Missing required settlement with non-zero inventory returns no label.
2. It reports `reason=missing_funding_settlement`, exact timestamp and position slots.
3. Worker action is `OUTCOME_WAIT_FUNDING_SETTLEMENT`, not invalid grid contract.
4. A repeated cycle inside one hour does not duplicate the same wait row.
5. Conflicting funding aliases remain permanent invalid-contract failures with issues.
6. Version increases to 1.0.38 without changing `grid_label_v18` or resetting v18 outcomes.

## 5. RED evidence
Command:
```bash
python -m pytest -q tests/test_iteration226_outcome_wait_diagnostics.py
```

Original v1.0.37 result:
```text
TypeError: _grid_outcome() got an unexpected keyword argument 'diagnostics'
version 1.0.38 assertion failed
3 failed
```

## 6. Implementation
### Production
- `app/outcomes.py`
  - optional structured diagnostic output while retaining the existing tuple/`None` return API;
  - transient reasons `missing_funding_settlement` and `funding_settlement_history_unavailable`;
  - permanent reasons for funding alias conflict, grid-count conflict, invalid range, entry outside range, kill-switch geometry and topology;
  - `OUTCOME_WAIT_FUNDING_SETTLEMENT` mapping;
  - per-rec/action cooldown query.
- `app/main.py`
  - FastAPI version `1.0.38`;
  - outcome label remains `grid_label_v18`.

### Tests
- Added `tests/test_iteration226_outcome_wait_diagnostics.py` (4 tests).
- Updated version-only assertions in historical iteration tests from 1.0.37 to 1.0.38; mathematical expectations were unchanged.

### Documentation
- `README.md`
- `CHANGELOG.md`
- `docs/KNOWN_RISKS.md`
- `docs/MODULES.md`
- `docs/SCENARIOS.md`
- `docs/ARCHITECTURE.md`

Operator DOCX/PDF/PNG were not changed because trading actions, sizing, statuses and preflight semantics did not change.

## 7. GREEN evidence
```text
4 passed in 0.68s
44 related outcome/funding tests passed
```

Full suite collection: `1005` nodes. Monolithic run timed out after partial progress without a failure summary and was not counted. Exhaustive non-overlapping run:
```text
252 + 251 + 251 + 251 = 1005
1005/1005 passed
```

Additional checks:
- compileall: passed;
- Node syntax: passed;
- PostgreSQL/dialect/write-retry subset: 20/20 passed;
- ruff: unavailable;
- pip check: pre-existing MoviePy/Pillow conflict.

## 8. Database/API/config compatibility
- No schema change.
- No migration action.
- No API or frontend contract change.
- No `.env` change.
- `grid_label_v18` unchanged, so upgrading v1.0.37 -> v1.0.38 does not trigger `OUTCOME_LABEL_VERSION_RESET`.

## 9. Residual risks
- A historical outcome remains unavailable until the matching settled funding row is present. This is intentional fail-closed behavior.
- Public funding-history/network errors can delay labels; inspect `COLLECT_ERROR` with `field=funding_history`.
- Other path-ambiguous outcomes still return unavailable; v1.0.38 improves their reason where covered but does not make unknowable OHLC paths knowable.

## 10. Rollback
Stop the application and restore the v1.0.37 code. Database rollback is not required because no schema or outcome-label version changed.
