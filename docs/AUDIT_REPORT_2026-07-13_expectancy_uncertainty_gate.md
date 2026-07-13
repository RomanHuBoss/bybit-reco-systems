# Audit iteration 231 — uncertainty-bounded monetary expectancy gate

## 1. Название итерации

Fail-closed uncertainty gate для денежного ожидания `futures_grid`.

## 2. Входной ZIP

`bybit-reco-systems-1.0.42-stale-calibrator-fail-closed.zip`

## 3. SHA-256 входного ZIP

`f6418babc8bec2e03a108a1a96f4c9e036d1d62616dbaeb0fc876c118d89e349`

## 4. Исходная версия

`1.0.42`, source of truth: `version=` при создании FastAPI в `app/main.py`.

## 5. Новая версия

`1.0.43`.

Новые calibration identities:

- `logreg_futures_grid_v8`;
- `logreg_global_v8`;
- direction key остаётся `platt_direction_v6`, поскольку direction probability не снимает bot-specific monetary gate.

## 6. Project fingerprint

Fingerprint совпал:

- Bybit Recommender recommendation/audit service;
- `futures_grid`;
- Bybit `category=linear`, USDT perpetual;
- SQLite и PostgreSQL compatibility layer;
- FastAPI в `app/main.py`;
- frontend в `app/ui/static/`;
- canonical directional semantics в `app/trading_semantics.py`;
- private order create/amend/cancel methods отсутствуют.

Входной ZIP: 269 entries, один root `bybit-reco-systems-main`, traversal/symlinks/duplicate paths/nested archives не обнаружены.

## 7. Цель итерации

После этой итерации технически сильная рекомендация не должна становиться actionable до появления воспроизводимого положительного monetary evidence. Положительное выборочное среднее недостаточно: односторонняя 95% нижняя граница recency-weighted mean proxy return должна быть строго положительной при достаточном эффективном размере выборки.

Это подтверждается независимой payoff-математикой, persistence test и end-to-end запуском `run_recommender_once`.

## 8. Критерии приёмки

1. Рассчитывается Kish effective sample size для recency weights.
2. Рассчитываются weighted return standard deviation и односторонняя 95% lower confidence bound.
3. Малое положительное среднее с `lower_bound <= 0` получает `expectancy_status=uncertain` и не fit-ит модель.
4. Только `lower_bound > 0` допускает статус `positive`; остальные calibration/risk gates сохраняются.
5. `unknown`, `insufficient`, `uncertain` дают явный `PROXY_MONETARY_EXPECTANCY_UNPROVEN` и shadow `no_trade`.
6. `negative` сохраняет более сильный `PROXY_MONETARY_EXPECTANCY_NON_POSITIVE` veto.
7. Диагностика переживает JSON persistence/restart.
8. Старые v7 coefficients не загружаются под новым контрактом.
9. Полный test suite, SQLite checks, PostgreSQL offline subset и release re-extract проходят.

## 9. Прочитанные источники

- README.md, CHANGELOG.md, requirements*.txt, `.env.example`;
- docs/KNOWN_RISKS.md, TRADING_LOGIC.md, ARCHITECTURE.md, MODULES.md, SCENARIOS.md, HOW_TO_TRADE_INFOGRAPHIC.md;
- последние audit reports iterations 227–230;
- `app/calibration.py`, `app/recommender.py`, `app/outcomes.py`, `app/db.py`, `app/db_backend.py`, `app/main.py`, `app/settings.py`;
- related regression tests, especially iterations 189, 208, 228–230 and `tests/test_logic.py`;
- operator DOCX/PDF/PNG artifacts.

## 10. Карта затронутого data flow

`matured outcomes + recommendation feature snapshots` → `fit_logreg` → recency weights → monetary diagnostics → expectancy state → persisted v8 calibrator → bot-specific recommendation calibration → thesis `no_trade` reasons → recommendation status / confidence-model audit payload → operator documentation.

## 11. Baseline environment

- Python: `3.13.5`.
- Node: `v22.16.0`.
- Baseline production Python files: 23.
- Baseline tests: 174 files, 1025 test nodes.
- Docs: 54 files.
- Frontend files: 3.
- Migration SQL files: 2.
- Ruff: unavailable (`No module named ruff`).
- `pip check`: external environment conflict — MoviePy 2.2.1 requires Pillow `<12`, installed 12.2.0.

## 12. Baseline commands и результаты

- `python -m compileall -q app tests main.py` — PASSED.
- `node --check app/ui/static/app.js` — PASSED.
- `python -m pytest -q` — `1025 passed in 27.86s`.
- `python -m pip check` — FAILED only because of external MoviePy/Pillow conflict.
- `python -m ruff check .` — UNAVAILABLE.

