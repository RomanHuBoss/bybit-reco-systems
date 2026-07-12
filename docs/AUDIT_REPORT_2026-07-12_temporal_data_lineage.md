# Audit iteration: temporal market-data and calibration lineage

## 1. Название итерации

`v1.0.23 — temporal market-data and calibration lineage`.

## 2. Входной ZIP

`bybit-reco-systems-main(1).zip`.

## 3. SHA-256 входного ZIP

`0b94ce66dac5ecedf9e7034e5f3caacbfdc91d47a59907ceb899761050cd5806`.

## 4. Исходная версия

`1.0.22`, определена по `FastAPI(..., version="1.0.22")` в `app/main.py`.

## 5. Новая версия

`1.0.23` (patch: fail-closed исправление temporal/data-lineage contract без изменения публичной схемы API или DB schema).

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender:

- обязательные root-файлы, application modules, frontend, tests, docs и обе SQL-схемы присутствуют;
- scope: Bybit `category=linear`, USDT perpetual, `futures_grid`, recommendation/audit-only;
- SQLite и PostgreSQL support сохранены;
- production private order create/amend/cancel endpoints не обнаружены;
- FastAPI application создаётся в `app/main.py`, frontend расположен в `app/ui/static/`.

Безопасность входного архива: 236 entries, один root, нет absolute/traversal paths, duplicate/conflicting paths, внешних symlink или вложенных архивов.

## 7. Цель итерации

После этой итерации authoritative exchange time должен сохраняться от Bybit response до freshness gate; shifted/malformed OHLCV не должен превращаться в допустимую свечу; feature/outcome/calibration chronology не должна создаваться через `int()`/floor fallback; proxy-outcome должен существовать только при непрерывном exact 1m horizon; calibration должна использовать только уже созревшие labels с доказанным `label_available_ts`.

Исправление снижает подтверждённые temporal leakage и sample-corruption risks, но не доказывает прибыльность или live edge.

## 8. Критерии приёмки

1. Top-level Bybit V5 response `time` доходит до collector ticker timestamp, если item не содержит собственного timestamp.
2. Kline timestamp принимается только как exact integer milliseconds, кратный секунде и requested timeframe; boolean/fractional/shifted значения отклоняются.
3. Feature timestamp не принимает boolean или fractional number.
4. Outcome entry равен ровно `features_ref_ts + 60`, весь 1m horizon непрерывен, exit candle существует ровно на `label_available_ts`.
5. Calibration исключает missing/malformed/future `label_available_ts` и не обучается на недоступных labels.
6. Dirty persisted outcome row не обрушает весь calibration read path.
7. Несовместимые старые proxy labels отделены через `OUTCOME_LABEL_VERSION=grid_label_v4`.
8. Новый regression test падает на pristine и проходит после fix; exact union полного suite остаётся зелёным.

## 9. Прочитанные источники

