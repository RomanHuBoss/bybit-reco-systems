# Audit report — 2026-05-10 — Tick-safe grid snapping hardening

## A. Краткое резюме

Проект повторно проверен по grid-only scope: Bybit Linear USDT Futures / USDT Perpetual, `futures_grid`, isolated margin, arithmetic grid. Основные торговые/risk guardrails уже присутствовали: net-profit после costs/funding, liquidation buffer, live price preflight, instrument metadata validation, funding interval handling и execution-time funding recheck.

Найдена дополнительная опасная зона в operator-facing auto-snap: когда UI/API подтягивали live Bybit `tick_size`, все цены округлялись к ближайшему tick. Это могло сужать основной range или kill-switch, а `grid_step` / `tp_per_leg` могли округляться вниз, делая отображаемую сетку более плотной и экономически лучше, чем безопасно проверенная модель.

Исправлено: generated operator payload теперь округляет lower-boundaries вниз, upper-boundaries вверх, а `grid_step.step_abs` и `tp_per_leg.abs` — вверх. Это сохраняет containment, не уменьшает per-grid edge после exchange alignment и не превращает округление в скрытое улучшение экономики.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Bybit tick-size snapping | Range/kill-switch boundaries auto-snapped to nearest tick | UI мог показать range/kill-switch уже, чем расчётная зона; live execution мог блокироваться или работать по другой геометрии | lower boundaries snap down, upper boundaries snap up | `app/main.py` |
| Grid economics / UI | `grid_step.step_abs` мог округляться вниз | Шаг сетки становился меньше net-edge floor, комиссии/spread/slippage/funding могли съесть прибыль | step snaps up for generated payload | `app/main.py` |
| TP hint / UI | `tp_per_leg.abs` мог округляться вниз | UI мог подсказывать TP ниже модели per-leg edge | TP snaps up for generated payload | `app/main.py` |
| Regression coverage | Не было теста на безопасное наружное округление границ | Повторная регрессия могла вернуть nearest-snap | добавлены iteration127 tests | `tests/test_iteration127_tick_safe_grid_snapping.py` |
| Документация | Tick-safe snapping не был явно описан | Оператор мог не понимать, почему snapped диапазон шире исходного | обновлены docs/changelog/readme | `README.md`, `docs/TRADING_LOGIC.md`, `docs/KNOWN_RISKS.md`, `CHANGELOG.md` |

## C. Исправления торговой логики

- Grid logic: сохранена только arithmetic futures grid geometry. Auto-snap больше не сжимает range: lower range и lower kill-switch округляются вниз, upper range и upper kill-switch округляются вверх.
- PnL / fees / funding: существующая net-of-costs модель не менялась. Исправление защищает эту модель от UI-level tick alignment, который мог сделать grid step меньше рассчитанного edge.
- Leverage / liquidation: существующий worst-side / worst-boundary liquidation buffer не менялся; диапазон/kill-switch после snapping теперь не создаёт искусственно более безопасную геометрию.
- Recommendation/rejection logic: execution preflight остаётся fail-closed. Для generated payload UI auto-snap приводит значения к exchange-aligned форме; manual/legacy off-tick values всё ещё проходят строгую валидацию.

## D. Исправления backend

- `app/main.py`: `_snap_reco_payload_to_bybit_meta()` теперь использует directional snap modes:
  - `reference_price`: nearest;
  - `range_lower`, `kill_switch_lower`: down;
  - `range_upper`, `kill_switch_upper`: up;
  - `grid_step.step_abs`, `tp_per_leg.abs`: up.

## E. Исправления frontend/UI/UX

Файлы фронтенда не менялись. Изменение влияет на данные, которые UI получает от backend: operator-facing details и validation теперь показывают exchange-aligned значения без скрытого сужения диапазона или занижения TP/шага.

## F. Исправления документации и конфигов

- `CHANGELOG.md`: добавлена запись `Tick-safe operator grid snapping`.
- `README.md`: описано безопасное auto-snap поведение для generated operator payload.
- `docs/TRADING_LOGIC.md`: добавлены правила snapping для range/kill-switch/step/TP.
- `docs/KNOWN_RISKS.md`: добавлен остаточный риск для manual/legacy off-tick payload.

## G. Тесты

Добавлены:

- `tests/test_iteration127_tick_safe_grid_snapping.py::test_tick_snapping_preserves_grid_range_and_kill_switch_containment`
- `tests/test_iteration127_tick_safe_grid_snapping.py::test_grid_step_and_tp_snap_up_so_economic_edge_is_not_thinned`

Проверки:

```bash
python -m pytest -q
python -m py_compile app/*.py tests/*.py main.py
node --check app/ui/static/app.js
```

Результат:

- `411 passed`
- `py_compile` passed
- `node --check` passed

## H. Остаточные риски

- Точные Bybit fees/funding/limits зависят от аккаунта, VIP tier, symbol metadata и текущих exchange filters.
- Liquidation model остаётся conservative estimate; точный liq price зависит от risk tier, mark price, margin и account state.
- Slippage и partial fills требуют paper/live execution telemetry.
- Funding can flip sign before inventory is accumulated; funding receipts still must not be counted as approval edge.
- Operator must re-check live Bybit preview before creating a real bot.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python -m pytest -q
python -m py_compile app/*.py tests/*.py main.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
