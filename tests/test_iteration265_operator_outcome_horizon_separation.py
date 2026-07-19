from pathlib import Path
import sqlite3
from types import SimpleNamespace

from app import db
from app.recommender import _apply_recent_publication_dedupe


def _root_recommendation(*, rec_id: str, ts: int, ttl_sec: int = 900) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "one_way",
        "margin_mode": "isolated",
        "score": 0.25,
        "confidence": 0.70,
        "expected_rr": 0.30,
        "risk_score": 0.20,
        "params": {
            "label_horizon_hours": 12,
            "trade_plan": {"entry_price": 100.0, "label_horizon_hours": 12},
        },
        "reasons": {
            "risk_checks": {"passed": True, "blocks": []},
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": True,
                "sample_role": "actionable_root",
            },
        },
        "blocks": [],
        "status": "recommended",
        "ttl_sec": ttl_sec,
        "model_version": "test-v265",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "outcome_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _candidate(*, rec_id: str, ts: int, ttl_sec: int = 900) -> dict:
    rec = _root_recommendation(rec_id=rec_id, ts=ts, ttl_sec=ttl_sec)
    rec["score"] = 0.31
    rec["confidence"] = 0.74
    rec["expected_rr"] = 0.36
    rec["params"] = {
        "label_horizon_hours": 12,
        "trade_plan": {"entry_price": 101.0, "label_horizon_hours": 12},
    }
    return rec


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        reco_republish_cooldown_sec=3600,
        reco_ttl_sec=900,
        outcome_horizon_fallback_sec=12 * 3600,
    )


def test_operator_ttl_starts_fresh_publication_chain_without_new_outcome_root(
    tmp_path: Path, monkeypatch,
) -> None:
    """A stale operator idea must not become a second overlapping training sample.

    After the 15-minute operator TTL, the recommender may publish a fresh actionable
    recommendation. Its publication root is new, but its statistical outcome root
    remains the still-open 12-hour pseudo-position.
    """
    conn = db.connect(str(tmp_path / "operator-outcome-separation.db"))
    db.init_db(conn)
    try:
        now = 1_800_000_000
        old_root = _root_recommendation(rec_id="R-old-outcome", ts=now - 20 * 60)
        db.insert_recommendations(conn, [old_root])
        monkeypatch.setattr(db, "now_ts", lambda: now)
        assert db.expire_stale_recommendations(conn) == 1
        assert db.get_recommendation_by_id(conn, "R-old-outcome")["status"] == "expired"

        fresh = _candidate(rec_id="R-fresh-operator", ts=now)
        _apply_recent_publication_dedupe(conn, [fresh], _settings(), now)

        assert fresh["status"] == "recommended"
        assert fresh["publication_root_rec_id"] == "R-fresh-operator"
        assert fresh["outcome_root_rec_id"] == "R-old-outcome"
        assert fresh["is_outcome_label_root"] is False
        dedupe = fresh["reasons"]["publication_dedupe"]
        assert dedupe["decision"] == "publish_fresh_operator_root"
        assert dedupe["operator_chain_reset"] is True
        assert dedupe["open_position_lock"] is True
        assert dedupe["previous_outcome_root_rec_id"] == "R-old-outcome"

        db.insert_recommendations(conn, [fresh])
        freshness = db.recommendation_chain_expiry_context(
            conn,
            rec_id=fresh["rec_id"],
            publication_root_rec_id=fresh["publication_root_rec_id"],
            row_ts=fresh["ts"],
            ttl_sec=fresh["ttl_sec"],
            ts_now=now,
        )
        assert freshness["is_publication_chain_expired"] is False
        roots = conn.execute(
            """SELECT rec_id, outcome_root_rec_id
                 FROM recommendations
                WHERE is_outcome_label_root=1
                  AND venue='linear' AND symbol='BTCUSDT'
                  AND bot_type='futures_grid' AND direction='long'"""
        ).fetchall()
        assert [(row["rec_id"], row["outcome_root_rec_id"]) for row in roots] == [
            ("R-old-outcome", "R-old-outcome")
        ]
    finally:
        conn.close()


def test_live_operator_chain_reuses_both_publication_and_outcome_roots(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "live-chain-reuse.db"))
    db.init_db(conn)
    try:
        now = 1_800_100_000
        root = _root_recommendation(rec_id="R-live-root", ts=now - 5 * 60)
        db.insert_recommendations(conn, [root])

        update = _candidate(rec_id="R-live-update", ts=now)
        _apply_recent_publication_dedupe(conn, [update], _settings(), now)

        assert update["status"] == "active"
        assert update["publication_root_rec_id"] == "R-live-root"
        assert update["outcome_root_rec_id"] == "R-live-root"
        assert update["is_outcome_label_root"] is False
    finally:
        conn.close()


