# Audit iteration 245 — policy-conditioned calibration and reconciled evidence

## 1. Название итерации

Bybit Recommender v1.0.57 — policy-conditioned calibration, censor-aware outcomes и terminal exchange reconciliation.

## 2. Входной ZIP

`bybit-reco-systems-main(1).zip`.

Архив распакован в новый каталог; исходный ZIP не изменялся. Фактический единственный root: `bybit-reco-systems-main`.

## 3. SHA-256 входного ZIP

`395f5a8bccd565f90527bbac8b9b4596f99284eb2a591fb8f53f09770de9caae`.

## 4. Исходная версия

- FastAPI: `1.0.56`, source of truth — `app/main.py`.
- Recommendation lineage: `bybit-taxonomy-v7-mr-floor-temporal-cohorts`.
- Bot/global calibrators: v18; direction calibrator: v13.
- Outcome target: `grid_label_v26`.
- Последняя завершённая audit iteration: 244.

## 5. Новая версия

- FastAPI: `1.0.57`.
- Recommendation lineage: `bybit-taxonomy-v8-policy-conditioned-censor-aware`.
- Bot/global calibrators: v19; direction calibrator: v14.
- Outcome target остаётся `grid_label_v26`: proxy execution math не менялась.
- Новая audit iteration: 245.

## 6. Project fingerprint

**PASSED.** Найдены все обязательные файлы из протокола: корневые README/CHANGELOG/dependency entry points, FastAPI backend, recommender, canonical directional/grid/risk/calibration/outcome/DB modules, SQLite/PostgreSQL migrations, static frontend, tests и required operator artifacts.

Устойчивые признаки подтверждены:

- продукт — Bybit Recommender recommendation/audit service, не OMS/EMS;
- единственный bot scope — `futures_grid`;
- биржевой scope — Bybit `category=linear`, USDT perpetual;
- persistence — SQLite и PostgreSQL;
- FastAPI app создаётся в `app/main.py`;
- frontend находится в `app/ui/static/`;
- directional source of truth остаётся `app/trading_semantics.py`;
- private Bybit order create/amend/cancel capability отсутствует.

## 7. Цель итерации

Исправить один связный P0/P1 work package: не позволять calibration/live-validation представлять несопоставимые, неотделённые политикой, цензурированные, in-sample или локально самоподтверждённые данные как доказательство положительного денежного ожидания либо готовой live-прибыли.

Итерация не должна заявлять прибыльность. Без достаточных policy-matched, observable и held-out данных система обязана оставаться fail-closed `no_trade`/unfitted.

## 8. Критерии приёмки

1. Monetary inference использует только outcomes, допущенные тем же полным policy contract, который публиковал рекомендацию.
2. SHA-256 policy fingerprint вычисляется из canonical contract, а не принимается на доверии из JSON.
3. Каждый matured policy root входит в знаменатель как `labeled`, `censored` или `unresolved`; пропуски блокируют положительное заключение.
4. Feature LogReg активируется только при достаточном purged chronological OOF skill против score-only и null baselines, включая terminal future block.
5. Terminal block не попадает в активный final fit; in-sample score-only Platt не используется как decision confidence.
6. Нижняя граница малого числа temporal clusters использует one-sided Student-t, а не normal 1.645.
7. Direction target — знак horizon exit относительно entry, не прибыль whole-grid outcome; непроверенный standalone Platt остаётся audit-only.
8. Waiting/censored roots не голодают очередь outcome worker.
9. Положительный live exact PnL требует stopped bot, локально полный/flat ledger и совпадающую terminal external reconciliation; потери учитываются консервативно и без неё.
10. Схемы SQLite/PostgreSQL обновляются additively и idempotently; upgrade существующей SQLite сохраняет данные.
11. Public API не теряет существующие поля/routes; новые поля и routes additive и admin-protected там, где меняют audit state.
12. Новые targeted tests проходят дважды, весь suite, offline PostgreSQL, SQLite fresh/re-init/upgrade и re-extracted release checks проходят.

