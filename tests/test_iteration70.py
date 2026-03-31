from __future__ import annotations

import importlib
import sys
from dataclasses import replace
import time
from pathlib import Path

import pytest

from app import db
from app import recommender as recommender_module
from app.collector import RuntimeLockLostError
from app.llm_review import LLMReviewResult
from app.recommender import run_llm_review_sweep_once
from app.settings import Settings


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "iter70.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def _settings_for_tests(**overrides):
    base = dict(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=["linear"],
        symbols_spot=[],
        symbols_linear=["BTCUSDT"],
        risk_limits={"max_concurrent_bots": 4, "max_daily_dd_usdt": 200.0, "cooldown_after_loss_min": 30, "max_symbol_bots": 1},
        min_score_to_recommend=0.08,
        min_conf_to_recommend=0.52,
        taker_fee_bps_spot=10.0,
        taker_fee_bps_linear=6.0,
        master_key=None,
        admin_api_key=None,
        sentiment_interval_sec=60,
        futures_collect_interval_sec=900,
        telegram_token=None,
        telegram_chat_id=None,
        require_conf_gate=False,
        llm_reviewer_enabled=True,
        llm_reviewer_mode="advisory",
        llm_reviewer_model="fake-llm",
        llm_reviewer_max_candidates=8,
        llm_reviewer_max_workers=1,
        llm_reviewer_cadence_sec=300,
        llm_reviewer_candles_per_tf=16,
        llm_reviewer_tf_secs=[900],
    )
    base.update(overrides)
    return Settings(**base)


class _FakeReviewer:
    provider = "ollama"
    model = "fake-llm"

    def __init__(self):
        self.calls = 0

    def review(self, payload):
        self.calls += 1
        symbol = str((payload or {}).get("symbol") or "")
        return LLMReviewResult(
            provider=self.provider,
            model=self.model,
            execution_direction="long",
            thesis_direction="long",
            confidence=0.74,
            regime_view="bullish_range",
            risk_flags=[],
            summary=f"ok {symbol}",
        )


def _insert_reco(conn, rec_id: str, ts: int, symbol: str, *, llm_status: str = "pending"):
    db.insert_recommendations(
        conn,
        [{
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": symbol,
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.8,
            "confidence": 0.72,
            "expected_rr": 1.3,
            "risk_score": 0.2,
            "params": {"grid_levels": 8},
            "reasons": {"llm_review": {"status": llm_status, "mode": "advisory", "gate_decision": "pending"}},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": ts,
        }],
    )


def test_run_llm_review_sweep_scopes_pending_counts_to_latest_snapshot(conn, monkeypatch: pytest.MonkeyPatch):
    ts_now = int(time.time())
    old_ts = ts_now - 120
    new_ts = ts_now
    _insert_reco(conn, "R-old-1", old_ts, "OLD1USDT")
    _insert_reco(conn, "R-old-2", old_ts, "OLD2USDT")
    _insert_reco(conn, "R-new-1", new_ts, "NEW1USDT")

    reviewer = _FakeReviewer()
    settings = _settings_for_tests()
    monkeypatch.setattr(recommender_module, "_make_llm_reviewer", lambda settings: reviewer)
    monkeypatch.setattr(recommender_module, "_load_llm_candles_for_symbol", lambda *args, **kwargs: {900: [[1, 1, 1, 1, 1, 1.0]]})

    stats = run_llm_review_sweep_once(conn, settings)

    old1 = db.get_recommendation_by_id(conn, "R-old-1")
    old2 = db.get_recommendation_by_id(conn, "R-old-2")
    new1 = db.get_recommendation_by_id(conn, "R-new-1")

    assert stats["snapshot_ts"] == new_ts
    assert stats["pending_before"] == 1
    assert stats["pending_after"] == 0
    assert reviewer.calls == 1
    assert old1["reasons"]["llm_review"]["status"] == "pending"
    assert old2["reasons"]["llm_review"]["status"] == "pending"
    assert new1["reasons"]["llm_review"]["status"] == "ok"


def test_mark_llm_reviews_async_does_not_commit_decisions_before_publish(conn):
    ts_now = int(time.time())
    cache_state = {
        "linear|BTCUSDT|futures_grid|neutral->long": {
            "ts": ts_now,
            "provider": "ollama",
            "model": "fake-llm",
            "prompt_version": recommender_module.PROMPT_VERSION,
            "thesis_direction": "long",
            "execution_direction": "long",
            "confidence": 0.81,
            "context_signature": recommender_module._llm_reviewer_context_signature(_settings_for_tests()),
            "regime_view": "bullish_range",
            "summary": "cached BTC",
            "risk_flags": [],
        }
    }
    db.set_app_config_json(conn, recommender_module.LLM_REVIEW_CACHE_APP_KEY, cache_state)
    recs = [{
        "rec_id": "R-cached-publish",
        "ts": ts_now,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "one_way",
        "margin_mode": "isolated",
        "score": 0.9,
        "confidence": 0.8,
        "expected_rr": 1.4,
        "risk_score": 0.2,
        "params": {},
        "reasons": {},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 1800,
        "model_version": "test",
        "features_ref_ts": ts_now,
    }]

    stats = recommender_module._mark_llm_reviews_async(conn, recs, _settings_for_tests(), reviewer=_FakeReviewer())
    assert stats["cached"] == 1
    assert recs[0]["reasons"]["llm_review"]["status"] == "ok"

    conn.rollback()
    cnt = int(conn.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action LIKE 'LLM_REVIEW_%'").fetchone()["c"])
    assert cnt == 0


def test_reco_thread_skips_housekeeping_after_runtime_lock_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "reco_lock_loss.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin")
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        called = {"expire": False, "prune": False}

        app_main.settings = replace(app_main.settings, reco_interval_sec=5, telegram_token=None)

        def fake_run_recommender_once(conn, settings, *, heartbeat=None):
            assert heartbeat is not None
            raise RuntimeLockLostError("lost reco leadership")

        def fake_expire(conn):
            called["expire"] = True
            return 0

        def fake_prune(conn, retain_days=7):
            called["prune"] = True
            return {}

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        monkeypatch.setattr(app_main, "run_recommender_once", fake_run_recommender_once)
        monkeypatch.setattr(app_main.db, "expire_stale_recommendations", fake_expire)
        monkeypatch.setattr(app_main.db, "prune_old_data", fake_prune)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._reco_thread()

        assert called["expire"] is False
        assert called["prune"] is False
    finally:
        sys.modules.pop("app.main", None)


def test_llm_reviewer_thread_does_not_record_runtime_lock_loss_as_model_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "llm_lock_loss.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "1")
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_SPOT", "")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(app_main.settings, llm_reviewer_enabled=True, llm_reviewer_cadence_sec=60)

        def fake_run_llm_review_sweep_once(conn, settings, *, heartbeat=None):
            assert heartbeat is not None
            raise RuntimeLockLostError("lost llm leadership")

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        monkeypatch.setattr(app_main, "run_llm_review_sweep_once", fake_run_llm_review_sweep_once)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._llm_reviewer_thread()

        conn = db.connect(str(db_path))
        try:
            state = db.get_app_config_json(conn, recommender_module.LLM_REVIEW_ASYNC_STATUS_APP_KEY, default={}) or {}
            assert state.get("error") in (None, "")
            errors = conn.execute(
                "SELECT COUNT(*) AS c FROM decision_log WHERE action='LLM_REVIEW_SWEEP_ERROR'"
            ).fetchone()["c"]
            assert int(errors) >= 1
        finally:
            conn.close()
    finally:
        sys.modules.pop("app.main", None)
