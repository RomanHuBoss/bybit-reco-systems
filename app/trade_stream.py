from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from websockets.sync.client import connect as websocket_connect

from . import db
from .grid_math import strict_integer

PUBLIC_TRADE_STREAM_SOURCE = "websocket_public_trade_v1"


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
    previous_key: tuple[int, int, str] | None = None
    for item in data:
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
        key = (int(trade_ts_ms), int(seq), trade_id)
        if previous_key is not None and key < previous_key:
            raise ValueError("non-monotonic public trade WebSocket row order")
        previous_key = key
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
) -> dict[str, Any]:
    """Run one public-trade WebSocket session until stop or disconnect.

    The supervising background wrapper is responsible for reconnect backoff. Each
    session creates separate per-symbol coverage spans; every exit closes them so
    no chronology is claimed across a reconnect.
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
    stats = {
        "session_id": session_id,
        "url_host": urlparse(url).hostname,
        "symbols": normalized_symbols,
        "messages": 0,
        "trades": 0,
        "inserted": 0,
        "started_ts": int(time.time()),
        "last_message_ts_ms": None,
    }
    close_reason = "websocket_disconnect"
    try:
        with connect_fn(
            url,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=10,
            max_size=16 * 1024 * 1024,
        ) as websocket:
            websocket.send(json.dumps({
                "op": "subscribe",
                "args": [f"publicTrade.{symbol}" for symbol in normalized_symbols],
            }, separators=(",", ":")))
            while not stop_requested():
                if heartbeat is not None and not heartbeat():
                    close_reason = "runtime_lock_lost"
                    raise RuntimeError("market trade stream runtime lock lost")
                try:
                    raw = websocket.recv(timeout=max(0.1, float(receive_timeout_sec)))
                except TimeoutError:
                    continue
                if raw is None:
                    break
                parsed = parse_public_trade_message(raw)
                if parsed is None:
                    continue
                symbol = str(parsed["symbol"])
                result = db.record_market_trade_stream_batch(
                    conn,
                    venue="linear",
                    symbol=symbol,
                    rows=list(parsed["rows"]),
                    message_ts_ms=int(parsed["message_ts_ms"]),
                    session_id=session_id,
                    coverage_id=coverage_ids.get(symbol),
                    commit=True,
                )
                coverage_id = str(result.get("coverage_id") or "")
                if coverage_id:
                    coverage_ids[symbol] = coverage_id
                stats["messages"] = int(stats["messages"]) + 1
                stats["trades"] = int(stats["trades"]) + len(parsed["rows"])
                stats["inserted"] = int(stats["inserted"]) + int(result.get("inserted") or 0)
                stats["last_message_ts_ms"] = int(parsed["message_ts_ms"])
            close_reason = "stream_shutdown" if stop_requested() else "websocket_disconnect"
    finally:
        for coverage_id in coverage_ids.values():
            db.close_market_trade_coverage(
                conn,
                coverage_id,
                gap_reason=close_reason,
                commit=False,
            )
        if coverage_ids:
            conn.commit()
    return stats
