from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app import db
from app.outcomes import _get_first_tradeable_candle_after
from app.recommender import _estimate_cost_model
from app.risk import compute_risk_status, gate_candidate


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "test.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()



def _bot(
    *,
    bot_id: str,
    origin_rec_id: str | None,
    bot_type: str = "futures_grid",
    venue: str = "linear",
    symbol: str = "BTCUSDT",
    status: str = "running",
    state: dict | None = None,
):
    return {
        "bot_id": bot_id,
        "started_ts": int(time.time()),
        "stopped_ts": None,
        "venue": venue,
        "symbol": symbol,
        "bot_type": bot_type,
        "mode": {"direction": "long"},
        "params": {"grid_levels": 8},
        "state": state or {"marker": bot_id},
        "status": status,
        "origin_rec_id": origin_rec_id,
    }



def test_active_bot_queries_ignore_unsupported_legacy_bots(conn):
    assert db.insert_bot_instance(conn, _bot(bot_id="B-supported", origin_rec_id="R-supported")) == "inserted"
    assert db.insert_bot_instance(
        conn,
        _bot(bot_id="B-legacy", origin_rec_id="R-legacy", bot_type="legacy_grid"),
    ) == "inserted"

    active = db.get_active_bots(conn)
    assert [row["bot_id"] for row in active] == ["B-supported"]
    assert db.count_active_bots_for_symbol(conn, "linear", "BTCUSDT") == 1



def test_legacy_bots_do_not_pollute_risk_gates(conn, monkeypatch):
    monkeypatch.setenv("RISK_DAY_TZ", "UTC")
    assert db.insert_bot_instance(
        conn,
        _bot(bot_id="B-legacy", origin_rec_id="R-legacy", bot_type="legacy_grid"),
    ) == "inserted"

    limits = {
        "max_concurrent_bots": 1,
        "max_daily_dd_usdt": 1_000_000.0,
        "cooldown_after_loss_min": 0,
        "max_symbol_bots": 1,
    }

    rs = compute_risk_status(conn, limits)
    assert rs.active_bots == 0
    assert gate_candidate(conn, "linear", "BTCUSDT", limits) == []



def test_insert_bot_instance_is_idempotent_for_same_origin_rec(conn):
    first = _bot(bot_id="B-1", origin_rec_id="R-1", state={"marker": "first"})
    second = _bot(bot_id="B-2", origin_rec_id="R-1", state={"marker": "second"})

    assert db.insert_bot_instance(conn, first) == "inserted"
    assert db.insert_bot_instance(conn, second) == "duplicate_origin"

    rows = conn.execute(
        "SELECT bot_id, origin_rec_id, state_json FROM bot_instances ORDER BY started_ts ASC, bot_id ASC"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["bot_id"] == "B-1"
    assert json.loads(rows[0]["state_json"]) == {"marker": "first"}



def test_insert_bot_instance_detects_duplicate_bot_id(conn):
    bot = _bot(bot_id="B-dup", origin_rec_id="R-dup", state={"marker": "same"})
    assert db.insert_bot_instance(conn, bot) == "inserted"
    assert db.insert_bot_instance(conn, bot) == "duplicate_bot_id"

    changed = _bot(bot_id="B-dup", origin_rec_id="R-dup", state={"marker": "changed"})
    with pytest.raises(ValueError):
        db.insert_bot_instance(conn, changed)



def test_risk_status_uses_peak_to_trough_drawdown_and_trade_losses_for_cooldown(conn, monkeypatch):
    monkeypatch.setenv("RISK_DAY_TZ", "UTC")
    now = int(time.time())
    db.insert_trade(conn, {"trade_id": "T1", "bot_id": "B1", "ts": now - 120, "symbol": "BTCUSDT", "pnl": 300.0, "fee": 0.0, "meta": {}})
    db.insert_trade(conn, {"trade_id": "T2", "bot_id": "B1", "ts": now - 30, "symbol": "BTCUSDT", "pnl": -250.0, "fee": 0.0, "meta": {}})

    limits = {
        "max_concurrent_bots": 10,
        "max_daily_dd_usdt": 1_000_000.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 10,
    }
    rs = compute_risk_status(conn, limits)
    assert rs.daily_pnl == 50.0
    assert rs.daily_dd == 250.0
    assert rs.cooldown_active is True



def test_estimate_cost_model_rolls_stale_funding_forward_and_counts_crossed_events():
    now = int(time.time())
    base_args = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "f": {"spread_bps": 1.0},
        "taker_fee_bps": 6.0,
        "direction": "long",
        "funding_rate": 0.0005,
        "ts_now": now,
    }

    stale = _estimate_cost_model(next_funding_ts=now - 3600, **base_args)
    assert stale["next_funding_ts"] > now
    assert stale["expected_funding_events"] == 0
    assert stale["expected_funding_bps"] == 0.0

    imminent = _estimate_cost_model(next_funding_ts=now + 3600, **base_args)
    assert imminent["expected_funding_events"] == 1
    assert imminent["expected_funding_bps"] == pytest.approx(5.0)

    short = _estimate_cost_model(next_funding_ts=now + 3600, direction="short", **{k: v for k, v in base_args.items() if k != "direction"})
    assert short["expected_funding_bps"] == pytest.approx(-5.0)



def test_get_first_tradeable_candle_after_uses_next_candle_open(conn):
    base_ts = 1_700_000_000
    rows = [
        {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": base_ts + 0, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10.0},
        {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": base_ts + 60, "open": 101.5, "high": 103.0, "low": 100.0, "close": 102.0, "volume": 11.0},
        {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": base_ts + 120, "open": 102.5, "high": 104.0, "low": 101.0, "close": 103.0, "volume": 12.0},
    ]
    db.upsert_ohlcv(conn, rows)

    tradeable = _get_first_tradeable_candle_after(conn, "linear", "BTCUSDT", base_ts)
    assert tradeable == (base_ts + 60, 101.5)
