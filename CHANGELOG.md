# Changelog

## 2026-04-06 audit hardening revision

### Исправлено
- Закрыт небезопасный сценарий, в котором mutating API оставался удалённо доступным при пустом `ADMIN_API_KEY`: теперь без ключа разрешён только loopback-доступ.
- Исправлена рассинхронизация `.env.example` и README по `LLM_REVIEWER_TTL_SEC`: auto-TTL снова документирован и конфигурируется пустым значением.
- Улучшен retry/backoff публичного Bybit-клиента: при retryable HTTP/Bybit-ошибках теперь учитывается `Retry-After`, если upstream его возвращает.

### Добавлено
- Документация `docs/ARCHITECTURE.md`.
- Документация `docs/MODULES.md`.
- Документация `docs/TRADING_LOGIC.md`.
- Документация `docs/SCENARIOS.md`.
- Документация `docs/KNOWN_RISKS.md`.

### Уточнено
- README теперь явно фиксирует, что проект не отправляет ордера на Bybit и не является полнофункциональным execution-engine.
