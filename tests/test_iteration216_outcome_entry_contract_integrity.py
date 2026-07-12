from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app import outcomes as outcomes_module
from app.outcomes import _grid_outcome, compute_outcomes_once


def _params(*, lower: float = 99.0, upper: float = 101.0, funding: dict | None = None) -> dict:
    cost_model = {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0}
    cost_model.update(funding or {})
    return {
        "grid_count": 2,
        "grid_levels": 2,
        "price_range_lower": lower,
        "price_range_upper": upper,
        "cost_model": dict(cost_model),
        "trade_plan": {
            "grid_count": 2,
            "cost_model": dict(cost_model),
            "levels": {
                "range": {"lower": lower, "upper": upper},
                "kill_switch": {"lower": lower - 5.0, "upper": upper + 5.0},
                "tp_per_leg": {"abs": (upper - lower) / 2.0},
            },
        },
    }


def _recommendation(rec_id: str, *, published_ts: int, features_ref_ts: int, params: dict) -> dict:
    return {
        "rec_id": rec_id,
        "ts": published_ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.7,
        "confidence": 0.6,
        "expected_rr": 1.2,
        "risk_score": 0.2,
        "params": params,
        "reasons": {"risk_checks": {"passed": True, "blocks": []}},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "bybit-taxonomy-v3-mean-reversion",
        "features_ref_ts": features_ref_ts,
    }


def _seed_rows(conn, rows: list[tuple[int, float, float]]) -> None:
    db.upsert_ohlcv(conn, [
        {
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts,
            "open": open_px,
            "high": max(open_px, close_px),
            "low": min(open_px, close_px),
            "close": close_px,
            "volume": 1_000.0,
        }
        for ts, open_px, close_px in rows
    ])


def test_outcome_entry_is_first_exact_candle_open_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "post-publication-entry.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_000_000
        published_ts = base_ts + 90
        monkeypatch.setitem(outcomes_module.BOT_HORIZONS, "futures_grid", 120)
        monkeypatch.setattr(db, "now_ts", lambda: base_ts + 10_000)
        db.insert_recommendations(conn, [
            _recommendation(
                "R-post-publication",
                published_ts=published_ts,
                features_ref_ts=base_ts,
                params=_params(lower=99.0, upper=103.0),
            )
        ])
        _seed_rows(conn, [
            (base_ts + 60, 100.0, 100.0),   # already opened before publication
            (base_ts + 120, 101.0, 101.0),  # first observable open after publication
            (base_ts + 180, 101.0, 101.0),
            (base_ts + 240, 101.0, 101.0),  # exact horizon exit open
        ])

        assert compute_outcomes_once(conn, max_to_process=10) == 1
        row = conn.execute(
            "SELECT entry_close, label_available_ts FROM reco_outcomes WHERE rec_id=?",
            ("R-post-publication",),
        ).fetchone()
        assert row["entry_close"] == pytest.approx(101.0)
        assert row["label_available_ts"] == base_ts + 240
    finally:
        conn.close()


def test_post_publication_price_outside_grid_is_not_fabricated_as_zero_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "outside-range.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_100_000
        monkeypatch.setitem(outcomes_module.BOT_HORIZONS, "futures_grid", 120)
        monkeypatch.setattr(db, "now_ts", lambda: base_ts + 10_000)
        db.insert_recommendations(conn, [
            _recommendation(
                "R-outside-range",
                published_ts=base_ts + 90,
                features_ref_ts=base_ts,
                params=_params(lower=99.0, upper=101.0),
            )
        ])
        _seed_rows(conn, [
            (base_ts + 60, 100.0, 100.0),
            (base_ts + 120, 106.0, 106.0),
            (base_ts + 180, 106.0, 106.0),
            (base_ts + 240, 106.0, 106.0),
        ])

        assert compute_outcomes_once(conn, max_to_process=10) == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM reco_outcomes").fetchone()["n"] == 0
    finally:
        conn.close()


