from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"


def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"function {name} not found in app.js"
    i = match.start()
    j = match.end()
    depth = 1
    while j < len(source) and depth:
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
        j += 1
    return source[i:j]


def _run_js(code: str) -> dict:
    result = subprocess.run(["node", "-e", code], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def _build_operator_values_harness() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    fns = [
        "toFiniteNumber",
        "toStrictInteger",
        "resolveGridCountForDisplay",
        "countDecimalsFromStep",
        "inferPriceDecimals",
        "formatDotNumber",
        "quantizeByStep",
        "formatBybitPrice",
        "formatPercentDot",
        "marginModeRu",
        "operatorExitLevels",
        "directionalExitGeometryOk",
        "operatorExitLevelsFromBackend",
        "buildOperatorValues",
        "firstFiniteValue",
        "firstFiniteField",
    ]
    return "\n".join(_extract_js_function(source, name) for name in fns) + "\n"


def test_linear_directional_ui_blocks_backend_exit_payload_direction_mismatch() -> None:
    harness = _build_operator_values_harness()
    code = harness + """
const result = buildOperatorValues({
  venue: 'linear',
  direction: 'short',
  directional_exit_levels: {
    direction: 'long',
    take_profit: 105,
    stop_loss: 95,
    kill_switch_lower: 95,
    kill_switch_upper: 105,
    take_profit_label: 'Take Profit',
    stop_loss_label: 'Stop Loss',
    has_directional_take_profit: true,
    geometry_valid: true,
    reference_price: 100
  },
  params: {
    margin_mode: 'isolated',
    leverage: 3,
    trade_plan: {
      reference_price: 100,
      levels: { kill_switch: { lower: 95, upper: 105 } }
    }
  },
  bybit_meta: { tick_size: '0.1' }
});
console.log(JSON.stringify({
  tp: result.takeProfitValue,
  sl: result.stopLossValue,
  label: result.takeProfitLabel,
  geometry: result.exitGeometry,
}));
"""
    out = _run_js(code)

    assert out["tp"] == "—"
    assert out["sl"] == "95.0 / 105.0"
    assert out["label"] == "Directional TP blocked"
    assert "direction mismatch" in out["geometry"]
    assert "item=short" in out["geometry"]
    assert "payload=long" in out["geometry"]
