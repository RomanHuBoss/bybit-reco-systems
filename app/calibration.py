from __future__ import annotations

import math
from dataclasses import dataclass

@dataclass
class PlattScaler:
    a: float = 1.0
    b: float = 0.0
    fitted: bool = False

    def predict(self, x: float) -> float:
        z = self.a * x + self.b
        return 1.0 / (1.0 + math.exp(-z))

def fit_platt(xs: list[float], ys: list[int], iters: int = 250, lr: float = 0.08, min_samples: int = 80) -> PlattScaler:
    if len(xs) < min_samples:
        return PlattScaler(fitted=False)
    a, b = 1.0, 0.0
    n = len(xs)
    for _ in range(iters):
        da = 0.0
        db = 0.0
        for x, y in zip(xs, ys):
            z = a * x + b
            p = 1.0 / (1.0 + math.exp(-z))
            da += (p - y) * x
            db += (p - y)
        a -= lr * (da / n)
        b -= lr * (db / n)
    return PlattScaler(a=a, b=b, fitted=True)

def save_platt_to_db(conn, key: str, scaler: PlattScaler) -> None:
    import json, time
    from . import db as _db
    payload = {"a": scaler.a, "b": scaler.b, "fitted": scaler.fitted, "ts": int(time.time())}
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
        return PlattScaler(a=float(obj.get("a",1.0)), b=float(obj.get("b",0.0)), fitted=bool(obj.get("fitted", False)))
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