## 13. Подтверждённые defects/gaps

### ITER231-01 — positive sample mean treated as established monetary edge

- Severity: **HIGH**.
- Type: **CONFIRMED DEFECT / model-risk fail-open**.
- Files/functions:
  - `app/calibration.py`, `fit_logreg`, previous monetary diagnostics;
  - `app/recommender.py`, `_calibration_expectancy_no_trade_reason` and recommendation publication flow.
- Input example: 80 independent-looking returns, 40 × `+1.01%` and 40 × `-1.00%`.
- Actual v1.0.42 behavior: weighted mean slightly positive, `expectancy_status=positive`; no uncertainty test.
- Expected behavior: because dispersion dominates the tiny mean and one-sided lower bound is non-positive, state must be `uncertain`, unfitted and non-actionable.
- Violated invariant: unknown/unverified required data must fail closed; raw confidence is not evidence of profitability.
- Financial effect: strategy could be launched on sampling noise even when true monetary expectation remained plausibly zero or negative.
- Why tests missed it: tests checked sign of observed mean, class balance and persistence, but no confidence bound or effective sample size.

### ITER231-02 — confidence gate bypassed when no fitted calibrator existed

- Severity: **HIGH**.
- Type: **CONFIRMED DEFECT / publication fail-open**.
- Input: valid range candidate, high raw heuristic confidence, zero matured bot-specific returns, `REQUIRE_CONF_GATE=1`.
- Actual v1.0.42 behavior: monetary helper returned no veto for `unknown/insufficient`; raw confidence could participate in actionability despite absent evidence.
- Expected behavior: retain the candidate only as shadow `no_trade` to accumulate outcomes.
- Reproducer: end-to-end iteration231 test through `run_recommender_once`.
- Financial effect: live/operator exposure before the strategy had demonstrated even proxy positive expectancy.

### ITER231-03 — floating-point boundary could reject effectively exact sample floor

- Severity: **LOW**.
- Type: **CONFIRMED DEFECT**.
- Actual behavior during post-fix regression: an effective count of `3.9999999927` failed a floor of 4 solely because of floating-point rounding.
- Fix: `1e-6` tolerance at the effective-sample comparison only. Meaningfully decayed cohorts remain below the floor.

## 14. Неподтверждённые claims

- **“Стратегия априори убыточна.”** Не доказано: release не содержит representative exact-fill runtime population.
- **“После v1.0.43 стратегия прибыльна.”** Не доказано: positive proxy lower bound remains only a prerequisite.
- **“Normal lower bound полностью решает dependence/regime drift.”** Не доказано; residual limitations are documented.

## 15. План исправления

1. Добавить independent red regression tests.
2. Расширить persisted calibration diagnostics.
3. Рассчитать weighted dispersion, Kish effective sample size and one-sided lower bound.
4. Ввести `uncertain` state.
5. Разрешать positive state only when lower bound is strictly positive.
6. Make every non-positive/unproven bot-specific state an explicit shadow `no_trade`.
7. Bump bot/global calibration identities.
8. Synchronize code, tests, docs and operator artifacts.
9. Run full post-check and validate repacked ZIP.

## 16. Фактический diff по файлам

### Production

- `app/calibration.py` — new diagnostics, state transition, persistence and v8 keys.
- `app/recommender.py` — unproven expectancy no-trade, diagnostics in audit payload, conservative negative-state handling.
- `app/main.py` — version `1.0.43`.

### Tests

- Added `tests/test_iteration231_expectancy_uncertainty_gate.py` — 9 tests.
- Updated positive-control payoff magnitudes in:
  - `tests/test_iteration189_purged_calibration_oof.py`;
  - `tests/test_iteration85_integrity_and_sanitization.py`;
  - `tests/test_logic.py`.
- Updated version assertions.

The three old tests were not weakened: their OOF/label-availability/sanitization contracts remain unchanged. Their tiny payoff fixtures encoded the superseded and economically wrong assumption that any positive mean was enough to fit.

### Documentation/operator artifacts

- README.md, CHANGELOG.md;
- docs/KNOWN_RISKS.md, TRADING_LOGIC.md, ARCHITECTURE.md, MODULES.md, SCENARIOS.md, HOW_TO_TRADE_INFOGRAPHIC.md;
- DOCX/PDF operator instructions;
- `how_to_trade.png`;
- this audit report.

### Database/migrations/frontend

- No relational schema or migration change.
- No frontend source change; existing generic reason rendering consumes the new diagnostic code.

## 17. Red → green evidence

Red command on pristine code plus new test:

