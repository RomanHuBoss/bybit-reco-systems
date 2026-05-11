# Audit report — LLM OK-verdict guard for Bybit Linear Futures Grid — 2026-05-11

## A. Краткое резюме

Повторный аудит выполнен по приложенному промпту: продуктовая граница остаётся строго одной — рекомендации только для `futures_grid` на Bybit `linear` USDT perpetual. Основная подтверждённая ошибка: операторский UI мог показывать `active`/`recommended` для строк, где LLM-review отсутствовал или ещё находился в `pending`. Это противоречило безопасной семантике: если LLM-reviewer включён, запуск не должен быть actionable до финального OK-вердикта.

Проект был потенциально опасен в operator lifecycle: строка без LLM-вердикта могла выглядеть запускаемой, хотя reviewer ещё не подтвердил направление/режим. Исправлено fail-closed: при `LLM_REVIEWER_ENABLED=1` actionable-статусы требуют `llm_review.status=ok`; иначе новая рекомендация сохраняется как `pending`, а legacy stored `active/recommended` в API/UI принудительно показываются как effective `pending`.

Полный тестовый набор после исправлений: `464 passed`.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| LLM lifecycle | `advisory`-режим оставлял `active/recommended` при `llm_review.status=pending` | Оператор мог запустить grid без LLM-вердикта | `_mark_llm_reviews_async()` теперь переводит actionable rows в `pending` до `llm_review.status=ok` | `app/recommender.py` |
| API/UI lifecycle | Legacy rows со stored `active/recommended` и отсутствующим LLM-review показывались как запускаемые | В интерфейсе статус противоречил safety contract | `_augment_reco_for_ui()` применяет effective-status guard: без OK LLM verdict строка становится `pending`, `stored_status` сохраняется для аудита | `app/main.py` |
| Pending visibility | При фильтре `show_pending=true` legacy stored `active` мог не попасть в выборку, хотя effective status должен быть `pending` | Оператор не видел удержанную рекомендацию в pending-режиме | `_operator_fetch_statuses_for_effective_filters()` расширяет выборку pending на stored `recommended/active` при включённом LLM | `app/main.py` |
| Timeout policy | Stale advisory pending мог возвращаться в actionable status через `skipped` | Строка снова становилась запускаемой без реального LLM OK | `_resolve_stale_llm_pending()` теперь fail-closed переводит stale LLM hold в `no_trade` для всех LLM modes | `app/recommender.py` |
| UI copy | Карточка LLM объясняла старую non-blocking advisory semantics | Пользователь получал неверное объяснение статуса | Текст UI обновлён: если LLM включён, запуск удерживается в pending до OK; timeout -> no_trade | `app/ui/static/app.js` |
| Docs/tests | README и тесты закрепляли старое advisory non-blocking поведение | Регрессия могла вернуться незамеченной | README/.env обновлены; добавлен regression test для API-demotion legacy active без verdict | `README.md`, `.env.example`, `tests/*` |

## C. Исправления торговой логики

- Grid logic: новых стратегий не добавлено; `SUPPORTED_BOT_TYPES` остаётся только `futures_grid`.
- PnL/fees/funding/leverage/liquidation: текущие fail-closed проверки сохранены; изменения не ослабляют economics/preflight. Рекомендация без LLM OK теперь не может быть operator-actionable даже при положительной сеточной экономике.
- Risk score/recommendation logic: добавлен отдельный lifecycle-risk layer — `llm_verdict_required`. Он не заменяет market/risk блокеры, а стоит поверх них как публикационный guard.
- Recommendation/rejection logic: без OK LLM verdict статус становится `pending`; если reviewer не завершился за `LLM_REVIEWER_PENDING_TIMEOUT_SEC`, статус становится `no_trade` fail-closed.

## D. Исправления backend

- `app/recommender.py`
  - добавлены `_llm_review_is_completed_ok()` и `_hold_recommendation_until_llm_verdict()`;
  - `_mark_llm_reviews_async()` больше не допускает `active/recommended` при `pending/error/none` LLM-review;
  - `_make_pending_llm_review()` пишет `hold_policy=llm_verdict_required` и `requires_ok_verdict=true`;
  - `_resolve_stale_llm_pending()` больше не восстанавливает advisory rows в actionable без OK; timeout переводит в `no_trade`.

- `app/main.py`
  - добавлены `_llm_status_from_reasons_dict()` и `_apply_llm_effective_pending_guard()`;
  - API сохраняет audit-поле `stored_status`, но отдаёт безопасный `status=pending`;
  - pending-фильтр подхватывает stored `recommended/active`, которые effective-guard демотирует в `pending`.

## E. Исправления frontend/UI/UX

- `app/ui/static/app.js`
  - LLM-card теперь прямо говорит: если reviewer включён, запуск удерживается в `pending` до OK-вердикта;
  - timeout объясняется как fail-closed переход в `no_trade`, а не как non-blocking advisory marker.

## F. Исправления документации и конфигов

- `README.md`
  - переписана семантика LLM-reviewer: `recommended/active` допустимы только после `llm_review.status=ok`;
  - lifecycle steps обновлены: `LLM_REVIEWER_ENABLED=1` удерживает actionable rows в `pending` до OK.

- `.env.example`
  - комментарии к LLM-reviewer обновлены: без OK verdict строка остаётся pending; timeout fail-closed.

## G. Тесты

Добавлены/обновлены:

- `tests/test_iteration143_llm_verdict_required.py`
  - legacy stored `active` без LLM verdict в API отдаётся как effective `pending`;
  - та же строка не показывается как actionable при `show_pending=false`.

- `tests/test_iteration141_llm_pending_timeout.py`
  - advisory и gate оба удерживают actionable до OK;
  - stale advisory/gate pending fail-closed в `no_trade`.

- `tests/test_iteration102_shutdown_and_llm_rank_hardening.py`
  - rank sanitization больше не допускает actionable без OK verdict.

- `tests/test_logic.py`
  - active/advisory LLM expectations обновлены под `llm_verdict_required`.

Команда и результат:

```bash
pytest -q
# 464 passed in 16.82s
```

## H. Остаточные риски

- Нужна live-проверка реального LLM/Ollama endpoint: latency, timeout, модель, GPU memory.
- Нужна сверка реальных Bybit fee tiers и funding с production-аккаунтом.
- Нужна периодическая live-проверка instrument filters Bybit: tick size, qty step, min notional, leverage limits.
- Slippage/fill model остаётся приближением; нужен paper/live shadow режим перед production.
- Если LLM reviewer недоступен, система будет чаще переводить идеи в `no_trade`; это безопаснее, но снижает число actionable рекомендаций.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
python main.py
```

Локальный LLM-reviewer:

```bash
export LLM_REVIEWER_ENABLED=1
export LLM_REVIEWER_MODE=advisory
export LLM_REVIEWER_MODEL=qwen3:8b
export LLM_REVIEWER_PENDING_TIMEOUT_SEC=900
python main.py
```

Gate-mode:

```bash
export LLM_REVIEWER_ENABLED=1
export LLM_REVIEWER_MODE=gate
export LLM_REVIEWER_PENDING_TIMEOUT_SEC=900
python main.py
```
