from __future__ import annotations

from pathlib import Path

import pytest

from app import collector, db, recommender, risk


def _complete_minute_bucket(*, bucket_ts: int, count: int = 15) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        price = 100.0 + index
        rows.append(
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": bucket_ts + index * 60,
                "open": price,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.5,
                "volume": 10.0 + index,
            }
        )
    return rows


@pytest.mark.parametrize(
    ("field", "value"),
    (("pnl", False), ("fee", False)),
)
def test_trade_pnl_and_fee_reject_booleans_in_insert_path(
    tmp_path: Path,
    field: str,
    value: bool,
) -> None:
    conn = db.connect(str(tmp_path / f"trade-{field}.db"))
    db.init_db(conn)
    trade = {
        "trade_id": f"trade-{field}",
        "bot_id": "bot-1",
        "ts": 1_700_000_000,
        "symbol": "BTCUSDT",
        "pnl": 12.5,
        "fee": 0.5,
        "meta": {},
    }
    trade[field] = value

    with pytest.raises(ValueError, match=field):
        db.insert_trade(conn, trade)

    count = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
    assert count == 0
    conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (("velocity", False), ("volume", False)),
)
def test_batch_sentiment_numeric_defaults_do_not_coerce_booleans(
    tmp_path: Path,
    field: str,
    value: bool,
) -> None:
    conn = db.connect(str(tmp_path / f"sentiment-{field}.db"))
    db.init_db(conn)
    row = {
        "scope": "symbol",
        "key": "BTCUSDT",
        "ts": 1_700_000_000,
        "sentiment": 0.2,
        "velocity": 0.0,
        "volume": 3,
        "sources": {},
        "tags": [],
    }
    row[field] = value

    with pytest.raises(ValueError, match=field):
        db.insert_sentiment_points(conn, [row])

    assert db.get_sentiment_series(conn, "symbol", "BTCUSDT", limit=10) == []
    conn.close()


def test_malformed_active_risk_override_preserves_strict_zero_fallback_caps() -> None:
    fallback = {
        **risk.DEFAULT_RISK_LIMITS,
        "max_daily_dd_usdt": 0.0,
        "max_position_notional_usdt": 0.0,
        "max_margin_per_bot_usdt": 0.0,
    }

    effective = risk.normalize_risk_limits(
        {
            "max_daily_dd_usdt": "invalid",
            "max_position_notional_usdt": "invalid",
            "max_margin_per_bot_usdt": "invalid",
        },
        fallback,
    )

    assert effective["max_daily_dd_usdt"] == pytest.approx(0.0)
    assert effective["max_position_notional_usdt"] == pytest.approx(0.0)
    assert effective["max_margin_per_bot_usdt"] == pytest.approx(0.0)


def test_fractional_integer_risk_override_uses_fallback_instead_of_truncating() -> None:
    fallback = {
        **risk.DEFAULT_RISK_LIMITS,
        "cooldown_after_loss_min": 45,
        "max_concurrent_bots": 7,
    }

    effective = risk.normalize_risk_limits(
        {
            "cooldown_after_loss_min": 0.9,
            "max_concurrent_bots": "3.5",
        },
        fallback,
    )

    assert effective["cooldown_after_loss_min"] == 45
    assert effective["max_concurrent_bots"] == 7
    assert risk.normalize_risk_limits({"cooldown_after_loss_min": 0.0})["cooldown_after_loss_min"] == 0
    assert risk.normalize_risk_limits({"max_leverage": "4.0"})["max_leverage"] == 4


def test_drop_open_candle_filters_every_open_future_and_malformed_row() -> None:
    ts_now = 1_000
    rows = [
        {"ts": 1_020, "close": 104.0},  # future row
        {"ts": 980, "close": 103.0},    # open row
        {"ts": "broken", "close": 999.0},
        {"ts": 940, "close": 102.0},    # closes exactly at ts_now
        {"ts": 880, "close": 101.0},
    ]

    closed = recommender._drop_open_candle(rows, tf_sec=60, ts_now=ts_now)

    assert [row["ts"] for row in closed] == [940, 880]


