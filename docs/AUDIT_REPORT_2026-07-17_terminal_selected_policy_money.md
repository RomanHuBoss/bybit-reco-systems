# Итерация 262: terminal-selected monetary holdout

## 1. Название итерации

**v1.0.75 — fail-closed денежная проверка exact-policy на terminal holdout.**

## 2. Входной ZIP

`bybit-reco-systems-main.zip`, 341 archive entries, единственный root `bybit-reco-systems-main/`.

## 3. SHA-256 входного ZIP

`e81f179449acce27861ee526414c7c4b33503a2b9a598e184f71f94d64984a13`

Архив прошёл `unzip -t`; absolute/traversal paths, duplicate/conflicting paths, symlinks и вложенные архивы не обнаружены. Входной ZIP не изменялся. Для работы созданы отдельные pristine, red-test и working copies.

## 4. Исходная версия

`1.0.74`, source of truth: `FastAPI(..., version="1.0.74")` в `app/main.py`.

## 5. Новая версия

`1.0.75` (patch). Frontend cache build синхронизирован с `1.0.75`.

## 6. Project fingerprint

Fingerprint совпал с Bybit Recommender:

- recommendation/audit service, не OMS/EMS;
- Bybit `category=linear`, USDT perpetual, `bot_type=futures_grid`;
- FastAPI entry point `app/main.py`, UI `app/ui/static/`;
- canonical direction semantics `app/trading_semantics.py`;
- dual persistence SQLite/PostgreSQL;
- реальные order create/amend/cancel endpoints отсутствуют.

Baseline inventory: 24 production Python files, 207 test files, 85 docs files, 3 frontend files, 2 migration SQL files; максимальная предыдущая итерация — 261; 22 `/api/` routes, из них 7 mutating audit/operator routes; 6 обязательных background loops и 1 optional LLM reviewer loop.

## 7. Цель итерации

Не допустить активацию вероятностной модели, когда exact confidence-selected policy прибыльна на объединённых старых OOF-строках, но убыточна в последнем whole-timestamp terminal holdout. Binary log-loss skill не должен подменять недавнее денежное evidence.

## 8. Критерии приёмки

1. Exact runtime selector повторно применяется к terminal OOF block.
2. Terminal-selected subset имеет отдельные row-level и temporal monetary diagnostics.
3. Для активации обязательны не менее 80 selected rows, 5 целых decision timestamps и оба положительных lower bounds.
4. `negative`, `uncertain`, `insufficient` и неизвестное evidence оставляют модель unfitted.
5. Fitted cache без нового terminal-selected contract отклоняется.
6. API/status, recommendation payload и UI объясняют новый gate.
7. SQLite/PostgreSQL support, routes, `.env` и outcome label contract не меняются.
8. Новый независимый regression падает на pristine и проходит после исправления; полный suite зелёный.

## 9. Прочитанные источники

- пользовательский `Bybit_Recommender_Iteration_Prompt.pdf` (редакция 10 июля 2026);
- `bybit-recommender-diagnostics-2026-07-17T05-14-15-709Z.json`;
- `README.md`, `CHANGELOG.md`, dependency manifests и `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`, текущий audit prompt;
- последние отчёты `health_outcomes_latency`, `llm_shutdown_liveness`, `selected_policy_terminal_holdout` и релевантные предшествующие reports;
- calibration/recommendation/outcome/risk/grid/direction/feature/collector/Bybit/DB/security/LLM/UI paths и релевантные tests.

Диагностика снята `2026-07-17T05:14:13.355Z` с локального `http://localhost:8000/` и показывает v1.0.74, PostgreSQL, 281 850 recommendations, 29 078 outcomes и 119 163 decision-log rows. Все 29 078 outcomes относятся к историческим model contracts; current-model, feature-eligible и calibration-eligible counts равны нулю. Calibrator unfitted/raw-only. Все 7 background loops — `running`; health — 35 `ok`, 0 stale/missing/disabled, 0 errors за 10 минут. Последний snapshot: 33 `no_trade`, 2 `blocked`, 0 actionable. Из 200 последних decisions: 105 `STALE_DATA_SKIP` вокруг restart, 44 sentiment collects, 40 publications, 9 fail-closed `OUTCOME_SKIP_INVALID_GRID_CONTRACT` с `intrabar_extreme_order_unobservable`, 2 DB prune.

