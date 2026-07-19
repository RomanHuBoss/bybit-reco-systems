from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app import db, outcomes
from app import main as main_module
from app import recommender as recommender_module
from app.bot_types import DIRECTIONAL_BOT_TYPES, GRID_BOT_TYPES, SUPPORTED_BOT_TYPES
from app.calibration import BOT_CALIB_KEYS, LogRegScaler
from app.recommender import (
    RECOMMENDER_MODEL_VERSION,
    TREND_RECOMMENDER_MODEL_VERSION,
    _build_trade_plan,
    _params,
    _score,
    calibration_lineage_diagnostics,
    run_recommender_once,
)
from app.settings import Settings


def _trend_feature(direction: str = "long") -> dict:
    sign = 1.0 if direction == "long" else -1.0
    return {
        "price": 100.0,
        "atr_pct": 0.005,
        "_atr_pct_15m": 0.012,
        "_atr_pct_1h": 0.020,
        "_atr_pct_4h": 0.030,
        "spread_bps": 2.0,
        "range_score": 0.10,
        "trend_strength": 0.82,
        "_direction_agg": {
            "direction": direction,
            "bias": direction,
            "scores": {"tactical": 0.65 * sign, "structural": 0.72 * sign, "all": 0.69 * sign},
            "strength": {"tactical": 0.65, "structural": 0.72, "all": 0.69},
            "trendiness": 0.82,
            "coherence": 0.86,
            "regime": "trend",
            "regime_confidence": 0.84,
            "mean_reversion_score": 0.08,
            "mean_reversion_evidence_valid": True,
            "mean_reversion_tf_count": 5,
            "mean_reversion_tf_coverage": 1.0,
            "structural_veto_applied": False,
            "tf_used": [900, 1800, 3600, 14_400, 86_400],
        },
    }


def _cost_model() -> dict:
    return {
        "spread_bps": 2.0,
        "execution_cost_bps": 10.0,
        "net_cost_bps": 10.0,
        "funding_cost_bps_for_approval": 0.0,
        "expected_funding_bps": 0.0,
        "expected_funding_events": 0,
        "funding_interval_min": 480,
    }


def test_directional_trend_is_separate_supported_strategy_family() -> None:
    assert "directional_trend" in SUPPORTED_BOT_TYPES
    assert "directional_trend" in DIRECTIONAL_BOT_TYPES
    assert "directional_trend" not in GRID_BOT_TYPES
    assert BOT_CALIB_KEYS["directional_trend"].startswith("logreg_directional_trend_")


def test_directional_trend_has_distinct_audit_model_identity_without_resetting_grid() -> None:
    assert TREND_RECOMMENDER_MODEL_VERSION.startswith(RECOMMENDER_MODEL_VERSION + "+")
    assert TREND_RECOMMENDER_MODEL_VERSION.endswith("directional-trend-v1")


def test_global_calibrator_does_not_pool_grid_and_trend_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []

    def fake_fit(rows, **_kwargs):
        captured.extend(list(rows))
        return LogRegScaler(fitted=False)

    monkeypatch.setattr(recommender_module, "fit_logreg", fake_fit)
    monkeypatch.setattr(recommender_module, "_apply_outcome_observability_gate", lambda model, _evidence: model)
    recommender_module._fit_global_logreg(
        None,
        min_samples=80,
        policy_fingerprint="a" * 64,
        settings_obj=object(),
        observability={"provided": True},
        policy_rows=[
            {"bot_type": "futures_grid", "rec_id": "R-grid"},
            {"bot_type": "directional_trend", "rec_id": "R-trend"},
        ],
    )
    assert [row["rec_id"] for row in captured] == ["R-grid"]


def test_directional_trend_score_rewards_trend_not_mean_reversion() -> None:
    score, confidence, reasons = _score(
        "directional_trend",
        "linear",
        _trend_feature("long"),
        taker_fee_bps=6.0,
        global_sent=0.25,
        cost_model=_cost_model(),
        sentiment_has_data=True,
    )
    assert score > 0.30
    assert confidence > 0.60
    positive = {str(item.get("feature")) for item in reasons["top_positive_factors"]}
    assert {"trend_strength", "direction_strength", "coherence"}.issubset(positive)
    assert "trend" in reasons["summary"].lower()


