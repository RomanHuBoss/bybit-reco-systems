# Аудит принципиальной состоятельности range-edge — v1.0.20

## 1. Название итерации

**Mean-reversion edge: запрет futures grid на одном лишь отсутствии тренда и изоляция старой калибровки.**

## 2. Входной ZIP

`bybit-reco-systems-main(2).zip`

## 3. SHA-256 входного ZIP

`d403f58e6998ce079ff7577382e658693a3f8f77f53a7208a7c6ee547037def7`

Приложенный протокол: `Bybit_Recommender_Iteration_Prompt(1).pdf`, SHA-256 `1e2d759151c2df3ea6781ddcb9bead7c467d4b8c59a97546550be77a17415647`.

## 4. Исходная версия

`1.0.19`, source of truth: `version=` при создании FastAPI в `app/main.py`.

## 5. Новая версия

`1.0.20` — patch release без изменения публичных routes, JSON field names, DB schema, migrations или environment variables.

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender:

- присутствуют обязательные production, frontend, test, documentation и migration files;
- поддерживаемый `bot_type`: только `futures_grid`;
- venue scope: Bybit `category=linear`, USDT perpetual;
- сервис остаётся recommendation/audit-only и не содержит private order create/amend/cancel endpoints;
- SQLite и PostgreSQL остаются поддерживаемыми backend;
- входной ZIP содержит 230 entries и один root `bybit-reco-systems-main`;
- CRC, absolute-path, traversal, symlink, duplicate/conflicting-path и nested-archive проверки пройдены.

## 7. Цель итерации

После этой итерации система должна отличать **отсутствие направленного тренда** от **подтверждённой антиперсистентности/возвратности**, не публиковать futures-grid рекомендацию для random-walk-подобного рынка, не переиспользовать старые calibrators/outcomes, построенные на прежней семантике, и не выдавать эвристический `expected_rr` за фактическое отношение прибыли к риску.

Это исправляет подтверждённый источник ложного grid-edge, но не является доказательством положительной live-доходности.

## 8. Критерии приёмки

1. Driftless IID/random-walk path с низким trend score не получает подтверждённый mean-reversion edge.
2. Материально антиперсистентный path получает существенно более высокий score по независимым path-statistics.
3. Для публикации grid требуются валидные evidence минимум по 3 timeframes и `mean_reversion_score >= 0.55`.
4. Отсутствующее evidence блокируется кодом `MEAN_REVERSION_EVIDENCE_INSUFFICIENT`; слабое — `MEAN_REVERSION_EDGE_UNCONFIRMED`.
5. Current score/calibration identity отделены от legacy-модели; старые outcomes без нового snapshot не участвуют в fit.
6. UI явно маркирует `expected_rr` как эвристический proxy, не profitability evidence.
7. Regression test падает на pristine-коде и проходит после исправления.
8. Full suite, compileall, Node syntax, SQLite bootstrap/re-init и PostgreSQL dialect/locking tests проходят; release не содержит runtime DB, caches, `.env` или credentials.

## 9. Прочитанные источники

Проверены:

- приложенный адаптированный итерационный протокол;
- `README.md`, `CHANGELOG.md`, `requirements.txt`, `requirements-dev.txt`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`, `docs/SCENARIOS.md`, `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- последние пять audit reports и существующий project audit prompt;
- `app/features.py`, `app/direction.py`, `app/regime.py`, `app/recommender.py`, `app/calibration.py`, `app/outcomes.py`, `app/risk.py`, `app/grid_math.py`, `app/trading_semantics.py`, `app/collector.py`, `app/bybit_client.py`, `app/db_backend.py`, релевантные части `app/db.py`, `app/main.py`, `app/settings.py`, `app/llm_review.py`, `app/security.py`;
- frontend и релевантные regression tests;
- operator DOCX/PDF/PNG и их исходный Markdown.

## 10. Карта затронутого data flow

`closed OHLCV by timeframe` → `vote_for_tf()` → independent path diagnostics (`lag-1 autocorrelation`, `variance ratio`, `sign reversal rate`) → `aggregate_direction()` → multi-TF mean-reversion evidence → `_stable_range_score()` → `_mean_reversion_grid_blocks()` → publication feasibility status.

`recommendation model_version + feature_snapshot` → outcome join → `_current_range_edge_calibration_rows()` → global/bot/direction calibrators.

`expected_rr` → API/history/detail UI → explicit heuristic proxy label.

Не изменялись order execution boundary, canonical long/short PnL, grid interval geometry, sizing, leverage, persistence schema и external executor responsibilities.

## 11. Baseline environment

- Python: `3.13.5`;
- Node: `v22.16.0`;
- production Python files: 23;
- test files до итерации: 151;
- docs до итерации: 31;
- frontend files: 3;
- migration SQL files: 2;
- max существующий iteration: 207; текущий: 208;
- `ruff` отсутствовал в фактическом interpreter environment;
- `pip check` обнаружил внешний конфликт MoviePy/Pillow, не относящийся к requirements проекта.

