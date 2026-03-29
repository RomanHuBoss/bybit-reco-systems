from __future__ import annotations

import importlib
import json
import math
import sys
import time
from pathlib import Path

import pytest

from app import db
from app import recommender as recommender_module
from app.direction import aggregate_direction
from app.outcomes import _get_first_tradeable_candle_after
from app.llm_review import LLMReviewResult, OllamaCandleReviewer, parse_review_content, parse_tf_secs
from app.recommender import (
    _apply_llm_reviewer,
    _advance_persistence_gate,
    _estimate_cost_model,
    _extreme_funding_block,
    _params,
    _persistence_fresh_gap,
    _persistence_gate_requirements,
    _recommendation_ttl_sec,
    _score,
    _stable_range_score,
    _stabilize_direction_agg,
    PERSISTENCE_BOTS,
    run_llm_review_sweep_once,
    run_recommender_once,
)
from app.settings import Settings
from app.risk import compute_risk_status, gate_candidate, get_risk_limits
from app.shock_guard import _stabilize_market_shock, _stabilize_fast_veto, compute_market_shock, apply_market_shock_gate
from app.features import compute_features_from_ohlcv




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
    )
    base.update(overrides)
    return Settings(**base)

@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "test.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()



def test_parse_review_content_handles_json_fence_and_spot_short_execution_neutralization():
    content = """```json
    {"thesis_direction":"short","execution_direction":"short","confidence":0.81,"regime_view":"bearish_range","risk_flags":["carry_risk"],"summary":"bearish but spot cannot short"}
    ```"""

    parsed = parse_review_content(content, bot_type="spot_grid", engine_direction="neutral")

    assert parsed["thesis_direction"] == "short"
    assert parsed["execution_direction"] == "neutral"
    assert parsed["confidence"] == pytest.approx(0.81)
    assert parsed["risk_flags"] == ["carry_risk"]





def test_parse_review_content_treats_nonfinite_confidence_as_zero():
    parsed = parse_review_content(
        '{"thesis_direction":"long","execution_direction":"long","confidence":"NaN","summary":"bad conf"}',
        bot_type="futures_grid",
        engine_direction="long",
    )

    assert parsed["confidence"] == 0.0



def test_parse_tf_secs_ignores_invalid_tokens_and_keeps_supported_order():
    assert parse_tf_secs("15m,garbage,1h,999,4h,bad") == [15 * 60, 60 * 60, 4 * 60 * 60]


def test_explicit_label_horizon_is_clamped_for_grid_bots():
    from app.outcomes import _resolve_effective_horizon

    horizon_sec, used_fallback = _resolve_effective_horizon("futures_grid", {"label_horizon_hours": 240}, 1800)

    assert used_fallback is False
    assert horizon_sec == 48 * 3600


def test_alert_cooldown_is_not_consumed_when_send_fails(monkeypatch: pytest.MonkeyPatch):
    from app import alerts

    alerts._last_sent.clear()
    attempts: list[str] = []

    def _fake_send(token: str, chat_id: str, text: str) -> bool:
        attempts.append(text)
        return False

    monkeypatch.setattr(alerts, "send_telegram", _fake_send)

    payload = [{"status": "ok"}, {"status": "missing"}]
    alerts.check_and_alert("token", "chat", payload, collect_errors_10m=7, reco_count=0)
    alerts.check_and_alert("token", "chat", payload, collect_errors_10m=7, reco_count=0)

    assert len(attempts) == 6
    assert alerts._last_sent == {}


def test_load_llm_candles_for_symbol_drops_open_candle(conn):
    ts_now = 1_700_000_090
    rows = [
        {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": 1_700_000_060, "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 12.0},
        {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": 1_700_000_000, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0},
    ]
    db.upsert_ohlcv(conn, rows)

    candles = recommender_module._load_llm_candles_for_symbol(conn, "linear", "BTCUSDT", [60], 8, ts_now=ts_now)

    assert list(candles) == [60]
    assert candles[60] == [[1_700_000_000, 100.0, 101.0, 99.0, 100.5, 10.0]]


def test_ollama_reviewer_falls_back_to_generate_and_keeps_chat_diagnostics():
    class FakeReviewer(OllamaCandleReviewer):
        def __init__(self):
            super().__init__(base_url="http://127.0.0.1:11434", model="fake-llm", timeout_sec=5)

        def _request_chat(self, payload):
            raise RuntimeError("chat timeout")

        def _request_generate(self, payload):
            return (
                '{"thesis_direction":"long","execution_direction":"long","confidence":0.67,"regime_view":"bullish_range","risk_flags":[],"summary":"fallback ok"}',
                {"endpoint": "/api/generate", "done": True, "done_reason": "stop", "eval_count": 42},
            )

    reviewer = FakeReviewer()
    payload = {"candidate": {"bot_type": "futures_grid", "engine_execution_direction": "long"}}
    result = reviewer.review(payload)

    assert result.status == "ok"
    assert result.execution_direction == "long"
    assert result.diagnostics["path"] == "generate"
    assert result.diagnostics["chat_error"] == "chat timeout"
    assert result.diagnostics["generate_endpoint"] == "/api/generate"


def test_ollama_reviewer_surfaces_chat_and_generate_failures_together():
    class FakeReviewer(OllamaCandleReviewer):
        def __init__(self):
            super().__init__(base_url="http://127.0.0.1:11434", model="fake-llm", timeout_sec=5)

        def _request_chat(self, payload):
            raise RuntimeError("chat timeout")

        def _request_generate(self, payload):
            raise ValueError("ollama /api/generate returned no response (done=True, done_reason=stop, eval_count=0)")

    reviewer = FakeReviewer()
    payload = {"candidate": {"bot_type": "futures_grid", "engine_execution_direction": "neutral"}}
    result = reviewer.review(payload)

    assert result.status == "error"
    assert "chat: chat timeout" in str(result.error)
    assert "generate: ollama /api/generate returned no response" in str(result.error)
    assert result.diagnostics["chat_error"] == "chat timeout"
    assert "ollama /api/generate returned no response" in result.diagnostics["generate_error"]


def test_apply_llm_reviewer_gate_vetoes_direction_mismatch(conn):
    class FakeReviewer:
        provider = "ollama"
        model = "fake-llm"

        def review(self, payload):
            return LLMReviewResult(
                provider=self.provider,
                model=self.model,
                execution_direction="short",
                thesis_direction="short",
                confidence=0.93,
                regime_view="bearish_range",
                risk_flags=["late_breakout_risk"],
                summary="strong disagreement",
            )

    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="gate",
        llm_reviewer_model="fake-llm",
        llm_reviewer_min_confidence=0.65,
    )
    rec = {
        "rec_id": "R-llm-1",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "status": "recommended",
        "score": 0.42,
        "confidence": 0.71,
        "expected_rr": 1.6,
        "risk_score": 0.2,
        "params": {"grid_levels": 8, "grid_spacing_pct": 0.9, "price_range_lower": 95.0, "price_range_upper": 105.0},
        "reasons": {
            "feature_snapshot": {"atr_pct": 0.01, "range_score": 0.76},
            "direction_agg": {"direction": "long", "raw_direction": "long", "regime": "range", "coherence": 0.7, "trendiness": 0.2},
            "execution_constraints": {"raw_direction": "long", "executable_direction": "long", "spot_short_neutralized": False},
            "decision_layers": {"final_status": "recommended"},
            "symbol_sentiment": {"effective": 0.1, "global": 0.1},
        },
    }

    stats = _apply_llm_reviewer(
        conn,
        [rec],
        settings,
        symbol_feature_map={("linear", "BTCUSDT"): {"_direction_agg": {"direction": "long"}}},
        symbol_llm_candle_map={("linear", "BTCUSDT"): {900: [[1, 1, 1, 1, 1, 1.0]]}},
        sent_agg={"effective_score": 0.1},
        market_shock={"state": "normal"},
        reviewer=FakeReviewer(),
    )

    assert rec["status"] == "no_trade"
    assert stats["reviewed"] == 1
    assert stats["vetoed"] == 1
    assert rec["reasons"]["llm_review"]["gate_decision"] == "veto"
    assert rec["reasons"]["decision_layers"]["final_status"] == "no_trade"



def test_apply_llm_reviewer_advisory_keeps_status_and_records_alignment(conn):
    class FakeReviewer:
        provider = "ollama"
        model = "fake-llm"

        def review(self, payload):
            return LLMReviewResult(
                provider=self.provider,
                model=self.model,
                execution_direction="long",
                thesis_direction="long",
                confidence=0.74,
                regime_view="bullish_range",
                risk_flags=[],
                summary="aligned",
            )

    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="advisory",
        llm_reviewer_model="fake-llm",
    )
    rec = {
        "rec_id": "R-llm-2",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "status": "recommended",
        "score": 0.44,
        "confidence": 0.72,
        "expected_rr": 1.7,
        "risk_score": 0.18,
        "params": {"grid_levels": 8, "grid_spacing_pct": 0.9, "price_range_lower": 95.0, "price_range_upper": 105.0},
        "reasons": {
            "feature_snapshot": {"atr_pct": 0.01, "range_score": 0.76},
            "direction_agg": {"direction": "long", "raw_direction": "long", "regime": "range", "coherence": 0.7, "trendiness": 0.2},
            "execution_constraints": {"raw_direction": "long", "executable_direction": "long", "spot_short_neutralized": False},
            "decision_layers": {"final_status": "recommended"},
            "symbol_sentiment": {"effective": 0.1, "global": 0.1},
        },
    }

    stats = _apply_llm_reviewer(
        conn,
        [rec],
        settings,
        symbol_feature_map={("linear", "BTCUSDT"): {"_direction_agg": {"direction": "long"}}},
        symbol_llm_candle_map={("linear", "BTCUSDT"): {900: [[1, 1, 1, 1, 1, 1.0]]}},
        sent_agg={"effective_score": 0.1},
        market_shock={"state": "normal"},
        reviewer=FakeReviewer(),
    )

    assert rec["status"] == "recommended"
    assert stats["reviewed"] == 1
    assert stats["vetoed"] == 0
    assert rec["reasons"]["llm_review"]["agree_with_engine"] is True
    assert rec["reasons"]["decision_layers"]["llm_reviewer"]["status"] == "ok"


