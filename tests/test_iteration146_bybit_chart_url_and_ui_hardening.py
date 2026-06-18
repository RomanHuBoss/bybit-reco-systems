from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _app_js() -> str:
    return (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")


def test_operator_ui_uses_working_bybit_usdt_perpetual_chart_route() -> None:
    app_js = _app_js()

    assert "function normalizeLinearUsdtPerpetualSymbol(symbol)" in app_js
    assert 'raw.replace(/[^A-Z0-9]/g, "")' in app_js
    assert "https://www.bybit.com/trade/usdt/" in app_js
    assert "return `https://www.bybit.com/trade/usdt/${encodeURIComponent(chartSymbol)}`;" in app_js
    assert "ru-RU/trade/linear" not in app_js
    assert "www.bybit.com/trade/linear" not in app_js


def test_operator_table_escapes_symbol_status_and_rec_id_in_rendered_html() -> None:
    app_js = _app_js()

    assert '<b>${escapeHtml(it.symbol || "—")}</b>' in app_js
    assert 'data-id="${escapeHtml(it.rec_id)}"' in app_js
    assert 'return `<span class="${cls}">${escapeHtml(status || "—")}</span>`;' in app_js


def test_operator_action_status_update_no_longer_depends_on_stale_column_index() -> None:
    app_js = _app_js()

    assert '<td data-cell="status">${pillStatus(operatorEffectiveStatus(it))}</td>' in app_js
    assert "row.querySelector('[data-cell=\"status\"]')" in app_js
    assert "cells[9]" not in app_js
    assert "column index 9" not in app_js


def test_static_asset_cache_key_bumped_after_bybit_chart_url_fix() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v45" in index
    assert "app.js?v=manual-ui-v45" in index
