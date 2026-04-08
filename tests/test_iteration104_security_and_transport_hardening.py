from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.bybit_client import BybitPublicClient
from app.security import is_authorized


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict:
        return dict(self._payload)


class _SequenceClient:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict | None = None):
        self.calls.append((url, dict(params or {})))
        if not self._responses:
            raise AssertionError("unexpected extra call")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        return None


@pytest.fixture()
def client_no_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "api_no_admin.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setenv("SYMBOLS_SPOT", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    client = TestClient(app_main.app)
    try:
        yield client
    finally:
        client.close()
        sys.modules.pop("app.main", None)


def test_is_authorized_allows_loopback_only_when_admin_key_missing() -> None:
    assert is_authorized(None, None) is True
    assert is_authorized(None, None, client_host="127.0.0.1") is True
    assert is_authorized(None, None, client_host="::1") is True
    assert is_authorized(None, None, client_host="localhost") is True
    assert is_authorized(None, None, client_host="192.168.1.10") is False
    assert is_authorized(None, None, client_host="example.com") is False


def test_mutating_api_is_not_open_to_non_loopback_when_admin_key_missing(client_no_admin: TestClient) -> None:
    resp = client_no_admin.post(
        "/api/v1/sentiment",
        json={
            "scope": "global",
            "key": "crypto",
            "sentiment": 0.1,
            "velocity": 0.0,
            "volume": 1,
            "sources": {},
            "tags": [],
        },
    )
    assert resp.status_code == 401
    assert "loopback" in resp.json()["detail"].lower()


def test_bybit_client_honors_retry_after_header_for_retryable_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=1, backoff_base_sec=0.25)
    fake_http = _SequenceClient(
        [
            _FakeResponse(429, {"retCode": 0, "result": {"list": []}}, headers={"Retry-After": "1.5"}),
            _FakeResponse(200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}}),
        ]
    )
    client._client = fake_http  # type: ignore[attr-defined]

    sleeps: list[float] = []
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda delay: sleeps.append(float(delay)))

    rows = client.get_tickers("linear", "BTCUSDT")
    client.close()

    assert rows == [{"symbol": "BTCUSDT"}]
    assert len(fake_http.calls) == 2
    assert sleeps == [1.5]


def test_bybit_client_retries_remote_protocol_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=1, backoff_base_sec=0.0)
    fake_http = _SequenceClient(
        [
            httpx.RemoteProtocolError("upstream protocol desync"),
            _FakeResponse(200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}}),
        ]
    )
    client._client = fake_http  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    rows = client.get_tickers("linear", "BTCUSDT")
    client.close()

    assert rows == [{"symbol": "BTCUSDT"}]
    assert len(fake_http.calls) == 2


def test_bybit_client_retries_retryable_decode_errors_on_200_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenJsonResponse(_FakeResponse):
        def json(self):
            raise ValueError("malformed json")

    client = BybitPublicClient("https://api.bybit.com", max_retries=1, backoff_base_sec=0.0)
    fake_http = _SequenceClient(
        [
            _BrokenJsonResponse(200, {}),
            _FakeResponse(200, {"retCode": 0, "result": {"list": [{"symbol": "ETHUSDT"}]}}),
        ]
    )
    client._client = fake_http  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    rows = client.get_tickers("linear", "ETHUSDT")
    client.close()

    assert rows == [{"symbol": "ETHUSDT"}]
    assert len(fake_http.calls) == 2


def test_docs_describe_loopback_only_admin_fallback_and_auto_llm_ttl() -> None:
    root = Path(__file__).resolve().parent.parent
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "ADMIN_API_KEY=" in env_example
    assert "loopback" in env_example.lower()
    assert "loopback" in readme.lower()
    assert "LLM_REVIEWER_TTL_SEC=" in env_example
    assert "LLM_REVIEWER_TTL_SEC=900" not in env_example