def test_apply_llm_reviewer_does_not_reuse_cache_across_venue_or_bot_type(conn):
    class CountingReviewer:
        provider = "ollama"
        model = "fake-llm"

        def __init__(self):
            self.calls = 0

        def review(self, payload):
            self.calls += 1
            return LLMReviewResult(
                provider=self.provider,
                model=self.model,
                execution_direction="short",
                thesis_direction="short",
                confidence=0.77,
                regime_view="bearish_range",
                risk_flags=["carry_risk"],
                summary="cached bearish thesis",
            )

    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="advisory",
        llm_reviewer_model="fake-llm",
        llm_reviewer_cadence_sec=300,
        llm_reviewer_max_candidates=4,
    )
    reviewer = CountingReviewer()
    recs = [
        {
            "rec_id": "R-llm-cache-1",
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "short",
            "status": "recommended",
            "score": 0.46,
            "confidence": 0.74,
            "expected_rr": 1.9,
            "risk_score": 0.18,
            "params": {"grid_levels": 8, "grid_spacing_pct": 0.9, "price_range_lower": 95.0, "price_range_upper": 105.0},
            "reasons": {
                "feature_snapshot": {"atr_pct": 0.01, "range_score": 0.76},
                "direction_agg": {"direction": "short", "raw_direction": "short", "regime": "range", "coherence": 0.72, "trendiness": 0.18},
                "execution_constraints": {"raw_direction": "short", "executable_direction": "short", "spot_short_neutralized": False},
                "decision_layers": {"final_status": "recommended"},
                "symbol_sentiment": {"effective": -0.1, "global": -0.1},
            },
        },
        {
            "rec_id": "R-llm-cache-2",
            "venue": "spot",
            "symbol": "BTCUSDT",
            "bot_type": "spot_grid",
            "direction": "neutral",
            "status": "recommended",
            "score": 0.38,
            "confidence": 0.69,
            "expected_rr": 1.2,
            "risk_score": 0.15,
            "params": {"grid_levels": 8, "grid_spacing_pct": 0.9, "price_range_lower": 95.0, "price_range_upper": 105.0},
            "reasons": {
                "feature_snapshot": {"atr_pct": 0.01, "range_score": 0.76},
                "direction_agg": {"direction": "neutral", "raw_direction": "short", "regime": "range", "coherence": 0.72, "trendiness": 0.18},
                "execution_constraints": {"raw_direction": "short", "executable_direction": "neutral", "spot_short_neutralized": True},
                "decision_layers": {"final_status": "recommended"},
                "symbol_sentiment": {"effective": -0.1, "global": -0.1},
            },
        },
    ]

    stats = _apply_llm_reviewer(
        conn,
        recs,
        settings,
        symbol_feature_map={("linear", "BTCUSDT"): {"_direction_agg": {"direction": "short"}}, ("spot", "BTCUSDT"): {"_direction_agg": {"direction": "neutral"}}},
        symbol_llm_candle_map={("linear", "BTCUSDT"): {900: [[1, 1, 1, 1, 1, 1.0]]}, ("spot", "BTCUSDT"): {900: [[1, 1, 1, 1, 1, 1.0]]}},
        sent_agg={"effective_score": -0.1},
        market_shock={"state": "normal"},
        reviewer=reviewer,
    )

    assert reviewer.calls == 2
    assert stats["reviewed"] == 2
    assert stats["cached"] == 0
    assert recs[1]["reasons"]["llm_review"].get("cached") is not True
    assert recs[1]["reasons"]["llm_review"]["execution_direction"] == "neutral"


def test_async_llm_sweep_gate_persists_veto_status_in_db(conn, monkeypatch):
    class FakeReviewer:
        provider = "ollama"
        model = "fake-llm"

        def review(self, payload):
            return LLMReviewResult(
                provider=self.provider,
                model=self.model,
                execution_direction="short",
                thesis_direction="short",
                confidence=0.91,
                regime_view="bearish_range",
                risk_flags=["llm_veto"],
                summary="async disagreement",
            )

    ts_now = int(time.time())
    db.insert_recommendations(
        conn,
        [{
            "rec_id": "R-async-veto",
            "ts": ts_now,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.55,
            "confidence": 0.8,
            "expected_rr": 1.4,
            "risk_score": 0.2,
            "params": {"grid_levels": 8},
            "reasons": {"llm_review": {"status": "pending", "mode": "gate", "gate_decision": "pending"}},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": ts_now,
        }],
    )

    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="gate",
        llm_reviewer_model="fake-llm",
        llm_reviewer_min_confidence=0.65,
        llm_reviewer_max_candidates=10,
    )
    monkeypatch.setattr(recommender_module, "_make_llm_reviewer", lambda settings: FakeReviewer())
    monkeypatch.setattr(recommender_module, "_load_llm_candles_for_symbol", lambda *args, **kwargs: {900: [[1, 1, 1, 1, 1, 1.0]]})

    stats = run_llm_review_sweep_once(conn, settings)
    rec = db.get_recommendation_by_id(conn, "R-async-veto")

    assert stats["completed"] == 1
    assert stats["vetoed"] == 1
    assert rec is not None
    assert rec["status"] == "no_trade"
    assert rec["reasons"]["llm_review"]["gate_decision"] == "veto"





def test_cached_llm_review_preserves_execution_direction_from_live_result(conn):
    ts_now = int(time.time())
    cached_state = {
        "linear|LINKUSDT|futures_grid|short": {
            "ts": ts_now,
            "provider": "ollama",
            "model": "fake-llm",
            "prompt_version": "ohlcv_multitf_v1",
            "thesis_direction": "short",
            "execution_direction": "neutral",
            "confidence": 0.70,
            "context_signature": "tf=900,3600,14400|candles=32",
            "regime_view": "range with weak downside bias",
            "summary": "neutral execution despite bearish thesis",
            "risk_flags": ["low_confidence"],
        }
    }
    db.set_app_config_json(conn, recommender_module.LLM_REVIEW_CACHE_APP_KEY, cached_state)

    recs = [{
        "rec_id": "R-cache-neutral-exec-1",
        "ts": ts_now,
        "venue": "linear",
        "symbol": "LINKUSDT",
        "bot_type": "futures_grid",
        "direction": "short",
        "account_mode": "one_way",
        "margin_mode": "isolated",
        "score": 0.47,
        "confidence": 0.73,
        "expected_rr": 1.18,
        "risk_score": 0.19,
        "params": {"grid_levels": 8},
        "reasons": {
            "direction_agg": {"direction": "short", "raw_direction": "short"},
            "execution_constraints": {"raw_direction": "short", "executable_direction": "short", "spot_short_neutralized": False},
        },
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 1800,
        "model_version": "test",
        "features_ref_ts": ts_now,
    }]
    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="advisory",
        llm_reviewer_model="fake-llm",
        llm_reviewer_cadence_sec=300,
        llm_reviewer_max_candidates=1,
    )

    class FakeReviewer:
        provider = "ollama"
        model = "fake-llm"

    stats = recommender_module._mark_llm_reviews_async(conn, recs, settings, reviewer=FakeReviewer())

    assert stats["cached"] == 1
    review = recs[0]["reasons"]["llm_review"]
    assert review["execution_direction"] == "neutral"
    assert review["thesis_direction"] == "short"
    assert review["agree_with_engine"] is False


def test_mark_llm_reviews_async_reuses_fresh_cache_for_new_rec_ids(conn):
    ts_now = int(time.time())
    cached_state = {
        "linear|BTCUSDT|futures_grid|long": {
            "ts": ts_now,
            "provider": "ollama",
            "model": "fake-llm",
            "prompt_version": "ohlcv_multitf_v1",
            "thesis_direction": "long",
            "execution_direction": "long",
            "confidence": 0.74,
            "context_signature": "tf=900,3600,14400|candles=32",
            "regime_view": "bullish_range",
            "summary": "reuse cached review",
            "risk_flags": ["carry_risk"],
        }
    }
    db.set_app_config_json(conn, recommender_module.LLM_REVIEW_CACHE_APP_KEY, cached_state)

    recs = [{
        "rec_id": "R-cache-inherit-1",
        "ts": ts_now,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "one_way",
        "margin_mode": "isolated",
        "score": 0.51,
        "confidence": 0.79,
        "expected_rr": 1.4,
        "risk_score": 0.18,
        "params": {"grid_levels": 8},
        "reasons": {
            "direction_agg": {"direction": "long", "raw_direction": "long"},
            "execution_constraints": {"raw_direction": "long", "executable_direction": "long", "spot_short_neutralized": False},
        },
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 1800,
        "model_version": "test",
        "features_ref_ts": ts_now,
    }]
    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="advisory",
        llm_reviewer_model="fake-llm",
        llm_reviewer_cadence_sec=300,
        llm_reviewer_max_candidates=1,
    )

    class FakeReviewer:
        provider = "ollama"
        model = "fake-llm"

    stats = recommender_module._mark_llm_reviews_async(conn, recs, settings, reviewer=FakeReviewer())

    assert stats["queued"] == 0
    assert stats["cached"] == 1
    assert stats["inherited"] == 1
    assert recs[0]["reasons"]["llm_review"]["status"] == "ok"
    assert recs[0]["reasons"]["llm_review"]["source"] == "cache_inherited"
    assert recs[0]["reasons"]["llm_review"]["summary"] == "reuse cached review"

