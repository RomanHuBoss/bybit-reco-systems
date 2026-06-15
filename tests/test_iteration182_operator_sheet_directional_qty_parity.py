from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/ui/static/app.js"


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration182.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration182_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _base_rec(operator_sheet: dict) -> dict:
    return {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "params": {
            "grid_count": 10,
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 80.0, "upper": 150.0},
                    "kill_switch": {"lower": 70.0, "upper": 160.0},
                },
            },
            "operator_sheet": operator_sheet,
        },
    }


@pytest.mark.parametrize(
    ("operator_sheet", "expected_qty", "expected_source", "expected_profit", "expected_loss"),
    [
        (
            {"estimated_position_qty": 12.5},
            12.5,
            "estimated_position_qty",
            375.0,
            750.0,
        ),
        (
            {"max_position_notional_usdt": 1500.0},
            10.0,
            "max_position_notional_usdt/max_grid_price",
            300.0,
            600.0,
        ),
    ],
)
def test_backend_directional_exit_qty_reads_operator_sheet_top_level_like_ui(
    app_main,
    operator_sheet: dict,
    expected_qty: float,
    expected_source: str,
    expected_profit: float,
    expected_loss: float,
) -> None:
    """Backend TP/SL math must not ignore operator_sheet top-level sizing fields.

    The operator panel already uses top-level operator_sheet as a position-size
    source. If backend skips that same mapping, the API can render directional
    TP/SL PnL as unknown/1-unit math while the UI position-size row shows a full
    operator-sheet exposure.
    """
    payload = app_main._directional_exit_payload_for_reco(_base_rec(operator_sheet))

    assert payload["qty"] == pytest.approx(expected_qty)
    assert payload["qty_source"] == expected_source
    assert payload["trade_math"]["gross_profit_usdt"] == pytest.approx(expected_profit)
    assert payload["trade_math"]["gross_loss_usdt"] == pytest.approx(expected_loss)


def test_operator_ui_position_notional_lookup_includes_top_level_operator_sheet() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    match = re.search(
        r"const positionNotionalPick = firstFiniteField\(\s*\[(?P<sources>[^\]]+)\],\s*positionNotionalKeys\s*\)",
        app_js,
        re.DOTALL,
    )
    assert match, "operator positionNotionalPick lookup not found"
    assert "operatorSheet" in match.group("sources")
    assert '"max_position_notional_usdt"' in app_js