## 9. Прочитанные источники

Порядок доверия соблюдён: текущее требование пользователя; фактический ZIP; runtime code и SQL; tests; README; KNOWN_RISKS; TRADING_LOGIC; ARCHITECTURE; MODULES; SCENARIOS; последние audit reports; CHANGELOG; приложенный `Bybit_Recommender_Iteration_Prompt.pdf` (редакция 10 июля 2026 г.).

Отдельно разобраны три последние итерации:

| Итерация | Что исправляла | Обнаруженный оставшийся глубокий риск |
|---|---|---|
| 242 / v1.0.54 | Запрет feature coefficients без достаточного purged OOF | Разрешала in-sample score-only Platt fallback; проверяла наличие OOF, но не превосходство feature model над score/null; после terminal selection выполняла fit по всей выборке |
| 243 / v1.0.55 | Достижимый mean-reversion floor и temporal cohort recovery | Материально меняла selection policy, но outcomes/calibration не были привязаны к полному policy contract; отклонённые той же политикой строки могли влиять на денежный gate |
| 244 / v1.0.56 | Reset model lineage и честные archive/current/eligible counts | Разделяла model version, но не варианты policy внутри lineage; inner-join по outcome исключал censored roots из знаменателя, поэтому положительная наблюдаемая подвыборка могла выглядеть полной |

Вывод: каждая из трёх итераций закрыла реальный локальный дефект, но их композиция всё ещё позволяла selection/survivorship leakage и ложную live finalization. Это подтверждено новыми RED-тестами, а не только ревью документации.

## 10. Карта затронутого data flow

`settings + risk limits + universe + reviewer gate` → canonical policy contract → SHA-256 fingerprint → recommendation/audit snapshot → independent shadow root → maturity/observability ledger → labeled/censored/unresolved denominator → policy-matched calibration rows → temporal thinning → Student-t monetary bound → purged walk-forward feature/score/null comparison → terminal holdout → active v19 model or fail-closed no model → recommendation confidence gate.

Отдельный live-evidence поток:

`operator execution events + funding events` → local signed ledger → stopped/flat completeness → immutable external terminal reconciliation → amount/count/position matching → finalized exact net PnL → loss-conservative risk stream.

## 11. Baseline environment

- OS/container timezone: Asia/Almaty; audit date `2026-07-14`.
- Python: `3.12.13` in isolated audit venv.
- Node: `v24.14.0`.
- Runtime dependency lock: FastAPI 0.115.6, Uvicorn 0.34.0, HTTPX 0.27.2, Pydantic 2.10.6, cryptography 44.0.1, psycopg[binary] 3.2.12, scikit-learn >=1.3.0, tzdata >=2024.1.
- Dev tools: pytest 9.0.2, pytest-cov 7.0.0, Ruff 0.15.9.
- Baseline inventory: 23 production Python files, 188 test files plus `conftest.py`, 67 docs, 3 frontend files, 2 migrations, 22 API routes, 6 mutating POST routes, 6 explicit background loops.

## 12. Baseline commands и точные результаты

В pristine copy до production-правок:

| Проверка | Точный результат |
|---|---|
| `python -m pytest --collect-only -q` | `1082 tests collected in 0.58s` |
| `python -m pytest -q` | `1082 passed in 19.60s` |
| `python -m compileall -q app tests` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pip check` | `No broken requirements found.` |
| `ruff check app tests` | FAILED: 24 pre-existing findings — 9 F841, 7 F401, 6 E402, 2 E741 |

Baseline Ruff findings зафиксированы как технический долг; они не скрывались и не объявлялись результатом этой итерации.

## 13. Подтверждённые defects/gaps

### PCER-01 — CRITICAL — policy-selection contamination

- **Reproducer:** rejected rows с плохим `ret` меняли monetary gate кандидата, хотя тот же mean-reversion floor не допускал их к действующей политике; альтернативно rejected rows могли veto profitable admitted cohort.
- **Root cause:** evidence rows фильтровались по model lineage и feature shape, но не по полному selection/risk/universe/reviewer contract.
- **Impact:** калибратор и денежный stop могли отвечать на другой вопрос, чем текущая стратегия; sign expectancy мог быть как искусственно ухудшен, так и улучшен.
- **Fix:** canonical policy contract + SHA-256 fingerprint сохраняются в recommendation/outcome metadata, входят в dynamic cache key и являются обязательным фильтром fit/status.
- **Regression:** первые два теста в `test_iteration245_policy_conditioned_calibration.py`.
- **Residual:** fingerprint доказывает равенство конфигурации, но не оптимальность самой политики.

### PCER-02 — HIGH — persisted policy digest принимался на доверии

- **Reproducer:** JSON contract можно было изменить, сохранив старое заявленное поле digest.
- **Root cause:** сравнивалась строка `policy_fingerprint`, без повторной canonical serialization и hashing.
- **Impact:** повреждённая или подменённая audit row могла стать training support.
- **Fix:** новый `app/policy.py` — единый canonical JSON/SHA-256 source of truth; loader и observability всегда пересчитывают digest и отклоняют tampered/missing contracts.
- **Regression:** `test_claimed_policy_digest_is_recomputed_from_persisted_contract`, `test_tampered_policy_contract_cannot_become_a_labeled_support_row`.
- **Residual:** это integrity check внутри DB, не криптографическая подпись внешнего источника.

### PCER-03 — HIGH — OOF presence без доказанного comparative skill

- **Reproducer:** feature model мог активироваться при достаточном количестве OOF logits, даже когда log-loss не превосходил score-only/null baselines.
- **Root cause:** v1.0.54 проверяла count/fitted Platt, но не требовала comparative predictive skill на future folds.
- **Impact:** более сложная модель могла ухудшать probability estimates и всё равно называться calibrated.
- **Fix:** purged walk-forward сравнение feature vs score-only vs null на aggregated OOF и terminal future block; требуется минимальное улучшение на обоих слоях.
- **Regression:** `test_feature_model_requires_oof_skill_over_score_and_null_baselines`.
- **Residual:** log-loss на историческом proxy target не доказывает устойчивость следующего market regime.

### PCER-04 — HIGH — terminal holdout leakage и in-sample probability fallback

- **Reproducer:** после выбора terminal candidate v1.0.56 могла refit model на всех rows, включая terminal future block; score-only Platt обучался на полной выборке и мог влиять на decision confidence.
- **Root cause:** selection и final-fit boundaries не были раздельными, а fallback считался достаточной calibration.
- **Impact:** optimistic confidence и скрытая post-selection leakage.
- **Fix:** активный feature pipeline сохраняет fit до terminal block; score-only in-sample fallback удалён; standalone direction Platt помечен audit-only; mandatory confidence gate fail-closed без validated probability model.
- **Regression:** `test_accepted_model_keeps_terminal_holdout_out_of_final_fit`, `test_required_confidence_gate_fails_closed_without_probability_model`, `test_direction_platt_without_chronological_skill_is_audit_only`.
- **Residual:** следующий шаг — preregistered rolling evaluation на полностью невидимых периодах.

### PCER-05 — HIGH — normal approximation на малом effective n

- **Reproducer:** при 20 temporal clusters использовался critical value 1.645, что уже не соответствует one-sided 95% Student-t bound.
- **Root cause:** uncertainty code не учитывал degrees of freedom малого effective sample.
- **Impact:** слишком узкая нижняя граница и преждевременное признание positive expectancy.
- **Fix:** deterministic inverse one-sided Student-t CDF с `df = n_eff - 1`; weighted/Kish semantics сохранены.
- **Regression:** `test_twenty_cluster_lower_bound_uses_small_sample_critical_value`.
- **Residual:** Student-t всё ещё предполагает достаточную независимость отобранных clusters.

### PCER-06 — HIGH — неверный directional target

- **Reproducer:** direction calibration использовала `success` whole-grid outcome, а не факт роста/падения horizon price.
- **Root cause:** один label применялся к различным estimands: прибыльности grid и направлению цены.
- **Impact:** profitable short-grid/path мог обучать вероятность направления с неверной семантикой.
- **Fix:** outcome rows содержат entry/exit, direction target равен знаку horizon price move; standalone Platt не меняет decision feature без chronological skill.
- **Regression:** `test_direction_calibrator_uses_horizon_price_direction_not_grid_profit`, audit-only regression.
- **Residual:** validated direction model пока отсутствует, поэтому feature остаётся raw/conservative.

### PCER-07 — HIGH — survivorship/censoring omission

- **Reproducer:** matured root без label отсутствовал в inner-joined fit set; corrupted labeled row либо tampered contract также выпадали из знаменателя.
- **Root cause:** outcome table использовалась и как numerator, и как population frame.
- **Impact:** positive observable subset могла выглядеть как полный sample, скрывая terminally unobservable paths.
- **Fix:** additive `reco_outcome_observability` ledger; outer population denominator; состояния `waiting/labeled/censored`; invalid/missing labels/contracts считаются unresolved; любое omission блокирует positive inference.
- **Regression:** censor/invalid/tampered observability tests в обоих iteration245 files.
- **Residual:** причины censoring надо мониторить; высокая доля censoring означает отсутствие пригодного evidence, а не нулевой риск.

### PCER-08 — HIGH — outcome-worker queue starvation

- **Reproducer:** старый waiting/censored root повторно занимал ограниченный batch и не давал обработать более новый matured root.
- **Root cause:** выборка сортировалась по старым rows без persisted attempt rotation/terminal state.
- **Impact:** labels и calibration liveness могли остановиться на одном неразрешимом observation.
- **Fix:** persisted attempt timestamp/state, waiting rotation и terminal censoring; queue query исключает censored rows.
- **Regression:** `test_censored_outcome_roots_cannot_starve_newer_matured_root`, `test_waiting_outcome_attempts_are_rotated_instead_of_starving_queue`.
- **Residual:** transient market-data outages остаются waiting и должны быть operationally monitored.

### PCER-09 — CRITICAL — локальный flat ledger выдавался за finalized live profit

- **Reproducer:** stopped bot с внутренне сбалансированными Buy/Sell events получал positive exact PnL без внешнего подтверждения позиции, orders, fees и funding.
- **Root cause:** локальный operator-fed ledger одновременно был claim и attestation.
- **Impact:** live-validation stop gate мог кредитовать несуществующую прибыль и продолжать потенциально убыточный режим.
- **Fix:** положительный finalized exact PnL требует complete terminal `bybit_private_reconciliation`, position=0, open orders=0, совпадение event counts и денежных totals; без reconciliation profit credit=0, losses сохраняются в risk stream.
- **Regression:** четыре первых exchange-attestation tests.
- **Residual:** проект принимает attestations от внешнего read-only adapter/operator; сам к Bybit private API не подключается и не доказывает provenance криптографически.

### PCER-10 — HIGH — reconciliation persistence boundary была недостаточной

- **Reproducer:** snapshot до stop либо повтор с тем же ID, но иным payload мог повредить terminal semantics.
- **Root cause:** terminal external evidence раньше не имело отдельной immutable schema/API boundary.
- **Impact:** post-hoc или conflicting evidence могло финализировать PnL.
- **Fix:** additive `execution_reconciliations`, строгие finite/integer/USDT/source checks, stopped + timestamp boundary, immutable IDs, idempotent exact duplicate и conflict rejection; admin API.
- **Regression:** pre-stop, monetary mismatch и idempotency paths в exchange test file.
- **Residual:** clock/provenance внешнего adapter остаются внешней trust boundary.

### PCER-11 — HIGH — detached/stale calibration cache

- **Reproducer:** свежий positive cache продолжал влиять после исчезновения supporting rows либо мог быть загружен под другой policy fingerprint; invalid direction label не отзывал direction cache.
- **Root cause:** freshness проверялась отдельно от текущего evidence denominator и policy identity.
- **Impact:** устаревшее положительное состояние переживало потерю или повреждение доказательств.
- **Fix:** full fingerprint входит в storage key/payload; loader сверяет его; observability support gap и invalid labels немедленно disable cache.
- **Regression:** dynamic-cache, support-disappearance и invalid-direction-label tests.
- **Residual:** app_config остаётся cache, а не первичным evidence source; восстановление зависит от retained rows.

### PCER-12 — MEDIUM — fixed SQL caps могли исказить support denominator

- **Reproducer:** calibration/status читали последние 5k/6k/8k rows; matured roots вне cap могли остаться в population, но не в fit/support comparison.
- **Root cause:** operational query limits стали неявной statistical truncation policy.
- **Impact:** крупный archive мог ошибочно классифицировать cache support или состав cohort.
- **Fix:** audit/calibration read caps подняты до 200,000 и support gap сравнивается с full policy observability denominator.
- **Regression:** full-suite compatibility плюс support-disappearance regression.
- **Residual:** 200,000 остаётся явным ресурсным пределом; при приближении к нему нужен cursor/streaming aggregation.

## 14. Отдельно неподтверждённые claims

- Claim «проект априори полностью несостоятелен/убыточен» **не подтверждён**: предоставленный архив не содержит независимого live execution dataset, достаточного для такого вывода.
- Claim «все скрытые ошибки найдены» принципиально недоказуем и не заявляется.
- Не подтверждены live edge, profitability, production-readiness auto-execution, exact Bybit fill/queue behavior и устойчивость к будущим режимам.
- 1106 passing tests доказывают исполнение проверенных контрактов, но не отсутствие неизвестных дефектов.

## 15. План исправления

1. Ввести единый canonical policy contract/fingerprint и binding всех evidence/cache paths.
2. Сделать matured recommendation roots population frame, независимо от наличия outcome row.
3. Разделить waiting/censored/labeled lifecycle и устранить starvation.
4. Усилить calibration: comparative OOF skill, terminal holdout, Student-t, no in-sample fallback.
5. Исправить direction estimand и не использовать непроверенный Platt в решении.
6. Разделить локальный execution claim и внешнюю terminal attestation; сделать positive PnL fail-closed.
7. Обновить обе схемы, runtime bootstrap, API/status/UI/docs/operator artifacts.
8. Добавить RED→GREEN regressions, затем полный и release-level verification.

План выполнен полностью в пределах recommendation/audit-only boundary.

## 16. Фактический diff по файлам

### Production

- `app/calibration.py` — OOF comparative skill, terminal holdout, strict model persistence, Student-t, direction target.
- `app/policy.py` — новый canonical contract SHA-256 helper.
- `app/recommender.py` — policy binding/cache identities, fail-closed confidence/observability gates, audit-only direction projection.
- `app/db.py` — observability/reconciliation persistence, validation summaries, loss-conservative risk events.
- `app/outcomes.py` — waiting/censored/labeled lifecycle и fair queue rotation.
- `app/risk.py` — loss-conservative reconciled event stream.
- `app/main.py` — v1.0.57, additive admin reconciliation/list routes и status diagnostics.

### Database

- `migrations/init.sql`.
- `migrations/init_postgres.sql`.
- Runtime bootstrap в `app/db.py`; таблицы/indexes additive/idempotent.

### Frontend

- `app/ui/static/index.html`.
- `app/ui/static/app.js`.

### Tests

- Новые: `tests/test_iteration245_policy_conditioned_calibration.py` (12 tests) и `tests/test_iteration245_exchange_attestation_and_queue.py` (12 tests).
- 33 legacy test files обновлены только для нового обязательного policy/reconciliation contract, новых identities/version и более строгого terminal evidence boundary; assertions не ослаблялись до fail-open.

### Documentation and operator artifacts

- `README.md`, `CHANGELOG.md`.
- `docs/ARCHITECTURE.md`, `KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`.
- `docs/instrukciya_operatora_bybit_recommender.docx` и `.pdf`.
- root `how_to_trade.png`.
- данный audit report.

Рабочая `data/app.db`, caches и bytecode являются test/runtime artifacts и не включаются в release ZIP.

## 17. Red → green evidence

RED выполнялся на отдельной копии исходного v1.0.56: добавлены новые tests, production code не переносился.

| Команда | RED result | Существенная причина |
|---|---|---|
| `pytest -q tests/test_iteration245_policy_conditioned_calibration.py` | `10 failed in 0.68s` | отсутствовали policy fingerprint/observability/confidence gates; monetary sample, OOF/holdout/t-bound и direction target не соответствовали контракту |
| `pytest -q tests/test_iteration245_exchange_attestation_and_queue.py` | `10 failed in 0.44s` | flat local ledger принимался без terminal exchange attestation; pre-stop boundary, queue rotation, support binding и full fingerprint отсутствовали |

После production fix все 24 iteration245 tests существуют в working copy, включая четыре дополнительные adversarial проверки tampering/audit-only/invalid-label paths.

GREEN final:

- iteration245 оба файла, run 1: `24 passed in 0.99s`;
- iteration245 оба файла, run 2: `24 passed in 1.00s`;
- relevant lineage/iteration245 subset: `28 passed in 1.32s`;
- full suite: `1106 passed in 24.80s`.

## 18. Database/schema compatibility

- Добавлены только таблицы `reco_outcome_observability` и `execution_reconciliations` с indexes/constraints; существующие таблицы и rows не удаляются.
- SQLite fresh init, повторный init и upgrade копии v1.0.56 выполняются штатным `app/db.py`, а не только init SQL.
- PostgreSQL schema содержит эквивалентные типы/constraints и проходит offline dialect/locking tests.
- `grid_label_v26` не сбрасывается; старые rows остаются audit history. Для v8/v19/v14 активны только rows с проверяемым current policy contract.
- Пользователю не требуется ручной SQL: normal startup создаёт additive schema. Перед upgrade всё равно рекомендуется backup DB.

## 19. API compatibility

- Ни один существующий route или обязательное response field не удалён.
- Добавлены admin-protected `POST /api/v1/bots/{bot_id}/execution-reconciliation` и read-only/admin-filtered `GET /api/v1/execution-reconciliations`.
- Existing execution/status responses получили additive reconciliation, policy, OOF, censoring и invalid-evidence diagnostics.
- Request model использует `StrictInt`, `StrictFloat`, `StrictBool`, finite checks, `source=bybit_private_reconciliation` и USDT-only persistence.
- Recommendation/audit-only semantics сохранены: endpoint записывает attestation, но не создаёт/изменяет/отменяет биржевые orders.

## 20. Config/env compatibility

- Новые environment variables не добавлены; существующие не удалены и не переосмыслены.
- Полный policy fingerprint вычисляется из уже существующих settings, active risk limits, universe и LLM reviewer gate.
- Dynamic cache keys меняются автоматически; ручное удаление старых v18/v13 values не требуется.

## 21. Security boundary

- Private Bybit credentials, production DSN и order endpoints не использовались.
- Статический поиск должен подтвердить отсутствие `/v5/order/create`, amend/cancel и batch equivalents.
- Mutating reconciliation route защищён тем же admin API-key boundary и rollback-safe transaction pattern, что остальные audit mutations.
- External snapshot считается attestational input, не автоматически доверенной биржевой истиной: schema проверяет consistency, но provenance/подпись внешнего adapter не доказывает.
- Release не должен содержать `.env`, API keys, database files, logs, caches или bytecode.

## 22. Post-check commands и точные результаты

| Проверка | Финальный результат |
|---|---|
| `python -m pytest --collect-only -q` | `1106 tests collected in 0.63s` |
| iteration245 targeted, два запуска | `24 passed in 0.99s`; `24 passed in 1.00s` |
| `python -m pytest -q` | `1106 passed in 24.80s` |
| relevant lineage/iteration245 subset | `28 passed in 1.32s` |
| PostgreSQL offline subset | `18 passed in 0.73s` |
| SQLite fresh init + repeat | PASSED; 16 reconciliation columns, 7 observability columns |
| SQLite v1.0.56 upgrade + repeat | PASSED; sentinel preserved, same additive columns |
| `python -m compileall -q app tests` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pip check` | `No broken requirements found.` |
| Ruff pristine → working | `24 → 22` historical findings; no new finding |
| DOCX render | PASSED; 10 pages visually inspected |
| PDF render/info | PASSED; 10 pages visually inspected |
| root PNG infographic | PASSED; visually inspected |
| private order-endpoint scan | PASSED; no create/amend/cancel implementation |
| clean ZIP + `unzip -t` + fresh re-extract | PASSED; 299 entries, one root, fingerprint/junk/secret scans passed |
| re-extracted ZIP: collect/static/targeted | 1106 collected; compileall + Node passed; 24 iteration245 tests passed |

