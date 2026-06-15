# Deep trading-system reaudit — Bybit futures / linear USDT

Дата: 2026-06-11  
Область: trading semantics, long/short TP/SL, risk-management, recommendation freshness, Bybit linear USDT validation, UI consistency, regression tests.

## Executive summary

Проведён повторный аудит проекта как потенциально боевого контура Bybit linear USDT. Основной найденный системный риск: рекомендация могла выглядеть свежей после перепубликации/уточнения, хотя исходная торговая идея жила значительно дольше. Это скрывало деградацию вероятности входа и актуальности уровней. Введён раздельный учёт возраста текущей строки и всей publication-chain от первого root-сигнала; execution-preflight теперь fail-closed блокирует цепочку, которая старше `ttl_sec`, даже если последняя дочерняя рекомендация недавно создана.

Отдельно реализована операторская политика по плечу: runtime risk-limits теперь содержат `min_leverage`, по умолчанию `5x`, и `max_leverage`, по умолчанию `5x`. Recommender выбирает операторский минимум только для достаточно сильных directional-сигналов; слабые/дорогие/волатильные идеи остаются с `1x` и блокируются `MIN_LEVERAGE_PER_BOT`, а не публикуются как низкоплечевые actionable-рекомендации.

Существующая directional-модель TP/SL (`app/trading_semantics.py`, `app/main.py`, `app/ui/static/app.js`) уже была в основном корректной: long TP выше entry / SL ниже entry; short TP ниже entry / SL выше entry; neutral/grid не получает directional TP/SL. Добавлены новые regression-тесты, чтобы это не расходилось с UI/исполнением.

## Найденные и исправленные проблемы

| Severity | Проблема | Файлы | Риск | Исправление | Тесты |
|---|---|---|---|---|---|
| critical | Скрытый возраст торговой идеи. UI/API показывали возраст текущей строки рекомендации, но не возраст всей publication-chain от первого root. Если рекомендация по символу уточнялась без прерывания цепочки, оператор не видел, что идея живёт давно. | `app/main.py`, `app/ui/static/app.js` | Вход по устаревшей идее, завышенная perceived probability/актуальность, запуск уровней после деградации edge. | Добавлены поля `publication_root_rec_id`, `recommendation_row_age_sec`, `publication_chain_started_ts`, `publication_chain_updated_ts`, `publication_chain_age_sec`, `publication_chain_update_count`, `publication_chain_expires_in_sec`, `is_publication_chain_expired`. UI теперь показывает «Возраст текущей строки» и «Возраст идеи с первого сигнала». | `tests/test_iteration152_deep_trading_reaudit.py::test_operator_context_exposes_publication_chain_age_not_only_row_age`, `::test_ui_exposes_publication_chain_age_in_details_panel` |
| critical | Execution-preflight не блокировал старую publication-chain, если текущая дочерняя рекомендация была свежей. | `app/main.py` | Запуск позиции по цепочке, которая пережила собственный TTL и могла потерять торговый смысл. | Добавлен `_execution_recommendation_freshness_blocks()`. Блоки: `RECOMMENDATION_ROW_EXPIRED` и `PUBLICATION_CHAIN_TOO_OLD`. `_execution_preflight()` вызывает этот блок до market-data/funding/Bybit validation. | `tests/test_iteration152_deep_trading_reaudit.py::test_execution_blocks_recommendation_chain_that_outlives_ttl_even_if_child_row_is_fresh` |
| high | Политика плеча не соответствовала операторскому требованию: recommender выбирал 1–3x, что делало большинство идей экономически непригодными для указанного стиля торговли. | `app/risk.py`, `app/settings.py`, `.env.example`, `app/recommender.py` | Рекомендации формально валидны, но практически не соответствуют целевому профилю доходности; оператор мог запускать низкоплечевые сетки вопреки собственному правилу. | Добавлен `min_leverage` в risk-limits. Defaults: `min_leverage=3`, `max_leverage=5`. Recommender выбирает 5x только для сильных directional-условий; остальные идеи fail-closed блокируются `MIN_LEVERAGE_PER_BOT`. Явный `max_leverage` ниже 5 сохраняется как операторский cap и снижает effective `min_leverage` до cap, чтобы не нарушать верхний риск-лимит. | `tests/test_iteration152_deep_trading_reaudit.py::test_risk_limits_default_to_operator_minimum_3x_5x_and_fail_closed_bounds`, обновлены existing risk-limit tests |
| high | Cooldown после реализованного убытка нуждался в regression-защите: эта зона критична для stop-trading после потерь. | `app/risk.py` | При ошибке запроса или отсутствии проверки реализованного loss cooldown может не включиться, и система продолжит открывать сделки после убытка. | Зафиксирован regression-тест, что `compute_risk_status()` корректно читает последний отрицательный trade и включает cooldown. | `tests/test_iteration152_deep_trading_reaudit.py::test_loss_cooldown_query_is_valid_and_blocks_after_realised_loss` |
| medium | Тесты отражали устаревший default leverage=3x и не фиксировали новое операторское правило. | `tests/test_grid_linear_economics.py`, `tests/test_iteration118_grid_bot_risk_caps.py`, `tests/test_iteration89_env_and_docs_integrity.py`, `tests/test_iteration94_risk_limits_and_outcome_bounds.py`, `tests/test_logic.py` | Regression-suite могла вернуть проект к низкоплечевой политике, противоречащей требованию оператора. | Обновлены expected values и добавлена проверка `min_leverage`. | Полный `pytest` |

## Проверенные области

### Directional semantics / TP-SL

Проверены backend helpers и UI fallback-paths:

