from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any


def _now_ts() -> int:
    return int(time.time())


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def ewma(points: list[tuple[int, float, float]], half_life_sec: int, now_ts: int) -> float:
    if not points:
        return 0.0
    lam = math.log(2.0) / float(half_life_sec)
    num = 0.0
    den = 0.0
    for ts, s, w in points:
        age = max(0, now_ts - int(ts))
        decay = math.exp(-lam * age)
        ww = float(w) * decay
        num += ww * float(s)
        den += ww
    return float(num / den) if den > 0 else 0.0


def fetch_sentiment_points(conn, scope: str, key: str, since_ts: int, limit: int = 4000) -> list[dict[str, Any]]:
    cur = conn.execute(
        """SELECT ts, sentiment, volume, sources_json, tags_json
           FROM sentiment
           WHERE scope=? AND key=? AND ts>=?
           ORDER BY ts ASC
           LIMIT ?""",
        (scope, key, since_ts, limit),
    )
    rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "ts": int(r["ts"]),
            "sentiment": float(r["sentiment"]),
            "volume": int(r["volume"]),
        })
    return out


def sentiment_vote(score: float) -> str:
    if score <= -0.35:
        return "risk_off"
    if score >= 0.35:
        return "risk_on"
    return "neutral"


def compute_sentiment_agg(conn, scope: str = "global", key: str = "crypto") -> dict[str, Any]:
    now = _now_ts()
    week = 7 * 24 * 3600
    pts = fetch_sentiment_points(conn, scope, key, since_ts=now - week, limit=8000)

    # Compress raw per-minute inserts so the signal reacts to NEW information
    # instead of becoming inert from many near-duplicate rows.
    points = [(p["ts"], p["sentiment"], min(4.0, math.sqrt(max(1.0, float(p["volume"]))))) for p in pts]
    if not points:
        return {
            "ts": now,
            "scope": scope,
            "key": key,
            "ewma": {"1h": 0.0, "6h": 0.0, "1d": 0.0, "7d": 0.0},
            "impulse": {"v_1h": 0.0},
            "effective_score": 0.0,
            "votes": [],
            "scores": {"risk_on": 0.0, "risk_off": 0.0, "neutral": 0.0},
            "regime": "neutral",
            "strength": 0.0,
            "flags": {"panic": False, "euphoria": False},
            "n_points_7d": 0,
            "data_quality": {"has_data": False, "coverage": "none"},
        }

    s_1h = ewma(points, half_life_sec=1 * 3600, now_ts=now)
    s_6h = ewma(points, half_life_sec=6 * 3600, now_ts=now)
    s_1d = ewma(points, half_life_sec=24 * 3600, now_ts=now)
    s_7d = ewma(points, half_life_sec=7 * 24 * 3600, now_ts=now)
    v_1h = float(s_1h - s_6h)

    # Primary numeric sentiment used by the recommender:
    # fast enough to react, but still anchored by slower horizons.
    effective_score = float(_clamp(
        0.50 * s_1h + 0.30 * s_6h + 0.15 * s_1d + 0.05 * s_7d + 0.20 * v_1h,
        -1.0,
        1.0,
    ))

    votes = [
        {"h": "1h", "score": float(s_1h), "vote": sentiment_vote(s_1h), "w": 1.30},
        {"h": "6h", "score": float(s_6h), "vote": sentiment_vote(s_6h), "w": 1.45},
        {"h": "1d", "score": float(s_1d), "vote": sentiment_vote(s_1d), "w": 1.00},
        {"h": "7d", "score": float(s_7d), "vote": sentiment_vote(s_7d), "w": 0.60},
        {"h": "blend", "score": float(effective_score), "vote": sentiment_vote(effective_score), "w": 1.35},
    ]

    score_map = {"risk_on": 0.0, "risk_off": 0.0, "neutral": 0.0}
    for v in votes:
        score_map[v["vote"]] += float(v["w"])

    regime = max(
        score_map.items(),
        key=lambda kv: (kv[1], kv[0] == sentiment_vote(effective_score), kv[0] == sentiment_vote(s_6h), kv[0] == "neutral"),
    )[0]
    sorted_vals = sorted(score_map.values(), reverse=True)
    strength = 0.0
    if sum(sorted_vals) > 0:
        strength = float(_clamp((sorted_vals[0] - sorted_vals[1]) / sum(sorted_vals), 0.0, 1.0))

    panic = (effective_score <= -0.62 and v_1h < -0.08) or (s_6h <= -0.60)
    euphoria = (effective_score >= 0.62 and v_1h > 0.08) or (s_6h >= 0.60)

    return {
        "ts": now,
        "scope": scope,
        "key": key,
        "ewma": {"1h": float(s_1h), "6h": float(s_6h), "1d": float(s_1d), "7d": float(s_7d)},
        "impulse": {"v_1h": float(v_1h)},
        "effective_score": effective_score,
        "votes": votes,
        "scores": score_map,
        "regime": regime,
        "strength": strength,
        "flags": {"panic": bool(panic), "euphoria": bool(euphoria)},
        "n_points_7d": len(pts),
        "data_quality": {"has_data": True, "coverage": "windowed_7d"},
    }


def compute_symbol_sentiment_map(conn, horizon_sec: int = 3600 * 6) -> dict[str, tuple[float, int]]:
    now = int(time.time())
    since = now - horizon_sec
    cur = conn.execute(
        """SELECT key, ts, sentiment, volume
           FROM sentiment
           WHERE scope='symbol' AND ts >= ?
           ORDER BY ts ASC""",
        (since,),
    )

    # Bucket rows into 15-minute slices so one collector run per minute does not
    # inflate confidence and create artificial inertia.
    bucket_sec = 15 * 60
    bucketed: dict[str, dict[int, list[tuple[int, float, float]]]] = defaultdict(lambda: defaultdict(list))
    for row in cur.fetchall():
        sym = str(row["key"])
        ts = int(row["ts"])
        s = float(row["sentiment"])
        # Compress volume contribution; volume is useful, but repeated polls of the same
        # source must not dominate simply because they were written many times.
        v = min(4.0, math.sqrt(max(1.0, float(row["volume"] or 1.0))))
        bucketed[sym][ts // bucket_sec].append((ts, s, v))

    result: dict[str, tuple[float, int]] = {}
    for sym, buckets in bucketed.items():
        points: list[tuple[int, float, float]] = []
        for bucket_id in sorted(buckets):
            items = buckets[bucket_id]
            total_w = sum(w for _, _, w in items)
            if total_w <= 0:
                continue
            avg_sent = sum(s * w for _, s, w in items) / total_w
            ts_bucket = max(ts for ts, _, _ in items)
            # Bucket weight is capped: 30 rows inside one 15m slice should not look like
            # 30 independent observations.
            weight = min(3.0, total_w)
            points.append((ts_bucket, float(avg_sent), float(weight)))

        if not points:
            continue

        s_fast = ewma(points, half_life_sec=45 * 60, now_ts=now)
        s_mid = ewma(points, half_life_sec=2 * 3600, now_ts=now)
        s_slow = ewma(points, half_life_sec=max(3600, horizon_sec), now_ts=now)
        impulse = float(s_fast - s_mid)
        effective = float(_clamp(
            0.60 * s_fast + 0.25 * s_mid + 0.15 * s_slow + 0.30 * impulse,
            -1.0,
            1.0,
        ))
        result[sym] = (effective, len(points))
    return result
