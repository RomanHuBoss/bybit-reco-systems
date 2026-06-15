from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_details_subtitle_no_long_bot_type_badge() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "botTypePillHtml(it.bot_type, true)" not in app_js
    assert "Bybit Linear USDT Futures Grid" not in app_js
    assert "Linear USDT Grid" not in app_js
    assert "directionBadge(it.direction)" in app_js
    assert "statusBadgeHtml(operatorEffectiveStatus(it))" in app_js


def test_details_subtitle_wraps_instead_of_overflowing_panel() -> None:
    styles = (ROOT / "app/ui/static/styles.css").read_text(encoding="utf-8")

    assert ".operator-subtitle-inline {" in styles
    assert "display: flex;" in styles
    assert "flex-wrap: wrap;" in styles
    assert "width: 100%;" in styles


def test_static_asset_cache_key_bumped_after_ui_fix() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v38" in index
    assert "app.js?v=manual-ui-v38" in index
