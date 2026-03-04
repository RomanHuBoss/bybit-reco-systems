"""Calibration module — two-stage confidence estimation.

Stage 1: LogisticRegression on raw features extracted from reasons_json.
         Learns weights from actual outcomes — replaces hand-tuned score formula.
         Falls back to score-only when insufficient per-bot data.

Stage 2: Platt scaling on top of LogReg probability output.
         Corrects any remaining systematic bias / overconfidence.

Architecture:
  P(success) = Platt( LogReg([range_score, trend, atr_pct, sent,
                               dir_conf, coherence, spread_bps, score]) )

Fallback chain (when not enough data to fit a layer):
  LogReg + Platt  →  Platt(score)  →  sigmoid(score × 2.5)
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any


# ── Platt scaler ──────────────────────────────────────────────────────────────

@dataclass
class PlattScaler:
    a: float = 1.0
    b: float = 0.0
    fitted: bool = False
    saved_ts: int = 0

    def predict(self, x: float) -> float:
        z = max(-500.0, min(500.0, self.a * x + self.b))
        return 1.0 / (1.0 + math.exp(-z))


def fit_platt(
    xs: list[float],
    ys: list[int],
    iters: int = 300,
    lr: float = 0.06,
    min_samples: int = 80,
) -> PlattScaler:
    if len(xs) < min_samples:
        return PlattScaler(fitted=False)
    a, b = 1.0, 0.0
    n = len(xs)
    for _ in range(iters):
        da = db_ = 0.0
        for x, y in zip(xs, ys):
            z = max(-500.0, min(500.0, a * x + b))
            p = 1.0 / (1.0 + math.exp(-z))
            err = p - y
            da  += err * x
            db_ += err
        a -= lr * (da / n)
        b -= lr * (db_ / n)
    return PlattScaler(a=a, b=b, fitted=True, saved_ts=int(time.time()))


# ── Feature extraction ────────────────────────────────────────────────────────

# Canonical feature order — must never change once models are saved to DB.
# New features can be appended at the end (old models will use 0.0 for them).
FEATURE_NAMES = [
    "range_score",        # 1 − trend_strength (multi-TF)
    "trend_strength",     # |all-TF trend strength|
    "atr_pct_norm",       # atr_pct / 0.02  (normalised, clipped 0..2)
    "effective_sentiment",# blended global+symbol sentiment [-1, 1]
    "dir_conf",           # direction_confidence_calibrated (or raw) [0, 1]
    "coherence",          # direction coherence [0, 1]
    "spread_bps_norm",    # spread_bps / 10  (normalised)
    "score",              # legacy score — still informative as a feature
]
N_FEATURES = len(FEATURE_NAMES)


def extract_features(row: dict[str, Any]) -> list[float] | None:
    """Extract a fixed-length feature vector from an outcome+rec joined row.

    Returns None if critical fields are missing.
    row must contain: score, reasons (parsed dict from reasons_json).
    """
    score   = row.get("score")
    reasons = row.get("reasons")
    if score is None or not isinstance(reasons, dict):
        return None

    # Parse reasons_json if it arrived as a string (raw DB row)
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except Exception:
            return None

    # direction_agg block
    dir_agg = reasons.get("direction_agg") or {}
    # Support both calibrated and raw direction confidence.
    # Use explicit None checks — `or` would incorrectly skip 0.0 (a valid probability).
    _dc = dir_agg.get("direction_confidence_calibrated")
    if _dc is None:
        _dc = dir_agg.get("direction_confidence")
    dir_conf = float(_dc) if _dc is not None else 0.5
    coherence = float(dir_agg.get("coherence") or 0.5)

    strengths = dir_agg.get("strength") or {}
    if isinstance(strengths, dict):
        trend_strength = abs(float(strengths.get("all") or 0.0))
    else:
        trend_strength = abs(float(strengths or 0.0))
    range_score = max(0.0, 1.0 - trend_strength)

    # cost_model block
    cost = reasons.get("cost_model") or {}
    spread_bps = float(cost.get("spread_bps") or cost.get("total_cost_bps") or 8.0)

    # effective_sentiment — stored directly in reasons by _score()
    sent = float(reasons.get("effective_sentiment") or 0.0)

    # atr_pct — stored in top_positive/negative factors feature values,
    # or reconstruct from cost model context. Best source: top factors list.
    atr_pct = _extract_factor_value(reasons, "atr_pct") or 0.0

    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    return [
        _clamp(range_score,           0.0, 1.0),
        _clamp(trend_strength,        0.0, 1.0),
        _clamp(atr_pct / 0.02,        0.0, 2.0),   # normalised
        _clamp(sent,                  -1.0, 1.0),
        _clamp(dir_conf,              0.0, 1.0),
        _clamp(coherence,             0.0, 1.0),
        _clamp(spread_bps / 10.0,     0.0, 5.0),   # normalised
        _clamp(float(score),          -1.0, 1.0),
    ]


def _extract_factor_value(reasons: dict, feature: str) -> float | None:
    """Pull a feature value from top_positive_factors or top_negative_factors."""
    for flist in (
        reasons.get("top_positive_factors") or [],
        reasons.get("top_negative_factors") or [],
    ):
        for f in flist:
            if isinstance(f, dict) and f.get("feature") == feature:
                v = f.get("value")
                if v is not None:
                    return float(v)
    return None


# ── LogisticRegression wrapper ────────────────────────────────────────────────

@dataclass
class LogRegScaler:
    """Thin wrapper around sklearn LogisticRegression with Platt on top."""
    coef: list[float] = field(default_factory=list)      # len == N_FEATURES
    intercept: float = 0.0
    platt: PlattScaler = field(default_factory=PlattScaler)
    fitted: bool = False
    saved_ts: int = 0
    n_samples: int = 0   # training set size — shown in UI status

    def predict(self, features: list[float]) -> float:
        """Return calibrated P(success) given a feature vector."""
        if not self.fitted:
            return 0.5
        if len(self.coef) == 0 or len(features) != len(self.coef):
            # Platt-only mode (insufficient data for LogReg) or feature length mismatch:
            # fall through to Platt on score if caller passes score as features[7]
            return 0.5  # caller should use predict_score_only() in this case
        # Linear combination
        z = self.intercept + sum(c * f for c, f in zip(self.coef, features))
        # Sigmoid → raw logistic probability
        p_raw = 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, z))))
        # Platt calibration on top
        if self.platt.fitted:
            return self.platt.predict(p_raw)
        return p_raw

    def predict_score_only(self, score: float) -> float:
        """Fallback: Platt calibration on the legacy scalar score."""
        if self.platt.fitted:
            return self.platt.predict(score)
        return 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, score * 2.5))))


def fit_logreg(
    rows: list[dict[str, Any]],
    min_samples: int = 80,
    logreg_min_samples: int = 300,
) -> LogRegScaler:
    """Fit LogReg + Platt from outcome rows (each must have score, reasons, success).

    - If n >= logreg_min_samples: fit full LogReg + Platt on LogReg probabilities.
    - If logreg_min_samples > n >= min_samples: fit Platt on score only (legacy mode).
    - If n < min_samples: return unfitted.
    """
    xs_score = [float(r["score"]) for r in rows]
    ys       = [int(r["success"]) for r in rows]
    n        = len(rows)

    if n < min_samples:
        return LogRegScaler(fitted=False)

    # Always fit a Platt on score as fallback/baseline
    platt = fit_platt(xs_score, ys, min_samples=min_samples)

    if n < logreg_min_samples:
        # Not enough data for reliable LogReg — return Platt-only wrapper
        return LogRegScaler(
            coef=[],
            intercept=0.0,
            platt=platt,
            fitted=True,
            saved_ts=int(time.time()),
            n_samples=n,
        )

    # Build feature matrix — skip rows with missing features
    X, y_used = [], []
    for r in rows:
        fv = extract_features(r)
        if fv is not None:
            X.append(fv)
            y_used.append(int(r["success"]))

    if len(X) < logreg_min_samples:
        return LogRegScaler(
            coef=[], intercept=0.0, platt=platt,
            fitted=True, saved_ts=int(time.time()), n_samples=n,
        )

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler as SkScaler
        import numpy as np

        Xnp = np.array(X, dtype=float)
        ynp = np.array(y_used, dtype=int)

        # Standardise features for numerical stability
        scaler = SkScaler()
        Xs = scaler.fit_transform(Xnp)

        clf = LogisticRegression(
            C=1.0,
            max_iter=500,
            solver="lbfgs",
            class_weight="balanced",  # handles win-rate imbalance gracefully
        )
        clf.fit(Xs, ynp)

        # Store un-standardised equivalent coefficients:
        # w_raw[i] = w_scaled[i] / std[i],  intercept_raw = b - sum(w_raw * mean)
        std  = scaler.scale_
        mean = scaler.mean_
        coef_raw = (clf.coef_[0] / std).tolist()
        intercept_raw = float(clf.intercept_[0] - (clf.coef_[0] / std).dot(mean))

        # Get LogReg predicted probabilities, convert to log-odds (logits) for Platt fitting.
        # Fitting Platt on probabilities ∈ [0,1] is incorrect — the logistic link expects
        # an unbounded input. Fitting on logit(p) gives proper temperature scaling:
        #   P_calibrated = sigmoid(a * logit(p_logreg) + b)
        # where a≈1 means well-calibrated, a<1 means overconfident, b shifts the threshold.
        import math as _math
        def _logit(p: float) -> float:
            p = max(1e-7, min(1.0 - 1e-7, p))
            return _math.log(p / (1.0 - p))
        p_logreg_raw = clf.predict_proba(Xs)[:, 1].tolist()
        logits_logreg = [_logit(p) for p in p_logreg_raw]

        # Fit Platt on log-odds of LogReg outputs
        platt_top = fit_platt(logits_logreg, y_used, min_samples=min_samples)

        return LogRegScaler(
            coef=coef_raw,
            intercept=intercept_raw,
            platt=platt_top,
            fitted=True,
            saved_ts=int(time.time()),
            n_samples=len(X),
        )

    except Exception:
        # sklearn not available or fitting failed — degrade to Platt-only
        return LogRegScaler(
            coef=[], intercept=0.0, platt=platt,
            fitted=True, saved_ts=int(time.time()), n_samples=n,
        )


# ── Persistence ───────────────────────────────────────────────────────────────

def save_logreg_to_db(conn, key: str, model: LogRegScaler) -> None:
    import json as _json
    from . import db as _db

    payload = {
        "type":       "logreg_platt_v1",
        "coef":       model.coef,
        "intercept":  model.intercept,
        "platt_a":    model.platt.a,
        "platt_b":    model.platt.b,
        "platt_fitted": model.platt.fitted,
        "fitted":     model.fitted,
        "ts":         model.saved_ts or int(time.time()),
        "n_samples":  model.n_samples,
    }
    conn.execute(
        """INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)""",
        (key, _json.dumps(payload), _db.now_ts()),
    )
    conn.commit()


def load_logreg_from_db(conn, key: str) -> LogRegScaler | None:
    cur = conn.execute("""SELECT value_json FROM app_config WHERE key=?""", (key,))
    r = cur.fetchone()
    if not r:
        return None
    try:
        obj = json.loads(r["value_json"])
        platt = PlattScaler(
            a=float(obj.get("platt_a", 1.0)),
            b=float(obj.get("platt_b", 0.0)),
            fitted=bool(obj.get("platt_fitted", False)),
            saved_ts=int(obj.get("ts", 0)),
        )
        return LogRegScaler(
            coef=list(obj.get("coef") or []),
            intercept=float(obj.get("intercept", 0.0)),
            platt=platt,
            fitted=bool(obj.get("fitted", False)),
            saved_ts=int(obj.get("ts", 0)),
            n_samples=int(obj.get("n_samples", 0)),
        )
    except Exception:
        return None


# ── Legacy Platt API (kept for backward compat & direction calibrator) ────────

def save_platt_to_db(conn, key: str, scaler: PlattScaler) -> None:
    import json as _json
    from . import db as _db

    payload = {
        "a": scaler.a, "b": scaler.b,
        "fitted": scaler.fitted,
        "ts": scaler.saved_ts or int(time.time()),
    }
    conn.execute(
        """INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)""",
        (key, _json.dumps(payload), _db.now_ts()),
    )
    conn.commit()


def load_platt_from_db(conn, key: str) -> PlattScaler | None:
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


# ── Key registry ──────────────────────────────────────────────────────────────

# LogReg+Platt keys (new)
BOT_LOGREG_KEYS = {
    "spot_grid":          "logreg_spot_grid_v1",
    "futures_grid":       "logreg_futures_grid_v1",
    "dca_bot":            "logreg_dca_v1",
    "futures_martingale": "logreg_martingale_v1",
    "futures_combo":      "logreg_combo_v1",
}
GLOBAL_LOGREG_KEY    = "logreg_global_v1"
DIRECTION_LOGREG_KEY = "logreg_direction_v1"

# Legacy Platt keys (kept for direction calibrator + backward compat)
BOT_CALIB_KEYS = {
    "spot_grid":          "platt_spot_grid_v1",
    "futures_grid":       "platt_futures_grid_v1",
    "dca_bot":            "platt_dca_v1",
    "futures_martingale": "platt_martingale_v1",
    "futures_combo":      "platt_combo_v1",
}
