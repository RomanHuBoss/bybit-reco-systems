from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app import recommender as recommender_module


def _feature() -> dict:
    return {
        "price": 1.25,
        "atr_pct": 0.01,
        "_atr_pct_1h": 0.015,
        "_direction_agg": {
            "direction": "neutral",
            "bias": "neutral",
            "strength": {"all": 0.05, "structural": 0.02},
            "trendiness": 0.25,
            "coherence": 0.40,
            "regime": "range",
            "tf_used": [900, 1800, 3600, 14400, 86400],
        },
    }


def _cost_model() -> dict:
    return {"execution_cost_bps": 12.0, "expected_funding_bps": 0.0}


def _row(rec_id: str, *, direction: str, candidate_kind: str, ts: int) -> dict:
    rejected = candidate_kind == "trend_evaluation_rejected"
    params = {
        "candidate_kind": candidate_kind,
        "strategy_family": "trend_evaluation" if rejected else "directional_trend",
        "direction_input_valid": not rejected,
        "price_input_valid": True,
        "price_ref": 1.25,
    }
    if not rejected:
        params.update({
            "entry_model": "single_position_no_pyramiding",
            "take_profit_price": 1.30,
            "stop_loss_price": 1.20,
            "trade_plan": {
                "strategy_family": "directional_trend",
                "reference_price": 1.25,
                "geometry_valid": True,
                "levels": {
                    "take_profit": {"price": 1.30},
                    "stop_loss": {"price": 1.20},
                },
            },
        })
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "ONDOUSDT",
        "bot_type": "directional_trend",
        "direction": direction,
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.1 if rejected else 0.8,
        "confidence": 0.2 if rejected else 0.8,
        "expected_rr": 0.0 if rejected else 1.8,
        "risk_score": 0.2,
        "params": params,
        "reasons": {
            "candidate_kind": candidate_kind,
            "decision_layers": {
                "no_trade_reasons": ([{"code": "TREND_DIRECTION_UNCONFIRMED", "msg": "direction not confirmed"}] if rejected else []),
            },
            "outcome_policy": {
                "eligible": not rejected,
                "policy_evaluation_eligible": not rejected,
                "sample_role": "excluded" if rejected else "shadow_no_trade",
                "label_due_ts": None if rejected else ts + 43260,
            },
        },
        "blocks": ([] if rejected else []),
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": "test-model",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "outcome_root_rec_id": rec_id,
        "is_outcome_label_root": not rejected,
    }


def test_neutral_trend_builds_rejected_evaluation_not_position_contract() -> None:
    params = recommender_module._params(
        "directional_trend",
        "linear",
        _feature(),
        global_sent=0.0,
        direction="neutral",
        taker_fee_bps=6.0,
        direction_bias="neutral",
        direction_bias_strength=0.0,
        atr_pct_for_grid=0.015,
        cost_model=_cost_model(),
        risk_limits={"min_leverage": 1, "max_leverage": 3},
    )
    assert params["candidate_kind"] == "trend_evaluation_rejected"
    assert params["strategy_family"] == "trend_evaluation"
    assert params["direction_input_valid"] is False
    assert "entry_model" not in params
    plan = recommender_module._build_trade_plan(
        "directional_trend", "linear", _feature(), "neutral", params, cost_model=_cost_model()
    )
    assert plan == {}


def test_rejected_trend_evaluation_never_materializes_outcome_schedule(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "rejected.db"))
    db.init_db(conn)
    row = _row("R-rejected", direction="neutral", candidate_kind="trend_evaluation_rejected", ts=1_800_000_000)
    db.insert_recommendations(conn, [row])
    stored = conn.execute(
        "SELECT direction, is_outcome_label_root, outcome_eligible, policy_evaluation_eligible, outcome_sample_role FROM recommendations WHERE rec_id=?",
        ("R-rejected",),
    ).fetchone()
    assert stored["direction"] == "neutral"
    assert int(stored["is_outcome_label_root"] or 0) == 0
    assert int(stored["outcome_eligible"] or 0) == 0
    assert int(stored["policy_evaluation_eligible"] or 0) == 0
    assert stored["outcome_sample_role"] == "excluded"
    assert conn.execute("SELECT 1 FROM reco_outcome_observability WHERE rec_id=?", ("R-rejected",)).fetchone() is None
    conn.close()


def test_trend_history_excludes_rejected_direction_evaluations(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "history.db"))
    db.init_db(conn)
    db.insert_recommendations(conn, [
        _row("R-rejected", direction="neutral", candidate_kind="trend_evaluation_rejected", ts=1_800_000_000),
        _row("R-long", direction="long", candidate_kind="strategy_recommendation", ts=1_800_000_060),
    ])
    rows, total = db.get_recommendation_history(
        conn, venue="linear", symbol="ONDOUSDT", bot_type="directional_trend", limit=100
    )
    assert total == 1
    assert [row["rec_id"] for row in rows] == ["R-long"]
    assert rows[0]["candidate_kind"] == "strategy_recommendation"
    conn.close()


