from __future__ import annotations

import sqlite3
from pathlib import Path

from app import db


class _FlakyLockConn:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self.committed = False

    def execute(self, sql, params=()):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("database is locked")

        class _Cursor:
            rowcount = 1

        return _Cursor()

    def commit(self):
        self.committed = True


def test_runtime_lock_db_uses_sidecar_path(tmp_path: Path):
    db_path = tmp_path / "app.db"
    lock_path = Path(db.runtime_lock_db_path(str(db_path)))
    assert lock_path != db_path
    assert lock_path.name.endswith(".runtime_locks.sqlite")


def test_runtime_lock_heartbeat_not_blocked_by_main_db_write_lock(tmp_path: Path):
    db_path = tmp_path / "app.db"

    main_conn = db.connect(str(db_path))
    db.init_db(main_conn)

    lock_conn = db.connect_runtime_locks(str(db_path))
    db.init_runtime_lock_db(lock_conn)
    assert db.acquire_runtime_lock(lock_conn, "runtime:collector", "owner-1", ttl_sec=90)

    blocker = db.connect(str(db_path))
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)",
        ("blocking-write", "{}", db.now_ts()),
    )

    try:
        assert db.heartbeat_runtime_lock(lock_conn, "runtime:collector", "owner-1") is True
    finally:
        blocker.rollback()
        blocker.close()
        lock_conn.close()
        main_conn.close()



def test_runtime_lock_heartbeat_retries_transient_locked_errors():
    conn = _FlakyLockConn(failures=2)
    ok = db.heartbeat_runtime_lock(conn, "runtime:collector", "owner-1")
    assert ok is True
    assert conn.calls == 3
    assert conn.committed is True
