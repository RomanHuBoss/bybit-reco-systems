from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome


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


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration192.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration192_runtime.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _meta() -> dict[str, str]:
    return {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "tick_size": "0.1",
        "min_price": "1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "max_order_qty": "100",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
    }


def _base_rec() -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_count": 5,
            "grid_levels": 5,
            "grid_type": "arithmetic",
            "grid_geometry_model": "bybit_arithmetic_range_width_div_grid_count",
            "actual_grid_step_abs": 0.4,
            "leverage": 1,
            "economics": {
                "gross_profit_bps": 50.0,
                "execution_cost_bps": 10.0,
                "funding_cost_bps": 0.0,
                "net_profit_bps": 40.0,
                "estimated_active_orders": 5,
                "estimated_total_order_notional_usdt": 25.5,
                "estimated_margin_required_usdt": 25.5,
            },
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "grid_count": 5,
                "sizing": {"order_qty": 0.051, "order_notional_usdt": 5.1},
                "levels": {
                    "range": {"lower": 99.0, "upper": 101.0},
                    "kill_switch": {"lower": 98.5, "upper": 101.5},
                    "grid_step": {"step_abs": 0.4, "step_pct": 0.4},
                    "tp_per_leg": {"abs": 0.3, "pct": 0.3},
                },
            },
        },
    }


def _codes(validation: dict, key: str = "errors") -> set[str]:
    return {str(item.get("code")) for item in validation.get(key, [])}


def test_safe_integer_parser_rejects_fractional_values_without_losing_integral_json_numbers(app_main) -> None:
    assert app_main._safe_int_or_none(5.9) is None
    assert app_main._safe_int_or_none("5.9") is None
    assert app_main._safe_int_or_none(True) is None
    assert app_main._safe_int_or_none(5.0) == 5
    assert app_main._safe_int_or_none("5.0") == 5
    assert app_main._safe_int_or_none("5") == 5


@pytest.mark.parametrize(
    ("grid_count", "legacy_grid_levels"),
    [
        (5.9, 5),
        (0, 5),
        (400.9, 400),
    ],
)
def test_execution_preflight_blocks_non_integer_or_masked_primary_grid_count(
    app_main,
    grid_count: float,
    legacy_grid_levels: int,
) -> None:
    rec = _base_rec()
    rec["params"]["grid_count"] = grid_count
    rec["params"]["grid_levels"] = legacy_grid_levels

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    assert validation["ok"] is False
    assert _codes(validation) & {"GRID_COUNT_NOT_INTEGER", "GRID_COUNT_CONFLICT", "GRID_LEVELS_INVALID"}


def test_execution_preflight_blocks_conflicting_integral_grid_count_aliases(app_main) -> None:
    rec = _base_rec()
    rec["params"]["grid_count"] = 5
    rec["params"]["grid_levels"] = 5
    rec["params"]["trade_plan"]["grid_count"] = 6

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    assert validation["ok"] is False
    assert "GRID_COUNT_CONFLICT" in _codes(validation)


def test_execution_preflight_rejects_fractional_estimated_active_orders(app_main) -> None:
    rec = _base_rec()
    rec["params"]["economics"]["estimated_active_orders"] = 5.4

    validation = app_main._validate_trade_plan_against_bybit_meta(
        rec,
        _meta(),
        require_meta=True,
        require_execution_plan=True,
    )

    assert validation["ok"] is False
    assert "ACTIVE_ORDERS_NOT_INTEGER" in _codes(validation)


def _seed_oscillating_1m_rows(conn, *, base_ts: int) -> int:
    closes = [96.0, 104.0, 96.0, 104.0, 96.0, 104.0, 100.0]
    rows = []
    for idx, close in enumerate(closes):
        open_price = 100.0 if idx == 0 else closes[idx - 1]
        rows.append(
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base_ts + idx * 60,
                "open": open_price,
                "high": max(open_price, close),
                "low": min(open_price, close),
                "close": close,
                "volume": 1000.0,
            }
        )
    db.upsert_ohlcv(conn, rows)
    return len(rows)


def test_outcome_label_uses_canonical_grid_count_alias_not_only_legacy_grid_levels(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "grid-count-outcome.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_000_000
        row_count = _seed_oscillating_1m_rows(conn, base_ts=base_ts)
        common = {
            "grid_spacing_pct": 1.0,
            "price_range_lower": 95.0,
            "price_range_upper": 105.0,
            "cost_model": {"execution_cost_bps": 10.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 90.0, "upper": 110.0},
                    "tp_per_leg": {"abs": 20.0},
                }
            },
        }

        canonical = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts,
            base_ts + row_count * 60,
            "neutral",
            {**common, "grid_count": 2},
        )
        legacy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts,
            base_ts + row_count * 60,
            "neutral",
            {**common, "grid_levels": 2},
        )

        assert canonical == pytest.approx(legacy)
        assert canonical[1] == pytest.approx(0.0096)
    finally:
        conn.close()


def test_operator_ui_grid_count_parser_rejects_fractional_boolean_and_conflicting_aliases() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, name)
        for name in ("toFiniteNumber", "toStrictInteger", "resolveGridCountForDisplay")
    )
    script = functions + r'''
console.log(JSON.stringify({
  fractional: toStrictInteger(5.9),
  boolean: toStrictInteger(true),
  integralFloat: toStrictInteger(5.0),
  integralString: toStrictInteger("5.0"),
  consistent: resolveGridCountForDisplay({params: {grid_count: 5, grid_levels: 5, trade_plan: {grid_count: 5}}}),
  conflict: resolveGridCountForDisplay({params: {grid_count: 5, grid_levels: 5, trade_plan: {grid_count: 6}}}),
  invalidPrimary: resolveGridCountForDisplay({params: {grid_count: 5.9, grid_levels: 5}})
}));
'''
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    out = json.loads(result.stdout)

    assert out["fractional"] is None
    assert out["boolean"] is None
    assert out["integralFloat"] == 5
    assert out["integralString"] == 5
    assert out["consistent"] == 5
    assert out["conflict"] is None
    assert out["invalidPrimary"] is None


def test_static_asset_cache_key_bumped_after_grid_count_semantics_patch() -> None:
    index = (ROOT / "app" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert "styles.css?v=manual-ui-v45" in index
    assert "app.js?v=manual-ui-v45" in index
