from __future__ import annotations

import json

import pytest

from app import db
from app import trade_stream


def _rest_trade(*, trade_id: str = "t-1", trade_ts_ms: int = 1_700_000_000_000) -> dict:
    return {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "trade_id": trade_id,
        "trade_ts_ms": trade_ts_ms,
        "seq": 1,
        "side": "Buy",
        "price": 100.0,
        "qty": 0.01,
        "received_ts_ms": trade_ts_ms,
        "source": "rest_recent_trade_v1",
        "is_block_trade": False,
        "is_rpi_trade": False,
    }


def test_rest_trade_poll_equal_snapshot_keeps_valid_conservative_coverage(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "rest-equal-snapshot.db"))
    db.init_db(conn)
    snapshot_ms = 1_700_000_000_000

    result = db.record_market_trade_poll(
        conn,
        venue="linear",
        symbol="BTCUSDT",
        rows=[_rest_trade(trade_ts_ms=snapshot_ms)],
        snapshot_ts_ms=snapshot_ms,
    )

    row = conn.execute(
        "SELECT coverage_start_ms, coverage_end_ms, state FROM market_trade_coverage WHERE coverage_id=?",
        (result["coverage_id"],),
    ).fetchone()
    assert row is not None
    assert int(row["coverage_start_ms"]) == snapshot_ms + 1
    assert int(row["coverage_end_ms"]) == snapshot_ms + 1
    assert row["state"] == "open"
    conn.close()


class _Cursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = list(rows or [])
        self.rowcount = 0

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _FakePgConnection:
    db_engine = "postgresql"

    def __init__(self) -> None:
        self.aborted = False
        self.sql: list[str] = []

    def execute(self, sql: str, params=()):
        normalized = " ".join(str(sql).split()).lower()
        self.sql.append(normalized)
        if self.aborted and not normalized.startswith("rollback to savepoint") and not normalized.startswith("release savepoint"):
            raise RuntimeError("current transaction is aborted")
        if normalized.startswith("select pg_advisory_xact_lock"):
            return _Cursor({"market_trade_ingest_lock": None})
        if normalized.startswith("savepoint "):
            return _Cursor()
        if normalized.startswith("rollback to savepoint "):
            self.aborted = False
            return _Cursor()
        if normalized.startswith("release savepoint "):
            return _Cursor()
        if normalized.startswith("select trade_id from market_trade"):
            return _Cursor(rows=[])
        if "from market_trade_coverage" in normalized:
            return _Cursor(None)
        if normalized == "select 1":
            return _Cursor({"ok": 1})
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        self.aborted = False


def test_rest_trade_poll_rewinds_postgres_transaction_after_caught_write_error(monkeypatch) -> None:
    conn = _FakePgConnection()

    def fail_upsert(*args, **kwargs):
        conn.aborted = True
        raise RuntimeError("simulated postgres write failure")

    monkeypatch.setattr(db, "upsert_market_trades", fail_upsert)

    with pytest.raises(RuntimeError, match="simulated postgres write failure"):
        db.record_market_trade_poll(
            conn,
            venue="linear",
            symbol="BTCUSDT",
            rows=[_rest_trade()],
            snapshot_ts_ms=1_700_000_000_100,
            commit=False,
        )

    # The caller catches per-symbol errors and writes COLLECT_ERROR on the same
    # connection. PostgreSQL permits that only if the failed unit was rewound.
    assert conn.execute("SELECT 1").fetchone() == {"ok": 1}
    assert any(item.startswith("rollback to savepoint") for item in conn.sql)


def test_public_trade_stream_disables_library_keepalive_and_uses_bybit_heartbeat(tmp_path) -> None:
    captured: dict = {}
    sent: list[dict] = []

    class _WebSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send(self, payload: str) -> None:
            sent.append(json.loads(payload))

        def recv(self, timeout: float):
            return None

    def connect_fn(*args, **kwargs):
        captured.update(kwargs)
        return _WebSocket()

    conn = db.connect(str(tmp_path / "ws-options.db"))
    db.init_db(conn)
    trade_stream.run_public_trade_stream_session(
        conn,
        bybit_http_base_url="https://api.bybit.com",
        symbols=["BTCUSDT"],
        stop_requested=lambda: False,
        connect_fn=connect_fn,
    )

    assert captured["ping_interval"] is None
    assert captured["ping_timeout"] is None
    assert sent[0]["op"] == "subscribe"
    conn.close()


def test_public_trade_stream_reconnects_when_application_heartbeat_has_no_reply(tmp_path, monkeypatch) -> None:
    class _Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def monotonic(self) -> float:
            self.value += 2.0
            return self.value

    recv_calls = {"n": 0}
    sent: list[dict] = []

    class _SilentWebSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send(self, payload: str) -> None:
            sent.append(json.loads(payload))

        def recv(self, timeout: float):
            recv_calls["n"] += 1
            raise TimeoutError("no frame")

    clock = _Clock()
    monkeypatch.setattr(trade_stream.time, "monotonic", clock.monotonic)

    conn = db.connect(str(tmp_path / "ws-idle.db"))
    db.init_db(conn)
    stats = trade_stream.run_public_trade_stream_session(
        conn,
        bybit_http_base_url="https://api.bybit.com",
        symbols=["BTCUSDT"],
        stop_requested=lambda: recv_calls["n"] >= 5,
        connect_fn=lambda *args, **kwargs: _SilentWebSocket(),
        ping_interval_sec=1,
        ping_timeout_sec=3,
    )

    assert stats["disconnect_reason"] == "application_heartbeat_timeout"
    assert stats["disconnect_error_type"] == "TimeoutError"
    assert any(item.get("op") == "ping" for item in sent)
    conn.close()
