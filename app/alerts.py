"""
Telegram alerting — optional, activated by TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env.
Sends alerts for: collect errors spike, all symbols stale, no recommendations.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

_last_sent: dict[str, float] = {}   # dedup: alert_key → last_sent_ts
COOLDOWN_SEC = 600                   # don't repeat same alert class within 10 min


def _alert_key(base_key: str, *, chat_id: str | None = None, bot_name: str | None = None) -> str:
    """Namespace cooldowns by destination/bot to avoid cross-instance suppression."""
    return f"{chat_id or '-'}::{bot_name or '-'}::{base_key}"


def _can_send(key: str) -> bool:
    now = time.time()
    return now - _last_sent.get(key, 0) >= COOLDOWN_SEC


def _mark_sent(key: str) -> None:
    _last_sent[key] = time.time()


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Fire-and-forget Telegram message. Returns True on success.

    Telegram иногда отвечает HTTP 200, но с payload `{"ok": false, ...}`.
    Для alerting это критично: ложный success привёл бы к установке cooldown
    и фактической потере следующего реального алерта. Поэтому считаем успехом
    только transport-level 200 + application-level `ok=true`.
    """
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        response = httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=8.0)
        if response.status_code != 200:
            return False
        payload = response.json()
        return isinstance(payload, dict) and bool(payload.get("ok"))
    except Exception:
        return False


def _health_status(row: Any) -> str:
    """Нормализует статус health-строки для alerting-контура.

    Alerting не должен ронять весь recommender-цикл из-за частично битой
    диагностической записи. В худшем случае такая запись считается "неok"
    и участвует только в stale/missing-сводке.
    """
    if isinstance(row, dict):
        return str(row.get("status") or "").strip().lower()
    return ""


def check_and_alert(
    token: str | None,
    chat_id: str | None,
    symbol_health: list[dict[str, Any]],
    collect_errors_10m: int,
    reco_count: int,
    bot_name: str = "Bybit Reco",
) -> None:
    """
    Called after each recommender cycle. Sends alerts if thresholds crossed.
    token / chat_id: None → silently skip (Telegram not configured).
    """
    if not token or not chat_id:
        return

    collect_errors_key = _alert_key("collect_errors", chat_id=chat_id, bot_name=bot_name)
    symbols_stale_key = _alert_key("symbols_stale", chat_id=chat_id, bot_name=bot_name)
    no_recos_key = _alert_key("no_recos", chat_id=chat_id, bot_name=bot_name)

    # 1. Collect error spike
    if collect_errors_10m >= 5 and _can_send(collect_errors_key):
        if send_telegram(token, chat_id,
            f"⚠️ <b>{bot_name}</b>\n"
            f"🔴 {collect_errors_10m} ошибок сбора за 10 мин\n"
            f"Проверьте подключение к Bybit API или символы в .env"
        ):
            _mark_sent(collect_errors_key)

    # 2. Majority of symbols stale/missing
    # Symbol health может содержать частично деградировавшие/legacy строки.
    # Alerting обязан оставаться fail-safe и не превращаться в источник падения.
    statuses = [_health_status(s) for s in symbol_health]
    n_bad = sum(1 for status in statuses if status in ("stale", "missing"))
    n_total = len(statuses)
    has_healthy_symbol = any(status == "ok" for status in statuses)
    if n_total > 0 and n_bad / n_total >= 0.5 and _can_send(symbols_stale_key):
        if send_telegram(token, chat_id,
            f"🔴 <b>{bot_name}</b>\n"
            f"Данные устарели/отсутствуют для {n_bad}/{n_total} символов\n"
            f"Возможно коллектор упал. Проверьте логи."
        ):
            _mark_sent(symbols_stale_key)

    # 3. No recommendations at all
    # Only meaningful when at least part of the market-data layer is healthy.
    # Otherwise warm-up / data outages would generate a misleading strategy alert.
    if reco_count == 0 and has_healthy_symbol and _can_send(no_recos_key):
        if send_telegram(token, chat_id,
            f"⚠️ <b>{bot_name}</b>\n"
            f"Нет рекомендаций в текущем цикле\n"
            f"Рынок в risk-off режиме или все символы заблокированы."
        ):
            _mark_sent(no_recos_key)
