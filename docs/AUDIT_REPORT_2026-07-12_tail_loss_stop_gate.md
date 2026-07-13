# Bybit Recommender v1.0.39 — Tail-loss exact-evidence stop gate

## 1. Название итерации

Tail-loss exact-evidence stop gate: устранение fail-open поведения при отрицательном cumulative net PnL и высоком win rate.

## 2. Входной ZIP

`bybit-reco-systems-main(1).zip`

## 3. SHA-256 входного ZIP

`62ff928b141c3da009edb52e88e092a2754e98f55a3b241e5b5e53c9ac92c252`

## 4. Исходная версия

`1.0.38`, source of truth: `FastAPI(..., version="1.0.38")` в `app/main.py`.

## 5. Новая версия

`1.0.39` — patch release. API, schema, env и outcome label не изменены; `OUTCOME_LABEL_VERSION=grid_label_v18` сохранён.

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender:

- root: `bybit-reco-systems-main`;
- `futures_grid`, Bybit `category=linear`, USDT perpetual;
- recommendation/audit service, не OMS/EMS;
- SQLite + PostgreSQL compatibility layer;
- FastAPI в `app/main.py`, frontend в `app/ui/static/`;
- canonical direction helpers в `app/trading_semantics.py`;
- все обязательные production, test, migration и operator artifacts присутствовали;
- статический поиск private order create/amend/cancel endpoints: ничего не найдено.

ZIP содержал 268 entries, один root, без absolute paths, `../`, symlinks, duplicate paths и nested archives. В исходном архиве не найдено `.env`, runtime DB, bytecode/cache или virtualenv.

## 7. Цель итерации

После этой итерации система должна прекращать новый operator `executed`, когда достаточно большая независимая выборка exact execution evidence уже имеет отрицательный cumulative realised net PnL, даже если большинство отдельных ботов прибыльны и median PnL положительна.

Это containment gate, а не заявление о наличии alpha.

## 8. Критерии приемки

1. Cohort `7 × +1 USDT` и `1 × -100 USDT` после direction minimum sample получает `LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY`.
2. Positive cumulative PnL не блокируется этим predicate.
3. Negative cumulative PnL ниже minimum sample не блокируется этим predicate.
4. Five-consecutive-loss guard, publication-root dedupe и model-version scoping сохраняются.
5. API, DB schema, env и `grid_label_v18` не меняются.
6. Новый regression test сначала падает на pristine code, затем проходит после fix.
7. Все 1008 собранных test nodes проходят post-check; release artifacts и документация синхронизированы.

## 9. Прочитанные источники

Полностью или релевантными разделами проверены:

- `README.md`, `CHANGELOG.md`, requirements и `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports, включая live-validation, execution-evidence, total-PnL, funding и outcome integrity;
- `app/main.py`, `trading_semantics.py`, `grid_math.py`, `risk.py`, `recommender.py`, `calibration.py`, `outcomes.py`, `features.py`, `direction.py`, `regime.py`, `collector.py`, `bybit_client.py`, `db.py`, `db_backend.py`, `settings.py`, `llm_review.py`, `security.py`;
- frontend `app/ui/static/app.js`;
- релевантные execution evidence/live validation/risk/database regression tests;
- operator DOCX/PDF/PNG artifacts.

## 10. Карта затронутого data flow

`execution_evidence` → stopped `bot_instance` → `db.list_live_validation_records()` → publication-root/model-version filtering → `_live_validation_scope_summary()` → `_negative_expectancy_condition()` → direction/symbol/portfolio `LIVE_VALIDATION_*` block → `_execution_preflight()` → operator action returns block before `bot_instance` materialization.

Не затронуты recommendation generation, market-data ingestion, grid construction, sizing, proxy outcome computation, calibration fit, database schema и frontend API parsing.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `v22.16.0`;
- production Python files: 24;
- test files: 170;
- docs files: 50;
- frontend files: 3;
- migration SQL files: 2;
- API routes: 22, из них mutating: 6;
- background threads: collector, backfill, futures metadata, sentiment, recommender/outcomes, optional LLM reviewer;
- DB backends: SQLite and PostgreSQL/psycopg compatibility path;
- max pre-existing iteration number: 226;
- baseline collection: 1005 tests.

## 12. Baseline commands и результаты

- `python --version`: PASSED — Python 3.13.5.
- `node --version`: PASSED — v22.16.0.
- `python -m pip check`: FAILED because of host-environment dependency conflict: MoviePy requires Pillow `<12`, host has Pillow 12.2.0. Project dependencies were not modified.
- `python -m compileall -q app tests main.py`: PASSED.
- `python -m ruff check .`: UNAVAILABLE at baseline (`No module named ruff`).
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest --collect-only -q`: PASSED — 1005 collected.
- `python -m pytest -q`: PASSED — 1005 passed in 25.61 s, exit 0.

