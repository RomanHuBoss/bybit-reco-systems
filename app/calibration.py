"""Calibration module — two-stage confidence estimation.

Stage 1: LogisticRegression on raw features extracted from reasons_json.
         Learns weights from actual outcomes — replaces hand-tuned score formula.
         Remains unavailable when held-out evidence is insufficient.

Stage 2: Platt scaling on top of LogReg probability output.
         Corrects any remaining systematic bias / overconfidence.

Architecture:
  P(success) = Platt( LogReg([range_score, trend, atr_pct, sent,
                               dir_conf, coherence, spread_bps, score,
                               oi_4h, funding, liq_tier, btc_corr, regime_conf]) )

Inference chain:
  held-out-skilled LogReg + Platt  →  capped raw heuristic (audit-only)
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from .grid_math import strict_integer


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
    parsed = strict_integer(value)
    return int(default) if parsed is None else int(parsed)


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
    policy_fingerprint: str = ""

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
    "range_score",        # current independent range-edge feature (v4+)
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
    """Deterministic in-repo logistic regression wrapper with optional Platt on top.

    ``P(success)`` is not sufficient evidence for a trading recommendation: a
    cohort can contain many tiny wins and a few much larger losses.  The
    expectancy fields retain the monetary proxy diagnostics used by the fit gate
    so persistence and the recommendation layer cannot silently forget them.
    """
    coef: list[float] = field(default_factory=list)
    intercept: float = 0.0
    platt: PlattScaler = field(default_factory=PlattScaler)
    fitted: bool = False
    saved_ts: int = 0
    n_samples: int = 0
    return_samples: int = 0
    expectancy_status: str = "unknown"
    weighted_mean_return: float | None = None
    weighted_expected_shortfall: float | None = None
    weighted_return_std: float | None = None
    weighted_effective_return_samples: float = 0.0
    weighted_mean_return_lower_bound: float | None = None
    temporal_cluster_count: int = 0
    temporal_cluster_width_sec: int = 0
    minimum_temporal_clusters: int = 0
    weighted_effective_temporal_clusters: float = 0.0
    weighted_temporal_return_std: float | None = None
    weighted_temporal_mean_return_lower_bound: float | None = None
    expectancy_confidence_level: float = 0.95
    # Probability inference is allowed only after purged chronological OOF has
    # produced enough genuinely out-of-fold predictions and demonstrated skill
    # over both score-only and null baselines.
    oof_status: str = "not_evaluated"
    oof_samples: int = 0
    oof_required_samples: int = 0
    oof_skill_status: str = "not_evaluated"
    oof_feature_log_loss: float | None = None
    oof_score_log_loss: float | None = None
    oof_null_log_loss: float | None = None
    oof_final_feature_log_loss: float | None = None
    oof_final_score_log_loss: float | None = None
    oof_final_null_log_loss: float | None = None
    oof_final_samples: int = 0
    policy_fingerprint: str = ""
    policy_matured_total: int = 0
    policy_labeled_total: int = 0
    policy_censored_total: int = 0
    policy_unresolved_total: int = 0
    policy_invalid_labeled_total: int = 0
    censoring_sensitivity_status: str = "not_evaluated"
    censoring_rate: float = 0.0
    censoring_assumed_return: float | None = None
    censoring_adjusted_mean_return: float | None = None

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


def _continued_beta_fraction(a: float, b: float, x: float) -> float:
    """Stable continued fraction used by the regularized incomplete beta."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    tiny = 1e-300
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) <= 3e-14:
            break
    return h


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_term = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    factor = math.exp(log_term)
    if x < (a + 1.0) / (a + b + 2.0):
        return factor * _continued_beta_fraction(a, b, x) / a
    return 1.0 - factor * _continued_beta_fraction(b, a, 1.0 - x) / b


def _student_t_cdf(value: float, degrees_of_freedom: float) -> float:
    if value == 0.0:
        return 0.5
    x = degrees_of_freedom / (degrees_of_freedom + value * value)
    tail = 0.5 * _regularized_incomplete_beta(
        degrees_of_freedom / 2.0,
        0.5,
        x,
    )
    return 1.0 - tail if value > 0.0 else tail


def _one_sided_student_t_critical(confidence: float, effective_samples: float) -> float:
    """Return a deterministic one-sided Student-t critical value.

    The inverse CDF is solved from the incomplete-beta representation, avoiding a
    runtime SciPy dependency while retaining small-sample accuracy.  Fewer than
    two effective observations cannot support a finite variance bound and fail
    closed.
    """
    confidence_num = _finite_float(confidence)
    n_eff = _finite_float(effective_samples)
    if (
        confidence_num is None
        or not (0.50 < confidence_num < 1.0)
        or n_eff is None
        or n_eff < 2.0
    ):
        return float("inf")
    df = max(1.0, n_eff - 1.0)
    low = 0.0
    high = 2.0
    while _student_t_cdf(high, df) < confidence_num and high < 1e6:
        high *= 2.0
    if high >= 1e6:
        return float("inf")
    for _ in range(80):
        midpoint = (low + high) / 2.0
        if _student_t_cdf(midpoint, df) < confidence_num:
            low = midpoint
        else:
            high = midpoint
    return float((low + high) / 2.0)


