from __future__ import annotations

import pytest

from app.bybit_client import BybitPublicClient
from app.recommender import _params


class _FakeHttpResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)

    def close(self):
        return None


def test_funding_helper_falls_back_to_instrument_info_for_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _Transport(
        [
            _FakeHttpResponse(
                {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "fundingRate": "0.0001",
                                "nextFundingTime": "1710000000000",
                                # Some ticker payloads do not carry fundingIntervalHour.
                            }
                        ]
                    },
                }
            ),
            _FakeHttpResponse(
                {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "contractType": "LinearPerpetual",
                                "quoteCoin": "USDT",
                                "settleCoin": "USDT",
                                "deliveryTime": "0",
                                "fundingInterval": 240,
                            }
                        ],
                    },
                }
            ),
        ]
    )
    client = BybitPublicClient("https://example.invalid", max_retries=0)
    client._client = transport  # type: ignore[attr-defined]
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda *_args, **_kwargs: None)

    try:
        row = client.get_funding_rate("BTCUSDT")
    finally:
        client.close()

    assert row is not None
    assert row["funding_rate"] == pytest.approx(0.0001)
    assert row["next_funding_ts"] == 1710000000
    assert row["funding_interval_min"] == 240
    assert transport.calls[0][1] == {"category": "linear", "symbol": "BTCUSDT"}
    assert transport.calls[1][1] == {"category": "linear", "symbol": "BTCUSDT"}


def test_grid_spacing_floor_excludes_horizon_funding_from_each_pair() -> None:
    params = _params(
        "futures_grid",
        "linear",
        {
            "price": 100.0,
            "atr_pct": 0.0001,
            "_atr_pct_1h": 0.0001,
            "_direction_agg": {"trendiness": 0.05, "coherence": 0.80, "regime": "range", "regime_confidence": 0.80},
        },
        global_sent=0.0,
        direction="long",
        taker_fee_bps=4.0,
        direction_bias="long",
        direction_bias_strength=0.25,
        atr_pct_for_grid=0.0001,
        cost_model={"execution_cost_bps": 8.0, "expected_funding_bps": 20.0},
    )

    assert params["grid_spacing_cost_floor_bps"] == pytest.approx(8.0)
    assert params["grid_spacing_funding_cost_bps"] == pytest.approx(0.0)
    assert params["cost_model"]["expected_funding_bps"] == pytest.approx(20.0)
    assert params["economics"]["net_profit_bps"] > 0


def test_grid_spacing_floor_does_not_use_funding_receipt_as_free_edge() -> None:
    params = _params(
        "futures_grid",
        "linear",
        {
            "price": 100.0,
            "atr_pct": 0.0001,
            "_atr_pct_1h": 0.0001,
            "_direction_agg": {"trendiness": 0.05, "coherence": 0.80, "regime": "range", "regime_confidence": 0.80},
        },
        global_sent=0.0,
        direction="short",
        taker_fee_bps=4.0,
        direction_bias="short",
        direction_bias_strength=0.25,
        atr_pct_for_grid=0.0001,
        cost_model={"execution_cost_bps": 8.0, "expected_funding_bps": -20.0},
    )

    assert params["grid_spacing_cost_floor_bps"] == pytest.approx(8.0)
    assert params["grid_spacing_funding_cost_bps"] == pytest.approx(0.0)
    assert params["economics"]["funding_benefit_excluded_bps"] == pytest.approx(20.0)
