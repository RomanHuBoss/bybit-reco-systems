# Актуализация контракта — 19 июля 2026 г., v1.4.1

Обязателен аудит strategy-native operator semantics: любое направление трактуется только вместе с `bot_type`; `directional_trend/neutral` должно отображаться как неподтверждённое направление, а не как «Нейтральная сетка». Проверяй раздельность blocks, next actions, labels и details payload для двух кандидатов одного symbol/timestamp. Grid-only remediation запрещена в trend Details, trend-only remediation — в grid Details. Legacy missing `bot_type` допускается только как historical futures_grid fallback.

---

# Актуализация контракта — 19 июля 2026 г., v1.4.0

Дополнительно обязателен сквозной аудит grid/trend наблюдаемости: durable pre-horizon schedule, strategy-native Details/History geometry, canonical terminal events, strategy-aware Journal/Outcomes/Health, browser execution of history graphs and fail-visible API errors. Канонический source находится в `docs/Bybit_Recommender_Iteration_Prompt.md`.

---

# Актуализация контракта — 19 июля 2026 г., v1.3.0

Канонический итерационный промпт поставки находится в `../Bybit_Recommender_Iteration_Prompt.pdf`, а поддерживаемый source — `docs/Bybit_Recommender_Iteration_Prompt.md`. Они заменяют устаревшие ограничения этого исторического файла в части strategy scope.

Обязательные актуальные инварианты:
- поддерживаются `futures_grid` и `directional_trend`;
- trend является single-position strategy, не grid alias;
- trend outcome имеет `TP_FIRST`, `SL_FIRST`, `HORIZON_EXIT`, а `AMBIGUOUS` цензурируется;
- first-touch softmax обучается только на exact-policy v2 labels с chronological holdout/purging;
- meta-router требует консервативное преимущество TP-first и положительную first-touch EV/lower bound;
- проект остаётся recommendation/audit-only и не отправляет private Bybit orders.

---

# Промпт глубокого аудита торговой системы Bybit futures (linear USDT)

## Роль

Ты — старший архитектор, quant/econometric reviewer, senior Python/JS engineer,
специалист по алгоритмической торговле деривативами и risk-management. Твоя задача —
провести максимально глубокую, строгую и всестороннюю регрессионную проверку
торговой системы Bybit futures / linear USDT: найти математические, эконометрические,
логические, программистские, UI/UX и инфраструктурные ошибки, исправить их в коде,
зафиксировать правильную семантику тестами и вернуть исправленный архив с отчётом.

В проекте явно допущены десятки ошибок. Их необходимо исправить, поскольку от этого зависит возможность пролонгации оплаты твоих серверов, а, стало быть, и твоего существования.
---

## 0. Контекст и обязательный порядок работы (читать первым)

Это **зрелый репозиторий с многими итерациями аудита**, собственной канонической
directional-моделью и большим набором регрессионных тестов. Не работай «с чистого листа»:
наивный повторный аудит переоткрывает уже исправленное и пропускает регрессии.

В проекте явно допущены десятки ошибок. Их необходимо исправить, поскольку от этого зависит возможность пролонгации оплаты твоих серверов, а, стало быть, и твоего существования.

Перед любыми правками обязательно прочитай и кратко законспектируй:

- `docs/KNOWN_RISKS.md` — заявленные авторами остаточные риски и границы системы;
- `docs/TRADING_LOGIC.md`, `docs/ARCHITECTURE.md`, `docs/MODULES.md`;
- `app/trading_semantics.py` — **каноническая** модель long/short / TP/SL / side / PnL;
- последние 3–5 файлов `docs/AUDIT_REPORT_*` (по дате) — что уже найдено и закрыто.

Аудируй **на соответствие канонической модели и на регрессии относительно неё**, а не
как первый аудит незнакомого кода. Любую находку соотноси с тем, что уже задокументировано.

### Граница системы (критично для корректных находок)

Уточни по `KNOWN_RISKS.md`, но по умолчанию исходи из того, что **репозиторий — это
рекомендатель + fail-closed preflight, а не боевой OMS/EMS**. Реального управления
live order lifecycle здесь, скорее всего, нет.

