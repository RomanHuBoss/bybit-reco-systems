# Audit report — LLM pending timeout and linear grid re-audit — 2026-05-11

> Superseded note (2026-05-11): subsequent audit `AUDIT_REPORT_2026-05-11_LLM_OK_VERDICT_GUARD.md` changes the LLM policy. If `LLM_REVIEWER_ENABLED=1`, actionable `recommended/active` now requires `llm_review.status=ok`; otherwise the row is held as effective `pending` and timeout fails closed to `no_trade`. The older non-blocking advisory semantics below are historical only.

## A. Краткое резюме

Повторно проверен проект рекомендательной системы только для Bybit Linear USDT Futures / USDT Perpetual grid-ботов. Основная подтверждённая аварийная проблема: async LLM-reviewer мог удерживать actionable-рекомендации в `pending` слишком долго, особенно если внешний Ollama/LLM слой не отвечал, возвращал error или не был доступен. Это создавало операторский deadlock: рекомендация уже была рассчитана движком, но UI не давал финального actionable/not-actionable статуса.

Исправлено поведение LLM-reviewer:

- `advisory` больше не переводит `recommended`/`active` в `pending`; LLM в этом режиме является second opinion и не блокирует запуск.
- `gate` по-прежнему может временно удерживать рекомендацию в `pending`, но только до `LLM_REVIEWER_PENDING_TIMEOUT_SEC`.
- stale `pending` в `gate` переводится fail-closed в `no_trade`.
- stale advisory-marker переводится в `skipped`, не блокируя engine-status.
- status/runtime API и UI показывают таймаут pending.

Полный тестовый набор после исправлений: `459 passed`.

## B. Критические ошибки

| Область | Ошибка | Риск | Исправление | Файлы |
|---|---|---|---|---|
| LLM-reviewer / lifecycle | `advisory`-reviewer мог переводить actionable рекомендации в `pending` | Рекомендации зависали без финального статуса, хотя advisory не должен быть hard gate | `advisory` теперь non-blocking; статус остаётся `recommended`/`active`, LLM marker пишется отдельно | `app/recommender.py`, tests |
| LLM-reviewer / fault tolerance | При недоступном LLM `pending` мог висеть дольше операторски допустимого времени | Оператор не понимает, запускать ли grid; возможна потеря актуальности market data | Добавлен `LLM_REVIEWER_PENDING_TIMEOUT_SEC`; stale gate-hold fail-closed в `no_trade`, advisory stale marker -> `skipped` | `app/settings.py`, `app/recommender.py`, `.env.example`, `README.md` |
| UI/UX | В health/config UI не было видно SLA/таймаута pending | Невозможно диагностировать, почему рекомендации висят | Добавлен показ `pending_timeout_sec`; текст LLM-card объясняет разницу advisory/gate | `app/main.py`, `app/ui/static/app.js` |
| Regression tests | Старые тесты закрепляли опасную семантику advisory-as-hold | Исправление считалось бы регрессией | Тесты обновлены и добавлены сценарии timeout/advisory/gate | `tests/test_iteration102_shutdown_and_llm_rank_hardening.py`, `tests/test_logic.py`, `tests/test_iteration141_llm_pending_timeout.py` |

## C. Исправления торговой логики

- Продуктовая граница остаётся прежней: только `futures_grid` на `venue=linear` и USDT symbols.
- Изменения не добавляют новых стратегий, не меняют PnL/formula layer и не ослабляют existing risk/preflight gates.
- Gate-mode LLM теперь fail-closed: если внешний reviewer не дал verdict в срок, рекомендация становится `no_trade`, а не остаётся неопределённой.
- Advisory-mode LLM теперь не является частью trading approval path: это снижает риск ложной блокировки валидной engine-рекомендации из-за внешнего LLM outage.

## D. Исправления backend

- `app/settings.py`
  - Добавлен `llm_reviewer_pending_timeout_sec`.
  - Env: `LLM_REVIEWER_PENDING_TIMEOUT_SEC`, default `900`, min `60`, max `86400`.

