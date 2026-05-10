from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_operator_ui_hides_create_bot_link_for_non_actionable_recommendations() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "function isLaunchableGridRecommendation(it)" in app_js
    assert 'it.status === "recommended" || it.status === "active"' in app_js
    assert 'riskDecision !== "recommended"' in app_js
    assert 'errors.length === 0' in app_js
    assert 'const botLink = isLaunchableGridRecommendation(it)' in app_js
    assert 'if (isLaunchableGridRecommendation(it)) {' in app_js
    assert 'bot.removeAttribute("href")' in app_js
    assert "Создание grid-бота скрыто" in app_js


def test_operator_ui_no_longer_builds_bybit_create_url_from_arbitrary_bot_type() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "function bybitBotCreateUrl" not in app_js
    assert "function futuresGridBotCreateUrl()" in app_js
    assert 'href="${escapeHtml(botUrl)}"' not in app_js
    assert 'bot.href = bybitBotCreateUrl(it.bot_type)' not in app_js
    assert 'it.direction === "short" ? "Стоп-лосс' not in app_js
    assert 'it.direction === "short" ? "Тейк-профит' not in app_js
