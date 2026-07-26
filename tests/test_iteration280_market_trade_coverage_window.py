from __future__ import annotations

import json
import time
from pathlib import Path

from app import db
from app.trade_stream import run_public_trade_stream_session


def _stream_row(*, trade_id: str, trade_ts_ms: int, message_ts_ms: int) -> dict:
    return {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "trade_id": trade_id,
        "trade_ts_ms": trade_ts_ms,
        "seq": 1,
        "side": "Buy",
        "price": 100.0,
        "qty": 0.1,
        "received_ts_ms": message_ts_ms + 1,
        "source": "websocket_public_trade_v1",
        "is_block_trade": False,
        "is_rpi_trade": False,
    }


def test_first_stream_batch_accepts_trade_at_envelope_timestamp(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "equal-ts.db"))
    db.init_db(conn)
    message_ts_ms = 10_000

    result = db.record_market_trade_stream_batch(
        conn,
        venue="linear",
        symbol="BTCUSDT",
        rows=[_stream_row(
            trade_id="equal-ts",
            trade_ts_ms=message_ts_ms,
            message_ts_ms=message_ts_ms,
        )],
        message_ts_ms=message_ts_ms,
        session_id="session-equal-ts",
    )

    assert result["coverage_start_ms"] == message_ts_ms + 1
    assert result["coverage_end_ms"] == message_ts_ms + 1
    coverage = conn.execute(
        "SELECT coverage_start_ms, coverage_end_ms, state FROM market_trade_coverage"
    ).fetchone()
    assert dict(coverage) == {
        "coverage_start_ms": message_ts_ms + 1,
        "coverage_end_ms": message_ts_ms + 1,
        "state": "open",
    }
    assert conn.execute("SELECT COUNT(*) AS c FROM market_trade").fetchone()["c"] == 1
    conn.close()


def test_live_session_does_not_crash_when_trade_time_equals_message_time(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "equal-ts-session.db"))
    db.init_db(conn)
    now_ms = int(time.time() * 1000)
    messages = [json.dumps({
        "topic": "publicTrade.BTCUSDT",
        "type": "snapshot",
        "ts": now_ms,
        "data": [{
            "T": now_ms,
            "s": "BTCUSDT",
            "S": "Buy",
            "v": "0.1",
            "p": "100.0",
            "i": "equal-ts-live",
            "BT": False,
            "RPI": False,
            "seq": 1,
        }],
    })]
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
                stopped["value"] = True
                return raw
            return None

    stats = run_public_trade_stream_session(
        conn,
        bybit_http_base_url="https://api.bybit.com",
        symbols=["BTCUSDT"],
        stop_requested=lambda: stopped["value"],
        connect_fn=lambda *args, **kwargs: FakeWebSocket(),
    )

    assert stats["messages"] == 1
    assert stats["trades"] == 1
    coverage = conn.execute(
        "SELECT coverage_start_ms, coverage_end_ms, state, gap_reason FROM market_trade_coverage"
    ).fetchone()
    assert dict(coverage) == {
        "coverage_start_ms": now_ms + 1,
        "coverage_end_ms": now_ms + 1,
        "state": "closed",
        "gap_reason": "stream_shutdown",
    }
    conn.close()
