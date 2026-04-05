from __future__ import annotations

import pytest

from app.llm_review import build_review_payload
from app.security import KeyStore
from app.settings import load_settings


def test_keystore_rejects_malformed_fernet_key() -> None:
    """Битый MASTER_KEY должен падать на bootstrap, а не в первой live-операции."""
    with pytest.raises(ValueError, match="valid Fernet key"):
        KeyStore.from_env("not-a-valid-fernet-key")


def test_load_settings_rejects_runtime_lock_db_path_collision(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "shared.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(db_path))
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")

    with pytest.raises(RuntimeError, match="RUNTIME_LOCK_DB_PATH must differ from DB_PATH"):
        load_settings()


def test_build_review_payload_tolerates_malformed_candidate_numbers() -> None:
    """Legacy/manual мусор в score/confidence не должен ронять review sweep."""
    payload = build_review_payload(
        rec={
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "status": "recommended",
            "score": "oops",
            "confidence": "broken",
            "expected_rr": "NaN",
            "risk_score": object(),
            "params": {"grid_levels": 8, "grid_spacing_pct": 1.1},
            "reasons": {"execution_constraints": {}, "funding": {}, "open_interest": {}, "fast_veto": {}},
        },
        feature_snapshot={"range_score": 0.8},
        direction_agg={"direction": "long", "raw_direction": "long", "scores": {"all": 0.5}},
        market_shock={"state": "normal", "guard_blocks_neutral": False},
        sentiment_summary={"effective_sentiment": 0.1},
        candles_by_tf={60: [[1, 100.0, 101.0, 99.0, 100.5, 10.0]]},
    )

    assert payload["candidate"]["score"] is None
    assert payload["candidate"]["confidence"] is None
    assert payload["candidate"]["expected_rr"] is None
    assert payload["candidate"]["risk_score"] is None