- `README.md`, `CHANGELOG.md`, requirements, `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние audit reports по temporal/funding, mean-reversion, outcome, risk sizing, signal durability и no-recommendation state;
- `app/bybit_client.py`, `collector.py`, `features.py`, `direction.py`, `regime.py`, `recommender.py`, `risk.py`, `outcomes.py`, `calibration.py`, `db.py`, `db_backend.py`, `main.py`, `trading_semantics.py`, frontend contract и релевантные tests.

## 10. Карта затронутого data flow

`Bybit V5 envelope/item -> bybit_client.get_tickers -> collector ticker freshness -> persistence -> feature timestamp validation -> recommendation features_ref_ts -> exact next 1m entry -> contiguous outcome horizon -> exact label availability -> reco_outcomes -> DB decoder -> fit_logreg chronological/purged calibration -> recommendation confidence layer`.

Direction aggregation, grid geometry, economics, deterministic risk gate, publication lifecycle and operator actions проверены полным regression suite и не изменялись в этой итерации.

## 11. Baseline environment

- Python `3.13.5`;
- Node `v22.16.0`;
- baseline inventory: 23 production Python files, 154 test files, 34 docs, 3 frontend files, 2 migration SQL files;
- API: 20 `/api/` routes, 6 mutating routes;
- DB backends: SQLite + PostgreSQL compatibility layer;
- disposable PostgreSQL DSN не предоставлен.

## 12. Baseline commands и точные результаты

| Команда | Результат |
|---|---|
| `python -m pip check` | FAILED: environment-level `moviepy 2.2.1` requires `pillow<12`, installed `12.2.0`; project dependencies не менялись |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | 877 collected |
| `python -m pytest -q` | TIMED OUT at 73% without final summary |
| exhaustive deterministic batches | PASSED: exact union 877/877, 0 duplicates, 8/8 batches green; aggregate batch runtime 83.379 s |

## 13. Подтверждённые defects/gaps

### DEF-211-01 — authoritative Bybit ticker time discarded

- Severity: **high**; type: **CONFIRMED DEFECT**.
- Original location: `app/bybit_client.py::get_tickers` around line 252.
- Input: valid `/v5/market/tickers` response with top-level exact `time`, ticker item without item-level timestamp.
- Actual pristine behavior: returned item had no `time`; collector could stamp it with local receipt time and make cached/stale exchange data look current.
- Expected: preserve authoritative envelope timestamp until freshness validation.
- Violated invariant: event time and availability/receipt time must not be conflated.
- Financial/trading impact: stale reference/spread/funding context could pass freshness gates and affect recommendation/preflight.
- Why old tests missed it: client tests validated symbol filtering and payload shape, not envelope-time provenance.
- Fix: propagate valid top-level `time` only to rows without any item timestamp.
- Regression: `test_bybit_ticker_response_time_reaches_collector_freshness_timestamp`.

### DEF-211-02 — shifted kline timestamp normalized into a valid candle

- Severity: **high**; type: **CONFIRMED DEFECT**.
- Original location: `app/collector.py::_sanitize_ohlcv_row` around line 242.
- Input: start milliseconds with subsecond remainder or not aligned to `tf_sec`; fractional timeframe.
- Actual: floor division manufactured an integer-second candle bucket.
- Expected: exact integer, whole-second and timeframe-aligned start; otherwise reject.
- Impact: duplicate/wrong bucket, closed-candle leakage, distorted rolling features.
- Regression: `test_kline_boundary_rejects_subsecond_or_timeframe_misaligned_start`.

### DEF-211-03 — feature timestamps accepted booleans/fractions through `int()`

- Severity: **high**; type: **CONFIRMED DEFECT**.
- Original location: `app/features.py::compute_features_from_ohlcv` around line 58.
- Actual: `True -> 1`, `x.9 -> x` could enter sorting/window logic.
- Expected: exact-integer timestamp only.
- Impact: wrong temporal ordering and possible use of malformed/future rows.
- Regression: `test_feature_layer_rejects_boolean_and_fractional_timestamps`.

### DEF-211-04 — sparse/gapped OHLCV could still create an outcome

- Severity: **high**; type: **CONFIRMED DEFECT**.
- Original locations: `app/outcomes.py::_get_first_tradeable_candle_after`, `_get_open_at_or_after`, `compute_outcomes_once` around lines 110-166 and 578+.
- Input: missing exact next minute, gaps inside 12h horizon, no exact exit candle but a later candle exists.
- Actual: worker selected first later entry/exit and labelled an incomplete path.
- Expected: exact next-minute entry, all expected minute starts present once, exact horizon exit.
- Model impact: return, range occupancy, kill-switch and success label were evaluated on a different market interval than declared.
- Regressions: `test_outcome_worker_requires_exact_contiguous_horizon_and_exact_exit_candle` plus corrected old sparse fixtures.

### DEF-211-05 — unavailable labels admitted to calibration

- Severity: **high**; type: **CONFIRMED DEFECT**.
- Original location: `app/calibration.py::fit_logreg` around line 587.
- Input: rows with absent, malformed or future `label_available_ts`.
- Actual: rows were sanitized by recommendation timestamp alone and could fit Platt/LogReg before target maturity was demonstrable.
- Expected: require exact positive `label_available_ts`, `>= recommendation.ts` and `<= fit_ts`.
- Model impact: label-availability leakage and overstated calibration quality.
- Regression: `test_calibration_excludes_labels_not_demonstrably_available`.

### DEF-211-06 — one malformed persisted row could crash calibration retrieval

- Severity: **high**; type: **CONFIRMED DEFECT**.
- Original location: `app/db.py::get_outcomes_with_recs` around line 2389.
- Input: malformed TEXT in `label_available_ts` or mandatory numeric columns of a dirty SQLite row.
- Actual: blind `int()`/`float()` raised and aborted the complete read cycle.
- Expected: strict parse; invalid mandatory rows skipped; malformed optional availability becomes unknown and remains ineligible for fit.
- Regression: `test_outcome_join_decoder_does_not_crash_on_malformed_label_availability`.

### DEF-211-07 — incompatible temporal target could mix with old calibration

- Severity: **high**; type: **CONFIRMED DEFECT** (data-version contract).
- Original location: `app/main.py`, `OUTCOME_LABEL_VERSION="grid_label_v3"`.
- Actual: existing v3 outcomes could have been generated from gapped/later entry-exit data and reused after code correction.
- Expected: one-time target reset before new semantics are calibrated.
- Fix: `grid_label_v4`; existing startup version guard clears proxy outcomes/calibrators only.
- Regression: `test_temporal_outcome_contract_uses_new_label_version`.

## 14. Неподтверждённые claims

- Не подтверждено, что стратегия априори прибыльна или убыточна на реальных fills.
- Не подтверждено, что найдены все дефекты проекта.
- Proxy outcomes не доказывают queue priority, actual fill sequence, partial inventory, live fee/slippage or liquidation behavior.
- Положительный score/confidence после fix не является доказательством live expectancy.

## 15. План исправления

1. Создать независимый iteration211 regression file и получить RED на pristine.
2. Сохранить authoritative ticker time и ужесточить market-data boundaries.
3. Ввести exact-integer feature timestamp contract.
4. Сделать outcome horizon exact/contiguous fail-closed.
5. Исключить unavailable labels и harden DB decoding.
6. Повысить label version, обновить старые тестовые fixtures, фиксировавшие sparse chronology.
7. Выполнить targeted, related, exhaustive full-suite, DB/dialect, static boundary и release-reextract checks.

## 16. Фактический diff по файлам

### Production

- `app/bybit_client.py` — V5 envelope time provenance.
- `app/collector.py` — exact timestamp/timeframe alignment.
- `app/features.py` — strict feature timestamp parsing.
- `app/outcomes.py` — exact entry/exit and contiguous horizon.
- `app/calibration.py` — mature label-only fit sample.
- `app/db.py` — strict/finite outcome join decoding.
- `app/main.py` — version `1.0.23`, `grid_label_v4`.

### Tests

- new `tests/test_iteration211_temporal_data_lineage.py` — 7 regressions;
- updated `test_logic.py`, iterations 85/87/108/191/209 — fixtures/expectations that encoded missing availability, sparse horizons or previous target version.

### Documentation

- `README.md`, `CHANGELOG.md`, `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- this audit report.

