# Аудит смены рекомендаций, возраста и UI-истории — 2026-06-18

**Scope:** Bybit V5 Linear USDT recommender / recommendation snapshots / publication-chain freshness / operator UI / history timeline  
**Репозиторий:** `bybit-reco-systems-main`  
**Исходный ZIP SHA-256:** `b2a0569b3419d226fab266481a3333b217345c50c7455c1ff0695d535e837400`

## 1. Итог

Подтверждены и исправлены четыре взаимосвязанные проблемы:

1. **HIGH:** режим `latest_operator` мог откатывать operator-list на более старый LLM-проверенный snapshot, пока новый цикл находился в `pending`. В интерфейсе это выглядело как исчезновение, возврат или внезапная смена рекомендации.
2. **HIGH:** повреждённая или далеко будущая метка `recommendation.ts` превращалась в возраст `0`, поэтому запись выглядела свежей и могла пройти часть TTL-проверок.
3. **MEDIUM:** открытая карточка продолжала обновлять прежний `rec_id`, хотя таблица уже показывала новый цикл для той же пары. Параллельные `refreshAll()` могли накладываться друг на друга.
4. **MEDIUM:** у оператора не было способа проследить полную динамику конкретной пары и отличить новую публикационную цепочку от обновления старой.

Добавлен диалог **«История и динамика»** в блоке «Цена и актуальность». Он показывает график LONG/NEUTRAL/SHORT по времени, точки публикаций, смены направления, root/update, статусы БД, LLM-состояние и таблицу всех сохранённых публикаций.

Исправления fail-closed. Directional TP/SL/PnL, Bybit side/reduceOnly semantics и границы внешнего OMS/EMS не изменялись.

## 2. Обязательный pre-read и граница системы

До правок изучены:

- `docs/KNOWN_RISKS.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `app/trading_semantics.py`;
- последние отчёты `docs/AUDIT_REPORT_*`.

Подтверждена граница: репозиторий является рекомендателем, audit/operator UI и fail-closed preflight-контуром. Реального Bybit OMS/EMS, websocket order reconciliation, partial-fill recovery и полного live order lifecycle в нём нет. Эти функции не выдумывались и не имитировались фиктивными тестами.

## 3. Baseline и post-validation

Среда:

```text
Python 3.13.5
Node v22.16.0
```

### Baseline исходного ZIP

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest, все 139 файлов в 3 группах           745 passed / 0 failed / 0 skipped
```

Прямой единый запуск `pytest -q` в окружении инструмента превышал лимит длительности. Поэтому baseline и post были повторно выполнены одинаковым полным покрытием всех 139 `test_*.py` файлов в трёх непересекающихся группах. Артефакты находятся в:

`docs/audit_artifacts/2026-06-18_recommendation_timeline/`

### После исправлений

```text
python -m compileall -q app tests main.py    PASS
node --check app/ui/static/app.js            PASS
pytest chunk 1                               249 passed
pytest chunk 2                               254 passed
pytest chunk 3                               247 passed
TOTAL                                        750 passed / 0 failed / 0 skipped
```

Итого: **745 → 750 тестов**, ранее зелёные торговые, directional, Bybit, risk и UI-parity тесты сохранены. Один прежний тест безопасности изменён намеренно: он требовал разрешать execution при `ts="broken-ts"` и `ttl_sec="broken-ttl"`; теперь ожидается HTTP 409.

## 4. Findings и исправления

### HIGH-01 — `latest_operator` воскрешал старый LLM-ready snapshot

**До исправления:** `app/main.py::_resolve_recommendation_snapshot_ts` искал назад последний snapshot, подходящий под LLM/status-фильтры. Если новый цикл был `pending`, список мог показывать старую `recommended` строку как текущую.

**Риск:**

- возраст относился к старому snapshot;
- направление могло выглядеть как вернувшееся назад;
- после окончания LLM-review интерфейс резко переключался на новую строку;
- оператор не видел факт существования более нового pending/no-trade цикла.

**Исправление:** `latest_operator` теперь всегда выбирает фактически последний publication cycle, а status/LLM-фильтры применяются только внутри него. Исторические режимы `latest_visible` и `latest_llm_ready` сохранены как явные диагностические режимы. См. `app/main.py:4400-4450`.

**Дополнение:** API теперь возвращает `effective_status_counts`, чтобы UI видел строки, которые runtime guards преобразовали из persisted `recommended` в `pending`/`blocked`.

### HIGH-02 — будущая/повреждённая дата маскировалась под возраст 0

**До исправления:** выражения вида `max(0, now - ts)` превращали далеко будущий timestamp в нулевой возраст. Повреждённая дата могла также обходить часть TTL-семантики при некорректном `ttl_sec`.

**Риск:** poisoned legacy/manual row выглядел как только что созданный; TTL и операторская карточка давали ложное ощущение актуальности.

**Исправление:**

- добавлена строгая проверка recommendation timestamp с допустимым clock skew 300 секунд;
- `None`, non-positive, malformed и timestamp дальше допустимого future skew получают `RECOMMENDATION_TIMESTAMP_INVALID` / `PUBLICATION_CHAIN_TIMESTAMP_INVALID`;
- возраст для такой строки равен `null`, а не `0`;
- execution fail-closed блокируется;
- operator guard получает ту же ошибку, effective status становится `blocked`;
- UI выводит **«Некорректная метка времени — запуск заблокирован»**;
- общий `timeAgo()` больше не показывает отрицательные секунды.

