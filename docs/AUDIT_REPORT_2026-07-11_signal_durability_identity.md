# Аудит signal durability, recommendation identity и технологической состоятельности — v1.0.15

## 1. Название итерации

Signal durability and immutable recommendation identity.

## 2. Входной ZIP

`bybit-reco-systems-1.0.14-live-execution-spread-economics(1).zip`

## 3. SHA-256 входного ZIP

`807ed4c89116cec59b76200905b9c32a69bdb23b0bb1fada2fd120a2cd67a818`

## 4. Исходная версия

`1.0.14`, source of truth: `FastAPI(..., version=...)` в `app/main.py`.

## 5. Новая версия

`1.0.15` (patch).

## 6. Project fingerprint

Совпадает с Bybit Recommender: присутствуют обязательные production-модули, frontend, tests, dual SQLite/PostgreSQL persistence, `futures_grid`, Bybit `linear` USDT perpetual и recommendation/audit-only boundary. Private order create/amend/cancel endpoints в production-коде не обнаружены.

## 7. Цель итерации

После итерации одноцикловый всплеск эвристического качества не должен становиться actionable без нового закрытого рыночного evidence, а обновление карточки не должно выдавать более новую строку по той же паре за изменение выбранной immutable-рекомендации. Одновременно требуется определить, доказывает ли текущая технология положительный live edge.

## 8. Критерии приёмки

1. Любой actionable `futures_grid` требует минимум двух разных возрастающих `features_ref_ts`.
2. Повторный recommender-cycle на том же `features_ref_ts` не увеличивает confirmation count.
3. Stale, out-of-order, malformed и legacy persistence state не может обойти gate.
4. Refresh карточки перечитывает exact selected `rec_id`.
5. Новые `no_trade`, `pending`, `blocked` и direction-flip rows остаются отдельными событиями истории.
6. Документация явно отличает raw heuristic confidence от вероятности прибыли и proxy calibration от live PnL truth.
7. Полный regression suite зелёный; release ZIP повторно распакован и проверен.

## 9. Прочитанные источники

README, CHANGELOG, requirements, `.env.example`, `KNOWN_RISKS`, `TRADING_LOGIC`, `ARCHITECTURE`, `MODULES`, `SCENARIOS`, operator artifacts, последние audit reports, `app/recommender.py`, `trading_semantics.py`, `grid_math.py`, `risk.py`, `outcomes.py`, `calibration.py`, `features.py`, `direction.py`, `regime.py`, `collector.py`, `bybit_client.py`, `db.py`, `db_backend.py`, `main.py`, `settings.py`, `llm_review.py`, frontend и релевантные regression tests.

## 10. Карта затронутого data flow

Closed OHLCV -> feature snapshot / `features_ref_ts` -> score / raw confidence -> risk/economics gates -> persistence publication gate -> recommendation row -> details UI refresh/history.

## 11. Baseline environment

- Python `3.13.5`.
- Node `v22.16.0`.
- Input archive: 218 entries; traversal, absolute paths, symlinks, duplicate/conflicting paths и nested archives не обнаружены.
- Production Python files: 23; test files: 146; docs: 26; frontend files: 3; migration SQL files: 2.
- Максимальный существующий iteration test: 202.

## 12. Baseline commands и результаты

| Проверка | Результат |
|---|---|
| `python -m pip check` | FAILED: environment-level `moviepy 2.2.1` требует `pillow<12`, установлена `12.2.0` |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE: `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest -q` | **838 passed in 23.58s**, exit 0 |

## 13. Подтверждённые defects/gaps

### D203-1 — HIGH — CONFIRMED DEFECT — false signal persistence

- Файл: `app/recommender.py`, прежние `_persistence_gate_requirements`, `_advance_persistence_gate`, publication loop.
- Вход: high-quality candidate либо повторные recommender cycles при неизменном `features_ref_ts`.
- Фактическое поведение: high-quality candidate получал `required_hits=1`; счётчик подтверждения зависел только от wall-clock cycle и мог расти без нового закрытого evidence.
- Ожидаемое: score не является независимым повторным наблюдением; требуется новый строго возрастающий closed-candle evidence timestamp.
- Нарушенный инвариант: fail-closed publication durability и temporal correctness.
- Финансовое влияние: кратковременный spike мог стать actionable непосредственно перед деградацией до `no_trade`.
- Почему тесты не поймали: старый тест прямо закреплял immediate high-quality bypass как ожидаемое поведение; distinct `features_ref_ts` не проверялся.
- Исправление: gate всегда требует два distinct snapshots; state хранит exact `evidence_ts`; duplicate/stale/out-of-order/legacy evidence не продвигает count.
- Остаточный риск: два последовательных snapshots не доказывают положительное матожидание и не заменяют regime-duration model.

