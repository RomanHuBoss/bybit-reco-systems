# CHANGELOG

## 2026-04-08 — release consistency and stop-state determinism

### Исправлено
- `.env.example` синхронизирован с фактическими runtime-дефолтами LLM-reviewer (`LLM_REVIEWER_MAX_CANDIDATES=24`, `LLM_REVIEWER_MAX_WORKERS=2`);
- остановка бота теперь использует единый `stopped_ts` для строки `bot_instances` и `state_json`, чтобы audit/state reconciliation был детерминированным.

### Добавлено
- `docs/AUDIT_REPORT_2026-04-08.md` с итогами red-team-аудита;
- API-регрессии на синхронность `stopped_ts` для manual stop и `stop_bot=true` при записи trade.

### Тесты
- расширен регрессионный набор на stop-state timestamp consistency;
- подтверждена согласованность `.env.example` с runtime/default docs.

## 2026-04-07 — audit hardening revision

### Исправлено
- усилена execution-time Bybit validation:
  - добавлена проверка внутренних инвариантов `bot_type ↔ venue ↔ direction`;
  - добавлена проверка `account_mode` / `margin_mode` против фактической модели проекта;
  - добавлена проверка `min_leverage`, `max_leverage`, `leverage_step`;
  - validation теперь явно показывает `snapped` leverage при off-step значении;
- шаблон `.env.example` теперь явно содержит `SYMBOLS_SPOT`, а не только закомментированный пример.

### Добавлено
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `docs/TRADING_LOGIC.md`
- `docs/SCENARIOS.md`
- `docs/KNOWN_RISKS.md`

### Тесты
- добавлены регрессионные тесты на mode/leverage validation;
- добавлены тесты release-integrity для новых docs/env cross-reference.
