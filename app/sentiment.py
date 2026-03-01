from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any
import httpx

# ── endpoints ────────────────────────────────────────────────────────────────

FNG_URL = "https://api.alternative.me/fng/?limit=1&format=json"

RSS_URLS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]

# Reddit JSON feed — public, no auth, no rate-limit issues at low frequency
REDDIT_RSS: dict[str, str] = {
    "BTCUSDT":  "https://www.reddit.com/r/bitcoin/hot.json?limit=25",
    "ETHUSDT":  "https://www.reddit.com/r/ethereum/hot.json?limit=25",
    "SOLUSDT":  "https://www.reddit.com/r/solana/hot.json?limit=25",
    "XRPUSDT":  "https://www.reddit.com/r/xrp/hot.json?limit=25",
    "DOGEUSDT": "https://www.reddit.com/r/dogecoin/hot.json?limit=25",
}

# CoinGecko IDs for price-momentum fetch (free tier, no auth)
COINGECKO_IDS: dict[str, str] = {
    "BTCUSDT":     "bitcoin",
    "ETHUSDT":     "ethereum",
    "SOLUSDT":     "solana",
    "XRPUSDT":     "ripple",
    "DOGEUSDT":    "dogecoin",
    "BNBUSDT":     "binancecoin",
    "ADAUSDT":     "cardano",
    "AVAXUSDT":    "avalanche-2",
    "DOTUSDT":     "polkadot",
    "LTCUSDT":     "litecoin",
    "LINKUSDT":    "chainlink",
    "UNIUSDT":     "uniswap",
    "AAVEUSDT":    "aave",
    "SUIUSDT":     "sui",
    "NEARUSDT":    "near",
    "MNTUSDT":     "mantle",
    "PEPEUSDT":    "pepe",
    "ENAUSDT":     "ethena",
    "TONUSDT":     "the-open-network",
    "HYPEUSDT":    "hyperliquid",
    "XAUTUSDT":    "tether-gold",
    "VIRTUALUSDT": "virtual-protocol",
    "ZROUSDT":     "layerzero",
    "GRASSUSDT":   "grass",
    "ASTERUSDT":   "aster",
    "MONUSDT":     "monad-testnet",   # may 404 — handled gracefully
    "BIRBUSDT":    "birb-2",
    "BARDUSDT":    "bard",
    "PUMPUSDT":    "pump-fun",
    "LITUSDT":     "litentry",
}

# Keywords for per-symbol RSS matching (lowercase)
SYMBOL_KEYWORDS: dict[str, list[str]] = {
    "BTCUSDT":     ["bitcoin", " btc "],
    "ETHUSDT":     ["ethereum", " eth "],
    "SOLUSDT":     ["solana", " sol "],
    "XRPUSDT":     [" xrp ", "ripple"],
    "DOGEUSDT":    ["dogecoin", " doge "],
    "BNBUSDT":     [" bnb ", "binance coin"],
    "ADAUSDT":     ["cardano", " ada "],
    "AVAXUSDT":    ["avalanche", " avax "],
    "DOTUSDT":     ["polkadot", " dot "],
    "LTCUSDT":     ["litecoin", " ltc "],
    "LINKUSDT":    ["chainlink", " link "],
    "UNIUSDT":     ["uniswap", " uni "],
    "AAVEUSDT":    [" aave "],
    "SUIUSDT":     [" sui "],
    "NEARUSDT":    ["near protocol", " near "],
    "MNTUSDT":     ["mantle", " mnt "],
    "PEPEUSDT":    [" pepe "],
    "ENAUSDT":     ["ethena", " ena "],
    "TONUSDT":     ["toncoin", " ton "],
    "HYPEUSDT":    ["hyperliquid", " hype "],
    "XAUTUSDT":    ["tether gold", " xaut ", " xau "],
    "VIRTUALUSDT": ["virtual protocol", "virtuals"],
    "ZROUSDT":     ["layerzero", " zro "],
    "GRASSUSDT":   [" grass "],
    "ASTERUSDT":   [" aster "],
    "MONUSDT":     [" monad ", " mon "],
    "BIRBUSDT":    [" birb "],
    "BARDUSDT":    [" bard "],
    "PUMPUSDT":    ["pump.fun", " pump "],
    "LITUSDT":     ["litentry", " lit "],
}

# ── text scoring ──────────────────────────────────────────────────────────────