## 12. Baseline commands и результаты

| Команда | Результат |
|---|---|
| `python --version` | PASSED — Python 3.13.5 |
| `node --version` | PASSED — v22.16.0 |
| `python -m pip check` | FAILED (environment) — MoviePy 2.2.1 требует Pillow <12, установлен Pillow 12.2.0 |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | UNAVAILABLE — `No module named ruff` |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | PASSED — 862 tests collected |
| `python -m pytest -q` | PASSED — 862 passed in 23.11s, exit 0 |

Baseline был полностью зелёным по test suite. Это и являлось частью проблемы: suite проверял внутренние контракты, но не содержал независимого null-hypothesis теста «низкий тренд без возвратности».

## 13. Подтверждённые defects/gaps

### MR-208-01 — HIGH — CONFIRMED DEFECT

- **Файлы pristine:** `app/features.py:124-128`, `app/recommender.py:1758-1779`, `app/recommender.py:1842-1849`.
- **Функции:** feature construction, `_stable_range_score()`, `_score()`.
- **Вход:** ряд с нулевым drift/низким MA slope, но без статистически выраженной антиперсистентности.
- **Путь данных:** low trend → `range_score = 1 - trend_strength` → 80% веса в stable range → положительный score contribution; затем trend penalized отдельно.
- **Фактическое поведение:** отсутствие тренда почти автоматически трактовалось как положительное качество для grid. Одна и та же характеристика учитывалась как положительный range factor и повторно как отсутствие отрицательного trend factor.
- **Ожидаемое поведение:** отсутствие тренда — необходимое, но недостаточное условие; требуется независимое evidence повторяемого возврата/осцилляции.
- **Нарушенный инвариант:** score не должен создавать executable economic edge из недоказанного fallback-признака.
- **Финансовое влияние:** random walk до costs не создаёт доказанного положительного ожидания самофинансируемой стратегии; commissions/spread/slippage/funding делают такой false-positive экономически отрицательным.
- **Trading/risk влияние:** система могла отбирать и публиковать grid в рынке, где единственное «доказательство диапазона» — отсутствие выраженного направления.
- **Почему старые тесты не поймали:** tests проверяли формулы и thresholds проекта, но не сравнивали IID null path с действительно антиперсистентным path независимым oracle.
- **RED:** pristine не содержит `mean_reversion_diagnostics`; новый test suite останавливается с ImportError.
- **Fix:** добавлены lag-1 return autocorrelation, 4-step variance ratio и sign-reversal diagnostics; новый evidence доминирует в stable range score; publication fail-closed ниже 0.55 или при недостаточной multi-TF coverage.
- **Monte Carlo regression:** 0/200 IID AR(1) paths с `phi=0` прошли threshold; 154/200 paths с `phi=-0.35` прошли threshold.
- **Остаточный риск:** эти statistics подтверждают антиперсистентность, но сами по себе не доказывают net alpha после fill mechanics и regime drift.

### CAL-208-02 — HIGH — CONFIRMED DEFECT

- **Файлы pristine:** `app/calibration.py:825-832`, calibration load/fit paths в `app/recommender.py`.
- **Вход:** persisted v3 calibrator и legacy outcome rows, созданные score-моделью `range = 1 - trend`.
- **Фактическое поведение после одного только score-fix:** старые models/outcomes могли продолжить преобразовывать новый raw score по старой зависимой выборке и частично вернуть ложный edge через confidence.
- **Ожидаемое поведение:** изменение feature semantics требует новой model identity, новых calibration keys и фильтрации train rows по current model/snapshot contract.
- **Нарушенный инвариант:** train/inference schema identity и calibration integrity.
- **Финансовое влияние:** stale confidence мог повысить вероятность actionable status для модели, смысл признаков которой уже изменился.
- **Почему старые тесты не поймали:** keys считались стабильными implementation details, а outcome query не экспортировал `model_version` для строгой фильтрации.
- **Fix:** `RECOMMENDER_MODEL_VERSION=bybit-taxonomy-v3-mean-reversion`; v4 global/bot/direction calibration keys; outcome join экспортирует `model_version`; fit принимает только current rows с валидным mean-reversion snapshot.
- **Остаточный риск:** после релиза калибровка начнёт с недостаточной current-sample истории и должна оставаться консервативной до накопления данных.

### UI-208-03 — MEDIUM — CONFIRMED DEFECT

