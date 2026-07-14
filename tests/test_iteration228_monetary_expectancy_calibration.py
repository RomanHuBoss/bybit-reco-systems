from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app import calibration, db, recommender


POLICY_FINGERPRINT = "a" * 64
POLICY_SETTINGS = SimpleNamespace(
    llm_reviewer_enabled=False,
    mean_reversion_min_score=0.25,
)


def _policy_key(base_key: str) -> str:
    return recommender.policy_calibration_storage_key(base_key, POLICY_FINGERPRINT)


def _rows(*, wins: int, losses: int, win_return: float, loss_return: float) -> list[dict]:
    now = int(time.time())
    total = wins + losses
    rows: list[dict] = []
    for index in range(total):
        success = 1 if index < wins else 0
        ret = win_return if success else loss_return
        ts = now - 1_000_000 + index * 1_000
        rows.append(
            {
                "score": 0.35 + (index % 10) * 0.01,
                "success": success,
                "ret": ret,
                "ts": ts,
                "label_available_ts": ts + 600,
                "reasons": {},
            }
        )
    return rows


def test_binary_hit_rate_cannot_fit_when_monetary_expectancy_is_negative() -> None:
    rows = _rows(wins=160, losses=40, win_return=0.001, loss_return=-0.05)
    assert sum(row["success"] for row in rows) / len(rows) == pytest.approx(0.80)
    assert sum(row["ret"] for row in rows) / len(rows) == pytest.approx(-0.0092)

    model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)

    # 80% tiny wins must not produce an actionable probability model when the
    # same matured cohort loses money in aggregate.
    assert model.fitted is False
    assert model.expectancy_status == "negative"
    assert model.return_samples == 200
    assert model.weighted_mean_return is not None
    assert model.weighted_mean_return < 0.0
    assert model.weighted_expected_shortfall is not None
    assert model.weighted_expected_shortfall < model.weighted_mean_return


def test_positive_monetary_expectancy_alone_does_not_activate_probability_model() -> None:
    rows = _rows(wins=120, losses=80, win_return=0.02, loss_return=-0.01)
    model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)

    assert model.expectancy_status == "positive"
    assert model.weighted_mean_return is not None
    assert model.weighted_mean_return > 0.0
    assert model.fitted is False
    assert model.oof_status == "insufficient"


def test_negative_proxy_expectancy_is_an_explicit_no_trade_policy() -> None:
    policy = getattr(recommender, "_calibration_expectancy_no_trade_reason", None)
    assert callable(policy)

    negative = calibration.LogRegScaler(
        fitted=False,
        n_samples=200,
        return_samples=200,
        expectancy_status="negative",
        weighted_mean_return=-0.0092,
        weighted_expected_shortfall=-0.05,
        policy_fingerprint=POLICY_FINGERPRINT,
    )
    reason = policy(negative)
    assert reason is not None
    assert reason["code"] == "PROXY_MONETARY_EXPECTANCY_NON_POSITIVE"
    assert "-0.9200%" in reason["msg"]

    positive = calibration.LogRegScaler(
        fitted=True,
        n_samples=200,
        return_samples=200,
        expectancy_status="positive",
        weighted_mean_return=0.002,
        weighted_expected_shortfall=-0.01,
    )
    assert policy(positive) is None



def test_rare_large_losses_cannot_hide_behind_insufficient_class_balance() -> None:
    rows = _rows(wins=95, losses=5, win_return=0.001, loss_return=-0.05)
    assert sum(row["success"] for row in rows) / len(rows) == pytest.approx(0.95)
    assert sum(row["ret"] for row in rows) / len(rows) == pytest.approx(-0.00155)

    model = calibration.fit_logreg(rows, min_samples=80, logreg_min_samples=300)

    assert model.fitted is False
    assert model.expectancy_status == "negative"
    assert model.return_samples == 100
    assert model.weighted_mean_return is not None and model.weighted_mean_return < 0.0