POS_WORDS = {
    "surge","rally","bull","breakout","win","approval","green","recover","record",
    "ath","up","optimism","strong","growth","inflows","partnership","upgrade","positive",
    "gain","profit","rise","launch","adoption","listing","milestone","soar"
}
NEG_WORDS = {
    "crash","dump","bear","hack","lawsuit","ban","liquidation","down","fear","panic",
    "exploit","loss","outflow","scam","negative","weak","collapse","warning","sell",
    "drop","decline","fraud","security","breach","delist","suspension","investigation"
}

def _now_ts() -> int:
    return int(time.time())

def _score_text(text: str) -> float:
    t = (text or "").lower()
    p = sum(1 for w in POS_WORDS if w in t)
    n = sum(1 for w in NEG_WORDS if w in t)
    if p == 0 and n == 0:
        return 0.0
    return (p - n) / float(p + n)

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

# ── 1. Fear & Greed (global) ──────────────────────────────────────────────────

def fetch_fear_greed(client: httpx.Client) -> dict[str, Any] | None:
    try:
        r = client.get(FNG_URL, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        item = (data.get("data") or [None])[0]
        if not item:
            return None
        value = float(item.get("value"))
        sentiment = (value - 50.0) / 50.0   # 0..100 → -1..1
        return {
            "scope": "global",
            "key": "crypto_fng",
            "ts": _now_ts(),
            "sentiment": _clamp(sentiment, -1.0, 1.0),
            "velocity": 0.0,
            "volume": 1,
            "sources": {"alternative_me_fng_value": value, "classification": item.get("value_classification")},
            "tags": ["fear_greed"],
        }
    except Exception:
        return None

# ── 2. RSS global + per-symbol ────────────────────────────────────────────────

def fetch_rss_sentiment(
    client: httpx.Client,
    limit_items: int = 40,
) -> tuple[dict[str, Any] | None, dict[str, list[float]]]:
    """
    Returns:
      global_point: global/crypto_news_rss sentiment dict
      per_symbol:   {SYMBOL: [scores]} — raw per-mention scores for blending
    """
    global_scores: list[float] = []
    per_symbol: dict[str, list[float]] = {}
    used: list[str] = []
    total = 0

    for url in RSS_URLS:
        try:
            r = client.get(url, timeout=10.0, headers={"User-Agent": "bybit-reco-v2/0.3"})
            r.raise_for_status()
            root = ET.fromstring(r.text)
            items = root.findall(".//item")[:limit_items]
            for it in items:
                title = (it.findtext("title") or "").strip()
                desc  = (it.findtext("description") or "").strip()
                full  = (title + " " + desc).lower()
                sc    = _score_text(full)
                global_scores.append(sc)
                total += 1
                # per-symbol routing
                for sym, keywords in SYMBOL_KEYWORDS.items():
                    if any(kw in full for kw in keywords):
                        per_symbol.setdefault(sym, []).append(sc)
            used.append(url)
        except Exception:
            continue

    global_point = None
    if total > 0:
        avg = sum(global_scores) / total
        global_point = {
            "scope": "global",
            "key": "crypto_news_rss",
            "ts": _now_ts(),
            "sentiment": _clamp(float(avg), -1.0, 1.0),
            "velocity": 0.0,
            "volume": int(total),
            "sources": {"rss": used},
            "tags": ["news_rss"],
        }

    return global_point, per_symbol

def _per_symbol_rss_point(sym: str, scores: list[float]) -> dict[str, Any] | None:
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    return {
        "scope": "symbol",
        "key": sym,
        "ts": _now_ts(),
        "sentiment": _clamp(float(avg), -1.0, 1.0),
        "velocity": 0.0,
        "volume": len(scores),
        "sources": {"rss_mentions": len(scores)},
        "tags": ["news_rss", "per_symbol"],
    }

# ── 3. Reddit per-symbol ──────────────────────────────────────────────────────

def fetch_reddit_sentiment(client: httpx.Client) -> dict[str, dict[str, Any]]:
    """Returns {SYMBOL: sentiment_point} for subreddits in REDDIT_RSS."""
    result: dict[str, dict[str, Any]] = {}
    headers = {
        "User-Agent": "bybit-reco-sentiment-bot/0.1 (anonymous; read-only)",
        "Accept": "application/json",
    }
    for sym, url in REDDIT_RSS.items():
        try:
            r = client.get(url, timeout=10.0, headers=headers)
            r.raise_for_status()
            data = r.json()
            posts = data.get("data", {}).get("children", [])
            scores: list[float] = []
            for post in posts:
                pd = post.get("data", {})
                title = pd.get("title", "")
                text  = pd.get("selftext", "")
                # Reddit-native sentiment proxy: upvote_ratio ∈ [0,1] → [-1,1]
                upvote_ratio = float(pd.get("upvote_ratio", 0.5))
                native_sent  = (upvote_ratio - 0.5) * 2.0
                # Text sentiment
                text_sent = _score_text(title + " " + text)
                # Blend: 60% text, 40% upvote ratio
                scores.append(0.6 * text_sent + 0.4 * native_sent)
            if scores:
                avg = sum(scores) / len(scores)
                result[sym] = {
                    "scope": "symbol",
                    "key": sym,
                    "ts": _now_ts(),
                    "sentiment": _clamp(float(avg), -1.0, 1.0),
                    "velocity": 0.0,
                    "volume": len(scores),
                    "sources": {"reddit_url": url, "posts_analyzed": len(scores)},
                    "tags": ["reddit", "per_symbol"],
                }
        except Exception:
            continue
    return result

# ── 4. CoinGecko trending (global signal) ────────────────────────────────────

def fetch_coingecko_trending(client: httpx.Client) -> dict[str, Any] | None:
    """
    CoinGecko /search/trending — top-7 trending coins.
    If any of our symbols is trending → positive signal per-symbol.
    Also returns a global hype point.
    """
    try:
        r = client.get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=10.0,
            headers={"User-Agent": "bybit-reco-v2/0.3"},
        )
        r.raise_for_status()
        data = r.json()
        coins = [c.get("item", {}) for c in data.get("coins", [])]
        return {
            "trending_ids": [c.get("id") for c in coins],
            "trending_symbols": [c.get("symbol","").upper() + "USDT" for c in coins],
        }
    except Exception:
        return None

