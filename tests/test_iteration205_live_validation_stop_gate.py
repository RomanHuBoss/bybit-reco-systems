from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import db
from app import main as app_main


@pytest.fixture()
def conn(tmp_path: Path):
    connection = db.connect(str(tmp_path / "live-validation-stop.db"))
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


def _seed_stopped_evidence_bot(
    conn,
    *,
    index: int,
    symbol: str = "BTCUSDT",
    net_pnl: float = -1.0,
    started_ts: int,
    model_version: str = "test-205",
) -> None:
    rec_id = f"R-205-{symbol}-{index}"
    bot_id = f"B-205-{symbol}-{index}"
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": rec_id,
                "ts": started_ts - 60,
                "venue": "linear",
                "symbol": symbol,
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "cross",
                "score": 0.5,
                "confidence": 0.7,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {},
                "reasons": {},
                "blocks": [],
                "status": "executed",
                "ttl_sec": 3600,
                "model_version": model_version,
                "features_ref_ts": started_ts - 120,
                "publication_root_rec_id": rec_id,
                "is_outcome_label_root": 1,
            }
        ],
    )
    db.insert_bot_instance(
        conn,
        {
            "bot_id": bot_id,
            "started_ts": started_ts,
            "stopped_ts": started_ts + 30,
            "venue": "linear",
            "symbol": symbol,
            "bot_type": "futures_grid",
            "mode": {"direction": "long"},
            "params": {},
            "state": {},
            "status": "stopped",
            "origin_rec_id": rec_id,
            "publication_root_rec_id": rec_id,
        },
    )
    # Exact live-validation evidence must represent a terminally flat execution
    # ledger. Record the opening Buy and matching closing Sell; only the close
    # carries the realised PnL used by the test.
    db.insert_execution_event(
        conn,
        {
            "event_id": f"EV-205-{symbol}-{index}-open",
            "bot_id": bot_id,
            "origin_rec_id": rec_id,
            "ts": started_ts + 10,
            "symbol": symbol,
            "event_type": "execution",
            "source": "bybit_execution",
            "external_event_id": f"exec-205-{symbol}-{index}-open",
            "external_order_id": f"order-205-{symbol}-{index}-open",
            "side": "Buy",
            "qty": 0.1,
            "price": 100.0,
            "order_price": 100.0,
            "benchmark_price": 99.0,
            "benchmark_ts": started_ts + 9,
            "benchmark_source": "pre_submit_mid",
            "gross_pnl": 0.0,
            "fee": 0.0,
            "funding": 0.0,
            "slippage": 0.1,
            "currency": "USDT",
            "meta": {},
        },
    )
    gross_pnl = float(net_pnl) + 0.1
    db.insert_execution_event(
        conn,
        {
            "event_id": f"EV-205-{symbol}-{index}-close",
            "bot_id": bot_id,
            "origin_rec_id": rec_id,
            "ts": started_ts + 20,
            "symbol": symbol,
            "event_type": "execution",
            "source": "bybit_execution",
            "external_event_id": f"exec-205-{symbol}-{index}-close",
            "external_order_id": f"order-205-{symbol}-{index}-close",
            "side": "Sell",
            "qty": 0.1,
            "price": 100.0,
            "order_price": 100.0,
            "benchmark_price": 101.0,
            "benchmark_ts": started_ts + 19,
            "benchmark_source": "pre_submit_mid",
            "gross_pnl": gross_pnl,
            "fee": 0.1,
            "funding": 0.0,
            "slippage": 0.1,
            "currency": "USDT",
            "meta": {},
        },
    )


def _patch_unrelated_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main, "_execution_recommendation_freshness_blocks", lambda *a, **k: [])
    monkeypatch.setattr(app_main, "_execution_market_data_blocks", lambda *a, **k: [])
    monkeypatch.setattr(app_main, "_execution_live_price_blocks", lambda *a, **k: [])
    monkeypatch.setattr(app_main, "_execution_funding_blocks", lambda *a, **k: [])
    monkeypatch.setattr(app_main, "apply_market_shock_gate", lambda *a, **k: [])
    monkeypatch.setattr(app_main, "compute_symbol_fast_veto", lambda *a, **k: {"blocks": []})
    monkeypatch.setattr(
        app_main,
        "_validate_trade_plan_against_bybit_meta",
        lambda *a, **k: {"ok": True, "errors": [], "warnings": [], "meta_checked": True, "snapped_levels": {}},
    )


