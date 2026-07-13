# Audit iteration 235–236: exchange-normalized proxy execution

## 1. Название итерации

**Bybit Recommender 1.0.46 → 1.0.48: strict limit-fill evidence and exchange-normalized outcome geometry.**

Версия 1.0.47 ранее упоминалась в переписке, но соответствующий ZIP отсутствовал в активной среде. Поэтому проверяемый вход — фактически доступный 1.0.46; релиз 1.0.48 включает два связанных red→green исправления и не притворяется проверкой недоступного артефакта.

## 2. Входной ZIP

`bybit-reco-systems-1.0.46-funding-receipt-not-alpha.zip`

## 3. SHA-256 входного ZIP

`97813bf0939c0669bb157a9aaeda10532caed6b77db010ca198d0f10692b3edb`

## 4. Исходная версия

`1.0.46`, source of truth: FastAPI `version=` в `app/main.py`.

## 5. Новая версия

`1.0.48` (patch release). Outcome contract: `grid_label_v19 → grid_label_v21`.

## 6. Project fingerprint

Fingerprint совпал: README/CHANGELOG, FastAPI, `futures_grid`, Bybit `linear` USDT perpetual, dual SQLite/PostgreSQL persistence, frontend `app/ui/static`, migrations и обязательные docs присутствуют. Private order create/amend/cancel flow отсутствует.

## 7. Цель итерации

После итерации система должна обучать proxy validation только на сетке, которая была нормализована и признана исполнимой по публичным Bybit instrument filters на момент публикации. OHLC equality с лимитом не должна сама создавать fill.

## 8. Критерии приёмки

1. Exact touch Buy/Sell не создаёт завершённый grid cycle.
2. Strict trade-through сохраняет прежнюю PnL-математику подтверждённого цикла.
3. Рекомендация до публикации получает snapped range/step/qty и immutable Bybit filter snapshot.
4. Missing/invalid metadata блокирует Linear futures-grid и исключает его из outcomes.
5. Current v5 outcome без verified snapshot не создаётся и оставляет diagnostic decision log.
6. Старые proxy outcomes/calibrators не переиспользуются под новым контрактом.
7. Полный набор тестов проходит без ослабления risk/fail-closed semantics.

## 9. Прочитанные источники

README, CHANGELOG, requirements, `.env.example`, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC, последние audit reports, `app/main.py`, `app/recommender.py`, `app/outcomes.py`, `app/grid_math.py`, `app/calibration.py`, `app/db.py`, `app/db_backend.py`, Bybit client, frontend и релевантные tests iterations 105, 127, 192, 209, 212–236.

## 10. Карта затронутого data flow

`features → theoretical recommendation → public Bybit metadata → snap/validate → persisted recommendation + immutable filter snapshot → maturity → snapshot revalidation → strict trade-through OHLC ledger → proxy ret/success → monetary calibration/publication gate`.

## 11. Baseline environment

- Python `3.13.5`.
- Node `v22.16.0`.
- Ruff: UNAVAILABLE (`No module named ruff`).
- `pip check`: external MoviePy/Pillow conflict, unrelated to this work package.
- No production credentials/network smoke tests.

## 12. Baseline commands и результаты

- ZIP traversal/duplicate/symlink/nested archive check: PASSED.
- `unzip -t`: PASSED.
- `python -m compileall -q app tests main.py`: PASSED.
- `node --check app/ui/static/app.js`: PASSED.
- `pytest --collect-only -q`: 1045 nodes.
- Monolithic pytest: no final summary within harness; NOT COUNTED.
- Exhaustive non-overlapping batches: **1045/1045 passed**.

## 13. Подтверждённые defects/gaps

### PX-235 — exact OHLC touch treated as resting-limit fill

- Severity: **high**.
- Type: CONFIRMED DEFECT.
- File: `app/outcomes.py`, `_grid_outcome/process_segment`.
- Reproducer: neutral Buy 99 with candle low exactly 99 and return to 100.
- Actual: `success=1`, positive `ret` from two equality touches.
- Expected: no confirmed fill without price trading through the queue level.
- Impact: artificial completed cycles, win rate, expectancy and calibration readiness.
- Why tests missed it: legacy fixtures encoded equality as execution oracle.

### PX-236 — proxy labels used pre-snap theoretical geometry

- Severity: **high**.
- Type: CONFIRMED DEFECT.
- Files: `app/main.py`, `app/recommender.py`, `app/outcomes.py`.
- Reproducer: theoretical range `99.1–100.9`, step `0.9`, qty `0.26`; Bybit filters tick `0.5`, qty step `0.1` imply `99–101`, step `1`, qty `0.2`.
- Actual: outcome/calibration used original parameters; Bybit normalization existed only in later operator execution preflight.
- Expected: only the persisted exchange-normalized plan may become an outcome root.
- Impact: model could learn from nonexistent levels, invalid quantity and minimum-order violations.
- Why tests missed it: execution preflight and outcome tests were independently green but lacked an end-to-end geometry identity invariant.

## 14. Неподтверждённые claims