def test_resample_emits_only_complete_contiguous_source_buckets() -> None:
    target_tf_sec = 15 * 60
    bucket_ts = 1_700_000_000 - (1_700_000_000 % target_tf_sec)
    complete = _complete_minute_bucket(bucket_ts=bucket_ts)

    aggregated = collector._resample_rows(complete, target_tf_sec)

    assert len(aggregated) == 1
    candle = aggregated[0]
    assert candle["ts"] == bucket_ts
    assert candle["tf_sec"] == target_tf_sec
    assert candle["open"] == pytest.approx(complete[0]["open"])
    assert candle["close"] == pytest.approx(complete[-1]["close"])
    assert candle["high"] == pytest.approx(max(float(row["high"]) for row in complete))
    assert candle["low"] == pytest.approx(min(float(row["low"]) for row in complete))
    assert candle["volume"] == pytest.approx(sum(float(row["volume"]) for row in complete))

    missing_source_bar = [row for index, row in enumerate(complete) if index != 7]
    assert collector._resample_rows(missing_source_bar, target_tf_sec) == []

    duplicate_source_bar = [*complete, dict(complete[7])]
    assert collector._resample_rows(duplicate_source_bar, target_tf_sec) == []


def test_operator_api_models_reject_boolean_numeric_payloads() -> None:
    from pydantic import ValidationError

    from app.main import BotTradeRequest, SentimentPointRequest

    invalid_trade_payloads = (
        {"pnl": False},
        {"pnl": 1.0, "fee": False},
        {"pnl": 1.0, "ts": False},
        {"pnl": 1.0, "ts": 0},
        {"pnl": 1.0, "ts": -1},
    )
    for payload in invalid_trade_payloads:
        with pytest.raises(ValidationError):
            BotTradeRequest.model_validate(payload)

    invalid_sentiment_payloads = (
        {"scope": "symbol", "key": "BTCUSDT", "sentiment": False},
        {"scope": "symbol", "key": "BTCUSDT", "sentiment": 0.1, "velocity": False},
        {"scope": "symbol", "key": "BTCUSDT", "sentiment": 0.1, "volume": False},
        {"scope": "symbol", "key": "BTCUSDT", "sentiment": 0.1, "ts": False},
        {"scope": "symbol", "key": "BTCUSDT", "sentiment": 0.1, "ts": 0},
        {"scope": "symbol", "key": "BTCUSDT", "sentiment": 0.1, "ts": -1},
    )
    for payload in invalid_sentiment_payloads:
        with pytest.raises(ValidationError):
            SentimentPointRequest.model_validate(payload)


def test_persistence_integer_guards_reject_fractional_and_boolean_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="volume"):
        db._require_non_negative_int("volume", 1.5)

    conn = db.connect(str(tmp_path / "integer-write-guards.db"))
    db.init_db(conn)
    base_trade = {
        "trade_id": "trade-ts",
        "bot_id": "bot-1",
        "symbol": "BTCUSDT",
        "pnl": 1.0,
        "fee": 0.1,
        "meta": {},
    }
    for index, invalid_ts in enumerate((False, 1_700_000_000.5)):
        with pytest.raises(ValueError, match="ts"):
            db.insert_trade(conn, {**base_trade, "trade_id": f"trade-ts-{index}", "ts": invalid_ts})
    conn.close()


def test_ohlcv_persistence_rejects_non_integer_timeframe_and_timestamp() -> None:
    import time

    now = int(time.time()) - 120
    base = {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "tf_sec": 60,
        "ts": now,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
    }

    assert db._is_valid_ohlcv_row({**base, "tf_sec": True}) is False
    assert db._is_valid_ohlcv_row({**base, "tf_sec": 60.5}) is False
    assert db._is_valid_ohlcv_row({**base, "ts": now + 0.5}) is False


def test_ticker_persistence_rejects_non_integer_timestamp() -> None:
    import time

    row = {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "ts": int(time.time()) + 0.5,
        "last": 100.0,
        "bid": 99.9,
        "ask": 100.1,
        "vol24h": 10.0,
        "turnover24h": 1000.0,
    }
    assert db._is_valid_ticker_row(row) is False
