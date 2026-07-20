# Audit report — wide observability windows and current-result de-duplication

## 1. Итерация

- Входной проект: `bybit-reco-systems-main(2).zip`.
- Версия приложения и торгового контракта сохранена: `1.4.2`.
- Scope: окна «Результаты наблюдений / Исходы» и «Здоровье», их ширина, высота, прокрутка и структура таблиц.
- Входная диагностика: `bybit-recommender-diagnostics-2026-07-20T04-51-32-158Z.json`.

Изменения не затрагивают торговую математику, risk gates, policy fingerprint, model lineage, БД, API и outcome semantics.

## 2. Требования пользователя

1. В верхнем блоке результатов оставить только таблицу со strategy/direction badges; две следующие дублирующие таблицы удалить.
2. Сделать окна результатов, исходов и здоровья пригодными для монитора шириной 1920 px — ориентир до 1900 px.
3. Пересмотреть ширины столбцов и полезную высоту таблиц.
4. Не маскировать плохое состояние системы косметическими выводами.

## 3. Подтверждённые UI-дефекты

### MEDIUM — один и тот же текущий набор исходов показывался тремя последовательными срезами

В блоке «Стратегии и типы терминальных событий» подряд выводились:

- strategy + raw/execution direction;
- strategy + event type;
- общий event type.

Это не три независимых набора доказательств, а три агрегации одних строк. Для первичного операторского экрана они создавали визуальный дубль. Оставлена одна таблица `byBot` с badges; event-type архив сохранён в историческом разделе, где он действительно нужен для аудита семантики исходов.

### MEDIUM — фиксированная ширина 1000 px не использовала рабочую область 1920 px

Большие журналы имели 10–23 столбца, но modal card оставался около 1000 px. Это приводило к чрезмерной горизонтальной прокрутке, узким заголовкам и потере контекста строки.

Добавлен отдельный wide contract:

```css
width: min(1900px, calc(100vw - 20px));
height: calc(100vh - 20px);
```

Он включается только для Health и Outcomes и сбрасывается при открытии обычного modal.

### MEDIUM — одинаковая геометрия применялась к таблицам с двумя и двадцатью столбцами

Добавлены классы по числу столбцов:

- `modal-table-two-column`: 34% / 66%, первая колонка не уже 260 px;
- `modal-table-many-columns`: `width:max-content` при сохранении `min-width:100%`;
- заголовки переносятся по словам;
- диагностический текст ограничен 420 px и переносится;
- sticky header сохранён.

### LOW/MEDIUM — полезная высота таблиц была недостаточной

Увеличены пределы:

- список инструментов Health: до 680 px;
- текущий журнал outcomes: до 640 px;
- последние архивные outcomes: до 480 px;
- wide-table wrapper: до `min(64vh, 680px)`.

Прокручивается содержимое окна и конкретная длинная таблица; заголовок modal остаётся видимым.

## 4. Фактические изменения

### `app/ui/static/app.js`

- добавлен `configureModalLayout({wide})`;
- `showModal()` и `showRawTechnicalModal()` сбрасывают wide state;
- `showModalHtml()` принимает `{wide}`;
- Health и Outcomes открываются в wide режиме уже на стадии loading;
- удалены current-policy `eventTypeByBotRows` и `eventTypeRows` из верхнего блока;
- заголовок блока сокращён до «Стратегии»;
- `buildModalTable()` назначает two-column / many-column классы;
- увеличены max-height крупных эксплуатационных таблиц.

### `app/ui/static/styles.css`

- modal card использует `box-sizing:border-box` и адаптивные viewport bounds;
- добавлен `modal-card-wide` до 1900 px;
- добавлены column-aware table contracts;
- улучшены перенос заголовков, длинного текста и mobile fallback.

### `app/ui/static/index.html`

- добавлен cache token `ui=wide-observability-v1` без изменения существующего release token `manual-ui-v49...`.

### Tests

Добавлен `tests/test_iteration272_wide_observability_windows.py`:

- доказывает, что в canonical strategy section осталась одна таблица;
- проверяет 1900 px wide contract и увеличенные table heights;
- реально выполняет `configureModalLayout()` в Node и проверяет включение/сброс класса.

## 5. Оценка качества текущей системы

Негативная оценка пользователя подтверждается не только внешним видом окна.

По приложенной диагностике:

- runtime healthy, но `trading_actionable=false`;
- в последнем snapshot 70 строк, все 70 имеют `no_trade`, actionable — 0;
- 51 строка не проходит доказательство положительного monetary expectancy;
- 51 строка не имеет валидированной calibrated confidence;
- 34 grid-кандидата не проходят floor возврата к среднему (`0.18 < 0.25`);
- trend часто не подтверждает направление, режим и достаточную силу.

Главный статистический bottleneck:

- всего сохранено 29 292 outcome, но 29 248 относятся к старой model lineage;
- для текущей модели имеется 44 outcome;
- feature-eligible — 20;
- calibration-eligible — 0;
- grid: 20 current-model / 20 feature-eligible / 0 policy-eligible;
- trend: 24 current-model / 0 feature-eligible / 0 policy-eligible;
- trend first-touch model имеет `n=0`.

Следовательно, система сейчас не просто «осторожная»: у неё отсутствует валидная обучающая выборка именно для текущего исполнимого policy contract. Она не способна доказать вероятность или monetary expectancy и закономерно остаётся fail-closed.

Скриншот также не даёт основания считать найденный edge устойчивым: grid имеет только 20 наблюдений и около нулевого средневзвешенного net proxy результата; trend разбит на малые группы 15 и 8 наблюдений. Такие объёмы не позволяют делать надёжный вывод о прибыльности.

## 6. Почему торговые gates не ослаблены в этой итерации

Искусственное снижение `mean_reversion_min_score`, отключение `REQUIRE_CONF_GATE`, включение raw confidence или перенос старых outcomes в новую lineage создали бы рекомендации без независимого доказательства. Это улучшило бы количество сделок, но не качество системы.

Поэтому текущая итерация:

- исправляет заявленные UI-дефекты;
- сохраняет fail-closed;
- не заявляет live edge;
- не делает систему trading-ready.

Следующая содержательная итерация должна отдельно проверить **liveness evidence pipeline**: почему `policy_evaluation_eligible` остаётся нулевым, какие конкретные причины исключают 20 feature-eligible grid outcomes, и способен ли shadow-контур при неизменном контракте накопить 80 monetary и 300 probability observations за разумный срок. Только после этого допустима корректировка candidate-generation или порогов на основании walk-forward/terminal evidence.

## 7. Проверки

- Новый regression-suite: `3 passed`.
- Focused UI/observability regression: `35 passed`.
- Полная коллекция: `1294 passed` в шести непересекающихся группах. Три группы выполнены агрегированно, одна order-sensitive группа — по каждому test file отдельно, ещё две — агрегированно. Пересечения отсутствуют.
- Монолитный запуск дошёл до 72% без failures, но был остановлен лимитом времени; результат монолита не используется как доказательство полного pass.
- `python -m compileall -q app tests` — passed.
- `node --check app/ui/static/app.js` — passed.

## 8. Совместимость и действия пользователя

- SQL и миграции не требуются.
- `.env` не меняется.
- БД и накопленные outcomes не изменяются.
- После замены релиза достаточно перезапустить frontend/service и выполнить hard refresh браузера, чтобы применился новый cache token.
