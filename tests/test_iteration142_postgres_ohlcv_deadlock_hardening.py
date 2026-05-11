from __future__ import annotations

import sqlite3

from app import db


class _CaptureExecutemanyConn:
    def __init__(self):
        self.params = None
        self.commits = 0

    def executemany(self, sql, params):
        self.sql = sql
        self.params = list(params)

        class _Cursor:
            rowcount = 1

        return _Cursor()

    def commit(self):
        self.commits += 1


class _FlakyOhlcvConn:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self.commits = 0
        self.rollbacks = 0

    def executemany(self, sql, params):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("обнаружена взаимоблокировка")

        class _Cursor:
            rowcount = 1

        return _Cursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _row(symbol: str, ts: int, close: float) -> dict:
    return {
        "venue": "linear",
        "symbol": symbol,
        "tf_sec": 60,
        "ts": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def test_upsert_ohlcv_uses_deterministic_pk_order_and_dedupes_batch_keys() -> None:
    conn = _CaptureExecutemanyConn()

    db.upsert_ohlcv(
        conn,
        [
            _row("ETHUSDT", 200, 2.0),
            _row("BTCUSDT", 100, 1.0),
            _row("BTCUSDT", 100, 1.5),
        ],
        commit=False,
    )

    assert conn.params == [
        ("linear", "BTCUSDT", 60, 100, 1.5, 1.5, 1.5, 1.5, 1.0),
        ("linear", "ETHUSDT", 60, 200, 2.0, 2.0, 2.0, 2.0, 1.0),
    ]
    assert conn.commits == 0


def test_upsert_ohlcv_retries_localized_postgres_deadlock_text_on_commit() -> None:
    conn = _FlakyOhlcvConn(failures=2)

    db.upsert_ohlcv(conn, [_row("BTCUSDT", 100, 1.0)], commit=True)

    assert conn.calls == 3
    assert conn.rollbacks == 2
    assert conn.commits == 1


def test_lock_retryable_error_accepts_postgres_sqlstate_deadlock() -> None:
    class _Diag:
        sqlstate = "40P01"

    class _Exc(Exception):
        diag = _Diag()

    assert db._is_lock_retryable_error(_Exc("localized database message")) is True
