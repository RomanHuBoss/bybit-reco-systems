from __future__ import annotations

import math

import pytest

from app import alerts
from app.outcomes import _extract_cost_components


@pytest.fixture(autouse=True)
def _reset_alert_cooldown_state():
    alerts._last_sent.clear()
    yield
    alerts._last_sent.clear()


def test_alerting_tolerates_missing_status_field(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def fake_send(token: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts, "send_telegram", fake_send)

    alerts.check_and_alert(
        token="tok",
        chat_id="chat",
        symbol_health=[{"status": "ok"}, {"symbol": "ETHUSDT"}, {"status": "missing"}],
        collect_errors_10m=0,
        reco_count=0,
        bot_name="Reco",
    )

    assert sent == [
        "⚠️ <b>Reco</b>\nНет рекомендаций в текущем цикле\nРынок в risk-off режиме или все символы заблокированы.",
    ]


def test_alerting_tolerates_non_mapping_health_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def fake_send(token: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts, "send_telegram", fake_send)

    alerts.check_and_alert(
        token="tok",
        chat_id="chat",
        symbol_health=[{"status": "stale"}, "bad-row", None, {"status": "missing"}],
        collect_errors_10m=0,
        reco_count=3,
        bot_name="Reco",
    )

    assert sent == [
        "🔴 <b>Reco</b>\nДанные устарели/отсутствуют для 2/4 символов\nВозможно коллектор упал. Проверьте логи.",
    ]


def test_extract_cost_components_rejects_non_finite_values() -> None:
    execution_bps, funding_bps = _extract_cost_components(
        {
            "cost_model": {
                "execution_cost_bps": float("nan"),
                "expected_funding_bps": float("inf"),
            }
        },
        fallback_execution_bps=15.0,
    )

    assert math.isfinite(execution_bps)
    assert math.isfinite(funding_bps)
    assert execution_bps == pytest.approx(15.0)
    assert funding_bps == pytest.approx(0.0)
