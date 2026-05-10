# Audit report — 2026-05-10 — Execution funding event count and outcome funding hardening

## A. Краткое резюме

Проект повторно проверен по приложенному ТЗ: поддерживается только `futures_grid` для Bybit Linear USDT Futures / USDT Perpetual, `venue=linear`, USDT-settled arithmetic grid, isolated margin, net-of-fees/funding economics and fail-closed execution preflight.

Критичные найденные зоны были в funding lifecycle, а не в базовой linear PnL формуле:

1. execution-time funding guard мог недооценить количество funding events, когда свежий funding row имел `funding_interval_min`, но не имел `next_funding_ts`;
2. outcome-labeling мог засчитать отрицательный signed funding как положительный edge, хотя recommendation/approval уже запрещают использовать funding receipt как устойчивое преимущество.

Обе проблемы могли сделать систему слишком оптимистичной: первая — разрешить запуск grid с уже отрицательным net edge, вторая — завысить исторические labels/calibration на funding receipt.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Execution funding preflight | При отсутствии `next_funding_ts` функция считала максимум 1 funding event, даже если горизонт 12h и interval 8h могут включать 2 события | Grid мог пройти execution, хотя актуальный funding carry за горизонт делает conservative net edge неположительным | `_funding_events_until_horizon()` теперь использует conservative `ceil(horizon / interval)` при неизвестном next timestamp и ограничивает результат sanity-cap 32 | `app/main.py`, `tests/test_iteration125_execution_funding_and_scope_hardening.py` |
| Outcome labeling / calibration | `expected_funding_bps < 0` уменьшал cost и мог повысить `ret_proxy`/success | Историческая calibration могла переоценить grid setups, которые выглядели прибыльными только из-за нестабильного funding receipt | Добавлен `_funding_cost_bps_for_outcome_label()`; labels списывают только adverse funding cost, а funding receipt не повышает outcome edge | `app/outcomes.py`, `tests/test_iteration100_outcome_cost_components.py` |

## C. Исправления торговой логики

- **Grid logic**: сохранена только Bybit arithmetic Futures Grid модель; `grid_count` остаётся числом price intervals.
- **PnL**: linear USDT PnL helpers не менялись; long/short формулы остаются USDT-settled.
- **Fees**: per-grid economics по-прежнему считает net edge после execution friction.
- **Funding**: execution preflight теперь согласован с recommendation-time funding schedule assumption: если `next_funding_ts` неизвестен, но interval известен, считаются все возможные funding events по горизонту, а не один event.
- **Leverage / liquidation**: existing worst-boundary liquidation buffer gates сохранены.
- **Risk score / recommendation rejection**: adverse funding продолжает ухудшать score/spacing/net edge; funding receipt остаётся diagnostic и не является approval edge.
- **Outcome labels**: historical outcome теперь не получает бесплатный boost от funding receipt, что снижает риск переобучения calibration на нестабильном carry.

## D. Исправления backend

- `app/main.py`
  - `_funding_events_until_horizon()` стал консервативным при missing `next_funding_ts`.
  - Поведение execution funding guard теперь совпадает с recommender-side conservative event counting.

- `app/outcomes.py`
  - Добавлен `_funding_cost_bps_for_outcome_label()`.
  - `compute_outcomes_once()` списывает только `max(expected_funding_bps, 0)` для label return, не кредитуя funding receipt.

## E. Исправления frontend/UI/UX

Изменений JS/CSS/HTML не потребовалось. UI уже показывает conservative net edge, signed funding diagnostics, excluded funding benefit, risk report, rejection reasons and Bybit validation blocks. Исправления меняют backend decisions/labels, которые UI получает через существующие payload поля.

## F. Исправления документации и конфигов

- `CHANGELOG.md`: добавлена запись о execution funding event count и outcome funding hardening.
- `README.md`: уточнено, что execution preflight также консервативно считает funding events при missing `next_funding_ts`, а historical labels не кредитуют funding receipt.
- `docs/TRADING_LOGIC.md`: обновлена секция funding/outcome labeling.
- `.env.example`: изменений не потребовалось; продуктовый scope и risk limits уже описаны.

## G. Тесты

Добавлены/обновлены тесты:

- `tests/test_iteration125_execution_funding_and_scope_hardening.py::test_execution_funding_counts_unknown_next_timestamp_conservatively`
- `tests/test_iteration100_outcome_cost_components.py::test_outcome_label_does_not_credit_funding_receipt_as_edge`

Команды проверки:

```bash
pytest -q tests/test_iteration100_outcome_cost_components.py tests/test_iteration125_execution_funding_and_scope_hardening.py tests/test_grid_linear_economics.py tests/test_iteration131_adverse_funding_score_costs.py
pytest -q tests/test_logic.py tests/test_api.py
python -m py_compile app/*.py tests/*.py main.py
node --check app/ui/static/app.js
pytest -q
```

Локальный результат на этой итерации:

- targeted funding/grid regression: `25 passed`;
- logic/API regression: `126 passed`;
- full suite: `430 passed`;
- py_compile and node syntax checks: passed.

## H. Остаточные риски

- реальные Bybit fees могут отличаться по VIP tier и maker/taker fill mix;
- точные instrument limits нужно подтверждать live `/v5/market/instruments-info`;
- точный liquidation price зависит от account risk tier, mark price, wallet margin and existing positions;
- live execution, partial fills, queue priority, slippage and order reconciliation остаются вне этого recommender-сервиса;
- funding может измениться на следующем событии, поэтому receipt не должен использоваться как запусковое преимущество;
- production API keys and paper/demo trading контур должны проверяться отдельно.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m py_compile app/*.py tests/*.py main.py
pytest -q
python main.py
```

Для локального dev-сервера FastAPI, если используется uvicorn напрямую:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
