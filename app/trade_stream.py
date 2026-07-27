from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as websocket_connect

from . import db
from .grid_math import strict_integer

PUBLIC_TRADE_STREAM_SOURCE = "websocket_public_trade_v1"

_PUBLIC_TRADE_STREAM_STATE_LOCK = threading.Lock()
_PUBLIC_TRADE_STREAM_STATE: dict[str, Any] = {
    "active": False,
    "session_id": None,
    "connected_ts": None,
    "last_message_ts_ms": None,
    "last_disconnect_ts": None,
    "disconnect_reason": None,
}


def _set_public_trade_stream_runtime_state(**fields: Any) -> None:
    with _PUBLIC_TRADE_STREAM_STATE_LOCK:
        _PUBLIC_TRADE_STREAM_STATE.update(fields)


def get_public_trade_stream_runtime_state() -> dict[str, Any]:
    with _PUBLIC_TRADE_STREAM_STATE_LOCK:
        return dict(_PUBLIC_TRADE_STREAM_STATE)


def public_linear_trade_ws_url(bybit_http_base_url: str) -> str:
    """Map known Bybit public HTTP hosts to the public Linear WebSocket endpoint.

    A custom HTTP proxy is intentionally not converted into an arbitrary WebSocket
    target.  This keeps the background stream from becoming a configurable SSRF
    primitive. Deployments using a proxy may disable the stream and retain the
    overlap-verified REST fallback.
    """
    parsed = urlparse(str(bybit_http_base_url or "").strip())
    host = str(parsed.hostname or "").strip().lower()
    if host in {"api-testnet.bybit.com", "api-testnet.bytick.com"}:
        return "wss://stream-testnet.bybit.com/v5/public/linear"
    if host in {
        "api.bybit.com",
        "api.bytick.com",
        "api-demo.bybit.com",
        "api-demo.bytick.com",
    }:
        return "wss://stream.bybit.com/v5/public/linear"
    raise ValueError("unsupported Bybit HTTP host for public trade WebSocket")


def _finite_positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def parse_public_trade_message(
    raw_message: str | bytes | dict[str, Any],
    *,
    received_ts_ms: int | None = None,
) -> dict[str, Any] | None:
    """Parse one Bybit ``publicTrade.{symbol}`` message fail-closed.

    Subscription acknowledgements and pong messages return ``None``. A malformed
    public-trade payload raises ``ValueError`` so the caller closes the current
    coverage span and reconnects instead of silently dropping an unknown trade.
    """
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8", errors="strict")
    if isinstance(raw_message, str):
        try:
            payload = json.loads(raw_message)
        except Exception as exc:
            raise ValueError("public trade WebSocket returned invalid JSON") from exc
    elif isinstance(raw_message, dict):
        payload = dict(raw_message)
    else:
        raise ValueError("public trade WebSocket returned unsupported message type")

    topic = str(payload.get("topic") or "").strip()
    if not topic:
        # subscribe/pong/control response
        return None
    if not topic.startswith("publicTrade."):
        return None
    symbol_from_topic = topic.split(".", 1)[1].strip().upper()
    message_ts_ms = strict_integer(payload.get("ts"))
    data = payload.get("data")
    if (
        not symbol_from_topic.endswith("USDT")
        or message_ts_ms is None
        or message_ts_ms <= 0
        or not isinstance(data, list)
        or not data
    ):
        raise ValueError("malformed public trade WebSocket envelope")
    received = strict_integer(received_ts_ms)
    if received is None or received <= 0:
        received = int(time.time() * 1000)
    if received + 30_000 < int(message_ts_ms):
        raise ValueError("public trade WebSocket message timestamp is too far in the future")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    previous_trade_ts_ms: int | None = None
    for row_index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("malformed public trade WebSocket row")
        symbol = str(item.get("s") or "").strip().upper()
        trade_id = str(item.get("i") or "").strip()
        trade_ts_ms = strict_integer(item.get("T"))
        seq = strict_integer(item.get("seq"))
        side = str(item.get("S") or "").strip().capitalize()
        price = _finite_positive(item.get("p"))
        qty = _finite_positive(item.get("v"))
        if (
            symbol != symbol_from_topic
            or not trade_id
            or trade_id in seen_ids
            or trade_ts_ms is None
            or trade_ts_ms <= 0
            or trade_ts_ms > int(message_ts_ms)
            or seq is None
            or seq < 0
            or side not in {"Buy", "Sell"}
            or price is None
            or qty is None
            or not isinstance(item.get("BT"), bool)
            or (item.get("RPI") is not None and not isinstance(item.get("RPI"), bool))
        ):
            raise ValueError("malformed public trade WebSocket row")
        # Bybit documents only that ``data`` is sorted by match time ``T``.
        # ``seq`` may be shared by multiple trades/messages and trade IDs are
        # opaque identifiers, so neither field is a valid tie-breaker. Preserve
        # the delivered row order for equal-millisecond trades and fail only when
        # the documented match timestamp itself goes backwards.
        if previous_trade_ts_ms is not None and int(trade_ts_ms) < previous_trade_ts_ms:
            raise ValueError("non-monotonic public trade WebSocket match timestamp")
        previous_trade_ts_ms = int(trade_ts_ms)
        seen_ids.add(trade_id)
        rows.append({
            "venue": "linear",
            "symbol": symbol,
            "trade_id": trade_id,
            "trade_ts_ms": int(trade_ts_ms),
            "seq": int(seq),
            "side": side,
            "price": float(price),
            "qty": float(qty),
            "received_ts_ms": int(received),
            "source": PUBLIC_TRADE_STREAM_SOURCE,
            "is_block_trade": item.get("BT") is True,
            "is_rpi_trade": item.get("RPI") is True,
            "stream_row_index": int(row_index),
        })
    return {
        "symbol": symbol_from_topic,
        "message_ts_ms": int(message_ts_ms),
        "received_ts_ms": int(received),
        "rows": rows,
    }


