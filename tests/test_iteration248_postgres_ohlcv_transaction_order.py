from __future__ import annotations

import sqlite3
from typing import Any

from app import collector, db


class _DeadlockOnceConnection:
    """Minimal DB-API connection that mimics a PostgreSQL deadlock victim."""

    def __init__(self) -> None:
        self.executemany_calls = 0
        self.commits = 0
        self.rollbacks = 0
        self.successful_batches: list[list[tuple[Any, ...]]] = []

    def executemany(self, _sql: str, params):
        self.executemany_calls += 1
        batch = list(params)
        if self.executemany_calls == 1:
            raise sqlite3.OperationalError("обнаружена взаимоблокировка")
        self.successful_batches.append(batch)

        class _Cursor:
            rowcount = len(batch)

        return _Cursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _row(symbol: str, tf_sec: int, ts: int) -> dict[str, Any]:
    price = 100.0 + tf_sec / 1000.0
    return {
        "venue": "linear",
        "symbol": symbol,
        "tf_sec": tf_sec,
        "ts": ts,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 1.0,
    }


def test_hot_collector_retries_deadlock_and_orders_each_derived_transaction(monkeypatch) -> None:
    conn = _DeadlockOnceConnection()
    symbols = ["ETHUSDT", "BTCUSDT"]

    monkeypatch.setattr(db, "now_ts", lambda: 1_800_000_000)
    monkeypatch.setattr(
        collector,
        "_fetch_ticker_payloads",
        lambda *_args, **_kwargs: ([], [], []),
    )
    monkeypatch.setattr(collector, "_should_fetch_api_tf", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(db, "get_latest_ohlcv_ts", lambda *_args, **_kwargs: None)

    class _Client:
        def get_kline(self, **_kwargs):
            return [["1799999940000", "100", "100", "100", "100", "1"]]

    monkeypatch.setattr(
        collector,
        "_derive_local_tf_rows",
        lambda _conn, _venue, symbol, _source_tf, target_tf: [
            _row(symbol, target_tf, 1_799_985_600 - (1_799_985_600 % target_tf))
        ],
    )

    stats = collector.collect_once(
        conn,
        client=_Client(),
        venue="linear",
        symbols=symbols,
        max_workers=1,
        api_fetch_tfs=(60,),
        allow_derived_bootstrap=False,
    )

    assert conn.rollbacks == 1
    assert conn.executemany_calls == 4  # one retry + one transaction per derived TF
    assert stats["derived_tf_writes"] == {"900": 2, "1800": 2}
    for batch in conn.successful_batches:
        keys = [(row[0], row[1], row[2], row[3]) for row in batch]
        assert keys == sorted(keys)
        assert {row[1] for row in batch} == {"BTCUSDT", "ETHUSDT"}


def test_backfill_bootstrap_retries_deadlock_as_one_canonical_transaction(monkeypatch) -> None:
    conn = _DeadlockOnceConnection()
    symbols = ["ETHUSDT", "BTCUSDT"]

    monkeypatch.setattr(db, "now_ts", lambda: 1_800_000_000)
    monkeypatch.setattr(
        collector,
        "_api_tf_fetch_state",
        lambda *_args, **_kwargs: (False, None),
    )
    monkeypatch.setattr(collector, "_should_bootstrap_derived_tf", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        collector,
        "_bootstrap_derived_tf_from_api",
        lambda _client, _venue, _category, symbol, target_tf: [
            _row(symbol, target_tf, 1_799_985_600 - (1_799_985_600 % target_tf))
        ],
    )
    monkeypatch.setattr(collector, "_derive_local_tf_rows", lambda *_args, **_kwargs: [])

    stats = collector.collect_backfill_once(
        conn,
        client=object(),
        venue="linear",
        symbols=symbols,
        max_workers=1,
        per_tf_budget=10,
    )

    assert conn.rollbacks == 1
    assert conn.executemany_calls == 2
    assert len(conn.successful_batches) == 1
    batch = conn.successful_batches[0]
    keys = [(row[0], row[1], row[2], row[3]) for row in batch]
    assert keys == sorted(keys)
    assert len(keys) == 6
    assert stats["derived_tf_bootstrap_fetches"] == {"900": 2, "1800": 2, "14400": 2}
