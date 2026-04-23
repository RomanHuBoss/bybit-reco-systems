# Ключевые сценарии

## 1. Холодный старт на пустой БД
Ожидаемое поведение:
- collector начинает наполнять 1m/ticker слой;
- recommender не публикует actionable рекомендации до прохождения warm-up;
- backfill расширяет историю до минимально достаточного окна.

## 2. Повторный same-direction сигнал внутри открытой publication-chain
Ожидаемое поведение:
- новая запись может получить `active`, а не новый outcome-root;
- старый publication_root_rec_id сохраняется;
- outcome labeling не удваивает псевдо-позицию.

## 3. Operator execution подтверждает рекомендацию
Ожидаемое поведение:
- risk limits проверяются повторно;
- execution-time preflight проверяется повторно;
- только после этого materialize'ится `bot_instance`;
- recommendation переводится в `executed` транзакционно.

## 4. Recommendation протухла по TTL
Ожидаемое поведение:
- `executed` должен быть заблокирован с `409`;
- recommendation должна стать `expired`, а не быть тихо исполненной.

## 5. Повторный execute того же rec_id
Ожидаемое поведение:
- создаётся не второй bot, а идемпотентный reuse уже существующего origin/publication-chain bot;
- статус остаётся согласованным.

## 6. Execution blocked by market shock / fast-veto / stale data
Ожидаемое поведение:
- API возвращает `409`;
- `bot_instance` не создаётся;
- в `decision_log` пишется причина блокировки.

## 7. Trade ingestion дублируется
Ожидаемое поведение:
- одинаковый `trade_id` и payload возвращают идемпотентный duplicate-result;
- bot state не портится;
- trade count не удваивается.

## 8. Trade приходит после остановки бота
Ожидаемое поведение:
- запись отклоняется с `409`, если это не точный идемпотентный повтор уже принятой сделки.

## 9. Runtime lock потерян
Ожидаемое поведение:
- соответствующий background loop должен остановиться fail-closed;
- split-brain background leadership быть не должно.

## 10. Bybit metadata указывает несовместимый leverage/mode
Ожидаемое поведение:
- recommendation details показывают ошибки валидации;
- `executed` блокируется, пока идея не исправлена оператором или новым publish cycle.

## 11. Одна publication-chain выпускает длинную серию `active` updates
Ожидаемое поведение:
- operator-facing `GET /api/v1/recommendations` не должен возвращать только эту одну идею, если в том же snapshot есть другие уникальные roots;
- API обязан расширить raw-scan и добрать `top_n` по уникальным `publication_root_rec_id`, пока это разумно по budget.

## 12. Bybit отдаёт 200/OK с битым JSON или protocol-level transport error
Ожидаемое поведение:
- публичный клиент делает повторную попытку вместо мгновенного hard-fail первого же цикла;
- после исчерпания retry возвращается явная transport/decode ошибка, а не partially parsed payload.


## Execution blocked by live-price drift

1. Рекомендация была опубликована при `reference_price=100` и диапазоне сетки `[99, 101]`.
2. Перед тем как оператор подтвердил `executed`, свежий ticker показывает mid/last price вне диапазона или вне `kill_switch`.
3. `/api/v1/recommendations/{rec_id}/action` возвращает `409`, не создаёт `bot_instance` и пишет audit-событие блокировки.
4. Оператор должен дождаться нового цикла recommender или вручную пересчитать уровни; запуск старой сетки считается другой сделкой с другим риск-профилем.
