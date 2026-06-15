from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/ui/static/app.js"


def _app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_operator_ui_derives_worst_case_position_qty_from_worst_grid_price() -> None:
    """Worst-case notional is not priced at reference_price.

    Red condition in the received archive: buildOperatorFieldSpecs preferred the
    conservative worst-case notional but divided it by reference_price when it
    had to infer base qty for display. A grid with reference=100, upper=150 and
    worst-case notional=1500 would be displayed as 15 base units instead of the
    executable 10 units.
    """
    app_js = _app_js()

    assert "function gridMaxNotionalPrice(referencePrice, rangeLower, rangeUpper)" in app_js
    assert "const positionNotionalPick = firstFiniteField" in app_js
    assert "worstCasePositionNotionalKeys.has(positionNotionalPick.key)" in app_js
    assert "gridMaxNotionalPrice(referencePrice, rangeLowerForQty, rangeUpperForQty)" in app_js
    assert "не из reference-price" in app_js


def test_static_asset_cache_key_bumped_after_worst_case_qty_ui_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v38" in index
    assert "app.js?v=manual-ui-v38" in index
