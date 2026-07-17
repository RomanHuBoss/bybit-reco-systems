# Audit report: selected-policy monetary validation and cohort-safe terminal holdout - v1.0.74

## 1. Название итерации

Iteration 261 — денежная валидация exact confidence-selected policy и terminal holdout без однострочного остатка/разреза timestamp.

## 2. Входной ZIP

Непосредственный baseline: `bybit-reco-systems-1.0.73-llm-shutdown-liveness.zip`, полученный в предыдущей завершённой итерации из пользовательского `bybit-reco-systems-main(4).zip`.

## 3. SHA-256 входного ZIP

Baseline 1.0.73: `b31e2d8c96cdb8d400517ae694df85bb42447e32e9c26bc18b184490dc3b00aa`.

Первоначальный пользовательский ZIP: `286eb13789f35836a97cd8ee6faa28f9cf7760084e173c3764211ac2e29c6647`.

Baseline содержит 331 entry и ровно один root `bybit-reco-systems-main/`.

## 4. Исходная версия

`1.0.73`; source of truth — `FastAPI(..., version="1.0.73")` в `app/main.py`.

## 5. Новая версия

`1.0.74` (patch). Model/policy/calibrator identities меняются намеренно; public routes, DB schema, env и outcome label не меняются.

## 6. Project fingerprint

Подтверждён Bybit Recommender: FastAPI service, `futures_grid`, Bybit linear USDT perpetual, SQLite/PostgreSQL persistence, deterministic risk/economic/preflight gates, OHLCV proxy outcomes, LLM advisory/reviewer, operator UI, regression suite и release builder. Private create/amend/cancel order calls отсутствуют; проект остаётся recommendation/audit-only.

Внешний PDF исторически указывает `isolated`, тогда как код, tests и Bybit Futures Grid contract используют `cross`. В этой итерации этот отдельный конфликт не менялся.

## 7. Цель итерации

Модель вероятности может активироваться только когда одновременно выполнены три условия: вся exact pre-calibration candidate-когорта имеет положительное uncertainty-bounded денежное ожидание; feature probability имеет purged aggregate/terminal skill; exact подвыборка, которую runtime confidence transform пропустит через threshold, также имеет положительные row/temporal monetary lower bounds. Terminal validation обязана состоять из целых timestamps и иметь заранее заданный минимальный размер.

## 8. Критерии приёмки

1. Контрпример с `+0.28%` mean всей когорты и `-0.16%` mean selected-подвыборки не активирует coefficients.
2. Selected subset рассчитывается тем же adaptive blend/context adjustment/threshold, что runtime.
3. Любой missing/non-finite selection input делает selected-policy evidence insufficient, а не удаляется оптимистически.
4. Terminal block содержит минимум `CALIB_MIN_SAMPLES` строк и 5 целых decision timestamps.
5. Один timestamp не разрезается между соседними validation blocks.
6. Fitted persistence без terminal/selected-policy evidence отклоняется при загрузке.
7. Новые diagnostics видны в recommendation details и `/api/v1/status`.
8. Старые v8/v19 evidence не используются под новым контрактом.
9. Новый test красный на pristine 1.0.73 и зелёный после fix; полный offline suite проходит.

## 9. Прочитанные источники

- пользовательский PDF-протокол итерации;
- оба приложенных diagnostic JSON: `...02-40-30-031Z.json` и `...03-46-45-543Z.json`;
- README, CHANGELOG, `.env.example`, requirements и release docs;
- последние audit reports, особенно monetary expectancy, purged OOF, temporal dependence, policy-conditioned calibration, censoring, outcome liveness и iteration 260;
- `app/calibration.py`, `app/recommender.py`, `app/main.py`, `app/outcomes.py`, `app/db.py`, settings/UI и релевантные tests.

Диагностика 1.0.73 в 03:46 содержала 279 050 recommendations, 29 075 outcomes, 113 current-model outcomes и 0 exact-policy eligible outcomes. За 66 минут относительно первого снимка добавились 2 310 recommendations и только 1 outcome; exact-policy eligible осталось 0. Все 113 current rows отбрасывались candidate-policy lineage, поэтому runtime calibrator не имел выборки. Retained history охватывала около 5.97 суток, тогда как действующий денежный contract требовал минимум 20 непересекающихся 12-часовых когорт, то есть теоретически не меньше 10 суток. Это объясняет текущее `no_trade`, но не отвечает на вопрос о корректности будущей активации.

