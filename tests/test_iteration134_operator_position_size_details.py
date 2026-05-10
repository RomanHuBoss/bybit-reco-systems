from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_details_panel_exposes_recommended_position_size_for_margin() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Размер позиции" in app_js
    assert "Маржа" in app_js
    assert "estimated_max_position_notional_usdt" in app_js
    assert "estimated_total_order_notional_usdt" in app_js
    assert "marginRequired * leverage" in app_js
    assert "formatPositionSizeValue" in app_js

    # Keep per-order diagnostics out of the top-level operator panel: the operator
    # needs the total position size for the selected margin, not a noisy leg dump.
    assert "Qty/order" not in app_js
    assert "Margin est." not in app_js


def test_static_asset_cache_key_bumped_after_position_size_details() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v20" in index
    assert "app.js?v=manual-ui-v20" in index
