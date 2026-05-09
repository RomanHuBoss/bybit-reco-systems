from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app.bybit_client import BybitPublicClient


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration116.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration116_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _base_rec():
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_levels": 5,
            "leverage": 1,
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


def _partial_meta_without_usdt_contract_fields():
    return {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "tick_size": "0.1",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "max_order_qty": "100",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def test_execution_preflight_requires_contract_quote_and_settle_fields(app_main):
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _base_rec(),
        _partial_meta_without_usdt_contract_fields(),
        require_meta=True,
    )

    codes = {item["code"] for item in validation["errors"]}
    assert validation["ok"] is False
    assert "BYBIT_CONTRACT_TYPE_MISSING" in codes
    assert "BYBIT_QUOTE_COIN_MISSING" in codes
    assert "BYBIT_SETTLE_COIN_MISSING" in codes


def test_reco_details_can_warn_on_partial_meta_without_hiding_it(app_main):
    validation = app_main._validate_trade_plan_against_bybit_meta(
        _base_rec(),
        _partial_meta_without_usdt_contract_fields(),
        require_meta=False,
    )

    error_codes = {item["code"] for item in validation["errors"]}
    warning_codes = {item["code"] for item in validation["warnings"]}
    assert validation["ok"] is True
    assert "BYBIT_CONTRACT_TYPE_MISSING" not in error_codes
    assert "BYBIT_CONTRACT_TYPE_MISSING" in warning_codes
    assert "BYBIT_QUOTE_COIN_MISSING" in warning_codes
    assert "BYBIT_SETTLE_COIN_MISSING" in warning_codes


def test_public_funding_helper_preserves_bybit_funding_interval(monkeypatch: pytest.MonkeyPatch):
    class DummyResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "fundingRate": "0.0001",
                            "nextFundingTime": "1710000000000",
                            "fundingIntervalHour": "4",
                        }
                    ]
                },
            }

    client = BybitPublicClient("https://example.invalid")
    monkeypatch.setattr(client._client, "get", lambda *args, **kwargs: DummyResponse())
    try:
        row = client.get_funding_rate("BTCUSDT")
    finally:
        client.close()

    assert row is not None
    assert row["funding_rate"] == pytest.approx(0.0001)
    assert row["next_funding_ts"] == 1710000000
    assert row["funding_interval_min"] == 240


def test_public_funding_helper_requires_exact_symbol_match(monkeypatch: pytest.MonkeyPatch):
    class DummyResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "ETHUSDT",
                            "fundingRate": "0.0001",
                            "nextFundingTime": "1710000000000",
                            "fundingIntervalHour": "8",
                        }
                    ]
                },
            }

    client = BybitPublicClient("https://example.invalid")
    monkeypatch.setattr(client._client, "get", lambda *args, **kwargs: DummyResponse())
    try:
        row = client.get_funding_rate("BTCUSDT")
    finally:
        client.close()

    assert row is None