def test_directional_trend_plan_is_single_position_without_grid_averaging() -> None:
    feature = _trend_feature("long")
    params = _params(
        "directional_trend",
        "linear",
        feature,
        global_sent=0.2,
        direction="long",
        taker_fee_bps=6.0,
        direction_bias="long",
        direction_bias_strength=0.69,
        atr_pct_for_grid=0.02,
        cost_model=_cost_model(),
        risk_limits={"min_leverage": 3, "max_leverage": 5},
    )
    plan = _build_trade_plan("directional_trend", "linear", feature, "long", params, cost_model=_cost_model())
    assert params["strategy_family"] == "directional_trend"
    assert params["entry_model"] == "single_position_no_pyramiding"
    assert params.get("grid_count") in (None, 0)
    assert plan["entry_model"] == "single_position_no_pyramiding"
    assert plan["levels"]["take_profit"]["price"] > plan["reference_price"]
    assert plan["levels"]["stop_loss"]["price"] < plan["reference_price"]
    assert "grid_step" not in plan["levels"]
    assert plan["averaging_allowed"] is False


def _seed_candles(conn, *, ts0: int, candles: list[tuple[float, float, float, float]]) -> None:
    rows = []
    for i, (open_px, high, low, close) in enumerate(candles):
        rows.append({
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts0 + i * 60,
            "open": open_px,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0,
        })
    db.upsert_ohlcv(conn, rows)


def _trend_outcome_params() -> dict:
    return {
        "strategy_family": "directional_trend",
        "entry_model": "single_position_no_pyramiding",
        "cost_model": {
            "execution_cost_bps": 10.0,
            "expected_funding_events": 0,
        },
        "trade_plan": {
            "reference_price": 100.0,
            "entry_model": "single_position_no_pyramiding",
            "levels": {
                "take_profit": {"price": 104.0},
                "stop_loss": {"price": 98.0},
            },
        },
    }


def test_directional_trend_outcome_uses_first_unambiguous_tp_or_sl(tmp_path: Path) -> None:
    fn = getattr(outcomes, "_directional_trend_outcome", None)
    assert callable(fn)
    conn = db.connect(str(tmp_path / "trend_outcome.db"))
    db.init_db(conn)
    ts0 = 1_700_000_000
    _seed_candles(conn, ts0=ts0, candles=[
        (100.0, 101.0, 99.5, 100.5),
        (100.5, 104.2, 100.0, 103.8),
        (103.8, 105.0, 103.0, 104.5),
    ])
    diagnostics: dict[str, object] = {}
    result = fn(
        conn,
        "linear",
        "BTCUSDT",
        100.0,
        104.5,
        ts0,
        ts0 + 180,
        "long",
        _trend_outcome_params(),
        diagnostics=diagnostics,
    )
    assert result is not None
    success, ret = result
    assert success == 1
    assert ret == pytest.approx(0.039, abs=1e-9)
    assert diagnostics["exit_reason"] == "take_profit"
    assert diagnostics["exit_price"] == pytest.approx(104.0)
    conn.close()


def test_directional_trend_short_outcome_uses_downside_tp() -> None:
    # Direct helper symmetry: SHORT must profit when price falls and must use the
    # persisted lower TP, not LONG arithmetic with a sign flipped afterwards.
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    ts0 = 1_700_025_000
    _seed_candles(conn, ts0=ts0, candles=[
        (100.0, 100.5, 99.0, 99.4),
        (99.4, 99.7, 95.8, 96.2),
    ])
    params = _trend_outcome_params()
    params["trade_plan"]["levels"] = {
        "take_profit": {"price": 96.0},
        "stop_loss": {"price": 102.0},
    }
    diagnostics: dict[str, object] = {}
    result = outcomes._directional_trend_outcome(
        conn,
        "linear",
        "BTCUSDT",
        100.0,
        96.2,
        ts0,
        ts0 + 120,
        "short",
        params,
        diagnostics=diagnostics,
    )
    assert result == pytest.approx((1, 0.039), abs=1e-9)
    assert diagnostics["exit_reason"] == "take_profit"
    assert diagnostics["exit_price"] == pytest.approx(96.0)
    conn.close()


