from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_details_panel_exposes_operator_bot_lifetime() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Время работы" in app_js
    assert "formatBotLifetimeValue" in app_js
    assert "expected_horizon" in app_js
    assert "max_hours" in app_js
    assert "label_horizon_hours" in app_js


def test_bot_lifetime_does_not_replace_recommendation_ttl() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    # Bot lifetime is the operator-facing holding window from trade_plan.expected_horizon,
    # not recommendation freshness TTL. TTL remains available only in tech payload / raw data.
    lifetime_fn = app_js.split("function formatBotLifetimeValue", 1)[1].split("function formatPositionSizeValue", 1)[0]
    assert "ttl_sec" not in lifetime_fn
    assert "expected_horizon" in lifetime_fn


def test_static_asset_cache_key_bumped_after_bot_lifetime_details() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v34" in index
    assert "app.js?v=manual-ui-v34" in index
