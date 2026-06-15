from __future__ import annotations

import json
import re
from pathlib import Path

from app.settings import load_settings


ROOT = Path(__file__).resolve().parent.parent


def _env_risk_limits_json() -> dict:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^RISK_LIMITS_JSON=(\{.*\})$", env, re.MULTILINE)
    assert match, ".env.example must expose a single RISK_LIMITS_JSON object"
    return json.loads(match.group(1))


def test_env_example_matches_current_shipped_3x_5x_operator_profile() -> None:
    limits = _env_risk_limits_json()

    assert limits["max_concurrent_bots"] == 1
    assert limits["max_symbol_bots"] == 1
    assert limits["min_leverage"] == 3
    assert limits["max_leverage"] == 5


def test_runtime_default_risk_profile_remains_3x_5x_when_env_absent(monkeypatch) -> None:
    monkeypatch.delenv("RISK_LIMITS_JSON", raising=False)
    settings = load_settings()

    assert settings.risk_limits["min_leverage"] == 3
    assert settings.risk_limits["max_leverage"] == 5


def test_how_to_trade_source_documents_non_oms_scope_and_3x_5x_gate() -> None:
    source = (ROOT / "docs" / "HOW_TO_TRADE_INFOGRAPHIC.md").read_text(encoding="utf-8")

    assert "recommendation/audit service, not OMS/EMS" in source
    assert "`min_leverage=3`, `max_leverage=5`" in source
    assert "3-5x is the baseline actionable leverage interval" in source
    assert "INVALID_MARKET_REFERENCE_PRICE" in source
    assert "Short: TP below entry/reference, SL above entry/reference" in source


def test_root_how_to_trade_png_exists_and_is_nonempty_png() -> None:
    png = ROOT / "how_to_trade.png"
    data = png.read_bytes()

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(data) > 100_000


def test_readme_links_operator_infographic_without_audit_artifact_references() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "how_to_trade.png" in readme
    assert "docs/HOW_TO_TRADE_INFOGRAPHIC.md" in readme
    assert "docs/audit_" not in readme
    assert "docs/test_report_" not in readme
