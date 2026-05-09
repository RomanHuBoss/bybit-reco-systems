from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

from app import db


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration125.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration125_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _meta(**overrides) -> dict[str, str]:
    meta = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "max_order_qty": "100",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }
    meta.update(overrides)
    return meta


def _base_rec(*, symbol: str = "BTCUSDT", direction: str = "long", net_profit_bps: float = 5.0) -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": symbol,
        "direction": direction,
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_count": 5,
            "grid_levels": 5,
            "grid_type": "arithmetic",
            "leverage": 1,
            "economics": {"net_profit_bps": net_profit_bps},
            "cost_model": {"expected_funding_bps": 0.0, "horizon_sec": 12 * 3600},
            "trade_plan": {
                "reference_price": 100.0,
                "sizing": {"order_qty": 0.051, "order_notional_usdt": 5.1},
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.5},
                    "tp_per_leg": {"abs": 0.3, "pct": 0.3},
                },
            },
        },
    }


def _codes(validation: dict, key: str = "errors") -> set[str]:
    return {str(item.get("code")) for item in validation.get(key, [])}


def test_bybit_preflight_rejects_malformed_legacy_symbols_even_without_meta(app_main):
    rec = _base_rec(symbol="BTC/USDT")

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, {}, require_meta=False)

    assert validation["ok"] is False
    assert "USDT_PERPETUAL_SYMBOL_REQUIRED" in _codes(validation)


def test_bybit_preflight_blocks_string_true_prelisting_flag(app_main):
    rec = _base_rec()
    meta = _meta(is_pre_listing="true")

    validation = app_main._validate_trade_plan_against_bybit_meta(rec, meta, require_meta=True)

    assert validation["ok"] is False
    assert "BYBIT_PRELISTING_UNSUPPORTED" in _codes(validation)


def test_execution_preflight_blocks_missing_and_stale_funding(app_main, tmp_path: Path):
    conn = db.connect(str(tmp_path / "funding_missing_stale.db"))
    db.init_db(conn)
    now = int(time.time())
    rec = _base_rec()
    try:
        blocks = app_main._execution_funding_blocks(conn, rec, now_ts=now)
        assert {block["code"] for block in blocks} == {"FUNDING_RATE_UNAVAILABLE_AT_EXECUTION"}

        db.upsert_funding_rate(
            conn,
            [{"symbol": "BTCUSDT", "ts": now - 4000, "funding_rate": 0.00001, "next_funding_ts": now + 4 * 3600, "funding_interval_min": 480}],
        )
        blocks = app_main._execution_funding_blocks(conn, rec, now_ts=now)
        assert {block["code"] for block in blocks} == {"STALE_FUNDING_RATE"}
    finally:
        conn.close()


def test_execution_preflight_blocks_when_current_funding_turns_grid_edge_negative(app_main, tmp_path: Path):
    conn = db.connect(str(tmp_path / "funding_edge_negative.db"))
    db.init_db(conn)
    now = int(time.time())
    rec = _base_rec(net_profit_bps=2.0)
    try:
        db.upsert_funding_rate(
            conn,
            [{"symbol": "BTCUSDT", "ts": now - 30, "funding_rate": 0.001, "next_funding_ts": now + 3600, "funding_interval_min": 480}],
        )
        blocks = app_main._execution_funding_blocks(conn, rec, now_ts=now)
        codes = {block["code"] for block in blocks}
        assert "FUNDING_EDGE_TURNED_NEGATIVE" in codes
        assert "FUNDING_EXTREME_AT_EXECUTION" in codes
    finally:
        conn.close()


def test_execution_funding_preflight_allows_fresh_low_funding(app_main, tmp_path: Path):
    conn = db.connect(str(tmp_path / "funding_low_ok.db"))
    db.init_db(conn)
    now = int(time.time())
    rec = _base_rec(net_profit_bps=5.0)
    try:
        db.upsert_funding_rate(
            conn,
            [{"symbol": "BTCUSDT", "ts": now - 30, "funding_rate": 0.000001, "next_funding_ts": now + 3600, "funding_interval_min": 480}],
        )
        blocks = app_main._execution_funding_blocks(conn, rec, now_ts=now)
        assert blocks == []
    finally:
        conn.close()