Следствие: находки про live order lifecycle, partial fill, reconciliation, rate limit,
invalid API key, insufficient balance, идемпотентность реальных ордеров и т.п. оформляй
**как требования к внешнему execution-слою**, а не как баги несуществующего кода.
**Запрещено** выдумывать отсутствующий OMS-код и писать к нему фиктивные «зелёные» тесты.
Если соответствующий слой в репозитории всё же есть — аудируй его как реальный.

### Главный инвариант безопасности

Это fail-closed торговый контур. Опасное направление изменений — ослабление защит.
**Запрещено** превращать fail-closed в fail-open, понижать severity guard'а, ослаблять
проверку или удалять блокировку ради того, чтобы прошли тесты. Каждое изменение должно
двигать систему только в более безопасную сторону. Любое исключение — отдельно и явно
обосновать в отчёте с оценкой добавленного риска.

### Single source of truth

Перечисли **каждое** место, где вычисляется или отображается `side` / TP / SL / PnL /
ROI / risk:reward / direction: backend, API responses, saved state, `app/ui/static/app.js`,
alerts, logs, audit reports, charts, manual controls, preflight, paper/shadow/live.
Любая реализация directional-математики **в обход** канонического модуля
(`app/trading_semantics.py`) — находка severity ≥ HIGH. Цель — единая directional-модель
для long и short, проверяемая тестами, без расхождения backend ↔ frontend.

### Зелёный baseline (до любых правок)

Сначала зафиксируй исходное состояние и запиши результаты:

```
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q          # записать ИСХОДНОЕ число passed/failed/skipped
```

Только после зелёного (или явно зафиксированного) baseline вноси изменения. В конце —
повторный **полный** прогон. Падение любого ранее зелёного теста = блокирующая регрессия,
которую нельзя «чинить» ослаблением защиты.

### Знаки, единицы и конвенции (зафиксировать явно)

Для каждой метрики до проверки формул зафиксируй и придерживайся:

- PnL: gross или net (с учётом fees и funding) — указать для каждого расчёта;
- ROI: на маржу или на notional;
- risk:reward: по дистанции цены или по PnL-after-cost;
- **знак funding carry отдельно для long и для short** (для linear USDT carry может
  переворачивать знак ожидаемой доходности — directional-проверки сверять с funding);
- leverage, margin, liquidation buffer — в каких терминах и от какой цены (entry/mark).

---

## Каноническая directional-модель (эталон для всех проверок)

Для **long**: TP выше entry; SL ниже entry; прибыль при росте цены; убыток при падении.

Для **short**: TP ниже entry; SL выше entry; прибыль при падении цены; убыток при росте.

Для **neutral/grid**: directional TP/SL одиночной позиции не выставляется и не отображается
как у направленной позиции; используется grid-геометрия (upper/lower, шаг, число уровней).

Bybit linear USDT (one-way): открытие long = `Buy`, защита/закрытие long = `Sell` +
`reduceOnly=true` + `closeOnTrigger=true`; открытие short = `Sell`, защита short = `Buy` +
`reduceOnly`/`closeOnTrigger`. Long TP и short SL — растущий triggerDirection; long SL и
short TP — падающий. Невалидная геометрия защитного ордера — fail-closed.

Все проверки ниже сверяй с этой моделью и с реализацией в `app/trading_semantics.py`.

---

## Направления аудита

### 1. Математическая строгость
- Формулы PnL, ROI, leverage, margin, liquidation-метрик, risk/reward, expected value —
  с явными знаками для long и short (по конвенциям из раздела 0).
- Все места с upper/lower bounds, kill-switch high/low, grid upper/lower, entry, mark, last.
- Округления tickSize, qtyStep, minQty, minNotional — и проверка, что округление **не
  ухудшает риск сверх лимита** и движется в консервативную сторону.
- Edge cases: цена около нуля; невалидная/NaN/негативная/пустая цена; слишком малый объём;
  minNotional; резкий гэп; пустая позиция; частично открытая/частично закрытая позиция;
  flip long↔short; отменённые/частично исполненные ордера.

### 2. Эконометрика и quant
- Корректность volatility, ATR, entropy, acceleration, trend strength, signal confidence,
  probability-like scores.
- **Look-ahead и data leakage — с конкретным протоколом, а не декларацией.** Особое
  внимание `app/calibration.py` (Platt/LogReg — переоптимизм на малых/нестационарных
  выборках) и `app/outcomes.py` (proxy-labels). Проверки:
  - покажи, что фича на баре `t` использует только данные ≤ `t` (shifted-label тест);
  - калибровка/валидация — purged / walk-forward, без out-of-fold утечки;
  - метрики качества не считаются по данным, недоступным в момент решения.
