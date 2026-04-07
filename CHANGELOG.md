# CHANGELOG

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
