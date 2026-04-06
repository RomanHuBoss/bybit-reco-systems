# Ключевые сценарии

## 1. Публикация новой идеи
- символ проходит stale-data gate;
- direction/regime и score достаточны;
- risk/shock/veto не блокируют;
- persistence-gate подтверждён;
- рекомендация получает `status=recommended`.

## 2. Повтор сигнала по той же chain
- если предыдущая same-direction root-идея ещё внутри horizon, новый сигнал переиспользует chain как `active`;
- если живой pseudo-position уже завершён и cooldown не мешает, возможна новая root-публикация.

## 3. Operator execute
- `POST /api/v1/recommendations/{rec_id}/action` с `executed`;
- recommendation переводится в `executed`;
- создаётся `bot_instance` или переиспользуется running bot той же chain.

## 4. Operator stop
- `POST /api/v1/bots/{bot_id}/stop`;
- bot получает `status=stopped`;
- в state пишутся `stop_reason`, `stopped_by`, `stopped_ts`.

## 5. Trade ingestion
- `POST /api/v1/bots/{bot_id}/trades` принимает realized trade event;
- `trade_id` идемпотентен;
- обновляются агрегаты PnL/fee в `state_json`.

## 6. Recovery после падения процесса
- фоновые циклы переизбирают leader через runtime lock;
- статус warm-up и thread state восстанавливается из `app_config`;
- open publication-chain и outcomes продолжают рассчитываться из SQLite.

## 7. Безопасность mutating API
- при заданном `ADMIN_API_KEY` mutating API требует корректный `X-API-Key`;
- при пустом ключе mutating API разрешён только с loopback, что сохраняет local-dev сценарий и закрывает удалённый небезопасный доступ.