```bash
python -m pytest -q tests/test_iteration231_expectancy_uncertainty_gate.py
```

Substantial red output:

```text
AttributeError: 'LogRegScaler' object has no attribute 'weighted_mean_return_lower_bound'
assert reason is not None
AssertionError: assert 'PROXY_MONETARY_EXPECTANCY_UNPROVEN' in set()
9 failed in 0.69s
```

Green command:

```bash
python -m pytest -q tests/test_iteration231_expectancy_uncertainty_gate.py
```

Green output:

```text
9 passed in 0.38s
```

End-to-end test observed raw confidence above 0.5 but required final status `no_trade` and code `PROXY_MONETARY_EXPECTANCY_UNPROVEN`.

## 18. Database/schema compatibility

- Schema change: none.
- `migrations/init.sql` and `migrations/init_postgres.sql`: unchanged.
- SQLite fresh init + idempotent second init: PASSED, 17 application tables.
- SQLite v1.0.42 → v1.0.43 upgrade smoke: PASSED; sentinel `app_config` row preserved.
- Old v7 calibration rows remain auditable but v8 keys prevent use under the new contract.
- Live PostgreSQL integration: SKIPPED because no explicitly disposable DSN was provided.
- Offline PostgreSQL dialect/locking subset: 24 passed.

## 19. API compatibility

- No route or existing JSON field removal.
- `confidence_model` gains additive diagnostics:
  - `weighted_return_std`;
  - `weighted_effective_return_samples`;
  - `weighted_mean_return_lower_bound`;
  - `expectancy_confidence_level`.
- Status semantics are tightened: previously actionable candidates with unproven evidence become `no_trade`.

## 20. Config/env compatibility

- No new environment variables.
- `.env.example` unchanged.
- `REQUIRE_CONF_GATE` can no longer make missing monetary evidence actionable through raw confidence.
- User DB/env action: none.

## 21. Security boundary

- No private Bybit order create/amend/cancel endpoint added.
- Recommendation/audit-only boundary retained.
- No credentials used or shipped.
- Security/mutating route behavior unchanged.

## 22. Post-check commands и результаты

- `python -m pytest --collect-only -q` — 1034 collected.
- `python -m pytest -q` — `1034 passed in 26.10s`.
- iteration231 targeted repeated — `9 passed in 0.38s`.
- related calibration suites iterations 208, 228–231 — 34 passed before full run.
- `python -m compileall -q app tests main.py` — PASSED.
- `node --check app/ui/static/app.js` — PASSED.
- PostgreSQL offline subset — 24 passed.
- SQLite fresh/upgrade checks — PASSED.
- DOCX render — 7 pages, all visually inspected; no clipping/overlap.
- PDF render — 7 pages, all visually inspected; infographic synchronized.
- `how_to_trade.png` — 1344×1120, visually inspected.
- private-order endpoint grep — no matches.
- `pip check` — external MoviePy/Pillow conflict only.
- Ruff — UNAVAILABLE.

## 23. Что не удалось проверить и почему

- Actual live PostgreSQL transaction behavior: no disposable DSN.
- Real Bybit fills, queue priority, partial fills, latency, exact fees and account equity: no production credentials/runtime DB were used.
- Live profitability, production readiness and persistent alpha: not established by source audit or proxy outcomes.
- Full Ruff baseline: module unavailable.

## 24. Остаточные риски

1. The one-sided normal bound assumes the retained independent roots are sufficiently representative; regime clustering and heavy tails can make it optimistic.
2. OHLCV proxy outcomes are not exact exchange fills.
3. The current confidence interval is not a block bootstrap or purged regime-aware inference.
4. Positive lower bound is necessary but not sufficient; exact-execution walk-forward remains mandatory.
5. The project still lacks a representative runtime database in the release, so the actual sign of live expectancy is unknown.

## 25. Rollback procedure

Replace v1.0.43 files with v1.0.42 and restart. No DB rollback is required. Rollback is not recommended because it restores the confirmed fail-open behavior: positive sample mean and raw confidence can become actionable without a positive uncertainty bound.

## 26. Рекомендуемый следующий work package

Validate the proxy-to-live bridge rather than adding another heuristic score:

- ingest an anonymized runtime export of exact fills, fees, settled funding, slippage and capital at risk;
- compare proxy `ret` with exact net return at publication-root level;
- perform purged walk-forward by time/symbol/regime;
- use block/bootstrap or HAC-aware one-sided bounds for dependent heavy-tailed returns;
- compare against no-trade and simple grid baselines;
- keep live disabled until the lower bound for exact net expectancy is positive and drawdown/expected-shortfall limits pass.