def _weighted_return_diagnostics(
    returns: list[float],
    weights: list[float],
    *,
    tail_fraction: float = 0.20,
    confidence_level: float = 0.95,
) -> dict[str, float | None]:
    """Return weighted monetary diagnostics and a one-sided mean lower bound.

    The tail statistic consumes the worst ``tail_fraction`` of total observation
    weight, including a fractional boundary observation.  It is descriptive
    proxy evidence, not a claim about exchange fill truth or future alpha.

    The effective sample size is Kish's weighted sample size.  The lower bound
    uses a one-sided Student-t critical value derived from that effective sample
    size.  This matters for the temporal gate, which can become eligible at only
    twenty independent cohorts.
    """
    cleaned: list[tuple[float, float]] = []
    for raw_ret, raw_weight in zip(returns, weights):
        ret = _finite_float(raw_ret)
        weight = _finite_float(raw_weight)
        if ret is None or weight is None or weight <= 0.0:
            continue
        cleaned.append((ret, weight))
    if not cleaned:
        return {
            "weighted_mean_return": None,
            "weighted_expected_shortfall": None,
            "weighted_return_std": None,
            "weighted_effective_return_samples": 0.0,
            "weighted_mean_return_lower_bound": None,
            "expectancy_confidence_level": float(confidence_level),
        }

    total_weight = sum(weight for _ret, weight in cleaned)
    squared_weight_sum = sum(weight * weight for _ret, weight in cleaned)
    if (
        not math.isfinite(total_weight)
        or total_weight <= 0.0
        or not math.isfinite(squared_weight_sum)
        or squared_weight_sum <= 0.0
    ):
        return {
            "weighted_mean_return": None,
            "weighted_expected_shortfall": None,
            "weighted_return_std": None,
            "weighted_effective_return_samples": 0.0,
            "weighted_mean_return_lower_bound": None,
            "expectancy_confidence_level": float(confidence_level),
        }
    weighted_mean = sum(ret * weight for ret, weight in cleaned) / total_weight
    effective_samples = (total_weight * total_weight) / squared_weight_sum

    weighted_variance = (
        sum(weight * (ret - weighted_mean) ** 2 for ret, weight in cleaned)
        / total_weight
    )
    if effective_samples > 1.0:
        weighted_variance *= effective_samples / (effective_samples - 1.0)
    weighted_variance = max(0.0, weighted_variance)
    weighted_std = math.sqrt(weighted_variance)

    confidence = _finite_float(confidence_level)
    if confidence is None or confidence < 0.50 or confidence >= 1.0:
        confidence = 0.95
    critical_value = _one_sided_student_t_critical(confidence, effective_samples)
    standard_error = (
        weighted_std / math.sqrt(effective_samples)
        if effective_samples > 0.0
        else float("inf")
    )
    lower_bound = weighted_mean - critical_value * standard_error

    fraction = _finite_float(tail_fraction)
    if fraction is None or fraction <= 0.0 or fraction > 1.0:
        fraction = 0.20
    target_weight = total_weight * fraction
    consumed = 0.0
    tail_sum = 0.0
    for ret, weight in sorted(cleaned, key=lambda item: item[0]):
        take = min(weight, target_weight - consumed)
        if take <= 0.0:
            break
        tail_sum += ret * take
        consumed += take
        if consumed >= target_weight - 1e-12:
            break
    expected_shortfall = tail_sum / consumed if consumed > 0.0 else None
    if not math.isfinite(weighted_mean):
        weighted_mean = None
    if not math.isfinite(weighted_std):
        weighted_std = None
    if not math.isfinite(effective_samples):
        effective_samples = 0.0
    if not math.isfinite(lower_bound):
        lower_bound = None
    if expected_shortfall is not None and not math.isfinite(expected_shortfall):
        expected_shortfall = None
    return {
        "weighted_mean_return": weighted_mean,
        "weighted_expected_shortfall": expected_shortfall,
        "weighted_return_std": weighted_std,
        "weighted_effective_return_samples": float(effective_samples),
        "weighted_mean_return_lower_bound": lower_bound,
        "expectancy_confidence_level": float(confidence),
    }


def _temporal_cluster_return_diagnostics(
    rows: list[dict[str, Any]],
    returns: list[float],
    row_weights: list[float],
    *,
    confidence_level: float = 0.95,
) -> dict[str, float | int | None]:
    """Estimate monetary uncertainty from pairwise non-overlapping decisions.

    Cross-sectional rows published at the same recommendation timestamp are one
    market decision and are first collapsed to one weighted cohort mean. Cohorts
    are then thinned with the standard earliest-finish interval-scheduling rule.
    The selected intervals are pairwise non-overlapping, while a long transitive
    chain of partially overlapping horizons cannot percolate into one permanent
    connected component. Row count and symbol count therefore still cannot
    manufacture temporal degrees of freedom.
    """
    observations: list[tuple[int, int, float, float]] = []
    horizon_candidates: list[int] = []
    for row, raw_ret, raw_weight in zip(rows, returns, row_weights):
        ts = strict_integer(row.get("ts"))
        available = strict_integer(row.get("label_available_ts"))
        ret = _finite_float(raw_ret)
        weight = _finite_float(raw_weight)
        if (
            ts is None
            or available is None
            or available <= ts
            or ret is None
            or weight is None
            or weight <= 0.0
        ):
            continue
        observations.append((int(ts), int(available), ret, weight))
        horizon_candidates.append(int(available - ts))

    cluster_width_sec = max(horizon_candidates) if horizon_candidates else 0
    if not observations or cluster_width_sec <= 0:
        return {
            "temporal_cluster_count": 0,
            "temporal_cluster_width_sec": 0,
            "weighted_effective_temporal_clusters": 0.0,
            "weighted_temporal_return_std": None,
            "weighted_temporal_mean_return_lower_bound": None,
        }

    # One recommender publication timestamp represents one decision across the
    # symbol universe. Use the longest maturity boundary conservatively and one
    # cross-sectional weighted mean; do not reward the number of symbols.
    by_decision_ts: dict[int, list[tuple[int, float, float]]] = {}
    for start_ts, end_ts, ret, weight in observations:
        by_decision_ts.setdefault(start_ts, []).append((end_ts, ret, weight))

    decision_cohorts: list[tuple[int, int, float, float]] = []
    for start_ts, items in by_decision_ts.items():
        total_weight = sum(weight for _end, _ret, weight in items)
        if total_weight <= 0.0:
            continue
        decision_cohorts.append((
            int(start_ts),
            max(int(end) for end, _ret, _weight in items),
            sum(ret * weight for _end, ret, weight in items) / total_weight,
            max(weight for _end, _ret, weight in items),
        ))

    # Earliest-finish greedy selection is the maximum-cardinality set of
    # pairwise non-overlapping intervals. Touching half-open horizons are allowed.
    decision_cohorts.sort(key=lambda item: (item[1], item[0], item[2], item[3]))
    cluster_returns: list[float] = []
    cluster_weights: list[float] = []
    last_end: int | None = None
    for start_ts, end_ts, ret, weight in decision_cohorts:
        if last_end is not None and start_ts < last_end:
            continue
        cluster_returns.append(ret)
        cluster_weights.append(weight)
        last_end = end_ts

    temporal = _weighted_return_diagnostics(
        cluster_returns,
        cluster_weights,
        confidence_level=confidence_level,
    )
    return {
        "temporal_cluster_count": len(cluster_returns),
        "temporal_cluster_width_sec": int(cluster_width_sec),
        "weighted_effective_temporal_clusters": float(
            temporal["weighted_effective_return_samples"] or 0.0
        ),
        "weighted_temporal_return_std": temporal["weighted_return_std"],
        "weighted_temporal_mean_return_lower_bound": temporal[
            "weighted_mean_return_lower_bound"
        ],
    }


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
    validation_ts = strict_integer(tss[split])
    if validation_ts is None:
        return []

    indices: list[int] = []
    for idx in range(split):
        train_ts = strict_integer(tss[idx])
        raw_available_ts = label_available_tss[idx]
        available_ts = strict_integer(raw_available_ts)
        if train_ts is None or available_ts is None:
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


