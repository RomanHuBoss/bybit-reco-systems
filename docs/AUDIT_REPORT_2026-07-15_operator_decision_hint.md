# Audit iteration: compact operator decision hint

## 1. Iteration name

Operator decision hint compaction and diagnostic-boundary correction.

## 2. Input ZIP

`bybit-reco-systems-1.0.62-outcome-liveness-operator-minimum.zip`

## 3. Input SHA-256

`03953d600381b117efc0ad0deedaa7a7757ff6bb9c96145afbdcf8274e9e7c40`

## 4. Source version

`1.0.62`, from `FastAPI(..., version="1.0.62")` in `app/main.py`.

## 5. New version

`1.0.63` (backward-compatible patch).

## 6. Project fingerprint

Matched: Bybit Recommender; `futures_grid`; Bybit linear USDT perpetual; recommendation/audit-only; SQLite and PostgreSQL; FastAPI in `app/main.py`; frontend in `app/ui/static/`; canonical direction semantics in `app/trading_semantics.py`.

## 7. Goal

After this iteration, the operator table must show only the minimum decision surface. The final decision badge must expose one short human-readable reason as a hover/focus hint, while raw technical messages, codes and thresholds remain in Details.

## 8. Acceptance criteria

1. The table has exactly five visible columns: symbol, direction, Plan RR, empirical expectancy, decision.
2. There is no standalone reason column.
3. `no_trade` renders as `НЕ ТОРГОВАТЬ`; blocked renders as `ЗАБЛОКИРОВАНО`.
4. The decision badge has a short Russian `title` and accessible `aria-label`.
5. Known internal reason codes map to bounded operator phrases.
6. Unknown codes never leak the raw long diagnostic into the table.
7. The original message remains available additively as `primary_reason_detail` and in full Details diagnostics.
8. No trading gate, schema, policy, outcome lineage or execution boundary changes.

## 9. Sources read

README, CHANGELOG, requirements, `.env.example`, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, recent audit reports, `app/main.py`, frontend files, iteration 249/250 tests, operator DOCX/PDF and the user-provided screenshot.

## 10. Affected data flow

`recommendation reasons/blocks -> _operator_summary_for_reco -> reason-code translation -> additive API operator_summary -> operatorDecisionCell -> decision badge title/aria-label`.

The full diagnostics path remains unchanged: recommendation payload -> Details panel.

## 11. Baseline environment

- Python: `3.13.5`
- Node: `22.16.0`
- Source tests collected: `1132`
- Production Python files: `24`
- Test files before iteration: `195`
- Frontend files: `3`
- Migration SQL files: `2`
- Highest previous iteration: `250`

## 12. Baseline commands and results