def trending_to_symbol_points(trending: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """
    For each of our symbols that appears in CoinGecko trending top-7:
    inject a positive sentiment point (being in trending = bullish momentum).
    """
    result: dict[str, dict[str, Any]] = {}
    if not trending:
        return result
    trending_syms = set(trending.get("trending_symbols", []))
    for sym in SYMBOL_KEYWORDS:
        if sym in trending_syms:
            result[sym] = {
                "scope": "symbol",
                "key": sym,
                "ts": _now_ts(),
                "sentiment": 0.6,   # being in top-7 trending is bullish
                "velocity": 0.3,    # momentum signal
                "volume": 7,
                "sources": {"coingecko_trending": True, "rank": trending_syms},
                "tags": ["coingecko_trending", "per_symbol"],
            }
    return result

# ── 5. CoinGecko price momentum (my addition) ────────────────────────────────

def fetch_coingecko_momentum(client: httpx.Client) -> dict[str, dict[str, Any]]:
    """
    CoinGecko /coins/markets — free, no auth.
    24h + 7d price change → normalized per-symbol sentiment.
    This is market-derived, real-time, and independent of text analysis.
    Formula: 0.6*clamp(change_24h/10, -1, 1) + 0.4*clamp(change_7d/20, -1, 1)
    """
    ids = list(set(COINGECKO_IDS.values()))
    # CoinGecko allows ~30 IDs per call on free tier
    chunk_size = 30
    result: dict[str, dict[str, Any]] = {}
    id_to_sym = {v: k for k, v in COINGECKO_IDS.items()}

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        try:
            r = client.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "ids": ",".join(chunk),
                    "price_change_percentage": "24h,7d",
                    "per_page": chunk_size,
                    "page": 1,
                },
                timeout=15.0,
                headers={"User-Agent": "bybit-reco-v2/0.3"},
            )
            r.raise_for_status()
            for coin in r.json():
                cid = coin.get("id")
                sym = id_to_sym.get(cid)
                if not sym:
                    continue
                ch24 = coin.get("price_change_percentage_24h") or 0.0
                ch7d = coin.get("price_change_percentage_7d_in_currency") or 0.0
                # 10% move in 24h ≈ ±1.0; 20% move in 7d ≈ ±1.0
                sent = 0.6 * _clamp(ch24 / 10.0, -1.0, 1.0) + \
                       0.4 * _clamp(ch7d / 20.0, -1.0, 1.0)
                result[sym] = {
                    "scope": "symbol",
                    "key": sym,
                    "ts": _now_ts(),
                    "sentiment": _clamp(float(sent), -1.0, 1.0),
                    "velocity": _clamp(ch24 / 10.0, -1.0, 1.0),
                    "volume": int(coin.get("total_volume") or 1),
                    "sources": {
                        "coingecko_id": cid,
                        "change_24h_pct": round(ch24, 2),
                        "change_7d_pct": round(ch7d, 2),
                    },
                    "tags": ["coingecko_momentum", "per_symbol"],
                }
        except Exception:
            continue
    return result

# ── 6. Blend per-symbol sources ───────────────────────────────────────────────