def run_public_trade_stream_session(
    conn,
    *,
    bybit_http_base_url: str,
    symbols: list[str],
    stop_requested: Callable[[], bool],
    heartbeat: Callable[[], bool] | None = None,
    connect_fn=websocket_connect,
    receive_timeout_sec: float = 1.0,
    ping_interval_sec: float = 20.0,
    ping_timeout_sec: float = 60.0,
    close_timeout_sec: float = 2.0,
    max_queue_messages: int = 256,
    commit_batch_messages: int = 32,
    commit_batch_sec: float = 0.5,
    max_session_sec: float | None = None,
) -> dict[str, Any]:
    """Run one public-trade WebSocket session until stop or disconnect.

    Network closures and heartbeat timeouts are normal transport events. The
    client disables the library's protocol keepalive and uses Bybit's documented
    JSON ping/pong heartbeat with an explicit receive watchdog. Disconnects close
    current coverage spans and return session statistics so the owning loop can
    reconnect without reporting a crashed worker. Malformed payloads and
    persistence errors still propagate fail-closed.
    """
    normalized_symbols = sorted({
        str(symbol or "").strip().upper()
        for symbol in symbols
        if str(symbol or "").strip().upper().endswith("USDT")
    })
    if not normalized_symbols:
        raise ValueError("public trade stream requires Linear USDT symbols")
    url = public_linear_trade_ws_url(bybit_http_base_url)
    session_id = uuid.uuid4().hex
    coverage_ids: dict[str, str] = {}
    stats: dict[str, Any] = {
        "session_id": session_id,
        "url_host": urlparse(url).hostname,
        "symbols": normalized_symbols,
        "messages": 0,
        "trades": 0,
        "inserted": 0,
        "commits": 0,
        "application_pings": 0,
        "started_ts": int(time.time()),
        "last_message_ts_ms": None,
        "disconnect_reason": None,
        "disconnect_error_type": None,
    }
    close_reason = "websocket_disconnect"
    message_index = 0
    pending_messages = 0
    session_started_monotonic = time.monotonic()
    last_commit_monotonic = session_started_monotonic
    last_application_ping_monotonic = session_started_monotonic
    last_receive_monotonic = session_started_monotonic

    def _commit_pending() -> None:
        nonlocal pending_messages, last_commit_monotonic
        if pending_messages <= 0:
            return
        conn.commit()
        stats["commits"] = int(stats["commits"]) + 1
        pending_messages = 0
        last_commit_monotonic = time.monotonic()

    try:
        try:
            with connect_fn(
                url,
                open_timeout=10,
                close_timeout=max(0.1, float(close_timeout_sec)),
                # Bybit requires its JSON application heartbeat. Running the
                # websockets protocol keepalive in parallel creates a second
                # timer thread that can emit noisy internal tracebacks during
                # local DB stalls. Disable it and supervise the documented
                # application ping/pong path below.
                ping_interval=None,
                ping_timeout=None,
                max_size=16 * 1024 * 1024,
                max_queue=max(128, int(max_queue_messages)),
            ) as websocket:
                _set_public_trade_stream_runtime_state(
                    active=True,
                    session_id=session_id,
                    connected_ts=int(time.time()),
                    last_message_ts_ms=None,
                    disconnect_reason=None,
                )
                websocket.send(json.dumps({
                    "op": "subscribe",
                    "args": [f"publicTrade.{symbol}" for symbol in normalized_symbols],
                }, separators=(",", ":")))
                while not stop_requested():
                    if max_session_sec is not None and time.monotonic() - session_started_monotonic >= max(10.0, float(max_session_sec)):
                        close_reason = "capture_scope_refresh"
                        stats["disconnect_reason"] = close_reason
                        break
                    if (
                        time.monotonic() - last_application_ping_monotonic
                        >= max(5.0, float(ping_interval_sec))
                    ):
                        websocket.send(json.dumps({"op": "ping"}, separators=(",", ":")))
                        stats["application_pings"] = int(stats["application_pings"]) + 1
                        last_application_ping_monotonic = time.monotonic()
                    if heartbeat is not None and not heartbeat():
                        close_reason = "runtime_lock_lost"
                        stats["disconnect_reason"] = close_reason
                        break
                    try:
                        raw = websocket.recv(timeout=max(0.1, float(receive_timeout_sec)))
                    except TimeoutError:
                        now_monotonic = time.monotonic()
                        if (
                            now_monotonic - last_receive_monotonic
                            >= max(5.0, float(ping_timeout_sec))
                        ):
                            close_reason = "application_heartbeat_timeout"
                            stats["disconnect_reason"] = close_reason
                            stats["disconnect_error_type"] = "TimeoutError"
                            break
                        if pending_messages and (
                            now_monotonic - last_commit_monotonic
                            >= max(0.05, float(commit_batch_sec))
                        ):
                            _commit_pending()
                        continue
                    if raw is None:
                        close_reason = "websocket_disconnect"
                        stats["disconnect_reason"] = close_reason
                        break
                    last_receive_monotonic = time.monotonic()
                    parsed = parse_public_trade_message(raw)
                    if parsed is None:
                        continue
                    symbol = str(parsed["symbol"])
                    message_index += 1
                    stream_rows: list[dict[str, Any]] = []
                    for row in parsed["rows"]:
                        enriched = dict(row)
                        enriched["stream_session_id"] = session_id
                        enriched["stream_message_index"] = int(message_index)
                        enriched["stream_message_ts_ms"] = int(parsed["message_ts_ms"])
                        stream_rows.append(enriched)
                    try:
                        result = db.record_market_trade_stream_batch(
                            conn,
                            venue="linear",
                            symbol=symbol,
                            rows=stream_rows,
                            message_ts_ms=int(parsed["message_ts_ms"]),
                            session_id=session_id,
                            coverage_id=coverage_ids.get(symbol),
                            commit=False,
                        )
                    except Exception:
                        conn.rollback()
                        raise
                    coverage_id = str(result.get("coverage_id") or "")
                    if coverage_id:
                        coverage_ids[symbol] = coverage_id
                    stats["messages"] = int(stats["messages"]) + 1
                    stats["trades"] = int(stats["trades"]) + len(parsed["rows"])
                    stats["inserted"] = int(stats["inserted"]) + int(result.get("inserted") or 0)
                    prior_message_ts = stats.get("last_message_ts_ms")
                    stats["last_message_ts_ms"] = max(
                        int(prior_message_ts) if prior_message_ts is not None else 0,
                        int(parsed["message_ts_ms"]),
                    )
                    _set_public_trade_stream_runtime_state(
                        active=True,
                        session_id=session_id,
                        last_message_ts_ms=int(stats["last_message_ts_ms"]),
                    )
                    pending_messages += 1
                    if (
                        pending_messages >= max(1, int(commit_batch_messages))
                        or time.monotonic() - last_commit_monotonic
                        >= max(0.05, float(commit_batch_sec))
                    ):
                        _commit_pending()
                if stop_requested():
                    close_reason = "stream_shutdown"
                    stats["disconnect_reason"] = close_reason
        except ConnectionClosed as exc:
            close_reason = "connection_closed"
            stats["disconnect_reason"] = close_reason
            stats["disconnect_error_type"] = exc.__class__.__name__
        except (TimeoutError, OSError) as exc:
            close_reason = "transport_timeout" if isinstance(exc, TimeoutError) else "transport_error"
            stats["disconnect_reason"] = close_reason
            stats["disconnect_error_type"] = exc.__class__.__name__
    finally:
        try:
            _commit_pending()
        except Exception:
            conn.rollback()
            raise
        close_error: Exception | None = None
        for coverage_id in coverage_ids.values():
            try:
                db.close_market_trade_coverage(
                    conn,
                    coverage_id,
                    gap_reason=close_reason,
                    commit=False,
                )
            except Exception as exc:
                close_error = close_error or exc
        if coverage_ids:
            if close_error is None:
                conn.commit()
            else:
                conn.rollback()
        _set_public_trade_stream_runtime_state(
            active=False,
            session_id=None,
            last_disconnect_ts=int(time.time()),
            disconnect_reason=close_reason,
        )
        if close_error is not None:
            raise close_error
    if stats.get("disconnect_reason") is None:
        stats["disconnect_reason"] = close_reason
    stats["ended_ts"] = int(time.time())
    stats["duration_sec"] = max(0, int(stats["ended_ts"]) - int(stats["started_ts"]))
    return stats

