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


def _operator_field_harness() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    fns = [
        "toFiniteNumber",
        "countDecimalsFromStep",
        "inferPriceDecimals",
        "formatDotNumber",
        "quantizeByStep",
        "formatBybitPrice",
        "formatPercentDot",
        "fmtPrice",
        "formatUsdValue",
        "directionRu",
        "splitLinearSymbol",
        "firstFiniteValue",
        "firstFiniteField",
        "gridMaxNotionalPrice",
        "formatHoursValue",
        "formatBotLifetimeValue",
        "formatPositionSizeValue",
        "directionalExitGeometryOk",
    ]
    helper_names = ["directionalExitMathForDisplay"]
    available_helpers = [name for name in helper_names if re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)]
    fns.extend(available_helpers)
    fns.append("buildOperatorFieldSpecs")
    return "\n".join(_extract_js_function(source, name) for name in fns) + "\n"


def test_operator_risk_math_is_hidden_when_backend_exit_payload_direction_mismatches() -> None:
    harness = _operator_field_harness()
    code = harness + """
const fields = buildOperatorFieldSpecs({
  symbol: 'BTCUSDT',
  venue: 'linear',
  direction: 'short',
  directional_exit_levels: {
    direction: 'long',
    take_profit: 105,
    stop_loss: 95,
    kill_switch_lower: 95,
    kill_switch_upper: 105,
    has_directional_take_profit: true,
    geometry_valid: true,
    reference_price: 100,
    trade_math: {
      take_profit_distance_pct: 5,
      stop_loss_distance_pct: 5,
      risk_reward: 1
    }
  },
  params: {
    leverage: 3,
    margin_mode: 'cross',
    trade_plan: {
      reference_price: 100,
      grid_count: 20,
      expected_horizon: { max_hours: 12 },
      levels: { range: { lower: 96, upper: 104 } }
    },
    sizing: { estimated_worst_case_margin_required_usdt: 50 }
  }
}, {
  rangeLower: '96.0',
  rangeUpper: '104.0',
  entryRef: '100.0',
  leverage: '3',
  takeProfitLabel: 'Направленная цель прибыли заблокирована',
  takeProfitValue: '—',
  stopLossLabel: 'Ограничение убытка / аварийная граница выхода',
  stopLossValue: '95.0 / 105.0'
});
const byLabel = Object.fromEntries(fields.map(f => [f.label, f.value]));
console.log(JSON.stringify({distance: byLabel['Расстояние до цели / ограничения'], hasRr: Object.prototype.hasOwnProperty.call(byLabel, 'RR защитных уровней')}));
"""
    out = _run_js(code)

    assert out == {"distance": "—", "hasRr": False}


def test_operator_risk_math_is_hidden_when_backend_exit_geometry_is_invalid() -> None:
    harness = _operator_field_harness()
    code = harness + """
const fields = buildOperatorFieldSpecs({
  symbol: 'BTCUSDT',
  venue: 'linear',
  direction: 'short',
  directional_exit_levels: {
    direction: 'short',
    take_profit: 105,
    stop_loss: 95,
    kill_switch_lower: 95,
    kill_switch_upper: 105,
    has_directional_take_profit: true,
    geometry_valid: false,
    reference_price: 100,
    trade_math: {
      take_profit_distance_pct: 5,
      stop_loss_distance_pct: 5,
      risk_reward: 1
    }
  },
  params: {
    leverage: 3,
    margin_mode: 'cross',
    trade_plan: {
      reference_price: 100,
      grid_count: 20,
      expected_horizon: { max_hours: 12 },
      levels: { range: { lower: 96, upper: 104 } }
    },
    sizing: { estimated_worst_case_margin_required_usdt: 50 }
  }
}, {
  rangeLower: '96.0',
  rangeUpper: '104.0',
  entryRef: '100.0',
  leverage: '3',
  takeProfitLabel: 'Направленная цель прибыли заблокирована',
  takeProfitValue: '—',
  stopLossLabel: 'Ограничение убытка / аварийная граница выхода',
  stopLossValue: '95.0 / 105.0'
});
const byLabel = Object.fromEntries(fields.map(f => [f.label, f.value]));
console.log(JSON.stringify({distance: byLabel['Расстояние до цели / ограничения'], hasRr: Object.prototype.hasOwnProperty.call(byLabel, 'RR защитных уровней')}));
"""
    out = _run_js(code)

    assert out == {"distance": "—", "hasRr": False}