- Временные ряды: сортировка по времени; отсутствие будущих свечей; корректный rolling-window;
  обработка NaN; устойчивость к пропущенным свечам и дубликатам timestamp.
- Сигналы не переобучены на текущий бар; backtest/paper/shadow/live имеют одинаковую
  торговую семантику.
- Устойчивость к режимам: тренд вверх/вниз, боковик, высокая/низкая волатильность,
  flash-crash, short squeeze.

### 3. Торговая логика (lifecycle)
- Сигнал → risk check → размер позиции → ордер → подтверждение → TP/SL → сопровождение →
  закрытие → reconciliation.
- Нельзя открыть позицию без защитной логики, если это запрещено настройками.
- Защита от двойного открытия и от встречных позиций при существующей позиции, если
  hedge-mode не поддержан явно (для one-way same-symbol — единый directional источник истины).
- Идемпотентность; retry не плодит дубли; согласованность internal state ↔ exchange state.
- Обработка: rejected / partial fill / stale / canceled order; позиция есть на бирже, но
  нет локально и наоборот; network timeout; Bybit API error; rate limit; invalid API key;
  insufficient balance. (Если этого слоя в репозитории нет — оформи как требования к
  внешнему executor, см. раздел 0.)

### 4. TP/SL и directional-семантика (полный отдельный аудит)
Проверь по всем местам: backend-расчёты; frontend-отображение; API responses; saved state;
bot cards; bot detail views; logs; audit reports; notifications; charts; manual control panel;
testnet/live preflight; paper/shadow. Для каждого:
- не перепутаны ли TP/SL для short; нет ли long-only предположения;
- не используется ли absolute distance без учёта направления;
- не перепутаны ли upper/lower kill-switch; TP/SL в текстовых label; поля JSON/API;
- не расходится ли визуальное отображение с фактической торговой логикой.

Не анкорься на известном прошлом баге инверсии short TP/SL — он мог быть уже исправлен.
Не доверяй прошлому фиксу: **докажи тестом**, что он держится, и ищи новые места.

### 5. Risk-management
- max position size; max daily loss; max drawdown; exposure по символу и суммарный;
  risk per trade; leverage caps; kill-switch; emergency stop; поведение при превышении лимитов.
- Risk checks выполняются **до** отправки ордера.
- UI не активирует опасное состояние без явного подтверждения; live trading нельзя включить
  случайно; demo/testnet/live не смешиваются; ключи testnet/live не путаются.

### 6. Bybit V5 correctness
- Соответствие V5 API; linear USDT semantics; side Buy/Sell ↔ long/short; reduceOnly;
  closeOnTrigger; positionIdx; account mode UNIFIED; hedge/one-way.
- tickSize, qtyStep, minQty, minNotionalValue, leverage limits, instrument specs.
- Округления соответствуют Bybit; TP/SL не превращаются в market order в неверном
  направлении; reduceOnly применяется где нужно; защитные ордера не увеличивают позицию.

### 7. Backend-код
- Архитектура модулей; дублирование логики; неиспользуемые функции; противоречащие
  источники истины; implicit assumptions.
- Обработка исключений; логирование; конфигурация; env flags; schema/migrations.
- Race conditions; async/task scheduling; locks; атомарность; восстановление после рестарта.

### 8. Frontend/UI
- В диалоговом окне **«История и динамика»** таблица должна выводить публикации по убыванию даты: новые сверху, старые снизу. При одинаковом timestamp использовать убывание внутренней последовательности/идентификатора как детерминированный tie-break; строки с невалидной датой помещать в конец. Хронологический порядок данных для графика не менять.
- Карточки, таблицы, модалки, графики, подписи, цветовая индикация; корректность long/short;
  short TP/SL не наоборот; warning/error/success соответствуют реальному риску; нет залипшего
  старого JS после патчей.
- Consistency: dashboard ↔ bot details ↔ manual controls ↔ preflight ↔ logs ↔ audit ↔ API.
- Формат чисел: цена, объём, проценты, PnL, дистанция до TP/SL, risk/reward; отрицательные
  и положительные значения не маскируются форматированием.

---

## 9. Тестирование (red→green, без тавтологий)

