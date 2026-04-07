# Changelog

## 2026-04-07 neutral TP semantics and default horizon revision

### Исправлено
- Для `neutral` у grid-ботов убран success-shortcut по одноразовому касанию `tp_per_leg`: outcome снова считается только по oscillation / PnL-логике, без двустороннего barrier-hit semantics.
- Дефолтный label horizon для `spot_grid` и `futures_grid` изменён с `6h` на `12h`; соответствующие default-значения trade plan / outcome labeling синхронизированы.

### Добавлено
- Тест на то, что `neutral` не получает `success=1` от одиночного касания одной TP-границы.

## 2026-04-07 grid TP outcome revision

### Исправлено
- Для `spot_grid` / `futures_grid` success в outcome-labeling теперь выставляется в `1`, если внутри окна разметки был фактически достигнут `trade_plan.levels.tp_per_leg`.
- WR-статистика по grid теперь наследует эту семантику через `reco_outcomes.success`, а не зависит только от matched oscillation legs / end-of-horizon proxy.
- `ret` для таких исходов больше не остаётся ложно-отрицательным при фактическом касании TP: если per-leg TP достигнут, outcome proxy фиксирует положительный realised-profit floor.

### Добавлено
- Тесты `tests/test_iteration106_grid_tp_success_semantics.py` на long/short grid и на `tp_per_leg` в форматах `abs` и `pct`.

## 2026-04-06 audit hardening revision

### Исправлено
- Закрыт небезопасный сценарий, в котором mutating API оставался удалённо доступным при пустом `ADMIN_API_KEY`: теперь без ключа разрешён только loopback-доступ.
- Исправлена рассинхронизация `.env.example` и README по `LLM_REVIEWER_TTL_SEC`: auto-TTL снова документирован и конфигурируется пустым значением.
- Улучшен retry/backoff публичного Bybit-клиента: при retryable HTTP/Bybit-ошибках теперь учитывается `Retry-After`, если upstream его возвращает.
- Закрыта опасная логическая дыра в `executed`-path: operator-confirmation больше не опирается только на recommendation-time snapshot и теперь блокируется при stale/missing market data, активном market shock / fast-veto и критичной невалидности trade plan относительно Bybit metadata.
- Панель деталей рекомендации теперь явно показывает `bybit_meta` и `bybit_plan_validation`, чтобы оператор видел ошибки и предупреждения по диапазону, шагу сетки и leverage до ручного запуска бота.

### Добавлено
- Документация `docs/ARCHITECTURE.md`.
- Документация `docs/MODULES.md`.
- Документация `docs/TRADING_LOGIC.md`.
- Документация `docs/SCENARIOS.md`.
- Документация `docs/KNOWN_RISKS.md`.
- Сценарные тесты `tests/test_iteration105_execution_preflight_and_bybit_validation.py` на execution-time preflight и выдачу Bybit-валидации в API деталей рекомендации.

### Уточнено
- README теперь явно фиксирует, что проект не отправляет ордера на Bybit и не является полнофункциональным execution-engine.
- Документация синхронизирована с фактическим execute-path: partial fill / private reconciliation по-прежнему отсутствуют, а Bybit-валидация внутри проекта остаётся частичной и не заменяет внешний sizing/OMS слой.