- A priori strategy loss is not proven by code alone.
- Strict trade-through does not prove queue position, market volume or partial fill.
- Live profitability remains unverified without complete exchange-attested ledger.

## 15. План исправления

1. Add independent red tests for touch-only fills and exchange geometry identity.
2. Reuse existing snap/validation helpers before publication.
3. Store immutable public instrument-filter snapshot in recommendation params.
4. Block/exclude recommendations when normalization is unavailable or invalid.
5. Independently revalidate snapshot at outcome maturity.
6. Require strict side-aware trade-through.
7. Reset model/calibration/outcome identities.

## 16. Фактический diff

Production:
- `app/main.py`: v1.0.48, `grid_label_v21`, recommendation-time normalizer and background wiring.
- `app/recommender.py`: v5 model, v7 direction key, fail-closed normalization contract.
- `app/outcomes.py`: strict trade-through and verified exchange-snapshot gate.
- `app/calibration.py`: bot/global v10 identities.

Tests:
- new `test_iteration235_limit_touch_fill_confirmation.py`;
- new `test_iteration236_exchange_geometry_evidence.py`;
- legacy economic/topology fixtures changed only from equality touches to explicit penetration; expectations retained;
- version/cache assertions synchronized.

Docs:
- README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, infographic markdown, operator DOCX/PDF/PNG and this report.

## 17. Red → green evidence

Red command:

```bash
python -m pytest -q tests/test_iteration235_limit_touch_fill_confirmation.py tests/test_iteration236_exchange_geometry_evidence.py
```

Red essential output:

```text
assert 1 == 0
production has no recommendation-time exchange normalization
assert row is not None
assert 'exchange_normalizer=' in thread_block
4 failed, 2 passed
```

Green output after production fix:

```text
6 passed
```

Related outcome/preflight regression set:

```text
146 passed
```

## 18. Database/schema compatibility

No schema or standalone migration change. Runtime bootstrap remains additive/idempotent. Outcome version reset deletes incompatible proxy outcomes/calibrators while preserving recommendations, bot instances, trades and exact execution evidence.

## 19. API compatibility

No public route or JSON field removed. New diagnostics are additive inside existing params/reasons payloads.

## 20. Config/env compatibility

No new environment variable. Public Bybit instrument metadata already existed in the project and uses existing cache/timeouts.

## 21. Security boundary

Recommendation/audit-only boundary preserved. No private order endpoint, order SDK call, credential exposure or auto-execution added.

## 22. Post-check commands и результаты

- compileall: PASSED.
- JavaScript syntax: PASSED.
- collection: **1051 nodes**.
- monolithic pytest: no final summary; NOT COUNTED.
- exhaustive disjoint batches: **1051/1051 passed** (211 + 210 + 210 + 210 + 210; the third batch was additionally proven in four disjoint parts 53 + 49 + 49 + 59 after a harness timeout).
- new tests: 6 passed.
- related suite: 146 passed.
- SQLite/PostgreSQL/release checks: see final release verification section below.

## 23. Что не удалось проверить

- Live PostgreSQL integration: no explicitly disposable DSN.
- Actual Bybit queue priority/partial fills: unavailable in public OHLCV.
- Production credentials/private reconciliation: intentionally not used.
- Ruff: unavailable.

## 24. Остаточные риски

- Trade-through remains a proxy; it may still overstate fills in thin markets.
- Instrument filters can change after publication; execution preflight must still re-fetch and may block a once-valid plan.
- Proxy labels do not substitute actual fees, rebates, settled funding, residual inventory and fill sequence.
- Positive proxy lower bound is not proof of live edge.

## 25. Rollback procedure

Code rollback to v1.0.46 requires no schema rollback, but reintroduces touch-as-fill and theoretical/executable geometry divergence. Deleted old outcomes/calibrators should not be restored for trading decisions.

## 26. Recommended next work package

Exchange-attested reconciliation and proxy-vs-exact attribution: compare every proxy cycle with complete fills, fees/rebates, funding, remaining position and open orders; measure false-fill rate and exact net expectancy using purged/block-bootstrap validation.

## Final release verification

- PostgreSQL offline translation/locking/publication-root subset: **15 passed**.
- Release DOCX/PDF/PNG artifact subset: **14 passed**.
- SQLite fresh initialization and repeat initialization: PASSED.
- Simulated existing SQLite upgrade from `grid_label_v19` to `grid_label_v21`: PASSED; sentinel row preserved; v10/v7 calibrator placeholders removed.
- Operator DOCX rendered to 8 page PNGs and every page visually inspected; no clipping or overlap.
- Operator PDF is the verified DOCX render, 8 pages.
- Root PNG infographic visually inspected after update.
- Production private-order endpoint scan: no create/amend/cancel endpoint or SDK-equivalent call found. Historical audit reports contain documentation URLs only.
- `pip check`: FAILED because environment MoviePy 2.2.1 requires Pillow `<12`, while Pillow 12.2.0 is installed; unrelated to changed project dependencies.
- Ruff: UNAVAILABLE (`No module named ruff`).
