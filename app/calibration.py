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
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _finite_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        return int(value)
    except Exception:
        return int(default)


def _strict_bool(value: Any) -> bool | None:
    """Accept only a real JSON boolean at persistence trust boundaries."""
    if isinstance(value, bool):
        return value
    return None


# ── Platt scaler ──────────────────────────────────────────────────────────────

@dataclass
class PlattScaler:
    a: float = 1.0
    b: float = 0.0
    fitted: bool = False
    saved_ts: int = 0

    def predict(self, x: float) -> float:
        x_num = _finite_float(x)
        a_num = _finite_float(self.a)
        b_num = _finite_float(self.b)
        if x_num is None or a_num is None or b_num is None:
            return 0.5
        z = a_num * x_num + b_num
        if not math.isfinite(z):
            return 0.5
        z = max(-500.0, min(500.0, z))
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
    if isinstance(value, bool):
        return default
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
            _clamp(float(_safe_float(snap.get("range_score"), 0.5)), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("trend_strength"), 0.0)), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("atr_pct_norm"), 0.0)), 0.0, 2.0),
            _clamp(float(_safe_float(snap.get("effective_sentiment"), 0.0)), -1.0, 1.0),
            _clamp(float(_safe_float(snap.get("dir_conf"), 0.5)), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("coherence"), 0.5)), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("spread_bps_norm"), 0.8)), 0.0, 5.0),
            _clamp(float(_safe_float(snap.get("score"), _safe_float(score, 0.0))), -1.0, 1.0),
            _clamp(float(_safe_float(snap.get("oi_4h_norm"), 0.0)), -3.0, 3.0),
            _clamp(float(_safe_float(snap.get("funding_norm"), 0.0)), -2.0, 2.0),
            _clamp(float(_safe_float(snap.get("liq_tier_num"), 0.67)), 0.0, 1.0),
            _clamp(float(_safe_float(snap.get("btc_corr"), 0.0)), -1.0, 1.0),
            _clamp(float(_safe_float(snap.get("regime_conf"), 0.5)), 0.0, 1.0),
        ]

    dir_agg = reasons.get("direction_agg") or {}
    _dc = dir_agg.get("direction_confidence_calibrated")
    if _dc is None:
        _dc = dir_agg.get("direction_confidence")
    dir_conf = float(_safe_float(_dc, 0.5))
    coherence_raw = dir_agg.get("coherence")
    coherence = float(_safe_float(coherence_raw, 0.5))

    strengths = dir_agg.get("strength") or {}
    if isinstance(strengths, dict):
        trend_strength = abs(float(_safe_float(strengths.get("all"), 0.0)))
    else:
        trend_strength = abs(float(_safe_float(strengths, 0.0)))
    range_score = max(0.0, 1.0 - trend_strength)

    cost = reasons.get("cost_model") or {}
    spread_raw = cost.get("spread_bps")
    if spread_raw is None:
        spread_raw = cost.get("total_cost_bps")
    spread_bps = float(_safe_float(spread_raw, 8.0))
    sent_raw = reasons.get("effective_sentiment")
    sent = float(_safe_float(sent_raw, 0.0))
    atr_pct = float(_safe_float(_extract_factor_value(reasons, "atr_pct"), 0.0))

    oi_block = reasons.get("open_interest") or {}
    oi_4h_raw = oi_block.get("oi_4h_chg_pct")
    oi_4h_norm = _clamp(float(_safe_float(oi_4h_raw, 0.0)) / 10.0, -3.0, 3.0) if oi_4h_raw is not None else 0.0

    fund_block = reasons.get("funding") or {}
    fund_raw = fund_block.get("expected_funding_bps")
    if fund_raw is None:
        fund_raw = fund_block.get("directional_funding_bps_per_event")
    if fund_raw is None:
        fund_raw = fund_block.get("directional_funding_bps_interval")
    if fund_raw is None:
        fund_raw = fund_block.get("directional_funding_bps_8h")
    if fund_raw is None:
        fund_raw = fund_block.get("carry_cost_bps_8h")
    funding_norm = _clamp(float(_safe_float(fund_raw, 0.0)) / 20.0, -2.0, 2.0) if fund_raw is not None else 0.0

    liq_block = reasons.get("liquidity") or {}
    liq_tier_str = str(liq_block.get("tier") or "medium").lower()
    liq_tier_num = _LIQ_TIER_MAP.get(liq_tier_str, 0.67)

    btc_block = reasons.get("btc_beta") or {}
    btc_corr_raw = btc_block.get("correlation")
    btc_corr = _clamp(float(_safe_float(btc_corr_raw, 0.0)), -1.0, 1.0) if btc_corr_raw is not None else 0.0

    regime_conf_raw = dir_agg.get("regime_confidence")
    regime_conf = _clamp(float(_safe_float(regime_conf_raw, 0.5)), 0.0, 1.0) if regime_conf_raw is not None else 0.5

    return [
        _clamp(range_score, 0.0, 1.0),
        _clamp(trend_strength, 0.0, 1.0),
        _clamp(atr_pct / 0.10, 0.0, 2.0),
        _clamp(sent, -1.0, 1.0),
        _clamp(dir_conf, 0.0, 1.0),
        _clamp(coherence, 0.0, 1.0),
        _clamp(spread_bps / 10.0, 0.0, 5.0),
        _clamp(float(_safe_float(score, 0.0)), -1.0, 1.0),
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
                    return float(_safe_float(v, 0.0))
    return None


# ── Logistic regression wrapper ────────────────────────────────────────────────

@dataclass
class LogRegScaler:
    """Deterministic in-repo logistic regression wrapper with optional Platt on top."""
    coef: list[float] = field(default_factory=list)
    intercept: float = 0.0
    platt: PlattScaler = field(default_factory=PlattScaler)
    fitted: bool = False
    saved_ts: int = 0
    n_samples: int = 0

    def predict(self, features: list[float]) -> float:
        """Return calibrated P(success) given a feature vector."""
        if not self.fitted or len(self.coef) == 0:
            return 0.5
        intercept = _finite_float(self.intercept)
        coef = [_finite_float(value) for value in self.coef]
        if intercept is None or any(value is None for value in coef):
            return 0.5
        try:
            raw_features = list(features)
        except Exception:
            return 0.5
        # Pad with zeros if incoming vector is shorter (schema drift / older snapshots).
        # The shared coefficient prefix remains usable, but every consumed value must be finite.
        raw_features += [0.0] * max(0, len(coef) - len(raw_features))
        fv = [_finite_float(value) for value in raw_features[:len(coef)]]
        if any(value is None for value in fv):
            return 0.5
        z = float(intercept) + sum(float(c) * float(f) for c, f in zip(coef, fv))
        if not math.isfinite(z):
            return 0.5
        if self.platt.fitted:
            return self.platt.predict(z)
        z = max(-500.0, min(500.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def predict_score_only(self, score: float) -> float:
        """Fallback: Platt calibration on the legacy scalar score."""
        score_num = _finite_float(score)
        if score_num is None:
            return 0.5
        if self.platt.fitted:
            return self.platt.predict(score_num)
        z = score_num * 2.5
        if not math.isfinite(z):
            return 0.5
        return 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, z))))