### Frontend/database/migrations

No frontend source or DB schema/migration change. Existing SQLite/PostgreSQL schemas remain compatible.

## 17. Red -> green evidence

RED command on pristine source plus final new test:

```bash
env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY python -m pytest -q tests/test_iteration211_temporal_data_lineage.py
```

Material RED lines:

```text
KeyError: 'time'
assert {...} is None
assert 1 == 0
assert True is False
ValueError: invalid literal for int() with base 10: 'not-an-integer'
assert 'grid_label_v3' == 'grid_label_v4'
7 failed in 2.11s
```

GREEN command on working source:

```bash
env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY python -m pytest -q tests/test_iteration211_temporal_data_lineage.py
```

GREEN result:

```text
7 passed in 1.83s
```

Deterministic repeat: `7 passed in 2.01s`.

## 18. Database/schema compatibility

- No schema or SQL migration change.
- Fresh/repeated SQLite bootstrap passed and created 16 application tables.
- PostgreSQL translation/locking/static dialect suite: `24 passed in 3.09s`.
- Live PostgreSQL integration: SKIPPED, no explicitly disposable DSN.
- First v1.0.23 startup detects `grid_label_v3 != grid_label_v4` and clears only `reco_outcomes` plus related calibrator keys through existing code.
- Recommendations, bot instances, trades, risk limits and exact execution evidence are preserved.

