from __future__ import annotations

import os
from pathlib import Path

from app import collector, db, direction, recommender, settings as settings_module


def _rec(rec_id: str, ts: int, bot_type: str, *, root: bool = False, score: float = 0.2) -> dict:
    direction_value = "neutral" if bot_type == "futures_grid" else "long"
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": bot_type,
        "direction": direction_value,
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": score,
        "confidence": 0.5,
        "expected_rr": 0.1,
        "risk_score": 0.2,
        "params": {"outcome_horizon_sec": 43200, "ranking_score": score},
        "reasons": {
            "outcome_policy": {
                "eligible": bool(root),
                "policy_evaluation_eligible": bool(root),
                "sample_role": "shadow_no_trade" if root else "excluded",
                "label_due_ts": ts + 43320,
            }
        },
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": recommender.RECOMMENDER_MODEL_VERSION,
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "outcome_root_rec_id": rec_id,
        "is_outcome_label_root": root,
        "outcome_eligible": root,
        "policy_evaluation_eligible": root,
        "outcome_sample_role": "shadow_no_trade" if root else "excluded",
        "risk_checks_passed": True,
        "risk_blocks_empty": True,
        "llm_review_status": "none",
        "candidate_kind": "strategy_recommendation",
    }


def test_both_grid_and_trend_remain_canonical_and_use_horizon_aligned_lineage() -> None:
    assert recommender.RECOMMENDER_MODEL_VERSION == "bybit-taxonomy-v14-horizon-aligned-dual-strategy"
    assert recommender.TREND_RECOMMENDER_MODEL_VERSION.endswith("+directional-trend-v7")
    assert direction.TF_WEIGHTS[86400] < direction.TF_WEIGHTS[3600]
    assert direction.TF_WEIGHTS[14400] >= direction.TF_WEIGHTS[3600]
    assert {"futures_grid", "directional_trend"}.issubset(recommender.SUPPORTED_BOT_TYPES)


def test_recommendation_latest_keeps_both_strategies_and_audit_is_event_driven(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "latest.db"))
    db.init_db(conn)
    first = db.persist_recommendation_cycle(
        conn,
        [_rec("grid-1", 1_700_000_000, "futures_grid"), _rec("trend-1", 1_700_000_000, "directional_trend")],
    )
    assert first["latest_upserts"] == 2
    assert first["audit_inserted"] == 2

    second = db.persist_recommendation_cycle(
        conn,
        [_rec("grid-2", 1_700_000_060, "futures_grid", score=0.201), _rec("trend-2", 1_700_000_060, "directional_trend", score=0.199)],
    )
    assert second["latest_upserts"] == 2
    assert second["audit_inserted"] == 0
    latest = db.get_latest_recommendation_states(conn, venue="linear", top_n=10, min_conf=0.0, statuses=["no_trade"])
    assert {(row["symbol"], row["bot_type"]) for row in latest} == {
        ("BTCUSDT", "futures_grid"),
        ("BTCUSDT", "directional_trend"),
    }

    third = db.persist_recommendation_cycle(
        conn,
        [_rec("grid-root-2", 1_700_043_500, "futures_grid", root=True, score=0.25)],
    )
    assert third["audit_inserted"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"] == 3
    conn.close()


def test_ohlcv_upsert_does_not_rewrite_identical_postgres_style_rows(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "ohlcv.db"))
    db.init_db(conn)
    row = {"venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": 1_700_000_020,
           "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0}
    assert db.upsert_ohlcv(conn, [row]) == 1
    before = conn.total_changes
    assert db.upsert_ohlcv(conn, [row]) == 0
    assert conn.total_changes == before
    changed = dict(row, close=100.7, high=101.2)
    assert db.upsert_ohlcv(conn, [changed]) == 1
    conn.close()


def test_local_derived_tf_recomputes_only_recent_two_buckets(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_latest(conn, venue, symbol, tf_sec, limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(db, "get_latest_ohlcv", fake_latest)
    collector._derive_local_tf_rows(object(), "linear", "BTCUSDT", 60, 900)
    assert captured["limit"] <= 32


def test_backfill_has_separate_idle_cadence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "locks.db"))
    monkeypatch.setenv("VENUES", "linear")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT,ETHUSDT")
    monkeypatch.setenv("BACKFILL_INTERVAL_SEC", "300")
    cfg = settings_module.load_settings()
    assert cfg.backfill_interval_sec == 300


def test_ticker_and_funding_snapshots_are_time_bucketed(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "snapshots.db"))
    db.init_db(conn)
    db.insert_tickers(conn, [
        {"venue": "linear", "symbol": "BTCUSDT", "ts": 1_700_000_001, "last": 100.0, "bid": 99.9, "ask": 100.1, "vol24h": 1.0, "turnover24h": 2.0},
        {"venue": "linear", "symbol": "BTCUSDT", "ts": 1_700_000_021, "last": 100.2, "bid": 100.1, "ask": 100.3, "vol24h": 1.1, "turnover24h": 2.1},
    ])
    assert conn.execute("SELECT COUNT(*) AS c FROM ticker_snap").fetchone()["c"] == 1
    assert conn.execute("SELECT last FROM ticker_snap").fetchone()["last"] == 100.2

    db.upsert_funding_rate(conn, [
        {"symbol": "BTCUSDT", "ts": 1_700_000_001, "funding_rate": 0.0001, "next_funding_ts": 1_700_020_000, "funding_interval_min": 480},
        {"symbol": "BTCUSDT", "ts": 1_700_000_061, "funding_rate": 0.0002, "next_funding_ts": 1_700_020_000, "funding_interval_min": 480},
    ])
    assert conn.execute("SELECT COUNT(*) AS c FROM funding_rate").fetchone()["c"] == 1
    assert conn.execute("SELECT funding_rate FROM funding_rate").fetchone()["funding_rate"] == 0.0002
    conn.close()


