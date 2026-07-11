from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

from app import db


@pytest.fixture()
def isolated_app_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "iteration202.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration202_runtime_lock.db"))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    conn = db.connect(str(db_path))
    try:
        yield app_main, conn
    finally:
        conn.close()
        sys.modules.pop("app.main", None)


def _costed_recommendation(*, gross_profit_bps: float) -> dict:
    stored_execution_cost_bps = 14.0
    funding_cost_bps = 0.0
    return {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "params": {
            "grid_levels": 8,
            "leverage": 2,
            "cost_model": {
                "spread_bps": 1.0,
                "fee_bps_round_trip": 12.0,
                "slippage_bps": 1.0,
                "execution_cost_bps": stored_execution_cost_bps,
                "expected_funding_bps": 0.0,
            },
            "economics": {
                "gross_profit_bps": gross_profit_bps,
                "execution_cost_bps": stored_execution_cost_bps,
                "funding_cost_bps": funding_cost_bps,
                "net_profit_bps": gross_profit_bps - stored_execution_cost_bps - funding_cost_bps,
            },
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 90.0, "upper": 110.0},
                    "grid_step": {"step_abs": 0.5},
                    "tp_per_leg": {"abs": 0.55, "pct": 0.55},
                },
            },
        },
    }


def _insert_ticker(conn, *, bid: float | None, ask: float | None, last: float = 100.0) -> None:
    db.insert_tickers(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "ts": int(time.time()),
                "last": last,
                "bid": bid,
                "ask": ask,
                "vol24h": 1_000.0,
                "turnover24h": 100_000.0,
            }
        ],
    )


def _blocks_by_code(app_main, conn, rec: dict) -> dict[str, dict]:
    return {
        block["code"]: block
        for block in app_main._execution_live_price_blocks(conn, rec)
    }


def test_execution_blocks_costed_recommendation_when_live_bid_ask_is_unavailable(
    isolated_app_and_conn,
):
    app_main, conn = isolated_app_and_conn
    _insert_ticker(conn, bid=None, ask=None, last=100.0)

    blocks = _blocks_by_code(app_main, conn, _costed_recommendation(gross_profit_bps=30.0))

    assert "LIVE_PRICE_UNAVAILABLE" not in blocks
    assert "LIVE_SPREAD_UNAVAILABLE" in blocks


def test_execution_blocks_wide_live_spread_and_non_positive_recomputed_edge(
    isolated_app_and_conn,
):
    app_main, conn = isolated_app_and_conn
    bid = 99.8
    ask = 100.2
    gross_profit_bps = 40.0
    _insert_ticker(conn, bid=bid, ask=ask)

    blocks = _blocks_by_code(
        app_main,
        conn,
        _costed_recommendation(gross_profit_bps=gross_profit_bps),
    )

    midpoint = (bid + ask) / 2.0
    live_spread_bps = (ask - bid) / midpoint * 10_000.0
    live_slippage_bps = max(1.0, live_spread_bps * 0.35)
    live_execution_cost_bps = 12.0 + live_spread_bps + live_slippage_bps
    live_net_profit_bps = gross_profit_bps - live_execution_cost_bps

    assert live_spread_bps == pytest.approx(40.0)
    assert live_execution_cost_bps == pytest.approx(66.0)
    assert live_net_profit_bps == pytest.approx(-26.0)
    assert "LIVE_SPREAD_TOO_WIDE" in blocks
    assert "LIVE_EXECUTION_EDGE_NON_POSITIVE" in blocks
    assert "spread_bps=40.00" in blocks["LIVE_SPREAD_TOO_WIDE"]["msg"]
    assert "execution_cost_bps=66.00" in blocks["LIVE_EXECUTION_EDGE_NON_POSITIVE"]["msg"]
    assert "net_profit_bps=-26.00" in blocks["LIVE_EXECUTION_EDGE_NON_POSITIVE"]["msg"]


def test_execution_blocks_live_edge_below_two_bps_even_when_spread_is_below_absolute_cap(
    isolated_app_and_conn,
):
    app_main, conn = isolated_app_and_conn
    bid = 99.95
    ask = 100.05
    gross_profit_bps = 27.0
    _insert_ticker(conn, bid=bid, ask=ask)

    blocks = _blocks_by_code(
        app_main,
        conn,
        _costed_recommendation(gross_profit_bps=gross_profit_bps),
    )

    midpoint = (bid + ask) / 2.0
    live_spread_bps = (ask - bid) / midpoint * 10_000.0
    live_slippage_bps = max(1.0, live_spread_bps * 0.35)
    live_execution_cost_bps = 12.0 + live_spread_bps + live_slippage_bps
    live_net_profit_bps = gross_profit_bps - live_execution_cost_bps

    assert live_spread_bps == pytest.approx(10.0)
    assert live_execution_cost_bps == pytest.approx(25.5)
    assert live_net_profit_bps == pytest.approx(1.5)
    assert "LIVE_SPREAD_TOO_WIDE" not in blocks
    assert "LIVE_EXECUTION_EDGE_NON_POSITIVE" not in blocks
    assert "LIVE_EXECUTION_EDGE_TOO_THIN" in blocks
    assert "execution_cost_bps=25.50" in blocks["LIVE_EXECUTION_EDGE_TOO_THIN"]["msg"]
    assert "net_profit_bps=1.50" in blocks["LIVE_EXECUTION_EDGE_TOO_THIN"]["msg"]


def test_execution_reapplies_gross_cost_coverage_to_live_spread(isolated_app_and_conn):
    app_main, conn = isolated_app_and_conn
    _insert_ticker(conn, bid=99.95, ask=100.05)

    blocks = _blocks_by_code(app_main, conn, _costed_recommendation(gross_profit_bps=28.0))

    # 28.0 bps is still positive after the 25.5 bps live execution estimate,
    # but does not satisfy the same 1.10x gross/cost safety floor used at publication.
    assert "LIVE_EXECUTION_EDGE_NON_POSITIVE" not in blocks
    assert "LIVE_EXECUTION_EDGE_TOO_THIN" not in blocks
    assert "LIVE_GROSS_EDGE_BELOW_COSTS" in blocks
    assert "gross_profit_bps=28.00" in blocks["LIVE_GROSS_EDGE_BELOW_COSTS"]["msg"]
    assert "execution_cost_bps=25.50" in blocks["LIVE_GROSS_EDGE_BELOW_COSTS"]["msg"]


def test_execution_allows_healthy_recomputed_live_edge(isolated_app_and_conn):
    app_main, conn = isolated_app_and_conn
    _insert_ticker(conn, bid=99.995, ask=100.005)

    blocks = _blocks_by_code(app_main, conn, _costed_recommendation(gross_profit_bps=30.0))

    assert not {
        "LIVE_SPREAD_UNAVAILABLE",
        "LIVE_SPREAD_TOO_WIDE",
        "LIVE_EXECUTION_EDGE_NON_POSITIVE",
        "LIVE_EXECUTION_EDGE_TOO_THIN",
        "LIVE_GROSS_EDGE_BELOW_COSTS",
    }.intersection(blocks)
