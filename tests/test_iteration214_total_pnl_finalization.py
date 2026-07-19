from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.outcomes import _grid_outcome, compute_outcomes_once


def _seed_path(conn, *, base_ts: int, closes: list[float]) -> None:
    rows = []
    previous = 100.0
    for index, close in enumerate(closes):
        rows.append({
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": base_ts + index * 60,
            "open": float(previous),
            "high": float(max(previous, close)),
            "low": float(min(previous, close)),
            "close": float(close),
            "volume": 1_000.0,
        })
        previous = float(close)
    db.upsert_ohlcv(conn, rows)


def _params(*, cost_bps: float = 0.0, funding: dict | None = None) -> dict:
    cost_model = {
        "execution_cost_bps": float(cost_bps),
        "expected_funding_bps": 0.0,
    }
    cost_model.update(funding or {})
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": 99.0,
        "price_range_upper": 101.0,
        "cost_model": cost_model,
        "trade_plan": {
            "grid_count": 2,
            "levels": {
                "range": {"lower": 99.0, "upper": 101.0},
                "kill_switch": {"lower": 95.0, "upper": 105.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def test_terminal_residual_position_pays_closing_execution_cost(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "terminal-close.db"))
    try:
        db.init_db(conn)
        base_ts = 1_704_000_000
        _seed_path(conn, base_ts=base_ts, closes=[102.0])

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            102.0,
            base_ts,
            base_ts + 60,
            "neutral",
            _params(cost_bps=10.0),
        )

        # Sell 1 slot at 101, then terminate the remaining short at 102.
        # Gross=-1. Fees/slippage proxy is 5 bps on each leg:
        # 101*0.0005 + 102*0.0005 = 0.1015. One-way neutral
        # full initial neutral commitment is buy 99 plus sell 101 = 200 USDT.
        assert ret_proxy == pytest.approx(-1.1015 / 200.0)
        assert success == 0
    finally:
        conn.close()


def test_positive_net_total_pnl_below_five_bps_is_still_a_win(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "small-positive.db"))
    try:
        db.init_db(conn)
        base_ts = 1_704_100_000
        _seed_path(conn, base_ts=base_ts, closes=[101.1, 99.9])

        success, ret_proxy = _grid_outcome(
            conn,
            "linear",
            "BTCUSDT",
            100.0,
            100.0,
            base_ts,
            base_ts + 120,
            "neutral",
            _params(cost_bps=95.0),
        )

        assert 0.0 < ret_proxy < 0.0005
        assert success == 1
    finally:
        conn.close()


def _recommendation(*, rec_id: str, signal_ts: int, direction: str, params: dict) -> dict:
    return {
        "rec_id": rec_id,
        "ts": signal_ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": direction,
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.6,
        "confidence": 0.6,
        "expected_rr": 1.0,
        "risk_score": 0.2,
        "params": params,
        "reasons": {"risk_checks": {"passed": True, "blocks": []}},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "test-v1",
        "features_ref_ts": signal_ts,
    }


def _seed_flat_horizon(conn, *, entry_ts: int, horizon_sec: int, price: float = 100.1) -> None:
    rows = []
    for ts in range(entry_ts, entry_ts + horizon_sec + 60, 60):
        rows.append({
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts,
            "open": float(price),
            "high": float(price + 0.05),
            "low": float(price - 0.05),
            "close": float(price),
            "volume": 1_000.0,
        })
    db.upsert_ohlcv(conn, rows)


def test_neutral_grid_with_no_inventory_pays_no_funding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "no-inventory-funding.db"))
    try:
        db.init_db(conn)
        signal_ts = 1_704_199_980
        entry_ts = signal_ts + 60
        horizon_sec = 6 * 3600
        params = _params(
            funding={
                "funding_rate": 0.001,
                "directional_funding_bps_per_event": 10.0,
                "expected_funding_events": 1,
                "expected_funding_bps": 10.0,
                "next_funding_ts": entry_ts + 3600,
                "funding_interval_min": 480,
            }
        )
        params["label_horizon_hours"] = 6
        db.insert_recommendations(conn, [_recommendation(
            rec_id="R-neutral-no-inventory",
            signal_ts=signal_ts,
            direction="neutral",
            params=params,
        )])
        _seed_flat_horizon(conn, entry_ts=entry_ts, horizon_sec=horizon_sec)
        monkeypatch.setattr(db, "now_ts", lambda: entry_ts + 12 * 3600 + 600)

        assert compute_outcomes_once(conn, max_to_process=10) == 1
        row = conn.execute(
            "SELECT ret, success FROM reco_outcomes WHERE rec_id=?",
            ("R-neutral-no-inventory",),
        ).fetchone()
        assert row is not None
        assert float(row["ret"]) == pytest.approx(0.0)
        assert int(row["success"]) == 0
    finally:
        conn.close()


