from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from app import calibration, db, features


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
    assert depth == 0, f"function {name} body is not balanced"
    return source[start:end]


def test_db_funding_and_open_interest_boundaries_reject_boolean_numbers() -> None:
    now = int(time.time())

    assert db._normalize_funding_row({
        "symbol": "BTCUSDT",
        "ts": now,
        "funding_rate": True,
    }) is None
    assert db._normalize_open_interest_row(
        "BTCUSDT",
        {"ts": now, "oi": False},
    ) is None

    normalized = db._normalize_funding_row({
        "symbol": "BTCUSDT",
        "ts": now,
        "funding_rate": "0.0001",
        "next_funding_ts": True,
        "funding_interval_min": False,
    })
    assert normalized is not None
    assert normalized["funding_rate"] == 0.0001
    assert normalized["next_funding_ts"] is None
    assert normalized["funding_interval_min"] is None


def test_feature_helpers_do_not_turn_booleans_into_funding_or_oi_signals() -> None:
    now = int(time.time())

    funding = features.funding_signal(True, 480)
    assert funding["value"] is None
    assert funding["signal"] == "unknown"

    valid_rate_bad_interval = features.funding_signal("0.0001", True)
    assert valid_rate_bad_interval["funding_interval_min"] == 480
    assert features.liquidity_tier(True) == "unknown"

    assert features.oi_trend([
        {"ts": now, "oi": 100.0},
        {"ts": now - 3600, "oi": True},
    ])["oi_now"] is None
    assert features.oi_trend([
        {"ts": now, "oi": 100.0},
        {"ts": True, "oi": 90.0},
    ])["oi_now"] is None


def _calibration_rows(*, success_value, ts_value, count: int = 100) -> list[dict]:
    now = int(time.time())
    rows = []
    for i in range(count):
        success = success_value(i) if callable(success_value) else success_value
        ts = ts_value(i, now) if callable(ts_value) else ts_value
        rows.append({
            "score": 0.2 if i % 2 else -0.2,
            "success": success,
            "ret": 0.02 if i % 2 else -0.01,
            "ts": ts,
            "label_available_ts": ts + 60 if not isinstance(ts, bool) else ts,
            "horizon_sec": 60,
            "reasons": {},
        })
    return rows


def test_calibration_rejects_boolean_labels_and_boolean_timestamps() -> None:
    boolean_labels = _calibration_rows(
        success_value=lambda i: i % 2 == 0,
        ts_value=lambda i, now: now - (i + 1) * 60,
    )
    model = calibration.fit_logreg(boolean_labels, min_samples=20, logreg_min_samples=300)
    assert model.fitted is False
    assert model.n_samples == 0

    boolean_timestamps = _calibration_rows(
        success_value=lambda i: i % 2,
        ts_value=True,
    )
    model = calibration.fit_logreg(boolean_timestamps, min_samples=20, logreg_min_samples=300)
    assert model.fitted is False
    assert model.n_samples == 0

    valid_rows = _calibration_rows(
        success_value=lambda i: i % 2,
        ts_value=lambda i, now: now - (i + 1) * 60,
    )
    model = calibration.fit_logreg(valid_rows, min_samples=20, logreg_min_samples=300)
    # Valid small samples still contribute monetary diagnostics, but they cannot
    # activate an in-sample score-only probability model.
    assert model.fitted is False
    assert model.n_samples == 100
    assert model.oof_status == "insufficient"


def test_all_operator_numeric_formatters_reject_boolean_values() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, name)
        for name in (
            "toFiniteNumber",
            "fmt",
            "countDecimalsFromStep",
            "inferPriceDecimals",
            "formatDotNumber",
            "quantizeByStep",
            "formatBybitPrice",
            "formatPercentDot",
            "formatBps",
            "fmtPrice",
            "formatUsdValue",
            "formatProbability",
        )
    )
    script = functions + r'''
console.log(JSON.stringify({
  fmtTrue: fmt(true),
  dotTrue: formatDotNumber(true),
  bybitPriceTrue: formatBybitPrice(true, {tick_size: 0.01}),
  percentFalse: formatPercentDot(false),
  bpsTrue: formatBps(true),
  usdFalse: formatUsdValue(false),
  probabilityTrue: formatProbability(true),
  fmtOne: fmt(1),
  dotOne: formatDotNumber(1),
  bybitPriceOne: formatBybitPrice(1, {tick_size: 0.01}),
  percentZero: formatPercentDot(0),
  bpsOne: formatBps(1),
  usdOne: formatUsdValue(1),
  probabilityHalf: formatProbability(0.5)
}));
'''
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    assert json.loads(result.stdout) == {
        "fmtTrue": "-",
        "dotTrue": "—",
        "bybitPriceTrue": "—",
        "percentFalse": "—",
        "bpsTrue": "—",
        "usdFalse": "—",
        "probabilityTrue": "—",
        "fmtOne": "1.00",
        "dotOne": "1",
        "bybitPriceOne": "1.00",
        "percentZero": "0%",
        "bpsOne": "1 bps",
        "usdOne": "$1",
        "probabilityHalf": "50%",
    }


def test_operator_ranking_confidence_and_btc_metrics_reject_booleans() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, name)
        for name in (
            "toFiniteNumber",
            "formatDotNumber",
            "scoreUiZone",
            "computeUiScoreMetaMap",
            "ensureUiScoreMeta",
            "getConfModel",
            "confCell",
            "dirConfCell",
            "btcRelationMetric",
        )
    )
    script = (
        "const SCORE_UI_NEAR_TIE_DELTA = 0.025; "
        "let lastItems = []; let uiScoreMetaById = new Map();\n"
        + functions
        + r'''
const ranked = computeUiScoreMetaMap([
  {rec_id: 'bad', score: true},
  {rec_id: 'good', score: 0.2}
]);
const fallback = ensureUiScoreMeta({rec_id: 'bad', score: false}, []);
console.log(JSON.stringify({
  rankedBad: ranked.has('bad'),
  rankedGood: ranked.has('good'),
  fallbackRaw: fallback.raw,
  confidenceBool: confCell({confidence: true, reasons: {confidence_model: {}}}),
  directionConfidenceBool: dirConfCell(false),
  confidenceValid: confCell({confidence: 0.75, reasons: {confidence_model: {fitted: true}}}),
  btcBool: btcRelationMetric({correlation: true, window: 24}, 'ETHUSDT').value,
  btcValid: btcRelationMetric({correlation: 0.5, window: 24}, 'ETHUSDT').value
}));
'''
    )
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    out = json.loads(result.stdout)
    assert out["rankedBad"] is False
    assert out["rankedGood"] is True
    assert out["fallbackRaw"] is None
    assert out["confidenceBool"] == "-"
    assert out["directionConfidenceBool"] == "-"
    assert "0.75" in out["confidenceValid"]
    assert out["btcBool"] == "—"
    assert out["btcValid"] == "r=0.5"