## 13. Подтверждённые defects/gaps

### BR-227-01 — fail-open tail-loss stop gate

- **Severity:** HIGH / priority P0 risk gate.
- **Type:** CONFIRMED DEFECT.
- **Original file/lines:** `app/main.py:4049-4059` in v1.0.38.
- **Function:** `_negative_expectancy_condition`.
- **Input:** eight independent validation-eligible stopped bots, same explicit model version and symbol/direction: seven `+1` outcomes and one `-100` outcome.
- **Computed metrics:** total `-93`, mean `-11.625`, median `+1`, positive rate `87.5%`.
- **Actual v1.0.38 behavior:** no negative-expectancy block, because the predicate additionally required negative median and positive rate below 50%.
- **Expected behavior:** after the predefined sample floor, negative cumulative exact net PnL must stop new execution regardless of how many small wins conceal a tail loss.
- **Violated invariant:** fail-closed operational risk gate; known aggregate loss was treated as executable because distribution-shape diagnostics were used as mandatory veto conditions.
- **Financial/trading impact:** a characteristic grid short-gamma/tail-loss pattern could continue consuming capital after exact evidence had already shown a cumulative loss.
- **Model/data impact:** none to stored labels; the defect was in consumption of authoritative exact execution evidence.
- **Why tests missed it:** existing tests covered consecutive losses and cohorts in which total, median and win rate all pointed negative. They did not use an asymmetric many-small-wins/one-large-loss distribution.
- **Reproducer:** `tests/test_iteration227_tail_risk_stop_gate.py`.
- **Residual risk after fix:** the gate still requires minimum samples and cannot prove profitability; it is a stop criterion, not an estimator of future expectancy.

### BR-227-G01 — profitability remains unproven

- **Severity:** HIGH as a decision limitation, not a newly introduced code defect.
- **Type:** DOCUMENTED LIMITATION / CONFIRMED GAP.
- The archive contains no authoritative live population of exact fills from which positive monetary expectancy can be established.
- Raw score/confidence are heuristic; fitted bot calibration targets proxy success, not a complete monetary utility distribution including tail loss and drawdown.
- Therefore the hypothesis “project is a priori unprofitable” cannot be proved from this ZIP, but the opposite claim — that it has a live edge — is also unsupported.

## 14. Неподтверждённые claims

- **“Проект априори убыточен.”** Not proven. Source code and proxy tests cannot establish live monetary expectancy without representative chronological exact execution evidence and a comparator/no-trade baseline.
- **“Все критичные ошибки найдены.”** Not claimed and not provable.
- **“Fix makes the strategy profitable.”** False; the fix only prevents one confirmed loss-continuation path.

## 15. План исправления

1. Add independent tail-loss regression test to pristine/red copy.
2. Demonstrate red on v1.0.38.
3. Keep sample floors, exact-evidence filtering, dedupe and model-version scoping.
4. Change only the sample-based negative-expectancy predicate: negative cumulative/mean exact PnL after sample floor is sufficient.
5. Keep median and positive rate as diagnostics in block payloads.
6. Synchronize code, tests, README, trading/risk/architecture/scenario docs and operator DOCX/PDF/PNG.
7. Bump patch version to 1.0.39 and build a clean verified ZIP.

## 16. Фактический diff по файлам

### Production

- `app/main.py`: corrected stop predicate, block messages/policy metadata, FastAPI version 1.0.39.

### Tests

- Added `tests/test_iteration227_tail_risk_stop_gate.py` (3 tests).
- Updated current-version assertions in iteration 213-226 release tests from 1.0.38 to 1.0.39; mathematical expectations unchanged.

### Frontend

- No JS/HTML/CSS runtime changes.
- Updated root operator infographic `how_to_trade.png`.

### Database/migrations

- No changes.

### Docs/operator artifacts

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- regenerated `docs/instrukciya_operatora_bybit_recommender.pdf`;
- updated `how_to_trade.png`;
- this audit report.

## 17. Red → green evidence

### Red

Command:

```text
python -m pytest -q tests/test_iteration227_tail_risk_stop_gate.py
```

Material output on pristine v1.0.38:

```text
AssertionError: assert 'LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY' in set()
1 failed, 2 passed in 1.21s
```

### Green

Command:

```text
python -m pytest -q tests/test_iteration227_tail_risk_stop_gate.py
```

Output after fix:

```text
3 passed in 0.97s
```

Repeated deterministic run: `3 passed in 0.91s`.

Relevant module suite:

```text
13 passed in 1.20s
```

## 18. Database/schema compatibility

- Schema unchanged; no migration or operator DB action required.
- Fresh SQLite bootstrap and second idempotent `init_db()` passed; 18 tables present and required evidence/outcome/funding tables found.
- Existing SQLite additive-upgrade test included in DB subset passed.
- PostgreSQL translation/locking/integrity/deadlock test subset: 33 passed.
- Live PostgreSQL integration: SKIPPED, because no explicitly disposable test DSN was supplied.

## 19. API compatibility

- No route, request field, response field or status contract removed.
- Existing `LIVE_VALIDATION_*_NEGATIVE_EXPECTANCY` codes retained.
- Block message and `policy` diagnostics are more explicit; behavior is intentionally stricter only when exact cumulative PnL is already negative after sample floor.

## 20. Config/env compatibility

No environment variable or default changed. `.env.example` unchanged. No user config action required.

## 21. Security boundary

- Recommendation/audit-only boundary preserved.
- No private Bybit order create/amend/cancel endpoints or SDK equivalents were added.
- No credentials, `.env`, DB or runtime logs are included in the release.
- Mutating actions remain under the existing authorization model.

## 22. Post-check commands и результаты

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest --collect-only -q`: PASSED — 1008 unique nodes.
- Monolithic `python -m pytest -q`: TIMED OUT after displaying 92% and no final summary; it is not counted as a pass.
- Exhaustive non-overlapping batched run covering the exact collected set:
  - batch 1: 252 passed;
  - batch 2: 252 passed;
  - batch 3a: 126 passed;
  - batch 3b: 126 passed;
  - batch 4: 252 passed;
  - union: 1008/1008 unique nodes passed, no overlap or omission.
- New test repeated: 3/3 passed twice.
- Relevant live-validation/risk suite: 13 passed.
- SQLite/PostgreSQL dialect/integrity subset: 33 passed.
- Fresh SQLite bootstrap: PASSED.
- `uvx ruff check .`: AVAILABLE post-baseline; FAILED with 21 historical findings outside this iteration (unused variables/imports, import ordering and ambiguous local names).
- `uvx ruff check tests/test_iteration227_tail_risk_stop_gate.py`: PASSED.
- `uvx ruff check app/main.py tests/test_iteration227_tail_risk_stop_gate.py --ignore F841`: PASSED; global pre-existing F841 findings were not mass-refactored.
- `python -m pip check`: retains the unrelated host MoviePy/Pillow conflict.
- Private-order endpoint scan: PASSED, none found.
- DOCX render: 6 pages, visually inspected.
- Regenerated PDF render: 6 pages, visually inspected.

## 23. Что не удалось проверить и почему

- No live PostgreSQL integration: no safely identifiable disposable DSN.
- No actual Bybit account/fill reconciliation: project boundary and no private credentials/evidence dataset supplied.
- No positive live edge/profitability: no representative exact execution dataset or chronological comparator.
- Monolithic post-check pytest did not return a summary in the harness; every collected node was instead executed through exhaustive deterministic batches.
- Host-wide `pip check` and global Ruff remain non-green for pre-existing environment/repository findings unrelated to this patch.

## 24. Остаточные риски

1. The system can lose before the exact-evidence minimum sample is reached.
2. Negative cumulative PnL is a conservative operational stop, not a statistical significance test; small negative totals around zero may be noise.
3. Conversely, absence of a block does not imply positive expectancy.
4. Proxy outcomes/calibration do not fully represent exact monetary tail loss, drawdown, queue/fill effects or regime-conditioned utility.
5. A future strategy version can start a separate cohort by design; model-version governance must prevent cosmetic version resets from evading evidence.
6. External executor must still verify wallet/account/risk-tier/open-order truth.

## 25. Rollback procedure

1. Stop the service.
2. Restore the previous v1.0.38 project directory or ZIP.
3. Restart with the same database and environment; no schema downgrade is required.
4. Be aware that rollback restores the confirmed fail-open tail-loss behavior. Prefer disabling operator execution rather than rolling back while aggregate exact PnL is negative.

## 26. Рекомендуемый следующий work package

Implement a **monetary walk-forward strategy validity report** based only on exact execution evidence:

- chronological, model-version-locked cohorts;
- net PnL, expected shortfall/tail loss, max drawdown and capital at risk;
- no-trade/simple-grid comparator;
- minimum sample and confidence interval/bootstrapped uncertainty;
- regime/symbol/direction segmentation without leakage;
- explicit status `INSUFFICIENT_EVIDENCE` instead of interpreting proxy win rate as edge.

This is the shortest path to answer whether the project is economically viable rather than merely internally consistent.
