# Audit report: health/outcomes latency — v1.0.72

Дата: 2026-07-17  
Итерация: 259  
Исходная версия: 1.0.71  
Релиз: 1.0.72

## 1. Наблюдаемый дефект

Оператор сообщил, что окна «Здоровье» и «Исходы» открываются до минуты и более. Диагностический снимок 1.0.71 показывает исправный Windows runtime, но уже крупную БД: 275 305 рекомендаций, 29 074 исхода и 118 416 записей журнала. Следовательно, задержка не объясняется stalled-worker или stale market data и должна рассматриваться как read-path scalability defect.

## 2. Подтверждённые причины

### 2.1 `/api/v1/status` декодировал весь архив outcomes

`api_status()` вызывал `iter_calibration_lineage_rows()` без SQL-фильтра текущей модели. На каждом открытии health endpoint передавал из БД все outcome rows вместе с `reasons_json`, декодировал JSON и только после этого отделял старые model lineage. Время запроса линейно росло с immutable archive.

### 2.2 Exact-policy observability читала все строки текущей модели

`get_policy_outcome_observability()` выбирала все roots текущей модели, включая многочисленные `shadow_no_trade` с `policy_evaluation_eligible=false`, и разбирала их JSON, хотя они заведомо не относятся к exact-policy cohort.

### 2.3 Окно «Исходы» дважды выполняло полную агрегацию

UI параллельно запрашивал `current_policy` и `archive`. Оба вызова `get_outcomes_stats()` загружали весь `reco_outcomes` archive, присоединяли крупный `reasons_json` и строили все Python matrices. При этом UI использовал от archive только headline totals и последние 20 строк.

### 2.4 Current-policy scope не фильтровался по model version в SQL

Scope проверялся после получения строк, поэтому даже маленькая текущая cohort требовала передачи всего архива.

### 2.5 Модальное окно отображалось только после завершения запросов

Даже при нормальной сетевой задержке оператор не видел подтверждения нажатия до окончания всех backend reads.

## 3. Реализованное исправление

1. Добавлен `db.get_outcome_history_summary()` — SQL-only totals, wins, losses, effective sample size и entropy по историческому архиву.
2. `iter_calibration_lineage_rows(current_model_version=...)` выполняет SQL prefilter и декодирует JSON только текущей модели.
3. `api_status()` объединяет SQL historical summary с полной canonical verification текущей model lineage.
4. `get_policy_outcome_observability()` prefilter-ит:
   - текущую модель;
   - bot type;
   - созревший timestamp;
   - exact-policy или подозрительные malformed rows;
   - materialized LLM eligibility.
   Легитимные `shadow_no_trade`/`excluded` строки не декодируются. Malformed/contradictory rows остаются fail-closed и могут попасть в invalid-contract counters.
5. `get_outcomes_stats()` фильтрует `current_model/current_policy` по `model_version` в SQL.
6. Добавлен архивный контракт `detail=summary`:
   - totals/cohorts агрегируются SQL;
   - detailed matrices не строятся;
   - JSON декодируется только для bounded recent list.
7. Frontend запрашивает `scope=archive&detail=summary` и немедленно открывает modal со статусом загрузки.
8. Добавлен индекс `idx_reco_model_outcome_scope(model_version, is_outcome_label_root, rec_id)` в SQLite/PostgreSQL init и runtime migration.

## 4. Неизменённая семантика

Не изменялись:

- `mean_reversion_min_score=0.25`;
- risk/economic gates;
- LLM advisory contract;
- policy fingerprint и canonical contract verification;
- outcome label `grid_label_v26`;
- fail-closed censoring внутрисвечной неоднозначности и недостаточного объёма;
- distinction `shadow_exploration` / exact-policy calibration;
- recommendation/audit-only boundary.

## 5. RED → GREEN regression

Новый файл: `tests/test_iteration259_health_outcomes_performance.py`.

Проверяется:

- historical archive не декодирует JSON в health path;
- current lineage декодирует только строки текущей модели;
- archive summary сохраняет headline totals при bounded JSON reads;
- observability отбрасывает 250 shadow rows до JSON и проверяет единственную exact-policy row;
- UI открывает modal до fetch и использует `detail=summary`.

Результат: `4 passed`.

## 6. Синтетический benchmark

SQLite, 8 000 outcomes, около 2 КБ дополнительного JSON на recommendation:

| Операция | Время |
|---|---:|
| Полная архивная статистика | 0,3963 с |
| Archive `detail=summary` | 0,0698 с |
| Lineage всех 8 000 строк | 0,1744 с |
| Lineage текущей модели, 20 строк | 0,0656 с |
| SQL historical aggregate | 0,0655 с |

Краткий архивный запрос в этой проверке быстрее полного примерно в 5,7 раза. На отдельной синтетической БД с 3 000 outcomes полный `api_status()` версии 1.0.72 завершился за 0,1247 с и корректно показал 3 000 historical / 10 current-model outcomes.

Benchmark демонстрирует topology improvement, но не является обещанием конкретной задержки на production PostgreSQL/Windows host.

## 7. Проверки

- Collection: 1183 tests.
- Full post-check: 1183/1183 passed в 8 непересекающихся shards.
- Targeted health/outcomes/runtime suite: 30 passed.
- Post-filter observability suite: 22 passed.
- `python -m compileall -q app`: passed.
- `node --check app/ui/static/app.js`: passed.
- Fresh/repeated SQLite initialization: passed.
- Новый индекс присутствует после init.
- Схема таблиц и данные не удаляются.

## 8. Миграция и эксплуатация

Ручная SQL-миграция не требуется. `db.init_db()` идемпотентно создаёт индекс. На первой загрузке 1.0.72 запуск приложения может потратить дополнительное время на создание индекса для существующей таблицы recommendations; после этого обычные health/outcomes reads используют новый bounded path.

Не требуется:

- очищать PostgreSQL;
- удалять outcomes/recommendations;
- менять `.env`;
- снижать торговые пороги;
- отключать LLM.

## 9. Остаточные риски

- Явный `detail=full` для archive остаётся O(N) и предназначен для редкого исследовательского аудита.
- При дальнейшем росте immutable history может потребоваться PostgreSQL partitioning/архивная политика.
- Реальный production PostgreSQL endpoint timing не был измерен без доступа к пользовательской БД.
- UI latency также зависит от локальной машины, браузера и конкурирующей нагрузки outcome/calibration workers.

## 10. Rollback

Вернуть файлы версии 1.0.71 и перезапустить приложение. Удалять индекс необязательно: он обратно совместим и не меняет данные. Откат таблиц или исходов не требуется.
