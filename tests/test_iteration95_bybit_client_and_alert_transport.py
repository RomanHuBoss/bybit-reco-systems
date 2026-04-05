from __future__ import annotations

import httpx
import pytest

from app import alerts
from app.bybit_client import BybitPublicClient


class _FakeHttpResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"retCode": 0, "result": {"list": []}}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "https://example.com"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        return None


@pytest.fixture(autouse=True)
def _reset_alert_state():
    alerts._last_sent.clear()
    yield
    alerts._last_sent.clear()


def test_send_telegram_requires_application_level_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict, float]] = []

    def fake_post(url: str, json: dict, timeout: float):
        calls.append((url, json, timeout))
        return _FakeHttpResponse(status_code=200, payload={"ok": False, "description": "chat not found"})

    monkeypatch.setattr(alerts.httpx, "post", fake_post)

    assert alerts.send_telegram("tok", "chat", "hello") is False
    assert calls == [
        (
            "https://api.telegram.org/bottok/sendMessage",
            {"chat_id": "chat", "text": "hello", "parse_mode": "HTML"},
            8.0,
        )
    ]


def test_check_and_alert_does_not_start_cooldown_after_transport_level_false_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []
    results = iter([False, True])

    def fake_send(token: str, chat_id: str, text: str) -> bool:
        attempts.append(text)
        return next(results)

    monkeypatch.setattr(alerts, "send_telegram", fake_send)

    for _ in range(2):
        alerts.check_and_alert(
            token="tok",
            chat_id="chat-1",
            symbol_health=[{"status": "ok"}],
            collect_errors_10m=5,
            reco_count=3,
            bot_name="Reco",
        )

    assert len(attempts) == 2
    assert alerts._can_send(alerts._alert_key("collect_errors", chat_id="chat-1", bot_name="Reco")) is False


def test_bybit_client_rejects_non_mapping_json_payload_with_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=0)
    client._client = _Transport([_FakeHttpResponse(payload=[{"unexpected": True}])])  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="expected JSON object"):
        client.get_tickers(category="linear")

    client.close()


def test_bybit_client_rejects_invalid_retcode_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=0)
    client._client = _Transport([_FakeHttpResponse(payload={"retCode": "oops", "result": {"list": []}})])  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="invalid retCode"):
        client.get_tickers(category="linear")

    client.close()


def test_bybit_client_ignores_non_mapping_items_inside_result_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=0)
    client._client = _Transport(
        [
            _FakeHttpResponse(
                payload={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {"symbol": "BTCUSDT", "lastPrice": "100"},
                            "broken",
                            123,
                            None,
                            {"symbol": "ETHUSDT", "lastPrice": "200"},
                        ]
                    },
                }
            )
        ]
    )  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    rows = client.get_tickers(category="linear")

    assert rows == [
        {"symbol": "BTCUSDT", "lastPrice": "100"},
        {"symbol": "ETHUSDT", "lastPrice": "200"},
    ]
    client.close()
