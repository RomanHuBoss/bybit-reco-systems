from __future__ import annotations

from app import main as app_main


def _record(index: int, pnl: float, *, symbol: str = "BTCUSDT", direction: str = "neutral") -> dict:
    return {
        "bot_id": f"B-227-{index}",
        "rec_id": f"R-227-{index}",
        "publication_root_rec_id": f"ROOT-227-{index}",
        "venue": "linear",
        "symbol": symbol,
        "direction": direction,
        "bot_type": "futures_grid",
        "model_version": "tail-risk-model",
        "validation_eligible": True,
        "realized_pnl_net": pnl,
    }


def test_exact_evidence_stop_gate_blocks_negative_total_despite_high_win_rate(monkeypatch) -> None:
    """A grid tail loss must not be hidden by positive median/win-rate statistics."""
    # Newest first. Seven small winners and one large range-break loss give an
    # 87.5% win rate and positive median, but the cohort lost 93 USDT in total.
    records = [_record(index, 1.0) for index in range(7)] + [_record(7, -100.0)]
    monkeypatch.setattr(app_main.db, "list_live_validation_records", lambda _conn, limit: records)

    health = app_main._compute_live_validation_strategy_health(
        object(),
        venue="linear",
        symbol="BTCUSDT",
        direction="neutral",
        bot_type="futures_grid",
        model_version="tail-risk-model",
    )

    codes = {item["code"] for item in health["blocks"]}
    assert health["direction"]["total_realized_pnl_net"] == -93.0
    assert health["direction"]["median_realized_pnl_net"] == 1.0
    assert health["direction"]["positive_bot_rate"] == 0.875
    assert "LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY" in codes


def test_exact_evidence_stop_gate_does_not_block_positive_total(monkeypatch) -> None:
    records = [_record(index, value) for index, value in enumerate([2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, -3.0])]
    monkeypatch.setattr(app_main.db, "list_live_validation_records", lambda _conn, limit: records)

    health = app_main._compute_live_validation_strategy_health(
        object(),
        venue="linear",
        symbol="BTCUSDT",
        direction="neutral",
        bot_type="futures_grid",
        model_version="tail-risk-model",
    )

    assert health["direction"]["total_realized_pnl_net"] == 5.0
    assert "LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY" not in {
        item["code"] for item in health["blocks"]
    }


def test_exact_evidence_stop_gate_keeps_minimum_sample_floor(monkeypatch) -> None:
    records = [_record(index, value) for index, value in enumerate([1.0, 1.0, 1.0, 1.0, 1.0, 1.0, -100.0])]
    monkeypatch.setattr(app_main.db, "list_live_validation_records", lambda _conn, limit: records)

    health = app_main._compute_live_validation_strategy_health(
        object(),
        venue="linear",
        symbol="BTCUSDT",
        direction="neutral",
        bot_type="futures_grid",
        model_version="tail-risk-model",
    )

    assert health["direction"]["eligible_stopped_bots"] == 7
    assert "LIVE_VALIDATION_DIRECTION_NEGATIVE_EXPECTANCY" not in {
        item["code"] for item in health["blocks"]
    }