def _sigmoid(z: float) -> float:
    z = max(-60.0, min(60.0, z))
    if z >= 0.0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def _weighted_mean_std(X: list[list[float]], ws: list[float]) -> tuple[list[float], list[float]]:
    if not X:
        return [], []
    m = len(X[0])
    w_sum = max(1e-12, sum(max(0.0, float(w)) for w in ws))
    means = [0.0] * m
    for row, w in zip(X, ws):
        weight = max(0.0, float(w))
        for j in range(m):
            means[j] += weight * float(row[j])
    means = [v / w_sum for v in means]

    vars_ = [0.0] * m
    for row, w in zip(X, ws):
        weight = max(0.0, float(w))
        for j in range(m):
            d = float(row[j]) - means[j]
            vars_[j] += weight * d * d
    stds = [max(1e-6, math.sqrt(v / w_sum)) for v in vars_]
    return means, stds


def _fit_weighted_logreg_raw(
    X: list[list[float]],
    ys: list[int],
    ws: list[float],
    *,
    iters: int = 450,
    lr: float = 0.12,
    l2: float = 0.08,
) -> tuple[list[float], float]:
    """Fit a compact weighted logistic regression without optional ML runtimes.

    Calibration runs during scheduler/API maintenance paths, so it must be
    deterministic and should not depend on importing large native libraries. The
    optimizer works on standardized features, applies recency weights plus
    balanced class weights, then converts coefficients back to raw feature units.
    """
    if not X or not ys or len(X) != len(ys):
        return [], 0.0
    m = len(X[0])
    if m == 0:
        return [], 0.0

    cleaned: list[tuple[list[float], int, float]] = []
    for row, y, w in zip(X, ys, ws):
        if len(row) != m:
            continue
        vals = [float(v) for v in row]
        if not all(math.isfinite(v) for v in vals):
            continue
        if int(y) not in (0, 1):
            continue
        weight = float(w) if math.isfinite(float(w)) and float(w) > 0.0 else 1.0
        cleaned.append((vals, int(y), weight))

    if not cleaned:
        return [0.0] * m, 0.0

    Xc = [row for row, _y, _w in cleaned]
    yc = [int(y) for _row, y, _w in cleaned]
    wc = [float(w) for _row, _y, w in cleaned]
    pos = sum(yc)
    neg = len(yc) - pos
    if pos == 0 or neg == 0:
        base = min(0.98, max(0.02, pos / max(1, len(yc))))
        return [0.0] * m, math.log(base / (1.0 - base))

    means, stds = _weighted_mean_std(Xc, wc)
    Xs = [[(row[j] - means[j]) / stds[j] for j in range(m)] for row in Xc]

    class_weights = {0: len(yc) / (2.0 * neg), 1: len(yc) / (2.0 * pos)}
    eff_w = [wc[i] * class_weights[yc[i]] for i in range(len(yc))]
    w_sum = max(1e-12, sum(eff_w))
    pos_rate = min(0.98, max(0.02, sum(eff_w[i] * yc[i] for i in range(len(yc))) / w_sum))

    coef = [0.0] * m
    intercept = math.log(pos_rate / (1.0 - pos_rate))

    for step in range(max(1, int(iters))):
        grad = [0.0] * m
        g_b = 0.0
        for row, y, weight in zip(Xs, yc, eff_w):
            z = intercept + sum(coef[j] * row[j] for j in range(m))
            err = _sigmoid(z) - y
            g_b += weight * err
            for j in range(m):
                grad[j] += weight * err * row[j]
        shrink = lr / math.sqrt(1.0 + step / 75.0)
        intercept -= shrink * (g_b / w_sum)
        for j in range(m):
            coef[j] -= shrink * ((grad[j] / w_sum) + l2 * coef[j])

    coef_raw = [coef[j] / stds[j] for j in range(m)]
    intercept_raw = intercept - sum(coef[j] * means[j] / stds[j] for j in range(m))

    if not math.isfinite(intercept_raw) or not all(math.isfinite(v) for v in coef_raw):
        return [0.0] * m, 0.0
    return coef_raw, float(intercept_raw)


