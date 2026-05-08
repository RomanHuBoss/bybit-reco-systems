from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app import sentiment as sentiment_module


@pytest.fixture()
def isolated_client_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "iteration91.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app, raise_server_exceptions=False)
    try:
        yield app_main, client, conn
    finally:
        client.close()
        conn.close()
        sys.modules.pop("app.main", None)


class _DummyResponse:
    def __init__(self, *, text: str = "", payload=None, should_raise: Exception | None = None):
        self.text = text
        self._payload = payload
        self._should_raise = should_raise

    def raise_for_status(self):
        if self._should_raise is not None:
            raise self._should_raise

    def json(self):
        return self._payload


class _RoutingClient:
    def __init__(self, mapping: dict[str, _DummyResponse | Exception]):
        self.mapping = mapping

    def get(self, url, *args, **kwargs):
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return value


# Русская проверка на операторский ручной ввод: API не должен принимать пустой key
# и не должен засорять БД/аудит лишними пробелами и дублями тегов.
def test_api_sentiment_put_normalizes_key_and_tags_before_persist(isolated_client_and_conn):
    _app_main, client, conn = isolated_client_and_conn

    resp = client.post(
        "/api/v1/sentiment",
        json={
            "scope": "global",
            "key": "  crypto  ",
            "ts": 1_700_100_000,
            "sentiment": 0.2,
            "velocity": 0.05,
            "volume": 2,
            "sources": {"manual": True},
            "tags": ["  macro  ", "", "macro", "  desk  ", "   "],
        },
        headers={"X-API-Key": "test-admin-key"},
    )

    assert resp.status_code == 200
    rows = db.get_sentiment_series(conn, "global", "crypto", limit=10)
    assert rows == [
        {
            "scope": "global",
            "key": "crypto",
            "ts": 1_700_100_000,
            "sentiment": 0.2,
            "velocity": 0.05,
            "volume": 2,
            "sources": {"manual": True},
            "tags": ["macro", "desk"],
        }
    ]
    audit = conn.execute(
        "SELECT details_json FROM decision_log WHERE action='SENTIMENT_PUT' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert audit is not None
    assert '"key": "crypto"' in audit["details_json"]


def test_api_sentiment_put_rejects_blank_key(isolated_client_and_conn):
    _app_main, client, conn = isolated_client_and_conn

    resp = client.post(
        "/api/v1/sentiment",
        json={
            "scope": "global",
            "key": "   ",
            "ts": 1_700_100_001,
            "sentiment": 0.1,
            "velocity": 0.0,
            "volume": 1,
            "sources": {},
            "tags": [],
        },
        headers={"X-API-Key": "test-admin-key"},
    )

    assert resp.status_code == 422
    assert db.get_sentiment_series(conn, "global", "", limit=10) == []
    assert db.get_sentiment_series(conn, "global", "crypto", limit=10) == []
    n_logs = conn.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='SENTIMENT_PUT'").fetchone()["c"]
    assert int(n_logs) == 0


# Один битый RSS-source не должен вырубать весь sentiment sweep, если соседний источник жив.
def test_fetch_rss_sentiment_skips_failed_feed_and_uses_remaining_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sentiment_module, "RSS_URLS", ["https://broken.test/rss", "https://good.test/rss"])
    monkeypatch.setattr(sentiment_module.time, "time", lambda: 1_700_200_000)

    xml = """
    <rss>
      <channel>
        <item>
          <title>Bitcoin rally and growth</title>
          <description>Strong optimism around bitcoin breakout</description>
        </item>
      </channel>
    </rss>
    """.strip()
    client = _RoutingClient(
        {
            "https://broken.test/rss": httpx.ReadTimeout("timeout"),
            "https://good.test/rss": _DummyResponse(text=xml),
        }
    )

    global_point, per_symbol = sentiment_module.fetch_rss_sentiment(client, limit_items=10)

    assert global_point is not None
    assert global_point["ts"] == 1_700_200_000
    assert global_point["volume"] == 1
    assert global_point["sources"] == {"rss": ["https://good.test/rss"]}
    assert global_point["sentiment"] > 0.0
    assert per_symbol["BTCUSDT"]


# Адаптер momentum должен fail-open: сетевой сбой не должен ронять весь sentiment thread.
def test_fetch_coingecko_momentum_returns_empty_map_on_transport_error() -> None:
    class _BrokenClient:
        def get(self, *args, **kwargs):
            raise httpx.ConnectError("network down")

    result = sentiment_module.fetch_coingecko_momentum(_BrokenClient())

    assert result == {}


# Smoke-проверка release-пакета: README и поставочные документы не должны расходиться.
def test_release_artifacts_are_present_and_cross_referenced() -> None:
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")

    assert "docs/audit_" not in readme
    assert "docs/test_report_" not in readme
    assert "instrukciya_operatora_bybit_recommender.docx" in readme
    assert "instrukciya_operatora_bybit_recommender.pdf" in readme
    assert (root / "docs" / "instrukciya_operatora_bybit_recommender.docx").exists()
    assert (root / "docs" / "instrukciya_operatora_bybit_recommender.pdf").exists()
    assert "ADMIN_API_KEY" in env_example
    assert "RUNTIME_LOCK_DB_PATH" in env_example
    assert "SYMBOLS_LINEAR=BTCUSDT,ETHUSDT" in env_example
    assert (root / "docs" / "ARCHITECTURE.md").exists()
    assert (root / "docs" / "MODULES.md").exists()
    assert (root / "docs" / "TRADING_LOGIC.md").exists()
    assert (root / "docs" / "SCENARIOS.md").exists()
    assert (root / "docs" / "KNOWN_RISKS.md").exists()
    assert (root / "CHANGELOG.md").exists()


def test_api_sentiment_put_rejects_nul_in_key(isolated_client_and_conn):
    _app_main, client, conn = isolated_client_and_conn

    resp = client.post(
        "/api/v1/sentiment",
        headers={"X-API-Key": "test-admin-key"},
        json={
            "scope": "global",
            "key": "crypto\u0000desk",
            "ts": 1_700_300_000,
            "sentiment": 0.2,
            "velocity": 0.0,
            "volume": 1,
            "sources": {"manual": ["desk"]},
            "tags": ["manual"],
        },
    )

    assert resp.status_code == 422
    assert "NUL byte" in resp.text
    assert db.get_sentiment_series(conn, "global", "crypto", limit=10) == []


def test_api_sentiment_get_normalizes_whitespace_in_scope_and_key(isolated_client_and_conn):
    _app_main, client, conn = isolated_client_and_conn
    db.insert_sentiment_point(
        conn,
        "global",
        "crypto",
        1_700_300_100,
        0.15,
        0.01,
        3,
        {"manual": ["desk"]},
        ["manual"],
    )

    resp = client.get('/api/v1/sentiment?scope=%20global%20&key=%20crypto%20&limit=5')

    assert resp.status_code == 200
    body = resp.json()
    assert body['scope'] == 'global'
    assert body['key'] == 'crypto'
    assert len(body['items']) == 1
    assert body['items'][0]['sentiment'] == 0.15
