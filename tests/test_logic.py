from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

from app import db
from app.outcomes import _get_first_tradeable_candle_after
from app.recommender import (
    _estimate_cost_model,
    _params,
    _score,
    _stable_range_score,
    _stabilize_direction_agg,
    PERSISTENCE_BOTS,
    run_recommender_once,
)
from app.settings import Settings
from app.risk import compute_risk_status, gate_candidate
from app.shock_guard import _stabilize_market_shock, _stabilize_fast_veto


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



def test_persistence_gate_enabled_for_grid_bots():
    assert {"spot_grid", "futures_grid"}.issubset(PERSISTENCE_BOTS)


def test_stable_range_score_prefers_multi_tf_context_over_noisy_1m_proxy():
    f = {"range_score": 0.18, "trend_strength": 0.70}
    agg = {"trendiness": 0.12, "coherence": 0.72, "regime": "range"}

    stable, meta = _stable_range_score(f, agg)

    assert meta["raw_range_score_1m"] == pytest.approx(0.18)
    assert meta["multi_tf_range_score"] > 0.85
    assert stable > 0.70


def test_score_uses_stable_range_score_to_avoid_false_no_trade_near_threshold():
    f = {
        "range_score": 0.18,
        "trend_strength": 0.70,
        "atr_pct": 0.008,
        "_atr_pct_1h": 0.008,
        "spread_bps": 1.2,
        "_direction_agg": {
            "direction": "neutral",
            "trendiness": 0.12,
            "coherence": 0.72,
            "regime": "range",
            "regime_confidence": 0.74,
            "strength": {"all": 0.08},
        },
    }
    cost_model = {"spread_bps": 1.2, "execution_cost_bps": 3.0, "total_cost_bps": 3.0, "net_cost_bps": 3.0}

    score, _, reasons = _score("futures_grid", "linear", f, taker_fee_bps=6.0, global_sent=0.0, cost_model=cost_model)

    assert score > 0.08
    assert reasons["score_components"]["range_score_meta"]["multi_tf_range_score"] > reasons["score_components"]["range_score_meta"]["raw_range_score_1m"]


def test_direction_hysteresis_holds_previous_direction_on_weak_flip():
    prev = {"ts": 1_700_000_000, "direction": "long", "bias": "long", "score_all": 0.19, "trendiness": 0.44, "coherence": 0.66}
    agg = {
        "direction": "short",
        "bias": "short",
        "scores": {"all": -0.13, "tactical": -0.14, "structural": -0.11},
        "strength": {"all": 0.13, "tactical": 0.14, "structural": 0.11},
        "trendiness": 0.36,
        "coherence": 0.54,
        "regime": "transition",
        "regime_confidence": 0.55,
    }

    stable, state = _stabilize_direction_agg(agg, prev_state=prev, now_ts=1_700_000_040, fresh_gap=120)

    assert stable["raw_direction"] == "short"
    assert stable["direction"] == "long"
    assert stable["direction_stability"]["applied"] is True
    assert stable["direction_stability"]["mode"] == "hysteresis_hold"
    assert state["direction"] == "long"


def test_market_shock_release_uses_cooldown():
    prev = {"ts": 1_700_000_000, "state": "red_down", "severity": "lockdown", "bias": "down"}
    raw = {"ts": 1_700_000_060, "state": "normal", "severity": "normal", "bias": "neutral", "reasons": []}

    stable = _stabilize_market_shock(raw, prev, now_ts=1_700_000_060, hold_sec=180)

    assert stable["raw_state"] == "normal"
    assert stable["state"] == "red_down"
    assert stable["stabilization"]["applied"] is True
    assert stable["stabilization"]["mode"] == "release_cooldown"


def test_fast_veto_release_uses_cooldown():
    prev = {"ts": 1_700_000_000, "state": "down_break", "triggered": True}
    raw = {"state": "normal", "triggered": False, "blocks": [], "metrics": {}}

    stable = _stabilize_fast_veto(raw, prev, now_ts=1_700_000_050, release_sec=120)

    assert stable["triggered"] is True
    assert stable["state"] == "down_break"
    assert stable["stabilization"]["applied"] is True
    assert stable["blocks"][0]["code"] == "FAST_VETO_RELEASE_COOLDOWN"



