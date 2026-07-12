from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import alerts, db
from app.calibration import (
    LogRegScaler,
    PlattScaler,
    load_logreg_from_db,
    load_platt_from_db,
)
from app.main import _validate_trade_plan_against_bybit_meta


def _valid_recommendation(*, rec_id: str = "R-final", account_mode: str = "unified") -> dict:
    return {
        "rec_id": rec_id,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
        "ts": 1_700_000_000,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "long",
        "account_mode": account_mode,
        "margin_mode": "cross",
        "score": 0.25,
        "confidence": 0.70,
        "expected_rr": 1.10,
        "risk_score": 0.20,
        "params": {
            "grid_count": 8,
            "grid_levels": 8,
            "grid_type": "arithmetic",
            "leverage": 3,
            "margin_mode": "cross",
            "trade_plan": {
                "reference_price": 100.0,
                "grid_type": "arithmetic",
                "levels": {
                    "range": {"lower": 96.0, "upper": 104.0},
                    "kill_switch": {"lower": 94.0, "upper": 106.0},
                    "grid_step": {"step_abs": 1.0, "step_pct": 1.0},
                    "tp_per_leg": {"abs": 1.0, "pct": 1.0},
                },
                "sizing": {
                    "qty_per_order": 0.06,
                    "order_notional_usdt": 6.0,
                    "estimated_total_order_notional_usdt": 48.0,
                    "estimated_margin_required_usdt": 48.0 / 3.0,
                },
            },
            "economics": {
                "gross_profit_bps": 70.0,
                "execution_cost_bps": 12.0,
                "funding_cost_bps": 0.0,
                "net_profit_bps": 58.0,
                "liquidation_buffer_pct": 20.0,
            },
        },
        "reasons": {},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 900,
        "model_version": "final-reaudit",
        "features_ref_ts": 1_699_999_940,
    }


def _valid_meta(**overrides) -> dict:
    meta = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contract_type": "LinearPerpetual",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "delivery_time": 0,
        "tick_size": "0.1",
        "min_price": "0.1",
        "max_price": "1000000",
        "qty_step": "0.001",
        "min_order_qty": "0.001",
        "max_order_qty": "1000",
        "min_notional": "5",
        "min_leverage": "1",
        "max_leverage": "100",
        "leverage_step": "0.01",
        "unified_margin_trade": True,
    }
    meta.update(overrides)
    return meta


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_platt_predict_nonfinite_input_fails_neutral(bad_value: float) -> None:
    model = PlattScaler(a=1.0, b=0.0, fitted=True)
    assert model.predict(bad_value) == pytest.approx(0.5)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_logreg_predict_nonfinite_feature_fails_neutral(bad_value: float) -> None:
    model = LogRegScaler(coef=[2.0], intercept=0.0, fitted=True)
    assert model.predict([bad_value]) == pytest.approx(0.5)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_logreg_score_only_nonfinite_input_fails_neutral(bad_value: float) -> None:
    model = LogRegScaler(fitted=True)
    assert model.predict_score_only(bad_value) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("coef", "intercept"),
    [([float("nan")], 0.0), ([1.0], float("inf"))],
)
def test_logreg_predict_nonfinite_model_parameters_fail_neutral(
    coef: list[float], intercept: float
) -> None:
    model = LogRegScaler(coef=coef, intercept=intercept, fitted=True)
    assert model.predict([0.25]) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "logreg",
            "coef": [0.4],
            "intercept": 0.2,
            "fitted": "false",
            "n_samples": 100,
            "ts": 111,
            "platt": {"a": 1.0, "b": 0.0, "fitted": False, "ts": 222},
        },
        {
            "type": "platt",
            "coef": [0.4],
            "intercept": 0.2,
            "fitted": True,
            "n_samples": 100,
            "ts": 111,
            "platt": {"a": 1.0, "b": 0.0, "fitted": False, "ts": 222},
        },
        {
            "type": "logreg",
            "coef": [0.4],
            "intercept": 0.2,
            "fitted": True,
            "n_samples": 100,
            "ts": 111,
            "platt": {"a": 1.0, "b": 0.0, "fitted": "false", "ts": 222},
        },
    ],
)
def test_logreg_loader_rejects_ambiguous_fitted_or_wrong_model_type(tmp_path: Path, payload: dict) -> None:
    conn = db.connect(str(tmp_path / "bad-logreg.db"))
    try:
        db.init_db(conn)
        conn.execute(
            "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES (?, ?, ?)",
            ("bad_logreg", json.dumps(payload), 1),
        )
        conn.commit()
        assert load_logreg_from_db(conn, "bad_logreg") is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "platt", "a": 1.0, "b": 0.0, "fitted": "false", "ts": 333},
        {"type": "logreg", "a": 1.0, "b": 0.0, "fitted": True, "ts": 333},
    ],
)
def test_platt_loader_rejects_ambiguous_fitted_or_wrong_model_type(tmp_path: Path, payload: dict) -> None:
    conn = db.connect(str(tmp_path / "bad-platt.db"))
    try:
        db.init_db(conn)
        conn.execute(
            "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES (?, ?, ?)",
            ("bad_platt", json.dumps(payload), 1),
        )
        conn.commit()
        assert load_platt_from_db(conn, "bad_platt") is None
    finally:
        conn.close()


