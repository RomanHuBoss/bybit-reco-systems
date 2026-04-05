from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app import alerts, db, main
from app.bybit_client import BybitPublicClient


class _BadJsonResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def json(self):
        raise ValueError("not json")


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

    def get(self, url, params=None):
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


def test_recommendations_loader_neutralizes_non_finite_json_tokens_in_reasons(tmp_path: Path) -> None:
    """Poisoned legacy JSON не должен менять min_conf-логику через bool(NaN)."""
    conn = db.connect(str(tmp_path / "poisoned-reasons.db"))
    try:
        db.init_db(conn)
        conn.execute(
            """INSERT INTO recommendations(
                rec_id, ts, venue, symbol, bot_type, direction, account_mode, margin_mode,
                score, confidence, expected_rr, risk_score,
                params_json, reasons_json, blocks_json,
                status, ttl_sec, model_version, features_ref_ts, publication_root_rec_id, is_outcome_label_root
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "R-JSON-NAN",
                2_000_000_000,
                "linear",
                "BTCUSDT",
                "futures_grid",
                "long",
                "oneway",
                "isolated",
                0.15,
                0.10,
                1.20,
                0.05,
                "{}",
                '{"confidence_model":{"fitted":NaN,"source":"platt"}}',
                "[]",
                "recommended",
                600,
                "v1",
                2_000_000_000,
                "R-JSON-NAN",
                1,
            ),
        )
        conn.commit()

        rows = db.get_recommendations(
            conn,
            venue="linear",
            top_n=10,
            min_conf=0.50,
            statuses=["recommended"],
            snapshot_ts=2_000_000_000,
            strict_min_conf=False,
        )

        assert [row["rec_id"] for row in rows] == ["R-JSON-NAN"]
        assert rows[0]["reasons"]["confidence_model"]["fitted"] is None
    finally:
        conn.close()


def test_main_json_loader_maps_non_finite_constants_to_none_inside_structure() -> None:
    payload = '{"details":{"latency_ms":NaN,"retries":2},"status":"ok"}'

    loaded = main._json_loads_mapping_or_default(payload, default={})

    assert loaded == {"details": {"latency_ms": None, "retries": 2}, "status": "ok"}


def test_send_telegram_returns_false_on_http_200_with_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts.httpx, "post", lambda *args, **kwargs: _BadJsonResponse(status_code=200))

    assert alerts.send_telegram("tok", "chat", "hello") is False
    assert alerts._last_sent == {}


def test_bybit_open_interest_page_drops_invalid_rows_and_blank_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=0)
    client._client = _Transport(
        [
            _FakeHttpResponse(
                payload={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {"timestamp": "1700000000000", "openInterest": "123.45"},
                            {"timestamp": "0", "openInterest": "100"},
                            {"timestamp": "1700000060000", "openInterest": "NaN"},
                            "broken",
                        ],
                        "nextPageCursor": "   ",
                    },
                }
            )
        ]
    )  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    rows, cursor = client.get_open_interest_page("BTCUSDT")

    assert rows == [{"ts": 1_700_000_000, "oi": 123.45}]
    assert cursor is None
    client.close()