def test_trade_capture_scope_contains_only_open_grid_windows(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "capture.db"))
    db.init_db(conn)
    grid = _rec("grid-open", 1_700_000_000, "futures_grid", root=True)
    trend = _rec("trend-open", 1_700_000_000, "directional_trend", root=True)
    trend["direction"] = "long"
    db.insert_recommendations(conn, [grid, trend])
    now = 1_700_000_100
    assert db.list_market_trade_capture_symbols(conn, venue="linear", now_ts=now) == ["BTCUSDT"]
    conn.execute("UPDATE reco_outcome_observability SET state='labeled' WHERE rec_id='grid-open'")
    conn.commit()
    assert db.list_market_trade_capture_symbols(conn, venue="linear", now_ts=now) == []
    conn.close()



def test_router_shadow_competitors_share_one_open_outcome_root() -> None:
    rec = _rec("shadow-competitor", 1_700_000_000, "directional_trend", root=True)
    rec["reasons"]["outcome_policy"]["sample_role"] = "shadow_competitor"
    assert recommender._is_shadow_no_trade_outcome_candidate(rec) is True


def test_current_lineage_outcome_evidence_is_not_pruned_after_fourteen_days(tmp_path: Path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "retention.db"))
    db.init_db(conn)
    now = 1_800_000_000
    old_ts = now - 120 * 86400
    row = _rec("current-root", old_ts, "directional_trend", root=True)
    db.insert_recommendations(conn, [row])
    conn.execute(
        "INSERT INTO reco_outcomes(rec_id,ts,venue,symbol,bot_type,direction,horizon_sec,success,ret,entry_close,exit_close,label_available_ts,event_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("current-root", old_ts + 43200, "linear", "BTCUSDT", "directional_trend", "long", 43200, 1, 0.01, 100.0, 101.0, old_ts + 43320, "TP_FIRST"),
    )
    conn.commit()
    monkeypatch.setattr(db, "now_ts", lambda: now)
    db.prune_old_data(conn, retain_days=7, current_model_version=recommender.RECOMMENDER_MODEL_VERSION)
    assert conn.execute("SELECT COUNT(*) AS c FROM reco_outcomes WHERE rec_id='current-root'").fetchone()["c"] == 1
    conn.close()


def test_old_non_root_refresh_is_pruned_while_latest_state_survives(tmp_path: Path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "refresh-retention.db"))
    db.init_db(conn)
    now = 1_800_000_000
    old_ts = now - 20 * 86400
    db.persist_recommendation_cycle(conn, [_rec("refresh-old", old_ts, "futures_grid")])
    monkeypatch.setattr(db, "now_ts", lambda: now)
    db.prune_old_data(
        conn,
        retain_days=7,
        standard_outcome_retain_days=90,
        exact_policy_retain_days=365,
        current_model_version=recommender.RECOMMENDER_MODEL_VERSION,
        current_lineage_retain_days=365,
    )
    assert conn.execute("SELECT COUNT(*) AS c FROM recommendations WHERE rec_id='refresh-old'").fetchone()["c"] == 0
    latest = db.get_latest_recommendation_states(
        conn, venue="linear", top_n=10, min_conf=0.0, statuses=["no_trade"]
    )
    assert [row["rec_id"] for row in latest] == ["refresh-old"]
    conn.close()


def test_latest_state_overlays_audited_operator_status(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "latest-overlay.db"))
    db.init_db(conn)
    row = _rec("trend-actionable", 1_700_000_000, "directional_trend", root=True)
    row["status"] = "recommended"
    db.persist_recommendation_cycle(conn, [row])
    assert db.update_recommendation_status(conn, "trend-actionable", "ignored", operator="qa") is True
    latest = db.get_latest_recommendation_states(
        conn, venue="linear", top_n=10, min_conf=0.0, statuses=["ignored"]
    )
    assert len(latest) == 1
    assert latest[0]["rec_id"] == "trend-actionable"
    assert latest[0]["status"] == "ignored"
    conn.close()


def test_recommendation_latest_schema_is_additive_for_sqlite_and_postgres(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "upgrade.db"
    conn = db.connect(str(sqlite_path))
    conn.execute("CREATE TABLE legacy_marker(id INTEGER PRIMARY KEY)")
    conn.commit()
    db.init_db(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(recommendation_latest)").fetchall()}
    assert {"venue", "symbol", "bot_type", "rec_id", "evaluated_ts", "state_hash", "payload_json"}.issubset(columns)
    assert conn.execute("SELECT COUNT(*) AS c FROM legacy_marker").fetchone()["c"] == 0
    conn.close()

    root = Path(__file__).resolve().parent.parent
    postgres_sql = (root / "migrations" / "init_postgres.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS recommendation_latest" in postgres_sql
    assert "PRIMARY KEY (venue, symbol, bot_type)" in postgres_sql
