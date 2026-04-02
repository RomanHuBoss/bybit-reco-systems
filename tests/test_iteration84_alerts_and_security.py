from __future__ import annotations

import pytest

from app import alerts
from app.security import KeyStore, is_authorized


@pytest.fixture(autouse=True)
def _reset_alert_cooldown_state():
    alerts._last_sent.clear()
    yield
    alerts._last_sent.clear()


def test_no_recos_alert_requires_at_least_one_healthy_symbol(monkeypatch: pytest.MonkeyPatch):
    sent: list[tuple[str, str, str]] = []

    def fake_send(token: str, chat_id: str, text: str) -> bool:
        sent.append((token, chat_id, text))
        return True

    monkeypatch.setattr(alerts, "send_telegram", fake_send)

    alerts.check_and_alert(
        token="tok",
        chat_id="chat-1",
        symbol_health=[{"status": "stale"}, {"status": "missing"}],
        collect_errors_10m=0,
        reco_count=0,
        bot_name="Reco",
    )

    assert sent == [
        (
            "tok",
            "chat-1",
            "🔴 <b>Reco</b>\nДанные устарели/отсутствуют для 2/2 символов\nВозможно коллектор упал. Проверьте логи.",
        )
    ]


def test_no_recos_alert_fires_when_data_layer_is_healthy(monkeypatch: pytest.MonkeyPatch):
    sent: list[str] = []

    def fake_send(token: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts, "send_telegram", fake_send)

    alerts.check_and_alert(
        token="tok",
        chat_id="chat-1",
        symbol_health=[{"status": "ok"}, {"status": "ok"}],
        collect_errors_10m=0,
        reco_count=0,
        bot_name="Reco",
    )

    assert sent == [
        "⚠️ <b>Reco</b>\nНет рекомендаций в текущем цикле\nРынок в risk-off режиме или все символы заблокированы.",
    ]


def test_alert_cooldown_is_scoped_by_chat_and_bot(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str, str]] = []

    def fake_send(token: str, chat_id: str, text: str) -> bool:
        calls.append((token, chat_id, text))
        return True

    monkeypatch.setattr(alerts, "send_telegram", fake_send)

    alerts.check_and_alert(
        token="tok",
        chat_id="chat-A",
        symbol_health=[{"status": "ok"}],
        collect_errors_10m=5,
        reco_count=3,
        bot_name="Reco-A",
    )
    alerts.check_and_alert(
        token="tok",
        chat_id="chat-A",
        symbol_health=[{"status": "ok"}],
        collect_errors_10m=5,
        reco_count=3,
        bot_name="Reco-A",
    )
    alerts.check_and_alert(
        token="tok",
        chat_id="chat-B",
        symbol_health=[{"status": "ok"}],
        collect_errors_10m=5,
        reco_count=3,
        bot_name="Reco-A",
    )
    alerts.check_and_alert(
        token="tok",
        chat_id="chat-A",
        symbol_health=[{"status": "ok"}],
        collect_errors_10m=5,
        reco_count=3,
        bot_name="Reco-B",
    )

    assert calls == [
        (
            "tok",
            "chat-A",
            "⚠️ <b>Reco-A</b>\n🔴 5 ошибок сбора за 10 мин\nПроверьте подключение к Bybit API или символы в .env",
        ),
        (
            "tok",
            "chat-B",
            "⚠️ <b>Reco-A</b>\n🔴 5 ошибок сбора за 10 мин\nПроверьте подключение к Bybit API или символы в .env",
        ),
        (
            "tok",
            "chat-A",
            "⚠️ <b>Reco-B</b>\n🔴 5 ошибок сбора за 10 мин\nПроверьте подключение к Bybit API или символы в .env",
        ),
    ]


def test_keystore_roundtrip_and_optional_env_behavior():
    master_key = b"YKuM2YgMCjd2VaNo2lQ9-kPz0-I2vsir0_MAHA2TIt4="
    ks = KeyStore.from_env(master_key.decode("utf-8"))

    assert ks is not None
    token = ks.encrypt("secret-value")
    assert token != "secret-value"
    assert ks.decrypt(token) == "secret-value"
    assert KeyStore.from_env(None) is None
    assert KeyStore.from_env("") is None


def test_is_authorized_handles_missing_and_constant_time_inputs():
    assert is_authorized(None, None) is True
    assert is_authorized("admin", None) is False
    assert is_authorized("admin", "wrong") is False
    assert is_authorized("admin", "admin") is True
