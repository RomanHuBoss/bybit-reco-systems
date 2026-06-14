from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app/ui/static/app.js"


def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"function {name} not found"
    i = match.start()
    j = match.end()
    depth = 1
    while j < len(source) and depth:
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
        j += 1
    assert depth == 0, f"function {name} body is not balanced"
    return source[i:j]


def test_frontend_tick_rounding_preserves_directional_boundaries_for_bybit_levels() -> None:
    """UI rounding must not shrink risk bounds before displaying Bybit prices.

    A previous implementation rounded the value to the tick precision before
    applying ceil/floor. For upper SL/kill-switch values that could round down,
    and for lower TP/kill-switch values that could round up, visually shrinking
    the exchange-executable boundary.
    """
    source = APP_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, name)
        for name in (
            "toFiniteNumber",
            "countDecimalsFromStep",
            "inferPriceDecimals",
            "quantizeByStep",
            "formatBybitPrice",
        )
    )
    script = f"""
{functions}
const meta = {{tick_size: 0.01}};
const out = {{
  upRaw: quantizeByStep(100.001, 0.01, "up"),
  downRaw: quantizeByStep(99.999, 0.01, "down"),
  nearestRaw: quantizeByStep(100.005, 0.01, "nearest"),
  shortStopLoss: formatBybitPrice(100.001, meta, "up"),
  shortTakeProfit: formatBybitPrice(99.999, meta, "down"),
}};
console.log(JSON.stringify(out));
"""
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    out = json.loads(result.stdout)

    assert out["upRaw"] == "100.01"
    assert out["downRaw"] == "99.99"
    assert out["nearestRaw"] == "100.01"
    assert out["shortStopLoss"] == "100.01"
    assert out["shortTakeProfit"] == "99.99"


def test_frontend_quantize_no_longer_pre_rounds_value_before_ceil_floor() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "const unitsRaw = v / tick;" in app_js
    assert "Math.ceil(unitsRaw - eps)" in app_js
    assert "Math.floor(unitsRaw + eps)" in app_js
    assert "const scaledValue = Math.round(v * factor);" not in app_js

from app.features import btc_beta


def test_btc_beta_fails_closed_on_unaligned_invalid_prices_inside_active_window() -> None:
    symbol = [100.0 + i for i in range(30)]
    btc = [200.0 + i * 2.0 for i in range(30)]
    symbol[-7] = float("nan")
    btc[-3] = 0.0

    out = btc_beta(symbol, btc, window=24)

    assert out["correlation"] is None
    assert out["beta"] is None
    assert out["is_btc_driven"] is False
    assert out["independent_signal"] is True