- `app/recommender.py`
  - Добавлен `LLM_REVIEWER_DEFAULT_PENDING_TIMEOUT_SEC`.
  - Добавлен `_llm_review_pending_timeout_sec()`.
  - `_mark_llm_reviews_async()` больше не ставит `rec["status"] = "pending"` в `advisory`.
  - Добавлен `_resolve_stale_llm_pending()`:
    - advisory + stale pending marker -> `skipped`, status сохраняется/restores;
    - gate + stale pending hold -> `no_trade`, `gate_decision=fail_closed`.
  - `run_llm_review_sweep_once()` теперь сначала разрешает stale pending/markers, даже если reviewer не инициализируется.
  - В stats добавлены `pending_timeout_sec`, `stale_resolved`, `stale_restored`, `stale_failed_closed`.

- `app/main.py`
  - `/api/v1/status` и runtime health возвращают `llm_reviewer.pending_timeout_sec`.

## E. Исправления frontend/UI/UX

- `app/ui/static/app.js`
  - Health/modal config показывает `Таймаут pending`.
  - LLM-card объясняет: advisory не блокирует запуск; gate удерживает pending только до таймаута и затем блокирует fail-closed.

## F. Исправления документации и конфигов

- `.env.example`
  - Добавлен `LLM_REVIEWER_PENDING_TIMEOUT_SEC=900` с пояснением.

- `README.md`
  - Переписана семантика LLM-reviewer:
    - `advisory` не блокирует `recommended/active`;
    - `gate` может удерживать `pending`, но ограниченно;
    - после таймаута gate fail-closed переводит рекомендацию в `no_trade`.
  - Обновлена семантика статуса `pending`.

## G. Тесты

Добавлены/обновлены тесты:

- `tests/test_iteration141_llm_pending_timeout.py`
  - advisory async review не переводит recommendation в `pending`;
  - gate async review удерживает launch до verdict;
  - stale gate pending при недоступном reviewer -> `no_trade`;
  - stale advisory marker -> `skipped`, engine-status остаётся actionable.

- `tests/test_iteration102_shutdown_and_llm_rank_hardening.py`
  - Обновлены ожидания: advisory marker queue не блокирует статус.

- `tests/test_logic.py`
  - Обновлен active/advisory LLM test под non-blocking semantics.

- `tests/test_iteration103_settings_and_docs_consistency.py`
  - README и `.env.example` проверяются на наличие `LLM_REVIEWER_PENDING_TIMEOUT_SEC=900`.

Команда и результат:

```bash
python -m pytest -q
# 459 passed in 9.97s
```

## H. Остаточные риски

- Нужна live-проверка реального Ollama/LLM endpoint: latency, timeout, memory pressure, model availability.
- Нужны реальные Bybit account fee tiers и funding history: проект использует заданные настройки/публичные market fields, но production economics должны сверяться с аккаунтными fee rates.
- Нужна периодическая live-проверка instrument limits Bybit: tick size, qty step, min notional, leverage filters.
- Slippage/fill model остаётся приближением; live execution на futures grid требует paper/live shadow validation.
- Pending timeout не заменяет мониторинг LLM worker: если `stale_failed_closed` растёт, LLM слой деградировал и gate-mode фактически будет отказывать в запуске.

## I. Команды запуска

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
python main.py
```

Для локального LLM-reviewer:

```bash
export LLM_REVIEWER_ENABLED=1
export LLM_REVIEWER_MODE=advisory   # non-blocking second opinion
export LLM_REVIEWER_PENDING_TIMEOUT_SEC=900
python main.py
```

Для gate-mode:

```bash
export LLM_REVIEWER_ENABLED=1
export LLM_REVIEWER_MODE=gate
export LLM_REVIEWER_PENDING_TIMEOUT_SEC=900
python main.py
```

В gate-mode LLM outage теперь не должен оставлять рекомендации в `pending` бесконечно: stale hold становится `no_trade` fail-closed.