def test_details_collapses_legacy_neutral_trend_to_single_primary_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "details.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()

    row = _row("R-legacy", direction="neutral", candidate_kind="trend_evaluation_rejected", ts=1_800_000_000)
    row["status"] = "blocked"
    row["blocks"] = [
        {"code": "DIRECTIONAL_TREND_DIRECTION_INVALID"},
        {"code": "DIRECTIONAL_TREND_LEVELS_MISSING"},
        {"code": "DIRECTIONAL_TREND_GEOMETRY_INVALID"},
    ]
    conn = db.connect(str(db_path))
    db.init_db(conn)
    db.insert_recommendations(conn, [row])
    conn.close()

    client = TestClient(app_main.app)
    try:
        payload = client.get("/api/v1/recommendations/R-legacy").json()
    finally:
        client.close()

    assert payload["candidate_kind"] == "trend_evaluation_rejected"
    assert payload["effective_status"] == "no_trade"
    assert payload["directional_exit_levels"]["level_source"] == "trend_evaluation_rejected"
    codes = {
        str(item.get("code"))
        for item in (payload.get("bybit_operator_guard", {}).get("warnings") or [])
        if isinstance(item, dict)
    }
    assert codes == {"TREND_DIRECTION_UNCONFIRMED"}
    assert payload.get("bybit_operator_guard", {}).get("errors") == []
    assert payload.get("outcome_tracking", {}).get("state") in {"not_applicable", "unavailable"}


