from __future__ import annotations

import httpx
from typing import Any

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
        # Bybit v5 standard fields: retCode, retMsg, result
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
        # interval: "1" = 1 minute
        params = {"category": category, "symbol": symbol, "interval": interval, "limit": str(limit)}
        data = self._get("/v5/market/kline", params=params)
        # result.list: [ [startTime, open, high, low, close, volume, turnover], ... ] in reverse chronological order
        return data.get("result", {}).get("list", []) or []
