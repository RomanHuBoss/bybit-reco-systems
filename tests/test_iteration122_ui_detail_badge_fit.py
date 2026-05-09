from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_compact_bot_type_label_stays_short_but_keeps_full_title() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert 'compact ? "Linear USDT Grid" : "Bybit Linear USDT Futures Grid"' in app_js
    assert 'title="${escapeHtml(fullLabel)}"' in app_js
    assert "botTypePillHtml(it.bot_type, true)" in app_js


def test_details_subtitle_wraps_instead_of_overflowing_panel() -> None:
    styles = (ROOT / "app/ui/static/styles.css").read_text(encoding="utf-8")

    assert ".operator-subtitle-inline {" in styles
    assert "display: flex;" in styles
    assert "flex-wrap: wrap;" in styles
    assert "width: 100%;" in styles
    assert ".operator-subtitle-inline .bot-type-pill.compact" in styles
    assert "white-space: normal;" in styles


def test_static_asset_cache_key_bumped_after_ui_fix() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v14" in index
    assert "app.js?v=manual-ui-v14" in index
