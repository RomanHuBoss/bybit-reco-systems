from __future__ import annotations

import math
import time
from typing import Any, Mapping

import httpx


RETRYABLE_BYBIT_RETCODES = {10000, 10006, 10016, 10018, 30034}


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


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None




def _header_value(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        return None
    return None if value in (None, "") else str(value)

def _result_list(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Возвращает list[dict] из `result.list` без протечки битой формы выше по стеку.

    Upstream API в норме всегда отдаёт dict -> result -> list, но в реальной жизни
    прокси, WAF или partially broken mock/stub могут вернуть иную форму. Для
    публичного клиента лучше деградировать к пустому списку, чем выбросить
    `AttributeError` из `.get(...)` в бизнес-слое.
    """
    result = _mapping_or_none(data.get("result")) or {}
    items = result.get("list")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, Mapping)]


class BybitPublicClient:
    def __init__(self, base_url: str, timeout: float = 10.0, max_retries: int = 2, backoff_base_sec: float = 0.25):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.backoff_base_sec = max(0.05, float(backoff_base_sec))
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        delay = self.backoff_base_sec * (2 ** max(0, int(attempt)))
        if retry_after not in (None, ""):
            try:
                header_delay = float(str(retry_after).strip())
            except Exception:
                header_delay = None
            if header_delay is not None and math.isfinite(header_delay):
                delay = max(delay, max(0.0, header_delay))
        return delay

    def _is_retryable_http_status(self, status_code: int) -> bool:
        return int(status_code) in {408, 429} or 500 <= int(status_code) <= 599

    def _is_retryable_bybit_error(self, ret_code: int, ret_msg: str) -> bool:
        msg = str(ret_msg or "").lower()
        if int(ret_code) in RETRYABLE_BYBIT_RETCODES:
            return True
        return any(
            token in msg
            for token in (
                "too many",
                "rate limit",
                "system busy",
                "server error",
                "service unavailable",
                "timeout",
                "temporarily unavailable",
            )
        )

    def _is_retryable_runtime_error(self, exc: RuntimeError) -> bool:
        msg = str(exc).lower()
        return any(
            token in msg
            for token in (
                "retryable upstream error",
                "response decode error",
                "response shape error",
            )
        )

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(url, params=params)
                retry_after = _header_value(response, "Retry-After") if response is not None else None
                if self._is_retryable_http_status(response.status_code):
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, retry_after))
                        continue
                    raise RuntimeError(f"Bybit HTTP {response.status_code}: retryable upstream error")
                response.raise_for_status()
                try:
                    data = response.json()
                except Exception as exc:
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, retry_after))
                        continue
                    raise RuntimeError("Bybit response decode error") from exc
                if not isinstance(data, dict):
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, retry_after))
                        continue
                    raise RuntimeError("Bybit response shape error: expected JSON object")

                ret_code_raw = data.get("retCode", 0)
                try:
                    ret_code = int(ret_code_raw or 0)
                except Exception as exc:
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, retry_after))
                        continue
                    raise RuntimeError(f"Bybit response shape error: invalid retCode={ret_code_raw!r}") from exc
                if ret_code != 0:
                    ret_msg = str(data.get("retMsg") or "")
                    if attempt < self.max_retries and self._is_retryable_bybit_error(ret_code, ret_msg):
                        time.sleep(self._retry_delay(attempt, _header_value(response, "Retry-After")))
                        continue
                    raise RuntimeError(f"Bybit error {ret_code}: {ret_msg}")
                return data
            except Exception as exc:
                last_exc = exc
                retryable = False
                if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
                    retryable = True
                elif isinstance(exc, RuntimeError):
                    retryable = self._is_retryable_runtime_error(exc)
                if attempt >= self.max_retries or not retryable:
                    raise
                time.sleep(self._retry_delay(attempt))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Bybit request failed")

    def get_tickers(self, category: str, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/v5/market/tickers", params=params)
        return _result_list(data)

    def get_kline(
        self,
        category: str,
        symbol: str,
        interval: str = "1",
        limit: int = 200,
        start: int | None = None,
        end: int | None = None,
    ) -> list[list[str]]:
        params: dict[str, str] = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "limit": str(max(1, min(int(limit), 1000))),
        }
        if start is not None:
            params["start"] = str(int(start))
        if end is not None:
            params["end"] = str(int(end))
        data = self._get("/v5/market/kline", params=params)
        result = _mapping_or_none(data.get("result")) or {}
        items = result.get("list")
        return items if isinstance(items, list) else []

    def get_funding_rate(self, symbol: str) -> dict[str, Any] | None:
        """Current funding rate from linear tickers endpoint."""
        data = self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        items = _result_list(data)
        if not items:
            return None
        ticker = items[0]
        next_funding_raw = _safe_int(ticker.get("nextFundingTime") or 0)
        next_funding_ts = next_funding_raw // 1000 if next_funding_raw > 10**11 else next_funding_raw
        funding_rate = _safe_float(ticker.get("fundingRate"))
        return {
            "symbol": symbol,
            "funding_rate": funding_rate,
            "next_funding_ts": next_funding_ts,
        }

    def get_open_interest_page(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 48,
        start_ms: int | None = None,
        end_ms: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Historical open interest page for a linear perpetual.
        interval: 5min / 15min / 30min / 1h / 4h / 1d
        Returns sanitized rows plus optional pagination cursor.
        """
        params: dict[str, str] = {
            "category": "linear",
            "symbol": symbol,
            "intervalTime": interval,
            "limit": str(max(1, min(int(limit), 200))),
        }
        if start_ms is not None:
            params["startTime"] = str(int(start_ms))
        if end_ms is not None:
            params["endTime"] = str(int(end_ms))
        if cursor:
            params["cursor"] = str(cursor)
        data = self._get(
            "/v5/market/open-interest",
            params,
        )
        result = _mapping_or_none(data.get("result")) or {}
        items = result.get("list")
        out: list[dict[str, Any]] = []
        if isinstance(items, list):
            for row in items:
                if not isinstance(row, Mapping):
                    continue
                ts_raw = _safe_int(row.get("timestamp"))
                oi = _safe_float(row.get("openInterest"))
                if ts_raw <= 0 or oi is None or oi < 0:
                    continue
                ts = ts_raw // 1000 if ts_raw > 10**11 else ts_raw
                out.append({"ts": ts, "oi": oi})
        next_cursor = result.get("nextPageCursor") or result.get("cursor") or None
        if next_cursor is not None:
            next_cursor = str(next_cursor)
            if not next_cursor.strip():
                next_cursor = None
        return out, next_cursor

    def get_open_interest(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 48,
        start_ms: int | None = None,
        end_ms: int | None = None,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        rows, _cursor = self.get_open_interest_page(
            symbol,
            interval=interval,
            limit=limit,
            start_ms=start_ms,
            end_ms=end_ms,
            cursor=cursor,
        )
        return rows

    def get_instrument_info(self, category: str, symbol: str) -> dict[str, Any] | None:
        """Metadata for a single instrument (tick size, lot size, etc.)."""
        data = self._get("/v5/market/instruments-info", {"category": category, "symbol": symbol})
        items = _result_list(data)
        return items[0] if items else None
