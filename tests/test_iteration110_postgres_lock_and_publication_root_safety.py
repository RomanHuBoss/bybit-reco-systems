from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import db
from app.db_backend import POSTGRES


class _FakeCursor:
    def __init__(self, row=None, rowcount: int = 0):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class _FakePostgresLockConn:
    db_engine = POSTGRES

    def __init__(self):
        self.runtime_locks: dict[str, tuple[str, int]] = {}
        self.executed_sql: list[str] = []
        self.in_transaction = False

    def execute(self, sql: str, params=()):
        self.executed_sql.append(str(sql))
        normalized = " ".join(str(sql).split()).lower()
        if "insert into runtime_locks" in normalized and "on conflict" in normalized and "returning owner" in normalized:
            lock_key, owner, now, expiry_before = params
            existing = self.runtime_locks.get(lock_key)
            if existing is None:
                self.runtime_locks[lock_key] = (owner, int(now))
                return _FakeCursor({"owner": owner}, rowcount=1)
            current_owner, heartbeat_ts = existing
            if current_owner == owner or int(heartbeat_ts) < int(expiry_before):
                self.runtime_locks[lock_key] = (owner, int(now))
                return _FakeCursor({"owner": owner}, rowcount=1)
            return _FakeCursor(None, rowcount=0)
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        self.in_transaction = False

    def rollback(self):
        self.in_transaction = False


def _make_sqlite_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_db(conn)
    return conn


def test_acquire_runtime_lock_is_atomic_in_postgres_mode() -> None:
    conn = _FakePostgresLockConn()

    assert db.acquire_runtime_lock(conn, "collector", "node-a", ttl_sec=90) is True
    assert db.acquire_runtime_lock(conn, "collector", "node-b", ttl_sec=90) is False
    assert any("ON CONFLICT" in sql for sql in conn.executed_sql)


def test_insert_bot_instance_rejects_second_running_bot_for_same_publication_root(tmp_path: Path) -> None:
    conn = _make_sqlite_conn(tmp_path)
    try:
        conn.execute(
            """INSERT INTO recommendations(
                rec_id, ts, venue, symbol, bot_type, direction, account_mode, margin_mode,
                score, confidence, expected_rr, risk_score, params_json, reasons_json, blocks_json,
                status, ttl_sec, model_version, features_ref_ts, publication_root_rec_id, is_outcome_label_root
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "R-root", 1_800_000_000, "linear", "BTCUSDT", "futures_grid", "long", "unified", "isolated",
                0.9, 0.8, 1.4, 0.2, "{}", "{}", "[]",
                "executed", 600, "test", 1_800_000_000, "R-root", 1,
            ),
        )
        conn.execute(
            """INSERT INTO recommendations(
                rec_id, ts, venue, symbol, bot_type, direction, account_mode, margin_mode,
                score, confidence, expected_rr, risk_score, params_json, reasons_json, blocks_json,
                status, ttl_sec, model_version, features_ref_ts, publication_root_rec_id, is_outcome_label_root
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "R-child", 1_800_000_060, "linear", "BTCUSDT", "futures_grid", "long", "unified", "isolated",
                0.91, 0.81, 1.45, 0.21, "{}", "{}", "[]",
                "active", 600, "test", 1_800_000_060, "R-root", 0,
            ),
        )
        conn.commit()

        first = {
            "bot_id": "B-1",
            "started_ts": 1_800_000_100,
            "stopped_ts": None,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "mode": {"account_mode": "unified", "margin_mode": "isolated", "direction": "long"},
            "params": {},
            "state": {},
            "status": "running",
            "origin_rec_id": "R-root",
            "publication_root_rec_id": "R-root",
        }
        second = {
            "bot_id": "B-2",
            "started_ts": 1_800_000_200,
            "stopped_ts": None,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "mode": {"account_mode": "unified", "margin_mode": "isolated", "direction": "long"},
            "params": {},
            "state": {},
            "status": "running",
            "origin_rec_id": "R-child",
            "publication_root_rec_id": "R-root",
        }

        assert db.insert_bot_instance(conn, first) == "inserted"
        assert db.insert_bot_instance(conn, second) == "duplicate_publication_root_running"

        running = db.get_bot_by_publication_root(conn, "R-root", status="running")
        assert running is not None
        assert running["bot_id"] == "B-1"
    finally:
        conn.close()


def test_init_db_fails_closed_on_preexisting_duplicate_running_publication_roots(tmp_path: Path) -> None:
    db_path = tmp_path / "unsafe.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript((Path(__file__).resolve().parents[1] / "migrations" / "init.sql").read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO bot_instances(bot_id, started_ts, stopped_ts, venue, symbol, bot_type, mode_json, params_json, state_json, status, origin_rec_id, publication_root_rec_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("B-old-1", 1, None, "linear", "BTCUSDT", "futures_grid", "{}", "{}", "{}", "running", "R-1", "R-root"),
    )
    conn.execute(
        "INSERT INTO bot_instances(bot_id, started_ts, stopped_ts, venue, symbol, bot_type, mode_json, params_json, state_json, status, origin_rec_id, publication_root_rec_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("B-old-2", 2, None, "linear", "BTCUSDT", "futures_grid", "{}", "{}", "{}", "running", "R-2", "R-root"),
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="Duplicate running bots detected"):
        db.init_db(conn)

    conn.close()
