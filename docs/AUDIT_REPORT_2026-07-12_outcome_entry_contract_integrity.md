# Audit iteration: post-publication entry and persisted grid-contract integrity

## 1. Название итерации

Bybit Recommender v1.0.27 → v1.0.28: устранение pre-publication outcome entry и fabricated labels при повреждённом grid/funding contract.

## 2. Входной ZIP

`bybit-reco-systems-1.0.27-outcome-label-integrity.zip`

## 3. SHA-256 входного ZIP

`7b84edc58cb1f2f99152e5ee53e86d582c72c5843ee021b0072fb8298daa45c1`

## 4. Исходная версия

- FastAPI: `1.0.27`
- Outcome target: `grid_label_v8`
- Источник версии: `app/main.py`

## 5. Новая версия

- FastAPI: `1.0.28`
- Outcome target: `grid_label_v9`
- SemVer: patch

## 6. Project fingerprint

Совпадает: Bybit Recommender; `futures_grid`; Bybit `category=linear`; USDT perpetual; recommendation/audit-only; SQLite + PostgreSQL; FastAPI в `app/main.py`; frontend в `app/ui/static`; canonical directional semantics в `app/trading_semantics.py`. Private Bybit order endpoints не обнаружены.

## 7. Цель итерации

После итерации proxy-outcome должен использовать только entry, доступный после публикации рекомендации, и должен размечать только одну непротиворечивую сохранённую геометрию grid/funding contract. Повреждённый контракт не должен превращаться в искусственный flat/loss label.

## 8. Критерии приёмки

1. Первая гипотетическая сделка начинается на open первой exact 1m candle строго после publication timestamp.
2. Уже открывшаяся до публикации candle не используется задним числом.
3. Entry вне сохранённого range после delayed publication не сохраняется как `ret=0` loss.
4. Разные валидные range aliases блокируют label.
5. Malformed explicit range блокирует label и не заменяется другой геометрией.
6. Конфликтующие/invalid grid-count aliases блокируют label.
7. Invalid grid direction/geometry возвращает unavailable, а не `(0, 0.0)`.
8. Funding aliases не смешиваются field-by-field; invalid/conflicting blocks блокируют label.
9. Valid identical aliases остаются labelable.
10. Full suite, docs, release ZIP и повторно распакованный targeted test зелёные.

## 9. Прочитанные источники

README, CHANGELOG, requirements, `.env.example`, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, последние audit reports, `app/outcomes.py`, `app/main.py`, `app/grid_math.py`, `app/trading_semantics.py`, `app/recommender.py`, `app/db.py`, relevant outcome/temporal/grid/funding tests.

## 10. Карта затронутого data flow

`recommendations.ts + features_ref_ts` → exact post-publication 1m entry → persisted params/trade_plan aliases → strict range/grid/funding contract → arithmetic inventory ledger → success/ret → `reco_outcomes` → calibration/statistics.

## 11. Baseline environment

- Python `3.13.5`
- Node `v22.16.0`
- Production Python files: 23
- Test files before iteration: 159
- Docs: 39
- Frontend files: 3
- Migration SQL files: 2
- DB backends: SQLite and PostgreSQL through psycopg compatibility layer
- Input DB contained 0 recommendations, 0 outcomes, 0 bot instances, 0 trades and 0 OHLCV rows.

## 12. Baseline commands and results

- `python -m pip check`: FAILED because environment has `moviepy 2.2.1` requiring `pillow<12`, while Pillow is `12.2.0`; unrelated to repository changes.
- `python -m compileall -q app tests main.py`: PASSED.
- `python -m ruff check .`: UNAVAILABLE; ruff not installed.
- `node --check app/ui/static/app.js`: PASSED.
- `python -m pytest -q`: `917 passed in 25.94s`.

## 13. Confirmed defects/gaps

### OECI-01 — HIGH — CONFIRMED DEFECT

- File/function: `app/outcomes.py::_get_first_tradeable_candle_after`, `compute_outcomes_once`.
- Input: `features_ref_ts=t`, recommendation published at `t+90`, candle at `t+60` already open, next candle at `t+120`.
- Actual: entry used open at `t+60`.
- Expected: entry at `t+120`.
- Violation: event/availability-time correctness; look-ahead-free outcome.
- Financial/model impact: impossible historical fill can alter inventory, fills, funding and PnL.
- Why tests missed: they assumed publication and feature timestamps were interchangeable.

### OECI-02 — HIGH — CONFIRMED DEFECT

- File/function: `app/outcomes.py::_grid_outcome`.
- Input: malformed/zero grid count, invalid direction or entry outside range.
- Actual: `(success=0, ret=0.0)` was returned and persisted as a loss.
- Expected: label unavailable; no `reco_outcomes` insertion.
- Impact: win rate, average return and calibrator training contaminated by fabricated losses.

### OECI-03 — HIGH — CONFIRMED DEFECT

- File/function: range/grid alias resolution in `_grid_outcome`.
- Input: valid but different top-level and nested ranges; conflicting `grid_count/grid_levels`; malformed explicit top-level range with valid nested range.
- Actual: first range or conservative minimum grid count was selected, simulating a different bot.
- Expected: fail-closed unavailable label.
- Impact: profit/loss calculated for geometry not actually persisted as one coherent plan.