def test_monetary_expectancy_state_round_trips_through_persistence(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "iteration228.sqlite"))
    db.init_db(conn)
    model = calibration.LogRegScaler(
        fitted=False,
        saved_ts=1_700_000_000,
        n_samples=200,
        return_samples=200,
        expectancy_status="negative",
        weighted_mean_return=-0.0092,
        weighted_expected_shortfall=-0.05,
    )
    calibration.save_logreg_to_db(conn, "iteration228-negative", model)
    loaded = calibration.load_logreg_from_db(conn, "iteration228-negative")

    assert loaded is not None
    assert loaded.fitted is False
    assert loaded.expectancy_status == "negative"
    assert loaded.return_samples == 200
    assert loaded.weighted_mean_return == pytest.approx(-0.0092)
    assert loaded.weighted_expected_shortfall == pytest.approx(-0.05)
    conn.close()


def test_boolean_or_missing_return_cannot_enter_monetary_calibration() -> None:
    rows = _rows(wins=60, losses=40, win_return=0.02, loss_return=-0.01)
    for index, row in enumerate(rows):
        row["ret"] = True if index % 2 else None

    model = calibration.fit_logreg(rows, min_samples=20, logreg_min_samples=300)

    assert model.fitted is False
    assert model.n_samples == 0
    assert model.return_samples == 0
    assert model.expectancy_status == "insufficient"


def test_fresh_negative_expectancy_cache_is_loaded_despite_unfitted_model(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "iteration228-cache.sqlite"))
    db.init_db(conn)
    model = calibration.LogRegScaler(
        fitted=False,
        saved_ts=int(time.time()),
        n_samples=200,
        return_samples=200,
        expectancy_status="negative",
        weighted_mean_return=-0.0092,
        weighted_expected_shortfall=-0.05,
        policy_fingerprint=POLICY_FINGERPRINT,
    )
    key = _policy_key(calibration.BOT_CALIB_KEYS["futures_grid"])
    calibration.save_logreg_to_db(conn, key, model)

    def unexpected_refit(*_args, **_kwargs):
        raise AssertionError("fresh negative expectancy state must be loaded, not silently refitted away")

    monkeypatch.setattr(recommender, "_fit_bot_logregs", unexpected_refit)
    loaded = recommender._load_or_fit_bot_logregs(
        conn,
        min_samples=80,
        policy_fingerprint=POLICY_FINGERPRINT,
        settings_obj=POLICY_SETTINGS,
    )

    assert loaded["futures_grid"].fitted is False
    assert loaded["futures_grid"].expectancy_status == "negative"
    assert loaded["futures_grid"].weighted_mean_return == pytest.approx(-0.0092)
    conn.close()


def test_stale_negative_cache_can_be_replaced_by_current_positive_expectancy(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "iteration228-refresh.sqlite"))
    db.init_db(conn)
    key = _policy_key(calibration.BOT_CALIB_KEYS["futures_grid"])
    calibration.save_logreg_to_db(
        conn,
        key,
        calibration.LogRegScaler(
            fitted=False,
            saved_ts=int(time.time()) - calibration.CALIB_REFIT_INTERVAL_SEC - 10,
            n_samples=100,
            return_samples=100,
            expectancy_status="negative",
            weighted_mean_return=-0.001,
            weighted_expected_shortfall=-0.02,
            policy_fingerprint=POLICY_FINGERPRINT,
        ),
    )
    current = calibration.LogRegScaler(
        fitted=False,
        saved_ts=int(time.time()),
        n_samples=100,
        return_samples=100,
        expectancy_status="positive",
        weighted_mean_return=0.002,
        weighted_expected_shortfall=-0.01,
    )
    monkeypatch.setattr(
        recommender,
        "_fit_bot_logregs",
        lambda *_args, **_kwargs: {"futures_grid": current},
    )

    loaded = recommender._load_or_fit_bot_logregs(
        conn,
        min_samples=80,
        policy_fingerprint=POLICY_FINGERPRINT,
        settings_obj=POLICY_SETTINGS,
    )

    assert loaded["futures_grid"].expectancy_status == "positive"
    assert recommender._calibration_expectancy_no_trade_reason(loaded["futures_grid"]) is None
    conn.close()
