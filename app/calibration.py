"""Calibration module — two-stage confidence estimation.

Stage 1: LogisticRegression on raw features extracted from reasons_json.
         Learns weights from actual outcomes — replaces hand-tuned score formula.
         Falls back to score-only when insufficient per-bot data.

Stage 2: Platt scaling on top of LogReg probability output.
         Corrects any remaining systematic bias / overconfidence.

Architecture:
  P(success) = Platt( LogReg([range_score, trend, atr_pct, sent,
                               dir_conf, coherence, spread_bps, score,
                               oi_4h, funding, liq_tier, btc_corr, regime_conf]) )

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


def _recency_weights(tss: list[int], half_life_days: float = 21.0) -> list[float]:
    """Exponential decay weights by recency. Newest observation = weight 1.0.

    half_life_days=21: observation 21 days old has weight 0.5, 42 days old = 0.25.
    This makes the calibrator track recent market regimes rather than stale history.
    """
    if not tss:
        return []
    now = int(time.time())
    hl_sec = half_life_days * 86400.0
    ws = [math.exp(-math.log(2.0) * max(0, now - t) / hl_sec) for t in tss]
    # Normalise so sum = len(ws) — preserves gradient scale relative to n
    total = sum(ws)
    n = len(ws)
    return [w * n / total for w in ws] if total > 0 else [1.0] * n


def fit_platt(
    xs: list[float],
    ys: list[int],
    iters: int = 300,
    lr: float = 0.06,
    min_samples: int = 80,
    ws: list[float] | None = None,   # per-sample recency weights (same length as xs)
) -> PlattScaler:
    if len(xs) < min_samples:
        return PlattScaler(fitted=False)

    # Guard: near-homogeneous labels make Platt scaling numerically valid but
    # practically meaningless. The optimizer drives the intercept to extremes,
    # collapsing calibrated probabilities toward 0 or 1 regardless of x.
    # We use a stricter band than 5/95 because crypto recommendation labels can
    # look deceptively "accurate" on small, regime-specific samples.
    win_rate = sum(int(y) for y in ys) / len(ys)
    if win_rate < 0.15 or win_rate > 0.85:
        return PlattScaler(fitted=False)

    weights = ws if (ws and len(ws) == len(xs)) else [1.0] * len(xs)
    a, b = 1.0, 0.0
    n = len(xs)
    w_sum = sum(weights)
    for _ in range(iters):
        da = db_ = 0.0
        for x, y, w in zip(xs, ys, weights):
            z = max(-500.0, min(500.0, a * x + b))
            p = 1.0 / (1.0 + math.exp(-z))
            err = p - y
            da  += w * err * x
            db_ += w * err
        a -= lr * (da / w_sum)
        b -= lr * (db_ / w_sum)
    return PlattScaler(a=a, b=b, fitted=True, saved_ts=int(time.time()))


# ── Feature extraction ────────────────────────────────────────────────────────

# Canonical feature order — must never change once models are saved to DB.
# New features can be appended at the end (old models will use 0.0 for them).
FEATURE_NAMES = [
    "range_score",        # 1 − trend_strength (multi-TF)
    "trend_strength",     # |all-TF trend strength|
    "atr_pct_norm",       # atr_pct / 0.10  (normalised, clipped 0..2; 1.0 ≈ 10% 1h ATR)
    "effective_sentiment",# blended global+symbol sentiment [-1, 1]
    "dir_conf",           # direction_confidence_calibrated (or raw) [0, 1]
    "coherence",          # direction coherence [0, 1]
    "spread_bps_norm",    # spread_bps / 10  (normalised)
    "score",              # legacy score — still informative as a feature
    # v3 additions — appended to avoid breaking existing saved models
    "oi_4h_norm",         # oi_4h_chg_pct / 10  (OI momentum, signed, clipped ±3)
    "funding_norm",       # carry_cost_bps_8h / 20  (funding pressure, clipped 0..2)
    "liq_tier_num",       # liquidity tier: micro=0, low=0.33, medium=0.67, high=1.0
    "btc_corr",           # btc_beta.correlation [−1, 1], 0 if unavailable
    "regime_conf",        # regime_confidence [0, 1] — how decisive the current regime is
]
N_FEATURES = len(FEATURE_NAMES)

_LIQ_TIER_MAP = {"micro": 0.0, "low": 0.33, "medium": 0.67, "high": 1.0}


def extract_features(row: dict[str, Any]) -> list[float] | None:
    """Extract a fixed-length feature vector from an outcome+rec joined row.

    Prefers the explicit feature_snapshot stored at recommendation time. This avoids
    train/inference skew when later code or defaults reconstruct features slightly
    differently from the original inference path.
    """
    score = row.get("score")
    reasons = row.get("reasons")
    if score is None or reasons is None:
        return None

    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except Exception:
            return None
    if not isinstance(reasons, dict):
        return None

    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    snap = reasons.get("feature_snapshot") or {}
    if isinstance(snap, dict) and snap:
        return [
            _clamp(float(snap.get("range_score", 0.5)), 0.0, 1.0),
            _clamp(float(snap.get("trend_strength", 0.0)), 0.0, 1.0),
            _clamp(float(snap.get("atr_pct_norm", 0.0)), 0.0, 2.0),
            _clamp(float(snap.get("effective_sentiment", 0.0)), -1.0, 1.0),
            _clamp(float(snap.get("dir_conf", 0.5)), 0.0, 1.0),
            _clamp(float(snap.get("coherence", 0.5)), 0.0, 1.0),
            _clamp(float(snap.get("spread_bps_norm", 0.8)), 0.0, 5.0),
            _clamp(float(snap.get("score", score)), -1.0, 1.0),
            _clamp(float(snap.get("oi_4h_norm", 0.0)), -3.0, 3.0),
            _clamp(float(snap.get("funding_norm", 0.0)), -2.0, 2.0),
            _clamp(float(snap.get("liq_tier_num", 0.67)), 0.0, 1.0),
            _clamp(float(snap.get("btc_corr", 0.0)), -1.0, 1.0),
            _clamp(float(snap.get("regime_conf", 0.5)), 0.0, 1.0),
        ]

    dir_agg = reasons.get("direction_agg") or {}
    _dc = dir_agg.get("direction_confidence_calibrated")
    if _dc is None:
        _dc = dir_agg.get("direction_confidence")
    dir_conf = float(_dc) if _dc is not None else 0.5
    coherence_raw = dir_agg.get("coherence")
    coherence = float(coherence_raw) if coherence_raw is not None else 0.5

    strengths = dir_agg.get("strength") or {}
    if isinstance(strengths, dict):
        trend_strength = abs(float(strengths.get("all") or 0.0))
    else:
        trend_strength = abs(float(strengths or 0.0))
    range_score = max(0.0, 1.0 - trend_strength)

    cost = reasons.get("cost_model") or {}
    spread_raw = cost.get("spread_bps")
    if spread_raw is None:
        spread_raw = cost.get("total_cost_bps")
    spread_bps = float(spread_raw) if spread_raw is not None else 8.0
    sent_raw = reasons.get("effective_sentiment")
    sent = float(sent_raw) if sent_raw is not None else 0.0
    atr_pct = _extract_factor_value(reasons, "atr_pct") or 0.0

    oi_block = reasons.get("open_interest") or {}
    oi_4h_raw = oi_block.get("oi_4h_chg_pct")
    oi_4h_norm = _clamp(float(oi_4h_raw) / 10.0, -3.0, 3.0) if oi_4h_raw is not None else 0.0

    fund_block = reasons.get("funding") or {}
    fund_raw = fund_block.get("directional_funding_bps_8h")
    if fund_raw is None:
        fund_raw = fund_block.get("carry_cost_bps_8h")
    funding_norm = _clamp(float(fund_raw) / 20.0, -2.0, 2.0) if fund_raw is not None else 0.0

    liq_block = reasons.get("liquidity") or {}
    liq_tier_str = str(liq_block.get("tier") or "medium").lower()
    liq_tier_num = _LIQ_TIER_MAP.get(liq_tier_str, 0.67)

    btc_block = reasons.get("btc_beta") or {}
    btc_corr_raw = btc_block.get("correlation")
    btc_corr = _clamp(float(btc_corr_raw), -1.0, 1.0) if btc_corr_raw is not None else 0.0

    regime_conf_raw = dir_agg.get("regime_confidence")
    regime_conf = _clamp(float(regime_conf_raw), 0.0, 1.0) if regime_conf_raw is not None else 0.5

    return [
        _clamp(range_score, 0.0, 1.0),
        _clamp(trend_strength, 0.0, 1.0),
        _clamp(atr_pct / 0.10, 0.0, 2.0),
        _clamp(sent, -1.0, 1.0),
        _clamp(dir_conf, 0.0, 1.0),
        _clamp(coherence, 0.0, 1.0),
        _clamp(spread_bps / 10.0, 0.0, 5.0),
        _clamp(float(score), -1.0, 1.0),
        oi_4h_norm,
        funding_norm,
        liq_tier_num,
        btc_corr,
        regime_conf,
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
    coef: list[float] = field(default_factory=list)
    intercept: float = 0.0
    platt: PlattScaler = field(default_factory=PlattScaler)
    fitted: bool = False
    saved_ts: int = 0
    n_samples: int = 0

    def predict(self, features: list[float]) -> float:
        """Return calibrated P(success) given a feature vector."""
        if not self.fitted:
            return 0.5
        if len(self.coef) == 0 or len(features) < len(self.coef):
            return 0.5
        # Pad with zeros if incoming vector is shorter (old saved model, new features)
        fv = list(features) + [0.0] * max(0, len(self.coef) - len(features))
        z = self.intercept + sum(c * f for c, f in zip(self.coef, fv))
        p_raw = 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, z))))
        if self.platt.fitted:
            return self.platt.predict(z)
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
    half_life_days: float = 21.0,
) -> LogRegScaler:
    """Fit LogReg + Platt from outcome rows with recency weighting.

    - Recency weighting: half_life_days=21 → observation 21 days old weighs 0.5x.
      Crypto regimes shift fast; old outcomes from a different regime should matter less.
    - If n >= logreg_min_samples: full LogReg + Platt.
    - If logreg_min_samples > n >= min_samples: Platt on score only.
    - If n < min_samples or degenerate WR: unfitted.
    """
    xs_score = [float(r["score"]) for r in rows]
    ys       = [int(r["success"]) for r in rows]
    tss      = [int(r.get("ts") or 0) for r in rows]
    n        = len(rows)

    if n < min_samples:
        return LogRegScaler(fitted=False)

    # Guard: degenerate class balance. A 90%+ hit-rate on proxy labels is not a
    # trustworthy basis for probability calibration; keep confidence heuristic.
    win_rate = sum(ys) / n
    if win_rate < 0.15 or win_rate > 0.85:
        return LogRegScaler(fitted=False)

    ws = _recency_weights(tss, half_life_days=half_life_days)

    # Platt on score (fallback/baseline)
    platt = fit_platt(xs_score, ys, min_samples=min_samples, ws=ws)

    if n < logreg_min_samples:
        return LogRegScaler(
            coef=[], intercept=0.0, platt=platt,
            fitted=True, saved_ts=int(time.time()), n_samples=n,
        )

    # Build feature matrix
    X, y_used, w_used = [], [], []
    for r, w in zip(rows, ws):
        fv = extract_features(r)
        if fv is not None:
            X.append(fv)
            y_used.append(int(r["success"]))
            w_used.append(w)

    if len(X) < logreg_min_samples:
        return LogRegScaler(
            coef=[], intercept=0.0, platt=platt,
            fitted=True, saved_ts=int(time.time()), n_samples=n,
        )

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler as SkScaler
        import numpy as np

        Xnp  = np.array(X, dtype=float)
        ynp  = np.array(y_used, dtype=int)
        wnp  = np.array(w_used, dtype=float)

        scaler = SkScaler()
        Xs = scaler.fit_transform(Xnp)

        clf = LogisticRegression(
            C=1.0, max_iter=500, solver="lbfgs",
            class_weight="balanced",
        )
        clf.fit(Xs, ynp, sample_weight=wnp)

        std  = scaler.scale_
        mean = scaler.mean_
        coef_raw      = (clf.coef_[0] / std).tolist()
        intercept_raw = float(clf.intercept_[0] - (clf.coef_[0] / std).dot(mean))

        import math as _math
        def _logit(p: float) -> float:
            p = max(1e-7, min(1.0 - 1e-7, p))
            return _math.log(p / (1.0 - p))
        p_logreg_raw  = clf.predict_proba(Xs)[:, 1].tolist()
        logits_logreg = [_logit(p) for p in p_logreg_raw]

        platt_top = fit_platt(logits_logreg, y_used, min_samples=min_samples, ws=w_used)

        return LogRegScaler(
            coef=coef_raw, intercept=intercept_raw, platt=platt_top,
            fitted=True, saved_ts=int(time.time()), n_samples=len(X),
        )

    except Exception:
        return LogRegScaler(
            coef=[], intercept=0.0, platt=platt,
            fitted=True, saved_ts=int(time.time()), n_samples=n,
        )


# ── Persistence ───────────────────────────────────────────────────────────────

def save_logreg_to_db(conn, key: str, model: LogRegScaler) -> None:
    obj = {
        "type":      "logreg",
        "coef":      model.coef,
        "intercept": model.intercept,
        "fitted":    model.fitted,
        "n_samples": model.n_samples,
        "ts":        model.saved_ts or int(time.time()),
        "platt": {
            "a":      model.platt.a,
            "b":      model.platt.b,
            "fitted": model.platt.fitted,
            "ts":     model.platt.saved_ts or int(time.time()),
        },
    }
    conn.execute(
        "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES (?, ?, ?)",
        (key, json.dumps(obj), int(__import__("time").time())),
    )
    conn.commit()


def load_logreg_from_db(conn, key: str) -> LogRegScaler | None:
    cur = conn.execute("SELECT value_json FROM app_config WHERE key=?", (key,))
    r   = cur.fetchone()
    if not r:
        return None
    try:
        obj  = json.loads(r["value_json"])
        platt_obj = obj.get("platt") or {}
        platt = PlattScaler(
            a=float(platt_obj.get("a", 1.0)),
            b=float(platt_obj.get("b", 0.0)),
            fitted=bool(platt_obj.get("fitted", False)),
            saved_ts=int(platt_obj.get("ts", 0)),
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


def save_platt_to_db(conn, key: str, scaler: PlattScaler) -> None:
    obj = {
        "type":   "platt",
        "a":      scaler.a,
        "b":      scaler.b,
        "fitted": scaler.fitted,
        "ts":     scaler.saved_ts or int(time.time()),
    }
    conn.execute(
        "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES (?, ?, ?)",
        (key, json.dumps(obj), int(__import__("time").time())),
    )
    conn.commit()


def load_platt_from_db(conn, key: str) -> PlattScaler | None:
    cur = conn.execute("SELECT value_json FROM app_config WHERE key=?", (key,))
    r   = cur.fetchone()
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


# ── Key registry ─────────────────────────────────────────────────────────────
# v3: feature vector expanded (+5 features: oi_4h, funding, liq_tier, btc_corr, regime_conf)
#     + recency weighting in fit_logreg/fit_platt → forces refit of all saved models

BOT_CALIB_KEYS: dict[str, str] = {
    "spot_grid":          "logreg_spot_grid_v3",
    "futures_grid":       "logreg_futures_grid_v3",
    "dca_bot":            "logreg_dca_v3",
    "futures_martingale": "logreg_martingale_v3",
    "futures_combo":      "logreg_combo_v3",
}
GLOBAL_LOGREG_KEY = "logreg_global_v3"

# Refit interval — don't refit more than once per hour
CALIB_REFIT_INTERVAL_SEC = 3600
