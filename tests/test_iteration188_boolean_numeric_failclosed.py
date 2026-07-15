from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from app.bybit_client import _safe_float as bybit_safe_float, _safe_int as bybit_safe_int
from app.main import _safe_int_or_none, _trade_plan_price_context
from app.trading_semantics import (
    bybit_linear_protective_order_plan,
    directional_exit_levels,
    directional_trade_math,
    validate_directional_exit_geometry,
)

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"
INDEX_HTML = ROOT / "app" / "ui" / "static" / "index.html"


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


def test_canonical_directional_math_rejects_boolean_numeric_fields() -> None:
    assert directional_trade_math("long", True, 2.0, 0.5, 1.0) is None
    assert directional_trade_math("long", 1.0, 2.0, 0.5, True) is None

    errors = validate_directional_exit_geometry("long", True, 2.0, 0.5)
    assert "DIRECTIONAL_ENTRY_PRICE_MISSING" in {item["code"] for item in errors}

    levels = directional_exit_levels("short", False, True)
    assert levels.take_profit is None
    assert levels.stop_loss is None

    protective = bybit_linear_protective_order_plan("short", "take_profit", False, True)
    assert protective["geometry_valid"] is False
    assert {item["code"] for item in protective["geometry_errors"]} == {
        "PROTECTIVE_REFERENCE_PRICE_INVALID",
        "PROTECTIVE_TRIGGER_PRICE_INVALID",
    }


def test_execution_price_context_rejects_boolean_prices_and_grid_counts() -> None:
    rec = {
        "params": {
            "price_ref": True,
            "grid_count": True,
            "trade_plan": {
                "reference_price": True,
                "grid_count": True,
                "levels": {
                    "range": {"lower": True, "upper": True},
                    "kill_switch": {"lower": True, "upper": True},
                    "grid_step": {"step_abs": True},
                    "tp_per_leg": {"abs": True, "pct": True},
                },
            },
        }
    }

    ctx = _trade_plan_price_context(rec)
    for key in (
        "reference_price",
        "range_lower",
        "range_upper",
        "kill_switch_lower",
        "kill_switch_upper",
        "grid_step_abs",
        "tp_per_leg_abs",
        "tp_per_leg_pct",
    ):
        assert ctx[key] is None, key
    assert ctx["grid_levels"] is None
    assert _safe_int_or_none(True) is None
    assert _safe_int_or_none(False) is None
    assert bybit_safe_float(True) is None
    assert bybit_safe_float(False) is None
    assert bybit_safe_int(True, default=7) == 7
    assert bybit_safe_int(False, default=7) == 7


def test_frontend_numeric_parser_rejects_boolean_values() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    fn = _extract_js_function(source, "toFiniteNumber")
    script = fn + """
console.log(JSON.stringify({
  trueValue: toFiniteNumber(true),
  falseValue: toFiniteNumber(false),
  one: toFiniteNumber(1),
  zero: toFiniteNumber(0),
  numericString: toFiniteNumber('1')
}));
"""
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    assert json.loads(result.stdout) == {
        "trueValue": None,
        "falseValue": None,
        "one": 1,
        "zero": 0,
        "numericString": 1,
    }


def test_static_asset_cache_key_bumped_after_boolean_numeric_patch() -> None:
    index = INDEX_HTML.read_text(encoding="utf-8")
    assert "styles.css?v=manual-ui-v49-russian-operator-language" in index
    assert "app.js?v=manual-ui-v49-russian-operator-language" in index
