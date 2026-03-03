from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass
class PlattScaler:
    a: float = 1.0
    b: float = 0.0
    fitted: bool = False
    saved_ts: int = 0  # unix timestamp of last fit/save — used for periodic re-calibration

    def predict(self, x: float) -> float:
        # Clamp to prevent math.exp overflow (|z| > ~710 overflows float64)
        z = max(-500.0, min(500.0, self.a * x + self.b))
        return 1.0 / (1.0 + math.exp(-z))


def fit_platt(
    xs: list[float],
    ys: list[int],
    iters: int = 300,
    lr: float = 0.06,
    min_samples: int = 80,
) -> PlattScaler:
    """Fit a Platt scaler via gradient descent on logistic loss.

    Improvements vs original:
    - More iterations (300 vs 250) and slightly lower LR for stabler convergence.
    - Overflow protection inside the gradient loop.
    - Returns saved_ts = now so callers can track freshness.
    """
    if len(xs) < min_samples:
        return PlattScaler(fitted=False)
    a, b = 1.0, 0.0
    n = len(xs)
    for _ in range(iters):
        da = 0.0
        db = 0.0
        for x, y in zip(xs, ys):
            z = max(-500.0, min(500.0, a * x + b))
            p = 1.0 / (1.0 + math.exp(-z))
            err = p - y
            da += err * x
            db += err
        a -= lr * (da / n)
        b -= lr * (db / n)
    return PlattScaler(a=a, b=b, fitted=True, saved_ts=int(time.time()))


def save_platt_to_db(conn, key: str, scaler: PlattScaler) -> None:
    import json
    from . import db as _db

    payload = {
        "a": scaler.a,
        "b": scaler.b,
        "fitted": scaler.fitted,
        "ts": scaler.saved_ts or int(time.time()),
    }
    conn.execute(
        """INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)""",
        (key, json.dumps(payload), _db.now_ts()),
    )
    conn.commit()


def load_platt_from_db(conn, key: str) -> PlattScaler | None:
    import json

    cur = conn.execute("""SELECT value_json FROM app_config WHERE key=?""", (key,))
    r = cur.fetchone()
    if not r:
        return None
    try:
        obj = json.loads(r["value_json"])
        return PlattScaler(
            a=float(obj.get("a", 1.0)),
            b=float(obj.get("b", 0.0)),
            fitted=bool(obj.get("fitted", False)),
            saved_ts=int(obj.get("ts", 0)),
        )
    except Exception:
        return None


# Keys used in app_config for per-bot calibrators
BOT_CALIB_KEYS = {
    "spot_grid":          "platt_spot_grid_v1",
    "futures_grid":       "platt_futures_grid_v1",
    "dca_bot":            "platt_dca_v1",
    "futures_martingale": "platt_martingale_v1",
    "futures_combo":      "platt_combo_v1",
}
