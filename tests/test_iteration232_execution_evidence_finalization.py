from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import db
from app import main as app_main


@pytest.fixture()
def conn(tmp_path: Path):
    connection = db.connect(str(tmp_path / "iteration232.db"))
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


def _seed_stopped_bot(conn, suffix: str) -> tuple[str, str, int]:
    now = int(time.time()) - 600
    rec_id = f"R-232-{suffix}"
    bot_id = f"B-232-{suffix}"
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": rec_id,
                "ts": now,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "cross",
                "score": 0.6,
                "confidence": 0.7,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {},
                "reasons": {},
                "blocks": [],
                "status": "executed",
                "ttl_sec": 3600,
                "model_version": "iteration232",
                "features_ref_ts": now - 60,
                "publication_root_rec_id": rec_id,
                "is_outcome_label_root": 1,
            }
        ],
    )
    db.insert_bot_instance(
        conn,
        {
            "bot_id": bot_id,
            "started_ts": now + 10,
            "stopped_ts": now + 300,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "mode": {"direction": "long"},
            "params": {},
            "state": {},
            "status": "stopped",
            "origin_rec_id": rec_id,
            "publication_root_rec_id": rec_id,
        },
    )
    return rec_id, bot_id, now


def _insert_execution(
    conn,
    *,
    rec_id: str,
    bot_id: str,
    suffix: str,
    ts: int,
    side: str,
    qty: float,
    gross_pnl: float,
    fee: float = 0.1,
) -> None:
    price = 100.0 if side == "Buy" else 101.0
    benchmark = 99.0 if side == "Buy" else 102.0
    db.insert_execution_event(
        conn,
        {
            "event_id": f"EV-232-{suffix}",
            "bot_id": bot_id,
            "origin_rec_id": rec_id,
            "ts": ts,
            "symbol": "BTCUSDT",
            "event_type": "execution",
            "source": "bybit_execution",
            "external_event_id": f"EXT-232-{suffix}",
            "external_order_id": f"ORD-232-{suffix}",
            "side": side,
            "qty": qty,
            "price": price,
            "order_price": price,
            "benchmark_price": benchmark,
            "benchmark_ts": ts - 1,
            "benchmark_source": "pre_submit_mid",
            "gross_pnl": gross_pnl,
            "fee": fee,
            "funding": 0.0,
            "slippage": qty,
            "currency": "USDT",
            "meta": {},
        },
    )


def test_stopped_bot_with_unmatched_fill_is_not_finalized_or_validation_eligible(conn) -> None:
    rec_id, bot_id, base = _seed_stopped_bot(conn, "partial")
    # A single profitable Sell can be only a partial/closing fragment. Without the
    # matching Buy ledger the service cannot prove that the bot is flat or that
    # realized PnL represents total bot PnL.
    _insert_execution(
        conn,
        rec_id=rec_id,
        bot_id=bot_id,
        suffix="partial-sell",
        ts=base + 100,
        side="Sell",
        qty=0.1,
        gross_pnl=10.0,
    )

    summary = db.get_bot_execution_summary(conn, bot_id)
    assert summary["net_position_qty"] == pytest.approx(-0.1)
    assert summary["position_flat"] is False
    assert summary["total_pnl_finalized"] is False

    record = next(item for item in db.list_live_validation_records(conn) if item["bot_id"] == bot_id)
    assert record["validation_eligible"] is False
    assert "residual_position" in record["validation_ineligible_reasons"]


def test_balanced_buy_sell_ledger_is_finalized_and_eligible(conn) -> None:
    rec_id, bot_id, base = _seed_stopped_bot(conn, "flat")
    _insert_execution(
        conn,
        rec_id=rec_id,
        bot_id=bot_id,
        suffix="flat-buy",
        ts=base + 80,
        side="Buy",
        qty=0.1,
        gross_pnl=0.0,
    )
    _insert_execution(
        conn,
        rec_id=rec_id,
        bot_id=bot_id,
        suffix="flat-sell",
        ts=base + 120,
        side="Sell",
        qty=0.1,
        gross_pnl=2.0,
    )

    summary = db.get_bot_execution_summary(conn, bot_id)
    assert summary["buy_qty"] == pytest.approx(0.1)
    assert summary["sell_qty"] == pytest.approx(0.1)
    assert summary["net_position_qty"] == pytest.approx(0.0, abs=1e-12)
    assert summary["position_flat"] is True
    assert summary["total_pnl_finalized"] is True

    record = next(item for item in db.list_live_validation_records(conn) if item["bot_id"] == bot_id)
    assert record["validation_eligible"] is True
    assert record["validation_ineligible_reasons"] == []


def test_live_validation_summary_defensively_excludes_unfinalized_rows() -> None:
    summary = app_main._live_validation_scope_summary(
        [
            {
                "bot_id": "B-232-fabricated-positive",
                "publication_root_rec_id": "ROOT-232-fabricated-positive",
                "validation_eligible": True,
                "total_pnl_finalized": False,
                "realized_pnl_net": 100.0,
            }
        ]
    )
    assert summary["eligible_stopped_bots"] == 0
    assert summary["total_realized_pnl_net"] == pytest.approx(0.0)
