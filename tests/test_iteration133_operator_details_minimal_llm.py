from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_details_panel_keeps_only_operator_launch_fields_and_llm() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Параметры запуска Bybit Futures Grid" in app_js
    assert "LLM-рекомендация" in app_js
    assert "Рекомендация LLM" in app_js
    assert "Вероятность LLM" in app_js
    assert "Сторона" in app_js
    assert "Диапазон входа" in app_js
    assert "Кол-во сеток" in app_js
    assert "Плечо" in app_js
    assert "Take Profit" in app_js
    assert "Stop Loss" in app_js

    # The top-level Details panel must not be a diagnostic dump.
    assert "<h3>Контроль запуска</h3>" not in app_js
    assert "<h3>Факторы решения</h3>" not in app_js
    assert "<h3>Защита рынка</h3>" not in app_js
    assert "<h3>Bybit validation</h3>" not in app_js
    assert "Net/сетка conservative" not in app_js
    assert "Qty/order" not in app_js
    assert "Margin est." not in app_js
    assert "${buildLlmReviewCardHtml(llmReview, it.direction)}" not in app_js


def test_details_panel_shows_blockers_only_when_they_matter() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "explicitHardBlocked" in app_js
    assert "blockerItems" in app_js
    assert "Фактическая причина блокировки / предупреждения" in app_js
    assert "bybitErrors.length" in app_js
    assert "riskReportRejected.length" in app_js
    assert "Есть жёсткий блокер, запрещающий ручное создание grid-бота" in app_js


def test_static_asset_cache_key_bumped_after_minimal_llm_details() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v46" in index
    assert "app.js?v=manual-ui-v46" in index