- `python -m pip check`: FAILED due environment conflict: MoviePy requires Pillow `<12`, installed Pillow is `12.2.0`.
- `python -m compileall -q app tests main.py`: PASSED.
- `python -m ruff check .`: UNAVAILABLE (`No module named ruff`).
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest --collect-only -q`: `1132 tests collected`.
- `python -m pytest -q`: TIMED OUT after 180 seconds at approximately 50%; no failure summary, therefore not counted as a pass.

## 13. Confirmed defects/gaps

### UI-251-01 - HIGH - CONFIRMED DEFECT

- Files: `app/ui/static/index.html`, `app/ui/static/app.js`.
- Actual behavior: the separate `Причина` column rendered the first raw gate message. Examples included numeric thresholds and mixed-language implementation terms such as `mean_reversion_score`, `candidate floor`, `bot-specific monetary expectancy`.
- Expected behavior: the primary table must communicate only enter/do-not-enter at a glance; one short reason should be a hint on the decision label.
- Operator impact: rows became extremely wide, the useful columns were displaced, and implementation diagnostics looked like decision metrics.
- Why tests missed it: iteration 250 explicitly asserted the six-column/raw-reason contract.

### API-251-02 - MEDIUM - CONFIRMED GAP

- File: `app/main.py`, `_operator_summary_for_reco`.
- Actual behavior: `primary_reason` copied the raw first message without a bounded operator vocabulary or safe fallback.
- Expected behavior: known codes map to short phrases; unknown codes use a status-level generic phrase. Raw detail remains separately auditable.
- Risk: arbitrary future diagnostic text could again leak into the primary table.

### REL-251-03 - LOW - CONFIRMED GAP

- Input ZIP contained `data/app.db` (runtime database artifact).
- The final release excludes `data/*.db` according to the release protocol. No production data from that file is used or migrated.

## 14. Unconfirmed claims

No claim is made that the short-reason mapping covers every future diagnostic code. Unknown codes are intentionally handled by a safe generic fallback.

## 15. Fix plan

- Introduce a backend reason-code-to-operator-hint mapping and a bounded status fallback.
- Preserve raw text as additive `primary_reason_detail`.
- Remove the reason table header/cell.
- Put the short reason on the decision badge via `title` and `aria-label`.
- Update wording from `НЕ ВХОДИТЬ` to `НЕ ТОРГОВАТЬ`; distinguish hard blocked rows.
- Synchronize version, cache key, tests and operator documentation.

## 16. Actual diff

### Production

- `app/main.py`: mapping, fallback, additive raw detail, version `1.0.63`.

### Frontend

- `app/ui/static/index.html`: five-column table; JS cache key v48.
- `app/ui/static/app.js`: decision badge owns the short tooltip; removed reason-cell rendering.

### Tests

- Added `tests/test_iteration251_operator_decision_hint.py`.
- Minimally updated iteration 250 expectation and current version/cache assertions.

### Documentation/artifacts

README, CHANGELOG, TRADING_LOGIC, KNOWN_RISKS, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, operator DOCX/PDF and `how_to_trade.png`.

### Database/migrations

No schema or migration changes.

## 17. RED -> GREEN evidence

RED command:

```bash
python -m pytest -q tests/test_iteration251_operator_decision_hint.py
```

RED result on pristine v1.0.62:

```text
4 failed in 1.12s
assert labels == [..., "Решение"]  # actual also contained "Причина"
assert ">НЕ ТОРГОВАТЬ<" in html  # actual: НЕ ВХОДИТЬ without hint
```

GREEN command: identical.

```text
4 passed in 1.50s
```

The final test executes the actual production JavaScript `operatorDecisionCell`, not only a string search.

## 18. Database/schema compatibility

No schema change. Fresh SQLite initialization and repeated initialization both produced 19 application tables. Existing-schema additive-upgrade regression passed. PostgreSQL translation/locking subset passed. Live PostgreSQL integration was not run because no explicitly disposable test DSN was provided.

## 19. API compatibility

Existing fields remain. `operator_summary.primary_reason` is now short operator text; `primary_reason_code` is unchanged; `primary_reason_detail` is additive and contains the original message. Status semantics and route names are unchanged.

## 20. Config/environment compatibility

No environment variable or default risk-profile change. Existing `.env` can be reused.

## 21. Security boundary

No private order create/amend/cancel endpoints were added. Mutating/security boundaries are unchanged. Tooltip and accessibility strings pass through HTML escaping.

## 22. Post-check commands and results

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest --collect-only -q`: `1136 tests collected`.
- Exhaustive deterministic file-batch run: 196 unique test files, no duplicates, union equal to collected files; `1136 passed` across 13 successful processes (`99+90+162+66+26+44+79+93+78+86+143+85+85`).
- Focused iteration 250/251 suite: `9 passed`.
- UI/docs/release focused suite: `40 passed`.
- PostgreSQL/SQLite targeted subset: `13 passed`.
- Fresh/repeated SQLite init: `19 / 19` application tables.
- DOCX rendered and visually reviewed: 11 pages.
- PDF independently rendered and visually reviewed: 11 pages.
- Infographic visually reviewed: `1600 x 1200`.
- Private Bybit order endpoint search: no matches.
- `pip check`: same external MoviePy/Pillow conflict as baseline.
- Ruff: unavailable in the environment.

## 23. Not verified

- Live PostgreSQL integration against a disposable server.
- Browser automation in every browser engine. The actual production JS function is executed under Node, and the static table contract is tested.

## 24. Residual risks

- Native browser tooltip appearance and delay vary by browser/OS.
- Future reason codes require vocabulary additions for the most specific phrase; until then the safe generic fallback is shown.
- The tooltip is presentation only and must not be interpreted as replacing full Details evidence.

## 25. Rollback

Stop v1.0.63, restore v1.0.62 files and restart with the same DB and `.env`. No DB rollback is required.

## 26. Recommended next work package

Observe the most frequent `primary_reason_code` distribution in production and add only genuinely decision-distinct short phrases. Do not add more columns to the primary table.
