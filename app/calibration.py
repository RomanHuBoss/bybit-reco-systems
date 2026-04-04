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


def _strict_json_dumps(value: Any) -> str:
    """Строгая JSON-сериализация без NaN/Infinity.

    Калибраторы влияют на confidence всей системы, поэтому даже единичный
    невалидный коэффициент нельзя тихо сохранять в SQLite как legacy-JSON.
    """
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("calibration payload must contain only finite JSON numbers") from exc


def _finite_float(value: Any) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _finite_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


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


def label_balance_stats(ys: list[int]) -> dict[str, float]:
    """Shared diagnostics for calibration eligibility.

    We keep both the classic totals and the minority-based "effective" sample count
    because the UI and the fit gate should speak the same language. A dataset with
    80 rows but only 20 minority examples is not as informative as a balanced 80-row
    sample, so calibration should be gated on effective_samples rather than raw n.
    """
    total = int(len(ys))
    wins = int(sum(int(y) for y in ys))
    losses = max(0, total - wins)
    minority = min(wins, losses)
    effective = max(0, 2 * minority)
    win_rate = (wins / total) if total else None
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "minority_class_count": minority,
        "effective_samples": effective,
        "win_rate": win_rate,
    }


def fit_platt(
    xs: list[float],
    ys: list[int],
    iters: int = 300,
    lr: float = 0.06,
    min_samples: int = 80,
    ws: list[float] | None = None,   # per-sample recency weights (same length as xs)
) -> PlattScaler:
    if not ys:
        return PlattScaler(fitted=False)

    balance = label_balance_stats(ys)
    if int(balance["effective_samples"]) < int(min_samples):
        return PlattScaler(fitted=False)

    # Guard: near-homogeneous labels make Platt scaling numerically valid but
    # practically meaningless. The optimizer drives the intercept to extremes,
    # collapsing calibrated probabilities toward 0 or 1 regardless of x.
    # We use a stricter band than 5/95 because crypto recommendation labels can
    # look deceptively "accurate" on small, regime-specific samples.
    win_rate = float(balance["win_rate"] or 0.0)
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