def test_new_outcome_root_is_allowed_only_after_full_label_horizon(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "horizon-release.db"))
    db.init_db(conn)
    try:
        now = 1_800_200_000
        old = _root_recommendation(
            rec_id="R-matured-root",
            ts=now - (12 * 3600) - 120,
        )
        db.insert_recommendations(conn, [old])

        fresh = _candidate(rec_id="R-new-outcome", ts=now)
        _apply_recent_publication_dedupe(conn, [fresh], _settings(), now)

        assert fresh["status"] == "recommended"
        assert fresh["publication_root_rec_id"] == "R-new-outcome"
        assert fresh["outcome_root_rec_id"] == "R-new-outcome"
        assert fresh["is_outcome_label_root"] is True
        assert "publication_dedupe" not in fresh.get("reasons", {})
    finally:
        conn.close()


def test_existing_sqlite_database_gets_additive_outcome_root_backfill(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-publication-lineage.db"
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            """CREATE TABLE recommendations (
                 rec_id TEXT PRIMARY KEY,
                 ts INTEGER NOT NULL,
                 venue TEXT NOT NULL,
                 symbol TEXT NOT NULL,
                 bot_type TEXT NOT NULL,
                 direction TEXT NOT NULL,
                 account_mode TEXT NOT NULL,
                 margin_mode TEXT NOT NULL,
                 score REAL NOT NULL,
                 confidence REAL NOT NULL,
                 expected_rr REAL NOT NULL,
                 risk_score REAL NOT NULL,
                 params_json TEXT NOT NULL,
                 reasons_json TEXT NOT NULL,
                 blocks_json TEXT NOT NULL,
                 status TEXT NOT NULL,
                 ttl_sec INTEGER NOT NULL,
                 model_version TEXT NOT NULL,
                 features_ref_ts INTEGER NOT NULL,
                 publication_root_rec_id TEXT,
                 is_outcome_label_root INTEGER NOT NULL DEFAULT 1
               )"""
        )
        raw.execute(
            """INSERT INTO recommendations VALUES (
                 'R-legacy', 1700000000, 'linear', 'BTCUSDT', 'futures_grid',
                 'long', 'one_way', 'isolated', 0.2, 0.7, 0.3, 0.2,
                 '{}', '{}', '[]', 'recommended', 900, 'legacy', 1700000000,
                 'R-legacy', 1
               )"""
        )
        raw.commit()
    finally:
        raw.close()

    conn = db.connect(str(db_path))
    try:
        db.init_db(conn)
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(recommendations)").fetchall()
        }
        assert "outcome_root_rec_id" in columns
        row = conn.execute(
            "SELECT outcome_root_rec_id FROM recommendations WHERE rec_id='R-legacy'"
        ).fetchone()
        assert row["outcome_root_rec_id"] == "R-legacy"
    finally:
        conn.close()


def test_historical_lineage_repair_preserves_operator_ttl_boundary(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "repair-separation.db"))
    db.init_db(conn)
    try:
        ts0 = 1_800_300_000
        first = _root_recommendation(rec_id="R-repair-outcome", ts=ts0, ttl_sec=900)
        second = _root_recommendation(
            rec_id="R-repair-fresh-operator",
            ts=ts0 + 20 * 60,
            ttl_sec=900,
        )
        third = _root_recommendation(
            rec_id="R-repair-child",
            ts=ts0 + 25 * 60,
            ttl_sec=900,
        )
        db.insert_recommendations(conn, [first, second, third])

        repaired = db.repair_async_llm_pending_publication_chains(conn)
        assert repaired == 2

        first_row = db.get_recommendation_by_id(conn, first["rec_id"])
        second_row = db.get_recommendation_by_id(conn, second["rec_id"])
        third_row = db.get_recommendation_by_id(conn, third["rec_id"])
        assert first_row["publication_root_rec_id"] == first["rec_id"]
        assert first_row["outcome_root_rec_id"] == first["rec_id"]
        assert first_row["is_outcome_label_root"] is True

        assert second_row["publication_root_rec_id"] == second["rec_id"]
        assert second_row["outcome_root_rec_id"] == first["rec_id"]
        assert second_row["is_outcome_label_root"] is False

        assert third_row["publication_root_rec_id"] == second["rec_id"]
        assert third_row["outcome_root_rec_id"] == first["rec_id"]
        assert third_row["is_outcome_label_root"] is False
    finally:
        conn.close()
