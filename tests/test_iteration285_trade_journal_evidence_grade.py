from __future__ import annotations

from pathlib import Path

from app import db
from app.outcomes import _grid_outcome


def _grid_params() -> dict:
    return {
        "grid_count": 4,
        "grid_levels": 4,
        "price_range_lower": 98.0,
        "price_range_upper": 102.0,
        "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
        "trade_plan": {
            "grid_count": 4,
            "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
            "levels": {
                "range": {"lower": 98.0, "upper": 102.0},
                "kill_switch": {"lower": 97.0, "upper": 103.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def _seed_one_sided_candle(conn, base_ts: int) -> None:
    db.upsert_ohlcv(conn, [{
        "venue": "linear",
        "symbol": "BTCUSDT",
        "tf_sec": 60,
        "ts": base_ts,
        "open": 99.0,
        "high": 100.5,
        "low": 99.0,
        "close": 100.5,
        "volume": 1_000.0,
    }])


def _rest_rows_with_ambiguous_equal_timestamp_close(base_ts: int) -> list[dict]:
    common = {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "qty": 1.0,
        "received_ts_ms": base_ts * 1000 + 59_900,
        "source": "rest_recent_trade_v1",
        "is_block_trade": False,
        "is_rpi_trade": False,
    }
    return [
        {**common, "trade_id": "a", "trade_ts_ms": base_ts * 1000 + 1_000, "seq": 1, "side": "Buy", "price": 99.0},
        # REST does not prove which of these equal-time/equal-sequence rows was
        # the actual candle close. Lexical trade-id order would incorrectly put
        # z2 last and manufacture a 99.5 close instead of the stored 100.5.
        {**common, "trade_id": "a2", "trade_ts_ms": base_ts * 1000 + 50_000, "seq": 2, "side": "Buy", "price": 100.5},
        {**common, "trade_id": "z2", "trade_ts_ms": base_ts * 1000 + 50_000, "seq": 2, "side": "Sell", "price": 99.5},
    ]


def test_rest_overlap_is_not_used_as_exact_intrabar_order(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "rest-evidence.db"))
    db.init_db(conn)
    base_ts = 1_709_200_000
    _seed_one_sided_candle(conn, base_ts)
    db.upsert_market_trades(conn, _rest_rows_with_ambiguous_equal_timestamp_close(base_ts))
    coverage_id = "trade:linear:BTCUSDT:rest"
    db.insert_market_trade_coverage(
        conn,
        coverage_id=coverage_id,
        venue="linear",
        symbol="BTCUSDT",
        coverage_start_ms=base_ts * 1000,
        coverage_end_ms=(base_ts + 60) * 1000,
        state="closed",
        source="rest_recent_trade_v1",
    )

    diagnostics: dict[str, object] = {}
    result = _grid_outcome(
        conn,
        "linear",
        "BTCUSDT",
        99.0,
        100.5,
        base_ts,
        base_ts + 60,
        "neutral",
        _grid_params(),
        diagnostics=diagnostics,
    )

    assert result is not None
    assert diagnostics.get("reason") != "trade_journal_ohlcv_mismatch"
    assert diagnostics["intrabar_observation_method"] == "ohlcv_path_equivalence_v1"
    assert diagnostics["trade_journal_replayed_candles"] == 0
    assert diagnostics["trade_journal_non_exact_coverage_ids"] == [coverage_id]
    conn.close()


def test_websocket_ohlcv_mismatch_remains_fail_closed_and_explains_delta(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "ws-mismatch.db"))
    db.init_db(conn)
    base_ts = 1_709_200_000
    _seed_one_sided_candle(conn, base_ts)
    session_id = "session-mismatch"
    rows = []
    for row_index, row in enumerate(_rest_rows_with_ambiguous_equal_timestamp_close(base_ts)):
        item = dict(row)
        item.update({
            "source": "websocket_public_trade_v1",
            "stream_session_id": session_id,
            "stream_message_index": 1,
            "stream_row_index": row_index,
            "stream_message_ts_ms": base_ts * 1000 + 59_900,
        })
        rows.append(item)
    db.upsert_market_trades(conn, rows)
    db.insert_market_trade_coverage(
        conn,
        coverage_id="ws:linear:BTCUSDT:mismatch",
        venue="linear",
        symbol="BTCUSDT",
        coverage_start_ms=base_ts * 1000,
        coverage_end_ms=(base_ts + 60) * 1000,
        state="closed",
        source="websocket_public_trade_v1",
        details={"session_id": session_id, "ordering_basis": "websocket_delivery_order_v1"},
    )

    diagnostics: dict[str, object] = {}
    result = _grid_outcome(
        conn,
        "linear",
        "BTCUSDT",
        99.0,
        100.5,
        base_ts,
        base_ts + 60,
        "neutral",
        _grid_params(),
        diagnostics=diagnostics,
    )

    assert result is None
    assert diagnostics["reason"] == "trade_journal_ohlcv_mismatch"
    assert diagnostics["trade_journal_source"] == "websocket_public_trade_v1"
    assert diagnostics["observed_trade_close"] == 99.5
    assert diagnostics["candle_close"] == 100.5
    assert diagnostics["ohlcv_mismatch_fields"] == ["close"]
    conn.close()


def test_legacy_rest_mismatch_is_requeued_but_websocket_mismatch_is_not(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "requeue.db"))
    db.init_db(conn)
    base_ts = 1_709_200_000
    db.upsert_outcome_observability(
        conn,
        rec_id="rest-rec",
        recommendation_ts=base_ts,
        label_due_ts=base_ts + 43_200,
        state="censored",
        reason="trade_journal_ohlcv_mismatch",
        details={"coverage_id": "trade:linear:BTCUSDT:legacy"},
    )
    db.upsert_outcome_observability(
        conn,
        rec_id="ws-rec",
        recommendation_ts=base_ts,
        label_due_ts=base_ts + 43_200,
        state="censored",
        reason="trade_journal_ohlcv_mismatch",
        details={
            "coverage_id": "ws:linear:BTCUSDT:session",
            "trade_journal_source": "websocket_public_trade_v1",
        },
    )

    assert db.requeue_rest_trade_ohlcv_mismatches(conn) == 1
    rows = {
        str(row["rec_id"]): row
        for row in conn.execute(
            "SELECT rec_id, state, reason, details_json FROM reco_outcome_observability"
        ).fetchall()
    }
    assert rows["rest-rec"]["state"] == "waiting"
    assert rows["rest-rec"]["reason"] == "observation_contract_upgrade_v4_retry"
    assert rows["ws-rec"]["state"] == "censored"
    assert rows["ws-rec"]["reason"] == "trade_journal_ohlcv_mismatch"
    conn.close()