### OECI-04 — HIGH — CONFIRMED DEFECT

- File/function: `_extract_inventory_funding_model`.
- Input: conflicting or malformed funding alias in one block and valid values in another.
- Actual: fields were resolved independently, allowing a synthetic model assembled from different blocks.
- Expected: duplicate aliases must be individually valid and mutually equal, otherwise no label.
- Impact: wrong funding sign, event count or schedule can create phantom cost/receipt and corrupt PnL.

## 14. Unconfirmed claims

The claim that the strategy is intrinsically and necessarily unprofitable was not confirmed. The release ZIP contains an empty runtime database, so the user’s observed monthly statistics could not be recomputed. Corrected proxy accounting is not evidence of positive live expectancy.

## 15. Fix plan

- Add publication timestamp to entry resolution.
- Select first exact minute open strictly after publication.
- Represent invalid grid contracts as `None`/unavailable.
- Resolve range and grid-count aliases strictly.
- Parse duplicate funding blocks as one atomic contract, rejecting invalid/conflicting fields.
- Skip insertion with explicit diagnostic code.
- Bump target version and update operator documentation.

## 16. Actual diff by file

### Production

- `app/outcomes.py`
- `app/main.py`

### Tests

- New `tests/test_iteration216_outcome_entry_contract_integrity.py`
- Updated old fixtures/assertions in iteration87, 93, 94, 108, 209, 211, 213, 214, 215 and `test_logic.py` where tests previously relied on incomplete or invalid grid contracts.

### Documentation/artifacts

- README, CHANGELOG, TRADING_LOGIC, KNOWN_RISKS, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC
- Operator DOCX/PDF and `how_to_trade.png`
- This audit report

No schema, migration, route, frontend or environment-variable change.

## 17. RED → GREEN evidence

RED command on pristine v1.0.27:

`python -m pytest -q tests/test_iteration216_outcome_entry_contract_integrity.py`

RED result after final regression set was added:

`9 failed, 1 passed in 0.57s`

Material failures included:

- entry `100.0` instead of `101.0` after delayed publication;
- processed `1` instead of `0` when post-publication entry was outside range;
- `(0, 0.0)` instead of unavailable for invalid/conflicting grid contract;
- `(1, 0.005)` for conflicting grid count/range aliases;
- funding conflict produced a label instead of unavailable;
- missing `grid_label_v9` / version `1.0.28`.

GREEN command:

`python -m pytest -q tests/test_iteration216_outcome_entry_contract_integrity.py`

GREEN results: `10 passed in 0.39s`; deterministic repeat `10 passed in 0.37s`.

## 18. Database/schema compatibility

No schema change. Fresh and repeated SQLite bootstrap both produced 17 tables. Existing label-version guard clears only incompatible proxy `reco_outcomes` and associated calibrators when `grid_label_v9` is detected. Recommendations, bot instances, trades, exact execution evidence and risk settings remain.

## 19. API compatibility

No route or JSON field change. FastAPI application version changes from `1.0.27` to `1.0.28`.

## 20. Config/env compatibility

No `.env` variable change. Existing configuration remains valid.

## 21. Security boundary

No order create/amend/cancel endpoint or SDK equivalent was added. No credentials were used. `.env`, runtime DB, lock DB, caches and bytecode are excluded from release ZIP.

## 22. Post-check commands and results

- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- Target regression: `10 passed`, repeated twice.
- Related outcome/temporal/grid suite: `81 passed in 2.20s`.
- PostgreSQL dialect/locking/red-team suite: `21 passed in 1.47s`.
- Full suite: `927 passed in 25.37s`.
- Collection: `927 tests collected`.
- SQLite fresh/repeated bootstrap: `17/17` tables.
- Private order endpoint static search: no hits.
- DOCX: 5 rendered pages visually inspected.
- PDF: 5 rendered pages visually inspected.
- PNG infographic visually inspected.

## 23. Not verified and why

- User’s live monthly SQLite database was not present; observed statistics could not be recalculated.
- Live PostgreSQL integration was not run because no explicitly disposable test DSN was supplied.
- Exact Bybit intrabar fill order, queue priority, partial fills, fee tier and future realised funding remain outside an OHLCV proxy.
- Ruff unavailable.
- External pip environment conflict remains unrelated.

## 24. Residual risks

- Close-to-close ledger cannot infer intrabar crossing sequence.
- Strictly post-publication next-candle entry is conservative and may skip fills available to a faster real executor.
- Proxy labels remain research/calibration targets, not live PnL truth.
- Strategy edge remains unproven until sufficient chronological `grid_label_v9` and exact execution evidence accumulate.

## 25. Rollback procedure

1. Stop the application.
2. Restore v1.0.27 code.
3. Restore the `data/app.db` backup taken before first v1.0.28 startup if old v8 proxy outcomes/calibrators must be retained.
4. Do not restore a stale runtime lock DB.

## 26. Recommended next work package

After enough `grid_label_v9` observations accumulate, compare proxy ledger against immutable exact fills/funding by symbol, direction and regime using chronological walk-forward splits. Quantify entry latency, fill disagreement, fee/funding error and calibration drift before changing strategy thresholds.
