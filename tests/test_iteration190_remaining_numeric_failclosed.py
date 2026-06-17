from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import collector, db, direction, features, grid_math, llm_review, outcomes, recommender, risk, sentiment, sentiment_features, shock_guard


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration190.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration190_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _ohlcv_rows(count: int = 30) -> list[dict[str, float | int | bool]]:
    start = 1_700_000_000
    rows: list[dict[str, float | int | bool]] = []
    for i in range(count):
        close = 100.0 + i * 0.1
        rows.append({
            "ts": start + i * 60,
            "open": close - 0.05,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 10.0,
        })
    return rows


def test_market_data_and_feature_boundaries_reject_boolean_numbers() -> None:
    ticker = collector._sanitize_ticker_payload({
        "lastPrice": True,
        "bid1Price": False,
        "ask1Price": True,
        "volume24h": True,
        "turnover24h": False,
    })
    assert ticker == {"last": None, "bid": None, "ask": None, "vol24h": None, "turnover24h": None}

    assert collector._sanitize_ohlcv_row(
        "linear",
        "BTCUSDT",
        60,
        [1_700_000_000_000, "100", "101", "99", "100.5", True],
    ) is None

    rows = _ohlcv_rows()
    rows[-1]["volume"] = True
    assert features.compute_features_from_ohlcv(rows, {"bid": 102.8, "ask": 102.9}) is None


def test_recommender_boolean_price_and_grid_values_fail_closed() -> None:
    assert recommender._finite_or_none(True) is None
    assert recommender._safe_int_or_none(True) is None

    params = recommender._params(
        "futures_grid",
        "linear",
        {"price": True, "atr_pct": 0.01, "_direction_agg": {}},
        0.0,
        "long",
        5.5,
        "long",
        0.5,
        0.01,
        cost_model={"total_cost_bps": 12.0, "expected_funding_bps": 0.0},
        risk_limits={"min_leverage": 1, "max_leverage": 3},
    )
    assert params["price_input_valid"] is False
    assert params["invalid_price_fail_closed"] is True
    assert params["grid_count"] == 0


def test_persistence_and_proxy_label_numeric_boundaries_reject_booleans() -> None:
    now = int(time.time())
    assert db._is_valid_ticker_row({
        "venue": "linear",
        "symbol": "BTCUSDT",
        "ts": now,
        "last": True,
        "bid": 100.0,
        "ask": 100.1,
        "vol24h": 1.0,
        "turnover24h": 100.0,
    }) is False
    assert db._is_valid_ohlcv_row({
        "venue": "linear",
        "symbol": "BTCUSDT",
        "tf_sec": 60,
        "ts": now,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": True,
    }) is False
    with pytest.raises(ValueError):
        db._require_finite_float("pnl", True)
    with pytest.raises(ValueError):
        db._require_non_negative_int("volume", False)

    assert outcomes._finite_positive_or_none(True) is None
    assert outcomes._finite_or_default(True, 15.0) == pytest.approx(15.0)
    assert outcomes._int_from_params(True, 0, minimum=0, maximum=1000) == 0


def test_sentiment_boundaries_do_not_turn_booleans_into_extreme_scores() -> None:
    assert sentiment._safe_sentiment(True) == pytest.approx(0.0)
    assert sentiment._safe_unit_interval(False) == pytest.approx(0.5)
    assert sentiment_features._finite_float(True) is None


def test_directional_api_marks_unit_qty_math_as_non_position_basis(app_main) -> None:
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "params": {
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 90.0, "upper": 110.0},
                    "kill_switch": {"lower": 80.0, "upper": 120.0},
                },
            },
        },
    }

    payload = app_main._directional_exit_payload_for_reco(rec)

    assert payload["qty"] is None
    assert payload["qty_source"] == "unit_qty_ratio_only"
    assert payload["trade_math"]["gross_pnl_is_position_estimate"] is False
    assert payload["trade_math"]["qty_basis"] == "one_base_asset_for_ratio_only"


