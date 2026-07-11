from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app import calibration, collector, db, grid_math, outcomes
from app.bybit_client import BybitPublicClient
from app.main import _execution_label_horizon_sec, _funding_events_until_horizon


def test_bybit_market_contract_rejects_fractional_timestamps_and_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=0)

    def fake_get(path: str, _params: dict) -> dict:
        if path == "/v5/market/tickers":
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "fundingRate": "0.0001",
                            "nextFundingTime": 1_700_028_800_000.75,
                            "fundingIntervalHour": 8.5,
                        }
                    ]
                },
            }
        if path == "/v5/market/open-interest":
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "timestamp": 1_700_000_000_000.75,
                            "openInterest": "123.45",
                        }
                    ]
                },
            }
        raise AssertionError(path)

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(
        client,
        "get_instrument_info",
        lambda _category, _symbol: {"fundingInterval": 480.5},
    )
    try:
        funding = client.get_funding_rate("BTCUSDT")
        oi_rows, _cursor = client.get_open_interest_page("BTCUSDT")
    finally:
        client.close()

    assert funding is not None
    assert funding["next_funding_ts"] is None
    assert funding["funding_interval_min"] is None
    assert oi_rows == []


def test_collector_does_not_normalize_fractional_market_time_into_valid_rows() -> None:
    fallback_ts = 1_700_000_000

    assert collector._sanitize_ohlcv_row(
        "linear",
        "BTCUSDT",
        60,
        [1_700_000_000_000.75, "100", "101", "99", "100.5", "5"],
    ) is None
    assert collector._remote_ticker_ts({"time": 1_700_000_000.75}, fallback_ts) == fallback_ts

    funding = collector._extract_funding_row(
        "BTCUSDT",
        {
            "fundingRate": "0.0001",
            "nextFundingTime": 1_700_028_800_000.75,
            "fundingIntervalHour": 8.5,
        },
        fallback_ts,
    )
    assert funding is not None
    assert funding["next_funding_ts"] is None
    assert funding["funding_interval_min"] is None


def test_fractional_funding_and_open_interest_rows_cannot_overwrite_integer_keys(
    tmp_path: Path,
) -> None:
    conn = db.connect(str(tmp_path / "temporal-integrity.db"))
    try:
        db.init_db(conn)
        db.upsert_funding_rate(
            conn,
            [
                {
                    "symbol": "BTCUSDT",
                    "ts": 1_700_000_000,
                    "funding_rate": "0.0001",
                    "next_funding_ts": 1_700_028_800,
                    "funding_interval_min": 480,
                }
            ],
        )
        db.upsert_open_interest(
            conn,
            "BTCUSDT",
            [{"ts": 1_700_000_000, "oi": "100"}],
        )

        # Before iteration 200 these malformed rows collided with the valid
        # integer-second keys after int() truncation. They must be dropped.
        db.upsert_funding_rate(
            conn,
            [
                {
                    "symbol": "BTCUSDT",
                    "ts": 1_700_000_000.75,
                    "funding_rate": "0.0099",
                    "next_funding_ts": 1_700_028_800.75,
                    "funding_interval_min": 480.5,
                }
            ],
        )
        db.upsert_open_interest(
            conn,
            "BTCUSDT",
            [{"ts": 1_700_000_000.75, "oi": "999"}],
        )

        funding = db.get_latest_funding_rate(conn, "BTCUSDT")
        oi_rows = db.get_oi_series(conn, "BTCUSDT", limit=10)
    finally:
        conn.close()

    assert funding is not None
    assert funding["funding_rate"] == pytest.approx(0.0001)
    assert funding["next_funding_ts"] == 1_700_028_800
    assert funding["funding_interval_min"] == 480
    assert oi_rows == [{"ts": 1_700_000_000, "oi": 100.0}]


def test_purged_oof_rejects_fractional_decision_and_label_availability_times() -> None:
    assert calibration._purged_train_indices(
        [100.5, 200],
        [150, None],
        validation_start_index=1,
    ) == []
    assert calibration._purged_train_indices(
        [100, 200],
        [199.5, None],
        validation_start_index=1,
    ) == []


def test_fractional_label_horizon_falls_back_to_canonical_grid_horizon() -> None:
    rec = {
        "params": {
            "cost_model": {"horizon_sec": 6 * 3600 + 0.5},
            "label_horizon_hours": 6.5,
        }
    }

    assert _execution_label_horizon_sec(rec) == 12 * 3600
    runtime_horizon, used_fallback = outcomes._resolve_effective_horizon(
        "futures_grid",
        {"label_horizon_hours": 6.5},
        900,
    )
    assert runtime_horizon == 12 * 3600
    assert used_fallback is False
    assert db._backfill_effective_horizon_sec(
        "futures_grid",
        {"label_horizon_hours": 6.5},
        900,
    ) == 12 * 3600


def test_fractional_next_funding_time_uses_unknown_schedule_conservative_count() -> None:
    # Unknown schedule => ceil(1500 / 1000) == 2 possible events. Truncating
    # next_funding_ts=2000.5 to 2000 incorrectly proves only one event.
    assert _funding_events_until_horizon(
        now_ts=1_000,
        next_funding_ts=2_000.5,
        interval_sec=1_000,
        horizon_sec=1_500,
    ) == 2


@pytest.mark.parametrize("bad_events", [1.1, 1.9, "1.5"])
def test_funding_cashflow_rejects_fractional_event_counts(bad_events: object) -> None:
    assert grid_math.funding_cashflow_usdt(
        "long",
        position_notional=1_000,
        funding_rate="0.001",
        events=bad_events,
    ) == Decimal("0")


def test_funding_cashflow_keeps_exact_integer_compatibility() -> None:
    assert grid_math.funding_cashflow_usdt(
        "long",
        position_notional=1_000,
        funding_rate="0.001",
        events=2.0,
    ) == Decimal("2.000")