def test_funding_cost_scales_to_position_value_at_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "inventory-funding.db"))
    try:
        db.init_db(conn)
        signal_ts = 1_704_300_000
        entry_ts = signal_ts + 60
        horizon_sec = 6 * 3600
        params = _params(
            funding={
                "funding_rate": 0.001,
                "directional_funding_bps_per_event": 10.0,
                "expected_funding_events": 1,
                "expected_funding_bps": 10.0,
                "next_funding_ts": entry_ts + 3600,
                "funding_interval_min": 480,
            }
        )
        params["label_horizon_hours"] = 6
        db.insert_recommendations(conn, [_recommendation(
            rec_id="R-long-half-inventory",
            signal_ts=signal_ts,
            direction="long",
            params=params,
        )])
        # One Long slot pays 0.1 USDT. Exact commitment is the initial
        # 100-USDT slot plus the adverse buy order at 99: 199 USDT.
        _seed_flat_horizon(conn, entry_ts=entry_ts, horizon_sec=horizon_sec, price=100.0)
        db.upsert_funding_settlements(conn, [{
            "symbol": "BTCUSDT", "ts": entry_ts + 3600, "funding_rate": 0.001,
        }])
        monkeypatch.setattr(db, "now_ts", lambda: entry_ts + 12 * 3600 + 600)

        assert compute_outcomes_once(conn, max_to_process=10) == 1
        row = conn.execute(
            "SELECT ret, success FROM reco_outcomes WHERE rec_id=?",
            ("R-long-half-inventory",),
        ).fetchone()
        assert row is not None
        assert float(row["ret"]) == pytest.approx(-0.1 / 199.0)
        assert int(row["success"]) == 0
    finally:
        conn.close()


def test_outcome_contract_is_bumped_for_inventory_aware_finalization() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in source
    assert 'version="1.0.78"' in source


def test_short_inventory_pays_negative_funding_at_position_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "short-adverse-funding.db"))
    try:
        db.init_db(conn)
        signal_ts = 1_704_399_960
        entry_ts = signal_ts + 60
        horizon_sec = 6 * 3600
        params = _params(
            funding={
                "funding_rate": -0.001,
                "directional_funding_bps_per_event": 10.0,
                "expected_funding_events": 1,
                "expected_funding_bps": 10.0,
                "next_funding_ts": entry_ts + 3600,
                "funding_interval_min": 480,
            }
        )
        params["label_horizon_hours"] = 6
        db.insert_recommendations(conn, [_recommendation(
            rec_id="R-short-half-inventory",
            signal_ts=signal_ts,
            direction="short",
            params=params,
        )])
        _seed_flat_horizon(conn, entry_ts=entry_ts, horizon_sec=horizon_sec, price=100.0)
        db.upsert_funding_settlements(conn, [{
            "symbol": "BTCUSDT", "ts": entry_ts + 3600, "funding_rate": -0.001,
        }])
        monkeypatch.setattr(db, "now_ts", lambda: entry_ts + 12 * 3600 + 600)

        assert compute_outcomes_once(conn, max_to_process=10) == 1
        row = conn.execute(
            "SELECT ret, success FROM reco_outcomes WHERE rec_id=?",
            ("R-short-half-inventory",),
        ).fetchone()
        assert row is not None
        assert float(row["ret"]) == pytest.approx(-0.1 / 201.0)
        assert int(row["success"]) == 0
    finally:
        conn.close()


def test_unknown_funding_schedule_uses_reached_inventory_not_full_capital(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "unknown-schedule-funding.db"))
    try:
        db.init_db(conn)
        signal_ts = 1_704_499_980
        entry_ts = signal_ts + 60
        horizon_sec = 6 * 3600
        params = _params(
            funding={
                "funding_rate": 0.001,
                "directional_funding_bps_per_event": 10.0,
                "expected_funding_events": 1,
                "expected_funding_bps": 10.0,
                "next_funding_ts": None,
                "funding_interval_min": None,
            }
        )
        params["label_horizon_hours"] = 6
        db.insert_recommendations(conn, [_recommendation(
            rec_id="R-long-unknown-schedule",
            signal_ts=signal_ts,
            direction="long",
            params=params,
        )])
        _seed_flat_horizon(conn, entry_ts=entry_ts, horizon_sec=horizon_sec, price=100.0)
        db.upsert_funding_settlements(conn, [{
            "symbol": "BTCUSDT", "ts": entry_ts + 3600, "funding_rate": 0.001,
        }])
        monkeypatch.setattr(db, "now_ts", lambda: entry_ts + 12 * 3600 + 600)

        assert compute_outcomes_once(conn, max_to_process=10) == 1
        row = conn.execute(
            "SELECT ret, success FROM reco_outcomes WHERE rec_id=?",
            ("R-long-unknown-schedule",),
        ).fetchone()
        assert row is not None
        assert float(row["ret"]) == pytest.approx(-0.1 / 199.0)
        assert int(row["success"]) == 0
    finally:
        conn.close()


def test_inventory_funding_model_rejects_non_exact_millisecond_timestamp() -> None:
    from app.outcomes import _extract_inventory_funding_model

    malformed = _params(funding={"next_funding_ts": 1_700_000_000_123})
    valid = _params(funding={"next_funding_ts": 1_700_000_000_000})

    assert _extract_inventory_funding_model(malformed)["next_funding_ts"] is None
    assert _extract_inventory_funding_model(valid)["next_funding_ts"] == 1_700_000_000