def _preflight(conn, monkeypatch: pytest.MonkeyPatch, *, symbol: str = "BTCUSDT", direction: str = "long") -> dict:
    _patch_unrelated_preflight(monkeypatch)
    return app_main._execution_preflight(
        conn,
        {
            "rec_id": "R-current-205",
            "venue": "linear",
            "symbol": symbol,
            "bot_type": "futures_grid",
            "direction": direction,
            "model_version": "test-205",
            "params": {},
        },
        now_ts=int(time.time()),
        bybit_meta={"symbol": symbol},
    )


def test_preflight_blocks_symbol_after_persistent_negative_exact_evidence(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    base_ts = int(time.time()) - 10_000
    pnl = [-1.0, -2.0, 0.5, -1.5, -0.8, 0.2, -1.1, -0.7]
    for index, value in enumerate(pnl):
        _seed_stopped_evidence_bot(conn, index=index, net_pnl=value, started_ts=base_ts + index * 60)

    result = _preflight(conn, monkeypatch)
    codes = {str(item.get("code")) for item in result["blocks"]}

    assert "LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY" in codes
    assert result["strategy_health"]["direction"]["eligible_stopped_bots"] == 8
    assert result["strategy_health"]["direction"]["total_realized_pnl_net"] == pytest.approx(sum(pnl))


def test_preflight_blocks_five_consecutive_symbol_losses_before_large_sample(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    base_ts = int(time.time()) - 10_000
    for index in range(5):
        _seed_stopped_evidence_bot(conn, index=index, net_pnl=-1.0 - index / 10, started_ts=base_ts + index * 60)

    result = _preflight(conn, monkeypatch)
    codes = {str(item.get("code")) for item in result["blocks"]}

    assert "LIVE_VALIDATION_DIRECTION_LOSS_STREAK" in codes
    assert result["strategy_health"]["direction"]["consecutive_losses"] == 5


def test_other_symbol_losses_do_not_poison_symbol_gate_below_portfolio_threshold(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    base_ts = int(time.time()) - 10_000
    for index in range(8):
        _seed_stopped_evidence_bot(
            conn,
            index=index,
            symbol="ETHUSDT",
            net_pnl=-2.0,
            started_ts=base_ts + index * 60,
        )

    result = _preflight(conn, monkeypatch, symbol="BTCUSDT")
    codes = {str(item.get("code")) for item in result["blocks"]}

    assert "LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY" not in codes
    assert "LIVE_VALIDATION_DIRECTION_LOSS_STREAK" not in codes
    assert "LIVE_VALIDATION_PORTFOLIO_NEGATIVE_EXPECTANCY" not in codes


def test_long_losses_do_not_block_short_before_symbol_level_threshold(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    base_ts = int(time.time()) - 10_000
    for index in range(8):
        _seed_stopped_evidence_bot(conn, index=index, net_pnl=-2.0, started_ts=base_ts + index * 60)

    result = _preflight(conn, monkeypatch, symbol="BTCUSDT", direction="short")
    codes = {str(item.get("code")) for item in result["blocks"]}

    assert "LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY" not in codes
    assert "LIVE_VALIDATION_DIRECTION_LOSS_STREAK" not in codes
    assert "LIVE_VALIDATION_SYMBOL_NEGATIVE_EXPECTANCY" not in codes


def test_previous_model_losses_do_not_block_new_explicit_model_version(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    base_ts = int(time.time()) - 10_000
    for index in range(8):
        _seed_stopped_evidence_bot(
            conn,
            index=index,
            net_pnl=-2.0,
            started_ts=base_ts + index * 60,
            model_version="old-model",
        )

    _patch_unrelated_preflight(monkeypatch)
    result = app_main._execution_preflight(
        conn,
        {
            "rec_id": "R-current-new-model-205",
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "model_version": "new-model",
            "params": {},
        },
        now_ts=int(time.time()),
        bybit_meta={"symbol": "BTCUSDT"},
    )
    assert result["strategy_health"]["model_version"] == "new-model"
    assert result["strategy_health"]["blocks"] == []


def test_live_validation_summary_exposes_same_gate_state(conn) -> None:
    base_ts = int(time.time()) - 10_000
    for index in range(5):
        _seed_stopped_evidence_bot(conn, index=index, net_pnl=-1.0, started_ts=base_ts + index * 60)

    health = app_main._compute_live_validation_strategy_health(
        conn,
        venue="linear",
        symbol="BTCUSDT",
        direction="long",
        bot_type="futures_grid",
        model_version="test-205",
    )

    assert health["blocked"] is True
    assert "LIVE_VALIDATION_DIRECTION_LOSS_STREAK" in {item["code"] for item in health["blocks"]}
    assert health["policy"]["direction_loss_streak"] == 5
