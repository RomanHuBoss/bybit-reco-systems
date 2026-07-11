# Audit iteration: outcome capital normalization and prospective daily-loss budget

## 1. Название итерации

`v1.0.21 — outcome capital normalization and daily loss budget`.

## 2. Входной ZIP

`bybit-reco-systems-main(1).zip`.

## 3. SHA-256 входного ZIP

`6c143bbfe899881378126c717e199588b3b5efd10d11075eca28f255db135b97`.

## 4. Исходная версия

`1.0.20`, определена по `FastAPI(..., version="1.0.20")` в `app/main.py`.

## 5. Новая версия

`1.0.21` (patch: исправление outcome/risk semantics без изменения публичной схемы API или БД).

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender:

- root: `bybit-reco-systems-main`;
- присутствуют обязательные `README.md`, `CHANGELOG.md`, `main.py`, `app/main.py`, `app/recommender.py`, `app/trading_semantics.py`, `app/grid_math.py`, `app/risk.py`, `app/calibration.py`, `app/outcomes.py`, оба persistence backend, frontend, tests и обе SQL-схемы;
- scope: Bybit `linear` USDT perpetual, `futures_grid`, recommendation/audit-only;
- SQLite и PostgreSQL сохранены;
- private order create/amend/cancel endpoints в production-коде не обнаружены.

Безопасность архива до распаковки: 232 entries, один root, нет absolute/traversal paths, конфликтующих duplicate paths, внешних symlink или вложенных архивов.

## 7. Цель итерации

После этой итерации proxy-outcome не должен превращать прибыль одной grid-leg в прибыль всего бота и не должен считать доходность одной заявки доходностью всего выделенного сетке капитала. Перед materialization `bot_instance` потенциальный loss до adverse kill-switch должен помещаться в остаток дневного drawdown budget.

Это исправляет подтверждённые смещения labels и fail-open risk gap, но не доказывает положительное live expectancy.

## 8. Критерии приёмки

1. TP-touch одной directional leg не создаёт `success=1`, если whole-grid proxy остаётся убыточным.
2. Одинаковая последовательность completed legs даёт return, обратно пропорциональный подтверждённому `grid_count`.
3. Kill-switch breach и отрицательный whole-grid proxy сохраняют fail-closed precedence.
4. Если `estimated kill-switch loss > max_daily_dd_usdt - daily_dd`, execute-preflight возвращает `DAILY_LOSS_BUDGET_EXCEEDED`.
5. Новая outcome-семантика отделена от legacy calibration через `grid_label_v3`.
6. Публичный API, DB schema, SQLite/PostgreSQL support и recommendation/audit-only boundary остаются совместимыми.
7. Новый test падает на pristine-коде и проходит после исправления; полный suite остаётся зелёным.

## 9. Прочитанные источники

- `README.md`, `CHANGELOG.md`, `.env.example`, requirements;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние отчёты: `AUDIT_REPORT_2026-07-11_temporal_funding_integer_semantics.md`, `...signal_durability_identity.md`, `...risk_sizing_integrity.md`, `...outcome_funding_integrity.md`, `...mean_reversion_edge.md`;
- `app/outcomes.py`, `app/calibration.py`, `app/risk.py`, `app/main.py`, `app/recommender.py`, `app/grid_math.py`, `app/trading_semantics.py`, DB/backend и релевантные tests;
- frontend contract и operator artifacts.

## 10. Карта затронутого data flow

`recommendation.params/trade_plan -> outcome maturity -> app.outcomes._grid_outcome -> reco_outcomes -> calibration sample/model`

и

`operator execute action -> current risk limits/status -> gate_candidate -> execution preflight -> conservative kill-switch loss -> block or bot_instance materialization`.

## 11. Baseline environment

- Python `3.13.5`;
- Node `v22.16.0`;
- baseline inventory: 23 production Python files, 152 test files, 32 docs, 3 frontend files, 2 migration SQL files;
- DB backends: SQLite + PostgreSQL compatibility layer;
- API routes: 22, из них 6 mutating POST routes;
- отдельный disposable PostgreSQL DSN не предоставлен.

## 12. Baseline commands и результаты