def test_async_llm_sweep_processes_pending_backlog_across_recent_snapshots(conn, monkeypatch):
    class FakeReviewer:
        provider = "ollama"
        model = "fake-llm"

        def __init__(self):
            self.calls = 0

        def review(self, payload):
            self.calls += 1
            return LLMReviewResult(
                provider=self.provider,
                model=self.model,
                execution_direction="long",
                thesis_direction="long",
                confidence=0.72,
                regime_view="bullish_range",
                risk_flags=[],
                summary="async ok",
            )

    ts_now = int(time.time())
    rows = []
    for i, rec_ts in enumerate((ts_now - 60, ts_now), start=1):
        rows.append({
            "rec_id": f"R-async-backlog-{i}",
            "ts": rec_ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.5 - i * 0.01,
            "confidence": 0.78 - i * 0.01,
            "expected_rr": 1.3,
            "risk_score": 0.2,
            "params": {"grid_levels": 8},
            "reasons": {"llm_review": {"status": "pending", "mode": "advisory", "gate_decision": "pending"}},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": rec_ts,
        })
    db.insert_recommendations(conn, rows)

    reviewer = FakeReviewer()
    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="advisory",
        llm_reviewer_model="fake-llm",
        llm_reviewer_max_candidates=2,
    )
    monkeypatch.setattr(recommender_module, "_make_llm_reviewer", lambda settings: reviewer)
    monkeypatch.setattr(recommender_module, "_load_llm_candles_for_symbol", lambda *args, **kwargs: {900: [[1, 1, 1, 1, 1, 1.0]]})

    stats = run_llm_review_sweep_once(conn, settings)
    rec_old = db.get_recommendation_by_id(conn, "R-async-backlog-1")
    rec_new = db.get_recommendation_by_id(conn, "R-async-backlog-2")

    assert reviewer.calls == 1
    assert stats["pending_before"] == 2
    assert stats["pending_after"] == 0
    assert stats["completed"] == 2
    assert stats["inherited"] == 1
    assert rec_old["reasons"]["llm_review"]["status"] == "ok"
    assert rec_new["reasons"]["llm_review"]["status"] == "ok"
    assert rec_old["reasons"]["llm_review"]["source"] == "async_inherited"
    assert rec_new["reasons"]["llm_review"]["source"] == "async_live"


def test_recent_pending_llm_candidates_prioritizes_unique_cache_keys(conn):
    ts_now = int(time.time())
    rows = []
    for i, (symbol, rec_ts) in enumerate([
        ("BTCUSDT", ts_now),
        ("BTCUSDT", ts_now - 20),
        ("BTCUSDT", ts_now - 40),
        ("ETHUSDT", ts_now - 10),
    ], start=1):
        rows.append({
            "rec_id": f"R-pending-unique-{i}",
            "ts": rec_ts,
            "venue": "linear",
            "symbol": symbol,
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.5,
            "confidence": 0.7,
            "expected_rr": 1.2,
            "risk_score": 0.2,
            "params": {"grid_levels": 8},
            "reasons": {"llm_review": {"status": "pending", "mode": "advisory", "gate_decision": "pending"}},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": rec_ts,
        })
    db.insert_recommendations(conn, rows)

    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_model="fake-llm",
        llm_reviewer_max_candidates=2,
    )

    candidates = recommender_module._recent_pending_llm_candidates(conn, settings, 2)
    symbols = {item["symbol"] for item in candidates}
    assert symbols == {"BTCUSDT", "ETHUSDT"}
    assert len(candidates) == 4




def test_persistence_fresh_gap_allows_confirmation_across_brief_collection_gaps():
    from app import recommender as recommender_module

    recommender_module._prev_recommended = {}
    settings = _settings_for_tests(reco_interval_sec=20)
    fresh_gap = _persistence_fresh_gap(settings)

    first = _advance_persistence_gate("linear", "BTCUSDT", "futures_grid", "long", now_ts=1_700_000_000, fresh_gap=fresh_gap)
    second = _advance_persistence_gate("linear", "BTCUSDT", "futures_grid", "long", now_ts=1_700_000_120, fresh_gap=fresh_gap)

    assert fresh_gap >= 180
    assert first == 1
    assert second == 2


def test_persistence_gate_allows_immediate_publish_for_high_quality_signal():
    settings = _settings_for_tests()
    rec = {
        "score": 0.19,
        "confidence": 0.67,
        "expected_rr": 0.22,
        "reasons": {
            "direction_agg": {
                "coherence": 0.72,
                "regime_confidence": 0.63,
            }
        },
    }

    required_hits, mode = _persistence_gate_requirements(rec, settings)

    assert required_hits == 1
    assert mode == "high_quality_signal"


def test_recommendation_ttl_defaults_to_reco_cadence_not_collect_cadence():
    settings = _settings_for_tests(collect_interval_sec=20, reco_interval_sec=240)

    ttl_sec = _recommendation_ttl_sec(settings)

    assert ttl_sec == 3600


def test_recommendation_ttl_respects_explicit_override():
    settings = _settings_for_tests(reco_interval_sec=20, reco_ttl_sec=1800)

    ttl_sec = _recommendation_ttl_sec(settings)

    assert ttl_sec == 1800


def _bot(
    *,
    bot_id: str,
    origin_rec_id: str | None,
    bot_type: str = "futures_grid",
    venue: str = "linear",
    symbol: str = "BTCUSDT",
    status: str = "running",
    state: dict | None = None,
):
    return {
        "bot_id": bot_id,
        "started_ts": int(time.time()),
        "stopped_ts": None,
        "venue": venue,
        "symbol": symbol,
        "bot_type": bot_type,
        "mode": {"direction": "long"},
        "params": {"grid_levels": 8},
        "state": state or {"marker": bot_id},
        "status": status,
        "origin_rec_id": origin_rec_id,
    }



def test_active_bot_queries_ignore_unsupported_legacy_bots(conn):
    assert db.insert_bot_instance(conn, _bot(bot_id="B-supported", origin_rec_id="R-supported")) == "inserted"
    assert db.insert_bot_instance(
        conn,
        _bot(bot_id="B-legacy", origin_rec_id="R-legacy", bot_type="legacy_grid"),
    ) == "inserted"

    active = db.get_active_bots(conn)
    assert [row["bot_id"] for row in active] == ["B-supported"]
    assert db.count_active_bots_for_symbol(conn, "linear", "BTCUSDT") == 1



def test_legacy_bots_do_not_pollute_risk_gates(conn, monkeypatch):
    monkeypatch.setenv("RISK_DAY_TZ", "UTC")
    assert db.insert_bot_instance(
        conn,
        _bot(bot_id="B-legacy", origin_rec_id="R-legacy", bot_type="legacy_grid"),
    ) == "inserted"

    limits = {
        "max_concurrent_bots": 1,
        "max_daily_dd_usdt": 1_000_000.0,
        "cooldown_after_loss_min": 0,
        "max_symbol_bots": 1,
    }

    rs = compute_risk_status(conn, limits)
    assert rs.active_bots == 0
    assert gate_candidate(conn, "linear", "BTCUSDT", limits) == []



