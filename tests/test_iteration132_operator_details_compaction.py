from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_details_panel_is_operator_first_not_diagnostic_dump() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Можно запускать после preflight" in app_js
    assert "Параметры запуска Bybit Futures Grid" in app_js
    assert "Параметры запуска Bybit Futures Grid" in app_js
    assert "LLM-рекомендация" in app_js
    assert "Техподробности" in app_js

    # Diagnostics remain available through the tech modal, but are no longer
    # rendered as top-level operator cards in the narrow Details panel.
    assert "<h3>Контекст сигнала</h3>" not in app_js
    assert "<h3>Исполнение и ликвидность</h3>" not in app_js
    assert "<h3>Риск-отчёт</h3>" not in app_js
    assert "${buildLlmReviewCardHtml(llmReview, it.direction)}" not in app_js


def test_details_panel_keeps_launch_blockers_visible() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "hardBlocked" in app_js
    assert "riskReportRejected.length" in app_js
    assert "bybitErrors.length" in app_js
    assert "blocks.length" in app_js
    assert "Есть блокер, запрещающий ручное создание grid-бота" in app_js
    assert "Есть блокер, запрещающий ручное создание grid-бота" in app_js


def test_static_asset_cache_key_bumped_after_details_compaction() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v18" in index
    assert "app.js?v=manual-ui-v18" in index
