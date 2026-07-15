# Audit iteration 250 — outcome liveness and minimum operator table

## 1. Input and versions

- Input ZIP: `bybit-reco-systems-1.0.61-operator-rr-metrics.zip`
- Input SHA-256: `a708cf51073dfefc3a67f71a30b31ce18476367a3d55bfe8fedec3b754b4f90d`
- Source version: `1.0.61`, from `FastAPI(version=...)` in `app/main.py`
- New version: `1.0.62` (backward-compatible patch)
- New regression number: `iteration250`
- Project fingerprint: PASSED. Bybit Recommender, `futures_grid`, Bybit Linear USDT perpetual, recommendation/audit-only, SQLite + PostgreSQL, canonical `app/trading_semantics.py`, frontend in `app/ui/static/`.

## 2. Iteration goal and acceptance criteria

After this iteration the service must continue collecting exact-policy shadow outcomes when the optional LLM reviewer is enabled, while actionable recommendations remain fail-closed behind the LLM verdict. The primary table must contain only the six fields agreed with the operator.

Acceptance criteria:

1. A risk-clean, explicitly eligible `shadow_no_trade` root matures without an LLM verdict.
2. An actionable root without an eligible LLM verdict is not labeled.
3. Matured eligible roots with no attempts produce `OUTCOME_WORKER_STALLED` in the read-only liveness contract.
4. The API publishes a stable additive `operator_summary` with Plan RR, empirical state, decision and one reason.
5. The primary table contains exactly: symbol, direction, Plan RR, empirical expectancy, decision, reason; all diagnostics remain in Details.
6. Bybit retCode `10006` honors the absolute reset timestamp.
7. An exact ticker miss disables a symbol only after public instrument metadata also confirms absence.
8. The complete offline test collection passes without weakening execution/risk gates.

## 3. Sources read

- User-provided iteration protocol dated 10 July 2026.
- README, CHANGELOG, requirements, `.env.example`.
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, operator infographic source and the five latest audit reports.
- `app/outcomes.py`, `db.py`, `db_backend.py`, `main.py`, `recommender.py`, `llm_review.py`, `collector.py`, `bybit_client.py`, settings, risk, grid and frontend files.
- User-provided PostgreSQL diagnostic CSV exports and UI screenshots.
- Official Bybit V5 rate-limit documentation for `10006` and reset headers.

## 4. Relevant data flow

`recommender publication -> outcome_policy/sample_role -> optional LLM reviewer -> outcome worker selection -> OHLCV proxy label -> observability -> calibration lineage -> empirical expectancy -> API operator_summary -> six-field frontend table`.

Collector path: `configured symbol -> batch ticker -> exact-symbol fallback -> instrument metadata confirmation -> temporary disable or ticker_missing -> OHLCV collection`.

## 5. Baseline environment and results

- Python: `3.13.5`
- Node: `v22.16.0`
- Production Python files: 24
- Test files: 194
- Documentation files: 72
- Frontend files: 3
- Migration SQL files: 2
- Baseline collection: 1126 tests
- Baseline `python -m pytest -q`: **1126 passed in 30.22s**
- Baseline `compileall`: PASSED
- Baseline `node --check`: PASSED
- Baseline `ruff`: UNAVAILABLE (`No module named ruff`)
- Baseline `pip check`: FAILED due host-environment conflict: MoviePy requires Pillow `<12`, installed Pillow is `12.2.0`. No project dependency was changed.

Archive validation: one project root, no traversal, external symlinks, duplicate paths or nested archives; `unzip -t` passed.

## 6. Confirmed defects and gaps

### I250-01 — HIGH — CONFIRMED DEFECT

**Location:** `app/outcomes.py:1614+`, `app/db.py:4022+`.

When `LLM_REVIEWER_ENABLED=1`, SQL required `llm_review.status='ok'` for every root. The reviewer intentionally processes only potentially actionable recommendations; explicit `shadow_no_trade` roots therefore could never receive a verdict and could never enter outcomes. PostgreSQL diagnostics showed matured eligible roots with `not_attempted`, while OHLCV coverage was sufficient.

**Impact:** closed bootstrap loop: no outcomes -> no empirical evidence -> no actionable recommendations -> no LLM-reviewed roots -> no outcomes.

**Fix:** one canonical eligibility predicate now permits only explicit, policy-evaluation-eligible, risk-clean `shadow_no_trade` roots to bypass the impossible LLM prerequisite. Actionable roots still require a completed eligible verdict.

### I250-02 — HIGH — CONFIRMED GAP

**Location:** `app/db.py:3180+`, `app/main.py:6554+, 6658+, 6946+`.

There was no direct health invariant distinguishing “label horizon not reached” from “matured eligible roots have never been attempted.”

**Fix:** read-only liveness payload with `matured_pending_total`, `unattempted_total`, oldest due age and state/code. `/api/v1/status`, Prometheus metrics and decision log expose `OUTCOME_WORKER_STALLED`. A mixed attempted/unattempted queue is reported as backlog rather than false stall.

### I250-03 — MEDIUM — CONFIRMED GAP

**Location:** `app/main.py:1437+`, `app/ui/static/*`.

DB diagnostics contained available Plan RR values while the displayed list contained dashes. The frontend depended on re-parsing several technical payload locations and the primary table was not the agreed one-glance decision surface.

**Fix:** additive `operator_summary` contract provides stable Plan RR, empirical state, decision and one primary reason. The table now has exactly six fields. Full confidence, risk, price, range, sizing, funding and model diagnostics remain in Details.

### I250-04 — MEDIUM — CONFIRMED DEFECT

**Location:** `app/bybit_client.py:167+`.