def test_conflicting_valid_range_aliases_make_outcome_unavailable(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "range-conflict.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_200_000
        _seed_rows(conn, [
            (base_ts, 100.0, 101.0),
            (base_ts + 60, 101.0, 100.0),
        ])
        params = _params(lower=99.0, upper=101.0)
        params["price_range_lower"] = 90.0
        params["price_range_upper"] = 110.0

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "neutral", params,
        ) is None
    finally:
        conn.close()


def test_invalid_grid_contract_is_unavailable_not_a_zero_return_failure(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "invalid-grid.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_300_000
        _seed_rows(conn, [(base_ts, 100.0, 100.0)])
        params = _params()
        params["grid_count"] = 0
        params["grid_levels"] = 0
        params["trade_plan"]["grid_count"] = 0

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 60, "neutral", params,
        ) is None
    finally:
        conn.close()


@pytest.mark.parametrize("bad_primary", [-0.001, True])
def test_conflicting_or_invalid_primary_funding_alias_does_not_create_a_synthetic_label(
    tmp_path: Path,
    bad_primary: object,
) -> None:
    conn = db.connect(str(tmp_path / f"funding-conflict-{bad_primary}.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_400_000
        _seed_rows(conn, [
            (base_ts, 100.0, 100.0),
            (base_ts + 60, 100.0, 100.0),
        ])
        params = _params(funding={
            "funding_rate": bad_primary,
            "next_funding_ts": base_ts + 60,
            "funding_interval_min": 480,
            "expected_funding_events": 1,
            "directional_funding_bps_per_event": -10.0 if bad_primary is not True else 10.0,
            "expected_funding_bps": -10.0 if bad_primary is not True else 10.0,
        })
        params["trade_plan"]["cost_model"].update({
            "funding_rate": 0.001,
            "next_funding_ts": base_ts + 60,
            "funding_interval_min": 480,
            "expected_funding_events": 1,
            "directional_funding_bps_per_event": 10.0,
            "expected_funding_bps": 10.0,
        })

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "long", params,
        ) is None
    finally:
        conn.close()


def test_identical_funding_aliases_remain_labelable(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "funding-identical.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_500_000
        _seed_rows(conn, [
            (base_ts, 100.0, 100.0),
            (base_ts + 60, 100.0, 100.0),
        ])
        params = _params(funding={
            "funding_rate": 0.001,
            "next_funding_ts": base_ts + 60,
            "funding_interval_min": 480,
            "expected_funding_events": 1,
            "directional_funding_bps_per_event": 10.0,
            "expected_funding_bps": 10.0,
        })

        result = _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "long", params,
        )
        assert result is not None
        success, ret_proxy = result
        assert success == 0
        assert ret_proxy == pytest.approx(-0.1 / 199.0)
    finally:
        conn.close()



def test_conflicting_valid_grid_count_aliases_make_outcome_unavailable(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "grid-count-conflict.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_550_000
        _seed_rows(conn, [
            (base_ts, 100.0, 101.0),
            (base_ts + 60, 101.0, 100.0),
        ])
        params = _params(lower=99.0, upper=101.0)
        params["grid_levels"] = 4

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "neutral", params,
        ) is None
    finally:
        conn.close()


def test_malformed_explicit_range_alias_does_not_fall_through_to_another_geometry(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "range-invalid-primary.db"))
    try:
        db.init_db(conn)
        base_ts = 1_707_560_000
        _seed_rows(conn, [
            (base_ts, 100.0, 101.0),
            (base_ts + 60, 101.0, 100.0),
        ])
        params = _params(lower=99.0, upper=101.0)
        params["price_range_lower"] = "NaN"
        params["price_range_upper"] = "Infinity"

        assert _grid_outcome(
            conn, "linear", "BTCUSDT", 100.0, 100.0,
            base_ts, base_ts + 120, "neutral", params,
        ) is None
    finally:
        conn.close()

def test_outcome_contract_is_bumped_for_post_publication_entry_integrity() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v15"' in source
    assert 'version="1.0.34"' in source
