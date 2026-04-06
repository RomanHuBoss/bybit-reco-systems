from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def reload_settings_module() -> None:
    sys.modules.pop("app.settings", None)
    yield
    sys.modules.pop("app.settings", None)


def test_load_settings_deduplicates_symbol_lists_preserving_order(
    monkeypatch: pytest.MonkeyPatch,
    reload_settings_module: None,
) -> None:
    """Конфиг не должен раздувать collector/recommender дублями символов."""
    monkeypatch.setenv("VENUES", "spot,linear")
    monkeypatch.setenv("SYMBOLS_SPOT", " btcusdt , ETHUSDT,btcUSDT , , ethusdt ,SOLUSDT ")
    monkeypatch.setenv("SYMBOLS_LINEAR", "ETHUSDT,BTCUSDT,ETHUSDT,BTCUSDT,SOLUSDT")

    settings_module = importlib.import_module("app.settings")
    settings = settings_module.load_settings()

    assert settings.symbols_spot == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert settings.symbols_linear == ["ETHUSDT", "BTCUSDT", "SOLUSDT"]


def test_env_example_documents_auto_llm_reviewer_ttl_consistently() -> None:
    """README и .env.example не должны расходиться по семантике auto-TTL."""
    root = Path(__file__).resolve().parent.parent
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "LLM_REVIEWER_TTL_SEC=" in env_example
    assert "LLM_REVIEWER_TTL_SEC=900" not in env_example
    assert "отдельный TTL валидности LLM-review" in readme
    assert "по умолчанию не короче TTL самой рекомендации" in readme
