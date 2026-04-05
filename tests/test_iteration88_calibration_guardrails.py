from __future__ import annotations


from app import db
from app.calibration import (
    LogRegScaler,
    PlattScaler,
    load_logreg_from_db,
    load_platt_from_db,
    save_logreg_to_db,
)
from app.recommender import _clamp


def test_clamp_treats_nan_as_safe_neutral_value() -> None:
    assert _clamp(float("nan"), 0.0, 1.0) == 0.0
    assert _clamp(float("nan"), -1.0, 1.0) == 0.0
    assert _clamp(float("inf"), 0.0, 1.0) == 1.0
    assert _clamp(float("-inf"), 0.0, 1.0) == 0.0


def test_save_logreg_to_db_rejects_non_finite_payload(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "calib_strict.db"))
    db.init_db(conn)
    try:
        model = LogRegScaler(
            coef=[0.2, float("nan")],
            intercept=0.1,
            platt=PlattScaler(a=1.0, b=0.0, fitted=True, saved_ts=123),
            fitted=True,
            saved_ts=456,
            n_samples=10,
        )

        try:
            save_logreg_to_db(conn, "bad_logreg", model)
            assert False, "save_logreg_to_db must reject non-finite calibration payloads"
        except ValueError as exc:
            assert "finite JSON numbers" in str(exc)

        row = conn.execute("SELECT value_json FROM app_config WHERE key='bad_logreg'").fetchone()
        assert row is None
    finally:
        conn.close()


def test_load_calibrators_skip_legacy_non_finite_rows(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "calib_legacy_nan.db"))
    db.init_db(conn)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES (?, ?, ?)",
            (
                "legacy_logreg_nan",
                '{"type":"logreg","coef":[0.4,NaN],"intercept":0.2,"fitted":true,"n_samples":12,"ts":111,"platt":{"a":1.0,"b":0.0,"fitted":true,"ts":222}}',
                1,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES (?, ?, ?)",
            (
                "legacy_platt_nan",
                '{"type":"platt","a":NaN,"b":0.0,"fitted":true,"ts":333}',
                1,
            ),
        )
        conn.commit()

        assert load_logreg_from_db(conn, "legacy_logreg_nan") is None
        assert load_platt_from_db(conn, "legacy_platt_nan") is None
    finally:
        conn.close()
