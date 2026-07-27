from __future__ import annotations

from types import SimpleNamespace

from websockets.exceptions import ConnectionClosedError

from app import main as main_module
from app.trade_stream import run_public_trade_stream_session


class _DisconnectingWebSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def send(self, payload: str) -> None:
        return None

    def recv(self, timeout: float):
        raise ConnectionClosedError(None, None)


def test_public_trade_network_disconnect_is_a_normal_session_end(tmp_path) -> None:
    from app import db

    conn = db.connect(str(tmp_path / "disconnect.db"))
    db.init_db(conn)
    stats = run_public_trade_stream_session(
        conn,
        bybit_http_base_url="https://api.bybit.com",
        symbols=["BTCUSDT"],
        stop_requested=lambda: False,
        connect_fn=lambda *args, **kwargs: _DisconnectingWebSocket(),
    )
    assert stats["disconnect_reason"] == "connection_closed"
    assert stats["messages"] == 0
    conn.close()


def test_public_trade_connection_uses_backpressure_tolerant_keepalive(tmp_path) -> None:
    from app import db

    captured: dict = {}

    class EmptyWebSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send(self, payload: str) -> None:
            return None

        def recv(self, timeout: float):
            return None

    def connect_fn(*args, **kwargs):
        captured.update(kwargs)
        return EmptyWebSocket()

    conn = db.connect(str(tmp_path / "connect-options.db"))
    db.init_db(conn)
    run_public_trade_stream_session(
        conn,
        bybit_http_base_url="https://api.bybit.com",
        symbols=["BTCUSDT"],
        stop_requested=lambda: False,
        connect_fn=connect_fn,
    )
    assert captured["ping_interval"] == 20
    assert captured["ping_timeout"] >= 30
    assert captured["max_queue"] >= 128
    assert captured["close_timeout"] <= 5
    conn.close()


def test_hot_collector_suppresses_rest_trade_poll_while_stream_is_active(monkeypatch) -> None:
    captured: dict = {}

    def fake_collect_once(*args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(main_module, "collect_once", fake_collect_once)
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(
            market_trade_journal_enabled=True,
            market_trade_stream_enabled=True,
            market_trade_poll_limit=1000,
            market_trade_retention_hours=72,
            funding_repair_max_per_cycle=16,
        ),
    )
    monkeypatch.setattr(
        main_module,
        "get_public_trade_stream_runtime_state",
        lambda: {"active": True},
        raising=False,
    )

    main_module._collect_hot_once(
        object(), object(), "linear", ["BTCUSDT"], lambda: True, 1
    )
    assert captured["market_trade_journal_enabled"] is False


def test_warmup_decision_events_are_transition_based() -> None:
    status = {
        "ready": False,
        "symbols_total": 35,
        "ready_symbols": 0,
        "ready_ratio": 0.0,
        "venues": [{
            "venue": "linear",
            "symbols_total": 35,
            "ready_symbols": 0,
            "reason_counts": {"candle_stale": 35},
            "sample_not_ready": [{
                "symbol": "BTCUSDT",
                "reasons": ["candle_stale"],
                "ticker_age_sec": 20,
                "candle_age_sec": 400,
            }],
        }],
    }
    state = None
    state, event = main_module._next_warmup_decision_event(
        state, status, now_ts=1000, cooldown_sec=120
    )
    assert event is not None
    assert event[0] == "RECO_WARMUP_SKIP"
    assert "sample_not_ready" not in str(event[1])

    state, duplicate = main_module._next_warmup_decision_event(
        state, status, now_ts=5000, cooldown_sec=120
    )
    assert duplicate is None

    recovered = dict(status)
    recovered["ready"] = True
    recovered["ready_symbols"] = 35
    recovered["ready_ratio"] = 1.0
    state, event = main_module._next_warmup_decision_event(
        state, recovered, now_ts=5001, cooldown_sec=120
    )
    assert event is not None
    assert event[0] == "RECO_WARMUP_RECOVERED"


def test_market_trade_background_loop_reconnects_without_supervisor_crash(monkeypatch) -> None:
    calls: list[int] = []

    class FakeConn:
        def close(self) -> None:
            return None

    def fake_session(*args, **kwargs):
        calls.append(len(calls) + 1)
        if len(calls) >= 2:
            main_module._BACKGROUND_STOP_EVENT.set()
        return {
            "session_id": f"s{len(calls)}",
            "messages": 0,
            "duration_sec": 0,
            "disconnect_reason": "connection_closed",
        }

    monkeypatch.setattr(main_module, "run_public_trade_stream_session", fake_session)
    monkeypatch.setattr(main_module, "_get_lock_conn", lambda: FakeConn())
    monkeypatch.setattr(main_module, "_get_conn", lambda: FakeConn())
    monkeypatch.setattr(main_module.db, "acquire_runtime_lock", lambda *a, **k: True)
    monkeypatch.setattr(main_module.db, "set_app_config_json", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "_make_runtime_lock_heartbeat", lambda *a, **k: (lambda: True))
    monkeypatch.setattr(main_module, "_set_background_thread_state", lambda *a, **k: None)
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(
            collect_interval_sec=20,
            bybit_base_url="https://api.bybit.com",
            symbols_linear=["BTCUSDT"],
            market_trade_stream_reconnect_min_sec=1,
            market_trade_stream_reconnect_max_sec=2,
            market_trade_stream_ping_interval_sec=20,
            market_trade_stream_ping_timeout_sec=60,
            market_trade_stream_close_timeout_sec=2,
            market_trade_stream_max_queue=256,
            market_trade_stream_commit_batch_messages=32,
            market_trade_stream_commit_batch_sec=0.5,
        ),
    )
    main_module._BACKGROUND_STOP_EVENT.clear()
    try:
        main_module._market_trade_stream_thread()
    finally:
        main_module._BACKGROUND_STOP_EVENT.clear()
    assert len(calls) == 2