- **Файлы pristine:** `_expected_rr()` в `app/recommender.py:1930-1960`, history/detail rendering.
- **Фактическое поведение:** поле называлось profit/risk/RR, хотя вычислялось как bounded heuristic capture-to-volatility ratio и не использовало фактическую loss geometry конкретного trade lifecycle.
- **Ожидаемое поведение:** либо рассчитывать конкретный net monetary reward/risk, либо явно маркировать поле как ranking proxy.
- **Нарушенный инвариант:** операторский UI не должен создавать misleading economic precision.
- **Финансовое влияние:** оператор мог принять proxy за прогнозируемое отношение прибыли к убытку.
- **Fix:** API reasons содержит `expected_rr_semantics`; UI показывает «Прокси capture/risk» / «Прокси C/R» и прямое предупреждение, что это не фактический reward/risk и не доказательство прибыльности.
- **Остаточный риск:** JSON field name сохранён ради compatibility; внешние consumers должны учитывать новую documented semantics.

### EDGE-208-04 — HIGH — DOCUMENTED LIMITATION

- В проекте нет доказательства live positive expectancy на независимой хронологической выборке с реальными fills, queue/partial-fill effects, funding, latency и regime drift.
- Unit/integration tests, proxy outcomes, calibration и даже новый mean-reversion gate доказывают корректность контрактов и снижают false-positive risk, но не доказывают прибыльность.
- Это не устраняется добавлением ещё одного heuristic score. Требуется отдельный walk-forward/live-evidence work package.

## 14. Неподтверждённые claims и итоговая оценка состоятельности

Утверждение **«проект полностью несостоятелен априори» не доказано**. Recommendation/audit architecture, canonical directional semantics, risk gates, persistence и lifecycle могут быть технически корректными.

Однако подтверждено, что версия 1.0.19 **не имела независимого доказательства основного экономического предположения grid-стратегии**: она практически отождествляла low trend и range edge. Поэтому опасение систематической убыточности было обоснованным: на random-walk-подобном рынке валовая положительная доходность не следовала из модели, а costs оставались реальными.

Версия 1.0.20 переводит этот класс ситуаций в fail-closed и устраняет загрязнение старой калибровкой. Она **безопаснее и логически состоятельнее**, но всё ещё не является доказанно прибыльной стратегией.

Не заявляется, что найдены все ошибки. Закрыт один наиболее приоритетный связный economic-model work package.

## 15. План исправления

1. Добавить независимый null-hypothesis regression test: IID low-trend path против anti-persistent path.
2. Рассчитать path diagnostics без использования существующего range score как oracle.
3. Агрегировать evidence по timeframes и установить minimum coverage.
4. Сделать publication gate fail-closed при отсутствующем/слабом evidence.
5. Изменить model identity и calibration keys.
6. Исключить legacy outcomes из нового fit.
7. Уточнить semantics `expected_rr` в API/UI/docs.
8. Обновить version, changelog, technical/operator docs и release artifacts.
9. Выполнить full post-check, clean packaging и re-extraction verification.

## 16. Фактический diff по файлам

### Production

- `app/direction.py` — independent mean-reversion diagnostics и multi-TF aggregation.
- `app/recommender.py` — новый stable range model, fail-closed publication blocks, current-model calibration filtering, model identity, `expected_rr` semantics.
- `app/calibration.py` — v4 calibrator keys.
- `app/db.py` — `model_version` в outcome/recommendation join.
- `app/main.py` — version 1.0.20 и v4 direction calibration reset key.

### Tests

- новый `tests/test_iteration208_mean_reversion_edge.py` — 8 independent regression tests, включая 200-seed IID/AR Monte Carlo contract.

### Frontend

- `app/ui/static/index.html` — proxy label и warning.
- `app/ui/static/app.js` — detail label и cache identity.

### Database/migrations

- SQL schema и migrations не изменялись.
- Query shape расширен только чтением существующего `recommendations.model_version`.

### Documentation/operator artifacts

- `README.md`;
- `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `docs/SCENARIOS.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- `docs/instrukciya_operatora_bybit_recommender.pdf`;
- `how_to_trade.png`;
- этот audit report.

## 17. Red → green evidence

### RED

```bash
cd red
python -m pytest -q tests/test_iteration208_mean_reversion_edge.py
```

Существенная строка:

```text
ImportError: cannot import name 'mean_reversion_diagnostics' from 'app.direction'
1 error during collection in 0.19s
```

До финального объединения теста отдельный red-run также дал 7 ожидаемых failures: отсутствовали MR fields/gate/model identity/UI semantics.

### GREEN

```bash
cd working
python -m pytest -q tests/test_iteration208_mean_reversion_edge.py
```

```text
8 passed in 0.41s
```

Повторный deterministic run: `8 passed`.

Relevant logic suite:

```text
91 passed
```

## 18. Database/schema compatibility

Schema и migrations не изменялись.

Проверено:

- fresh SQLite bootstrap: 17 tables;
- повторный `init_db()`: 17 tables, idempotent;
- core tables присутствуют;
- PostgreSQL translation/dialect/locking suite: 24 passed.

