from __future__ import annotations

import pytest

from app import db


def _minimal_reco(*, rec_id: str, ts: int) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "one_way",
        "margin_mode": "cross",
        "score": 0.21,
        "confidence": 0.64,
        "expected_rr": 0.25,
        "risk_score": 0.20,
        "params": {"trade_plan": {"entry_price": 100.0}},
        "reasons": {},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 1800,
        "model_version": "test",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def test_init_db_skips_recommendation_lineage_backfill_when_rows_are_already_materialized(tmp_path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "restart_clean_reco.db"))
    try:
        db.init_db(conn)
        db.insert_recommendations(
            conn,
            [
                _minimal_reco(rec_id="R-1", ts=1_700_000_000),
                _minimal_reco(rec_id="R-2", ts=1_700_000_060),
            ],
        )

        called = {"count": 0}

        def _unexpected_backfill(_conn):
            called["count"] += 1
            raise AssertionError("startup must not run full recommendation lineage backfill on already materialized rows")

        monkeypatch.setattr(db, "backfill_recommendation_publication_lineage", _unexpected_backfill)

        db.init_db(conn)
        assert called["count"] == 0
    finally:
        conn.close()



def test_init_db_skips_bot_publication_root_backfill_when_rows_are_already_materialized(tmp_path, monkeypatch: pytest.MonkeyPatch):
    conn = db.connect(str(tmp_path / "restart_clean_bots.db"))
    try:
        db.init_db(conn)
        db.insert_recommendations(conn, [_minimal_reco(rec_id="R-root", ts=1_700_000_000)])
        db.insert_bot_instance(
            conn,
            {
                "bot_id": "B-1",
                "started_ts": 1_700_000_100,
                "stopped_ts": None,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "mode": {"margin_mode": "cross"},
                "params": {"grid_levels": 5},
                "state": {},
                "status": "stopped",
                "origin_rec_id": "R-root",
                "publication_root_rec_id": "R-root",
            },
        )

        called = {"count": 0}

        def _unexpected_backfill(_conn):
            called["count"] += 1
            raise AssertionError("startup must not rescan all bot_instances when publication_root_rec_id is already filled")

        monkeypatch.setattr(db, "_backfill_bot_publication_root", _unexpected_backfill)

        db.init_db(conn)
        assert called["count"] == 0
    finally:
        conn.close()
