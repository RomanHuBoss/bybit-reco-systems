from __future__ import annotations

import json
from pathlib import Path

from app import db
from app.trade_stream import parse_public_trade_message, run_public_trade_stream_session


def _same_millisecond_message(now_ms: int = 1_800_000_000_000) -> dict:
    return {
        "topic": "publicTrade.BTCUSDT",
        "ts": now_ms,
        "data": [
            {
                "T": now_ms - 5,
                "s": "BTCUSDT",
                "S": "Buy",
                "v": "0.1",
                "p": "101.0",
                "i": "z-trade",
                "BT": False,
                "RPI": False,
                "seq": 42,
            },
            {
                "T": now_ms - 5,
                "s": "BTCUSDT",
                "S": "Sell",
                "v": "0.2",
                "p": "99.0",
                "i": "a-trade",
                "BT": False,
                "RPI": False,
                "seq": 42,
            },
        ],
    }


def test_public_trade_parser_accepts_same_timestamp_rows_without_trade_id_sorting() -> None:
    parsed = parse_public_trade_message(
        _same_millisecond_message(), received_ts_ms=1_800_000_000_010
    )
    assert parsed is not None
    assert [row["trade_id"] for row in parsed["rows"]] == ["z-trade", "a-trade"]
    assert [row["stream_row_index"] for row in parsed["rows"]] == [0, 1]


def test_public_trade_stream_persists_delivered_row_order_for_equal_timestamps(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "stream-order.db"))
    db.init_db(conn)
    import time

    target_second_ms = (int(time.time() * 1000) // 1000 + 1) * 1000
    warmup = {
        "topic": "publicTrade.BTCUSDT",
        "ts": target_second_ms - 1000,
        "data": [{
            "T": target_second_ms - 1100,
            "s": "BTCUSDT", "S": "Buy", "v": "0.1", "p": "100.0",
            "i": "warmup", "BT": False, "RPI": False, "seq": 41,
        }],
    }
    target = _same_millisecond_message(target_second_ms + 1000)
    for item in target["data"]:
        item["T"] = target_second_ms + 100
    messages = [json.dumps(warmup), json.dumps(target)]
    stopped = {"value": False}

    class FakeWebSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send(self, payload: str) -> None:
            assert json.loads(payload)["op"] == "subscribe"

        def recv(self, timeout: float):
            if messages:
                raw = messages.pop(0)
                if not messages:
                    stopped["value"] = True
                return raw
            return None

    result = run_public_trade_stream_session(
        conn,
        bybit_http_base_url="https://api.bybit.com",
        symbols=["BTCUSDT"],
        stop_requested=lambda: stopped["value"],
        connect_fn=lambda *args, **kwargs: FakeWebSocket(),
    )
    assert result["messages"] == 2
    coverage = conn.execute(
        "SELECT coverage_start_ms, coverage_end_ms FROM market_trade_coverage WHERE source=?",
        ("websocket_public_trade_v1",),
    ).fetchone()
    assert coverage is not None
    path = db.get_market_trade_path(
        conn,
        "linear",
        "BTCUSDT",
        target_second_ms // 1000,
        target_second_ms // 1000 + 1,
    )
    assert path is not None
    assert path["ordering_basis"] == "websocket_delivery_order_v1"
    assert [row["trade_id"] for row in path["items"]] == ["z-trade", "a-trade"]
    conn.close()


def test_market_trade_stream_order_columns_upgrade_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    conn.execute("DROP INDEX IF EXISTS idx_market_trade_stream_order")
    # Simulate the 1.4.8 table shape on an existing database.
    conn.execute("ALTER TABLE market_trade RENAME TO market_trade_new_shape")
    conn.execute(
        """CREATE TABLE market_trade (
          venue TEXT NOT NULL, symbol TEXT NOT NULL, trade_id TEXT NOT NULL,
          trade_ts_ms INTEGER NOT NULL, seq INTEGER, side TEXT NOT NULL,
          price REAL NOT NULL, qty REAL NOT NULL, received_ts_ms INTEGER NOT NULL,
          source TEXT NOT NULL, is_block_trade INTEGER NOT NULL DEFAULT 0,
          is_rpi_trade INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (venue, symbol, trade_id)
        )"""
    )
    conn.execute("DROP TABLE market_trade_new_shape")
    conn.commit()
    db.init_db(conn)
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(market_trade)")}
    assert {
        "stream_session_id",
        "stream_message_index",
        "stream_row_index",
        "stream_message_ts_ms",
    } <= columns
    conn.close()


def test_stream_message_timestamp_regression_does_not_crash_or_shrink_coverage(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "message-ts.db"))
    db.init_db(conn)

    def row(trade_id: str, trade_ts_ms: int) -> dict:
        return {
            "venue": "linear",
            "symbol": "BTCUSDT",
            "trade_id": trade_id,
            "trade_ts_ms": trade_ts_ms,
            "seq": 1,
            "side": "Buy",
            "price": 100.0,
            "qty": 1.0,
            "received_ts_ms": 20_000,
            "source": "websocket_public_trade_v1",
            "is_block_trade": False,
            "is_rpi_trade": False,
        }

    first = db.record_market_trade_stream_batch(
        conn,
        venue="linear",
        symbol="BTCUSDT",
        rows=[row("first", 10_000)],
        message_ts_ms=10_100,
        session_id="session",
    )
    second = db.record_market_trade_stream_batch(
        conn,
        venue="linear",
        symbol="BTCUSDT",
        rows=[row("second", 10_020)],
        message_ts_ms=10_050,
        session_id="session",
        coverage_id=first["coverage_id"],
    )
    assert second["coverage_end_ms"] == 10_100
    coverage = conn.execute(
        "SELECT coverage_end_ms, last_poll_ts_ms FROM market_trade_coverage WHERE coverage_id=?",
        (first["coverage_id"],),
    ).fetchone()
    assert coverage is not None
    assert int(coverage["coverage_end_ms"]) == 10_100
    assert int(coverage["last_poll_ts_ms"]) == 10_100
    conn.close()
