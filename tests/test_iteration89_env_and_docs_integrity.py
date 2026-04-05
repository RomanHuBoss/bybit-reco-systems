from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from app import db
from app.risk import get_risk_limits


@pytest.mark.parametrize(
    "payload",
    [
        '{"max_concurrent_bots": NaN, "max_daily_dd_usdt": 200.0}',
        '{"max_concurrent_bots": 4, "max_daily_dd_usdt": Infinity}',
        '{"max_concurrent_bots": -Infinity, "max_daily_dd_usdt": 200.0}',
    ],
)
def test_load_settings_rejects_non_finite_risk_limits_json(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    """Настройки должны принимать только strict JSON без NaN/Infinity."""
    monkeypatch.setenv("RISK_LIMITS_JSON", payload)

    sys.modules.pop("app.settings", None)
    settings_module = importlib.import_module("app.settings")
    settings = settings_module.load_settings()

    assert settings.risk_limits == {
        "max_concurrent_bots": 4,
        "max_daily_dd_usdt": 200.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 1,
    }

    sys.modules.pop("app.settings", None)


def test_get_risk_limits_sanitizes_corrupted_fallback_when_active_limits_absent(tmp_path: Path) -> None:
    """Даже без активной записи в БД runtime не должен возвращать poisoned fallback."""
    conn = db.connect(str(tmp_path / "risk-fallback.db"))
    try:
        db.init_db(conn)

        normalized = get_risk_limits(
            conn,
            {
                "max_concurrent_bots": float("nan"),
                "max_daily_dd_usdt": float("nan"),
                "cooldown_after_loss_min": "oops",
                "max_symbol_bots": -5,
            },
        )

        assert normalized == {
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
        }
    finally:
        conn.close()


def test_readme_has_no_audit_or_test_report_artifact_references() -> None:
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "docs/audit_" not in readme
    assert "docs/test_report_" not in readme