def _seed_ohlcv_wave(conn, *, venue: str, symbol: str, now_ts: int, tf_sec: int, n: int, base_price: float) -> None:
    rows = []
    start_ts = now_ts - tf_sec * (n + 2)
    for i in range(n):
        ts = start_ts + i * tf_sec
        mid = base_price * (1.0 + 0.0020 * math.sin(i / 8.0) + 0.0005 * math.sin(i / 3.0))
        open_px = mid * (1.0 + 0.0002 * math.sin(i))
        close_px = mid * (1.0 + 0.0002 * math.cos(i))
        high_px = max(open_px, close_px) * 1.0015
        low_px = min(open_px, close_px) * 0.9985
        rows.append({
            "venue": venue,
            "symbol": symbol,
            "tf_sec": tf_sec,
            "ts": ts,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": 1000.0 + i,
        })
    db.upsert_ohlcv(conn, rows)



def test_params_uses_direction_aggregate_range_score_without_name_error():
    f = {
        "price": 50_000.0,
        "atr_pct": 0.008,
        "range_score": 0.18,
        "trend_strength": 0.70,
        "_direction_agg": {
            "direction": "neutral",
            "trendiness": 0.12,
            "coherence": 0.72,
            "regime": "range",
            "regime_confidence": 0.74,
            "strength": {"all": 0.08},
        },
    }

    params = _params(
        "futures_grid",
        "linear",
        f,
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=6.0,
        direction_bias="neutral",
        direction_bias_strength=0.08,
        atr_pct_for_grid=0.008,
        cost_model={"execution_cost_bps": 6.0, "total_cost_bps": 6.0, "net_cost_bps": 6.0},
    )

    assert params["grid_levels"] >= 10
    assert params["price_range_upper"] > params["price_range_lower"]
    assert params["grid_spacing_pct"] > 0.0



def test_run_recommender_once_smoke_generates_recommendations_without_runtime_name_error(conn):
    now = int(time.time())
    symbol = "BTCUSDT"
    venue = "linear"
    base_price = 50_000.0

    for tf_sec, n in ((60, 220), (900, 120), (1800, 120), (3600, 120), (14_400, 100), (86_400, 100)):
        _seed_ohlcv_wave(conn, venue=venue, symbol=symbol, now_ts=now, tf_sec=tf_sec, n=n, base_price=base_price)

    db.insert_tickers(conn, [{
        "venue": venue,
        "symbol": symbol,
        "ts": now,
        "last": base_price,
        "bid": base_price - 5.0,
        "ask": base_price + 5.0,
        "vol24h": 12_345.0,
        "turnover24h": 5_000_000.0,
    }])
    db.upsert_funding_rate(conn, [{
        "symbol": symbol,
        "ts": now,
        "funding_rate": 0.0001,
        "next_funding_ts": now + 4 * 3600,
    }])

    settings = Settings(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=[venue],
        symbols_spot=[],
        symbols_linear=[symbol],
        risk_limits={"max_concurrent_bots": 4, "max_daily_dd_usdt": 200.0, "cooldown_after_loss_min": 30, "max_symbol_bots": 1},
        min_score_to_recommend=0.08,
        min_conf_to_recommend=0.52,
        taker_fee_bps_spot=10.0,
        taker_fee_bps_linear=6.0,
        master_key=None,
        admin_api_key=None,
        sentiment_interval_sec=60,
        futures_collect_interval_sec=900,
        telegram_token=None,
        telegram_chat_id=None,
        require_conf_gate=True,
    )

    result = run_recommender_once(conn, settings)

    assert result["count"] >= 1
    assert result["count_recommended"] + result["count_blocked"] + result["count_no_trade"] + result["count_suppressed"] == result["count"]
    rows = conn.execute("SELECT rec_id, status, params_json FROM recommendations ORDER BY ts DESC").fetchall()
    assert rows
    params = json.loads(rows[0]["params_json"])
    assert params["price_range_upper"] > params["price_range_lower"]
    assert params["grid_levels"] >= 4
