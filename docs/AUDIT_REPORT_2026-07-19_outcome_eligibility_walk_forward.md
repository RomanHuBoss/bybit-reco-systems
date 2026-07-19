# Outcome eligibility, retention и offline walk-forward — v1.0.77

Дата анализа: 2026-07-19 UTC. Входной файл: `current_model_stats.json`,
SHA-256 `4185eccf3c6a69467b57e4be28c76b714487d63e560b73d189db96a40860400f`.

## Решение

Universe и торговые пороги не изменены. Действующие floors `score >= 0.14` и
`mean_reversion_score >= 0.25` использованы только как заранее заданные срезы.
Offline-анализ не оптимизирует production threshold и не может активировать
торговлю.

## Что исправлено

- Scope `current_policy` теперь явно означает все завершённые outcome-корни с
  активным, криптографически проверенным fingerprint, но не объявляет их одной
  калибровочной когортой.
- Outcome API возвращает `mean_reversion_score`, флаг валидности evidence,
  materialized policy flags и полный объект `eligibility` с gate values,
  reason codes и взаимоисключающей когортой.
- Сводка API отдельно считает `calibration_eligible`,
  `policy_evaluation_candidate`, `shadow_exploration`, `outcome_only`,
  `other_policy` и `excluded`. Сумма этих когорт равна числу root outcomes в
  fingerprint scope.
- Decision reason codes и причины исключения из exact-policy опубликованы
  раздельно: бизнес-решение больше не смешивается с проверкой целостности label,
  fingerprint или feature evidence.
- Обычные recommendations/outcomes/observability остаются в горячем окне 14 дней.
  Materialized `policy_evaluation_eligible` root evidence сохраняется 90 дней;
  JSON policy contract, mean-reversion gate и maturity повторно проверяются при
  чтении и обучении. Retention selector является консервативным супермножеством,
  а не разрешением калибровки.
- Операторский экран показывает eligibility-когорты, причины, score и
  mean-reversion для каждого завершённого outcome.

## Метод walk-forward

Команда воспроизведения:

```bash
python scripts/offline_walk_forward.py current_model_stats.json \
  --score-floor 0.14 \
  --mean-reversion-floor 0.25 \
  --min-training-cohorts 2 \
  --output walk-forward.json
```

Полный bounded export для повторного анализа доступен через
`/api/v1/outcomes/stats?scope=current_policy&recent_limit=6000`; значение limit
ограничивается сервером диапазоном до 6000 строк.

Для каждого validation timestamp training содержит только строки, у которых
`label_available_ts <= validation.ts`. В legacy-экспорте поле
`label_available_ts` ещё отсутствовало, поэтому использована консервативная
формула `ts + horizon_sec + 120`; это явно помечено в отчёте. Все строки одного
timestamp остаются одной cross-sectional decision cohort. Ранговые tertiles
score/mean-reversion вычисляются только по доступному training прошлого fold.

## Фактический результат на приложенном срезе

| Показатель | Результат |
|---|---:|
| Принято строк | 60 |
| Decision timestamps | 25 |
| Временной span | 0.894 дня |
| Horizon | 12 часов |
| Walk-forward folds | 13 |
| Validation rows | 15 |
| Horizon-separated validation cohorts | 1 |
| Validation strategy success | 5/15 = 33.3% |
| Validation average proxy return | -0.265526% |
| Validation median proxy return | -0.083368% |
| Validation cumulative proxy return | -3.982896% |

Validation по направлению:

| Direction | n | Strategy success | Average proxy return |
|---|---:|---:|---:|
| long | 2 | 50.0% | +0.326180% |
| neutral | 3 | 66.7% | -0.018385% |
| short | 10 | 20.0% | -0.458010% |

Validation по действующему score floor:

| Срез | n | Strategy success | Average proxy return |
|---|---:|---:|---:|
| `score >= 0.14` | 1 | 0.0% | -1.251285% |
| `score < 0.14` | 14 | 35.7% | -0.195115% |

Training-derived score tertiles на validation не показали монотонности:
верхний tertile дал `n=9`, 22.2% strategy success и `-0.253054%` среднего
proxy-return; средний tertile — `n=3`, 66.7% и `-0.074912%`; нижний — `n=3`,
33.3% и `-0.493558%`. Эти строки кластеризованы и не являются 15 независимыми
испытаниями.

Полный описательный срез из 60 строк выглядел лучше — 60.0% strategy success и
`+0.062376%` среднего proxy-return, — но этот headline смешивает ранние и поздние
режимы. Корреляция score с proxy-return равна `0.001806`, а со strategy success
`-0.292547`; обе оценки только описательные.

## Ограничение mean reversion

В приложенном legacy outcome export `mean_reversion_score` отсутствует у 60 из
60 строк. Значения не восстанавливались и не импутировались. Поэтому фактический
отчёт имеет статус `partial_missing_mean_reversion`: score и direction прошли
walk-forward, а mean-reversion coverage корректно равен нулю. После установки
v1.0.77 новый outcome API сохраняет это поле в каждой строке, и тот же скрипт
автоматически построит mean-reversion tertiles и срез действующего floor 0.25.

## Вывод

Данных недостаточно для доказательства преимущества или изменения production
контракта: доступна только одна независимая validation-когорта, поздний short
сегмент отрицателен, единственное наблюдение выше score floor убыточно, а
mean-reversion отсутствует в старом read model. Корректное действие — оставить
universe и торговые пороги без изменений, накопить 90-дневный exact-policy lane
и повторить тот же pre-registered walk-forward после появления минимум пяти
horizon-separated validation cohorts с полным mean-reversion coverage.

## Проверка

- Полный набор: `1201 passed`.
- Новый eligibility/retention/walk-forward module и старые outcome scope
  compatibility tests входят в этот прогон.
- Python compile и `node --check` проходят.