# Weights for blending per-symbol sources
# momentum is most reliable (actual market data), reddit is high signal,
# rss is noisy but broad, trending is a binary spike signal
_SOURCE_WEIGHTS = {
    "coingecko_momentum": 0.45,
    "reddit":             0.30,
    "news_rss":           0.15,
    "coingecko_trending": 0.10,
}

def blend_per_symbol(
    rss_map: dict[str, list[float]],
    reddit_map: dict[str, dict[str, Any]],
    trending_map: dict[str, dict[str, Any]],
    momentum_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Merge all per-symbol sources into one blended point per symbol.
    Only symbols with at least one source are returned.
    """
    all_syms = set(rss_map) | set(reddit_map) | set(trending_map) | set(momentum_map)
    result: dict[str, dict[str, Any]] = {}
    ts = _now_ts()

    for sym in all_syms:
        wsum = 0.0
        wtotal = 0.0
        sources_used: list[str] = []

        mom = momentum_map.get(sym)
        if mom:
            w = _SOURCE_WEIGHTS["coingecko_momentum"]
            wsum += float(mom["sentiment"]) * w
            wtotal += w
            sources_used.append("momentum")

        red = reddit_map.get(sym)
        if red:
            w = _SOURCE_WEIGHTS["reddit"]
            wsum += float(red["sentiment"]) * w
            wtotal += w
            sources_used.append("reddit")

        rss_scores = rss_map.get(sym, [])
        if rss_scores:
            rss_avg = sum(rss_scores) / len(rss_scores)
            w = _SOURCE_WEIGHTS["news_rss"]
            wsum += rss_avg * w
            wtotal += w
            sources_used.append("rss")

        trd = trending_map.get(sym)
        if trd:
            w = _SOURCE_WEIGHTS["coingecko_trending"]
            wsum += float(trd["sentiment"]) * w
            wtotal += w
            sources_used.append("trending")

        if wtotal == 0:
            continue

        blended = wsum / wtotal
        result[sym] = {
            "scope": "symbol",
            "key": sym,
            "ts": ts,
            "sentiment": _clamp(float(blended), -1.0, 1.0),
            "velocity": float(mom["velocity"]) if mom else 0.0,
            "volume": 1,
            "sources": {
                "sources_used": sources_used,
                "momentum": float(mom["sentiment"]) if mom else None,
                "reddit":   float(red["sentiment"]) if red else None,
                "rss_mentions": len(rss_scores) if rss_scores else 0,
                "trending": bool(trd),
            },
            "tags": ["per_symbol", "blended"] + sources_used,
        }
    return result

# ── 7. Global combine ─────────────────────────────────────────────────────────

def combine_global_sentiment(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not points:
        return None
    wsum = 0.0
    w = 0.0
    tags: list[str] = []
    sources: dict = {}
    for p in points:
        vol = float(p.get("volume") or 1.0)
        wsum += float(p.get("sentiment") or 0.0) * vol
        w += vol
        tags.extend(p.get("tags") or [])
        sources[p.get("key", "src")] = p.get("sources", {})
    s = wsum / w if w else 0.0
    return {
        "scope": "global",
        "key": "crypto",
        "ts": _now_ts(),
        "sentiment": _clamp(float(s), -1.0, 1.0),
        "velocity": 0.0,
        "volume": int(w),
        "sources": sources,
        "tags": sorted(list(set(tags))),
    }

# ── 8. Main collect entry point ───────────────────────────────────────────────

def collect_sentiment_once() -> list[dict[str, Any]]:
    """
    Collect all sentiment sources. Returns list of points to insert into DB.
    Global: FnG + RSS aggregate + combined global.
    Per-symbol: RSS filter + Reddit + CoinGecko trending + CoinGecko momentum → blended.
    """
    pts: list[dict[str, Any]] = []

    with httpx.Client() as client:
        # Global sources
        fng = fetch_fear_greed(client)
        if fng:
            pts.append(fng)

        global_rss, rss_per_sym = fetch_rss_sentiment(client)
        if global_rss:
            pts.append(global_rss)

        # Per-symbol sources
        reddit_map   = fetch_reddit_sentiment(client)
        trending_raw = fetch_coingecko_trending(client)
        trending_map = trending_to_symbol_points(trending_raw)
        momentum_map = fetch_coingecko_momentum(client)

    # Blended per-symbol points
    per_sym_blended = blend_per_symbol(
        rss_per_sym, reddit_map, trending_map, momentum_map
    )
    pts.extend(per_sym_blended.values())

    # Global combined
    global_pts = [p for p in pts if p.get("scope") == "global"]
    combo = combine_global_sentiment(global_pts)
    if combo:
        pts.append(combo)

    return pts