LLM snapshot во втором файле всё ещё предшествовал start нового процесса на 74 секунды; возраст процесса был меньше cadence reviewer. Поэтому отсутствие нового sweep не признано дополнительным defect.

## 10. Карта затронутого data flow

`recommendation feature/context` -> persisted `selection_confidence_raw` + cumulative adjustment -> matured exact-policy outcome -> purged chronological folds -> feature/score/null log-loss -> shared `selected_policy_confidence()` -> confidence threshold -> selected OOF returns -> row/temporal lower bounds -> terminal candidate -> strict persistence/load -> recommendation confidence gate/status/UI.

## 11. Baseline environment

- Python `3.12.13`;
- Node `v24.14.0`;
- отдельный venv `/tmp/bybit-runtime-venv` вне project root;
- proxy variables очищались для deterministic offline HTTPX tests;
- baseline collection: 1187 tests;
- Ruff baseline: 24 pre-existing findings.

## 12. Baseline commands и точные результаты

На неизменённом 1.0.73:

- `python -m pip check` — PASSED: `No broken requirements found.`
- `python -m compileall -q app tests main.py` — PASSED.
- `node --check app/ui/static/app.js` — PASSED.
- `python -m pytest --collect-only -q` — 1187 collected.
- sanitized offline `python -m pytest -q` — **1187 passed in 43.71s**.
- `python -m ruff check .` — 24 pre-existing findings; production code до baseline не менялся.

## 13. Подтверждённые defects/gaps

### SPM-261-01 — CRITICAL — CONFIRMED DEFECT

- Файлы: `app/calibration.py`, `app/recommender.py`.
- Фактическое поведение: денежный gate проверял returns всей candidate-когорты, затем LogReg оптимизировал бинарный `success`, а runtime threshold применял blended confidence без денежной проверки выбранных строк.
- Динамический контрпример: 40 temporal cohorts x 30 rows; половина A имела 80% wins и mean `-0.16%`, половина B — 20% wins и mean `+0.72%`; общая mean `+0.28%`, row LCB `+0.218499%`, temporal LCB `+0.28%`. 1.0.73 вернула `fitted=True`, `expectancy_status=positive`, `oof_skill=accepted`. Вероятность A была выше threshold, B — ниже; значит разрешённая policy была денежно-отрицательной.
- Нарушенный инвариант: probability calibration не может подменять денежную цель бинарной точностью.
- Влияние: ложное `recommended` для статистически high-hit-rate, но убыточного monetary subset; потенциальный систематический убыток при внешнем исполнении.
- Почему tests не поймали: iteration 228 проверяла negative returns всей когорты; iteration 245 проверяла pre-calibration candidate floor; ни один test не пересчитывал returns после confidence selector.
- Fix: exact purged OOF predictions проходят shared runtime transform; selected rows получают отдельные Kish/expected-shortfall/Student-t/temporal diagnostics; activation требует `selected_policy_expectancy_status=positive`.

### THO-261-02 — HIGH — CONFIRMED DEFECT

- Файл: `app/calibration.py`.
- Фактическое поведение: `fold_size=n//(splits+1)` и `range(..., fold_size)` делали последний validation block простым остатком и могли разрезать cross-sectional timestamp.
- Динамический контрпример: `n=301`, 20 timestamps, сильный сигнал. 1.0.73 приняла модель с `samples=151`, `final_samples=1`, `status=accepted`; одна строка terminal tail считалась достаточным режимным подтверждением.
- Нарушенный инвариант: terminal evidence должна быть заранее ограниченной, целой и независимой от арифметического остатка размера массива.
- Влияние: модель могла пройти terminal skill по одной удобной строке или части одной рыночной публикации.
- Почему tests не поймали: fake terminal test возвращал 40 rows, но fit не проверял minimum; отсутствовал remainder/timestamp regression.
- Fix: deterministic whole-timestamp blocks; terminal минимум `min_samples` rows и 5 timestamps; fitted/load/runtime gates проверяют оба фактических и требуемых значения.