Эти данные подтверждают безопасное текущее `no_trade` состояние и отсутствие пригодной current-model когорты, которую мог бы потерять identity bump. Они не содержат наблюдаемого terminal reversal и не заменяют отдельный математический reproducer.

## 10. Карта затронутого data flow

`exact-policy matured outcomes` → purged whole-timestamp OOF folds → feature probability → shared adaptive confidence transform → aggregate selected-policy monetary evidence → **terminal selected-policy monetary evidence** → strict fitted-cache persistence/load → recommendation probability gate → `confidence_model`/`/api/v1/status` → operator UI.

Outcome labeling, direction target, deterministic risk/economic gates, publication-chain, database schema и operator audit lifecycle находятся вне изменённой границы.

## 11. Baseline environment

- Python `3.12.13`;
- Node.js `v24.14.0`;
- отдельный venv `/tmp/bybit-audit-venv` вне project root;
- runtime: FastAPI 0.115.6, uvicorn 0.34.0, httpx 0.27.2, Pydantic 2.10.6, python-dotenv 1.0.1, cryptography 44.0.1, scikit-learn >=1.3, tzdata >=2024.1, psycopg 3.2.12;
- dev: pytest 9.0.2, pytest-cov 7.0.0, ruff 0.15.9;
- package manifest для npm/yarn отсутствует, поэтому npm-команды не запускались.

## 12. Baseline commands и точные результаты

| Проверка | Результат |
|---|---|
| `python -m pip check` | PASSED — `No broken requirements found.` |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | FAILED — 24 существующих finding; 8 auto-fixable, массовое исправление вне scope |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pytest --collect-only -q` | PASSED — 1193 collected |
| `python -m pytest -q` с унаследованным `ALL_PROXY=socks5h://...` | FAILED — 26 httpx proxy-scheme failures, 1167 passed; environment artifact |
| тот же pytest с удалёнными proxy variables | PASSED — `1193 passed in 76.20s`, exit 0 |

Ruff baseline повторно подтверждён на pristine: `Found 24 errors`. Входной ZIP не содержал `.env`, credentials, caches, bytecode или database files.

## 13. Подтверждённые defects/gaps

### HIGH — recent monetary reversal selected-policy не блокировал activation

v1.0.74 проверяла binary feature-model log-loss на terminal whole-timestamp block, но monetary lower bounds exact confidence-selected policy считала только по объединённым OOF rows. Старые прибыльные selected rows могли скрыть убыток последних cohorts, после чего `fit_logreg()` возвращал `fitted=True`.

Независимый fixed-time reproducer: 40 cohorts × 30 rows = 1200 rows. Вся candidate-когорта имеет mean `+0.326667%`. Aggregate exact-policy OOF subset: 660 rows, mean `+0.219394%`, row LCB `+0.199228%`, temporal LCB `+0.176448%`. Terminal 5 cohorts: 100 selected rows, mean `−0.120000%`, row LCB `−0.193425%`, temporal LCB `−0.120000%`. Binary terminal log-loss при этом лучше baselines: feature `0.500486`, score `0.673012`, null `0.673012`. Pristine вернула `fitted=True`.

Это model/risk fail-open: недавнее отрицательное денежное evidence не останавливало probability inference. Дефект latent в приложенной диагностике, поскольку current-model outcomes = 0 и runtime уже fail-closed, но потенциальное последствие для следующей накопленной когорты соответствует HIGH.

### HIGH — fitted cache не доказывал terminal-selected monetary contract

Кэш мог иметь positive aggregate selected-policy evidence и загружаться fitted без отдельной terminal-selected секции. После добавления runtime gate это создавало бы persistence bypass. Pristine regression фактически загрузил такой payload вместо отклонения.

## 14. Неподтверждённые claims

