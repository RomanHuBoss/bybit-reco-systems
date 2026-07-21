from __future__ import annotations

from pathlib import Path

from app import db
from app.outcomes import _grid_outcome


def _seed_flat(conn, base_ts: int, minutes: int = 2) -> None:
    db.upsert_ohlcv(
        conn,
        [
            {
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": 60,
                "ts": base_ts + i * 60,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1_000.0,
            }
            for i in range(minutes)
        ],
    )


def _params(base_ts: int) -> dict:
    cost = {
        "execution_cost_bps": 0.0,
        "grid_round_trip_fee_bps": 0.0,
        "funding_rate": 0.001,
        "next_funding_ts": base_ts + 60,
        "funding_interval_min": 60,
        "expected_funding_events": 1,
        "directional_funding_bps_per_event": 10.0,
        "expected_funding_bps": 10.0,
    }
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "cost_model": dict(cost),
        "trade_plan": {
            "grid_count": 2,
            "cost_model": dict(cost),
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 98.0, "upper": 102.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def test_missing_settlement_is_reported_as_transient_wait(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "wait.db"))
    try:
        db.init_db(conn)
        base = 1_720_900_000
        _seed_flat(conn, base)
        diagnostics: dict[str, object] = {}
        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base,
            base + 120,
            "long",
            _params(base),
            diagnostics=diagnostics,
        )
        assert result is None
        assert diagnostics["reason"] == "missing_funding_settlement"
        assert diagnostics["transient"] is True
        assert diagnostics["missing_funding_ts"] == base + 60
    finally:
        conn.close()


def test_invalid_funding_aliases_have_structured_reason(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "invalid.db"))
    try:
        db.init_db(conn)
        base = 1_721_000_000
        _seed_flat(conn, base)
        params = _params(base)
        params["trade_plan"]["cost_model"]["funding_rate"] = 0.002
        diagnostics: dict[str, object] = {}
        result = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base,
            base + 120,
            "long",
            params,
            diagnostics=diagnostics,
        )
        assert result is None
        assert diagnostics["reason"] == "invalid_funding_contract"
        assert diagnostics["transient"] is False
        assert diagnostics["issues"]
    finally:
        conn.close()


def test_release_contract_bumped_for_outcome_diagnostics() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'version="1.4.4"' in source
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source


def test_worker_logs_wait_not_invalid_contract_for_missing_settlement(tmp_path: Path, monkeypatch) -> None:
    from app import outcomes as outcomes_module

    conn = db.connect(str(tmp_path / "worker.db"))
    try:
        db.init_db(conn)
        base = 1_721_100_000
        monkeypatch.setitem(outcomes_module.BOT_HORIZONS, "futures_grid", 120)
        monkeypatch.setattr(db, "now_ts", lambda: base + 10_000)
        params = _params(base + 120)
        db.insert_recommendations(
            conn,
            [
                {
                    "rec_id": "R-wait-funding",
                    "ts": base + 90,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "futures_grid",
                    "direction": "long",
                    "account_mode": "unified",
                    "margin_mode": "cross",
                    "score": 0.7,
                    "confidence": 0.6,
                    "expected_rr": 1.2,
                    "risk_score": 0.2,
                    "params": params,
                    "reasons": {"risk_checks": {"passed": True, "blocks": []}},
                    "blocks": [],
                    "status": "recommended",
                    "ttl_sec": 900,
                    "model_version": "test",
                    "features_ref_ts": base,
                }
            ],
        )
        db.upsert_ohlcv(
            conn,
            [
                {
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "tf_sec": 60,
                    "ts": base + offset,
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1_000.0,
                }
                for offset in (60, 120, 180, 240)
            ],
        )

        assert outcomes_module.compute_outcomes_once(conn, max_to_process=10) == 0
        row = conn.execute(
            "SELECT action, details_json FROM decision_log WHERE rec_id=? ORDER BY id DESC LIMIT 1",
            ("R-wait-funding",),
        ).fetchone()
        assert row is not None
        assert row["action"] == "OUTCOME_WAIT_FUNDING_SETTLEMENT"
        details = db._json_loads_mapping_or_default(row["details_json"], {})
        assert details["reason"] == "missing_funding_settlement"
        assert details["transient"] is True

        # A second cycle within the cooldown must not spam the decision log.
        assert outcomes_module.compute_outcomes_once(conn, max_to_process=10) == 0
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM decision_log WHERE action=? AND rec_id=?",
            ("OUTCOME_WAIT_FUNDING_SETTLEMENT", "R-wait-funding"),
        ).fetchone()["n"]
        assert count == 1
    finally:
        conn.close()