def test_directional_trend_outcome_does_not_wait_for_funding_after_early_exit(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "trend_early_exit_funding.db"))
    db.init_db(conn)
    ts0 = 1_700_050_000
    params = _trend_outcome_params()
    params["cost_model"].update({
        "next_funding_ts": ts0 + 3600,
        "funding_interval_min": 480,
        "expected_funding_events": 1,
    })
    _seed_candles(conn, ts0=ts0, candles=[
        (100.0, 104.2, 99.5, 103.8),
        (103.8, 104.5, 103.0, 104.0),
    ])
    diagnostics: dict[str, object] = {}
    result = outcomes._directional_trend_outcome(
        conn,
        "linear",
        "BTCUSDT",
        100.0,
        104.0,
        ts0,
        ts0 + 7200,
        "long",
        params,
        diagnostics=diagnostics,
    )
    assert result == pytest.approx((1, 0.039), abs=1e-9)
    assert diagnostics["exit_reason"] == "take_profit"
    conn.close()


def test_directional_trend_outcome_censors_gap_before_exit(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "trend_gap.db"))
    db.init_db(conn)
    ts0 = 1_700_075_000
    _seed_candles(conn, ts0=ts0, candles=[
        (100.0, 101.0, 99.5, 100.5),
    ])
    # Deliberately skip ts0+60 and place a TP candle at ts0+120.
    db.upsert_ohlcv(conn, [{
        "venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": ts0 + 120,
        "open": 100.5, "high": 104.2, "low": 100.0, "close": 103.8, "volume": 1000.0,
    }])
    diagnostics: dict[str, object] = {}
    result = outcomes._directional_trend_outcome(
        conn, "linear", "BTCUSDT", 100.0, 103.8, ts0, ts0 + 180, "long",
        _trend_outcome_params(), diagnostics=diagnostics,
    )
    assert result is None
    assert diagnostics["reason"] == "missing_1m_candle_before_directional_exit"
    assert diagnostics["expected_ts"] == ts0 + 60
    conn.close()


def test_directional_trend_outcome_censors_same_candle_tp_sl_ambiguity(tmp_path: Path) -> None:
    fn = getattr(outcomes, "_directional_trend_outcome", None)
    assert callable(fn)
    conn = db.connect(str(tmp_path / "trend_ambiguous.db"))
    db.init_db(conn)
    ts0 = 1_700_100_000
    _seed_candles(conn, ts0=ts0, candles=[
        (100.0, 104.5, 97.5, 100.5),
        (100.5, 101.0, 99.0, 100.0),
    ])
    diagnostics: dict[str, object] = {}
    result = fn(
        conn,
        "linear",
        "BTCUSDT",
        100.0,
        100.0,
        ts0,
        ts0 + 120,
        "long",
        _trend_outcome_params(),
        diagnostics=diagnostics,
    )
    assert result is None
    assert diagnostics["reason"] == "directional_tp_sl_intrabar_order_unobservable"
    conn.close()


def test_calibration_lineage_accepts_trend_evidence_without_mean_reversion() -> None:
    row = {
        "rec_id": "R-trend",
        "ts": 1_700_000_000,
        "bot_type": "directional_trend",
        "model_version": RECOMMENDER_MODEL_VERSION,
        "success": 1,
        "reasons": {
            "feature_snapshot": {
                "strategy_family": "directional_trend",
                "trend_evidence_valid": True,
                "trend_strength": 0.82,
                "coherence": 0.86,
                "regime": "trend",
                "strategy_contract_version": "directional_trend_shadow_v1",
                "outcome_label_version": "directional_trend_label_v1",
                "mean_reversion_evidence_valid": False,
                "mean_reversion_score": 0.0,
            },
            "outcome_policy": {
                "policy_evaluation_eligible": True,
                "strategy_family": "directional_trend",
                "bot_outcome_label_version": "directional_trend_label_v1",
                "strategy_contract_version": "directional_trend_shadow_v1",
            },
        },
    }
    diag = calibration_lineage_diagnostics([row], policy_fingerprint=None)
    assert diag["feature_eligible_total"] == 1
    assert diag["policy_eligible_total"] == 1


def _settings() -> Settings:
    return Settings(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=["linear"],
        symbols_linear=["BTCUSDT"],
        risk_limits={
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
            "min_leverage": 3,
            "max_leverage": 5,
        },
        min_score_to_recommend=0.08,
        min_conf_to_recommend=0.52,
        taker_fee_bps_linear=6.0,
        master_key=None,
        admin_api_key=None,
        sentiment_interval_sec=60,
        futures_collect_interval_sec=900,
        telegram_token=None,
        telegram_chat_id=None,
        require_conf_gate=False,
    )