def test_insert_bot_instance_is_idempotent_for_same_origin_rec(conn):
    first = _bot(bot_id="B-1", origin_rec_id="R-1", state={"marker": "first"})
    second = _bot(bot_id="B-2", origin_rec_id="R-1", state={"marker": "second"})

    assert db.insert_bot_instance(conn, first) == "inserted"
    assert db.insert_bot_instance(conn, second) == "duplicate_origin"

    rows = conn.execute(
        "SELECT bot_id, origin_rec_id, state_json FROM bot_instances ORDER BY started_ts ASC, bot_id ASC"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["bot_id"] == "B-1"
    assert json.loads(rows[0]["state_json"]) == {"marker": "first"}



def test_insert_bot_instance_detects_duplicate_bot_id(conn):
    bot = _bot(bot_id="B-dup", origin_rec_id="R-dup", state={"marker": "same"})
    assert db.insert_bot_instance(conn, bot) == "inserted"
    assert db.insert_bot_instance(conn, bot) == "duplicate_bot_id"

    changed = _bot(bot_id="B-dup", origin_rec_id="R-dup", state={"marker": "changed"})
    with pytest.raises(ValueError):
        db.insert_bot_instance(conn, changed)



def test_risk_status_uses_peak_to_trough_drawdown_and_trade_losses_for_cooldown(conn, monkeypatch):
    monkeypatch.setenv("RISK_DAY_TZ", "UTC")
    now = int(time.time())
    db.insert_trade(conn, {"trade_id": "T1", "bot_id": "B1", "ts": now - 120, "symbol": "BTCUSDT", "pnl": 300.0, "fee": 0.0, "meta": {}})
    db.insert_trade(conn, {"trade_id": "T2", "bot_id": "B1", "ts": now - 30, "symbol": "BTCUSDT", "pnl": -250.0, "fee": 0.0, "meta": {}})

    limits = {
        "max_concurrent_bots": 10,
        "max_daily_dd_usdt": 1_000_000.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 10,
    }
    rs = compute_risk_status(conn, limits)
    assert rs.daily_pnl == 50.0
    assert rs.daily_dd == 250.0
    assert rs.cooldown_active is True



def test_estimate_cost_model_rolls_stale_funding_forward_and_counts_crossed_events():
    now = int(time.time())
    base_args = {
        "bot_type": "futures_grid",
        "venue": "linear",
        "f": {"spread_bps": 1.0},
        "taker_fee_bps": 6.0,
        "direction": "long",
        "funding_rate": 0.0005,
        "ts_now": now,
    }

    stale = _estimate_cost_model(next_funding_ts=now - 3600, **base_args)
    assert stale["next_funding_ts"] > now
    assert stale["expected_funding_events"] == 0
    assert stale["expected_funding_bps"] == 0.0

    imminent = _estimate_cost_model(next_funding_ts=now + 3600, **base_args)
    assert imminent["expected_funding_events"] == 1
    assert imminent["expected_funding_bps"] == pytest.approx(5.0)

    short = _estimate_cost_model(next_funding_ts=now + 3600, direction="short", **{k: v for k, v in base_args.items() if k != "direction"})
    assert short["expected_funding_bps"] == pytest.approx(-5.0)



def test_get_first_tradeable_candle_after_uses_next_candle_open(conn):
    base_ts = 1_700_000_000
    rows = [
        {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": base_ts + 0, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 10.0},
        {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": base_ts + 60, "open": 101.5, "high": 103.0, "low": 100.0, "close": 102.0, "volume": 11.0},
        {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": base_ts + 120, "open": 102.5, "high": 104.0, "low": 101.0, "close": 103.0, "volume": 12.0},
    ]
    db.upsert_ohlcv(conn, rows)

    tradeable = _get_first_tradeable_candle_after(conn, "linear", "BTCUSDT", base_ts)
    assert tradeable == (base_ts + 60, 101.5)



def test_persistence_gate_enabled_for_grid_bots():
    assert {"spot_grid", "futures_grid"}.issubset(PERSISTENCE_BOTS)


def test_stable_range_score_prefers_multi_tf_context_over_noisy_1m_proxy():
    f = {"range_score": 0.18, "trend_strength": 0.70}
    agg = {"trendiness": 0.12, "coherence": 0.72, "regime": "range"}

    stable, meta = _stable_range_score(f, agg)

    assert meta["raw_range_score_1m"] == pytest.approx(0.18)
    assert meta["multi_tf_range_score"] > 0.85
    assert stable > 0.70


def test_score_uses_stable_range_score_to_avoid_false_no_trade_near_threshold():
    f = {
        "range_score": 0.18,
        "trend_strength": 0.70,
        "atr_pct": 0.008,
        "_atr_pct_1h": 0.008,
        "spread_bps": 1.2,
        "_direction_agg": {
            "direction": "neutral",
            "trendiness": 0.12,
            "coherence": 0.72,
            "regime": "range",
            "regime_confidence": 0.74,
            "strength": {"all": 0.08},
        },
    }
    cost_model = {"spread_bps": 1.2, "execution_cost_bps": 3.0, "total_cost_bps": 3.0, "net_cost_bps": 3.0}

    score, _, reasons = _score("futures_grid", "linear", f, taker_fee_bps=6.0, global_sent=0.0, cost_model=cost_model)

    assert score > 0.08
    assert reasons["score_components"]["range_score_meta"]["multi_tf_range_score"] > reasons["score_components"]["range_score_meta"]["raw_range_score_1m"]


def test_direction_hysteresis_holds_previous_direction_on_weak_flip():
    prev = {"ts": 1_700_000_000, "direction": "long", "bias": "long", "score_all": 0.19, "trendiness": 0.44, "coherence": 0.66}
    agg = {
        "direction": "short",
        "bias": "short",
        "scores": {"all": -0.13, "tactical": -0.14, "structural": -0.11},
        "strength": {"all": 0.13, "tactical": 0.14, "structural": 0.11},
        "trendiness": 0.36,
        "coherence": 0.54,
        "regime": "transition",
        "regime_confidence": 0.55,
    }

    stable, state = _stabilize_direction_agg(agg, prev_state=prev, now_ts=1_700_000_040, fresh_gap=120)

    assert stable["raw_direction"] == "short"
    assert stable["direction"] == "long"
    assert stable["direction_stability"]["applied"] is True
    assert stable["direction_stability"]["mode"] == "hysteresis_hold"
    assert state["direction"] == "long"


def test_aggregate_direction_allows_coherent_range_biased_long_signal():
    tf_map = {
        15 * 60: {"score": 0.18, "trend_strength": 0.12},
        30 * 60: {"score": 0.16, "trend_strength": 0.14},
        60 * 60: {"score": 0.19, "trend_strength": 0.16},
        4 * 60 * 60: {"score": 0.17, "trend_strength": 0.15},
        24 * 60 * 60: {"score": 0.15, "trend_strength": 0.14},
    }

    agg = aggregate_direction(tf_map)

    assert agg["regime"] == "range"
    assert agg["bias"] == "long"
    assert agg["direction"] == "long"
    assert agg["direction_mode"] == "range_biased"


def test_aggregate_direction_allows_coherent_range_biased_short_signal():
    tf_map = {
        15 * 60: {"score": -0.18, "trend_strength": 0.12},
        30 * 60: {"score": -0.16, "trend_strength": 0.14},
        60 * 60: {"score": -0.19, "trend_strength": 0.16},
        4 * 60 * 60: {"score": -0.17, "trend_strength": 0.15},
        24 * 60 * 60: {"score": -0.15, "trend_strength": 0.14},
    }

    agg = aggregate_direction(tf_map)

    assert agg["regime"] == "range"
    assert agg["bias"] == "short"
    assert agg["direction"] == "short"
    assert agg["direction_mode"] == "range_biased"


def test_stabilize_direction_preserves_strong_range_biased_short_signal():
    agg = {
        "direction": "short",
        "bias": "short",
        "scores": {"all": -0.17, "tactical": -0.18, "structural": -0.15},
        "strength": {"all": 0.17, "tactical": 0.18, "structural": 0.15},
        "trendiness": 0.14,
        "coherence": 0.86,
        "regime": "range",
        "regime_confidence": 0.68,
        "direction_mode": "range_biased",
    }

    stable, state = _stabilize_direction_agg(agg, prev_state=None, now_ts=1_700_000_000, fresh_gap=120)

    assert stable["raw_direction"] == "short"
    assert stable["direction"] == "short"
    assert stable["direction_stability"]["directional_range_allowed"] is True
    assert state["direction"] == "short"


def test_market_shock_release_uses_cooldown():
    prev = {"ts": 1_700_000_000, "state": "red_down", "severity": "lockdown", "bias": "down"}
    raw = {"ts": 1_700_000_060, "state": "normal", "severity": "normal", "bias": "neutral", "reasons": []}

    stable = _stabilize_market_shock(raw, prev, now_ts=1_700_000_060, hold_sec=180)

    assert stable["raw_state"] == "normal"
    assert stable["state"] == "red_down"
    assert stable["stabilization"]["applied"] is True
    assert stable["stabilization"]["mode"] == "release_cooldown"


def test_fast_veto_release_uses_cooldown():
    prev = {"ts": 1_700_000_000, "state": "down_break", "triggered": True}
    raw = {"state": "normal", "triggered": False, "blocks": [], "metrics": {}}

    stable = _stabilize_fast_veto(raw, prev, now_ts=1_700_000_050, release_sec=120)

    assert stable["triggered"] is True
    assert stable["state"] == "down_break"
    assert stable["stabilization"]["applied"] is True
    assert stable["blocks"][0]["code"] == "FAST_VETO_RELEASE_COOLDOWN"





def _seed_ohlcv_trend(conn, *, venue: str, symbol: str, now_ts: int, tf_sec: int, n: int, base_price: float, drift_per_bar: float) -> None:
    rows = []
    start_ts = now_ts - tf_sec * (n + 2)
    for i in range(n):
        ts = start_ts + i * tf_sec
        mid = base_price * ((1.0 + drift_per_bar) ** i)
        open_px = mid * 0.9992
        close_px = mid * 1.0008
        high_px = close_px * 1.0007
        low_px = open_px * 0.9993
        rows.append({
            "venue": venue,
            "symbol": symbol,
            "tf_sec": tf_sec,
            "ts": ts,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": 1000.0 + i,
        })
    db.upsert_ohlcv(conn, rows)


def test_market_shock_downgrades_weak_guard_when_symbol_coverage_is_too_low(conn):
    now = int(time.time())
    _seed_ohlcv_trend(conn, venue="linear", symbol="BTCUSDT", now_ts=now, tf_sec=60, n=40, base_price=50_000.0, drift_per_bar=0.002)

    settings = Settings(
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
        symbols_linear=["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT"],
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
    )

    shock = compute_market_shock(
        conn,
        settings,
        sent_agg={"regime": "risk_on", "strength": 0.60, "flags": {}},
        symbol_feature_map={("linear", "BTCUSDT"): {"volume_z": 0.9, "spread_bps": 1.0}},
        ts_now=now,
    )

    assert shock["state"] == "normal"
    assert shock["metrics"]["active_symbols"] == 1
    assert any(r["code"] == "LOW_COVERAGE" for r in shock["reasons"])


def test_market_shock_resets_to_normal_when_1m_data_is_stale(conn):
    now = int(time.time())
    stale_now = now - 3 * 3600
    _seed_ohlcv_trend(conn, venue="linear", symbol="BTCUSDT", now_ts=stale_now, tf_sec=60, n=40, base_price=50_000.0, drift_per_bar=0.002)

    settings = Settings(
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
    )

    shock = compute_market_shock(conn, settings, sent_agg={}, symbol_feature_map={}, ts_now=now)

    assert shock["state"] == "normal"
    assert shock["metrics"]["active_symbols"] == 0
    assert any(r["code"] == "NO_FRESH_1M_DATA" for r in shock["reasons"])


def test_apply_market_shock_gate_blocks_neutral_only_in_strong_guard_mode():
    moderate_guard = {
        "state": "amber_up",
        "title": "Осторожно: рынок выстреливает вверх",
        "reasons": [{"code": "BTC_5M_UP", "msg": "BTC 5m=+1.00%"}],
        "guard_blocks_neutral": False,
    }
    strong_guard = dict(moderate_guard, guard_blocks_neutral=True)

    assert apply_market_shock_gate(moderate_guard, "linear", "futures_grid", "neutral") == []
    assert apply_market_shock_gate(moderate_guard, "linear", "futures_grid", "short")[0]["code"] == "MARKET_GUARDED_UP"
    assert apply_market_shock_gate(strong_guard, "linear", "futures_grid", "neutral")[0]["code"] == "MARKET_GUARDED_UP_NEUTRAL"

def _seed_ohlcv_wave(conn, *, venue: str, symbol: str, now_ts: int, tf_sec: int, n: int, base_price: float) -> None:
    rows = []
    start_ts = now_ts - tf_sec * (n + 2)
    for i in range(n):
        ts = start_ts + i * tf_sec
        mid = base_price * (1.0 + 0.0020 * math.sin(i / 8.0) + 0.0005 * math.sin(i / 3.0))
        open_px = mid * (1.0 + 0.0002 * math.sin(i))
        close_px = mid * (1.0 + 0.0002 * math.cos(i))
        high_px = max(open_px, close_px) * 1.0015
        low_px = min(open_px, close_px) * 0.9985
        rows.append({
            "venue": venue,
            "symbol": symbol,
            "tf_sec": tf_sec,
            "ts": ts,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": 1000.0 + i,
        })
    db.upsert_ohlcv(conn, rows)


def _seed_ohlcv_bullish_range(conn, *, venue: str, symbol: str, now_ts: int, tf_sec: int, n: int, base_price: float) -> None:
    rows = []
    start_ts = now_ts - tf_sec * (n + 2)
    for i in range(n):
        ts = start_ts + i * tf_sec
        drift = 0.00002 * i
        cyc = 0.0020 * math.sin(i / 8.0)
        cyc2 = 0.0007 * math.sin(i / 2.7)
        mid = base_price * (1.0 + drift + cyc + cyc2)
        open_px = mid * (1.0 + 0.00025 * math.sin(i / 3.0))
        close_px = mid * (1.0 + 0.00025 * math.cos(i / 4.0))
        high_px = max(open_px, close_px) * 1.0013
        low_px = min(open_px, close_px) * 0.9987
        rows.append({
            "venue": venue,
            "symbol": symbol,
            "tf_sec": tf_sec,
            "ts": ts,
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": 1000.0 + i,
        })
    db.upsert_ohlcv(conn, rows)



def test_params_uses_direction_aggregate_range_score_without_name_error():
    f = {
        "price": 50_000.0,
        "atr_pct": 0.008,
        "range_score": 0.18,
        "trend_strength": 0.70,
        "_direction_agg": {
            "direction": "neutral",
            "trendiness": 0.12,
            "coherence": 0.72,
            "regime": "range",
            "regime_confidence": 0.74,
            "strength": {"all": 0.08},
        },
    }

    params = _params(
        "futures_grid",
        "linear",
        f,
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=6.0,
        direction_bias="neutral",
        direction_bias_strength=0.08,
        atr_pct_for_grid=0.008,
        cost_model={"execution_cost_bps": 6.0, "total_cost_bps": 6.0, "net_cost_bps": 6.0},
    )

    assert params["grid_levels"] >= 10
    assert params["price_range_upper"] > params["price_range_lower"]
    assert params["grid_spacing_pct"] > 0.0



def test_run_recommender_once_smoke_generates_recommendations_without_runtime_name_error(conn):
    now = int(time.time())
    symbol = "BTCUSDT"
    venue = "linear"
    base_price = 50_000.0

    for tf_sec, n in ((60, 220), (900, 120), (1800, 120), (3600, 120), (14_400, 100), (86_400, 100)):
        _seed_ohlcv_wave(conn, venue=venue, symbol=symbol, now_ts=now, tf_sec=tf_sec, n=n, base_price=base_price)

    db.insert_tickers(conn, [{
        "venue": venue,
        "symbol": symbol,
        "ts": now,
        "last": base_price,
        "bid": base_price - 5.0,
        "ask": base_price + 5.0,
        "vol24h": 12_345.0,
        "turnover24h": 5_000_000.0,
    }])
    db.upsert_funding_rate(conn, [{
        "symbol": symbol,
        "ts": now,
        "funding_rate": 0.0001,
        "next_funding_ts": now + 4 * 3600,
    }])

    settings = Settings(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=[venue],
        symbols_spot=[],
        symbols_linear=[symbol],
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
        require_conf_gate=True,
    )

    result = run_recommender_once(conn, settings)

    assert result["count"] >= 1
    assert result["count_recommended"] + result["count_blocked"] + result["count_no_trade"] + result["count_suppressed"] == result["count"]
    rows = conn.execute("SELECT rec_id, status, params_json FROM recommendations ORDER BY ts DESC").fetchall()
    assert rows
    params = json.loads(rows[0]["params_json"])
    assert params["price_range_upper"] > params["price_range_lower"]
    assert params["grid_levels"] >= 4


def test_run_recommender_once_emits_long_for_bullish_range_market(conn):
    now = int(time.time())
    symbol = "BTCUSDT"
    base_price = 50_000.0

    for venue in ("spot", "linear"):
        for tf_sec, n in ((60, 220), (900, 120), (1800, 120), (3600, 120), (14_400, 100), (86_400, 100)):
            _seed_ohlcv_bullish_range(conn, venue=venue, symbol=symbol, now_ts=now, tf_sec=tf_sec, n=n, base_price=base_price)
        db.insert_tickers(conn, [{
            "venue": venue,
            "symbol": symbol,
            "ts": now,
            "last": base_price,
            "bid": base_price - 5.0,
            "ask": base_price + 5.0,
            "vol24h": 12_345.0,
            "turnover24h": 5_000_000.0,
        }])

    db.upsert_funding_rate(conn, [{
        "symbol": symbol,
        "ts": now,
        "funding_rate": 0.0001,
        "next_funding_ts": now + 4 * 3600,
    }])

    settings = Settings(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=["spot", "linear"],
        symbols_spot=[symbol],
        symbols_linear=[symbol],
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
    )

    result = run_recommender_once(conn, settings)

    assert result["count"] >= 2
    rows = conn.execute(
        "SELECT venue, bot_type, direction, reasons_json FROM recommendations ORDER BY venue, bot_type"
    ).fetchall()
    assert rows
    by_key = {(row["venue"], row["bot_type"]): row for row in rows}

    spot_row = by_key[("spot", "spot_grid")]
    spot_reasons = json.loads(spot_row["reasons_json"])
    assert spot_row["direction"] == "long"
    assert spot_reasons["direction_agg"]["direction"] == "long"
    assert spot_reasons["direction_agg"]["regime"] == "range"
    assert spot_reasons["direction_agg"]["direction_mode"] == "range_biased"

    fut_row = by_key[("linear", "futures_grid")]
    fut_reasons = json.loads(fut_row["reasons_json"])
    assert fut_row["direction"] == "long"
    assert fut_reasons["direction_agg"]["direction"] == "long"
    assert fut_reasons["direction_agg"]["direction_mode"] == "range_biased"


def test_extreme_funding_block_is_symmetric_for_the_paying_side():
    fr_sig = {"value": 0.0005, "signal": "bearish"}

    long_cost = {
        "expected_funding_events": 1,
        "expected_funding_bps": 6.5,
    }
    short_receiving_cost = {
        "expected_funding_events": 1,
        "expected_funding_bps": -6.5,
    }
    short_paying_cost = {
        "expected_funding_events": 1,
        "expected_funding_bps": 6.5,
    }

    long_block = _extreme_funding_block("long", fr_sig, long_cost)
    assert long_block is not None
    assert long_block["code"] == "FUNDING_EXTREME"

    short_receiving_block = _extreme_funding_block("short", fr_sig, short_receiving_cost)
    assert short_receiving_block is None

    short_paying_block = _extreme_funding_block("short", fr_sig, short_paying_cost)
    assert short_paying_block is not None
    assert short_paying_block["code"] == "FUNDING_EXTREME"


def _reco_row(*, rec_id: str, ts: int, venue: str, symbol: str, bot_type: str, direction: str, reasons: dict | None = None):
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": venue,
        "symbol": symbol,
        "bot_type": bot_type,
        "direction": direction,
        "account_mode": "demo",
        "margin_mode": "cash" if venue == "spot" else "isolated",
        "score": 0.42,
        "confidence": 0.61,
        "expected_rr": 1.4,
        "risk_score": 0.2,
        "params": {"grid_levels": 8, "grid_spacing_pct": 0.9, "price_range_lower": 95.0, "price_range_upper": 105.0},
        "reasons": reasons or {},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "test",
        "features_ref_ts": ts,
    }


def test_outcomes_stats_separate_true_neutral_from_spot_short_neutralized(conn):
    now = int(time.time())
    db.insert_recommendations(conn, [
        _reco_row(
            rec_id="R-neutralized",
            ts=now - 200,
            venue="spot",
            symbol="ETHUSDT",
            bot_type="spot_grid",
            direction="neutral",
            reasons={
                "execution_constraints": {
                    "raw_direction": "short",
                    "executable_direction": "neutral",
                },
                "llm_review": {
                    "status": "ok",
                    "provider": "ollama",
                    "model": "qwen3:8b",
                    "mode": "advisory",
                    "gate_decision": "pass",
                    "agree_with_engine": True,
                    "confidence": 0.71,
                    "thesis_direction": "short",
                    "execution_direction": "neutral",
                    "regime_view": "bearish_range",
                    "risk_flags": ["carry_risk"],
                    "summary": "spot short neutralized",
                },
            },
        ),
        _reco_row(
            rec_id="R-true-neutral",
            ts=now - 180,
            venue="spot",
            symbol="BTCUSDT",
            bot_type="spot_grid",
            direction="neutral",
            reasons={
                "execution_constraints": {
                    "raw_direction": "neutral",
                    "executable_direction": "neutral",
                },
                "llm_review": {
                    "status": "error",
                    "provider": "ollama",
                    "model": "qwen3:8b",
                    "mode": "advisory",
                    "gate_decision": "pass",
                    "agree_with_engine": None,
                    "confidence": 0.0,
                    "thesis_direction": "neutral",
                    "execution_direction": "neutral",
                    "regime_view": "flat",
                    "risk_flags": [],
                    "summary": None,
                    "error": "timeout",
                },
            },
        ),
        _reco_row(
            rec_id="R-futures-long",
            ts=now - 160,
            venue="linear",
            symbol="SOLUSDT",
            bot_type="futures_grid",
            direction="long",
            reasons={
                "execution_constraints": {
                    "raw_direction": "long",
                    "executable_direction": "long",
                },
                "llm_review": {
                    "status": "ok",
                    "provider": "ollama",
                    "model": "qwen3:8b",
                    "mode": "gate",
                    "gate_decision": "veto",
                    "agree_with_engine": False,
                    "confidence": 0.82,
                    "thesis_direction": "short",
                    "execution_direction": "short",
                    "regime_view": "bearish_range",
                    "risk_flags": ["late_breakout_risk"],
                    "summary": "llm disagrees with engine",
                },
            },
        ),
    ])

    db.insert_outcome(conn, {
        "rec_id": "R-neutralized",
        "ts": now - 200,
        "venue": "spot",
        "symbol": "ETHUSDT",
        "bot_type": "spot_grid",
        "direction": "neutral",
        "horizon_sec": 3600,
        "entry_close": 100.0,
        "exit_close": 99.5,
        "ret": -0.012,
        "success": 0,
    })
    db.insert_outcome(conn, {
        "rec_id": "R-true-neutral",
        "ts": now - 180,
        "venue": "spot",
        "symbol": "BTCUSDT",
        "bot_type": "spot_grid",
        "direction": "neutral",
        "horizon_sec": 3600,
        "entry_close": 100.0,
        "exit_close": 100.6,
        "ret": 0.008,
        "success": 1,
    })
    db.insert_outcome(conn, {
        "rec_id": "R-futures-long",
        "ts": now - 160,
        "venue": "linear",
        "symbol": "SOLUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "horizon_sec": 3600,
        "entry_close": 100.0,
        "exit_close": 101.0,
        "ret": 0.015,
        "success": 1,
    })

    stats = db.get_outcomes_stats(conn)

    assert stats["summary"]["total"] == 3
    assert stats["summary"]["true_neutral_total"] == 1
    assert stats["summary"]["spot_short_neutralized_total"] == 1
    assert stats["llm_summary"]["present_total"] == 3
    assert stats["llm_summary"]["ok_total"] == 2
    assert stats["llm_summary"]["agree_total"] == 1
    assert stats["llm_summary"]["disagree_total"] == 1
    assert stats["llm_summary"]["error_total"] == 1
    assert stats["llm_summary"]["veto_total"] == 1

    pair_map = {
        (row["raw_direction"], row["execution_direction"], row["neutral_source"]): row
        for row in stats["direction_pairs"]
    }
    assert pair_map[("short", "neutral", "spot_short_neutralized")]["total"] == 1
    assert pair_map[("neutral", "neutral", "true_neutral")]["total"] == 1
    assert pair_map[("long", "long", "")]["total"] == 1

    by_bot_map = {
        (row["bot_type"], row["raw_direction"], row["execution_direction"]): row
        for row in stats["by_bot"]
    }
    assert by_bot_map[("spot_grid", "short", "neutral")]["total"] == 1
    assert by_bot_map[("spot_grid", "neutral", "neutral")]["wins"] == 1
    assert by_bot_map[("futures_grid", "long", "long")]["wins"] == 1

    llm_map = {
        (row["llm_status"], row["llm_execution_direction"], row["llm_alignment"], row["llm_gate_decision"]): row
        for row in stats["llm_alignment"]
    }
    assert llm_map[("ok", "neutral", "agree", "pass")]["total"] == 1
    assert llm_map[("ok", "short", "disagree", "veto")]["total"] == 1
    assert llm_map[("error", "neutral", "unknown", "pass")]["total"] == 1

    recent_map = {row["rec_id"]: row for row in stats["recent"]}
    assert recent_map["R-neutralized"]["raw_direction"] == "short"
    assert recent_map["R-neutralized"]["execution_direction"] == "neutral"
    assert recent_map["R-neutralized"]["neutral_source"] == "spot_short_neutralized"
    assert recent_map["R-neutralized"]["llm_review"]["status"] == "ok"
    assert recent_map["R-neutralized"]["llm_review"]["agree_with_engine"] is True
    assert recent_map["R-futures-long"]["llm_review"]["gate_decision"] == "veto"


def test_outcomes_stats_fallback_to_outcome_direction_when_recommendation_missing(conn):
    now = int(time.time())
    db.insert_outcome(conn, {
        "rec_id": "R-missing-rec",
        "ts": now - 100,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "short",
        "horizon_sec": 1800,
        "entry_close": 100.0,
        "exit_close": 99.0,
        "ret": 0.01,
        "success": 1,
    })

    stats = db.get_outcomes_stats(conn)
    assert stats["summary"]["total"] == 1
    assert stats["summary"]["wins"] == 1
    assert stats["direction_pairs"][0]["raw_direction"] == "short"
    assert stats["direction_pairs"][0]["execution_direction"] == "short"
    assert stats["recent"][0]["raw_direction"] == "short"
    assert stats["recent"][0]["execution_direction"] == "short"


def test_run_llm_review_sweep_once_updates_latest_snapshot_asynchronously(conn, monkeypatch):
    from app import recommender as recommender_module

    class FakeReviewer:
        provider = "ollama"
        model = "fake-llm"

        def review(self, payload):
            return LLMReviewResult(
                provider=self.provider,
                model=self.model,
                execution_direction="neutral",
                thesis_direction="neutral",
                confidence=0.71,
                regime_view="range",
                risk_flags=["ok"],
                summary="async ok",
            )

    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode="advisory",
        llm_reviewer_model="fake-llm",
        llm_reviewer_max_candidates=60,
        llm_reviewer_max_workers=4,
        llm_reviewer_cadence_sec=5,
        llm_reviewer_tf_secs=[900],
        llm_reviewer_candles_per_tf=16,
    )

    ts_now = int(time.time())
    rec = {
        "rec_id": "R-async-1",
        "ts": ts_now,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.41,
        "confidence": 0.73,
        "expected_rr": 1.5,
        "risk_score": 0.17,
        "params": {"grid_levels": 8, "grid_spacing_pct": 0.9, "price_range_lower": 95.0, "price_range_upper": 105.0},
        "reasons": {
            "feature_snapshot": {"atr_pct": 0.01, "range_score": 0.76},
            "direction_agg": {"direction": "neutral", "raw_direction": "neutral", "regime": "range", "coherence": 0.7, "trendiness": 0.2},
            "execution_constraints": {"raw_direction": "neutral", "executable_direction": "neutral", "spot_short_neutralized": False},
            "decision_layers": {"final_status": "recommended"},
            "symbol_sentiment": {"effective": 0.1, "global": 0.1},
            "market_shock": {"state": "normal"},
            "llm_review": {"status": "pending", "mode": "advisory", "queued_ts": ts_now},
        },
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "test",
        "features_ref_ts": ts_now,
    }
    db.insert_recommendations(conn, [rec])
    db.upsert_ohlcv(conn, [{
        "venue": "linear", "symbol": "BTCUSDT", "tf_sec": 900, "ts": ts_now,
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 123.0,
    }])

    monkeypatch.setattr(recommender_module, "_make_llm_reviewer", lambda settings: FakeReviewer())

    stats = run_llm_review_sweep_once(conn, settings)
    saved = db.get_recommendation_by_id(conn, "R-async-1")

    assert stats["queued"] == 1
    assert stats["completed"] == 1
    assert saved is not None
    assert saved["reasons"]["llm_review"]["status"] == "ok"
    assert saved["reasons"]["llm_review"]["summary"] == "async ok"



def test_compute_outcomes_scans_past_stuck_unprocessable_rows(conn):
    from app.outcomes import compute_outcomes_once

    now = db.now_ts()
    stuck_ts = now - 9 * 3600
    good_ts = now - 8 * 3600

    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": "R-stuck-old",
                "ts": stuck_ts,
                "venue": "linear",
                "symbol": "ETHUSDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.1,
                "confidence": 0.6,
                "expected_rr": 1.2,
                "risk_score": 0.2,
                "params": {"grid_levels": 5, "grid_spacing_pct": 1.0},
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": stuck_ts,
            },
            {
                "rec_id": "R-good-newer",
                "ts": good_ts,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.2,
                "confidence": 0.62,
                "expected_rr": 1.1,
                "risk_score": 0.25,
                "params": {"grid_levels": 5, "grid_spacing_pct": 1.0},
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": good_ts,
            },
        ],
    )

    entry_ts = good_ts + 60
    exit_ts = entry_ts + 6 * 3600
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": entry_ts,
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.2,
                "volume": 10.0,
            },
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": exit_ts,
                "open": 100.1,
                "high": 100.5,
                "low": 99.8,
                "close": 100.0,
                "volume": 8.0,
            },
        ],
    )

    done = compute_outcomes_once(conn, horizon_sec=30 * 60, max_to_process=1)

    assert done == 1
    assert db.outcome_exists(conn, "R-good-newer") is True
    assert db.outcome_exists(conn, "R-stuck-old") is False