Retry for Bybit `10006` used only local exponential backoff/`Retry-After`, ignoring `X-Bapi-Limit-Reset-Timestamp` present in the response.

**Fix:** `10006` delay is at least the remaining exchange reset interval, bounded to 60 seconds, with existing retry behavior preserved.

### I250-05 — MEDIUM — CONFIRMED GAP

**Location:** `app/collector.py:500+`.

An exact-symbol ticker miss was repeatedly logged as `ticker_missing` even when the instrument might no longer exist. Conversely, disabling solely from an empty ticker would be unsafe.

**Fix:** after a successful exact ticker miss, public instrument metadata is queried. Only confirmed metadata absence triggers temporary `SYMBOL_DISABLED / INSTRUMENT_METADATA_ABSENT`; missing metadata capability or transport errors remain transient and fail closed.

### Not reclassified as new defects

- PostgreSQL OHLCV deadlock prevention from v1.0.60/v1.0.61 remains present and its regression tests pass.
- Low Plan RR values are not changed in this iteration; this iteration fixes outcome acquisition and presentation, not profitability.
- No live edge or production auto-execution is claimed.

## 7. RED -> GREEN evidence

New file: `tests/test_iteration250_runtime_liveness_operator_minimum.py`.

RED command on pristine code plus the new test:

```bash
python -m pytest -q tests/test_iteration250_runtime_liveness_operator_minimum.py
```

RED result:

```text
6 failed in 1.17s
assert 0 == 1
AttributeError: module 'app.db' has no attribute 'get_outcome_worker_liveness'
AttributeError: module 'app.main' has no attribute '_operator_summary_for_reco'
assert 0.25 >= 1.5
```

GREEN command after production changes:

```bash
python -m pytest -q tests/test_iteration250_runtime_liveness_operator_minimum.py
```

GREEN result, repeated deterministically:

```text
6 passed in 1.11s
6 passed in 1.08s
```

Relevant cross-module suite: **48 passed**. PostgreSQL translation/locking/liveness subset: **25 passed**.

## 8. Implementation diff

### Production

- `app/outcomes.py` — selection of LLM-ready actionable roots plus explicit safe shadow roots.
- `app/db.py` — canonical shadow/LLM eligibility and worker liveness.
- `app/main.py` — version 1.0.62, `operator_summary`, liveness status/metrics/logging.
- `app/bybit_client.py` — reset-aware `10006` retry.
- `app/collector.py` — metadata-confirmed temporary symbol disable.

### Frontend

- `app/ui/static/index.html` — exact six-column header and new cache key.
- `app/ui/static/app.js` — decision/reason rendering; Details button in symbol cell; stable summary fallback.
- `app/ui/static/styles.css` — compact decision/reason styling.

### Tests

- New `test_iteration250_runtime_liveness_operator_minimum.py` with six independent regressions.
- Existing version/cache-contract tests updated from 1.0.61/v46 to 1.0.62/v47.
- Iteration249 UI expectation updated because the user explicitly removed Risk buffer from the primary table; the metric remains in Details.

### Documentation

README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, DOCX/PDF operator instruction and `how_to_trade.png`.

### Database/migrations

No schema or migration changes.

## 9. Compatibility

- API: additive `operator_summary`; existing fields retained.
- Database: unchanged; fresh and existing SQLite initialization both produce 20 tables and are idempotent.
- PostgreSQL: SQL translation/locking tests passed; no live PostgreSQL integration was run because no explicitly disposable test DSN was provided.
- Configuration: no `.env` changes.
- Model/outcome lineage: model version, policy fingerprint logic and outcome label version are unchanged; existing matured roots can be processed without resetting evidence.
- Security/execution boundary: no private Bybit order endpoint or execution flow added.

## 10. Post-check results

- Collection: **1132 tests**.
- Monolithic run: TIMED OUT in the harness at approximately 82%; no failure had been emitted at timeout.
- Exhaustive deterministic batched run: 16 non-overlapping batches (`15 x 75 + 7`), union equals collected set: **1132 passed**.
- `compileall`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `pip check`: inherited host conflict, FAILED as described above.
- `ruff`: UNAVAILABLE.
- SQLite fresh init: 20 tables; repeated init: 20; v1.0.61 DB opened/initialized by v1.0.62: 20.
- Private order endpoint static scan: none.
- Main table header: exactly six agreed fields.
- DOCX: rendered to 11 pages and visually inspected; no clipping/overflow observed.
- PDF: regenerated from DOCX and rendered independently.
- Infographic: regenerated at 1600 x 1200 and visually inspected.

## 11. What was not verified

- Live PostgreSQL concurrency/integration against a disposable DSN: SKIPPED. The supplied diagnostics came from an operational database and were not used as a test target.
- Live Bybit network retry timing: SKIPPED; deterministic mocked response/header tests were used.
- Live execution profitability: outside project scope and not established.

## 12. Residual risks

- OHLCV outcomes remain proxy labels without exchange fill attestation.
- A large backlog may require several worker cycles due `outcomes_max_to_process`; it is reported as backlog after at least one attempt, not as a stalled worker.
- Temporary disable relies on the public instrument-info response and is retried after the existing TTL.
- Plan RR below one may still be unattractive; this release does not alter economics or gates.

## 13. Rollback

1. Stop v1.0.62.
2. Restore v1.0.61 application files.
3. Restart with the same DB and `.env`.

No DB rollback is required. Outcomes created by v1.0.62 are immutable audit rows and should not be deleted merely to roll back application code.

## 14. Recommended next work package

Observe one complete outcome horizon after deployment and compare `outcome_worker` liveness, newly created exact-policy outcomes, reason distribution and empirical cohort growth. Do not alter thresholds until the acquisition path is shown to advance reliably.