def _seed_trending_history(conn, *, now: int) -> None:
    for tf_sec, count in ((60, 220), (900, 120), (1800, 120), (3600, 120), (14_400, 100), (86_400, 100)):
        rows = []
        for idx in range(count):
            ts = now - (count - idx) * tf_sec
            price = 100.0 * math.exp(idx * 0.0015)
            rows.append({
                "venue": "linear",
                "symbol": "BTCUSDT",
                "tf_sec": tf_sec,
                "ts": ts,
                "open": price * 0.999,
                "high": price * 1.003,
                "low": price * 0.998,
                "close": price,
                "volume": 10_000.0 + idx,
            })
        db.upsert_ohlcv(conn, rows)


def test_recommender_publishes_trend_candidate_only_as_outcome_eligible_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "trend_shadow.db"))
    db.init_db(conn)
    now = 1_800_000_000
    monkeypatch.setattr(db, "now_ts", lambda: now)
    gate_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        recommender_module,
        "gate_candidate",
        lambda _conn, venue, symbol, _limits, cached_status=None: gate_calls.append((venue, symbol)) or [],
    )
    _seed_trending_history(conn, now=now)
    latest = conn.execute(
        "SELECT close FROM ohlcv WHERE venue='linear' AND symbol='BTCUSDT' AND tf_sec=60 ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    price = float(latest["close"])
    db.insert_tickers(conn, [{
        "venue": "linear",
        "symbol": "BTCUSDT",
        "ts": now - 10,
        "last": price,
        "bid": price * 0.9999,
        "ask": price * 1.0001,
        "vol24h": 100_000.0,
        "turnover24h": 100_000_000.0,
    }])
    db.upsert_funding_rate(conn, [{
        "symbol": "BTCUSDT",
        "ts": now - 10,
        "funding_rate": 0.0,
        "next_funding_ts": now + 4 * 3600,
        "funding_interval_min": 480,
    }])

    run_recommender_once(conn, _settings())
    row = conn.execute(
        "SELECT status, direction, params_json, reasons_json, outcome_eligible, outcome_sample_role "
        "FROM recommendations WHERE bot_type='directional_trend' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["status"] == "no_trade"
    assert row["direction"] == "long"
    assert int(row["outcome_eligible"]) == 1
    assert row["outcome_sample_role"] == "shadow_no_trade"
    params = json.loads(row["params_json"])
    reasons = json.loads(row["reasons_json"])
    assert params["entry_model"] == "single_position_no_pyramiding"
    model_row = conn.execute(
        "SELECT model_version FROM recommendations WHERE bot_type='directional_trend' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert str(model_row["model_version"]).startswith(TREND_RECOMMENDER_MODEL_VERSION)
    codes = {str(item.get("code")) for item in reasons["decision_layers"]["no_trade_reasons"]}
    assert "DIRECTIONAL_TREND_SHADOW_ONLY" not in codes
    assert "PROXY_MONETARY_EXPECTANCY_UNPROVEN" in codes
    # One symbol produces one execution-capacity check for futures_grid only;
    # directional_trend remains observable without consuming a portfolio slot.
    assert gate_calls == [("linear", "BTCUSDT"), ("linear", "BTCUSDT")]
    conn.close()


def test_directional_trend_execution_validation_requires_complete_single_position_contract() -> None:
    rec = {
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "directional_trend",
        "direction": "long",
        "params": {
            "trade_plan": {
                "reference_price": 100.0,
                "entry_model": "single_position_no_pyramiding",
                "levels": {
                    "take_profit": {"price": 104.0},
                    "stop_loss": {"price": 98.0},
                },
            }
        },
    }
    result = main_module._validate_trade_plan_against_bybit_meta(
        rec,
        {},
        require_meta=False,
        require_execution_plan=True,
    )
    codes = {str(item.get("code")) for item in result["errors"]}
    assert "DIRECTIONAL_TREND_SHADOW_ONLY" not in codes
    assert "EXTERNAL_EXECUTION_PACKAGE_MISSING" in codes
    assert "BOT_TYPE_UNSUPPORTED" not in codes


def test_frontend_marks_directional_trend_as_single_position_external_execution() -> None:
    app_js = (Path(__file__).parents[1] / "app/ui/static/app.js").read_text(encoding="utf-8")
    assert 'DIRECTIONAL_TREND_BOT_TYPE = "directional_trend"' in app_js
    assert 'Направленный тренд · одна позиция' in app_js
    assert 'Параметры single-position trend-плана' in app_js
    assert 'audit-instance' in app_js
    assert 'isLaunchableRecommendation(it)' in app_js


def test_calibration_lineage_rejects_wrong_trend_contract_version() -> None:
    row = {
        "rec_id": "R-trend-wrong-contract",
        "ts": 1_700_000_000,
        "bot_type": "directional_trend",
        "model_version": RECOMMENDER_MODEL_VERSION,
        "success": 1,
        "reasons": {
            "feature_snapshot": {
                "trend_evidence_valid": True,
                "trend_strength": 0.82,
                "coherence": 0.86,
                "regime": "trend",
                "strategy_contract_version": "directional_trend_shadow_v0",
                "outcome_label_version": "directional_trend_label_v1",
            },
            "outcome_policy": {
                "policy_evaluation_eligible": True,
                "strategy_family": "directional_trend",
                "bot_outcome_label_version": "directional_trend_label_v1",
                "strategy_contract_version": "directional_trend_shadow_v0",
            },
        },
    }
    diag = calibration_lineage_diagnostics([row], policy_fingerprint=None)
    assert diag["feature_eligible_total"] == 0
    assert diag["policy_eligible_total"] == 0
    assert diag["dropped_invalid_feature_evidence"] == 1


def test_outcome_cycle_labels_directional_trend_shadow_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = db.connect(str(tmp_path / "trend_cycle.db"))
    db.init_db(conn)
    rec_ts = 1_710_000_000
    features_ref_ts = rec_ts - 60
    entry_ts = rec_ts + 60
    horizon_sec = 12 * 3600
    params = _trend_outcome_params()
    reasons = {
        "risk_checks": {"passed": True, "blocks": []},
        "feature_snapshot": {
            "strategy_family": "directional_trend",
            "trend_evidence_valid": True,
            "trend_strength": 0.82,
            "coherence": 0.86,
            "regime": "trend",
            "strategy_contract_version": "directional_trend_shadow_v1",
            "outcome_label_version": "directional_trend_label_v1",
        },
        "outcome_policy": {
            "eligible": True,
            "policy_evaluation_eligible": True,
            "sample_role": "shadow_no_trade",
            "strategy_family": "directional_trend",
            "bot_outcome_label_version": "directional_trend_label_v1",
            "strategy_contract_version": "directional_trend_shadow_v1",
        },
    }
    db.insert_recommendations(conn, [{
        "rec_id": "R-trend-cycle",
        "ts": rec_ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "directional_trend",
        "direction": "long",
        "account_mode": "unified",
        "margin_mode": "shadow",
        "score": 0.8,
        "confidence": 0.7,
        "expected_rr": 1.5,
        "risk_score": 0.2,
        "params": params,
        "reasons": reasons,
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": RECOMMENDER_MODEL_VERSION,
        "features_ref_ts": features_ref_ts,
        "publication_root_rec_id": "R-trend-cycle",
        "outcome_root_rec_id": "R-trend-cycle",
        "is_outcome_label_root": True,
    }])
    rows = []
    for idx in range(horizon_sec // 60 + 1):
        ts = entry_ts + idx * 60
        if idx == 0:
            o, h, lo, c = 100.0, 101.0, 99.5, 100.5
        elif idx == 1:
            o, h, lo, c = 100.5, 104.2, 100.0, 103.8
        else:
            o, h, lo, c = 103.8, 104.0, 103.5, 103.8
        rows.append({
            "venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": ts,
            "open": o, "high": h, "low": lo, "close": c, "volume": 1000.0,
        })
    db.upsert_ohlcv(conn, rows)
    monkeypatch.setattr(db, "now_ts", lambda: entry_ts + horizon_sec + 61)
    stats = outcomes.compute_outcomes_cycle(conn, max_to_process=10)
    assert stats["rows_labeled"] == 1
    row = conn.execute(
        "SELECT success, ret, direction, label_available_ts FROM reco_outcomes WHERE rec_id=?",
        ("R-trend-cycle",),
    ).fetchone()
    assert row is not None
    assert int(row["success"]) == 1
    assert float(row["ret"]) == pytest.approx(0.039, abs=1e-9)
    assert row["direction"] == "long"
    assert int(row["label_available_ts"]) == entry_ts + horizon_sec + 60
    conn.close()