- Диагностический JSON не доказывает, что дефект уже породил live-loss или actionable recommendation: actionable count = 0, current-model outcomes = 0.
- 105 restart-time stale skips не воспроизводят liveness defect: итоговая health snapshot полностью восстановлена.
- 9 invalid-grid outcome skips имеют явную OHLCV-неопределённость и являются ожидаемым fail-closed censoring, а не доказанным дефектом.
- Profitability/live edge, real fills, queue priority, fees/funding reconciliation и production readiness не подтверждались.
- Отдельные claims внешних источников без независимого reproducer не повышались до defect.

## 15. План исправления

1. Применить общий `selected_policy_confidence()` к `final_indices` того же purged OOF run.
2. Рассчитать отдельные terminal-selected row/temporal diagnostics с floor 80 rows/5 timestamps.
3. Включить positive terminal status в fit, load и recommendation gates.
4. Сделать persistence schema fail-closed для fitted payload.
5. Обновить model/policy/calibrator lineage, API diagnostics и UI explanation.
6. Добавить RED→GREEN regression, обновить только необходимые старые positive fixtures/version contracts.

## 16. Фактический diff по файлам

**Production**

- `app/calibration.py` — terminal-selected diagnostics, fit/load/persistence gate, calibrator v21;
- `app/recommender.py` — model v10/policy v3, probability gate и confidence diagnostics;
- `app/main.py` — app 1.0.75 и additive status/contract fields.

**Frontend**

- `app/ui/static/app.js` — локализованный terminal-selected readiness text;
- `app/ui/static/index.html` — cache build 1.0.75.

**Tests**

- новый `tests/test_iteration262_terminal_selected_policy_monetary.py`;
- complete-positive fixtures обновлены в iteration 242, обоих iteration 245 и iteration 261;
- lineage/version assertions синхронизированы в iterations 208, 213–226, 229, 231, 238, 240, 244 и 256.

**Docs**

- `README.md`, `CHANGELOG.md`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- этот audit report.

**Database/migrations/config**

- изменений нет; hashes обоих migration SQL совпадают с pristine; `.env.example` не менялся.

## 17. Red → green evidence

RED на отдельной red-copy с единственным новым test-файлом:

```text
env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY -u all_proxy -u https_proxy -u http_proxy \
  PYTHONDONTWRITEBYTECODE=1 /tmp/bybit-audit-venv/bin/python -m pytest -q \
  -p no:cacheprovider tests/test_iteration262_terminal_selected_policy_monetary.py
```

Существенные строки: `AssertionError: assert True is False`, `assert loaded is None` при фактически загруженном fitted model; итог `2 failed in 5.79s`, exit 1.

GREEN тем же command в working copy: `2 passed in 5.42s`; независимый повтор: `2 passed in 5.48s`, оба exit 0. Релевантный calibration suite: `51 passed in 15.12s`.

Новый test использует fixed timestamp, явную денежную истину и не вызывает сеть/production DB.

## 18. Database/schema compatibility

- SQLite fresh schema и повторный `init_db()` на disposable temp DB: PASSED, 20 tables.
- Existing SQLite additive-upgrade tests: PASSED в составе explicit DB suite.
- PostgreSQL translation, locking, integrity, deadlock и transaction-order tests: PASSED; combined SQLite/PostgreSQL-dialect suite — `28 passed in 2.76s`.
- `migrations/init.sql` SHA-256: `80c3d8c3fa59f63ec220debf58c4f38d70f7621bb062f79ea4ac1e7a05045d6a` — unchanged.
- `migrations/init_postgres.sql` SHA-256: `6157b2da066ded3d23f865195105bf015bdce260339d4ec6e636c4d3436d6ec5` — unchanged.
- Live PostgreSQL integration: SKIPPED, disposable DSN явно не предоставлен; диагностическая production-like PostgreSQL instance не использовалась.

Пользовательские DB migrations не требуются. Исторические outcomes сохраняются; новые v10/v3/v21 identities начинают отдельную exact-policy cohort.

## 19. API compatibility

22 API routes и 7 mutating operator/audit routes сохранены. Удалённых/переименованных routes и request fields нет. `/api/v1/status` и `confidence_model` получили только additive terminal-selected diagnostic fields. HTTP order execution не добавлен.

