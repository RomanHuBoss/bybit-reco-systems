from __future__ import annotations

import time
from types import SimpleNamespace

from app import calibration, db, recommender


POLICY_FINGERPRINT = "b" * 64
POLICY_SETTINGS = SimpleNamespace(
    llm_reviewer_enabled=False,
    mean_reversion_min_score=0.25,
)


def _policy_key(base_key: str) -> str:
    return recommender.policy_calibration_storage_key(base_key, POLICY_FINGERPRINT)


def _stale_ts() -> int:
    return int(time.time()) - calibration.CALIB_REFIT_INTERVAL_SEC - 10


def _positive_logreg() -> calibration.LogRegScaler:
    return calibration.LogRegScaler(
        coef=[0.25] * calibration.N_FEATURES,
        intercept=0.1,
        platt=calibration.PlattScaler(a=1.0, b=0.0, fitted=True, saved_ts=_stale_ts()),
        fitted=True,
        saved_ts=_stale_ts(),
        n_samples=320,
        return_samples=320,
        expectancy_status="positive",
        weighted_mean_return=0.003,
        weighted_expected_shortfall=-0.01,
        policy_fingerprint=POLICY_FINGERPRINT,
    )


def _insufficient_logreg() -> calibration.LogRegScaler:
    return calibration.LogRegScaler(
        fitted=False,
        saved_ts=int(time.time()),
        n_samples=12,
        return_samples=12,
        expectancy_status="insufficient",
    )


def test_stale_positive_bot_calibrator_is_not_kept_when_current_evidence_is_insufficient(
    tmp_path, monkeypatch
) -> None:
    conn = db.connect(str(tmp_path / "iteration230-bot.sqlite"))
    db.init_db(conn)
    key = _policy_key(calibration.BOT_CALIB_KEYS["futures_grid"])
    calibration.save_logreg_to_db(conn, key, _positive_logreg())
    monkeypatch.setattr(
        recommender,
        "_fit_bot_logregs",
        lambda *_args, **_kwargs: {"futures_grid": _insufficient_logreg()},
    )

    loaded = recommender._load_or_fit_bot_logregs(
        conn,
        min_samples=80,
        policy_fingerprint=POLICY_FINGERPRINT,
        settings_obj=POLICY_SETTINGS,
    )["futures_grid"]

    assert loaded.fitted is False
    assert loaded.expectancy_status == "insufficient"
    assert loaded.n_samples == 12
    persisted = calibration.load_logreg_from_db(
        conn, key
    )
    assert persisted is not None
    assert persisted.fitted is False
    assert persisted.expectancy_status == "insufficient"
    conn.close()


def test_stale_positive_global_calibrator_is_not_kept_when_current_evidence_is_insufficient(
    tmp_path, monkeypatch
) -> None:
    conn = db.connect(str(tmp_path / "iteration230-global.sqlite"))
    db.init_db(conn)
    key = _policy_key(calibration.GLOBAL_LOGREG_KEY)
    calibration.save_logreg_to_db(conn, key, _positive_logreg())
    monkeypatch.setattr(recommender, "_fit_global_logreg", lambda *_args, **_kwargs: _insufficient_logreg())

    loaded = recommender._load_or_fit_global_logreg(
        conn,
        min_samples=80,
        policy_fingerprint=POLICY_FINGERPRINT,
        settings_obj=POLICY_SETTINGS,
    )

    assert loaded.fitted is False
    assert loaded.expectancy_status == "insufficient"
    assert loaded.n_samples == 12
    persisted = calibration.load_logreg_from_db(conn, key)
    assert persisted is not None
    assert persisted.fitted is False
    assert persisted.expectancy_status == "insufficient"
    conn.close()


def test_stale_direction_calibrator_is_not_kept_when_current_evidence_is_insufficient(
    tmp_path, monkeypatch
) -> None:
    conn = db.connect(str(tmp_path / "iteration230-direction.sqlite"))
    db.init_db(conn)
    key = _policy_key(recommender.DIRECTION_CALIBRATION_KEY)
    calibration.save_platt_to_db(
        conn,
        key,
        calibration.PlattScaler(a=3.0, b=-1.0, fitted=True, saved_ts=_stale_ts()),
    )
    monkeypatch.setattr(
        recommender,
        "_fit_direction_calibrator",
        lambda *_args, **_kwargs: calibration.PlattScaler(fitted=False, saved_ts=int(time.time())),
    )

    loaded = recommender._load_or_fit_direction_calibrator(
        conn,
        min_samples=80,
        policy_fingerprint=POLICY_FINGERPRINT,
        settings_obj=POLICY_SETTINGS,
    )

    assert loaded.fitted is False
    persisted = calibration.load_platt_from_db(conn, key)
    assert persisted is not None
    assert persisted.fitted is False
    conn.close()
