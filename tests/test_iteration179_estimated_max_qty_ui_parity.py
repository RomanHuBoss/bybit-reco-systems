from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app/ui/static/app.js"
INDEX_HTML = ROOT / "app/ui/static/index.html"


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration179.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration179_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def test_backend_directional_exit_accepts_estimated_max_position_qty(app_main) -> None:
    """Backend already treats estimated_max_position_qty as full-position qty."""
    rec = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "params": {
            "trade_plan": {
                "reference_price": 100.0,
                "levels": {
                    "range": {"lower": 90.0, "upper": 110.0},
                    "kill_switch": {"lower": 80.0, "upper": 120.0},
                },
            },
            "economics": {"estimated_max_position_qty": 7.5},
        },
    }

    payload = app_main._directional_exit_payload_for_reco(rec)

    assert payload["qty"] == pytest.approx(7.5)
    assert payload["qty_source"] == "estimated_max_position_qty"
    assert payload["trade_math"]["gross_profit_usdt"] == pytest.approx(150.0)
    assert payload["trade_math"]["gross_loss_usdt"] == pytest.approx(150.0)


def test_operator_ui_uses_same_estimated_max_position_qty_key_as_backend() -> None:
    """Operator position-size display must not drop a backend-recognized qty key."""
    app_js = APP_JS.read_text(encoding="utf-8")
    match = re.search(
        r"const explicitPositionQty = firstFiniteValue\(\s*\[[^\]]+\],\s*\[(?P<keys>[^\]]+)\]",
        app_js,
        re.DOTALL,
    )
    assert match, "explicitPositionQty key list not found in operator UI"
    explicit_qty_keys = match.group("keys")

    assert '"estimated_max_position_qty"' in explicit_qty_keys


def test_static_asset_cache_key_bumped_after_estimated_qty_ui_patch() -> None:
    index = INDEX_HTML.read_text(encoding="utf-8")
    assert "styles.css?v=manual-ui-v46" in index
    assert "app.js?v=manual-ui-v46" in index