def test_boolean_risk_limits_cannot_disable_cooldown_or_rewrite_caps() -> None:
    effective = risk.normalize_risk_limits({
        "cooldown_after_loss_min": False,
        "max_concurrent_bots": True,
        "max_leverage": True,
        "max_position_notional_usdt": True,
    })

    assert effective["cooldown_after_loss_min"] == risk.DEFAULT_RISK_LIMITS["cooldown_after_loss_min"]
    assert effective["max_concurrent_bots"] == risk.DEFAULT_RISK_LIMITS["max_concurrent_bots"]
    assert effective["max_leverage"] == risk.DEFAULT_RISK_LIMITS["max_leverage"]
    assert effective["max_position_notional_usdt"] == pytest.approx(risk.DEFAULT_RISK_LIMITS["max_position_notional_usdt"])
    assert shock_guard._safe_num(True, 7.0) == pytest.approx(7.0)


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"


def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"function {name} not found in app.js"
    start = match.start()
    end = match.end()
    depth = 1
    while end < len(source) and depth:
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
        end += 1
    return source[start:end]


def test_remaining_llm_direction_funding_and_timestamp_boundaries_reject_booleans() -> None:
    assert llm_review._safe_payload_number(True) is None
    parsed = llm_review.parse_review_content(
        '{"thesis_direction":"long","execution_direction":"long","confidence":true}',
        bot_type="futures_grid",
        engine_direction="long",
    )
    assert parsed["confidence"] == pytest.approx(0.0)

    payload = llm_review.build_review_payload(
        rec={"score": True, "confidence": False, "expected_rr": True, "risk_score": False},
        feature_snapshot={},
        direction_agg={},
        market_shock={},
        sentiment_summary={},
        candles_by_tf={},
    )
    assert payload["candidate"]["score"] is None
    assert payload["candidate"]["confidence"] is None

    closes, highs, lows = direction._safe_ohlc_vectors(
        [100.0, True],
        [101.0, 102.0],
        [99.0, 100.0],
    )
    assert closes == [100.0]
    assert highs == [101.0]
    assert lows == [99.0]

    agg = direction.aggregate_direction({
        15 * 60: {"score": True, "trend_strength": True},
        4 * 60 * 60: {"score": False, "trend_strength": False},
    })
    assert agg["scores"]["all"] == pytest.approx(0.0)
    assert agg["trendiness"] == pytest.approx(0.0)

    stable, meta = recommender._stable_range_score(
        {"range_score": True, "trend_strength": False},
        {"trendiness": True, "coherence": False, "regime": "range"},
    )
    assert meta["raw_range_score_1m"] == pytest.approx(0.0)
    assert meta["trendiness"] == pytest.approx(0.0)
    assert meta["coherence"] == pytest.approx(0.5)
    assert 0.0 <= stable <= 1.0

    fallback_ts = 1_700_000_000
    assert collector._remote_ticker_ts({"time": True}, fallback_ts) == fallback_ts
    funding_row = collector._extract_funding_row(
        "BTCUSDT",
        {"fundingRate": "0.0001", "nextFundingTime": True},
        fallback_ts,
    )
    assert funding_row is not None
    assert funding_row["next_funding_ts"] is None

    assert grid_math.funding_cashflow_usdt("long", 1000, "0.001", True) == grid_math.ZERO


def test_frontend_price_percent_and_qty_formatters_reject_booleans() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, name)
        for name in ("toFiniteNumber", "formatDotNumber", "fmtPrice", "fmtPct", "formatPositionSizeValue")
    )
    script = functions + """
console.log(JSON.stringify({
  priceTrue: fmtPrice(true),
  pctFalse: fmtPct(false),
  positionTrue: formatPositionSizeValue(null, true, 'BTC'),
  priceOne: fmtPrice(1),
  pctZero: fmtPct(0),
  positionOne: formatPositionSizeValue(null, 1, 'BTC')
}));
"""
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    assert json.loads(result.stdout) == {
        "priceTrue": "—",
        "pctFalse": "—",
        "positionTrue": "—",
        "priceOne": "1",
        "pctZero": "+0.00%",
        "positionOne": "1 BTC",
    }


def test_boolean_quality_fields_cannot_bypass_two_cycle_publication_gate() -> None:
    class Settings:
        min_score_to_recommend = 0.08
        min_conf_to_recommend = 0.52

    rec = {
        "score": True,
        "confidence": True,
        "expected_rr": True,
        "reasons": {"direction_agg": {"coherence": True, "regime_confidence": True}},
    }
    assert recommender._persistence_gate_requirements(rec, Settings()) == (2, "two_cycle_confirmation")