| Команда | Результат |
|---|---|
| `python -m pip check` | FAILED: global environment mismatch `moviepy 2.2.1` requires `pillow<12`, installed `12.2.0`; проектные зависимости не изменялись |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest -q` | PASSED: `870 passed in 24.74s` |

## 13. Подтверждённые defects/gaps

### DEF-209-01 — isolated TP leg labelled the whole grid successful

- Severity: **high**.
- Type: **CONFIRMED DEFECT**.
- Original location: `app/outcomes.py::_grid_outcome`, TP shortcut immediately before final success decision.
- Input: long grid, entry 100, early high 100.35 crosses `tp_per_leg=0.25`, then closes near 95.1 with unresolved adverse inventory; lower kill-switch 94.5 is not crossed.
- Actual pristine behavior: `(success=1, ret_proxy=0.001)`.
- Expected: one OHLCV touch cannot prove queue/fill sequence or closure of whole inventory; success must remain 0 and proxy negative.
- Violated invariant: proxy outcome must not reconstruct fills/inventory it does not observe; a per-leg TP is not terminal whole-grid PnL.
- Financial/model impact: false-positive labels inflate win rate and poison calibration/operator confidence.
- Why old tests missed it: two tests explicitly encoded “one TP touch = whole-grid success”.
- Regression: `test_directional_per_leg_tp_touch_does_not_override_unresolved_whole_grid_loss`.

### DEF-209-02 — per-order percentage treated as full-grid capital return

- Severity: **high**.
- Type: **CONFIRMED DEFECT**.
- Original location: `app/outcomes.py::_grid_outcome`, `gross_proxy = completed_steps * gross_leg_pct` and unweighted cost subtraction.
- Input: identical oscillation path with `grid_count=2` and `grid_count=20`.
- Actual pristine behavior: both return `0.0086`.
- Expected: if one order is one capital slice, whole-grid return must be normalized by the committed capital denominator; the two-level case must be 10x the twenty-level case for the same completed legs.
- Violated invariant: ROI/return denominator must be explicit and consistent with total grid capital.
- Financial/model impact: return and calibration quality were overstated approximately in proportion to grid count.
- Why old tests missed it: tests compared canonical and legacy aliases but retained the unnormalised constant `0.0096` as oracle.
- Regression: `test_grid_proxy_return_is_normalized_by_committed_grid_capital`.

### GAP-209-03 — new grid could make the daily DD cap impossible to respect

- Severity: **high**.
- Type: **CONFIRMED GAP**.
- Original locations: `app/risk.py::gate_candidate`, `app/main.py::_execution_preflight` and execute path.
- Input: daily DD 8 USDT, cap 10 USDT, grid notional 500 USDT, neutral adverse distance 5%, execution friction 15 bps.
- Actual pristine behavior: no daily prospective risk block.
- Expected: estimated loss `500 * (0.05 + 0.0015) = 25.75 USDT` exceeds the 2 USDT remaining budget and must block before bot audit materialization.
- Violated invariant: risk cap cannot be applied only after a loss is realised; sizing/preflight must fail closed when the next bounded loss exceeds available budget.
- Operational impact: one legally launched grid could breach the configured daily cap by construction.
- Why old tests missed it: runtime caps covered current DD, not prospective kill-switch exposure.
- Regression: `test_execution_preflight_blocks_kill_switch_loss_above_remaining_daily_budget`.

### DEF-209-04 — incompatible outcome semantics could mix with legacy calibration

- Severity: **high**.
- Type: **CONFIRMED DEFECT** (data-version contract).
- Original location: `app/main.py`, `OUTCOME_LABEL_VERSION="grid_label_v2"`.
- Actual pristine behavior: changing success and return denominator without a label-version bump would preserve incompatible outcomes/calibrators.
- Expected: versioned reset before new labels are used.
- Model impact: coefficients fitted on old target scale/classes would be interpreted under new semantics.
- Regression: `test_outcome_semantics_bump_label_version_to_avoid_mixing_legacy_calibration`.

## 14. Неподтверждённые claims

- Не подтверждено, что стратегия прибыльна или убыточна на реальных fills: в проекте нет достаточного независимого live execution dataset с comparator/no-trade baseline.
- Не подтверждено, что все remaining defects найдены.
- Proxy outcomes остаются моделью OHLCV, а не восстановлением реальной очереди, partial fills или inventory.
- Никакой backtest/proxy metric не интерпретируется как live edge.

## 15. План исправления

1. Добавить независимые red regressions.
2. Удалить TP shortcut из whole-grid success path.
3. Нормировать completed-leg gross/cost на canonical grid capital slots.
4. Добавить conservative daily-loss-budget guard в execution preflight.
5. Передать один cached runtime risk status в оба execution gate, не удваивая DB scan.
6. Повысить label version и синхронизировать docs/operator artifacts.
7. Выполнить targeted, relevant, full и release-reextract checks.

## 16. Фактический diff по файлам

### Production

- `app/outcomes.py`: capital normalization; TP diagnostic-only; whole-grid success only from matched cycles/positive net proxy/intact kill-switch.
- `app/main.py`: `grid_label_v3`; `_execution_daily_loss_budget_guard`; cached risk status through preflight; version `1.0.21`.

### Tests

- `tests/test_iteration209_outcome_capital_and_daily_risk.py`: 4 regressions.
- `tests/test_iteration106_grid_tp_success_semantics.py`: removed invalid TP-as-whole-grid oracle.
- `tests/test_iteration192_grid_count_integer_semantics.py`: corrected expected capital-normalized return.

### Documentation/operator artifacts

- `README.md`, `CHANGELOG.md`, `docs/TRADING_LOGIC.md`, `docs/KNOWN_RISKS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx` and `.pdf`;
- `how_to_trade.png`.

### Database/migrations/frontend

No schema, migration or frontend source changes.

## 17. Red -> green evidence

Red command on pristine source plus new test:

```bash
python -m pytest -q tests/test_iteration209_outcome_capital_and_daily_risk.py
```

Material red lines:

```text
assert 1 == 0
assert 0.0086 > 0.0086
assert 'DAILY_LOSS_BUDGET_EXCEEDED' in {}
assert 'grid_label_v2' == 'grid_label_v3'
4 failed in 1.07s
```

Green command on working source:

```bash
python -m pytest -q tests/test_iteration209_outcome_capital_and_daily_risk.py
```

Green result:

```text
4 passed in 0.92s
```

A deterministic repeat also passed (`4 passed in 0.84s`).

## 18. Database/schema compatibility

- No DB schema change.
- `migrations/init.sql`, `migrations/init_postgres.sql` and runtime bootstrap are unchanged.
- SQLite/PostgreSQL dialect suite: `18 passed in 0.58s`.
- On first startup, the existing label-version reset path removes legacy `reco_outcomes` and associated calibrators because `grid_label_v3` differs from `grid_label_v2`.
- Recommendations, bot instances, trades, exact execution evidence and risk limits are not deleted.
- Live PostgreSQL integration: SKIPPED, no explicitly disposable DSN supplied.

## 19. API compatibility

No public route or request field was removed/renamed. `_execution_preflight` gains optional internal arguments and an additive `daily_loss_budget` diagnostic block. Existing action semantics remain unchanged; a new hard-block code may be returned when the prospective daily risk is too large.

## 20. Config/env compatibility

No new environment variable. Existing `max_daily_dd_usdt` is now enforced both as realised DD stop and prospective kill-switch budget. No `.env` or credentials are included in release.

## 21. Security boundary

- Project remains recommendation/audit-only; no private Bybit order endpoint or auto-execution added.
- No production credentials used.
- Secret scan found no embedded key/token patterns; `.env.example` values remain blank.
- External executor must still verify live balance, positions, open orders, actual inventory and exchange state.

## 22. Post-check commands и результаты

| Проверка | Результат |
|---|---|
| targeted regression, repeated | `4 passed`; `4 passed` |
| relevant outcome/risk suite | `35 passed in 1.71s` |
| SQLite/PostgreSQL dialect/locking suite | `18 passed in 0.58s` |
| collection | `874 tests collected` |
| full pytest | `874 passed in 24.55s` |
| compileall | PASSED |
| Node syntax | PASSED |
| private order endpoint static search | no matches in `app/` or `main.py` |
| version/label consistency | PASSED |
| DOCX render | PASSED, 5 pages |
| PDF render | PASSED, 5 pages |
| pip check | FAILED only on unrelated global moviepy/Pillow mismatch |
| ruff | UNAVAILABLE in current environment |

## 23. Что не удалось проверить

- Live Bybit fills, queue priority, partial fills, liquidation waterfall and account-level inventory.
- Positive live expectancy or statistical superiority to a no-trade/comparator baseline.
- Disposable live PostgreSQL integration.
- Ruff lint because the environment lacks the module and dependency installation was not required for this patch.

## 24. Остаточные риски

1. Proxy outcome remains an OHLCV proxy. Capital normalization removes a confirmed scale error but does not make it realised PnL.
2. Prospective kill-switch loss is conservative and can overblock: it uses maximum persisted/derived notional, not actual live inventory.
3. It can also understate risk if the external executor increases qty, changes geometry, incurs gap/slippage beyond the model, or fails to close at kill-switch.
4. Daily DD is based on realised evidence available to this service; unrealised/account-level losses require external reconciliation.
5. New calibration must accumulate sufficient `grid_label_v3` samples before being treated as informative.

## 25. Rollback procedure

1. Stop the service.
2. Restore the previous `1.0.20` archive/code and previous database backup if retaining pre-reset `grid_label_v2` outcomes is required.
3. Do not merge v2 and v3 proxy outcomes manually.
4. Restart with the prior configuration and verify operator actions remain disabled until health/preflight is green.

## 26. Рекомендуемый следующий work package

Build an exact-execution validation cohort: ingest immutable fills/fees/funding, reconstruct realised inventory/PnL outside proxy labels, and compare stopped bot cohorts against no-trade and simple benchmark policies with chronological walk-forward evaluation. Until that evidence exists, profitability remains unsupported.