## 14. Неподтверждённые claims

- Не доказано, что стратегия априори убыточна: приложенные diagnostics не содержат достаточной exact-current-policy когорты или reconciled live fills.
- Не доказано и обратное: 29 075 archive outcomes относятся к разным историческим contracts и не подтверждают v9 policy.
- Старые 123 proxy outcomes с win rate около 22% и mean около `-1.49%` являются тревожным историческим сигналом, но не валидным тестом текущей стратегии.
- Ноль actionable в diagnostics не является самостоятельным runtime defect: текущие gates корректно fail-closed при нулевой exact-policy evidence.
- Исправление calibration methodology не устраняет OHLCV-to-fill gap и не является обещанием прибыли.

## 15. План исправления

Один связанный work package: воспроизвести обе ошибки; ввести единый confidence transform; сохранять его pre-model inputs; вычислять selected OOF monetary evidence; заменить index remainder splitter на whole-timestamp blocks; сделать persistence/load/runtime fail-closed; сменить model/policy/calibrator identity; обновить status/UI/docs; выполнить полный и re-extracted release check.

## 16. Фактический diff по файлам

Production:

- `app/calibration.py` — shared confidence transform, chronological blocks, selected-policy diagnostics, dataclass/persistence/load gates, calibrators v20.
- `app/recommender.py` — model v9/policy v2, selection inputs, exact runtime parity, activation/status diagnostics.
- `app/main.py` — FastAPI 1.0.74 и additive status fields/contract.
- `app/ui/static/app.js`, `index.html` — readiness explanation и cache build 1.0.74.

Tests:

- новый `tests/test_iteration261_selected_policy_and_terminal_holdout.py` (6 tests);
- синхронизированы существующие lineage/version/fake-model fixtures с новым строгим контрактом.

Docs:

- README, CHANGELOG, KNOWN_RISKS, TRADING_LOGIC, ARCHITECTURE, MODULES, SCENARIOS, HOW_TO_TRADE_INFOGRAPHIC и этот report.

DB/reference migrations, `.env.example`, outcome target, direction target и operator DOCX/PDF/PNG не менялись.

## 17. Red -> green evidence

RED на pristine 1.0.73:

`python -m pytest -q tests/test_iteration261_selected_policy_and_terminal_holdout.py`

Результат: **2 failed in 5.29s**. Существенные строки: `LogRegScaler has no attribute selected_policy_expectancy_status`; `assert 1 >= 80` для terminal block.

GREEN после fix:

`python -m pytest -q tests/test_iteration261_selected_policy_and_terminal_holdout.py`

Результат расширенного regression: **6 passed in 5.87s**.

Исправленный monetary counterexample: overall mean `+0.28%`, но `selected_policy_samples=495`, selected mean `-0.16%`, row LCB `-0.198555%`, temporal LCB `-0.16%`; `oof_skill=accepted`, `oof_status=selected_policy_unproven`, `fitted=False`.

Исправленный `n=301`: `status=accepted`, aggregate OOF `121`, terminal `91/80` rows и `6/5` whole timestamps вместо одной строки.

## 18. Database/schema compatibility

Schema и migrations не меняются. Новые fields находятся внутри existing `app_config.value_json` и recommendation `reasons_json`. Loader строгий: fitted v20 payload без selected/terminal contract возвращает `None`. v19 keys не читаются через v20 registry. Historical recommendations/outcomes сохраняются; ручная миграция не требуется.

## 19. API compatibility

Все существующие routes и поля сохранены. `/api/v1/status` и `confidence_model` получили только additive diagnostics. FastAPI patch version — 1.0.74. Статус recommendation может остаться `no_trade` там, где 1.0.73 ошибочно мог активировать probability model; это намеренное исправление safety semantics.

## 20. Config/env compatibility

Новых env variables и default threshold changes нет. `MIN_CONF_TO_RECOMMEND`, `CALIB_MIN_SAMPLES`, `REQUIRE_CONF_GATE`, horizons, fees/risk limits сохраняются и входят в policy fingerprint. Новая policy schema меняет fingerprint даже при том же `.env`.

