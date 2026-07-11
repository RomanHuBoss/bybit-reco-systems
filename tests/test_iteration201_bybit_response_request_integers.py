from __future__ import annotations

from typing import Any

import pytest

from app import bybit_client as bybit_client_module
from app.bybit_client import BybitPublicClient


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


class _Transport:
    def __init__(self, payloads: list[dict[str, Any]]):
        self._responses = [_Response(payload) for payload in payloads]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any] | None = None) -> _Response:
        self.calls.append((url, dict(params or {})))
        if not self._responses:
            raise AssertionError("unexpected extra Bybit request")
        return self._responses.pop(0)

    def close(self) -> None:
        return None


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, Any]],
    *,
    max_retries: int = 0,
) -> tuple[BybitPublicClient, _Transport]:
    transport = _Transport(payloads)
    monkeypatch.setattr(bybit_client_module.httpx, "Client", lambda **_kwargs: transport)
    client = BybitPublicClient(
        "https://api.bybit.com",
        max_retries=max_retries,
        backoff_base_sec=0.05,
    )
    return client, transport


@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]}},
        {"retCode": None, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]}},
        {"retCode": False, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]}},
        {"retCode": 0.5, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]}},
        {"retCode": "", "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]}},
        {"retCode": [], "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]}},
    ],
)
def test_bybit_response_requires_present_exact_integer_retcode(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    client, transport = _make_client(monkeypatch, [payload])

    with pytest.raises(RuntimeError, match="invalid retCode"):
        client.get_tickers("linear", "BTCUSDT")

    client.close()
    assert len(transport.calls) == 1


def test_malformed_zero_like_retcode_is_retried_instead_of_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, transport = _make_client(
        monkeypatch,
        [
            {
                "retCode": False,
                "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "1"}]},
            },
            {
                "retCode": 0,
                "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]},
            },
        ],
        max_retries=1,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(bybit_client_module.time, "sleep", lambda delay: sleeps.append(float(delay)))

    rows = client.get_tickers("linear", "BTCUSDT")

    client.close()
    assert rows == [{"symbol": "BTCUSDT", "lastPrice": "100"}]
    assert len(transport.calls) == 2
    assert len(sleeps) == 1


def test_exact_integral_numeric_retcode_remains_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _transport = _make_client(
        monkeypatch,
        [
            {
                "retCode": 0.0,
                "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100"}]},
            }
        ],
    )

    assert client.get_tickers("linear", "BTCUSDT") == [
        {"symbol": "BTCUSDT", "lastPrice": "100"}
    ]
    client.close()


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("kline", {"limit": True}),
        ("kline", {"limit": 5.7}),
        ("kline", {"start": True}),
        ("kline", {"start": 1_700_000_000_000.5}),
        ("kline", {"end": False}),
        ("kline", {"end": -1}),
        ("open_interest", {"limit": False}),
        ("open_interest", {"limit": 48.5}),
        ("open_interest", {"start_ms": True}),
        ("open_interest", {"start_ms": 1_700_000_000_000.5}),
        ("open_interest", {"end_ms": False}),
        ("open_interest", {"end_ms": -1}),
    ],
)
def test_bybit_request_integer_fields_reject_boolean_fractional_and_negative_time(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    kwargs: dict[str, Any],
) -> None:
    client, transport = _make_client(
        monkeypatch,
        [{"retCode": 0, "result": {"list": []}}],
    )

    with pytest.raises(ValueError, match="exact integer|non-negative"):
        if method == "kline":
            client.get_kline("linear", "BTCUSDT", **kwargs)
        else:
            client.get_open_interest_page("BTCUSDT", **kwargs)

    client.close()
    assert transport.calls == []


@pytest.mark.parametrize("method", ["kline", "open_interest"])
def test_bybit_request_rejects_inverted_time_window_before_network(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    client, transport = _make_client(
        monkeypatch,
        [{"retCode": 0, "result": {"list": []}}],
    )

    with pytest.raises(ValueError, match="must not be greater"):
        if method == "kline":
            client.get_kline("linear", "BTCUSDT", start=2_000, end=1_000)
        else:
            client.get_open_interest_page("BTCUSDT", start_ms=2_000, end_ms=1_000)

    client.close()
    assert transport.calls == []


def test_exact_integral_request_numbers_are_normalized_without_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, transport = _make_client(
        monkeypatch,
        [
            {"retCode": 0, "result": {"list": []}},
            {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}},
        ],
    )

    client.get_kline(
        "linear",
        "BTCUSDT",
        limit=5.0,
        start=1_700_000_000_000.0,
        end=1_700_000_060_000.0,
    )
    client.get_open_interest_page(
        "BTCUSDT",
        limit=48.0,
        start_ms=1_700_000_000_000.0,
        end_ms=1_700_014_400_000.0,
    )

    client.close()
    assert transport.calls[0][1]["limit"] == "5"
    assert transport.calls[0][1]["start"] == "1700000000000"
    assert transport.calls[0][1]["end"] == "1700000060000"
    assert transport.calls[1][1]["limit"] == "48"
    assert transport.calls[1][1]["startTime"] == "1700000000000"
    assert transport.calls[1][1]["endTime"] == "1700014400000"