def _weighted_log_loss(
    probabilities: list[float],
    labels: list[int],
    weights: list[float],
) -> float | None:
    cleaned: list[tuple[float, int, float]] = []
    for raw_p, raw_y, raw_weight in zip(probabilities, labels, weights):
        p = _finite_float(raw_p)
        y = strict_integer(raw_y)
        weight = _finite_float(raw_weight)
        if p is None or y not in (0, 1) or weight is None or weight <= 0.0:
            continue
        cleaned.append((min(1.0 - 1e-12, max(1e-12, p)), int(y), weight))
    total_weight = sum(weight for _p, _y, weight in cleaned)
    if not cleaned or total_weight <= 0.0:
        return None
    loss = -sum(
        weight * (y * math.log(p) + (1 - y) * math.log(1.0 - p))
        for p, y, weight in cleaned
    ) / total_weight
    return float(loss) if math.isfinite(loss) else None


def _time_series_oof_skill_diagnostics(
    X: list[list[float]],
    scores: list[float],
    ys: list[int],
    ws: list[float],
    *,
    min_samples: int,
    tss: list[int],
    label_available_tss: list[int | None],
) -> dict[str, Any]:
    """Compare feature, score-only and null models on purged future folds.

    Each feature model and both calibration baselines are fit only on labels that
    were available before the validation decision.  The final chronological fold
    is reported separately and must agree with the aggregate result; row count
    alone is never evidence of predictive skill.
    """
    empty = {
        "status": "insufficient",
        "samples": 0,
        "final_samples": 0,
        "feature_log_loss": None,
        "score_log_loss": None,
        "null_log_loss": None,
        "final_feature_log_loss": None,
        "final_score_log_loss": None,
        "final_null_log_loss": None,
        "candidate_coef": None,
        "candidate_intercept": None,
        "candidate_platt": None,
    }
    n = len(X)
    if not (
        n == len(scores) == len(ys) == len(ws) == len(tss) == len(label_available_tss)
        and n >= max(6, min_samples * 2)
    ):
        return empty

    n_splits = min(5, max(2, n // max(1, min_samples)))
    fold_size = max(1, n // (n_splits + 1))
    feature_probs: list[float] = []
    score_probs: list[float] = []
    null_probs: list[float] = []
    labels: list[int] = []
    weights: list[float] = []
    fold_ids: list[int] = []
    candidate_coef: list[float] | None = None
    candidate_intercept: float | None = None
    candidate_platt: PlattScaler | None = None
    candidate_validation_end = 0

    for split in range(fold_size, n, fold_size):
        end = min(n, split + fold_size)
        if split < min_samples or end <= split:
            continue
        train_indices = _purged_train_indices(
            tss,
            label_available_tss,
            validation_start_index=split,
        )
        if len(train_indices) < min_samples:
            continue
        train_y = [ys[idx] for idx in train_indices]
        if min(sum(train_y), len(train_y) - sum(train_y)) * 2 < min_samples:
            continue
        train_X = [X[idx] for idx in train_indices]
        train_w = [ws[idx] for idx in train_indices]
        train_scores = [scores[idx] for idx in train_indices]
        coef, intercept = _fit_weighted_logreg_raw(
            train_X,
            train_y,
            train_w,
            iters=260,
        )
        if not coef:
            continue
        train_logits = [
            intercept + sum(coef[j] * float(row[j]) for j in range(len(coef)))
            for row in train_X
        ]
        feature_platt = fit_platt(
            train_logits,
            train_y,
            min_samples=min_samples,
            ws=train_w,
        )
        score_platt = fit_platt(
            train_scores,
            train_y,
            min_samples=min_samples,
            ws=train_w,
        )
        if not feature_platt.fitted or not score_platt.fitted:
            continue
        total_weight = sum(train_w)
        null_probability = (
            sum(weight * y for weight, y in zip(train_w, train_y)) / total_weight
            if total_weight > 0.0
            else 0.5
        )
        null_probability = min(1.0 - 1e-12, max(1e-12, null_probability))

        for idx in range(split, end):
            row = X[idx]
            if len(row) != len(coef):
                continue
            logit = intercept + sum(coef[j] * float(row[j]) for j in range(len(coef)))
            if not math.isfinite(logit):
                continue
            feature_probs.append(feature_platt.predict(logit))
            score_probs.append(score_platt.predict(scores[idx]))
            null_probs.append(float(null_probability))
            labels.append(int(ys[idx]))
            weights.append(float(ws[idx]))
            fold_ids.append(int(split))
        # Preserve the pipeline fitted strictly before this validation block.
        # Only the terminal candidate may be activated; fitting again on its
        # validation rows would destroy the held-out evidence boundary.
        candidate_coef = list(coef)
        candidate_intercept = float(intercept)
        candidate_platt = feature_platt
        candidate_validation_end = int(end)
        if end >= n:
            break

    if (
        len(feature_probs) < int(min_samples)
        or not fold_ids
        or candidate_validation_end != n
        or candidate_coef is None
        or candidate_intercept is None
        or candidate_platt is None
        or not candidate_platt.fitted
    ):
        return {**empty, "samples": len(feature_probs)}

    feature_loss = _weighted_log_loss(feature_probs, labels, weights)
    score_loss = _weighted_log_loss(score_probs, labels, weights)
    null_loss = _weighted_log_loss(null_probs, labels, weights)
    final_fold = max(fold_ids)
    final_indices = [idx for idx, fold_id in enumerate(fold_ids) if fold_id == final_fold]
    final_feature_loss = _weighted_log_loss(
        [feature_probs[idx] for idx in final_indices],
        [labels[idx] for idx in final_indices],
        [weights[idx] for idx in final_indices],
    )
    final_score_loss = _weighted_log_loss(
        [score_probs[idx] for idx in final_indices],
        [labels[idx] for idx in final_indices],
        [weights[idx] for idx in final_indices],
    )
    final_null_loss = _weighted_log_loss(
        [null_probs[idx] for idx in final_indices],
        [labels[idx] for idx in final_indices],
        [weights[idx] for idx in final_indices],
    )
    metrics = (
        feature_loss,
        score_loss,
        null_loss,
        final_feature_loss,
        final_score_loss,
        final_null_loss,
    )
    if any(value is None for value in metrics):
        return {**empty, "samples": len(feature_probs), "final_samples": len(final_indices)}

    minimum_improvement = 1e-4
    accepted = bool(
        float(feature_loss) + minimum_improvement < float(score_loss)
        and float(feature_loss) + minimum_improvement < float(null_loss)
        and float(final_feature_loss) + minimum_improvement < float(final_score_loss)
        and float(final_feature_loss) + minimum_improvement < float(final_null_loss)
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "samples": len(feature_probs),
        "final_samples": len(final_indices),
        "feature_log_loss": feature_loss,
        "score_log_loss": score_loss,
        "null_log_loss": null_loss,
        "final_feature_log_loss": final_feature_loss,
        "final_score_log_loss": final_score_loss,
        "final_null_log_loss": final_null_loss,
        "candidate_coef": candidate_coef,
        "candidate_intercept": candidate_intercept,
        "candidate_platt": candidate_platt,
    }


def fit_logreg(
    rows: list[dict[str, Any]],
    min_samples: int = 80,
    logreg_min_samples: int = 300,
    half_life_days: float = 21.0,
) -> LogRegScaler:
    """Fit LogReg + Platt from outcome rows with recency weighting.

    - Recency weighting: half_life_days=21 → observation 21 days old weighs 0.5x.
      Crypto regimes shift fast; old outcomes from a different regime should matter less.
    - If n >= logreg_min_samples: full LogReg + OOF Platt, subject to held-out skill.
    - If n < logreg_min_samples: probability inference remains unavailable.
    - If n < min_samples or degenerate WR: unfitted.

    The historical joins used for calibration should be robust to dirty rows in SQLite.
    A single malformed `score`, `success`, `ret` or timestamp must not crash the whole
    fit.  Crucially, binary hit-rate calibration is allowed only when the same matured
    cohort has positive recency-weighted monetary proxy expectancy.
    """
    sanitized_rows: list[dict[str, Any]] = []
    ys: list[int] = []
    tss: list[int] = []
    returns: list[float] = []
    fit_ts = int(time.time())
    for row in rows:
        score = _safe_float(row.get("score"), None)
        success_raw = row.get("success")
        ret = _finite_float(row.get("ret"))
        ts = strict_integer(row.get("ts"))
        if score is None or ret is None:
            continue
        success = strict_integer(success_raw)
        if ts is None or ts <= 0 or success is None:
            continue
        if success not in (0, 1):
            continue
        raw_available_ts = row.get("label_available_ts")
        if raw_available_ts is not None:
            available_ts = strict_integer(raw_available_ts)
            if available_ts is None or available_ts <= 0 or available_ts < ts or available_ts > fit_ts:
                continue
        else:
            # Legacy rows without a demonstrable label maturity timestamp are
            # not eligible for time-aware calibration. Treating them as known
            # at recommendation time would reintroduce label-availability leakage.
            continue
        sanitized_rows.append({
            **row,
            "ts": ts,
            "success": success,
            "ret": ret,
            "label_available_ts": available_ts,
        })
        ys.append(success)
        tss.append(ts)
        returns.append(ret)

    n = len(sanitized_rows)
    balance = label_balance_stats(ys)

    # Monetary expectancy uses the raw matured-return sample floor.  Class
    # balance is a probability-calibration concern, not permission to ignore a
    # rare but economically dominant loss.
    if n < int(min_samples):
        return LogRegScaler(
            fitted=False,
            saved_ts=fit_ts,
            n_samples=n,
            return_samples=n,
            expectancy_status="insufficient",
        )

    ws = _recency_weights(tss, half_life_days=half_life_days)
    monetary = _weighted_return_diagnostics(returns, ws)
    weighted_mean_return = monetary["weighted_mean_return"]
    weighted_expected_shortfall = monetary["weighted_expected_shortfall"]
    weighted_return_std = monetary["weighted_return_std"]
    weighted_effective_return_samples = float(
        monetary["weighted_effective_return_samples"] or 0.0
    )
    weighted_mean_return_lower_bound = monetary["weighted_mean_return_lower_bound"]
    expectancy_confidence_level = float(
        monetary["expectancy_confidence_level"] or 0.95
    )
    temporal = _temporal_cluster_return_diagnostics(
        sanitized_rows,
        returns,
        ws,
        confidence_level=expectancy_confidence_level,
    )
    temporal_cluster_count = int(temporal["temporal_cluster_count"] or 0)
    temporal_cluster_width_sec = int(temporal["temporal_cluster_width_sec"] or 0)
    minimum_temporal_clusters = max(1, min(20, int(math.ceil(float(min_samples) / 4.0))))
    weighted_effective_temporal_clusters = float(
        temporal["weighted_effective_temporal_clusters"] or 0.0
    )
    weighted_temporal_return_std = temporal["weighted_temporal_return_std"]
    weighted_temporal_mean_return_lower_bound = temporal[
        "weighted_temporal_mean_return_lower_bound"
    ]

    monetary_fields = {
        "weighted_mean_return": weighted_mean_return,
        "weighted_expected_shortfall": weighted_expected_shortfall,
        "weighted_return_std": weighted_return_std,
        "weighted_effective_return_samples": weighted_effective_return_samples,
        "weighted_mean_return_lower_bound": weighted_mean_return_lower_bound,
        "temporal_cluster_count": temporal_cluster_count,
        "temporal_cluster_width_sec": temporal_cluster_width_sec,
        "minimum_temporal_clusters": minimum_temporal_clusters,
        "weighted_effective_temporal_clusters": weighted_effective_temporal_clusters,
        "weighted_temporal_return_std": weighted_temporal_return_std,
        "weighted_temporal_mean_return_lower_bound": weighted_temporal_mean_return_lower_bound,
        "expectancy_confidence_level": expectancy_confidence_level,
    }

    if (
        weighted_mean_return is None
        or weighted_mean_return_lower_bound is None
        or weighted_temporal_mean_return_lower_bound is None
        or weighted_effective_return_samples + 1e-6 < float(min_samples)
        or temporal_cluster_count < minimum_temporal_clusters
        or weighted_effective_temporal_clusters + 1e-6 < float(minimum_temporal_clusters)
    ):
        return LogRegScaler(
            fitted=False,
            saved_ts=fit_ts,
            n_samples=n,
            return_samples=n,
            expectancy_status="insufficient",
            **monetary_fields,
        )
    if weighted_mean_return <= 0.0:
        return LogRegScaler(
            fitted=False,
            saved_ts=fit_ts,
            n_samples=n,
            return_samples=n,
            expectancy_status="negative",
            **monetary_fields,
        )
    if (
        weighted_mean_return_lower_bound <= 0.0
        or weighted_temporal_mean_return_lower_bound <= 0.0
    ):
        return LogRegScaler(
            fitted=False,
            saved_ts=fit_ts,
            n_samples=n,
            return_samples=n,
            expectancy_status="uncertain",
            **monetary_fields,
        )

    # Probability calibration still requires the balanced effective sample;
    # monetary evidence above was intentionally evaluated first.
    if int(balance["effective_samples"]) < int(min_samples):
        return LogRegScaler(
            fitted=False,
            saved_ts=fit_ts,
            n_samples=n,
            return_samples=n,
            expectancy_status="positive",
            **monetary_fields,
        )

    # Guard: degenerate class balance. A 90%+ hit-rate on proxy labels is not a
    # trustworthy basis for probability calibration; keep confidence heuristic,
    # while retaining the positive monetary-expectancy diagnostic.
    win_rate = float(balance["win_rate"] or 0.0)
    if win_rate < 0.15 or win_rate > 0.85:
        return LogRegScaler(
            fitted=False,
            saved_ts=fit_ts,
            n_samples=n,
            return_samples=n,
            expectancy_status="positive",
            **monetary_fields,
        )

    if n < logreg_min_samples:
        return LogRegScaler(
            coef=[], intercept=0.0, platt=PlattScaler(fitted=False),
            fitted=False, saved_ts=fit_ts, n_samples=n,
            return_samples=n, expectancy_status="positive",
            oof_status="insufficient",
            oof_samples=0,
            oof_required_samples=int(min_samples),
            oof_skill_status="insufficient",
            **monetary_fields,
        )

    # Build feature matrix
    X, y_used, w_used, ts_used, label_available_used, score_used = [], [], [], [], [], []
    for r, w in zip(sanitized_rows, ws):
        fv = extract_features(r)
        if fv is not None:
            X.append(fv)
            y_used.append(int(r["success"]))
            w_used.append(w)
            ts_used.append(int(r.get("ts") or 0))
            score_used.append(float(r.get("score") or 0.0))
            raw_available_ts = r.get("label_available_ts")
            parsed_available_ts = strict_integer(raw_available_ts)
            label_available_used.append(
                parsed_available_ts
                if parsed_available_ts is not None and parsed_available_ts > 0
                else None
            )

    if len(X) < logreg_min_samples:
        return LogRegScaler(
            coef=[], intercept=0.0, platt=PlattScaler(fitted=False),
            fitted=False, saved_ts=fit_ts, n_samples=n,
            return_samples=n, expectancy_status="positive",
            oof_status="insufficient",
            oof_samples=0,
            oof_required_samples=int(min_samples),
            oof_skill_status="insufficient",
            **monetary_fields,
        )

    try:
        ordered = sorted(
            zip(ts_used, label_available_used, X, y_used, w_used, score_used),
            key=lambda item: item[0],
        )
        ts_ord = [item[0] for item in ordered]
        label_available_ord = [item[1] for item in ordered]
        X_ord = [item[2] for item in ordered]
        y_ord = [item[3] for item in ordered]
        w_ord = [item[4] for item in ordered]
        score_ord = [item[5] for item in ordered]

        oof_logits, oof_y, oof_w = _time_series_oof_logits(
            X_ord,
            y_ord,
            w_ord,
            min_samples=min_samples,
            tss=ts_ord,
            label_available_tss=label_available_ord,
        )

        oof_required_samples = int(min_samples)
        oof_samples = len(oof_logits)
        platt_top = (
            fit_platt(oof_logits, oof_y, min_samples=min_samples, ws=oof_w)
            if oof_samples >= oof_required_samples
            else PlattScaler(fitted=False)
        )
        skill = (
            _time_series_oof_skill_diagnostics(
                X_ord,
                score_ord,
                y_ord,
                w_ord,
                min_samples=min_samples,
                tss=ts_ord,
                label_available_tss=label_available_ord,
            )
            if oof_samples >= oof_required_samples
            else {"status": "insufficient"}
        )
        skill_fields = {
            "oof_skill_status": str(skill.get("status") or "insufficient"),
            "oof_feature_log_loss": skill.get("feature_log_loss"),
            "oof_score_log_loss": skill.get("score_log_loss"),
            "oof_null_log_loss": skill.get("null_log_loss"),
            "oof_final_feature_log_loss": skill.get("final_feature_log_loss"),
            "oof_final_score_log_loss": skill.get("final_score_log_loss"),
            "oof_final_null_log_loss": skill.get("final_null_log_loss"),
            "oof_final_samples": int(skill.get("final_samples") or 0),
        }

        # A model trained on the full retained sample is not an out-of-sample
        # probability model by itself.  When chronological purging leaves too few
        # predictions, expose neither feature coefficients nor the in-sample
        # score-only Platt baseline as calibrated confidence.
        if (
            oof_samples < oof_required_samples
            or not platt_top.fitted
        ):
            return LogRegScaler(
                coef=[], intercept=0.0, platt=PlattScaler(fitted=False),
                fitted=False, saved_ts=fit_ts, n_samples=n,
                return_samples=n, expectancy_status="positive",
                oof_status="insufficient",
                oof_samples=int(oof_samples),
                oof_required_samples=int(oof_required_samples),
                **skill_fields,
                **monetary_fields,
            )

        candidate_coef_raw = skill.get("candidate_coef")
        candidate_intercept = _finite_float(skill.get("candidate_intercept"))
        candidate_platt = skill.get("candidate_platt")
        candidate_coef = (
            [_finite_float(value) for value in candidate_coef_raw]
            if isinstance(candidate_coef_raw, list)
            else []
        )
        candidate_is_valid = bool(
            candidate_coef
            and len(candidate_coef) == len(FEATURE_NAMES)
            and all(value is not None for value in candidate_coef)
            and candidate_intercept is not None
            and isinstance(candidate_platt, PlattScaler)
            and candidate_platt.fitted
        )
        if str(skill.get("status") or "") != "accepted" or not candidate_is_valid:
            return LogRegScaler(
                coef=[], intercept=0.0, platt=PlattScaler(fitted=False),
                fitted=False, saved_ts=fit_ts, n_samples=n,
                return_samples=n, expectancy_status="positive",
                oof_status="no_skill",
                oof_samples=int(oof_samples),
                oof_required_samples=int(oof_required_samples),
                **skill_fields,
                **monetary_fields,
            )

        return LogRegScaler(
            coef=[float(value) for value in candidate_coef if value is not None],
            intercept=float(candidate_intercept),
            platt=candidate_platt,
            fitted=True, saved_ts=fit_ts, n_samples=len(X),
            return_samples=n, expectancy_status="positive",
            oof_status="sufficient",
            oof_samples=int(oof_samples),
            oof_required_samples=int(oof_required_samples),
            **skill_fields,
            **monetary_fields,
        )

    except Exception:
        return LogRegScaler(
            coef=[], intercept=0.0, platt=PlattScaler(fitted=False),
            fitted=False, saved_ts=fit_ts, n_samples=n,
            return_samples=n, expectancy_status="positive",
            oof_status="error",
            oof_samples=0,
            oof_required_samples=int(min_samples),
            **monetary_fields,
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
        "oof_validation": {
            "status": model.oof_status,
            "samples": model.oof_samples,
            "required_samples": model.oof_required_samples,
            "skill_status": model.oof_skill_status,
            "feature_log_loss": model.oof_feature_log_loss,
            "score_log_loss": model.oof_score_log_loss,
            "null_log_loss": model.oof_null_log_loss,
            "final_feature_log_loss": model.oof_final_feature_log_loss,
            "final_score_log_loss": model.oof_final_score_log_loss,
            "final_null_log_loss": model.oof_final_null_log_loss,
            "final_samples": model.oof_final_samples,
        },
        "policy_evidence": {
            "fingerprint": model.policy_fingerprint,
            "matured_total": model.policy_matured_total,
            "labeled_total": model.policy_labeled_total,
            "censored_total": model.policy_censored_total,
            "unresolved_total": model.policy_unresolved_total,
            "sensitivity_status": model.censoring_sensitivity_status,
            "censoring_rate": model.censoring_rate,
            "assumed_return": model.censoring_assumed_return,
            "adjusted_mean_return": model.censoring_adjusted_mean_return,
            "invalid_labeled_total": model.policy_invalid_labeled_total,
        },
        "expectancy": {
            "status": model.expectancy_status,
            "return_samples": model.return_samples,
            "weighted_mean_return": model.weighted_mean_return,
            "weighted_expected_shortfall": model.weighted_expected_shortfall,
            "weighted_return_std": model.weighted_return_std,
            "weighted_effective_return_samples": model.weighted_effective_return_samples,
            "weighted_mean_return_lower_bound": model.weighted_mean_return_lower_bound,
            "temporal_cluster_count": model.temporal_cluster_count,
            "temporal_cluster_width_sec": model.temporal_cluster_width_sec,
            "minimum_temporal_clusters": model.minimum_temporal_clusters,
            "weighted_effective_temporal_clusters": model.weighted_effective_temporal_clusters,
            "weighted_temporal_return_std": model.weighted_temporal_return_std,
            "weighted_temporal_mean_return_lower_bound": model.weighted_temporal_mean_return_lower_bound,
            "confidence_level": model.expectancy_confidence_level,
        },
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

        expectancy_obj = obj.get("expectancy") or {}
        if not isinstance(expectancy_obj, dict):
            return None
        expectancy_status = str(expectancy_obj.get("status") or "unknown").strip().lower()
        if expectancy_status not in {
            "unknown",
            "insufficient",
            "negative",
            "uncertain",
            "positive",
            "censored",
        }:
            return None
        return_samples = max(0, _finite_int(expectancy_obj.get("return_samples", 0), 0))
        mean_raw = expectancy_obj.get("weighted_mean_return")
        es_raw = expectancy_obj.get("weighted_expected_shortfall")
        std_raw = expectancy_obj.get("weighted_return_std")
        eff_raw = expectancy_obj.get("weighted_effective_return_samples", 0.0)
        lower_raw = expectancy_obj.get("weighted_mean_return_lower_bound")
        temporal_cluster_count = max(0, _finite_int(expectancy_obj.get("temporal_cluster_count", 0), 0))
        temporal_cluster_width_sec = max(0, _finite_int(expectancy_obj.get("temporal_cluster_width_sec", 0), 0))
        minimum_temporal_clusters = max(0, _finite_int(expectancy_obj.get("minimum_temporal_clusters", 0), 0))
        temporal_eff_raw = expectancy_obj.get("weighted_effective_temporal_clusters", 0.0)
        temporal_std_raw = expectancy_obj.get("weighted_temporal_return_std")
        temporal_lower_raw = expectancy_obj.get("weighted_temporal_mean_return_lower_bound")
        confidence_raw = expectancy_obj.get("confidence_level", 0.95)
        weighted_mean_return = None if mean_raw is None else _finite_float(mean_raw)
        weighted_expected_shortfall = None if es_raw is None else _finite_float(es_raw)
        weighted_return_std = None if std_raw is None else _finite_float(std_raw)
        weighted_effective_return_samples = _finite_float(eff_raw)
        weighted_mean_return_lower_bound = None if lower_raw is None else _finite_float(lower_raw)
        weighted_effective_temporal_clusters = _finite_float(temporal_eff_raw)
        weighted_temporal_return_std = None if temporal_std_raw is None else _finite_float(temporal_std_raw)
        weighted_temporal_mean_return_lower_bound = None if temporal_lower_raw is None else _finite_float(temporal_lower_raw)
        expectancy_confidence_level = _finite_float(confidence_raw)
        if mean_raw is not None and weighted_mean_return is None:
            return None
        if es_raw is not None and weighted_expected_shortfall is None:
            return None
        if std_raw is not None and weighted_return_std is None:
            return None
        if lower_raw is not None and weighted_mean_return_lower_bound is None:
            return None
        if temporal_std_raw is not None and weighted_temporal_return_std is None:
            return None
        if temporal_lower_raw is not None and weighted_temporal_mean_return_lower_bound is None:
            return None
        if weighted_effective_return_samples is None or weighted_effective_return_samples < 0.0:
            return None
        if weighted_effective_temporal_clusters is None or weighted_effective_temporal_clusters < 0.0:
            return None
        if expectancy_confidence_level is None or not (0.50 <= expectancy_confidence_level < 1.0):
            return None
        if expectancy_status in {"negative", "uncertain", "positive"} and (
            return_samples <= 0 or weighted_mean_return is None
        ):
            return None

        oof_obj = obj.get("oof_validation") or {}
        if not isinstance(oof_obj, dict):
            return None
        oof_status = str(oof_obj.get("status") or "not_evaluated").strip().lower()
        if oof_status not in {
            "not_evaluated",
            "not_required_score_only",
            "insufficient",
            "sufficient",
            "no_skill",
            "error",
        }:
            return None
        oof_samples = max(0, _finite_int(oof_obj.get("samples", 0), 0))
        oof_required_samples = max(0, _finite_int(oof_obj.get("required_samples", 0), 0))
        if oof_status == "sufficient" and (
            oof_required_samples <= 0 or oof_samples < oof_required_samples
        ):
            return None
        oof_skill_status = str(
            oof_obj.get("skill_status") or "not_evaluated"
        ).strip().lower()
        if oof_skill_status not in {
            "not_evaluated",
            "insufficient",
            "accepted",
            "rejected",
        }:
            return None

        def _optional_metric(name: str) -> float | None:
            raw = oof_obj.get(name)
            if raw is None:
                return None
            return _finite_float(raw)

        oof_feature_log_loss = _optional_metric("feature_log_loss")
        oof_score_log_loss = _optional_metric("score_log_loss")
        oof_null_log_loss = _optional_metric("null_log_loss")
        oof_final_feature_log_loss = _optional_metric("final_feature_log_loss")
        oof_final_score_log_loss = _optional_metric("final_score_log_loss")
        oof_final_null_log_loss = _optional_metric("final_null_log_loss")
        for metric_name, metric_value in (
            ("feature_log_loss", oof_feature_log_loss),
            ("score_log_loss", oof_score_log_loss),
            ("null_log_loss", oof_null_log_loss),
            ("final_feature_log_loss", oof_final_feature_log_loss),
            ("final_score_log_loss", oof_final_score_log_loss),
            ("final_null_log_loss", oof_final_null_log_loss),
        ):
            if oof_obj.get(metric_name) is not None and metric_value is None:
                return None
        oof_final_samples = max(0, _finite_int(oof_obj.get("final_samples", 0), 0))

        policy_obj = obj.get("policy_evidence") or {}
        if not isinstance(policy_obj, dict):
            return None
        policy_fingerprint = str(policy_obj.get("fingerprint") or "").strip()
        policy_matured_total = max(0, _finite_int(policy_obj.get("matured_total", 0), 0))
        policy_labeled_total = max(0, _finite_int(policy_obj.get("labeled_total", 0), 0))
        policy_censored_total = max(0, _finite_int(policy_obj.get("censored_total", 0), 0))
        policy_unresolved_total = max(0, _finite_int(policy_obj.get("unresolved_total", 0), 0))
        censoring_sensitivity_status = str(policy_obj.get("sensitivity_status") or "not_evaluated").strip().lower()
        if censoring_sensitivity_status not in {"not_evaluated", "passed", "failed", "hard_block"}:
            return None
        censoring_rate = _finite_float(policy_obj.get("censoring_rate", 0.0))
        censoring_assumed_return = _finite_float(policy_obj.get("assumed_return"))
        censoring_adjusted_mean_return = _finite_float(policy_obj.get("adjusted_mean_return"))
        if censoring_rate is None or censoring_rate < 0.0 or censoring_rate > 1.0:
            return None
        policy_invalid_labeled_total = max(
            0,
            _finite_int(policy_obj.get("invalid_labeled_total", 0), 0),
        )
        if policy_labeled_total + policy_censored_total + policy_unresolved_total > policy_matured_total:
            return None
        if policy_invalid_labeled_total > policy_labeled_total:
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
        if fitted and (
            len(coef) != N_FEATURES
            or not platt.fitted
            or expectancy_status != "positive"
            or oof_status != "sufficient"
            or oof_skill_status != "accepted"
            or oof_required_samples <= 0
            or oof_samples < oof_required_samples
        ):
            return None
        if not fitted and (coef or platt.fitted):
            return None
        return LogRegScaler(
            coef=coef,
            intercept=intercept,
            platt=platt,
            fitted=fitted,
            saved_ts=_finite_int(obj.get("ts", 0), 0),
            n_samples=max(0, _finite_int(obj.get("n_samples", 0), 0)),
            return_samples=return_samples,
            expectancy_status=expectancy_status,
            weighted_mean_return=weighted_mean_return,
            weighted_expected_shortfall=weighted_expected_shortfall,
            weighted_return_std=weighted_return_std,
            weighted_effective_return_samples=float(weighted_effective_return_samples),
            weighted_mean_return_lower_bound=weighted_mean_return_lower_bound,
            temporal_cluster_count=temporal_cluster_count,
            temporal_cluster_width_sec=temporal_cluster_width_sec,
            minimum_temporal_clusters=minimum_temporal_clusters,
            weighted_effective_temporal_clusters=float(weighted_effective_temporal_clusters),
            weighted_temporal_return_std=weighted_temporal_return_std,
            weighted_temporal_mean_return_lower_bound=weighted_temporal_mean_return_lower_bound,
            expectancy_confidence_level=float(expectancy_confidence_level),
            oof_status=oof_status,
            oof_samples=int(oof_samples),
            oof_required_samples=int(oof_required_samples),
            oof_skill_status=oof_skill_status,
            oof_feature_log_loss=oof_feature_log_loss,
            oof_score_log_loss=oof_score_log_loss,
            oof_null_log_loss=oof_null_log_loss,
            oof_final_feature_log_loss=oof_final_feature_log_loss,
            oof_final_score_log_loss=oof_final_score_log_loss,
            oof_final_null_log_loss=oof_final_null_log_loss,
            oof_final_samples=oof_final_samples,
            policy_fingerprint=policy_fingerprint,
            policy_matured_total=policy_matured_total,
            policy_labeled_total=policy_labeled_total,
            policy_censored_total=policy_censored_total,
            policy_unresolved_total=policy_unresolved_total,
            censoring_sensitivity_status=censoring_sensitivity_status,
            censoring_rate=float(censoring_rate),
            censoring_assumed_return=censoring_assumed_return,
            censoring_adjusted_mean_return=censoring_adjusted_mean_return,
            policy_invalid_labeled_total=policy_invalid_labeled_total,
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
        "policy_fingerprint": scaler.policy_fingerprint,
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
            policy_fingerprint=str(obj.get("policy_fingerprint") or "").strip(),
        )
    except Exception:
        return None


# ── Key registry ─────────────────────────────────────────────────────────────
# v19: binds cached evidence to an immutable policy fingerprint, requires held-out
#      skill over score-only/null baselines, and uses small-sample temporal bounds.
# v18: retains the v17 purged-OOF/temporal rule and starts a new model-lineage dataset;
# v17: retains the v16 purged-OOF activation rule and replaces transitive
#      overlap components with deterministic non-overlapping decision cohorts.
#      Existing outcomes remain valid, but cached v16 diagnostics must refit.

BOT_CALIB_KEYS: dict[str, str] = {
    "futures_grid": "logreg_futures_grid_v19",
}
GLOBAL_LOGREG_KEY = "logreg_global_v19"

# Refit interval — don't refit more than once per hour
CALIB_REFIT_INTERVAL_SEC = 3600