## 21. Security boundary

Private Bybit create/amend/cancel endpoints не добавлены. Selection inputs содержат только bounded model diagnostics, не secrets. Release не должен включать `.env`, production DB, credentials, caches или runtime logs. Strict JSON finite-number boundary сохранён.

## 22. Post-check commands и точные результаты

Working tree:

- новый regression — 6 passed in 5.87s;
- calibration/lineage focused suite — 39 passed in 2.39s после identity synchronization;
- docs/version/regression suite — 60 passed in 8.05s;
- `python -m pip check` — PASSED: `No broken requirements found.`;
- `python -m compileall -q app tests main.py` — PASSED;
- `node --check app/ui/static/app.js` — PASSED;
- `python -m pytest --collect-only -q` — 1193 collected in 1.71s;
- финальный полный sanitized offline suite после docs — **1193 passed in 54.59s**;
- new-file Ruff — PASSED; full-project Ruff — те же 24 baseline findings, delta 0.

Первичная release-сборка:

- 333 entries, ровно один root `bybit-reco-systems-main/`;
- ZIP CRC — PASSED;
- absolute/traversal, duplicate, symlink, nested archive, cache, `.env`, DB/SQLite и compiled-bytecode findings — 0;
- re-extracted `pip check`, compileall и Node syntax — PASSED;
- re-extracted collection — 1193 tests in 4.52s;
- re-extracted полный sanitized suite — **1193 passed in 48.69s**;
- fresh/repeated SQLite init — `20/20` tables, `PRAGMA integrity_check=ok`;
- private order create/amend/cancel scan — 0; secret-pattern scan — 0;
- version/cache/lineage consistency — `1.0.74`, build `1.0.74`, model v9, bot/global v20.

## 23. Что не удалось проверить и почему

- Live PostgreSQL — disposable DSN не предоставлен.
- Live Bybit/private reconciliation — credentials и production account не использовались.
- Реальную v9 profitability — новая exact-policy когорта ещё не существует.
- Queue priority, partial fills и market impact — OHLCV proxy этого не наблюдает.
- Production VM restart/RSS и 14-day retention under live symbol cadence — offline suite не воспроизводит deployment load.

## 24. Остаточные риски

- Binary feature skill и пять terminal timestamps не равны пяти независимым market regimes; отдельный monetary temporal gate по-прежнему является обязательным.
- Selected-policy OOF объединяет последовательные past-only fold models; final candidate дополнительно проверяется terminal log-loss, но будущий regime shift остаётся возможным.
- При редком selector sample floor может долго не достигаться в 14-day retention window. Это должно оставаться `no_trade`, а не лечиться снижением threshold без новой гипотезы.
- Proxy labels консервативны, но не являются exchange-attested live PnL.
- Полная экономическая несостоятельность/жизнеспособность проекта всё ещё требует frozen-policy walk-forward и внешней fill reconciliation.

## 25. Rollback procedure

Остановить 1.0.74, сохранить резервную копию БД и восстановить code/docs 1.0.73. DB downgrade не требуется: v20 JSON/keys и v9 archive rows не мешают старой схеме. Однако 1.0.73 снова содержит оба подтверждённых дефекта и может использовать/refit v19 calibrator; безопасный rollback для торгового использования невозможен. До повторного применения fix сервис следует держать только в audit/no-action режиме и не трактовать `recommended` 1.0.73 как денежно валидированную policy.

## 26. Один рекомендуемый следующий work package

После накопления достаточной неизменной v9 exact-policy когорты выполнить заранее зарегистрированный viability decision package: экспортировать candidate/selected/rejected returns по temporal cohorts и regime, добавить external reconciled-fill comparison, применить неизменные acceptance/rejection bounds и принять одно из решений — продолжать shadow validation либо признать стратегию экономически несостоятельной. Не менять thresholds в том же пакете.

## Готовый commit message

`fix(calibration): validate selected policy and terminal cohorts`

- validate monetary expectancy on the exact OOF confidence-selected subset
- reserve a minimum whole-timestamp terminal holdout
- persist strict terminal/selected diagnostics and advance model policy lineage
- add iteration 261 regressions and synchronize version/docs
