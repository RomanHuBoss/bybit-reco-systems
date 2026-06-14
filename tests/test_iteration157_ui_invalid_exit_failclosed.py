from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_operator_ui_does_not_render_invalid_backend_directional_tp_sl_as_local_mapping() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert "backend directional TP/SL invalid; rendering kill-switch only" in app_js
    assert 'takeProfitLabel: "Directional TP blocked"' in app_js
    assert 'takeProfitValue: "—"' in app_js
    assert "stopLossValue: `${lower} / ${upper}`" in app_js
    assert "backend directional TP/SL invalid; using local kill-switch mapping" not in app_js


def test_static_asset_cache_key_bumped_after_invalid_exit_failclosed_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v31" in index
    assert "app.js?v=manual-ui-v31" in index