def test_ui_has_distinct_rejected_evaluation_label_and_no_position_geometry() -> None:
    js = (Path(__file__).resolve().parents[1] / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function candidateKindOf" in js
    assert "function strategyLabelForItem" in js
    assert "Проверка тренда · сигнал отклонён" in js
    assert "trend_evaluation_rejected" in js
    assert "Для отклонённой проверки тренда позиция, TP и SL не формируются" in js
    assert "Направление не подтверждено" in js
    assert "Отклонённая предварительная проверка тренда" in js
    assert "Не планируется" in js
    assert "const historyButtonHtml = rejectedTrendEvaluation" in js


def test_recommender_persists_neutral_trend_only_as_rejected_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_iteration266_directional_trend_shadow import _seed_trending_history, _settings

    conn = db.connect(str(tmp_path / "neutral-run.db"))
    db.init_db(conn)
    now = 1_800_100_000
    monkeypatch.setattr(db, "now_ts", lambda: now)
    monkeypatch.setattr(recommender_module, "gate_candidate", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        recommender_module,
        "aggregate_direction",
        lambda _tf_map: {
            "direction": "neutral",
            "bias": "neutral",
            "direction_confidence": 0.5,
            "scores": {"tactical": 0.0, "structural": 0.0, "all": 0.0},
            "strength": {"tactical": 0.0, "structural": 0.0, "all": 0.0},
            "trendiness": 0.15,
            "coherence": 0.4,
            "regime": "range",
            "regime_confidence": 0.7,
            "structural_veto_applied": False,
            "tf_used": [900, 1800, 3600, 14_400, 86_400],
        },
    )
    _seed_trending_history(conn, now=now)
    latest = conn.execute(
        "SELECT close FROM ohlcv WHERE venue='linear' AND symbol='BTCUSDT' AND tf_sec=60 ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    price = float(latest["close"])
    db.insert_tickers(conn, [{
        "venue": "linear", "symbol": "BTCUSDT", "ts": now - 10,
        "last": price, "bid": price * 0.9999, "ask": price * 1.0001,
        "vol24h": 100_000.0, "turnover24h": 100_000_000.0,
    }])
    db.upsert_funding_rate(conn, [{
        "symbol": "BTCUSDT", "ts": now - 10, "funding_rate": 0.0,
        "next_funding_ts": now + 4 * 3600, "funding_interval_min": 480,
    }])

    recommender_module.run_recommender_once(conn, _settings())
    row = conn.execute(
        "SELECT rec_id, status, direction, params_json, reasons_json, blocks_json, "
        "is_outcome_label_root, outcome_eligible, policy_evaluation_eligible, outcome_sample_role "
        "FROM recommendations WHERE bot_type='directional_trend' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    params = __import__("json").loads(row["params_json"])
    reasons = __import__("json").loads(row["reasons_json"])
    assert row["status"] == "no_trade"
    assert row["direction"] == "neutral"
    assert params["candidate_kind"] == "trend_evaluation_rejected"
    assert params["trade_plan"] == {}
    assert __import__("json").loads(row["blocks_json"]) == []
    assert int(row["is_outcome_label_root"] or 0) == 0
    assert int(row["outcome_eligible"] or 0) == 0
    assert int(row["policy_evaluation_eligible"] or 0) == 0
    assert row["outcome_sample_role"] == "excluded"
    codes = [item["code"] for item in reasons["decision_layers"]["no_trade_reasons"]]
    assert codes == ["TREND_DIRECTION_UNCONFIRMED"]
    assert conn.execute(
        "SELECT 1 FROM reco_outcome_observability WHERE rec_id=?", (row["rec_id"],)
    ).fetchone() is None
    conn.close()


def test_existing_database_upgrade_materializes_candidate_kind_and_removes_waiting_schedule(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-neutral-trend.db"
    conn = db.connect(str(db_path))
    db.init_db(conn)
    row = _row("R-legacy-neutral", direction="neutral", candidate_kind="trend_evaluation_rejected", ts=1_800_200_000)
    # Simulate the pre-v1.4.2 semantics: the row was treated as an outcome root.
    row["is_outcome_label_root"] = True
    row["reasons"]["outcome_policy"] = {
        "eligible": True,
        "policy_evaluation_eligible": True,
        "sample_role": "shadow_no_trade",
        "label_due_ts": row["ts"] + 43_320,
    }
    db.insert_recommendations(conn, [row])
    assert conn.execute(
        "SELECT 1 FROM reco_outcome_observability WHERE rec_id=?", (row["rec_id"],)
    ).fetchone() is not None
    conn.execute("DROP INDEX IF EXISTS idx_reco_candidate_kind_ts")
    conn.execute("ALTER TABLE recommendations DROP COLUMN candidate_kind")
    conn.commit()
    conn.close()

    upgraded = db.connect(str(db_path))
    db.init_db(upgraded)
    columns = {str(item["name"]) for item in upgraded.execute("PRAGMA table_info(recommendations)").fetchall()}
    assert "candidate_kind" in columns
    stored = upgraded.execute(
        "SELECT candidate_kind, is_outcome_label_root, outcome_eligible, "
        "policy_evaluation_eligible, outcome_sample_role FROM recommendations WHERE rec_id=?",
        (row["rec_id"],),
    ).fetchone()
    assert stored["candidate_kind"] == "trend_evaluation_rejected"
    assert int(stored["is_outcome_label_root"] or 0) == 0
    assert int(stored["outcome_eligible"] or 0) == 0
    assert int(stored["policy_evaluation_eligible"] or 0) == 0
    assert stored["outcome_sample_role"] == "excluded"
    assert upgraded.execute(
        "SELECT 1 FROM reco_outcome_observability WHERE rec_id=?", (row["rec_id"],)
    ).fetchone() is None
    continuity = db.get_database_continuity_status(upgraded)
    assert continuity["recommendations_by_candidate_kind"]["trend_evaluation_rejected"] == 1
    upgraded.close()


def test_first_touch_model_rejects_neutral_or_rejected_training_rows() -> None:
    from app.trend_events import fit_trend_event_model

    base = {
        "bot_type": "directional_trend",
        "event_type": "TP_FIRST",
        "ts": 1_800_000_000,
        "label_available_ts": 1_800_043_200,
        "ret": 0.01,
        "score": 0.7,
        "confidence": 0.7,
        "expected_rr": 1.5,
        "risk_score": 0.2,
        "reasons": {},
    }
    neutral = dict(base, direction="neutral", candidate_kind="strategy_recommendation")
    explicitly_rejected = dict(base, direction="long", candidate_kind="trend_evaluation_rejected", ts=1_800_000_100, label_available_ts=1_800_043_300)
    model = fit_trend_event_model([neutral, explicitly_rejected], min_samples=1)
    assert model.n_samples == 0


def test_migrations_persist_candidate_kind_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("init.sql", "init_postgres.sql"):
        sql = (root / "migrations" / name).read_text(encoding="utf-8")
        assert "candidate_kind TEXT NOT NULL DEFAULT 'strategy_recommendation'" in sql
        assert "idx_reco_candidate_kind_ts" in sql


def test_neutral_direction_overrides_stale_strategy_candidate_kind() -> None:
    from app import main as app_main
    from app.strategy_router import evaluate_candidate

    stale = {
        "bot_type": "directional_trend",
        "direction": "neutral",
        "candidate_kind": "strategy_recommendation",
        "status": "recommended",
        "params": {"candidate_kind": "strategy_recommendation"},
        "reasons": {"candidate_kind": "strategy_recommendation"},
    }
    assert app_main._candidate_kind_for_reco(stale) == "trend_evaluation_rejected"
    evaluation = evaluate_candidate(stale)
    assert evaluation["eligible"] is False
    assert "TREND_DIRECTION_UNCONFIRMED" in evaluation["reason_codes"]


def test_rejected_trend_evaluation_cannot_materialize_execution_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    db_path = tmp_path / "execution-rejected.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(app_main, "_prefetch_execution_bybit_meta", lambda *_args, **_kwargs: {})

    conn = db.connect(str(db_path))
    db.init_db(conn)
    row = _row("R-no-exec", direction="neutral", candidate_kind="trend_evaluation_rejected", ts=1_800_300_000)
    db.insert_recommendations(conn, [row])
    with pytest.raises(HTTPException) as exc:
        app_main._materialize_bot_from_rec(conn, row["rec_id"], operator="test")
    assert exc.value.status_code == 409
    assert "diagnostic only" in str(exc.value.detail)
    assert conn.execute("SELECT 1 FROM bot_instances WHERE origin_rec_id=?", (row["rec_id"],)).fetchone() is None
    conn.close()
