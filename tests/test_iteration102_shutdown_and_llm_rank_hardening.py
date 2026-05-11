from __future__ import annotations

import importlib
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from app import db
from app import recommender as recommender_module
from app.llm_review import LLMReviewResult
from app.recommender import run_llm_review_sweep_once
from app.settings import Settings


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "iter102.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


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
            confidence=0.71,
            regime_view="bullish_range",
            risk_flags=[],
            summary=f"ok {symbol}",
        )


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
        symbols_linear=["BTCUSDT"],
        risk_limits={"max_concurrent_bots": 4, "max_daily_dd_usdt": 200.0, "cooldown_after_loss_min": 30, "max_symbol_bots": 1},
        min_score_to_recommend=0.08,
        min_conf_to_recommend=0.52,
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


def _insert_reco(
    conn,
    rec_id: str,
    ts: int,
    symbol: str,
    *,
    score=0.8,
    confidence=0.72,
    llm_status: str = "pending",
):
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
            "score": score,
            "confidence": confidence,
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


# Graceful shutdown не должен трактоваться supervisor-ом как авария фонового потока.
def test_background_supervisor_treats_stop_event_as_clean_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "graceful_shutdown.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    monkeypatch.setenv("SYMBOLS_LINEAR", "")
    monkeypatch.setenv("VENUES", "linear")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main._BACKGROUND_STOP_EVENT.clear()

        def clean_target():
            app_main._BACKGROUND_STOP_EVENT.set()
            return None

        app_main._run_supervised_background_target(
            "collector",
            clean_target,
            restart_delay_sec=0,
            max_restarts=1,
            sleep_fn=lambda _: None,
            treat_return_as_error=True,
        )

        with closing(db.connect(str(db_path))) as conn:
            state = db.get_app_config_json(conn, app_main._background_thread_state_key("collector"), default={}) or {}
            errors = int(conn.execute("SELECT COUNT(*) AS c FROM decision_log WHERE action='COLLECT_ERROR'").fetchone()["c"])

        assert state["state"] == "stopped"
        assert int(state.get("restart_count") or 0) == 0
        assert errors == 0
    finally:
        app_main._BACKGROUND_STOP_EVENT.clear()
        sys.modules.pop("app.main", None)


# Broken legacy/manual numerics не должны валить async-очередь reviewer-а.
def test_mark_llm_reviews_async_sanitizes_candidate_rank_fields(conn):
    reviewer = _FakeReviewer()
    settings = _settings_for_tests(llm_reviewer_max_candidates=2)
    recs = [
        {
            "rec_id": "R-bad-rank",
            "ts": int(time.time()),
            "venue": "linear",
            "symbol": "BADUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "score": "not-a-number",
            "confidence": "NaN",
            "expected_rr": 1.2,
            "risk_score": 0.2,
            "params": {},
            "reasons": {},
            "status": "recommended",
        },
        {
            "rec_id": "R-good-rank",
            "ts": int(time.time()),
            "venue": "linear",
            "symbol": "GOODUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "score": 0.91,
            "confidence": 0.81,
            "expected_rr": 1.4,
            "risk_score": 0.15,
            "params": {},
            "reasons": {},
            "status": "recommended",
        },
    ]

    stats = recommender_module._mark_llm_reviews_async(conn, recs, settings, reviewer=reviewer)

    assert stats["queued"] == 2
    assert recs[0]["reasons"]["llm_review"]["status"] == "pending"
    assert recs[1]["reasons"]["llm_review"]["status"] == "pending"
    # Advisory reviewer is non-blocking: rank-field sanitization must still queue
    # the LLM marker, but it must not park actionable recommendations in pending.
    assert recs[0]["status"] == "recommended"
    assert recs[1]["status"] == "recommended"
    assert recs[0]["reasons"]["llm_review"]["hold_policy"] == "non_blocking_advisory"
    assert recs[1]["reasons"]["llm_review"]["hold_policy"] == "non_blocking_advisory"


# Pending-sweep должен переживать битые score/confidence в latest snapshot из SQLite.
def test_run_llm_review_sweep_sanitizes_pending_rank_fields(conn, monkeypatch: pytest.MonkeyPatch):
    ts_now = int(time.time())
    _insert_reco(conn, "R-bad-sweep", ts_now, "BADUSDT", score="bad-score", confidence="Infinity")
    _insert_reco(conn, "R-good-sweep", ts_now, "GOODUSDT", score=0.93, confidence=0.82)

    reviewer = _FakeReviewer()
    settings = _settings_for_tests(llm_reviewer_max_candidates=2)
    monkeypatch.setattr(recommender_module, "_make_llm_reviewer", lambda settings: reviewer)
    monkeypatch.setattr(recommender_module, "_load_llm_candles_for_symbol", lambda *args, **kwargs: {900: [[1, 1, 1, 1, 1, 1.0]]})

    stats = run_llm_review_sweep_once(conn, settings)
    bad = db.get_recommendation_by_id(conn, "R-bad-sweep")
    good = db.get_recommendation_by_id(conn, "R-good-sweep")

    assert stats["errors"] == 0
    assert reviewer.calls == 2
    assert bad["reasons"]["llm_review"]["status"] == "ok"
    assert good["reasons"]["llm_review"]["status"] == "ok"
