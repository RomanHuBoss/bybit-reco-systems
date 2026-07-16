from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.bybit_client import BybitPublicClient
from app import collector
from app.outcomes import _grid_outcome


def _seed_flat(conn, base_ts: int, minutes: int = 2) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base_ts + i * 60,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1_000.0,
            }
            for i in range(minutes)
        ],
    )


def _params(base_ts: int, forecast_rate: float = 0.009) -> dict:
    cost = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "market_round_trip_cost_bps": 0.0,
        "funding_rate": forecast_rate,
        "next_funding_ts": base_ts + 60,
        "funding_interval_min": 60,
        "expected_funding_events": 1,
        "directional_funding_bps_per_event": abs(forecast_rate) * 10_000,
        "expected_funding_bps": abs(forecast_rate) * 10_000,
    }
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "cost_model": dict(cost),
        "trade_plan": {
            "grid_count": 2,
            "cost_model": dict(cost),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 102.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def test_bybit_client_parses_settled_funding_history() -> None:
    assert hasattr(BybitPublicClient, "get_funding_rate_history")
    client = BybitPublicClient("https://example.invalid")
    try:
        calls: list[tuple[str, dict]] = []

        def fake_get(path: str, params: dict):
            calls.append((path, dict(params)))
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": "1720000060000"},
                        {"symbol": "ETHUSDT", "fundingRate": "0.9", "fundingRateTimestamp": "1720000060000"},
                        {"symbol": "BTCUSDT", "fundingRate": "NaN", "fundingRateTimestamp": "1720000120000"},
                    ]
                },
            }

        client._get = fake_get  # type: ignore[method-assign]
        rows = client.get_funding_rate_history("BTCUSDT", start_ms=1720000000000, end_ms=1720000200000)
        assert rows == [{"symbol": "BTCUSDT", "ts": 1720000060, "funding_rate": 0.0001}]
        assert calls == [
            (
                "/v5/market/funding/history",
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "limit": "200",
                    "startTime": "1720000000000",
                    "endTime": "1720000200000",
                },
            )
        ]
    finally:
        client.close()


def test_collector_backfills_settled_funding_history(tmp_path: Path) -> None:
    assert hasattr(collector, "_fetch_funding_settlements_for_symbol")
    conn = db.connect(str(tmp_path / "collector.db"))
    try:
        db.init_db(conn)

        class StubClient:
            def get_funding_rate_history(self, symbol: str, *, start_ms: int, end_ms: int, limit: int):
                assert symbol == "BTCUSDT"
                assert limit == 200
                return [
                    {"symbol": symbol, "ts": 1_720_000_060, "funding_rate": 0.0001},
                    {"symbol": symbol, "ts": 1_720_000_120, "funding_rate": -0.0002},
                ]

        rows = collector._fetch_funding_settlements_for_symbol(
            conn, StubClient(), "linear", "BTCUSDT", 1_720_000_180
        )
        assert rows == [
            {"symbol": "BTCUSDT", "ts": 1_720_000_060, "funding_rate": 0.0001},
            {"symbol": "BTCUSDT", "ts": 1_720_000_120, "funding_rate": -0.0002},
        ]
    finally:
        conn.close()


def test_database_persists_and_queries_settled_funding(tmp_path: Path) -> None:
    assert hasattr(db, "upsert_funding_settlements")
    assert hasattr(db, "get_funding_settlements")
    conn = db.connect(str(tmp_path / "funding.db"))
    try:
        db.init_db(conn)
        db.upsert_funding_settlements(
            conn,
            [
                {"symbol": "BTCUSDT", "ts": 1_720_000_060, "funding_rate": 0.0001},
                {"symbol": "BTCUSDT", "ts": 1_720_000_120, "funding_rate": -0.0002},
                {"symbol": "BTCUSDT", "ts": True, "funding_rate": 1.0},
            ],
        )
        rows = db.get_funding_settlements(conn, "BTCUSDT", 1_720_000_000, 1_720_000_120)
        assert rows == [
            {"symbol": "BTCUSDT", "ts": 1_720_000_060, "funding_rate": 0.0001},
            {"symbol": "BTCUSDT", "ts": 1_720_000_120, "funding_rate": -0.0002},
        ]
    finally:
        conn.close()


def test_short_funding_receipt_is_excluded_from_canonical_outcome_edge(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "short.db"))
    try:
        db.init_db(conn)
        base = 1_720_100_000
        _seed_flat(conn, base)
        db.upsert_funding_settlements(conn, [{"symbol": "BTCUSDT", "ts": base + 60, "funding_rate": 0.001}])
        result = _grid_outcome(conn, "linear", "BTCUSDT", 100.0, 100.0, base, base + 120, "short", _params(base, forecast_rate=-0.009))
        assert result is not None
        success, ret = result
        assert success == 0
        assert ret == pytest.approx(0.0)
    finally:
        conn.close()


def test_long_pays_positive_settled_funding_even_when_forecast_was_receipt(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "long.db"))
    try:
        db.init_db(conn)
        base = 1_720_200_000
        _seed_flat(conn, base)
        db.upsert_funding_settlements(conn, [{"symbol": "BTCUSDT", "ts": base + 60, "funding_rate": 0.001}])
        result = _grid_outcome(conn, "linear", "BTCUSDT", 100.0, 100.0, base, base + 120, "long", _params(base, forecast_rate=-0.009))
        assert result is not None
        success, ret = result
        assert success == 0
        assert ret == pytest.approx(-0.1 / 199.0)
    finally:
        conn.close()


def test_negative_settled_funding_reverses_long_short_cashflows(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "negative.db"))
    try:
        db.init_db(conn)
        base = 1_720_300_000
        _seed_flat(conn, base)
        db.upsert_funding_settlements(conn, [{"symbol": "BTCUSDT", "ts": base + 60, "funding_rate": -0.001}])
        long_result = _grid_outcome(conn, "linear", "BTCUSDT", 100.0, 100.0, base, base + 120, "long", _params(base))
        short_result = _grid_outcome(conn, "linear", "BTCUSDT", 100.0, 100.0, base, base + 120, "short", _params(base))
        assert long_result == pytest.approx((0, 0.0))
        assert short_result == pytest.approx((0, -0.1 / 201.0))
    finally:
        conn.close()


def test_outcome_is_unavailable_when_expected_settlement_is_missing(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "missing.db"))
    try:
        db.init_db(conn)
        base = 1_720_400_000
        _seed_flat(conn, base)
        assert _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base,
            base + 120,
            "long",
            _params(base),
        ) is None
    finally:
        conn.close()


def test_forecast_rate_does_not_change_historical_outcome_when_settlement_is_same(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "forecast.db"))
    try:
        db.init_db(conn)
        base = 1_720_500_000
        _seed_flat(conn, base)
        db.upsert_funding_settlements(conn, [{"symbol": "BTCUSDT", "ts": base + 60, "funding_rate": 0.001}])
        a = _grid_outcome(conn, "linear", "BTCUSDT", 100.0, 100.0, base, base + 120, "short", _params(base, 0.009))
        b = _grid_outcome(conn, "linear", "BTCUSDT", 100.0, 100.0, base, base + 120, "short", _params(base, -0.009))
        assert a == pytest.approx(b)
    finally:
        conn.close()


def test_outcome_contract_bumped_for_settled_funding() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'version="1.0.69"' in source
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