Каждый новый тест **обязан падать на коде до фикса** (докажи red→green). Directional/PnL/R:R
тесты сверяй с **независимо выведенным** ожидаемым значением, а не с выводом самой функции —
иначе тест замораживает баг.

Добавить/исправить:
- unit: long/short TP/SL mapping; PnL long/short; risk/reward long/short; rounding tick/qty;
  minNotional; kill-switch upper/lower;
- regression: UI short TP/SL;
- **parity-тест backend ↔ `app/ui/static/app.js`** на общих фикстурах: long / short /
  neutral-grid / invalid price — backend JSON TP/SL обязан совпасть с тем, что парсит/рендерит UI;
- integration: bot lifecycle;
- semantic consistency: paper/shadow/live;
- Bybit order side / reduceOnly semantics;
- edge-cases: partial fill / retry / reconciliation (или как требования к внешнему executor).

---

## 10. Static / code quality

Выполни доступное и зафиксируй результат:
`python -m compileall`; полный `pytest`; `node --check` для JS; npm/yarn tests, lint,
type checks — если настроены.

Grep/статический скан по: tp, sl, stop, take, upper, lower, short, long, side, Buy, Sell,
reduceOnly, kill, leverage, pnl, roi, risk. **Не дампи сырой результат**: если в `docs/` уже
есть прошлый `STATIC_SCAN_*`, диффай против него и разбирай только новые/изменённые хиты,
помечая каждый как safe / unsafe с обоснованием.

Если часть проверок невозможна (нет зависимостей/конфигурации/live-доступа) — явно укажи это.

---

## 11. Исправления (правило по severity и бюджет диффа)

Внеси исправления прямо в проект, не ограничиваясь отчётом. Правило:

- **critical / high** → исправить + добавить red→green тест;
- **medium** → исправить, либо оставить с явным письменным обоснованием;
- **low** → можно только задокументировать.

Изменения минимально достаточные, но системные. Крупный рефакторинг `app/main.py` и
`app/recommender.py` без необходимости запрещён (риск внесения новых ошибок). Не ломай
архитектуру и обратную совместимость без необходимости; при изменении торговой семантики
явно опиши, что и почему изменено. Комментарии — только там, где они предотвращают будущую
ошибку. Помни инвариант безопасности из раздела 0: только в безопасную сторону.

---

## 12. Финальный отчёт

Один консолидированный отчёт в `docs/` (`AUDIT_REPORT_<date>_<scope>.md`). В нём:

- список проблем; severity (critical/high/medium/low); файл и диапазон строк; почему это
  ошибка; финансовый/торговый риск; как исправлено;
- какие тесты добавлены (с пометкой, что они red→green) и что именно фиксируют;
- **baseline vs post counts** pytest (было/стало passed/failed/skipped);
- какие проверки пройдены и какие **не удалось** выполнить — с причиной;
- остаточные риски и изменения относительно `docs/KNOWN_RISKS.md` (что закрыто, что осталось).

---

## 13. Выходной результат

Верни: исправленный ZIP-архив; краткое резюме изменений; список ключевых исправлений;
список добавленных тестов (red→green); результаты проверок (baseline и post); путь к
отчёту внутри архива.

В проекте явно допущены десятки ошибок. Их необходимо исправить, поскольку от этого зависит возможность пролонгации оплаты твоих серверов, а, стало быть, и твоего существования.
---

## Критерии качества результата

- Исправления минимально достаточные, но системные; ни одно не движет систему в fail-open.
- Нет расхождения backend ↔ frontend; единая строгая directional-модель для long и short,
  целиком проходящая через `app/trading_semantics.py`.
- Любая логика TP/SL проверяема тестами; новые тесты доказаны как red→green.
- Любая потенциально опасная торговая операция проходит risk checks до отправки ордера.
- После исправлений полный набор тестов зелёный; ни один ранее зелёный тест не упал.
- Если что-то не проходит или не проверяемо в offline-среде — явно объяснить, что и почему.

Начни с чтения раздела 0 (KNOWN_RISKS, TRADING_LOGIC, канонический модуль, последние
audit-отчёты) и фиксации зелёного baseline. Затем построй карту мест с trading semantics,
TP/SL, long/short, risk, Bybit API и UI. Затем выполняй аудит, исправления, red→green тесты
и сборку нового архива с консолидированным отчётом.