def test_publication_lineage_backfill_does_not_treat_string_false_as_active_reuse(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "lineage-bool.db"))
    try:
        db.init_db(conn)
        first = _valid_recommendation(rec_id="R-root")
        second = _valid_recommendation(rec_id="R-independent")
        second["ts"] += 60
        second["features_ref_ts"] += 60
        db.insert_recommendations(conn, [first, second])
        poisoned_reasons = {
            "publication_dedupe": {
                "active_reuse": "false",
                "previous_rec_id": "R-root",
                "decision": "new_publication",
            }
        }
        conn.execute(
            "UPDATE recommendations SET reasons_json=?, publication_root_rec_id='', is_outcome_label_root=0 WHERE rec_id=?",
            (json.dumps(poisoned_reasons), "R-independent"),
        )
        conn.commit()

        db.backfill_recommendation_publication_lineage(conn)
        row = conn.execute(
            "SELECT publication_root_rec_id, is_outcome_label_root FROM recommendations WHERE rec_id=?",
            ("R-independent",),
        ).fetchone()
        assert row is not None
        assert row["publication_root_rec_id"] == "R-independent"
        assert int(row["is_outcome_label_root"]) == 1
    finally:
        conn.close()


def test_strict_preflight_rejects_explicit_unsupported_account_mode() -> None:
    rec = _valid_recommendation(account_mode="hedge")
    result = _validate_trade_plan_against_bybit_meta(
        rec,
        _valid_meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    codes = {item["code"] for item in result["errors"]}
    assert "ACCOUNT_MODE_UNSUPPORTED" in codes


def test_strict_preflight_rejects_missing_account_mode() -> None:
    rec = _valid_recommendation(account_mode="")
    result = _validate_trade_plan_against_bybit_meta(
        rec,
        _valid_meta(),
        require_meta=True,
        require_execution_plan=True,
    )
    codes = {item["code"] for item in result["errors"]}
    assert "ACCOUNT_MODE_MISSING" in codes


def test_strict_preflight_rejects_instrument_explicitly_incompatible_with_unified_margin() -> None:
    rec = _valid_recommendation(account_mode="unified")
    result = _validate_trade_plan_against_bybit_meta(
        rec,
        _valid_meta(unified_margin_trade=False),
        require_meta=True,
        require_execution_plan=True,
    )
    codes = {item["code"] for item in result["errors"]}
    assert "BYBIT_UNIFIED_MARGIN_UNSUPPORTED" in codes


def test_telegram_transport_requires_literal_boolean_true(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"ok": "false"}

    monkeypatch.setattr(alerts.httpx, "post", lambda *args, **kwargs: Response())
    assert alerts.send_telegram("token", "chat", "message") is False
