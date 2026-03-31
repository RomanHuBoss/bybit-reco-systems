from __future__ import annotations

import importlib
from contextlib import closing
import sqlite3
import sys
from pathlib import Path

import pytest

from app import db


def test_make_runtime_lock_heartbeat_survives_closed_outer_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "hb_reopen.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        with closing(db.connect_runtime_locks(str(db_path))) as conn:
            db.init_runtime_lock_db(conn)
            assert db.acquire_runtime_lock(conn, "runtime:test", app_main.RUNTIME_OWNER, ttl_sec=90)

        heartbeat = app_main._make_runtime_lock_heartbeat("runtime:test")
        assert heartbeat() is True
        assert heartbeat() is True
    finally:
        sys.modules.pop("app.main", None)


def test_make_runtime_lock_heartbeat_opens_new_connection_each_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "hb_factory.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        created = []

        class _ConnWrapper:
            def __init__(self, inner):
                self.inner = inner
                self.closed = False
            def execute(self, *args, **kwargs):
                return self.inner.execute(*args, **kwargs)
            def commit(self):
                return self.inner.commit()
            def rollback(self):
                return self.inner.rollback()
            def close(self):
                self.closed = True
                return self.inner.close()

        def factory():
            wrapped = _ConnWrapper(db.connect_runtime_locks(str(db_path)))
            created.append(wrapped)
            return wrapped

        with closing(db.connect_runtime_locks(str(db_path))) as conn:
            db.init_runtime_lock_db(conn)
            assert db.acquire_runtime_lock(conn, "runtime:test", app_main.RUNTIME_OWNER, ttl_sec=90)

        heartbeat = app_main._make_runtime_lock_heartbeat("runtime:test", lock_conn_factory=factory)
        assert heartbeat() is True
        assert heartbeat() is True

        assert len(created) == 2
        assert all(conn.closed for conn in created)
    finally:
        sys.modules.pop("app.main", None)