## 19. API compatibility

No public route, request field, response field or status semantics changed. New behavior is stricter internal eligibility: incomplete market chronology produces no outcome/calibration row.

## 20. Config/env compatibility

No new environment variables. No `.env` action. No dependency pin change.

## 21. Security boundary

- Recommendation/audit-only boundary preserved; no order create/amend/cancel code added.
- No production credentials used or included.
- Private order endpoint static search: no matches in `app/` or root `main.py`.
- External execution layer remains responsible for actual account/order/fill reconciliation.

## 22. Post-check commands и точные результаты

| Проверка | Результат |
|---|---|
| targeted iteration211 | `7 passed in 1.83s`; repeat `7 passed in 2.01s` |
| related temporal/outcome/calibration suites | `166 passed` before strict missing-label expansion; final affected suites green |
| collection | `884 tests collected` |
| exhaustive deterministic full suite | PASSED: exact union 884/884, 0 duplicates, 8/8 batches green; aggregate batch runtime 87.801 s |
| PostgreSQL dialect/locking suite | `24 passed in 3.09s` |
| fresh/repeated SQLite bootstrap | PASSED, 16 application tables |
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| private order endpoint static search | PASSED, no production matches |
| `python -m pip check` | FAILED only on unrelated global MoviePy/Pillow mismatch |
| `python -m ruff check .` | UNAVAILABLE: ruff not installed |

An intermediate post-check found two old sparse-outcome fixtures (`iteration87`, `iteration108`). They were not hidden: both were updated to provide the complete 1m horizon, then the exact 884-node suite was rerun from scratch and passed.

## 23. Что не удалось проверить

- Live Bybit network/account/fills, actual stale-cache behavior at exchange/CDN boundary.
- Disposable live PostgreSQL integration.
- Ruff lint in the current environment.
- Positive live expectancy, profitability, queue priority, partial fills and liquidation waterfall.

## 24. Остаточные риски

1. Complete candles still do not prove fills or inventory path.
2. Strict contiguous horizon can reduce label volume during data outages; this is intentional fail-closed behavior, but calibrator may remain unfitted longer.
3. Bybit envelope time is propagated only when an item-level timestamp is absent; contradictory upstream timestamps remain a future hardening scope.
4. Existing exact execution evidence is not re-evaluated by this proxy-label reset and remains a separate source of realised truth.
5. A strategy may remain economically unviable even after data leakage is removed; that requires independent live/shadow comparator evidence, not code assertions.

## 25. Rollback procedure

1. Stop the service.
2. Restore the previous v1.0.22 code/archive.
3. If preserving pre-reset `grid_label_v3` outcomes is required, restore the pre-v1.0.23 database backup as a whole; do not merge v3 and v4 proxy samples manually.
4. Restart and verify health/preflight before operator actions.

## 26. Рекомендуемый следующий work package

Build a chronology-aware offline evaluation that compares `recommended`, `shadow_no_trade` and simple benchmark/no-trade cohorts using immutable exact execution evidence where available. Include purged walk-forward splits, transaction costs and confidence intervals. Until then, the project is safer and internally more consistent, but profitability remains unproven.
