from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_no_trade_copy_distinguishes_score_rejection_from_hard_blocker() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "explicitHardBlocked" in app_js
    assert "noTradeDecision" in app_js
    assert "const noTradeDecision = status === \"no_trade\"" in app_js
    assert "risk_report.decision is intentionally conservative for pending async-LLM holds" in app_js
    assert "no_trade означает: grid сейчас не запускать" in app_js
    assert "причина показана сразу под этим решением и отделена от относительного ранга" in app_js
    assert "Есть блокер, запрещающий ручное создание grid-бота" not in app_js


def test_no_trade_warning_card_is_not_rendered_as_blocker_card() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
    styles = (ROOT / "app/ui/static/styles.css").read_text(encoding="utf-8")

    assert "Почему запуск не рекомендован / предупреждения" in app_js
    assert "const blockersCardClass = explicitHardBlocked ? \"launch-blockers-card\" : \"launch-warnings-card\"" in app_js
    assert "NO_TRADE" in app_js
    assert "общий скор" not in app_js
    assert "Ранг не равен разрешению запуска" in app_js
    assert "noTradeDecisionMessage" in app_js
    assert ".launch-warnings-card" in styles


def test_static_asset_cache_key_bumped_after_no_trade_copy_fix() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v41" in index
    assert "app.js?v=manual-ui-v41" in index