def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        num = float(value)
        if not math.isfinite(num):
            return default
        return num
    except Exception:
        return default



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
            _clamp(float(_safe_float(snap.get("range_score"), 0.5) or 0.5), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("trend_strength"), 0.0) or 0.0), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("atr_pct_norm"), 0.0) or 0.0), 0.0, 2.0),
            _clamp(float(_safe_float(snap.get("effective_sentiment"), 0.0) or 0.0), -1.0, 1.0),
            _clamp(float(_safe_float(snap.get("dir_conf"), 0.5) or 0.5), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("coherence"), 0.5) or 0.5), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("spread_bps_norm"), 0.8) or 0.8), 0.0, 5.0),
            _clamp(float(_safe_float(snap.get("score"), _safe_float(score, 0.0)) or 0.0), -1.0, 1.0),
            _clamp(float(_safe_float(snap.get("oi_4h_norm"), 0.0) or 0.0), -3.0, 3.0),
            _clamp(float(_safe_float(snap.get("funding_norm"), 0.0) or 0.0), -2.0, 2.0),
            _clamp(float(_safe_float(snap.get("liq_tier_num"), 0.67) or 0.67), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("btc_corr"), 0.0) or 0.0), -1.0, 1.0),
            _clamp(float(_safe_float(snap.get("regime_conf"), 0.5) or 0.5), 0.0, 1.0),
        ]

    dir_agg = reasons.get("direction_agg") or {}
    _dc = dir_agg.get("direction_confidence_calibrated")
    if _dc is None:
        _dc = dir_agg.get("direction_confidence")
    dir_conf = float(_safe_float(_dc, 0.5) or 0.5)
    coherence_raw = dir_agg.get("coherence")
    coherence = float(_safe_float(coherence_raw, 0.5) or 0.5)

    strengths = dir_agg.get("strength") or {}
    if isinstance(strengths, dict):
        trend_strength = abs(float(_safe_float(strengths.get("all"), 0.0) or 0.0))
    else:
        trend_strength = abs(float(_safe_float(strengths, 0.0) or 0.0))
    range_score = max(0.0, 1.0 - trend_strength)

    cost = reasons.get("cost_model") or {}
    spread_raw = cost.get("spread_bps")
    if spread_raw is None:
        spread_raw = cost.get("total_cost_bps")
    spread_bps = float(_safe_float(spread_raw, 8.0) or 8.0)
    sent_raw = reasons.get("effective_sentiment")
    sent = float(_safe_float(sent_raw, 0.0) or 0.0)
    atr_pct = float(_safe_float(_extract_factor_value(reasons, "atr_pct"), 0.0) or 0.0)

    oi_block = reasons.get("open_interest") or {}
    oi_4h_raw = oi_block.get("oi_4h_chg_pct")
    oi_4h_norm = _clamp(float(_safe_float(oi_4h_raw, 0.0) or 0.0) / 10.0, -3.0, 3.0) if oi_4h_raw is not None else 0.0

    fund_block = reasons.get("funding") or {}
    fund_raw = fund_block.get("expected_funding_bps")
    if fund_raw is None:
        fund_raw = fund_block.get("directional_funding_bps_8h")
    if fund_raw is None:
        fund_raw = fund_block.get("carry_cost_bps_8h")
    funding_norm = _clamp(float(_safe_float(fund_raw, 0.0) or 0.0) / 20.0, -2.0, 2.0) if fund_raw is not None else 0.0

    liq_block = reasons.get("liquidity") or {}
    liq_tier_str = str(liq_block.get("tier") or "medium").lower()
    liq_tier_num = _LIQ_TIER_MAP.get(liq_tier_str, 0.67)

    btc_block = reasons.get("btc_beta") or {}
    btc_corr_raw = btc_block.get("correlation")
    btc_corr = _clamp(float(_safe_float(btc_corr_raw, 0.0) or 0.0), -1.0, 1.0) if btc_corr_raw is not None else 0.0

    regime_conf_raw = dir_agg.get("regime_confidence")
    regime_conf = _clamp(float(_safe_float(regime_conf_raw, 0.5) or 0.5), 0.0, 1.0) if regime_conf_raw is not None else 0.5

    return [
        _clamp(range_score, 0.0, 1.0),
        _clamp(trend_strength, 0.0, 1.0),
        _clamp(atr_pct / 0.10, 0.0, 2.0),
        _clamp(sent, -1.0, 1.0),
        _clamp(dir_conf, 0.0, 1.0),
        _clamp(coherence, 0.0, 1.0),
        _clamp(spread_bps / 10.0, 0.0, 5.0),
        _clamp(float(_safe_float(score, 0.0) or 0.0), -1.0, 1.0),
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
                    return float(_safe_float(v, 0.0) or 0.0)
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
        if len(self.coef) == 0:
            return 0.5
        # Pad with zeros if incoming vector is shorter (schema drift / older snapshots).
        # The previous guard returned 0.5 here, which silently collapsed confidence instead
        # of preserving the existing coefficients on the shared prefix.
        fv = list(features) + [0.0] * max(0, len(self.coef) - len(features))
        z = self.intercept + sum(c * f for c, f in zip(self.coef, fv))
        if self.platt.fitted:
            return self.platt.predict(z)
        z = max(-500.0, min(500.0, z))
        return 1.0 / (1.0 + math.exp(-z))

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

    The historical joins used for calibration should be robust to dirty rows in SQLite.
    A single malformed `score`, `success` or timestamp must not crash the whole fit and
    silently disable recalibration for every symbol/bot in the current cycle.
    """
    sanitized_rows: list[dict[str, Any]] = []
    xs_score: list[float] = []
    ys: list[int] = []
    tss: list[int] = []
    for row in rows:
        try:
            score = _safe_float(row.get("score"), None)
            success_raw = row.get("success")
            ts = int(row.get("ts") or 0)
        except Exception:
            continue
        if score is None:
            continue
        try:
            success = int(success_raw)
        except Exception:
            continue
        if success not in (0, 1):
            continue
        sanitized_rows.append(row)
        xs_score.append(float(score))
        ys.append(success)
        tss.append(ts)

    n = len(sanitized_rows)
    balance = label_balance_stats(ys)

    if int(balance["effective_samples"]) < int(min_samples):
        return LogRegScaler(fitted=False)

    # Guard: degenerate class balance. A 90%+ hit-rate on proxy labels is not a
    # trustworthy basis for probability calibration; keep confidence heuristic.
    win_rate = float(balance["win_rate"] or 0.0)
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
    X, y_used, w_used, ts_used = [], [], [], []
    for r, w in zip(sanitized_rows, ws):
        fv = extract_features(r)
        if fv is not None:
            X.append(fv)
            y_used.append(int(r["success"]))
            w_used.append(w)
            ts_used.append(int(r.get("ts") or 0))

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

        order = np.argsort(np.array(ts_used, dtype=int)) if len(ts_used) == len(X) else np.arange(len(X))
        if len(X) == len(order):
            Xnp = Xnp[order]
            ynp = ynp[order]
            wnp = wnp[order]

        oof_logits: list[float] = []
        oof_y: list[int] = []
        oof_w: list[float] = []

        from sklearn.model_selection import TimeSeriesSplit

        n_splits = min(5, max(2, len(X) // max(1, min_samples)))
        if len(X) >= (n_splits + 1) * 2:
            splitter = TimeSeriesSplit(n_splits=n_splits)
            for train_idx, val_idx in splitter.split(Xnp):
                if len(train_idx) < min_samples or len(val_idx) == 0:
                    continue
                y_train = ynp[train_idx]
                if len(set(int(v) for v in y_train.tolist())) < 2:
                    continue
                scaler_fold = SkScaler()
                X_train = scaler_fold.fit_transform(Xnp[train_idx])
                X_val = scaler_fold.transform(Xnp[val_idx])
                clf_fold = LogisticRegression(
                    C=1.0, max_iter=500, solver="lbfgs",
                    class_weight="balanced",
                )
                clf_fold.fit(X_train, y_train, sample_weight=wnp[train_idx])
                logits_fold = clf_fold.decision_function(X_val)
                oof_logits.extend(float(x) for x in logits_fold.tolist())
                oof_y.extend(int(x) for x in ynp[val_idx].tolist())
                oof_w.extend(float(x) for x in wnp[val_idx].tolist())

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

        platt_top = fit_platt(oof_logits, oof_y, min_samples=min_samples, ws=oof_w) if len(oof_logits) >= min_samples else PlattScaler(fitted=False)

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
        (key, _strict_json_dumps(obj), int(__import__("time").time())),
    )
    conn.commit()


def load_logreg_from_db(conn, key: str) -> LogRegScaler | None:
    cur = conn.execute("SELECT value_json FROM app_config WHERE key=?", (key,))
    r   = cur.fetchone()
    if not r:
        return None
    try:
        obj = json.loads(r["value_json"])
        if not isinstance(obj, dict):
            return None
        raw_coef = obj.get("coef") or []
        if not isinstance(raw_coef, list):
            return None
        coef: list[float] = []
        for item in raw_coef:
            num = _finite_float(item)
            if num is None:
                return None
            coef.append(num)

        intercept = _finite_float(obj.get("intercept", 0.0))
        if intercept is None:
            return None

        platt_obj = obj.get("platt") or {}
        if not isinstance(platt_obj, dict):
            return None
        platt_a = _finite_float(platt_obj.get("a", 1.0))
        platt_b = _finite_float(platt_obj.get("b", 0.0))
        if platt_a is None or platt_b is None:
            return None
        platt = PlattScaler(
            a=platt_a,
            b=platt_b,
            fitted=bool(platt_obj.get("fitted", False)),
            saved_ts=_finite_int(platt_obj.get("ts", 0), 0),
        )
        return LogRegScaler(
            coef=coef,
            intercept=intercept,
            platt=platt,
            fitted=bool(obj.get("fitted", False)),
            saved_ts=_finite_int(obj.get("ts", 0), 0),
            n_samples=max(0, _finite_int(obj.get("n_samples", 0), 0)),
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
        (key, _strict_json_dumps(obj), int(__import__("time").time())),
    )
    conn.commit()


def load_platt_from_db(conn, key: str) -> PlattScaler | None:
    cur = conn.execute("SELECT value_json FROM app_config WHERE key=?", (key,))
    r   = cur.fetchone()
    if not r:
        return None
    try:
        obj = json.loads(r["value_json"])
        if not isinstance(obj, dict):
            return None
        a = _finite_float(obj.get("a", 1.0))
        b = _finite_float(obj.get("b", 0.0))
        if a is None or b is None:
            return None
        return PlattScaler(
            a=a,
            b=b,
            fitted=bool(obj.get("fitted", False)),
            saved_ts=_finite_int(obj.get("ts", 0), 0),
        )
    except Exception:
        return None


# ── Key registry ─────────────────────────────────────────────────────────────
# v3: feature vector expanded (+5 features: oi_4h, funding, liq_tier, btc_corr, regime_conf)
#     + recency weighting in fit_logreg/fit_platt → forces refit of all saved models

BOT_CALIB_KEYS: dict[str, str] = {
    "spot_grid":    "logreg_spot_grid_v3",
    "futures_grid": "logreg_futures_grid_v3",
}
GLOBAL_LOGREG_KEY = "logreg_global_v3"

# Refit interval — don't refit more than once per hour
CALIB_REFIT_INTERVAL_SEC = 3600
