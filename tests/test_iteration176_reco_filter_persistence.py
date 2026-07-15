from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_recommendation_status_filters_are_restored_before_initial_fetch() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert 'const RECO_FILTER_STORAGE_KEY = "operator.recommendationStatusFilters.v1"' in app_js
    assert '"showPending"' in app_js
    assert 'function restoreRecommendationFilterState()' in app_js
    assert 'restoreRecommendationFilterState();\nrefreshAll();' in app_js


def test_recommendation_status_filter_changes_are_persisted() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert 'function persistRecommendationFilterState()' in app_js
    assert 'window.localStorage.setItem(RECO_FILTER_STORAGE_KEY' in app_js
    assert 'persistRecommendationFilterState();\n    refreshAll();' in app_js


def test_static_asset_cache_key_bumped_after_filter_persistence_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v46" in index
    assert "app.js?v=manual-ui-v46" in index
