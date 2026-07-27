from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app import collector, db


def _trade(*, source: str, session_id: str | None = None) -> dict:
    row = {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "trade_id": "trade-1",
        "trade_ts_ms": 1_700_000_000_000,
        "seq": 1,
        "side": "Buy",
        "price": 100.0,
        "qty": 0.01,
        "received_ts_ms": 1_700_000_000_001,
        "source": source,
        "is_block_trade": False,
        "is_rpi_trade": False,
    }
    if session_id is not None:
        row.update({
            "stream_session_id": session_id,
            "stream_message_index": 1,
            "stream_row_index": 0,
            "stream_message_ts_ms": 1_700_000_000_001,
        })
    return row


def test_market_trade_poll_and_stream_enter_shared_ingest_lock_before_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "trade-lock.db"))
    db.init_db(conn)
    calls: list[str] = []

    monkeypatch.setattr(
        db,
        "acquire_market_trade_ingest_lock",
        lambda _conn: calls.append("lock"),
        raising=False,
    )

    db.record_market_trade_poll(
        conn,
        venue="linear",
        symbol="BTCUSDT",
        rows=[_trade(source="rest_recent_trade_v1")],
        snapshot_ts_ms=1_700_000_000_001,
    )
    db.record_market_trade_stream_batch(
        conn,
        venue="linear",
        symbol="BTCUSDT",
        rows=[_trade(source="websocket_public_trade_v1", session_id="session-1")],
        message_ts_ms=1_700_000_000_001,
        session_id="session-1",
    )

    assert calls == ["lock", "lock"]
    conn.close()


def test_rest_fallback_commits_each_symbol_so_ingest_lock_is_not_held_across_http_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    commit_flags: list[bool] = []

    class _Client:
        def get_recent_public_trades(self, symbol: str, limit: int):
            return {
                "snapshot_ts_ms": 1_700_000_000_001,
                "items": [{**_trade(source="rest_recent_trade_v1"), "symbol": symbol, "trade_id": f"{symbol}-1"}],
            }

    class _Conn:
        def commit(self) -> None:
            return None

    monkeypatch.setattr(
        db,
        "record_market_trade_poll",
        lambda *args, **kwargs: (
            commit_flags.append(bool(kwargs.get("commit"))),
            {"inserted": 1, "coverage_extended": False, "gap_detected": False},
        )[1],
    )
    monkeypatch.setattr(collector, "_MARKET_TRADE_PRUNE_LAST_TS", 1_700_000_000)

    stats = collector._collect_market_trade_journal(
        _Conn(),
        _Client(),
        "linear",
        ["BTCUSDT", "ETHUSDT"],
        poll_limit=1000,
        retention_hours=72,
        now_ts=1_700_000_001,
    )

    assert commit_flags == [True, True]
    assert stats["symbols_polled"] == 2


def _import_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "locks.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "0")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def test_startup_reclaims_only_dead_same_host_runtime_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_main = _import_main(monkeypatch, tmp_path)
    app_main.RUNTIME_OWNER = "RRMPC:999"
    with closing(app_main._get_lock_conn()) as conn:
        db.init_runtime_lock_db(conn)
        conn.execute(
            "INSERT INTO runtime_locks(lock_key, owner, heartbeat_ts) VALUES(?,?,?)",
            ("runtime:collector", "RRMPC:111", 1_800_000_000),
        )
        conn.execute(
            "INSERT INTO runtime_locks(lock_key, owner, heartbeat_ts) VALUES(?,?,?)",
            ("runtime:reco", "RRMPC:222", 1_800_000_000),
        )
        conn.execute(
            "INSERT INTO runtime_locks(lock_key, owner, heartbeat_ts) VALUES(?,?,?)",
            ("runtime:outcomes", "REMOTE:333", 1_800_000_000),
        )
        conn.commit()

    reclaimed = app_main._reclaim_dead_local_runtime_locks(
        process_alive_fn=lambda pid: pid == 222,
        hostname="RRMPC",
    )

    assert reclaimed == ["runtime:collector"]
    with closing(app_main._get_lock_conn()) as conn:
        rows = conn.execute("SELECT lock_key, owner FROM runtime_locks ORDER BY lock_key").fetchall()
    assert [(row["lock_key"], row["owner"]) for row in rows] == [
        ("runtime:outcomes", "REMOTE:333"),
        ("runtime:reco", "RRMPC:222"),
    ]