- `app/trading_semantics.py`: canonical `directional_exit_levels()`, `validate_directional_exit_geometry()`, `bybit_linear_order_semantics()`.
- `app/main.py`: `_directional_exit_payload_for_reco()`, `_validate_trade_plan_against_bybit_meta()`, `_snap_reco_payload_to_bybit_meta()`, execution preflight.
- `app/ui/static/app.js`: `operatorExitLevels()`, `operatorExitLevelsFromBackend()`, labels/details panel.

Итоговая модель:

- long: TP = upper, SL = lower, profit on price up, loss on price down;
- short: TP = lower, SL = upper, profit on price down, loss on price up;
- neutral: directional TP/SL не создаются, используются range/kill-switch levels;
- kill-switch lower/upper остаются geometry-boundaries, а не long-only TP/SL.

### Bybit linear USDT semantics

Проверены места, связанные с:

- `Buy`/`Sell` mapping;
- `reduceOnly` и `closeOnTrigger` для закрывающих/защитных ордеров;
- `positionIdx=0` для one-way режима;
- `tick_size`, `qty_step`, `min_order_qty`, `min_notional`, `max_leverage`;
- fail-closed validation, если Bybit metadata отсутствует или неполна.

Системных новых ошибок short TP/SL mapping в уже существующей canonical-модели не обнаружено. Изменения сконцентрированы на stale-chain/risk-leverage, чтобы закрыть фактический источник ложной актуальности и несоответствие операторскому профилю.

### Quant/econometric checks

Проверены кодовые зоны:

- rolling/feature extraction в `app/features.py`;
- `_drop_open_candle()` в `app/recommender.py` как защита от look-ahead на текущем незакрытом баре;
- confidence/probability-like scoring и publication dedupe в `app/recommender.py`;
- cost model: fees/spread/slippage/funding, net profit per grid;
- liquidation buffer на adverse boundary;
- grid arithmetic range/grid-count semantics.

В рамках текущего патча не изменялась calibration/math-модель вероятности: без исторического live/paper outcome-dataset нельзя честно перекалибровать вероятности. Остаточный риск указан ниже.

## Добавленные / изменённые тесты

Новый файл:

- `tests/test_iteration152_deep_trading_reaudit.py`
  - `test_loss_cooldown_query_is_valid_and_blocks_after_realised_loss`
  - `test_risk_limits_default_to_operator_minimum_3x_5x_and_fail_closed_bounds`
  - `test_operator_context_exposes_publication_chain_age_not_only_row_age`
  - `test_execution_blocks_recommendation_chain_that_outlives_ttl_even_if_child_row_is_fresh`
  - `test_ui_exposes_publication_chain_age_in_details_panel`

Обновлены существующие тесты:

- `tests/test_grid_linear_economics.py`
- `tests/test_iteration118_grid_bot_risk_caps.py`
- `tests/test_iteration89_env_and_docs_integrity.py`
- `tests/test_iteration94_risk_limits_and_outcome_bounds.py`
- `tests/test_logic.py`

## Выполненные проверки

```text
python -m compileall -q app tests
PASS

node --check app/ui/static/app.js
PASS

pytest -q
507 passed in 20.45s

static grep scan:
1134 matches across app/tests for tp/sl/stop/take/upper/lower/short/long/side/Buy/Sell/reduceOnly/kill/leverage/pnl/roi/risk reviewed at high level
```

## Невыполненные проверки / ограничения

- Реальный Bybit live/testnet order placement не выполнялся: нет API keys и безопасного sandbox-сценария для отправки ордеров.
- `npm test`, `yarn test`, ESLint, mypy, ruff/flake8 не запускались: в архиве не найдено `package.json`, `pyproject.toml`, `setup.cfg`, `tox.ini`, `mypy.ini`, `.eslintrc` или `eslint.config.js`.
- Не выполнялся full economic backtest/recalibration вероятностей на историческом датасете, потому что в архиве нет достаточного labeled outcome dataset для честной out-of-sample проверки.
- Не выполнялась нагрузочная проверка race conditions с реальной БД/несколькими воркерами; покрыты доступные unit/integration checks.

## Остаточные риски

1. **Калибровка вероятностей входа.** Даже с исправленным stale-chain age confidence/probability-like scores могут быть завышены. Нужен отдельный walk-forward/out-of-sample calibration report по paper/shadow/live outcomes.
2. **Exchange-state reconciliation.** Код содержит preflight и idempotency-защиты, но без live/testnet ключей нельзя подтвердить все ветки partial fill/retry/rate-limit/timeout на реальном Bybit V5.
3. **Leverage policy.** Default 5x соответствует операторскому требованию, но увеличивает liquidation sensitivity. Защита — fail-closed liquidation buffer, max leverage cap и `MIN_LEVERAGE_PER_BOT`. Для live желательно явно настроить `RISK_LIMITS_JSON` под размер счёта.
4. **UI cache.** JS исправлен, но production deployment должен отдавать новую версию ассета без старого браузерного cache.

## Изменённые файлы

- `.env.example`
- `app/main.py`
- `app/recommender.py`
- `app/risk.py`
- `app/settings.py`
- `app/ui/static/app.js`
- `tests/test_grid_linear_economics.py`
- `tests/test_iteration118_grid_bot_risk_caps.py`
- `tests/test_iteration89_env_and_docs_integrity.py`
- `tests/test_iteration94_risk_limits_and_outcome_bounds.py`
- `tests/test_logic.py`
- `tests/test_iteration152_deep_trading_reaudit.py`
- `docs/AUDIT_REPORT_2026-06-11_DEEP_TRADING_SYSTEM_REAUDIT.md`
