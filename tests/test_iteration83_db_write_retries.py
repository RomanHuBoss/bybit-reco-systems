from __future__ import annotations

import sqlite3

from app import db


class _FlakyWriteConn:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("database is locked")

        class _Cursor:
            rowcount = 1

        return _Cursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_set_app_config_json_retries_transient_locked_errors():
    conn = _FlakyWriteConn(failures=2)
    db.set_app_config_json(conn, "llm_review_async_status", {"enabled": True})
    assert conn.calls == 3
    assert conn.commits == 1
    assert conn.rollbacks == 2



def test_log_decision_retries_transient_locked_errors():
    conn = _FlakyWriteConn(failures=2)
    db.log_decision(conn, "LLM_REVIEW_OK", "rec-1", None, {"source": "async"})
    assert conn.calls == 3
    assert conn.commits == 1
    assert conn.rollbacks == 2