def test_lifespan_reclaims_dead_local_locks_before_starting_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_main = _import_main(monkeypatch, tmp_path)
    events: list[str] = []
    monkeypatch.setattr(app_main, "_reclaim_dead_local_runtime_locks", lambda: events.append("reclaim"), raising=False)
    monkeypatch.setattr(app_main, "_start_background_thread", lambda name, target: events.append(f"start:{name}"))
    monkeypatch.setattr(app_main, "_join_background_threads", lambda: None)

    async def _run() -> None:
        async with app_main.lifespan(app_main.app):
            assert events

    asyncio.run(_run())
    assert events[0] == "reclaim"
    assert any(item == "start:collector" for item in events)


def test_periodic_prune_enforces_market_trade_retention_while_websocket_is_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_main = _import_main(monkeypatch, tmp_path)
    old_ms = int(__import__("time").time() * 1000) - 73 * 3600 * 1000
    with closing(app_main._get_conn()) as conn:
        db.upsert_market_trades(conn, [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "trade_id": "old-trade",
            "trade_ts_ms": old_ms,
            "seq": 1,
            "side": "Buy",
            "price": 100.0,
            "qty": 0.01,
            "received_ts_ms": old_ms,
            "source": "websocket_public_trade_v1",
            "is_block_trade": False,
            "is_rpi_trade": False,
        }])
        db.insert_market_trade_coverage(
            conn,
            coverage_id="old-coverage",
            venue="linear",
            symbol="BTCUSDT",
            coverage_start_ms=old_ms,
            coverage_end_ms=old_ms + 1,
            state="closed",
            source="websocket_public_trade_v1",
            gap_reason="test",
        )

    deleted = app_main._prune_technical_data_once()

    assert deleted["market_trade"] == 1
    assert deleted["market_trade_coverage"] == 1
    with closing(app_main._get_conn()) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM market_trade").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM market_trade_coverage").fetchone()["c"] == 0


def test_postgres_ingest_lock_uses_transaction_scoped_advisory_lock() -> None:
    calls: list[tuple[str, tuple]] = []

    class _PgConn:
        db_engine = db.POSTGRES

        def execute(self, sql: str, params=()):
            calls.append((sql, tuple(params)))
            return self

    assert db.acquire_market_trade_ingest_lock(_PgConn()) is True
    assert len(calls) == 1
    sql, params = calls[0]
    assert "pg_advisory_xact_lock" in sql
    assert params == (db.MARKET_TRADE_INGEST_ADVISORY_LOCK_ID,)


def test_market_trade_upsert_orders_unique_keys_deterministically() -> None:
    batches: list[list[tuple]] = []

    class _Cursor:
        rowcount = 3

    class _Conn:
        total_changes = None

        def executemany(self, _sql: str, values):
            batches.append(list(values))
            return _Cursor()

        def commit(self) -> None:
            return None

    rows = [
        {**_trade(source="rest_recent_trade_v1"), "symbol": "ETHUSDT", "trade_id": "b"},
        {**_trade(source="rest_recent_trade_v1"), "symbol": "BTCUSDT", "trade_id": "z"},
        {**_trade(source="rest_recent_trade_v1"), "symbol": "BTCUSDT", "trade_id": "a"},
    ]
    assert db.upsert_market_trades(_Conn(), rows) == 3
    assert [(row[0], row[1], row[2]) for row in batches[0]] == [
        ("linear", "BTCUSDT", "a"),
        ("linear", "BTCUSDT", "z"),
        ("linear", "ETHUSDT", "b"),
    ]


def test_market_trade_prune_enters_shared_ingest_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "prune-lock.db"))
    db.init_db(conn)
    calls: list[str] = []
    monkeypatch.setattr(
        db,
        "acquire_market_trade_ingest_lock",
        lambda _conn: calls.append("lock"),
        raising=False,
    )

    db.prune_market_trade_journal(conn, 1_700_000_000_000)

    assert calls == ["lock"]
    conn.close()
