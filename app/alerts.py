from __future__ import annotations

"""
Telegram alerting — optional, activated by TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env.
Sends alerts for: collect errors spike, all symbols stale, no recommendations.
"""

import time
from typing import Any

import httpx

_last_sent: dict[str, float] = {}   # dedup: alert_key → last_sent_ts
COOLDOWN_SEC = 600                   # don't repeat same alert class within 10 min


def _can_send(key: str) -> bool:
    now = time.time()
    return now - _last_sent.get(key, 0) >= COOLDOWN_SEC


def _mark_sent(key: str) -> None:
    _last_sent[key] = time.time()


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Fire-and-forget Telegram message. Returns True on success."""
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=8.0)
        return r.status_code == 200
    except Exception:
        return False


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

    # 1. Collect error spike
    if collect_errors_10m >= 5 and _can_send("collect_errors"):
        if send_telegram(token, chat_id,
            f"⚠️ <b>{bot_name}</b>\n"
            f"🔴 {collect_errors_10m} ошибок сбора за 10 мин\n"
            f"Проверьте подключение к Bybit API или символы в .env"
        ):
            _mark_sent("collect_errors")

    # 2. Majority of symbols stale/missing
    n_bad = sum(1 for s in symbol_health if s["status"] in ("stale", "missing"))
    n_total = len(symbol_health)
    if n_total > 0 and n_bad / n_total >= 0.5 and _can_send("symbols_stale"):
        if send_telegram(token, chat_id,
            f"🔴 <b>{bot_name}</b>\n"
            f"Данные устарели/отсутствуют для {n_bad}/{n_total} символов\n"
            f"Возможно коллектор упал. Проверьте логи."
        ):
            _mark_sent("symbols_stale")

    # 3. No recommendations at all
    if reco_count == 0 and _can_send("no_recos"):
        if send_telegram(token, chat_id,
            f"⚠️ <b>{bot_name}</b>\n"
            f"Нет рекомендаций в текущем цикле\n"
            f"Рынок в risk-off режиме или все символы заблокированы."
        ):
            _mark_sent("no_recos")
