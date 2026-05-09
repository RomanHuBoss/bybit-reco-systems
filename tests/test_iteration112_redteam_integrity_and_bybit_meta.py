from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

from app import db
from app.bybit_client import BybitPublicClient


class _FakeHttpResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

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


class _Cursor:
    def __init__(self, row=None):
        self._row = row
        self.rowcount = 1 if row is not None else 0

    def fetchone(self):
        return self._row


class _FakeIntegrityError(Exception):
    pass


class _FakePgConnForBotInsert:
    db_engine = "postgresql"

    def __init__(self):
        self.aborted = False
        self.sql: list[str] = []

    def execute(self, sql: str, params=()):
        normalized = " ".join(str(sql).split()).lower()
        self.sql.append(normalized)
        if self.aborted and not normalized.startswith("rollback to savepoint") and not normalized.startswith("release savepoint"):
            raise RuntimeError("current transaction is aborted")
        if normalized.startswith("savepoint "):
            return _Cursor()
        if normalized.startswith("rollback to savepoint "):
            self.aborted = False
            return _Cursor()
        if normalized.startswith("release savepoint "):
            return _Cursor()
        if "from bot_instances where bot_id=" in normalized:
            return _Cursor(None)
        if normalized.startswith("insert into bot_instances"):
            self.aborted = True
            raise _FakeIntegrityError("duplicate key value violates unique constraint")
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        return None


class _FakePgConnForTradeInsert:
    db_engine = "postgresql"

    def __init__(self):
        self.aborted = False
        self.sql: list[str] = []
        self._after_conflict_row = {
            "bot_id": "B-1",
            "ts": 1700000000,
            "symbol": "BTCUSDT",
            "pnl": 12.5,
            "fee": 0.4,
            "meta_json": "{}",
        }

    def execute(self, sql: str, params=()):
        normalized = " ".join(str(sql).split()).lower()
        self.sql.append(normalized)
        if self.aborted and not normalized.startswith("rollback to savepoint") and not normalized.startswith("release savepoint"):
            raise RuntimeError("current transaction is aborted")
        if normalized.startswith("savepoint "):
            return _Cursor()
        if normalized.startswith("rollback to savepoint "):
            self.aborted = False
            return _Cursor()
        if normalized.startswith("release savepoint "):
            return _Cursor()
        if normalized.startswith("select bot_id, ts, symbol, pnl, fee, meta_json from trades where trade_id="):
            if any(item.startswith("insert into trades") for item in self.sql):
                return _Cursor(self._after_conflict_row)
            return _Cursor(None)
        if normalized.startswith("insert into trades"):
            self.aborted = True
            raise _FakeIntegrityError("duplicate key value violates unique constraint")
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        return None


def test_bybit_client_get_instrument_info_requires_exact_symbol_match(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=0)
    client._client = _Transport(
        [
            _FakeHttpResponse(
                {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {"symbol": "ETHUSDT", "priceFilter": {"tickSize": "0.01"}},
                            {"symbol": "DOGEUSDT", "priceFilter": {"tickSize": "0.0001"}},
                        ]
                    },
                }
            )
        ]
    )  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    assert client.get_instrument_info("linear", "BTCUSDT") is None
    client.close()


def test_prefetched_bybit_meta_keeps_actual_upstream_symbol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "meta-actual-symbol.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def get_instrument_info(self, category: str, symbol: str):
            return {
                "symbol": "ETHUSDT",
                "category": "linear",
                "priceFilter": {"tickSize": "0.1", "minPrice": "1", "maxPrice": "1000000"},
                "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "100"},
            }

        def close(self):
            return None

    monkeypatch.setattr(app_main, "BybitPublicClient", _Client)
    app_main._instrument_meta_cache.clear()

    try:
        meta = app_main._fetch_bybit_instrument_meta("linear", "BTCUSDT")
    finally:
        sys.modules.pop("app.main", None)

    assert meta["symbol"] == "ETHUSDT"
    assert meta["category"] == "linear"


def test_insert_bot_instance_classifies_duplicate_origin_after_postgres_integrity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakePgConnForBotInsert()
    monkeypatch.setattr(db, "INTEGRITY_ERRORS", (_FakeIntegrityError,))
    monkeypatch.setattr(db, "get_bot_by_origin_rec", lambda _conn, rec_id: {"bot_id": "B-existing", "origin_rec_id": rec_id})
    monkeypatch.setattr(db, "get_bot_by_publication_root", lambda *_args, **_kwargs: None)

    result = db.insert_bot_instance(
        conn,
        {
            "bot_id": "B-new",
            "started_ts": 1700000000,
            "stopped_ts": None,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "mode": {"account_mode": "unified", "margin_mode": "isolated", "direction": "long"},
            "params": {},
            "state": {},
            "status": "running",
            "origin_rec_id": "R-1",
            "publication_root_rec_id": "R-root",
        },
    )

    assert result == "duplicate_origin"
    assert any(item.startswith("rollback to savepoint") for item in conn.sql)


def test_insert_trade_recovers_from_postgres_integrity_error_via_savepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakePgConnForTradeInsert()
    monkeypatch.setattr(db, "INTEGRITY_ERRORS", (_FakeIntegrityError,))

    result = db.insert_trade(
        conn,
        {
            "trade_id": "T-1",
            "bot_id": "B-1",
            "ts": 1700000000,
            "symbol": "BTCUSDT",
            "pnl": 12.5,
            "fee": 0.4,
            "meta": {},
        },
    )

    assert result == "duplicate"
    assert any(item.startswith("rollback to savepoint") for item in conn.sql)


def test_release_docs_omit_audit_report_artifact_references() -> None:
    root = Path(__file__).resolve().parent.parent
    for path in (root / "README.md", root / "CHANGELOG.md"):
        payload = path.read_text(encoding="utf-8")
        assert "AUDIT_REPORT_" not in payload
        assert "docs/AUDIT_REPORT_" not in payload
