from __future__ import annotations

import importlib
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app import recommender as recommender_module


def test_get_latest_ticker_ts_ignores_invalid_newer_fallback_rows(tmp_path: Path):
    conn = db.connect(str(tmp_path / "ticker_ts.db"))
    db.init_db(conn)
    try:
        db.insert_tickers(
            conn,
            [{
                "venue": "spot",
                "symbol": "BTCUSDT",
                "ts": 1_700_000_000,
                "last": 100.0,
                "bid": 99.5,
                "ask": 100.5,
                "vol24h": 10.0,
                "turnover24h": 1_000.0,
            }],
        )
        conn.execute(
            """INSERT INTO ticker_snap(venue,symbol,ts,last,bid,ask,vol24h,turnover24h)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("spot", "BTCUSDT", 1_700_000_060, 101.0, 102.0, 101.0, 9.0, 900.0),
        )
        conn.commit()

        latest = db.get_latest_ticker(conn, "spot", "BTCUSDT")
        assert latest is not None
        assert int(latest["ts"]) == 1_700_000_000
        assert db.get_latest_ticker_ts(conn, "spot", "BTCUSDT") == 1_700_000_000

        conn.execute(
            "DELETE FROM ticker_snap WHERE venue='spot' AND symbol='BTCUSDT' AND ts=?",
            (1_700_000_000,),
        )
        conn.commit()
        fallback = db.get_latest_ticker(conn, "spot", "BTCUSDT")
        assert fallback is not None
        assert fallback["bid"] is None and fallback["ask"] is None
        assert db.get_latest_ticker_ts(conn, "spot", "BTCUSDT") is None
    finally:
        conn.close()


def test_sentiment_thread_rolls_back_partial_write_when_lock_is_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "sentiment_lock_loss.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    try:
        app_main.settings = replace(app_main.settings, sentiment_interval_sec=5)

        def fake_collect_sentiment_once():
            return [{
                "scope": "global",
                "key": "crypto",
                "ts": int(time.time()),
                "sentiment": 0.3,
                "velocity": 0.0,
                "volume": 1,
                "sources": {"test": True},
                "tags": ["synthetic"],
            }]

        beats = iter([True, True, False])

        def fake_heartbeat_factory(*args, **kwargs):
            return lambda: next(beats)

        def stop_after_first_wait(*args, **kwargs):
            raise StopIteration

        monkeypatch.setattr(app_main, "collect_sentiment_once", fake_collect_sentiment_once)
        monkeypatch.setattr(app_main, "_make_runtime_lock_heartbeat", fake_heartbeat_factory)
        monkeypatch.setattr(app_main, "_interval_loop_wait", stop_after_first_wait)

        with pytest.raises(StopIteration):
            app_main._sentiment_thread()

        conn = db.connect(str(db_path))
        try:
            sentiment_count = int(conn.execute("SELECT COUNT(*) AS c FROM sentiment").fetchone()["c"])
            assert sentiment_count == 0
            row = conn.execute(
                "SELECT details_json FROM decision_log WHERE action='SENTIMENT_ERROR' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            details = app_main._json_loads_or_default(row["details_json"], {})
            assert details.get("field") == "runtime_lock"
        finally:
            conn.close()
    finally:
        sys.modules.pop("app.main", None)


def _insert_recent_reco(conn, rec_id: str, ts: int, *, confidence: float = 0.60, score: float = 0.20):
    db.insert_recommendations(
        conn,
        [{
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": score,
            "confidence": confidence,
            "expected_rr": 0.20,
            "risk_score": 0.30,
            "params": {"trade_plan": {"entry_price": 100.0}},
            "reasons": {},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": ts,
        }],
    )


def test_recent_publication_dedupe_marks_duplicate_signal_active(tmp_path: Path):
    conn = db.connect(str(tmp_path / "reco_dedupe.db"))
    db.init_db(conn)
    try:
        ts_now = int(time.time())
        _insert_recent_reco(conn, "R-prev", ts_now - 120, confidence=0.60, score=0.20)
        recs = [{
            "rec_id": "R-new",
            "ts": ts_now,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.22,
            "confidence": 0.62,
            "expected_rr": 0.22,
            "risk_score": 0.28,
            "params": {"trade_plan": {"entry_price": 100.2}},
            "reasons": {},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": ts_now,
        }]
        settings = SimpleNamespace(reco_republish_cooldown_sec=3600, reco_ttl_sec=1800, outcome_horizon_fallback_sec=6 * 3600)

        recommender_module._apply_recent_publication_dedupe(conn, recs, settings, ts_now)

        assert recs[0]["status"] == "active"
        dedupe = recs[0]["reasons"].get("publication_dedupe") or {}
        assert dedupe.get("previous_rec_id") == "R-prev"
        assert dedupe.get("active_reuse") is True
        assert dedupe.get("decision") == "reuse_active"
    finally:
        conn.close()


def test_recent_publication_dedupe_allows_material_upgrade_after_previous_chain_is_closed(tmp_path: Path):
    conn = db.connect(str(tmp_path / "reco_dedupe_upgrade.db"))
    db.init_db(conn)
    try:
        ts_now = int(time.time())
        ts_prev = ts_now - 120
        _insert_recent_reco(conn, "R-prev", ts_prev, confidence=0.55, score=0.18)
        db.insert_outcome(
            conn,
            {
                "rec_id": "R-prev",
                "ts": ts_prev,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "horizon_sec": 6 * 3600,
                "entry_close": 100.0,
                "exit_close": 101.0,
                "ret": 0.01,
                "success": 1,
            },
        )
        recs = [{
            "rec_id": "R-new",
            "ts": ts_now,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.35,
            "confidence": 0.70,
            "expected_rr": 0.34,
            "risk_score": 0.18,
            "params": {"trade_plan": {"entry_price": 103.0}},
            "reasons": {},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": ts_now,
        }]
        settings = SimpleNamespace(reco_republish_cooldown_sec=3600, reco_ttl_sec=1800, outcome_horizon_fallback_sec=6 * 3600)

        recommender_module._apply_recent_publication_dedupe(conn, recs, settings, ts_now)

        assert recs[0]["status"] == "recommended"
        dedupe = recs[0]["reasons"].get("publication_dedupe") or {}
        assert dedupe.get("material_upgrade") is True
        assert dedupe.get("open_position_lock") is False
        assert dedupe.get("suppressed") is False
    finally:
        conn.close()


def test_open_position_lock_keeps_same_direction_signal_in_existing_chain_after_cooldown(tmp_path: Path):
    """Даже после истечения republish-cooldown нельзя открывать новый pseudo-entry,
    пока прошлый same-direction root ещё живёт внутри outcome-horizon.
    """
    conn = db.connect(str(tmp_path / "reco_open_position_lock.db"))
    db.init_db(conn)
    try:
        ts_now = int(time.time())
        ts_prev = ts_now - (2 * 3600)
        _insert_recent_reco(conn, "R-prev", ts_prev, confidence=0.55, score=0.18)

        recs = [{
            "rec_id": "R-new",
            "ts": ts_now,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.38,
            "confidence": 0.74,
            "expected_rr": 0.40,
            "risk_score": 0.18,
            "params": {"trade_plan": {"entry_price": 104.0}},
            "reasons": {},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": ts_now,
        }]
        settings = SimpleNamespace(reco_republish_cooldown_sec=3600, reco_ttl_sec=1800, outcome_horizon_fallback_sec=6 * 3600)

        recommender_module._apply_recent_publication_dedupe(conn, recs, settings, ts_now)

        assert recs[0]["status"] == "active"
        assert recs[0]["publication_root_rec_id"] == "R-prev"
        assert recs[0]["is_outcome_label_root"] is False
        dedupe = recs[0]["reasons"].get("publication_dedupe") or {}
        assert dedupe.get("decision") == "reuse_active"
        assert dedupe.get("open_position_lock") is True
        assert dedupe.get("previous_root_rec_id") == "R-prev"
        assert int(dedupe.get("lock_until_ts") or 0) > ts_now
    finally:
        conn.close()


def test_open_position_lock_releases_chain_after_horizon_elapsed(tmp_path: Path):
    """Просроченный unresolved root не должен вечно блокировать новые сигналы."""
    conn = db.connect(str(tmp_path / "reco_open_position_release.db"))
    db.init_db(conn)
    try:
        ts_now = int(time.time())
        ts_prev = ts_now - (13 * 3600)
        _insert_recent_reco(conn, "R-prev", ts_prev, confidence=0.55, score=0.18)

        recs = [{
            "rec_id": "R-new",
            "ts": ts_now,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.38,
            "confidence": 0.74,
            "expected_rr": 0.40,
            "risk_score": 0.18,
            "params": {"trade_plan": {"entry_price": 104.0}},
            "reasons": {},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": ts_now,
        }]
        settings = SimpleNamespace(reco_republish_cooldown_sec=3600, reco_ttl_sec=1800, outcome_horizon_fallback_sec=6 * 3600)

        recommender_module._apply_recent_publication_dedupe(conn, recs, settings, ts_now)

        assert recs[0]["status"] == "recommended"
        assert recs[0].get("publication_root_rec_id") in (None, "R-new")
        dedupe = recs[0]["reasons"].get("publication_dedupe") or {}
        assert dedupe == {}
    finally:
        conn.close()


def test_recent_publication_dedupe_reuses_pending_llm_hold_row(tmp_path: Path):
    """Async LLM hold must still lock the publication-chain for the next cycle."""
    conn = db.connect(str(tmp_path / "reco_pending_hold_dedupe.db"))
    db.init_db(conn)
    try:
        ts_now = int(time.time())
        ts_prev = ts_now - 120
        db.insert_recommendations(
            conn,
            [{
                "rec_id": "R-prev-pending",
                "ts": ts_prev,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.21,
                "confidence": 0.61,
                "expected_rr": 0.22,
                "risk_score": 0.30,
                "params": {"trade_plan": {"entry_price": 100.0}},
                "reasons": {
                    "llm_review": {
                        "status": "pending",
                        "mode": "advisory",
                        "gate_decision": "pending",
                        "publish_target_status": "recommended",
                        "queued_ts": ts_prev,
                    }
                },
                "blocks": [],
                "status": "pending",
                "ttl_sec": 1800,
                "model_version": "test",
                "features_ref_ts": ts_prev,
            }],
        )
        recs = [{
            "rec_id": "R-new",
            "ts": ts_now,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.24,
            "confidence": 0.63,
            "expected_rr": 0.25,
            "risk_score": 0.28,
            "params": {"trade_plan": {"entry_price": 100.1}},
            "reasons": {},
            "blocks": [],
            "status": "recommended",
            "ttl_sec": 1800,
            "model_version": "test",
            "features_ref_ts": ts_now,
        }]
        settings = SimpleNamespace(reco_republish_cooldown_sec=3600, reco_ttl_sec=1800, outcome_horizon_fallback_sec=6 * 3600)

        recommender_module._apply_recent_publication_dedupe(conn, recs, settings, ts_now)

        assert recs[0]["status"] == "active"
        assert recs[0]["publication_root_rec_id"] == "R-prev-pending"
        assert recs[0]["is_outcome_label_root"] is False
        dedupe = recs[0]["reasons"].get("publication_dedupe") or {}
        assert dedupe.get("previous_rec_id") == "R-prev-pending"
        assert dedupe.get("open_position_lock") is True
    finally:
        conn.close()



def test_backfill_repairs_async_llm_pending_duplicate_roots(tmp_path: Path):
    conn = db.connect(str(tmp_path / "reco_pending_hold_backfill.db"))
    db.init_db(conn)
    try:
        ts0 = int(time.time()) - 15 * 3600
        first_ref = ts0
        second_ref = ts0 + 180
        db.upsert_ohlcv(conn, [
            {
                "venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": first_ref + 60,
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0,
            },
            {
                "venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": second_ref + 60,
                "open": 100.2, "high": 101.2, "low": 99.2, "close": 100.7, "volume": 12.0,
            },
        ])
        db.insert_recommendations(
            conn,
            [
                {
                    "rec_id": "R-root-1",
                    "ts": ts0,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "futures_grid",
                    "direction": "long",
                    "account_mode": "one_way",
                    "margin_mode": "isolated",
                    "score": 0.20,
                    "confidence": 0.60,
                    "expected_rr": 0.22,
                    "risk_score": 0.30,
                    "params": {"trade_plan": {"entry_price": 100.0}},
                    "reasons": {
                        "llm_review": {
                            "status": "pending",
                            "mode": "advisory",
                            "gate_decision": "pending",
                            "publish_target_status": "recommended",
                            "queued_ts": ts0,
                        }
                    },
                    "blocks": [],
                    "status": "pending",
                    "ttl_sec": 1800,
                    "model_version": "test",
                    "features_ref_ts": first_ref,
                },
                {
                    "rec_id": "R-root-2",
                    "ts": ts0 + 180,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "futures_grid",
                    "direction": "long",
                    "account_mode": "one_way",
                    "margin_mode": "isolated",
                    "score": 0.21,
                    "confidence": 0.61,
                    "expected_rr": 0.23,
                    "risk_score": 0.29,
                    "params": {"trade_plan": {"entry_price": 100.1}},
                    "reasons": {
                        "llm_review": {
                            "status": "ok",
                            "mode": "advisory",
                            "gate_decision": "pass",
                            "execution_direction": "long",
                            "thesis_direction": "long",
                            "confidence": 0.72,
                        }
                    },
                    "blocks": [],
                    "status": "recommended",
                    "ttl_sec": 1800,
                    "model_version": "test",
                    "features_ref_ts": second_ref,
                },
            ],
        )

        repaired = db.repair_async_llm_pending_publication_chains(conn)
        assert repaired >= 1

        first = db.get_recommendation_by_id(conn, "R-root-1")
        second = db.get_recommendation_by_id(conn, "R-root-2")
        assert first is not None and second is not None
        assert first["publication_root_rec_id"] == "R-root-1"
        assert first["is_outcome_label_root"] is True
        assert second["publication_root_rec_id"] == "R-root-1"
        assert second["is_outcome_label_root"] is False
    finally:
        conn.close()
