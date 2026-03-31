from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from app import db
from app import recommender as recommender_module


def test_insert_sentiment_points_commit_false_rolls_back(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "sentiment_commit_false.db"))
    db.init_db(conn)
    try:
        db.insert_sentiment_points(
            conn,
            [{
                "scope": "global",
                "key": "crypto",
                "ts": int(time.time()),
                "sentiment": 0.4,
                "velocity": 0.0,
                "volume": 1,
                "sources": {"unit": 1},
                "tags": ["rollback"],
            }],
            commit=False,
        )
        conn.rollback()
        count = int(conn.execute("SELECT COUNT(*) AS c FROM sentiment").fetchone()["c"])
        assert count == 0
    finally:
        conn.close()


def _insert_recent_reco(conn, rec_id: str, ts: int, *, direction: str) -> None:
    db.insert_recommendations(
        conn,
        [{
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": direction,
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.20,
            "confidence": 0.60,
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


def test_recent_publication_dedupe_ignores_opposite_direction(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "dedupe_direction.db"))
    db.init_db(conn)
    try:
        ts_now = int(time.time())
        _insert_recent_reco(conn, "R-prev-short", ts_now - 120, direction="short")
        recs = [{
            "rec_id": "R-new-long",
            "ts": ts_now,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "isolated",
            "score": 0.21,
            "confidence": 0.61,
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
        settings = SimpleNamespace(reco_republish_cooldown_sec=3600, reco_ttl_sec=1800)

        recommender_module._apply_recent_publication_dedupe(conn, recs, settings, ts_now)

        assert recs[0]["status"] == "recommended"
        assert "publication_dedupe" not in recs[0]["reasons"]
    finally:
        conn.close()
