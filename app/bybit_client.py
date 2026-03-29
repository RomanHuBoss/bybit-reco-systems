from __future__ import annotations

import math

import httpx
from typing import Any


def _safe_float(value: Any) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return num


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)

class BybitPublicClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        r = self._client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')}")
        return data

    def get_tickers(self, category: str, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/v5/market/tickers", params=params)
        return data.get("result", {}).get("list", []) or []

    def get_kline(self, category: str, symbol: str, interval: str = "1", limit: int = 200) -> list[list[str]]:
        params = {"category": category, "symbol": symbol, "interval": interval, "limit": str(limit)}
        data = self._get("/v5/market/kline", params=params)
        return data.get("result", {}).get("list", []) or []

    def get_funding_rate(self, symbol: str) -> dict[str, Any] | None:
        """Current funding rate from linear tickers endpoint."""
        data = self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        items = data.get("result", {}).get("list", [])
        if not items:
            return None
        t = items[0]
        next_funding_raw = _safe_int(t.get("nextFundingTime") or 0)
        # Bybit returns nextFundingTime in milliseconds. Normalize to seconds so
        # all downstream horizon comparisons use one unit system.
        next_funding_ts = next_funding_raw // 1000 if next_funding_raw > 10**11 else next_funding_raw
        funding_rate = _safe_float(t.get("fundingRate"))
        return {
            "symbol": symbol,
            "funding_rate": funding_rate,
            "next_funding_ts": next_funding_ts,
        }

    def get_open_interest(self, symbol: str, interval: str = "1h", limit: int = 48) -> list[dict[str, Any]]:
        """Historical open interest for a linear perpetual.
        interval: 5min / 15min / 30min / 1h / 4h / 1d
        Returns list newest-first: [{ts, oi}, ...]
        """
        data = self._get("/v5/market/open-interest", {
            "category": "linear",
            "symbol": symbol,
            "intervalTime": interval,
            "limit": str(limit),
        })
        items = data.get("result", {}).get("list", []) or []
        out: list[dict[str, Any]] = []
        for r in items:
            ts_raw = _safe_int(r.get("timestamp"))
            oi = _safe_float(r.get("openInterest"))
            if ts_raw <= 0 or oi is None or oi < 0:
                continue
            ts = ts_raw // 1000 if ts_raw > 10**11 else ts_raw
            out.append({"ts": ts, "oi": oi})
        return out
    def get_instrument_info(self, category: str, symbol: str) -> dict[str, Any] | None:
        """Metadata for a single instrument (tick size, lot size, etc.)."""
        data = self._get("/v5/market/instruments-info", {"category": category, "symbol": symbol})
        items = data.get("result", {}).get("list", []) or []
        return items[0] if items else None

