from __future__ import annotations

import json
import time

import pytest

from app import calibration, db
from app.calibration import LogRegScaler, fit_logreg, load_logreg_from_db, save_logreg_to_db
from app.recommender import _calibration_expectancy_no_trade_reason, run_recommender_once
from app.settings import Settings
from tests.test_logic import _seed_ohlcv_wave


def _rows(returns: list[float]) -> list[dict]:
    now = int(time.time())
    rows: list[dict] = []
    horizon = 12 * 3600
    for index, ret in enumerate(returns):
        ts = now - (len(returns) - index + 1) * horizon
        rows.append(
            {
                "score": 0.45 if ret > 0 else -0.15,
                "success": int(ret > 0),
                "ret": ret,
                "ts": ts,
                "label_available_ts": ts + horizon,
                "horizon_sec": horizon,
                "reasons": {},
            }
        )
    return rows


def test_tiny_positive_mean_is_not_positive_expectancy_without_positive_lower_bound() -> None:
    # 50/50 labels, positive observed mean, but the edge is tiny relative to dispersion.
    returns = [0.0101] * 40 + [-0.0100] * 40

    model = fit_logreg(_rows(returns), min_samples=80, half_life_days=100_000)

    assert model.weighted_mean_return is not None and model.weighted_mean_return > 0.0
    assert model.weighted_mean_return_lower_bound is not None
    assert model.weighted_mean_return_lower_bound <= 0.0
    assert model.expectancy_status == "uncertain"
    assert model.fitted is False


def test_material_positive_expectancy_requires_positive_lower_bound() -> None:
    returns = [0.0200] * 40 + [-0.0100] * 40

    model = fit_logreg(_rows(returns), min_samples=80, half_life_days=100_000)

    assert model.weighted_mean_return_lower_bound is not None
    assert model.weighted_mean_return_lower_bound > 0.0
    assert model.weighted_effective_return_samples >= 79.9
    assert model.expectancy_status == "positive"
    assert model.fitted is True


@pytest.mark.parametrize("status", ["unknown", "insufficient", "uncertain"])
def test_unproven_monetary_expectancy_is_explicit_no_trade(status: str) -> None:
    model = LogRegScaler(
        fitted=False,
        expectancy_status=status,
        return_samples=0 if status != "uncertain" else 80,
        weighted_mean_return=0.0001 if status == "uncertain" else None,
    )

    reason = _calibration_expectancy_no_trade_reason(model)

    assert reason is not None
    assert reason["code"] == "PROXY_MONETARY_EXPECTANCY_UNPROVEN"


def test_positive_expectancy_state_does_not_create_no_trade_reason() -> None:
    model = LogRegScaler(
        fitted=False,
        expectancy_status="positive",
        return_samples=80,
        weighted_mean_return=0.005,
        weighted_mean_return_lower_bound=0.002,
        weighted_effective_return_samples=80.0,
    )

    assert _calibration_expectancy_no_trade_reason(model) is None


def test_uncertainty_diagnostics_survive_persistence() -> None:
    conn = db.connect(":memory:")
    db.init_db(conn)
    model = LogRegScaler(
        fitted=False,
        saved_ts=int(time.time()),
        n_samples=80,
        return_samples=80,
        expectancy_status="uncertain",
        weighted_mean_return=0.0001,
        weighted_expected_shortfall=-0.02,
        weighted_return_std=0.01,
        weighted_effective_return_samples=76.5,
        weighted_mean_return_lower_bound=-0.0018,
        expectancy_confidence_level=0.95,
    )

    save_logreg_to_db(conn, "iteration231_uncertain", model)
    loaded = load_logreg_from_db(conn, "iteration231_uncertain")

    assert loaded is not None
    assert loaded.expectancy_status == "uncertain"
    assert loaded.weighted_return_std == pytest.approx(0.01)
    assert loaded.weighted_effective_return_samples == pytest.approx(76.5)
    assert loaded.weighted_mean_return_lower_bound == pytest.approx(-0.0018)
    assert loaded.expectancy_confidence_level == pytest.approx(0.95)


def test_calibrator_identity_changes_for_new_expectancy_contract() -> None:
    assert calibration.GLOBAL_LOGREG_KEY.endswith("_v18")
    assert calibration.BOT_CALIB_KEYS["futures_grid"].endswith("_v18")


def test_recommender_keeps_raw_high_confidence_shadow_only_without_positive_expectancy(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "unproven_expectancy.db"))
    db.init_db(conn)
    now = int(time.time())
    monkeypatch.setattr(db, "now_ts", lambda: now)
    symbol = "BTCUSDT"
    base_price = 50_000.0

    for tf_sec, n in ((60, 260), (900, 160), (1800, 140), (3600, 140), (14_400, 110), (86_400, 100)):
        _seed_ohlcv_wave(
            conn, venue="linear", symbol=symbol, now_ts=now, tf_sec=tf_sec, n=n, base_price=base_price
        )
    db.insert_tickers(conn, [{
        "venue": "linear",
        "symbol": symbol,
        "ts": now,
        "last": base_price,
        "bid": base_price - 2.0,
        "ask": base_price + 2.0,
        "vol24h": 25_000.0,
        "turnover24h": 25_000_000.0,
    }])
    db.upsert_funding_rate(conn, [{
        "symbol": symbol,
        "ts": now,
        "funding_rate": 0.00001,
        "next_funding_ts": now + 4 * 3600,
        "funding_interval_min": 480,
    }])
    settings = Settings(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=["linear"],
        symbols_linear=[symbol],
        risk_limits={
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
            "max_leverage": 3,
            "max_position_notional_usdt": 5000.0,
            "max_margin_per_bot_usdt": 1000.0,
        },
        min_score_to_recommend=-1.0,
        min_conf_to_recommend=0.0,
        taker_fee_bps_linear=6.0,
        master_key=None,
        admin_api_key=None,
        sentiment_interval_sec=60,
        futures_collect_interval_sec=900,
        telegram_token=None,
        telegram_chat_id=None,
        require_conf_gate=True,
    )

    result = run_recommender_once(conn, settings)

    assert result["count"] >= 1
    row = conn.execute(
        "SELECT status, confidence, reasons_json FROM recommendations ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    reasons = json.loads(row["reasons_json"] or "{}")
    decision_layers = reasons.get("decision_layers") if isinstance(reasons.get("decision_layers"), dict) else {}
    no_trade_codes = {
        str(item.get("code") or "")
        for item in decision_layers.get("no_trade_reasons", [])
        if isinstance(item, dict)
    }
    assert float(row["confidence"]) > 0.5
    assert row["status"] == "no_trade"
    assert "PROXY_MONETARY_EXPECTANCY_UNPROVEN" in no_trade_codes
    conn.close()
