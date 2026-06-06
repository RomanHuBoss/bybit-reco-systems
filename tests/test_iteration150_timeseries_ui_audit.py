from __future__ import annotations

import math
from pathlib import Path

from app.features import btc_beta, compute_features_from_ohlcv, oi_trend


def _row(ts: int, close: float, volume: float = 100.0) -> dict[str, float | int]:
    return {
        "ts": ts,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": volume,
    }


def test_feature_layer_sorts_and_deduplicates_candles_before_rolling_indicators() -> None:
    base_rows = [_row(ts, 100.0 + ts, volume=10.0 + ts) for ts in range(1, 71)]
    replacement_last_bar = _row(70, 250.0, volume=999.0)

    canonical = compute_features_from_ohlcv(base_rows[:-1] + [replacement_last_bar], None)
    noisy_newest_first = compute_features_from_ohlcv(list(reversed(base_rows)) + [replacement_last_bar], None)

    assert canonical is not None
    assert noisy_newest_first is not None
    assert noisy_newest_first["ts_last"] == 70
    assert noisy_newest_first["price"] == 250.0
    for key in ("atr_pct", "sma_fast", "sma_slow", "slope", "trend_strength", "range_score"):
        assert noisy_newest_first[key] == canonical[key]


def test_oi_trend_normalizes_order_and_duplicate_timestamps() -> None:
    rows = [
        {"ts": 1, "oi": 100.0},
        {"ts": 5, "oi": 150.0},
        {"ts": 3, "oi": 125.0},
        {"ts": 4, "oi": 140.0},
        {"ts": 2, "oi": 110.0},
        {"ts": 5, "oi": 160.0},
    ]
    out = oi_trend(rows)

    assert out["oi_now"] == 160.0
    assert out["oi_4h_chg_pct"] == 60.0
    assert out["trend"] == "unknown"


def test_btc_beta_ignores_non_finite_and_non_positive_prices_without_crashing() -> None:
    symbol = [100.0 + i for i in range(40)] + [float("nan"), -1.0, 141.0, 142.0]
    btc = [200.0 + i * 2.0 for i in range(40)] + [0.0, float("inf"), 282.0, 284.0]

    out = btc_beta(symbol, btc, window=24)

    assert out["window"] >= 8
    assert out["correlation"] is None or math.isfinite(float(out["correlation"]))
    assert out["beta"] is None or math.isfinite(float(out["beta"]))


def test_operator_ui_reads_canonical_trade_plan_before_legacy_param_aliases() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert 'const rangeLowerRaw = firstFiniteValue([range, params, operatorSheet], ["lower", "price_range_lower", "range_lower"]);' in app_js
    assert 'const rangeUpperRaw = firstFiniteValue([range, params, operatorSheet], ["upper", "price_range_upper", "range_upper"]);' in app_js
    assert 'const entryRefRaw = firstFiniteValue([plan, params, operatorSheet], ["reference_price", "price_ref"]);' in app_js
    assert 'formatBybitPrice(params.price_range_lower' not in app_js
    assert 'formatBybitPrice(params.price_range_upper' not in app_js
    assert 'formatBybitPrice(params.price_ref' not in app_js