Основные места: `app/main.py:1016-1195`, `app/main.py:1510-1590`, `app/ui/static/app.js:32-43`, `app/ui/static/app.js:840-915`.

### MEDIUM-01 — карточка и таблица могли показывать разные поколения рекомендации

**До исправления:** `currentMeta` уже хранил `(venue, symbol, bot_type)`, но refresh карточки повторно загружал только старый `currentRecId`. Таблица в это время переходила на новый snapshot.

**Исправление:** перед обновлением карточки UI запрашивает последнюю публикацию именно для открытой пары и переходит на её `rec_id`. Последовательность карточки защищена существующим `detailsRequestSeq`. См. `app/ui/static/app.js:2330-2348`.

### MEDIUM-02 — перекрывающиеся циклы refresh создавали race в UI

**До исправления:** `setInterval(refreshAll, 10000)` мог запустить следующий цикл до завершения предыдущего. Это усиливало визуальные скачки status/card/table при медленном API.

**Исправление:** добавлен единый `refreshInFlight`; повторный вызов получает тот же Promise и не запускает второй параллельный цикл. См. `app/ui/static/app.js:2631-2644`.

### MEDIUM-03 — отсутствовала наблюдаемость истории конкретной пары

**Исправление backend:**

- `app/db.py:1495-1568` — `get_recommendation_history()` читает raw publication rows одной пары без collapse;
- `GET /api/v1/recommendations/history` — chronological items, root/update, direction/status changes, LLM state, timestamp validity, latest effective status;
- лимит ответа 2000 строк; по умолчанию 500.

**Исправление UI:**

- кнопка **«История и динамика»** рядом с возрастом;
- модальный SVG-график направления;
- крупная точка = новый root, малая = update;
- вертикальная отметка = смена направления;
- цвет точки = persisted status group;
- таблица времени, возраста, направления, статуса, LLM, confidence, score, R/R и типа изменения;
- historical runtime Bybit guards намеренно не реконструируются сегодняшними данными; актуальный effective status вычисляется только для последней записи.

Основные места: `app/ui/static/app.js:1784-1992`, `app/ui/static/styles.css:1374-1490`.

## 5. Red → green доказательства

Артефакты:

- `red_iteration195.txt` — исходные 3 падения: stale snapshot, отсутствующий history endpoint, отсутствующий UI timeline;
- `red_timestamp_validation.txt` — 2 падения: future timestamp давал age=0/не имел validity fields, UI не отображал ошибку;
- `red_timestamp_operator_guard.txt` — persisted future row не попадал в blocked view.

После исправлений:

```text
tests/test_iteration195_recommendation_history_ui.py    5 passed
полный post suite                                  750 passed
```

Новые проверки фиксируют:

1. `latest_operator` не откатывается на старый LLM-ready snapshot;
2. history endpoint возвращает chronological root/update/flip sequence;
3. карточка имеет modal timeline и следует за последним `rec_id` пары;
4. future timestamp не считается age=0 и блокирует execution/operator status;
5. UI явно показывает некорректное время и использует актуальный renderer.

Также обновлены cache-key assertions с `manual-ui-v44` на `manual-ui-v45`, чтобы браузер не продолжал исполнять старый JS.

## 6. Проверка directional single source of truth

Изменения не добавили самостоятельной TP/SL/PnL математики. Исторический график отображает только persisted `direction` и статус. Каноническими остаются:

- `app/trading_semantics.py` — long/short TP/SL, PnL, R:R, Bybit side/trigger/reduceOnly;
- `app/main.py` — backend payload и fail-closed validation;
- UI — renderer backend-полей, а не независимый execution calculator.

Полные directional/Bybit regression-файлы прошли в составе 750 тестов.

## 7. Остаточные риски

1. История хранится полностью в БД, но API/диалог показывают максимум 2000 последних строк. Для более глубокой forensic-выгрузки нужен raw DB/export endpoint.
2. График показывает publication decisions, а не реальные fills/PnL. Без внешнего OMS/reconciliation нельзя утверждать, что точка была исполнена на Bybit.
3. Текущий effective Bybit/preflight status достоверно пересчитывается только для последней записи. Применять сегодняшние instrument/ticker данные к старым строкам было бы исторически неверно.
4. Допустим clock skew до 300 секунд. Более крупное рассогласование системных часов блокирует рекомендацию и требует исправления NTP/host clock.
5. Автоматическое обновление открытой карточки теперь следует последней публикации пары. Для forensic-просмотра старой точки следует пользоваться таблицей в истории; отдельный «pin historical rec_id» пока не реализован.

## 8. Изменённые файлы

```text
app/db.py
app/main.py
app/ui/static/app.js
app/ui/static/index.html
app/ui/static/styles.css
tests/test_iteration101_resilience_hardening.py
tests/test_iteration122_ui_detail_badge_fit.py и другие cache-key regression tests
tests/test_iteration195_recommendation_history_ui.py
docs/KNOWN_RISKS.md
docs/AUDIT_REPORT_2026-06-18_recommendation_freshness_timeline.md
```

## 9. Release result

Исправленный архив содержит этот отчёт и test artifacts. Основной отчёт внутри ZIP:

`docs/AUDIT_REPORT_2026-06-18_recommendation_freshness_timeline.md`