def _purged_train_indices(
    tss: list[int],
    label_available_tss: list[int | None],
    *,
    validation_start_index: int,
) -> list[int]:
    """Indices whose labels were observable before a validation decision.

    Outcome ``ts`` is recommendation time, while ``label_available_ts`` is the
    exact end of the future window measured from the first tradeable candle.
    Legacy rows without that timestamp are deliberately excluded from OOF
    training rather than assigned an optimistic synthetic maturity time.
    """
    split = int(validation_start_index)
    if split <= 0 or split >= len(tss) or len(label_available_tss) != len(tss):
        return []
    try:
        validation_ts = int(tss[split])
    except Exception:
        return []

    indices: list[int] = []
    for idx in range(split):
        try:
            train_ts = int(tss[idx])
            raw_available_ts = label_available_tss[idx]
            if isinstance(raw_available_ts, bool) or raw_available_ts is None:
                continue
            available_ts = int(raw_available_ts)
        except Exception:
            continue
        # A same-timestamp decision is not guaranteed to observe a label that
        # completes at that instant. Malformed availability before signal time
        # is rejected rather than trusted.
        if train_ts < validation_ts and train_ts <= available_ts < validation_ts:
            indices.append(idx)
    return indices


def _time_series_oof_logits(
    X: list[list[float]],
    ys: list[int],
    ws: list[float],
    *,
    min_samples: int,
    tss: list[int] | None = None,
    label_available_tss: list[int | None] | None = None,
) -> tuple[list[float], list[int], list[float]]:
    """Purged chronological out-of-fold logits for Platt-on-top.

    When label timing is supplied, each fold trains only on outcomes whose full
    horizon ended before the first validation decision. This prevents overlapping
    future windows and duplicate timestamps from leaking into OOF calibration.
    """
    n = len(X)
    if n < max(6, min_samples * 2):
        return [], [], []
    n_splits = min(5, max(2, n // max(1, min_samples)))
    fold = max(1, n // (n_splits + 1))
    logits: list[float] = []
    y_out: list[int] = []
    w_out: list[float] = []
    timing_available = (
        tss is not None
        and label_available_tss is not None
        and len(tss) == n
        and len(label_available_tss) == n
    )

    for split in range(fold, n, fold):
        end = min(n, split + fold)
        if split < min_samples or end <= split:
            continue
        train_indices = (
            _purged_train_indices(tss, label_available_tss, validation_start_index=split)
            if timing_available
            else list(range(split))
        )
        if len(train_indices) < min_samples:
            continue
        train_X = [X[idx] for idx in train_indices]
        train_y = [ys[idx] for idx in train_indices]
        train_w = [ws[idx] for idx in train_indices]
        if min(sum(train_y), len(train_y) - sum(train_y)) * 2 < min_samples:
            continue
        coef, intercept = _fit_weighted_logreg_raw(train_X, train_y, train_w, iters=260)
        if not coef:
            continue
        for row, y, weight in zip(X[split:end], ys[split:end], ws[split:end]):
            if len(row) != len(coef):
                continue
            z = intercept + sum(coef[j] * float(row[j]) for j in range(len(coef)))
            if math.isfinite(z):
                logits.append(float(max(-60.0, min(60.0, z))))
                y_out.append(int(y))
                w_out.append(float(weight))
        if end >= n:
            break
    return logits, y_out, w_out


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
            ts_raw = row.get("ts")
            if isinstance(success_raw, bool) or isinstance(ts_raw, bool):
                continue
            ts = int(ts_raw or 0)
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
    X, y_used, w_used, ts_used, label_available_used = [], [], [], [], []
    for r, w in zip(sanitized_rows, ws):
        fv = extract_features(r)
        if fv is not None:
            X.append(fv)
            y_used.append(int(r["success"]))
            w_used.append(w)
            ts_used.append(int(r.get("ts") or 0))
            raw_available_ts = r.get("label_available_ts")
            label_available_used.append(
                None
                if raw_available_ts is None or isinstance(raw_available_ts, bool)
                else _finite_int(raw_available_ts, 0)
            )

    if len(X) < logreg_min_samples:
        return LogRegScaler(
            coef=[], intercept=0.0, platt=platt,
            fitted=True, saved_ts=int(time.time()), n_samples=n,
        )

    try:
        ordered = sorted(
            zip(ts_used, label_available_used, X, y_used, w_used),
            key=lambda item: item[0],
        )
        ts_ord = [item[0] for item in ordered]
        label_available_ord = [item[1] for item in ordered]
        X_ord = [item[2] for item in ordered]
        y_ord = [item[3] for item in ordered]
        w_ord = [item[4] for item in ordered]

        oof_logits, oof_y, oof_w = _time_series_oof_logits(
            X_ord,
            y_ord,
            w_ord,
            min_samples=min_samples,
            tss=ts_ord,
            label_available_tss=label_available_ord,
        )
        coef_raw, intercept_raw = _fit_weighted_logreg_raw(X_ord, y_ord, w_ord)

        platt_top = (
            fit_platt(oof_logits, oof_y, min_samples=min_samples, ws=oof_w)
            if len(oof_logits) >= min_samples
            else PlattScaler(fitted=False)
        )

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
        if not isinstance(obj, dict) or obj.get("type") != "logreg":
            return None
        fitted = _strict_bool(obj.get("fitted"))
        if fitted is None:
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
        platt_fitted = _strict_bool(platt_obj.get("fitted", False))
        if platt_a is None or platt_b is None or platt_fitted is None:
            return None
        platt = PlattScaler(
            a=platt_a,
            b=platt_b,
            fitted=platt_fitted,
            saved_ts=_finite_int(platt_obj.get("ts", 0), 0),
        )
        return LogRegScaler(
            coef=coef,
            intercept=intercept,
            platt=platt,
            fitted=fitted,
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
        if not isinstance(obj, dict) or obj.get("type") != "platt":
            return None
        fitted = _strict_bool(obj.get("fitted"))
        a = _finite_float(obj.get("a", 1.0))
        b = _finite_float(obj.get("b", 0.0))
        if a is None or b is None or fitted is None:
            return None
        return PlattScaler(
            a=a,
            b=b,
            fitted=fitted,
            saved_ts=_finite_int(obj.get("ts", 0), 0),
        )
    except Exception:
        return None


# ── Key registry ─────────────────────────────────────────────────────────────
# v3: feature vector expanded (+5 features: oi_4h, funding, liq_tier, btc_corr, regime_conf)
#     + recency weighting in fit_logreg/fit_platt → forces refit of all saved models

BOT_CALIB_KEYS: dict[str, str] = {
    "futures_grid": "logreg_futures_grid_v3",
}
GLOBAL_LOGREG_KEY = "logreg_global_v3"

# Refit interval — don't refit more than once per hour
CALIB_REFIT_INTERVAL_SEC = 3600
