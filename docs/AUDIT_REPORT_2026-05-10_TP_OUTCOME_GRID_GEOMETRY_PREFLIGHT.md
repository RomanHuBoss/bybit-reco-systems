# Audit report — TP outcome and grid geometry preflight hardening

Date: 2026-05-10
Scope: Bybit Linear USDT Futures Grid recommendation system, outcome-labeling, execution preflight, tests, documentation.

## A. Краткое резюме

Проект уже был жёстко ограничен `futures_grid` для Bybit `linear` USDT perpetual. Повторный аудит по приложенному промпту выявил две оставшиеся торгово-критичные неоднозначности:

1. Outcome-labeling мог засчитать touch `tp_per_leg` как успешный исход даже тогда, когда TP меньше execution-cost floor и net per-leg результат отрицателен.
2. Execution preflight проверял рассинхронизацию `grid_count` и фактических интервалов после tick-rounding слишком мягко: legacy payload получал warning, но сгенерированный payload с явной `grid_geometry_model` должен блокироваться fail-closed.

После исправлений historical calibration не получает ложные win labels от экономически тонких TP-touch, а новые generated-рекомендации не проходят strict execution-preflight, если `grid_step`/range/`grid_count` описывают разные сетки.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Outcome-labeling | `tp_hit` сразу давал `success=1`, а `tp_realized_net` искусственно поднимался до `0.0001` даже при TP ниже costs. | Calibration/win-rate могли переоценивать grid setup, который после fees/spread/slippage убыточен. | `tp_success` теперь возможен только если TP-touch имеет положительный net edge выше защитного минимума; иначе `ret_proxy` остаётся отрицательным/тонким. | `app/outcomes.py`, `tests/test_iteration137_grid_outcome_and_preflight_hardening.py` |
| Execution preflight | `intervals + 1 < grid_levels` пропускал расхождение на один интервал и рассматривал Bybit Number of Grids как почти price-points. | Operator мог видеть TP/economics для одной сетки, а запускать другую geometry на Bybit. | Проверяется точное совпадение фактических интервалов с `grid_count`; для generated payload с `grid_geometry_model` mismatch становится blocking error в strict preflight, для legacy/manual остаётся warning. | `app/main.py`, `tests/test_iteration137_grid_outcome_and_preflight_hardening.py` |

## C. Исправления торговой логики

- Grid logic: `grid_count` подтверждён как Bybit Number of Grids / число интервалов. Новый generated payload с `grid_geometry_model=bybit_arithmetic_range_width_div_grid_count` блокируется, если `range / grid_step` после tick-rounding даёт другое число интервалов.
- PnL/outcomes: per-leg TP больше не считается успехом сам по себе. Success по TP требует net-positive edge после execution costs.
- Fees/slippage/funding: outcome TP-гейт использует существующий `cost_floor`, уже построенный из execution-cost components; funding receipt по-прежнему не улучшает outcome labels.
- Leverage/liquidation/risk: существующие fail-closed проверки не ослаблялись.
- Recommendation/rejection: generated execution payload теперь строже синхронизирует UI/economics и исполнимую Bybit geometry.

## D. Исправления backend

- `app/outcomes.py`
  - Убран `max(0.0001, tp_realized_net)` для TP-touch.
  - Добавлен `tp_success`, который срабатывает только при net-positive TP после costs.
- `app/main.py`
  - Заменён мягкий `intervals + 1 < grid_levels` на проверку `intervals != grid_levels`.
  - Для новых generated payload с явной geometry mismatch блокируется в strict execution-preflight; legacy/manual payload получает warning, чтобы не ломать старые неполные записи без generated geometry markers.

## E. Исправления frontend/UI/UX

Отдельных изменений в `app/ui/static/*` не потребовалось. UI уже отображает `bybit_plan_validation.errors/warnings`, `risk_report`, `grid_count`, `grid_step`, net economics и блокирует action-link для non-actionable recommendations. Новая ошибка `GRID_STEP_LEVELS_MISMATCH` будет видна в существующей панели Bybit validation.

## F. Исправления документации и конфигов

- `CHANGELOG.md` — добавлена запись о TP outcome / generated-grid geometry hardening.
- `docs/TRADING_LOGIC.md` — уточнено, что generated payload с mismatch блокируется strict preflight, а TP success требует net-positive edge.
- `README.md` — обновлён перечень регрессий.
- Конфиги не менялись.

## G. Тесты

Добавлен файл `tests/test_iteration137_grid_outcome_and_preflight_hardening.py`:

- `test_grid_outcome_does_not_label_tp_touch_success_when_tp_is_below_costs`
- `test_execution_preflight_blocks_grid_count_step_interval_mismatch`
- `test_detail_preflight_warns_on_grid_count_step_interval_mismatch`

Проверки:

```bash
python -m pytest tests/test_iteration137_grid_outcome_and_preflight_hardening.py -q
# 3 passed

python -m pytest -q
# 447 passed

python -m compileall -q app tests
```

## H. Остаточные риски

- Реальные Bybit fees, funding interval/rate, tick/qty filters и risk tiers должны перепроверяться live перед запуском.
- Slippage/fill efficiency остаются conservative model, а не гарантией исполнения.
- Outcome-labeling остаётся proxy-моделью без фактической очереди лимитных ордеров, partial fills и mark-price liquidation engine.
- Exact liquidation price зависит от Bybit risk tier, wallet margin, mark price и текущей позиции.
- Paper/live execution path всё ещё требует отдельного end-to-end теста с реальными exchange constraints и operator balance.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python -m pytest -q
python -m compileall -q app tests
uvicorn app.main:app --host 0.0.0.0 --port 8000
python main.py
```