Live PostgreSQL integration: **SKIPPED** — безопасный disposable DSN не предоставлен.

Действия пользователя по БД: отсутствуют. Existing DB продолжит работать; новые v4 calibrators будут накапливаться под новыми keys.

## 19. API compatibility

- route names, request/response field names и status lifecycle не изменялись;
- legacy `expected_rr` JSON field сохранён, но его semantics теперь явно документирована как heuristic proxy;
- model version обновлена, что намеренно разделяет publication/calibration identity;
- FastAPI version: `1.0.20`.

## 20. Config/env compatibility

- новые environment variables не добавлялись;
- `.env.example` не требует изменения;
- действия пользователя по `.env`: отсутствуют;
- production credentials не использовались и не включены в release.

## 21. Security boundary

- project остаётся recommendation/audit-only;
- private Bybit order create/amend/cancel endpoints отсутствуют;
- network/live credentials не использовались;
- secrets/DSN не выводились;
- release проверяется на `.env`, key/pem artifacts, runtime DB и caches;
- LLM reviewer не может отменить новый deterministic hard block.

## 22. Post-check commands и результаты

| Проверка | Результат |
|---|---|
| targeted iteration 208 | PASSED — 8 passed in 0.39s, повторено |
| relevant logic + iteration 208 | PASSED — 91 passed in 1.15s |
| full collection | PASSED — 870 collected in 1.08s |
| full pytest | PASSED — 870 passed in 23.89s |
| compileall | PASSED |
| Node syntax | PASSED |
| SQLite fresh/re-init | PASSED — 17/17 tables |
| PostgreSQL dialect/locking tests | PASSED — 24 passed in 1.66s |
| Monte Carlo contract | PASSED — IID 0/200; AR(-0.35) 154/200 |
| `pip check` | FAILED (environment-only MoviePy/Pillow conflict) |
| Ruff | UNAVAILABLE — module not installed |
| operator DOCX/PDF render | PASSED — 5 pages, visual inspection без clipping/overlap |
| private order endpoint scan | PASSED — 0 hits |

Release-копия дополнительно проверяется после повторной распаковки: fingerprint, один root, отсутствие мусора, compileall, Node syntax и targeted iteration 208.

## 23. Что не удалось проверить и почему

- Реальная прибыльность/Sharpe/drawdown — нет независимого historical/live fill dataset в архиве.
- Queue priority, partial fills и exchange latency — recommendation service не содержит OMS execution truth.
- Live Bybit network behavior — не использовались production/test credentials и сетевой smoke не требовался для локального defect.
- Live PostgreSQL — нет заведомо disposable DSN.
- Ruff — module отсутствует в текущем interpreter.
- `pip check` не зелёный из-за внешнего MoviePy/Pillow конфликта глобального environment, не входящего в project requirements.

## 24. Остаточные риски

1. Порог 0.55 и weights diagnostics являются консервативными эвристиками; их нужно валидировать walk-forward, а не подгонять на одном периоде.
2. Антиперсистентность может исчезать быстрее TTL; execution preflight должен использовать свежие closed data.
3. Mean reversion может быть статистической, но недостаточной по амплитуде после fees/spread/slippage/funding.
4. Current v4 calibration сначала будет иметь мало samples; confidence нельзя интерпретировать как доказанную probability of profit.
5. Outcome proxy не реконструирует реальный order-by-order grid inventory path.
6. Внешний executor обязан независимо контролировать fills, inventory, kill-switch и reconciliation.

## 25. Rollback procedure

1. Остановить сервис.
2. Вернуть предыдущий verified ZIP v1.0.19 и прежний application directory.
3. Existing database rollback не требуется: schema не менялась.
4. При необходимости удалить только новые v4 calibration rows/keys; legacy v3 rows остаются совместимыми с v1.0.19.
5. Запустить compileall, targeted smoke и health checks перед возвратом операторского доступа.

Rollback возвращает прежнее поведение `range = absence of trend`; использовать его для новых торговых решений не рекомендуется.

## 26. Рекомендуемый следующий work package

**Chronological economic validation of complete grid inventory.**

Нужно построить независимый walk-forward evaluator по immutable recommendations и реальным execution evidence, который:

- разделяет decision time, availability time и fill time;
- моделирует order-by-order inventory, partial fills и kill-switch;
- использует actual fees, spread/slippage и signed funding;
- сравнивает новую MR-gated модель с no-trade/null и прежней v3 моделью;
- применяет purged/embargoed chronological splits;
- отчётливо отделяет gross edge, net edge, drawdown и uncertainty;
- не повышает confidence/thresholds по final validation period.

Только такой пакет может ответить, существует ли у системы net live-like edge, а не только корректна ли её программная логика.