## 20. Config/env compatibility

Environment variables, defaults и `.env.example` не менялись. Действия пользователя по config не требуются. `REQUIRE_CONF_GATE=1` продолжает fail-closed; новый gate является частью model evidence, а не новым operator setting.

## 21. Security boundary

- static search по application code не нашёл `/v5/order/create`, amend/cancel/batch endpoints или SDK-equivalent order methods;
- реальные API keys, `.env`, credentials и production database в release не включаются;
- сервис остаётся recommendation/audit-only;
- attached diagnostics использовалась read-only; локальный URL и `admin_key_configured=false` сами по себе не трактовались как remotely exploitable finding;
- сетевые Bybit/private smoke tests и реальные торговые действия не выполнялись.

## 22. Post-check commands и точные результаты

| Проверка | Результат |
|---|---|
| `python -m pip check` | PASSED — no broken requirements |
| `python -m compileall -q app tests main.py` | PASSED |
| `python -m ruff check .` | FAILED — те же 24 baseline finding, `delta=0` |
| `ruff check app/calibration.py tests/test_iteration262_terminal_selected_policy_monetary.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| `pytest --collect-only -q -p no:cacheprovider` | PASSED — 1195 collected in 5.25s |
| финальный sanitized `pytest -q -p no:cacheprovider` | PASSED — 1195 passed in 58.02s, 0 failed/skipped/xfailed/xpassed/errors, exit 0 |
| новый regression, два запуска | PASSED — 2 passed in 5.42s; 2 passed in 5.48s |
| SQLite fresh/repeated init | PASSED — 20 tables |
| SQLite upgrade + PostgreSQL dialect suite | PASSED — 28 passed in 2.76s |
| forbidden private-order static scan | PASSED — 0 matches in application code |
| re-extracted release compileall/Node/targeted regression | PASSED |

Final release проверен на один root, отсутствие traversal/symlink/duplicate paths, secrets, caches, bytecode и database artifacts; все изменённые production/frontend/test/doc files присутствуют.

## 23. Что не удалось проверить и почему

- Live PostgreSQL integration не запускался без явно disposable DSN; production-like instance из diagnostics намеренно не использовалась.
- Реальный Bybit, Ollama, market stream, fills и private account не вызывались: offline suite достаточен для выбранного калибровочного scope и безопаснее.
- Поведение под будущим рыночным режимом, live profitability и execution slippage не доказаны.
- Ruff остаётся красным на 24 исторических finding; текущий scope не добавил новых.

## 24. Остаточные риски

- 5 timestamps не равны 5 независимым режимам и не доказывают out-of-regime edge.
- Floor 80 terminal-selected rows может продлевать штатный `no_trade` при редком прохождении confidence threshold; снижать его ради liveness нельзя.
- OHLCV proxy не моделирует queue priority, partial fills, market impact, реальные fees/funding и account-level cross-margin PnL.
- После identity bump необходимо накопить новую exact-policy outcome cohort; 29 078 исторических outcomes остаются archive, а не evidence v1.0.75.
- Terminal gate ловит наблюдаемый recent reversal, но не гарантирует отсутствие иных distribution shifts.

## 25. Rollback procedure

1. Остановить v1.0.75 штатным shutdown и дождаться release runtime locks.
2. Вернуть предыдущий v1.0.74 application ZIP; DB rollback/migration не требуется.
3. Не переименовывать v21 cache в v20 и не копировать v10/v3 evidence в старую cohort.
4. После запуска проверить database instance ID, thread ownership/freshness и оставить рекомендации `no_trade`, пока v1.0.74 gates не подтвердят собственную cohort.

Rollback возвращает известный HIGH-риск этой итерации, поэтому допустим только как временная операционная мера.

## 26. Рекомендуемый следующий work package

Добавить отдельный deterministic multi-regime backtest/report для terminal-selected gate: rolling whole-timestamp terminal windows, block-bootstrap uncertainty по режимам и sensitivity по confidence threshold **без** изменения production floors. Цель — измерить liveness/false-activation trade-off на архивной proxy-history; это не должно автоматически разрешать торговлю или заявлять live edge.