### D203-2 — HIGH — CONFIRMED DEFECT — UI recommendation identity substitution

- Файл: `app/ui/static/app.js`, прежние `resolveLatestDetailsRecId()` и `refreshCurrentDetails()`.
- Вход: открыта карточка `R-selected`; позже по той же `(venue, symbol, bot_type)` появилась новая row, например `no_trade` или opposite direction.
- Фактическое поведение: refresh запрашивал history `limit=1`, получал `latest_rec_id` и загружал другую audit-row. Для оператора одна рекомендация выглядела как резко «выродившаяся».
- Ожидаемое: immutable `rec_id` перечитывается без подмены; новая публикация видна отдельно в timeline.
- Нарушенный инвариант: recommendation immutable audit identity и UI/backend lifecycle parity.
- Финансовое/операционное влияние: ошибочная интерпретация стабильности сигнала, невозможность корректно сопоставить решение оператора с конкретной рекомендацией.
- Почему тесты не поймали: старый static test требовал наличие `resolveLatestDetailsRecId`, но не исполнял функцию и не проверял загружаемый id.
- Исправление: refresh вызывает `loadDetails(currentRecId)`; новый Node-based regression исполняет production function.
- Остаточный риск: история по паре по-прежнему может содержать много похожих rows, но они больше не маскируются как mutation одной карточки.

### G203-3 — HIGH — CONFIRMED GAP / DOCUMENTED LIMITATION — profitability technology is unvalidated

- Файлы: `app/recommender.py::_score`, calibration/outcome path и документация.
- Математика raw grid score:
  - `raw = 1.35*range + 0.22*coherence + 0.16*regime_conf - 1.00*trend - 0.75*atr_penalty - 0.40*cost_penalty + small direction/sentiment terms`;
  - `score = clip(raw / 2.2)`;
  - `raw_confidence = sigmoid(2.1*raw)`, то есть до context penalties это детерминированная функция того же score, а не независимая вероятность.
- Status использует жёсткий score threshold; calibrated confidence gate включается только при наличии bot-specific fitted calibrator.
- Outcome target остаётся proxy grid path model и не знает actual queue priority, exact fill sequence, partial fills, live fee tier, latency и margin waterfall.
- В архиве отсутствуют live DB/trade/fill данные, поэтому фактическое net expectancy проверить невозможно.
- Вывод: утверждение «технология математически невозможна» не доказано. Но утверждение «текущая система доказала прибыльную технологию» опровергнуто архитектурой и отсутствием evidence. До положительной chronological walk-forward/shadow статистики по фактическим fills систему следует считать hypothesis-generation/recommendation layer, не validated alpha.

## 14. Неподтверждённые claims

- Нельзя подтвердить, что все убыточные сделки вызваны двумя найденными defects: live executions и account-level data не приложены.
- Нельзя подтвердить положительную или отрицательную долгосрочную expectancy стратегии по исходному ZIP.
- Нельзя доказать, что два distinct snapshots достаточно оптимальны; это минимальный fail-closed durability floor, а не оптимизированный alpha parameter.

## 15. План исправления

1. Добавить независимый regression test с RED на high-quality bypass, same-candle duplicate и UI substitution.
2. Ввести exact `evidence_ts` в persistence state без schema migration.
3. Удалить one-hit bypass и требовать два distinct snapshots.
4. Сохранить exact selected `rec_id` в details refresh.
5. Синхронизировать README, trading logic, known risks и operator artifacts.
6. Повысить patch version и выполнить full post-check/repack verification.

## 16. Фактический diff по файлам

### Production
- `app/recommender.py`
- `app/main.py`

### Frontend
- `app/ui/static/app.js`
- `app/ui/static/index.html`

### Tests
- `tests/test_iteration203_signal_durability_identity.py`
- `tests/test_logic.py`
- `tests/test_iteration190_remaining_numeric_failclosed.py`
- `tests/test_iteration195_recommendation_history_ui.py`
- `tests/test_iteration197_history_horizon_rr_regression.py`

