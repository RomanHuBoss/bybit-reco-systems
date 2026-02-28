from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any
import httpx

FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"

RSS_URLS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]

POS_WORDS = {
    "surge","rally","bull","breakout","win","approval","green","recover","record","ath","up",
    "optimism","strong","growth","inflows","partnership","upgrade","positive"
}
NEG_WORDS = {
    "crash","dump","bear","hack","lawsuit","ban","liquidation","down","fear","panic",
    "exploit","loss","outflow","scam","negative","weak","collapse"
}

def _now_ts() -> int:
    return int(time.time())

def fetch_fear_greed(client: httpx.Client) -> dict[str, Any] | None:
    try:
        r = client.get(FNG_URL, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        item = (data.get("data") or [None])[0]
        if not item:
            return None
        value = float(item.get("value"))
        sentiment = (value - 50.0) / 50.0  # 0..100 -> -1..1
        return {
            "scope": "global",
            "key": "crypto_fng",
            "ts": _now_ts(),
            "sentiment": max(-1.0, min(1.0, sentiment)),
            "velocity": 0.0,
            "volume": 1,
            "sources": {"alternative_me_fng_value": value, "classification": item.get("value_classification")},
            "tags": ["fear_greed"],
        }
    except Exception:
        return None

def _score_text(text: str) -> float:
    t = (text or "").lower()
    p = sum(1 for w in POS_WORDS if w in t)
    n = sum(1 for w in NEG_WORDS if w in t)
    if p == 0 and n == 0:
        return 0.0
    return (p - n) / float(p + n)

def fetch_rss_headlines_sentiment(client: httpx.Client, limit_items: int = 30) -> dict[str, Any] | None:
    scores = []
    total = 0
    used = []
    for url in RSS_URLS:
        try:
            r = client.get(url, timeout=10.0, headers={"User-Agent":"bybit-reco-v2/0.2"})
            r.raise_for_status()
            root = ET.fromstring(r.text)
            items = root.findall(".//item")[:limit_items]
            for it in items:
                title = (it.findtext("title") or "").strip()
                desc = (it.findtext("description") or "").strip()
                scores.append(_score_text(title + " " + desc))
                total += 1
            used.append(url)
        except Exception:
            continue
    if total == 0:
        return None
    avg = sum(scores) / total
    return {
        "scope": "global",
        "key": "crypto_news_rss",
        "ts": _now_ts(),
        "sentiment": max(-1.0, min(1.0, float(avg))),
        "velocity": 0.0,
        "volume": int(total),
        "sources": {"rss": used},
        "tags": ["news_rss"],
    }

def combine_global_sentiment(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not points:
        return None
    wsum = 0.0
    w = 0.0
    tags = []
    sources = {}
    for p in points:
        vol = float(p.get("volume") or 1.0)
        wsum += float(p.get("sentiment") or 0.0) * vol
        w += vol
        tags.extend(p.get("tags") or [])
        sources[p.get("key","src")] = p.get("sources", {})
    s = wsum / w if w else 0.0
    return {
        "scope": "global",
        "key": "crypto",
        "ts": _now_ts(),
        "sentiment": max(-1.0, min(1.0, float(s))),
        "velocity": 0.0,
        "volume": int(w),
        "sources": sources,
        "tags": sorted(list(set(tags))),
    }

def collect_sentiment_once() -> list[dict[str, Any]]:
    pts: list[dict[str, Any]] = []
    with httpx.Client() as client:
        fng = fetch_fear_greed(client)
        if fng:
            pts.append(fng)
        rss = fetch_rss_headlines_sentiment(client)
        if rss:
            pts.append(rss)
    combo = combine_global_sentiment(pts)
    if combo:
        pts.append(combo)
    return pts