def test_fit_logreg_tolerates_malformed_feature_snapshot_values():
    from app.calibration import fit_logreg

    now = int(time.time())
    base_snapshot = {
        "range_score": 0.7,
        "trend_strength": 0.2,
        "atr_pct_norm": 0.4,
        "effective_sentiment": 0.1,
        "dir_conf": 0.65,
        "coherence": 0.6,
        "spread_bps_norm": 0.5,
        "score": 0.35,
        "oi_4h_norm": 0.2,
        "funding_norm": -0.1,
        "liq_tier_num": 0.9,
        "btc_corr": 0.25,
        "regime_conf": 0.7,
    }
    rows = []
    for idx in range(12):
        snap = dict(base_snapshot)
        if idx == 0:
            snap["range_score"] = "oops"
            snap["dir_conf"] = {"unexpected": True}
            snap["btc_corr"] = "nan"
        success = 1 if idx < 6 else 0
        score = 0.6 if success else -0.6
        rows.append({
            "score": score,
            "success": success,
            "ts": now - idx * 60,
            "reasons": {"feature_snapshot": snap},
        })

    model = fit_logreg(rows, min_samples=4, logreg_min_samples=4)

    assert model.fitted is True
    assert model.n_samples == len(rows)



def test_load_settings_resolves_relative_db_path_against_project_root(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", "./tmp/test-relative.db")

    sys.modules.pop("app.settings", None)
    settings_module = importlib.import_module("app.settings")
    settings = settings_module.load_settings()

    assert settings.db_path == str((settings_module._PROJECT_ROOT / "tmp" / "test-relative.db").resolve())

    sys.modules.pop("app.settings", None)


def test_load_settings_falls_back_when_risk_limits_json_is_malformed(monkeypatch: pytest.MonkeyPatch):
    import importlib
    import sys

    monkeypatch.setenv("RISK_LIMITS_JSON", "{bad-json")
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


def test_load_settings_clamps_and_defaults_invalid_numeric_env(monkeypatch: pytest.MonkeyPatch):
    import importlib
    import sys

    monkeypatch.setenv("COLLECT_INTERVAL_SEC", "oops")
    monkeypatch.setenv("RECO_INTERVAL_SEC", "0")
    monkeypatch.setenv("STALE_DATA_MAX_SEC", "-5")
    monkeypatch.setenv("TOP_N", "-100")
    monkeypatch.setenv("MIN_CONF_TO_RECOMMEND", "1.7")
    monkeypatch.setenv("TAKER_FEE_BPS_LINEAR", "-9")
    monkeypatch.setenv("OUTCOME_HORIZON_FALLBACK_SEC", "nan")
    monkeypatch.setenv("SENTIMENT_INTERVAL_SEC", "3")
    monkeypatch.setenv("FUTURES_COLLECT_INTERVAL_SEC", "-1")

    sys.modules.pop("app.settings", None)
    settings_module = importlib.import_module("app.settings")
    settings = settings_module.load_settings()

    assert settings.collect_interval_sec == 20
    assert settings.reco_interval_sec == 5
    assert settings.stale_data_max_sec == 60
    assert settings.top_n == 1
    assert settings.min_conf_to_recommend == 1.0
    assert settings.taker_fee_bps_linear == 0.0
    assert settings.outcome_horizon_fallback_sec == 900
    assert settings.sentiment_interval_sec == 10
    assert settings.futures_collect_interval_sec == 60

    sys.modules.pop("app.settings", None)


def test_get_risk_limits_normalizes_corrupted_active_limits(tmp_path: Path):
    conn = db.connect(str(tmp_path / "risk.db"))
    db.init_db(conn)
    db.upsert_risk_limits(
        conn,
        version="bad-active",
        limits={
            "max_concurrent_bots": "oops",
            "max_daily_dd_usdt": "-15",
            "cooldown_after_loss_min": "abc",
            "max_symbol_bots": -7,
        },
        is_active=True,
    )

    normalized = get_risk_limits(
        conn,
        {
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
        },
    )

    assert normalized == {
        "max_concurrent_bots": 4,
        "max_daily_dd_usdt": 0.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 1,
    }

    blocks = gate_candidate(conn, "linear", "BTCUSDT", normalized)
    assert any(b["code"] == "MAX_DD_DAY" for b in blocks)


class _RetryingCollectorClient:
    def __init__(self):
        self.ticker_calls: list[str] = []
        self.kline_calls: list[tuple[str, str]] = []
        self.failures_left = 1

    def get_tickers(self, *, category: str, symbol: str):
        self.ticker_calls.append(symbol)
        if self.failures_left > 0:
            self.failures_left -= 1
            raise RuntimeError("Bybit error 10001: params error: symbol invalid")
        return [{
            "lastPrice": "100",
            "bid1Price": "99",
            "ask1Price": "101",
            "volume24h": "1000",
            "turnover24h": "100000",
        }]

    def get_kline(self, *, category: str, symbol: str, interval: str, limit: int):
        self.kline_calls.append((symbol, interval))
        return [["1700000000000", "100", "101", "99", "100.5", "10", "0"]]


def test_db_get_latest_ohlcv_skips_corrupted_rows(conn):
    good_ts = 1_700_000_000
    db.upsert_ohlcv(conn, [
        {
            'venue': 'linear', 'symbol': 'BTCUSDT', 'tf_sec': 60, 'ts': good_ts,
            'open': 100.0, 'high': 101.0, 'low': 99.5, 'close': 100.5, 'volume': 12.0,
        },
        {
            'venue': 'linear', 'symbol': 'BTCUSDT', 'tf_sec': 60, 'ts': good_ts + 60,
            'open': 0.0, 'high': 102.0, 'low': 100.0, 'close': 101.0, 'volume': 10.0,
        },
    ])

    rows = db.get_latest_ohlcv(conn, 'linear', 'BTCUSDT', 60, limit=10)

    assert [int(r['ts']) for r in rows] == [good_ts]



def test_db_get_latest_ohlcv_overfetches_past_invalid_newest_rows(conn):
    base_ts = 1_700_000_000
    rows = []
    for idx in range(6):
        rows.append({
            'venue': 'linear', 'symbol': 'BTCUSDT', 'tf_sec': 60, 'ts': base_ts + idx * 60,
            'open': 100.0 + idx, 'high': 101.0 + idx, 'low': 99.0 + idx, 'close': 100.5 + idx, 'volume': 10.0,
        })
    rows.extend([
        {
            'venue': 'linear', 'symbol': 'BTCUSDT', 'tf_sec': 60, 'ts': base_ts + 6 * 60,
            'open': 106.0, 'high': 105.0, 'low': 104.0, 'close': 104.5, 'volume': 11.0,
        },
        {
            'venue': 'linear', 'symbol': 'BTCUSDT', 'tf_sec': 60, 'ts': base_ts + 7 * 60,
            'open': 107.0, 'high': 108.0, 'low': 108.5, 'close': 107.5, 'volume': 12.0,
        },
    ])
    db.upsert_ohlcv(conn, rows)

    latest = db.get_latest_ohlcv(conn, 'linear', 'BTCUSDT', 60, limit=6)

    assert len(latest) == 6
    assert [int(r['ts']) for r in latest] == [base_ts + idx * 60 for idx in range(5, -1, -1)]


def test_collector_skips_nonfinite_market_payload_rows(tmp_path: Path):
    from app import collector

    collector._DISABLED_SYMBOLS["spot"].clear()
    collector._DISABLED_SYMBOLS["linear"].clear()

    class BadPayloadClient:
        def get_tickers(self, *, category: str, symbol: str):
            return [{
                "lastPrice": "NaN",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "inf",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int):
            return [
                ["1700000060000", "NaN", "101", "99", "100.5", "10", "0"],
                ["1700000000000", "100", "101", "99", "100.5", "10", "0"],
            ]

        def get_funding_rate(self, symbol: str):
            return {"symbol": symbol, "funding_rate": float('nan'), "next_funding_ts": 1700003600}

        def get_open_interest(self, symbol: str, interval: str = "1h", limit: int = 48):
            return [
                {"ts": 1700003600, "oi": float('nan')},
                {"ts": 1700000000, "oi": 123.0},
            ]

    conn = db.connect(str(tmp_path / "collector_bad_rows.db"))
    db.init_db(conn)
    client = BadPayloadClient()

    collector.collect_once(conn, client, "spot", ["BTCUSDT"])
    collector.collect_futures_once(conn, client, ["BTCUSDT"])

    ticker = db.get_latest_ticker(conn, "spot", "BTCUSDT")
    rows = db.get_latest_ohlcv(conn, "spot", "BTCUSDT", 60, limit=10)
    oi_rows = db.get_oi_series(conn, "BTCUSDT", limit=10)
    funding = db.get_latest_funding_rate(conn, "BTCUSDT")

    assert ticker is not None
    assert ticker["turnover24h"] is None
    assert len(rows) == 1
    assert int(rows[0]["ts"]) == 1700000000
    assert len(oi_rows) == 1
    assert funding is None

    conn.close()



def test_collector_sanitizes_crossed_quotes(tmp_path: Path):
    from app import collector

    collector._DISABLED_SYMBOLS["spot"].clear()
    collector._DISABLED_SYMBOLS["linear"].clear()

    class CrossedQuoteClient:
        def get_tickers(self, *, category: str, symbol: str):
            return [{
                "lastPrice": "100",
                "bid1Price": "101",
                "ask1Price": "99",
                "volume24h": "1000",
                "turnover24h": "500000",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int):
            return [["1700000000000", "100", "101", "99", "100.5", "10", "0"]]

    conn = db.connect(str(tmp_path / "collector_crossed_quotes.db"))
    db.init_db(conn)

    collector.collect_once(conn, CrossedQuoteClient(), "spot", ["BTCUSDT"])

    ticker = db.get_latest_ticker(conn, "spot", "BTCUSDT")
    assert ticker is not None
    assert ticker["last"] == 100.0
    assert ticker["bid"] is None
    assert ticker["ask"] is None

    feat = compute_features_from_ohlcv(
        [{"ts": i, "close": 100 + i, "high": 101 + i, "low": 99 + i, "volume": 10.0} for i in range(1, 35)],
        ticker,
    )
    assert feat is not None
    assert feat["spread_bps"] is None

    conn.close()


def test_collector_retries_temporarily_disabled_symbol_after_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app import collector

    collector._DISABLED_SYMBOLS["spot"].clear()
    collector._DISABLED_SYMBOLS["linear"].clear()

    conn = db.connect(str(tmp_path / "collector.db"))
    db.init_db(conn)
    client = _RetryingCollectorClient()
    base_ts = 1_700_000_000

    monkeypatch.setattr(db, "now_ts", lambda: base_ts)
    collector.collect_once(conn, client, "spot", ["btcusdt"])
    assert client.ticker_calls == ["BTCUSDT"]
    assert collector._DISABLED_SYMBOLS["spot"]["BTCUSDT"] == base_ts + collector.DISABLED_SYMBOL_RETRY_TTL_SEC

    monkeypatch.setattr(db, "now_ts", lambda: base_ts + 60)
    collector.collect_once(conn, client, "spot", ["BTCUSDT"])
    assert client.ticker_calls == ["BTCUSDT"]

    monkeypatch.setattr(db, "now_ts", lambda: base_ts + collector.DISABLED_SYMBOL_RETRY_TTL_SEC + 1)
    collector.collect_once(conn, client, "spot", ["BTCUSDT"])

    assert client.ticker_calls == ["BTCUSDT", "BTCUSDT"]
    assert any(symbol == "BTCUSDT" and interval == "1" for symbol, interval in client.kline_calls)
    assert db.get_latest_ticker(conn, "spot", "BTCUSDT") is not None


def test_llm_cache_context_signature_mismatch_does_not_get_reused(conn):
    cache_key = 'linear|BTCUSDT|futures_grid|long'
    db.set_app_config_json(
        conn,
        recommender_module.LLM_REVIEW_CACHE_APP_KEY,
        {
            cache_key: {
                'ts': int(time.time()),
                'provider': 'ollama',
                'model': 'fake-llm',
                'prompt_version': 'ohlcv_multitf_v1',
                'context_signature': 'tf=900,3600|candles=32',
                'thesis_direction': 'long',
                'execution_direction': 'long',
                'confidence': 0.82,
                'summary': 'stale context',
                'risk_flags': [],
            }
        },
    )

    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode='advisory',
        llm_reviewer_model='fake-llm',
        llm_reviewer_tf_secs=[15 * 60, 60 * 60, 4 * 60 * 60],
        llm_reviewer_candles_per_tf=48,
        llm_reviewer_max_candidates=5,
        llm_reviewer_cadence_sec=300,
    )

    class FakeReviewer:
        provider = 'ollama'
        model = 'fake-llm'
        prompt_version = 'ohlcv_multitf_v1'

    rec = {
        'rec_id': 'R-cache-context',
        'ts': int(time.time()),
        'venue': 'linear',
        'symbol': 'BTCUSDT',
        'bot_type': 'futures_grid',
        'direction': 'long',
        'status': 'recommended',
        'confidence': 0.7,
        'score': 0.3,
        'reasons': {},
    }

    stats = recommender_module._mark_llm_reviews_async(conn, [rec], settings, reviewer=FakeReviewer())

    assert stats['cached'] == 0
    assert rec['reasons']['llm_review']['status'] == 'pending'



def test_llm_cache_prompt_version_mismatch_does_not_get_reused(conn):
    now = int(time.time())
    cache_key = 'linear|BTCUSDT|futures_grid|long'
    db.set_app_config_json(
        conn,
        recommender_module.LLM_REVIEW_CACHE_APP_KEY,
        {
            cache_key: {
                'ts': now,
                'provider': 'ollama',
                'model': 'fake-llm',
                'prompt_version': 'older_prompt_v0',
                'thesis_direction': 'long',
                'execution_direction': 'long',
                'confidence': 0.8,
                'regime_view': 'range',
                'summary': 'stale cache',
                'risk_flags': [],
            }
        },
    )

    settings = _settings_for_tests(
        llm_reviewer_enabled=True,
        llm_reviewer_mode='advisory',
        llm_reviewer_model='fake-llm',
        llm_reviewer_max_candidates=5,
        llm_reviewer_cadence_sec=300,
    )

    class FakeReviewer:
        provider = 'ollama'
        model = 'fake-llm'
        prompt_version = 'ohlcv_multitf_v1'

    rec = {
        'rec_id': 'R-cache-version',
        'venue': 'linear',
        'symbol': 'BTCUSDT',
        'bot_type': 'futures_grid',
        'direction': 'long',
        'status': 'recommended',
        'score': 0.51,
        'confidence': 0.81,
        'expected_rr': 1.4,
        'risk_score': 0.2,
        'params': {},
        'reasons': {
            'direction_agg': {'raw_direction': 'long'},
            'execution_constraints': {'raw_direction': 'long'},
        },
    }

    stats = recommender_module._mark_llm_reviews_async(conn, [rec], settings, reviewer=FakeReviewer())

    assert stats['queued'] == 1
    assert stats['cached'] == 0
    assert rec['reasons']['llm_review']['status'] == 'pending'


def test_trade_summary_ignores_non_finite_rows(conn):
    conn.execute(
        """INSERT INTO trades(trade_id, bot_id, ts, symbol, pnl, fee, meta_json) VALUES(?,?,?,?,?,?,?)""",
        ('T-good', 'B-1', 1_700_000_000, 'BTCUSDT', 10.0, 1.5, '{}'),
    )
    conn.execute(
        """INSERT INTO trades(trade_id, bot_id, ts, symbol, pnl, fee, meta_json) VALUES(?,?,?,?,?,?,?)""",
        ('T-bad', 'B-1', 1_700_000_060, 'BTCUSDT', float('inf'), float('inf'), '{}'),
    )
    conn.commit()

    summary = db.get_bot_trade_summary(conn, 'B-1')

    assert summary['trade_count'] == 2
    assert summary['realized_pnl_gross'] == pytest.approx(10.0)
    assert summary['realized_fee'] == pytest.approx(1.5)
    assert summary['realized_pnl_net'] == pytest.approx(8.5)


def test_insert_trade_rejects_non_finite_numbers(conn):
    with pytest.raises(ValueError, match='finite number'):
        db.insert_trade(
            conn,
            {
                'trade_id': 'T-nonfinite',
                'bot_id': 'B-1',
                'ts': 1_700_000_000,
                'symbol': 'BTCUSDT',
                'pnl': float('inf'),
                'fee': 0.1,
                'meta': {},
            },
        )