## 23. Что не удалось проверить и почему

- Live PostgreSQL integration не запускалась: явно disposable DSN не предоставлен; использование неизвестной/production DB запрещено протоколом.
- Private read-only Bybit reconciliation adapter не запускался: архив не содержит безопасных test credentials и сам сервис по design не подключается к private execution API.
- Live fills, queue priority, partial fills, latency, mark-price liquidation, wallet equity и external order state не воспроизводимы публичными OHLCV/funding data.
- Будущая прибыльность и regime stability не проверяемы unit/integration suite.

## 24. Остаточные риски

1. Положительное proxy expectancy даже после этих исправлений не равно live edge.
2. Student-t/temporal thinning снижают псевдорепликацию, но не гарантируют независимость regimes/symbols.
3. Direction Platt намеренно audit-only; validated directional probability model отсутствует.
4. Новый v8 policy lineage начинает накапливать сопоставимые labels заново; до достаточного observable/held-out evidence ожидаемое состояние — `no_trade`.
5. External reconciliation provenance остаётся trust boundary; приложение проверяет consistency, не подлинность источника.
6. Query cap 200,000 требует перехода на streaming/cursor aggregation при росте archive.
7. 22 Ruff findings остаются историческим code-quality debt, хотя новых lint defects не внесено.

## 25. Rollback procedure

1. Остановить v1.0.57 и сохранить backup DB/архива.
2. Развернуть прежний v1.0.56 ZIP и перезапустить сервис.
3. Новые additive tables можно оставить: v1.0.56 их не использует; destructive SQL не требуется.
4. Не переносить v19/v14 cache payloads в старые keys.

Rollback восстанавливает доступность старой версии, но одновременно возвращает все PCER-01…PCER-12 риски; это аварийная процедура, не рекомендуемое постоянное состояние.

## 26. Один рекомендуемый следующий work package

Реализовать **отдельный read-only, signed/provenance-checked private Bybit reconciliation adapter** и preregistered walk-forward validation harness: adapter только получает terminal positions/open orders/executions/fees/funding и подписывает snapshot для текущего admin ingestion endpoint; harness фиксирует policy до будущего периода, проверяет censoring/coverage и сравнивает feature model с score/null на полностью невидимых regimes. Никакого order create/amend/cancel в scope не добавлять.
