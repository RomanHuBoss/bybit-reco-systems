from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration151.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration151_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def _rec(now: int) -> dict:
    return {
        "rec_id": "distance-context-rec",
        "ts": now - 30,
        "ttl_sec": 600,
        "bot_type": "futures_grid",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "short",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "params": {
            "grid_count": 10,
            "grid_levels": 10,
            "grid_type": "arithmetic",
            "leverage": 3,
            "price_ref": 100.0,
            "price_range_lower": 50.0,
            "price_range_upper": 150.0,
            "economics": {"net_profit_bps": 8.0, "gross_profit_bps": 24.0, "execution_cost_bps": 10.0},
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "levels": {
                    "range": {"lower": 50.0, "upper": 150.0},
                    "kill_switch": {"lower": 40.0, "upper": 160.0},
                    "grid_step": {"step_abs": 10.0, "step_pct": 10.0},
                    "tp_per_leg": {"abs": 7.0, "pct": 7.0},
                },
                "economics": {"net_profit_bps": 8.0, "gross_profit_bps": 24.0, "execution_cost_bps": 10.0},
            },
        },
    }


def test_operator_bound_distances_use_current_price_as_symmetric_denominator(app_main, tmp_path: Path) -> None:
    now = int(time.time())
    conn = app_main.db.connect(str(tmp_path / "distance-context.sqlite"))
    app_main.db.init_db(conn)
    app_main.db.insert_tickers(
        conn,
        [{"venue": "linear", "symbol": "BTCUSDT", "ts": now - 5, "last": 100.0, "bid": 99.9, "ask": 100.1, "vol24h": 1000.0, "turnover24h": 100000.0}],
    )

    ctx = app_main._operator_decision_context_for_reco(
        _rec(now),
        conn=conn,
        guard={"ok": True, "errors": [], "warnings": []},
    )

    assert ctx["current_price"] == pytest.approx(100.0)
    assert ctx["distance_to_lower_pct"] == pytest.approx(50.0)
    assert ctx["distance_to_upper_pct"] == pytest.approx(50.0)
    assert ctx["distance_to_kill_lower_pct"] == pytest.approx(60.0)
    assert ctx["distance_to_kill_upper_pct"] == pytest.approx(60.0)


def test_operator_bound_distance_turns_negative_after_bound_breach(app_main) -> None:
    assert app_main._distance_from_current_to_bound_pct(100.0, 105.0, side="lower") == pytest.approx(-5.0)
    assert app_main._distance_from_current_to_bound_pct(100.0, 95.0, side="upper") == pytest.approx(-5.0)
    assert app_main._distance_from_current_to_bound_pct(0.0, 95.0, side="upper") is None


def test_operator_ui_normalizes_direction_labels_and_fails_closed_for_malformed_exit_payload() -> None:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")

    assert 'const normalized = String(dir || "").trim().toLowerCase();' in app_js
    assert 'if (normalized === "short") return "Шорт";' in app_js
    assert 'exitLevels.has_directional_take_profit === true && (dir === "long" || dir === "short")' in app_js


def test_static_asset_cache_key_bumped_after_distance_semantics_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v31" in index
    assert "app.js?v=manual-ui-v31" in index