### Database/migrations
- Schema и migration SQL не изменялись.

### Docs/operator artifacts
- `README.md`
- `CHANGELOG.md`
- `docs/TRADING_LOGIC.md`
- `docs/KNOWN_RISKS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- `docs/instrukciya_operatora_bybit_recommender.docx`
- `docs/instrukciya_operatora_bybit_recommender.pdf`
- `how_to_trade.png`
- этот audit report.

## 17. RED -> GREEN evidence

RED command:

```bash
python -m pytest -q tests/test_iteration203_signal_durability_identity.py
```

Существенные RED-строки на pristine `1.0.14`:

```text
assert required_hits == 2
E assert 1 == 2
assert payload["loaded"] == "R-selected"
E AssertionError: assert 'R-newer-no-trade' == 'R-selected'
2 failed in 0.51s
```

GREEN command: тот же.

```text
2 passed in 0.44s
```

Детерминированный повтор:

```text
2 passed in 0.45s
```

Relevant suite:

```text
100 passed in 2.07s
```

## 18. Database/schema compatibility

- Schema не менялась; `migrations/init.sql` и `init_postgres.sql` не требуют правок.
- Persistence gate хранится в существующем `app_config` JSON.
- Новое поле `evidence_ts` additive. Legacy state без него загружается консервативно с `evidence_ts=0` и не может сразу подтвердить публикацию.
- Fresh/legacy SQLite и PostgreSQL dialect/locking suites входят в full suite; отдельный релевантный DB/dialect run: **18 passed**.
- Live PostgreSQL integration: SKIPPED, verified disposable DSN не предоставлен.

## 19. API compatibility

Публичные routes и JSON field names не менялись. OpenAPI smoke: version `1.0.15`, 24 routes, 18 paths. В `reasons.publication_gate` добавлен диагностический `evidence_ref_ts`; это обратно совместимое additive поле.

## 20. Config/env compatibility

Новые environment variables не добавлены. Действия пользователя с `.env` не требуются.

## 21. Security boundary

Order create/amend/cancel не добавлены. Реальные credentials, `.env`, production DB и live network calls не использовались. Recommendation/audit-only boundary сохранён.

## 22. Post-check commands и результаты

| Проверка | Результат |
|---|---|
| Targeted regression | 2 passed; повтор 2 passed |
| Relevant module/UI suite | 100 passed |
| DB/PostgreSQL dialect/locking subset | 18 passed |
| `pytest --collect-only -q` | 840 tests collected |
| `python -m pytest -q` | **840 passed in 21.18s**, exit 0 |
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| OpenAPI smoke | version 1.0.15; 24 routes; 18 paths |
| DOCX render/visual QA | 3 pages rendered; all pages inspected, no clipping/overlap after correction |
| `python -m pip check` | same environment-level MoviePy/Pillow conflict |
| Ruff | UNAVAILABLE |

## 23. Что не удалось проверить

- Real Bybit fills, account balance/margin, queue priority, latency и realised fee/slippage distribution.
- Live strategy expectancy, Sharpe, drawdown и calibration reliability on actual fills.
- Live PostgreSQL integration без disposable DSN.
- Ruff lint в текущем окружении.

## 24. Остаточные риски

1. Score остаётся heuristic regime-suitability model, а не direct expected-PnL model.
2. Hard thresholds сохраняют discontinuous status changes; это безопасный fail-closed отказ, но не smooth probabilistic lifecycle.
3. Two-snapshot confirmation уменьшает transient entries, но не гарантирует устойчивость на горизонте grid outcome.
4. Proxy labels могут калибровать неверную цель даже при корректной temporal validation.
5. Без фактических fill rows невозможно отделить model loss от execution loss.

## 25. Rollback procedure

Остановить сервис, сохранить текущую БД, вернуть архив v1.0.14 и перезапустить. Schema rollback не требуется. При rollback persistence JSON с `evidence_ts` будет проигнорирован старым кодом как дополнительное поле, но старый one-hit behavior вернётся.

## 26. Рекомендуемый следующий work package

Ввести evidence-grade live validation dataset: immutable linkage `rec_id -> operator decision -> exact order/fill snapshots -> fees/funding/slippage -> realised net PnL`, затем chronological walk-forward evaluation с baseline/no-trade comparator, regime cohorts, calibration reliability и stop criterion. До этого live использование стратегии как доказанного alpha следует приостановить или ограничить shadow/paper режимом.
