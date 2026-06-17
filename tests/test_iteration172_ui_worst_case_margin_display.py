from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/ui/static/app.js"


def _app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_operator_ui_prefers_worst_case_margin_before_reference_price_margin() -> None:
    """Operator capital must not regress to legacy reference-price margin.

    Red condition in the received archive: buildOperatorFieldSpecs searched for
    estimated_margin_required_usdt before any worst-case margin field, so the UI
    could display 200 USDT while backend/runtime caps already used 300 USDT for
    the same fixed-qty grid envelope.
    """
    app_js = _app_js()
    assert '"estimated_worst_case_margin_required_usdt"' in app_js
    assert '"worst_case_margin_required_usdt"' in app_js
    assert app_js.index('"estimated_worst_case_margin_required_usdt"') < app_js.index('"estimated_margin_required_usdt"')
    assert app_js.index('"worst_case_margin_required_usdt"') < app_js.index('"margin_required_usdt"')


def test_operator_ui_prefers_worst_case_total_notional_before_reference_notional() -> None:
    """Position-size display must match the conservative grid exposure model."""
    app_js = _app_js()
    assert '"estimated_worst_case_total_order_notional_usdt"' in app_js
    assert '"worst_case_total_order_notional_usdt"' in app_js
    assert app_js.index('"estimated_worst_case_total_order_notional_usdt"') < app_js.index('"estimated_max_position_notional_usdt"')
    assert app_js.index('"worst_case_total_order_notional_usdt"') < app_js.index('"estimated_total_order_notional_usdt"')
    assert "legacy reference-price notional" in app_js


def test_static_asset_cache_key_bumped_after_worst_case_margin_ui_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")
    assert "styles.css?v=manual-ui-v42" in index
    assert "app.js?v=manual-ui-v42" in index
