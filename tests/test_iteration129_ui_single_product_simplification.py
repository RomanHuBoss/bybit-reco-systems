from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_operator_ui_names_both_supported_strategy_families_without_legacy_product_label() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Bybit Recommender — Сетка и тренд" in index
    assert "Bybit Linear USDT Futures Grid" not in index
    assert "Bybit Linear USDT Futures Grid" not in app_js
    assert "Linear USDT Grid" not in app_js
    assert 'if (botType === SUPPORTED_GRID_BOT_TYPE) return "Фьючерсная сетка";' in app_js
    assert 'if (botType === DIRECTIONAL_TREND_BOT_TYPE) return "Направленный тренд · одна позиция";' in app_js


def test_main_recommendations_table_has_no_bot_type_column() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert 'data-sort="bot_type"' not in index
    assert "Тип бота" not in index
    assert "botTypePillHtml(it.bot_type)" not in app_js
    assert "botTypePillHtml(it.bot_type, true)" not in app_js


def test_venue_filter_removed_but_api_keeps_linear_scope() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
    styles = (ROOT / "app/ui/static/styles.css").read_text(encoding="utf-8")

    assert 'id="venue"' not in index
    assert "Площадка" not in index
    assert 'const venue = "linear";' in app_js
    assert '["topN", "minConf"].forEach' in app_js
    assert "control-venue" not in styles


def test_subwindow_tables_hide_redundant_product_and_venue_dimensions() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert '<div class="modal-section-title">7. По типу бота</div>' not in app_js
    assert '{ label: "Площадка"' not in app_js
    assert '{ label: "Бот", render: row => botTypePillHtml(row.bot_type, true) }' not in app_js


def test_static_asset_cache_key_bumped_after_single_product_ui_change() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v49-russian-operator-language" in index
    assert "app.js?v=manual-ui-v49-russian-operator-language" in index
