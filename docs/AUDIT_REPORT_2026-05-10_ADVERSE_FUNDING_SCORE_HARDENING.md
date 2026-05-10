# AUDIT REPORT — 2026-05-10 — Adverse funding score hardening

## A. Краткое резюме

Повторный аудит проведён по приложенному ТЗ: продукт остаётся строго рекомендационной системой только для Futures Grid на Bybit Linear USDT perpetual. Основной найденный дефект был не в execution-preflight, а в раннем ранжировании: adverse funding уже попадал в spacing/net-profit gates, но score/feature snapshot могли недооценивать carry-risk, особенно для neutral grid, где inventory может накопиться в любую сторону.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| Scoring / funding | Score penalty использовал execution cost, но не полный adverse funding carry | Кандидат с дорогим funding мог выглядеть лучше в ранжировании и UI, даже если позже блокировался экономикой | Добавлен `economic_cost_bps = execution_cost + adverse_funding_cost`; score, reasons и components используют этот cost | `app/recommender.py` |
| Feature snapshot / calibration | Signed negative funding мог выглядеть как отрицательная стоимость | Возможное улучшение feature vector из-за funding receipt, который не является устойчивым edge | `funding_norm` канонизирован к adverse-only cost; добавлен явный `funding_cost_norm` | `app/recommender.py` |
| Grid density | Количество grid intervals реагировало на execution-cost, но не на adverse funding | Сетка могла оставаться слишком плотной при дорогом funding, увеличивая order-count/margin exposure | Density теперь использует `grid_density_economic_cost_bps` | `app/recommender.py` |

## C. Исправления торговой логики

- Grid logic: шаг сетки уже строился от execution-cost + adverse funding; теперь также уменьшает плотность grid при высоком funding carry.
- PnL/fees/funding: net edge остаётся adverse-only; funding receipt не улучшает approval-edge, score или feature vector.
- Leverage/liquidation: существующая worst-boundary liquidation-buffer логика сохранена.
- Risk score/recommendation logic: score теперь видит `economic_cost_bps`, а `reasons.top_negative_factors` явно показывает `adverse_funding_cost_bps`.

## D. Backend

- `app/recommender.py`: `_score()` переведён с execution-only penalty на funding-aware economic penalty.
- `app/recommender.py`: `_build_feature_snapshot()` больше не передаёт signed funding receipt как полезный отрицательный cost.
- `app/recommender.py`: `_params()` добавляет `grid_density_economic_cost_bps` и применяет его к `grid_count`.

## E. Frontend/UI/UX

UI-контракт расширен через существующие поля `reasons.score_components`, `reasons.cost_model` и `params.grid_density_economic_cost_bps`. Отдельных изменений в JS/CSS не потребовалось: детали уже отображают raw payload и risk/context blocks.

## F. Документация и конфиги

- `README.md`: уточнено, что score и grid density учитывают adverse funding carry, а funding receipt не повышает edge.
- `CHANGELOG.md`: добавлена запись о hardening.

## G. Тесты

Добавлен файл `tests/test_iteration131_adverse_funding_score_costs.py`:

- score neutral grid снижается при adverse funding;
- feature snapshot не трактует funding receipt как отрицательную стоимость;
- grid density уменьшается при высоком economic cost.

Результат: `428 passed`.

## H. Остаточные риски

- реальные Bybit fees и VIP tier требуют live/account-aware источника;
- instrument limits нужно сверять с live `/v5/market/instruments-info` перед запуском;
- точная liquidation price зависит от risk tier, mark price и состояния аккаунта;
- slippage/fill quality остаются модельной оценкой;
- funding history и будущие flips не гарантируются текущей ставкой;
- нужен paper/staging прогон перед production execution.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
python main.py
```
